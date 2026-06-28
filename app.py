import os
import re
import logging
import html
import base64
import uuid

import requests
from flask import Flask, request, session, Response, jsonify

try:
    from groq import Groq
except Exception:  # pragma: no cover
    Groq = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pastilles")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "pastilles-demo-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

MAX_IMAGES_PER_SESSION = 6
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 6 * 1024 * 1024
session_images = {}

@app.after_request
def add_security_headers(response):
    # Safe, simple security headers. These improve browser trust/security without
    # breaking the chat widget or images/videos loaded from Pastilles' current site.
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response

# ---- CONFIG (set in Render -> Environment) --------------------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FALLBACK_MODEL = os.environ.get("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "Pastilles Assistant <onboarding@resend.dev>")
NOTIFY_TO = os.environ.get("NOTIFY_TO", "andradudan4@gmail.com")
DEBUG_KEY = os.environ.get("DEBUG_KEY", "pastillestest")

BUSINESS = "Pastilles Painting & Decorating"
PHONE = "07360 094 318"
EMAIL = "pastillespainting@gmail.com"

SYSTEM_PROMPT = f"""You are the friendly assistant for {BUSINESS}, a trusted family-run painting and decorating company in Southampton, run by Sam.

ABOUT THE BUSINESS:
- Family-run, established 1998. Founded by Sam's late mother Sue and passed down to Sam, who runs it today with his team.
- Specialist in HVLP spray finishing - less overspray, a beautifully smooth factory-quality finish on skirting, architrave, doors and staircases.
- Services: interior & exterior painting, traditional painting & decorating, commercial painting, specialist spray painting, sanding & varnishing, furniture painting, wallpapering.
- Trusted on big commercial jobs too - Butlins (Bognor Regis), Fareham Shopping Centre, Poole Dolphin Centre and RDL Commercial.
- Known for tidy, careful work, great communication, fair prices and lots of repeat custom.
- Areas: Southampton, Hampshire and Surrey - including Winchester, Eastleigh, Fareham and Havant.
- Contact: phone {PHONE}, email {EMAIL}.

YOUR JOB:
- Be warm, helpful and down-to-earth. Keep replies short (1-3 sentences). Plain British English.
- Answer questions about the work, spray finishing, areas covered, etc.
- Main goal: capture an enquiry so Sam can give a FREE, no-obligation quote.
- Work through the enquiry like a good local tradesperson. Ask one thing at a time and remember what they already answered.

CONVERSATION FLOW - ask these in order:
1. What needs doing: interior, exterior, spray finishing, wallpapering, commercial work, etc.
2. Scope: how many rooms, rough size, woodwork/stairs/doors, condition, or any useful details.
3. Photos or visit: ask if they have a couple of photos they can send by WhatsApp/email, or say Sam can arrange a visit if easier.
4. Area: town/postcode/part of Hampshire or Surrey.
5. Budget: ask for a rough budget if they have one. If they don't know or don't want to say, that's fine - move on.
6. Timing/urgency: when they want it done and whether it is urgent.
7. Name and contact: get their name and a phone number. Their name and phone number are essential for a proper lead; email can be extra.
8. Confirm the phone number by repeating it back and asking if it is correct.
9. Only after the customer has confirmed the phone/contact, wrap up and say Sam will be in touch with a free, no-obligation quote.

QUOTING:
- Do NOT give exact prices yourself - every job differs, so Sam gives a free quote. Reassure it's free and no-obligation.

STYLE RULES:
- Accept short, casual or misspelled answers and move on - never re-ask something answered.
- Ask only one thing at a time unless two tiny details naturally fit together.
- Never output [[READY]] just because they gave a phone number.
- Only output [[READY]] once you have ASKED about all essentials: job, scope, photos/visit, area, budget, timing/urgency, name, phone number, and you have repeated the phone number/contact back for confirmation.
- If you have the phone number but not the customer's name, ask for their name before confirming or finishing.
- The token [[READY]] is hidden and stripped before the customer sees it. Put it on its own line at the very end of the final wrap-up message.
"""

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+44|0)\d[\d\s\-\.]{8,11}(?!\d)")
YES_RE = re.compile(r"\b(yes|yeah|yep|correct|right|that's right|thats right|perfect|confirm|confirmed)\b", re.I)
NAME_PREFIX_RE = re.compile(r"\b(?:my name is|name is|i am|i'm|im|this is)\s+([A-Za-z][A-Za-z' -]{1,40})", re.I)
AREA_RE = re.compile(r"\b(southampton|winchester|eastleigh|fareham|havant|portsmouth|hampshire|surrey|bognor|poole|romsey|totton|lymington|andover|chichester)\b", re.I)
BUDGET_RE = re.compile(r"(?:£\s?\d|budget|around\s+\d|about\s+\d|\d+\s?(?:pounds|quid|gbp))", re.I)
TIMING_RE = re.compile(r"\b(urgent|asap|soon|this week|next week|month|no rush|not urgent|flexible|before|after|timeline|timing)\b", re.I)


def clean_name(value):
    value = re.split(r"\b(?:and|phone|number|email|tel|mobile|budget|area)\b", value or "", 1, flags=re.I)[0]
    value = re.sub(r"[^A-Za-z' -]", " ", value or "").strip()
    value = re.sub(r"\s+", " ", value)
    words = [w for w in value.split() if len(w) > 1]
    if not words:
        return None
    name = " ".join(words[:3]).strip()
    bad = {"yes", "yeah", "yep", "correct", "right", "thanks", "hello", "hi", "budget", "painting", "decorating", "no rush", "not urgent", "urgent"}
    if name.lower() in bad:
        return None
    return name.title()


def customer_text(messages):
    return " ".join(m["content"] for m in messages if m.get("role") == "user")


def find_email(messages):
    match = EMAIL_RE.search(customer_text(messages))
    return match.group(0) if match else None


def find_name(messages):
    text = customer_text(messages)
    explicit = NAME_PREFIX_RE.search(text)
    if explicit:
        name = clean_name(explicit.group(1))
        if name:
            return name

    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        prompt = msg.get("content", "").lower()
        if "name" not in prompt:
            continue
        for reply in messages[i + 1:]:
            if reply.get("role") != "user":
                continue
            content = EMAIL_RE.sub(" ", reply.get("content", ""))
            content = PHONE_RE.sub(" ", content)
            content = re.sub(r"\b(no rush|not urgent|urgent|asap|flexible|this week|next week|soon)\b", " ", content, flags=re.I)
            parts = [p for p in re.split(r"[.,;|]", content) if p.strip()]
            content = parts[-1] if parts else content
            name = clean_name(content)
            if name:
                return name
            break
    return None


def find_phone(messages):
    for candidate in PHONE_RE.findall(customer_text(messages)):
        digits = re.sub(r"\D", "", candidate)
        if digits.startswith("00"):
            continue
        if digits.startswith("44"):
            digits = "0" + digits[2:]
        if len(digits) == 11 and digits.startswith("0"):
            return f"{digits[:5]} {digits[5:]}"
    return None


def has_contact_info(messages) -> bool:
    return bool(find_phone(messages) or find_email(messages))


def phone_confirmed(messages) -> bool:
    """Require the assistant to ask for confirmation and the customer to agree."""
    phone = find_phone(messages)
    if not phone:
        return False
    phone_digits = re.sub(r"\D", "", phone)
    asked_at = None
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        text = msg.get("content", "").lower()
        msg_digits = re.sub(r"\D", "", text)
        if ("confirm" in text or "correct" in text or "right" in text) and phone_digits[-6:] in msg_digits:
            asked_at = i
    if asked_at is None:
        return False
    return any(
        msg.get("role") == "user" and YES_RE.search(msg.get("content", ""))
        for msg in messages[asked_at + 1:]
    )


def has_job_details(messages):
    text = customer_text(messages).lower()
    return bool(
        answer_after_prompt(messages, ["what needs doing", "looking to get done"]) or
        re.search(r"\b(interior|exterior|painting|decorating|spray|wallpaper|stair|door|woodwork|commercial|room|wall)\b", text)
    )


def has_scope_details(messages):
    text = customer_text(messages).lower()
    return bool(
        answer_after_prompt(messages, ["roughly how big", "how many", "rough size", "scope"]) or
        re.search(r"\b(room|rooms|wall|walls|door|doors|front|outside|stair|stairs|whole house|hallway|ceiling|woodwork)\b", text)
    )


def has_photo_context(messages, image_count=0):
    text = customer_text(messages).lower()
    return bool(image_count or re.search(r"\b(photo|photos|picture|pictures|attached|visit|come round|look at it)\b", text))


def has_area_details(messages):
    return bool(AREA_RE.search(customer_text(messages)) or answer_after_prompt(messages, ["town", "postcode", "area", "whereabouts"]))


def has_budget_details(messages):
    text = customer_text(messages).lower()
    return bool(BUDGET_RE.search(text) or re.search(r"\b(no budget|not sure|don't know|dont know|unsure|need a quote)\b", text))


def has_timing_details(messages):
    text = customer_text(messages).lower()
    return bool(TIMING_RE.search(text) or re.search(r"\b(today|tomorrow|asap|this weekend|next few days|next month)\b", text))


def lead_details_complete(messages, image_count=0):
    return bool(
        has_job_details(messages) and
        has_scope_details(messages) and
        has_photo_context(messages, image_count) and
        has_area_details(messages) and
        has_budget_details(messages) and
        has_timing_details(messages) and
        find_name(messages) and
        find_phone(messages) and
        phone_confirmed(messages)
    )


def closing_reply(text):
    return bool(re.search(r"\b(sam will be in touch|send this over|sent over|free quote|no-obligation quote|that's everything|thats everything)\b", text or "", re.I))


def lead_can_send(messages, lead_ready: bool, image_count=0) -> bool:
    return bool((lead_ready or closing_reply(messages[-1].get("content", ""))) and lead_details_complete(messages, image_count))


def missing_lead_reply(messages, image_count=0):
    if not has_job_details(messages):
        return "Before I send it over, what exactly needs doing — interior, exterior, spray finishing or something else?"
    if not has_scope_details(messages):
        return "Could you give Sam a rough idea of the size — how many rooms, walls, doors or areas need doing?"
    if not has_photo_context(messages, image_count):
        return "Have you got a couple of photos you can attach here, or would you rather Sam arranges a visit?"
    if not has_area_details(messages):
        return "What town or postcode is the job in?"
    if not has_budget_details(messages):
        return "Do you have a rough budget in mind? No worries if not — just say if you're not sure yet."
    if not has_timing_details(messages):
        return "How soon are you hoping to get it done — urgent, tomorrow, this week, or no rush?"
    if not find_name(messages):
        return "Almost there — could I grab your name for Sam?"
    phone = find_phone(messages)
    if not phone:
        return "Could I grab the best phone number for Sam to contact you on? He'll use it for the free quote."
    if not phone_confirmed(messages):
        return f"Just to confirm, is {phone} the right number for Sam to contact you on?"
    return None


def transcript_from(messages):
    return "\n".join(
        ("Customer: " if m["role"] == "user" else "Assistant: ") + m["content"]
        for m in messages if m.get("role") in ("user", "assistant")
    )


def ensure_session_id():
    sid = session.get("sid")
    if not sid:
        sid = uuid.uuid4().hex
        session["sid"] = sid
    return sid


def parse_image(data_url):
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    try:
        header, b64 = data_url.split(",", 1)
    except ValueError:
        return None
    if ";base64" not in header:
        return None
    content_type = header[len("data:"):].split(";", 1)[0].lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        return None
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]
    return {
        "filename": f"pastilles-job-photo-{uuid.uuid4().hex[:8]}.{ext}",
        "content_type": content_type,
        "b64": base64.b64encode(raw).decode("ascii"),
    }


def scripted_reply(messages):
    """A sensible lead-capture flow used when Groq is unavailable, so the
    assistant always works and never goes silent."""
    all_user = customer_text(messages).strip()
    all_chat = transcript_from(messages).lower()
    phone = find_phone(messages)
    if not all_user or len(all_user) < 8:
        return "No problem — what needs doing? Interior, exterior, spray finishing, wallpapering, or something else?"
    if "scope" not in all_chat and "how many" not in all_chat and "rough size" not in all_chat:
        return "Got it. Roughly how big is the job — how many rooms, doors, stairs, or areas need doing?"
    if "photo" not in all_chat and "visit" not in all_chat:
        return "Have you got a couple of photos you can send by WhatsApp or email, or would you rather Sam arranges a visit?"
    if "area" not in all_chat and "postcode" not in all_chat and "whereabouts" not in all_chat:
        return "What town or postcode is the job in?"
    if "budget" not in all_chat:
        return "Do you have a rough budget in mind? No worries if not — it just helps Sam tailor the quote."
    if "urgent" not in all_chat and "how soon" not in all_chat and "timing" not in all_chat:
        return "How soon are you hoping to get it done — urgent, or no particular rush?"
    if not find_name(messages):
        return "Perfect. What name should Sam ask for?"
    if not phone:
        return "Could I grab the best phone number so Sam can contact you about the free quote?"
    if not phone_confirmed(messages):
        return f"Just to confirm, is {phone} the right number for Sam to contact you on?"
    return ("Brilliant — that's everything Sam needs. I'll send this over now and he'll be in touch "
            "about your free, no-obligation quote.\n[[READY]]")


def groq_reply(messages):
    if Groq is None or not GROQ_API_KEY:
        return scripted_reply(messages)
    full = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    for model in (GROQ_MODEL, GROQ_FALLBACK_MODEL):
        try:
            client = Groq(api_key=GROQ_API_KEY)
            resp = client.chat.completions.create(
                model=model, messages=full, temperature=0.4, max_tokens=320)
            out = (resp.choices[0].message.content or "").strip()
            if out:
                return out
            log.warning("Groq model %s returned empty content; trying fallback", model)
        except Exception as e:
            log.error("Groq call failed on %s: %s", model, e)
    return scripted_reply(messages)


def answer_after_prompt(messages, keywords):
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        prompt = msg.get("content", "").lower()
        if not any(k in prompt for k in keywords):
            continue
        for reply in messages[i + 1:]:
            if reply.get("role") != "user":
                continue
            text = reply.get("content", "").strip()
            if text and "attached a photo" not in text.lower() and not YES_RE.fullmatch(text):
                return text
            break
    return None


def lead_fields(messages, image_count=0):
    text = customer_text(messages)
    area = None
    area_match = AREA_RE.search(text)
    if area_match:
        area = area_match.group(0).title()
    budget = None
    budget_match = BUDGET_RE.search(text)
    if budget_match:
        budget = budget_match.group(0)
    timing = None
    timing_match = TIMING_RE.search(text)
    if timing_match:
        timing = timing_match.group(0)

    return {
        "Name": find_name(messages),
        "Phone": find_phone(messages),
        "Email": find_email(messages),
        "Job / work wanted": answer_after_prompt(messages, ["what needs doing", "looking to get done"]) or "See conversation",
        "Scope": answer_after_prompt(messages, ["roughly how big", "how many", "rough size"]) or "See conversation",
        "Photos": f"{image_count} attached" if image_count else answer_after_prompt(messages, ["photo", "visit"]) or "Not attached yet",
        "Area": area or answer_after_prompt(messages, ["town", "postcode", "area", "whereabouts"]),
        "Budget": budget or answer_after_prompt(messages, ["budget"]),
        "Timing / urgency": timing or answer_after_prompt(messages, ["how soon", "urgent", "timing"]),
    }


def urgency_level(messages):
    text = customer_text(messages).lower()
    if re.search(r"\b(tomorrow|asap|urgent|emergency|today|tonight|this weekend)\b", text):
        return 5, "Level 5 - urgent", "#ffebee", "#b71c1c"
    if re.search(r"\b(this week|next few days|few days|before friday|before monday|soon)\b", text):
        return 4, "Level 4 - fairly urgent", "#fff3e0", "#e65100"
    if re.search(r"\b(next week|couple of weeks|within two weeks|2 weeks)\b", text):
        return 3, "Level 3 - normal", "#fff8e1", "#a16207"
    if re.search(r"\b(next month|month|flexible|whenever|no rush|not urgent)\b", text):
        return 1, "Level 1 - no rush", "#e8f5e9", "#2e7d32"
    return 2, "Level 2 - low/unknown", "#eef2ff", "#3730a3"


def email_row(label, value):
    value = value or "Not captured"
    return (
        "<tr>"
        f"<td style='padding:13px 18px;border-bottom:1px solid #e9dfcc;color:#817766;"
        f"font-size:13px;vertical-align:top;width:168px'>{html.escape(label)}</td>"
        f"<td style='padding:13px 18px;border-bottom:1px solid #e9dfcc;color:#111827;"
        f"font-size:15px;font-weight:700'>{html.escape(str(value))}</td>"
        "</tr>"
    )


def transcript_html(messages):
    rows = []
    for msg in messages:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        who = "Customer" if role == "user" else "Pastilles Assistant"
        accent = "#c6a253" if role == "assistant" else "#0c1a2b"
        bg = "#f6f1e7" if role == "user" else "#ffffff"
        text = html.escape(msg.get("content", "")).replace("\n", "<br>")
        rows.append(
            "<div style='margin:0 0 14px'>"
            f"<div style='font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:{accent};"
            f"font-weight:800;margin-bottom:6px'>{who}</div>"
            f"<div style='background:{bg};border:1px solid #e9dfcc;border-radius:10px;"
            f"padding:12px 15px;font-size:14px;line-height:1.55;color:#1f2937'>{text}</div>"
            "</div>"
        )
    return "".join(rows)


def lead_email_html(fields, messages, image_count):
    rows = "".join(email_row(label, value) for label, value in fields.items())
    level, urgency_label, urgency_bg, urgency_fg = urgency_level(messages)
    photo_note = (
        f"<p style='margin:0 0 22px;color:#15202e;font-size:14px'><strong>{image_count} customer photo(s)</strong> attached to this email.</p>"
        if image_count else
        "<p style='margin:0 0 22px;color:#5d6877;font-size:14px'>No photos attached yet. If the customer adds photos later, they will be forwarded separately.</p>"
    )
    return (
        "<!doctype html><html><body style='margin:0;background:#efe7d6;padding:26px;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif'>"
        "<div style='max-width:680px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;"
        "box-shadow:0 10px 35px rgba(12,26,43,.12)'>"
        "<div style='background:#0c1a2b;padding:28px 32px;border-bottom:4px solid #c6a253'>"
        "<div style='color:#c6a253;font-size:12px;letter-spacing:.28em;text-transform:uppercase;font-weight:800'>Pastilles</div>"
        "<div style='color:#fff;font-size:28px;font-weight:800;line-height:1.15;margin-top:8px'>New lead from your website</div>"
        "<div style='color:rgba(255,255,255,.72);font-size:14px;margin-top:8px'>Captured by the Pastilles quote assistant</div>"
        "</div>"
        "<div style='padding:30px 32px'>"
        "<p style='margin:0 0 22px;color:#5d6877;font-size:15px'>Here are the details to follow up with for a free, no-obligation quote:</p>"
        f"<div style='margin:0 0 22px'><div style='font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#817766;font-weight:800;margin-bottom:8px'>Urgency</div>"
        f"<span style='display:inline-block;background:{urgency_bg};color:{urgency_fg};border:1px solid {urgency_fg};border-radius:999px;padding:7px 15px;font-size:14px;font-weight:800'>{urgency_label}</span></div>"
        f"{photo_note}"
        f"<table style='width:100%;border-collapse:collapse;border:1px solid #e9dfcc;border-radius:10px;overflow:hidden;margin-bottom:30px'>{rows}</table>"
        "<div style='font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:#817766;font-weight:800;margin-bottom:14px'>Full conversation</div>"
        f"{transcript_html(messages)}"
        "</div>"
        "<div style='background:#f6f1e7;padding:18px 32px;border-top:1px solid #e9dfcc;color:#817766;font-size:12px'>"
        "Sent automatically by the Pastilles Painting & Decorating website assistant.</div>"
        "</div></body></html>"
    )


def send_lead_email(messages_or_transcript, recap, images=None):
    images = images or []
    if not RESEND_API_KEY:
        log.warning("LEAD captured but RESEND_API_KEY not set. Recap: %s", recap)
        return False
    if isinstance(messages_or_transcript, list):
        messages = messages_or_transcript
        transcript = transcript_from(messages)
    else:
        transcript = str(messages_or_transcript)
        messages = [{"role": "user", "content": transcript}]
    fields = lead_fields(messages, len(images))
    level, urgency_label, _, _ = urgency_level(messages)
    bits = [fields.get("Name"), fields.get("Area"), fields.get("Phone")]
    subject_tail = " · ".join(str(b) for b in bits if b)
    urgent_prefix = "URGENT L5 - " if level >= 5 else ("Priority L4 - " if level >= 4 else "")
    subject = urgent_prefix + "New Pastilles lead" + (f" - {subject_tail}" if subject_tail else "")
    html_body = lead_email_html(fields, messages, len(images))
    text = f"New enquiry from the Pastilles website\n\nUrgency: {urgency_label}\n\nSummary:\n{recap}\n"
    if images:
        text += f"\nPhotos attached: {len(images)}\n"
    text += f"\nConversation:\n{transcript}"
    payload = {"from": MAIL_FROM, "to": [NOTIFY_TO], "subject": subject, "html": html_body, "text": text}
    if images:
        payload["attachments"] = [{"filename": img["filename"], "content": img["b64"]} for img in images]
    try:
        r = requests.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=20)
        if 200 <= r.status_code < 300:
            log.info("LEAD email sent OK id=%s", r.json().get("id"))
            return True
        log.error("LEAD email failed status=%s body=%s", r.status_code, r.text[:400])
        return False
    except Exception as e:  # pragma: no cover
        log.error("LEAD email exception: %s", e)
        return False


def send_photo_followup(image):
    if not RESEND_API_KEY:
        return False
    html_body = (
        "<div style='font-family:Arial,sans-serif;color:#111827'>"
        "<h2 style='color:#0c1a2b'>Extra photo for a Pastilles enquiry</h2>"
        "<p>A customer just added another photo to the enquiry you already received. "
        "It's attached to this email.</p>"
        "<p style='color:#888;font-size:13px'>Sent automatically by the Pastilles assistant.</p></div>"
    )
    payload = {
        "from": MAIL_FROM, "to": [NOTIFY_TO],
        "subject": "Extra photo - Pastilles enquiry",
        "html": html_body,
        "text": "A customer added another photo to their Pastilles enquiry. It's attached.",
        "attachments": [{"filename": image["filename"], "content": image["b64"]}],
    }
    try:
        r = requests.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json=payload, timeout=20)
        return 200 <= r.status_code < 300
    except Exception as e:  # pragma: no cover
        log.error("Photo follow-up email failed: %s", e)
        return False


@app.route("/chat", methods=["POST"])
def chat():
    log.info("CHAT endpoint hit")
    sid = ensure_session_id()
    data = request.get_json(force=True, silent=True) or {}
    history = [m for m in data.get("messages", [])
               if m.get("role") in ("user", "assistant") and m.get("content")][-16:]
    reply = groq_reply(history)
    lead_ready = "[[READY]]" in reply
    clean = reply.replace("[[READY]]", "").strip()
    conversation_for_email = history + [{"role": "assistant", "content": clean}]
    images = session_images.get(sid, [])
    wants_finish = lead_ready or closing_reply(clean)
    missing = missing_lead_reply(conversation_for_email, len(images)) if wants_finish else None
    if missing:
        clean = missing
        wants_finish = False
        conversation_for_email = history + [{"role": "assistant", "content": clean}]
    if wants_finish and lead_details_complete(conversation_for_email, len(images)) and not session.get("lead_sent"):
        recap = clean or "Website enquiry - see conversation below."
        ok = send_lead_email(conversation_for_email, recap, images)
        if ok:
            session["lead_sent"] = True
        log.info("Lead trigger fired (emailed=%s)", ok)
    elif wants_finish and not session.get("lead_sent"):
        log.info("Lead handoff withheld: missing required lead details")
    return jsonify({"reply": clean})


@app.route("/upload", methods=["POST"])
def upload():
    sid = ensure_session_id()
    images = session_images.setdefault(sid, [])
    if len(images) >= MAX_IMAGES_PER_SESSION:
        return jsonify({"reply": "Thanks — that's plenty of photos for now. I'll keep those with your enquiry for Sam."})
    data = request.get_json(force=True, silent=True) or {}
    image = parse_image(data.get("image"))
    if not image:
        return jsonify({"reply": "Sorry, I couldn't read that photo. Please try a JPG, PNG or WebP image."}), 400
    images.append(image)
    if session.get("lead_sent"):
        sent = send_photo_followup(image)
        log.info("Photo follow-up fired (emailed=%s)", sent)
    return jsonify({
        "reply": "Thanks, got the photo — that really helps Sam see the job. You can add another, or carry on with the details."
    })


@app.route("/_debug/email")
def debug_email():
    if request.args.get("key") != DEBUG_KEY:
        return Response("nope", status=403)
    if request.args.get("send") == "1":
        ok = send_lead_email("Customer: test\nAssistant: test reply", "TEST lead - debug route.")
        return jsonify({"resend_key_set": bool(RESEND_API_KEY), "mail_from": MAIL_FROM, "notify_to": NOTIFY_TO, "sent": ok})
    return jsonify({"resend_key_set": bool(RESEND_API_KEY), "groq_key_set": bool(GROQ_API_KEY),
                    "mail_from": MAIL_FROM, "notify_to": NOTIFY_TO, "hint": "add &send=1 to send a test email"})


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/")
def home():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pastilles Painting &amp; Decorating | Southampton, Hampshire &amp; Surrey</title>
<meta name="description" content="Pastilles Painting & Decorating - trusted family-run painters and decorators in Southampton, Hampshire & Surrey since 1998. HVLP spray finishing specialists. Free, no-obligation quotes.">
<meta name="theme-color" content="#0c1a2b">
<meta property="og:title" content="Pastilles Painting & Decorating">
<meta property="og:description" content="Exceptional finishes. Timeless spaces. Family-run painters & decorators since 1998.">
<link rel="icon" href="https://pastillespainting.co.uk/wp-content/uploads/2024/03/cropped-pastilles-logo-270x270.webp">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,600;0,700;1,500&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --navy:#0c1a2b;--navy2:#102a44;--paper:#f6f1e7;--paper2:#efe7d6;
    --gold:#c6a253;--gold2:#dcc488;--ink:#15202e;--muted:#5d6877;--line:#e3d9c6;
    --serif:'Playfair Display',Georgia,serif;--sans:'Inter',-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html{scroll-behavior:smooth}
  body{font-family:var(--sans);color:var(--ink);background:var(--paper);line-height:1.65;-webkit-font-smoothing:antialiased;overflow-x:hidden}
  .wrap{max-width:1140px;margin:0 auto;padding:0 24px}
  h1,h2,h3{font-family:var(--serif);font-weight:700;line-height:1.12;letter-spacing:-.01em}
  a{color:inherit;text-decoration:none}
  .gold{color:var(--gold)}
  .btn{display:inline-block;background:var(--gold);color:#1a130a;font-weight:600;font-family:var(--sans);padding:14px 26px;border-radius:2px;border:none;cursor:pointer;font-size:15px;letter-spacing:.02em;transition:.2s}
  .btn:hover{background:var(--gold2);transform:translateY(-2px)}
  .btn.outline{background:transparent;color:#fff;border:1px solid rgba(255,255,255,.5)}
  .btn.outline:hover{border-color:var(--gold);color:var(--gold)}
  .eyebrow{font-family:var(--sans);font-size:12.5px;font-weight:600;letter-spacing:.22em;text-transform:uppercase;color:var(--gold)}
  .reveal{opacity:0;transform:translateY(22px);transition:opacity .7s ease,transform .7s ease}
  .reveal.in{opacity:1;transform:none}

  nav{position:sticky;top:0;z-index:50;background:rgba(12,26,43,.92);backdrop-filter:blur(10px);border-bottom:1px solid rgba(198,162,83,.22)}
  nav .row{display:flex;align-items:center;justify-content:space-between;padding:13px 24px;max-width:1140px;margin:0 auto}
  nav .brand{display:flex;align-items:center;gap:12px}
  nav .brand img{height:44px;width:auto;display:block}
  nav .brand .nm{font-family:var(--serif);color:#fff;font-size:19px;font-weight:600;letter-spacing:.04em;line-height:1}
  nav .brand .nm small{display:block;font-family:var(--sans);font-size:9px;letter-spacing:.26em;color:var(--gold);margin-top:3px}
  nav .links{display:flex;align-items:center;gap:26px}
  nav .links a{color:rgba(255,255,255,.8);font-size:14.5px;font-weight:500}
  nav .links a:hover{color:var(--gold)}
  nav .call{color:#fff !important;font-weight:600}
  nav .call span{color:var(--gold)}
  .burger{display:none;flex-direction:column;gap:5px;background:none;border:none;cursor:pointer;padding:6px}
  .burger span{width:24px;height:2px;background:#fff;display:block;transition:.2s}
  #mobnav{display:none;background:var(--navy);border-bottom:1px solid rgba(198,162,83,.2)}
  #mobnav a{display:block;color:rgba(255,255,255,.9);padding:13px 24px;border-top:1px solid rgba(255,255,255,.06);font-size:15px}
  #mobnav a:last-child{color:var(--gold);font-weight:600}

  header.hero{position:relative;background:var(--navy);color:#fff;overflow:hidden}
  header.hero::before{content:"";position:absolute;inset:0;background:radial-gradient(1200px 520px at 78% -10%,rgba(198,162,83,.18),transparent 60%),linear-gradient(180deg,#0c1a2b 0%,#0e2138 100%)}
  .hero .inner{position:relative;max-width:1140px;margin:0 auto;padding:92px 24px 100px;display:grid;grid-template-columns:1.05fr .95fr;gap:48px;align-items:center}
  .hero h1{font-size:58px;color:#fff;margin:18px 0 0}
  .hero h1 em{font-style:italic;color:var(--gold)}
  .hero p.lead{margin-top:22px;font-size:18px;color:rgba(255,255,255,.82);max-width:30em}
  .hero .cta{margin-top:34px;display:flex;gap:14px;flex-wrap:wrap}
  .hero .est{margin-top:28px;color:rgba(255,255,255,.62);font-size:13.5px}
  .hero .est b{color:var(--gold)}
  .hero-proof{margin-top:22px;display:grid;grid-template-columns:repeat(3,1fr);gap:10px;max-width:560px}
  .hero-proof span{border:1px solid rgba(198,162,83,.28);background:rgba(255,255,255,.05);padding:12px 14px;color:rgba(255,255,255,.86);font-size:13px;border-radius:4px}
  .hero-proof b{display:block;color:var(--gold);font-size:15px;margin-bottom:2px}
  .hero .frame{position:relative}
  .hero .frame img{width:100%;height:460px;object-fit:cover;border-radius:3px;border:1px solid rgba(198,162,83,.35)}
  .hero .frame .badge{position:absolute;left:-18px;bottom:-18px;background:var(--gold);color:#1a130a;font-family:var(--serif);font-weight:700;font-size:20px;padding:14px 18px;border-radius:2px;text-align:center;line-height:1}
  .hero .frame .badge small{display:block;font-family:var(--sans);font-size:10px;letter-spacing:.16em;font-weight:600;margin-top:5px}
  .hero .finish-card{position:absolute;right:-18px;top:28px;width:190px;background:#fff;color:var(--ink);border:1px solid rgba(198,162,83,.5);border-radius:6px;padding:16px;box-shadow:0 18px 38px rgba(0,0,0,.22)}
  .hero .finish-card b{display:block;font-family:var(--serif);font-size:20px;color:var(--navy);line-height:1.1}
  .hero .finish-card span{display:block;margin-top:7px;color:var(--muted);font-size:12.5px;line-height:1.35}

  .clients{background:var(--navy2);color:#fff;border-top:1px solid rgba(198,162,83,.2)}
  .clients .row{padding:20px 24px;max-width:1140px;margin:0 auto;display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:12px 30px}
  .clients .lbl{font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:var(--gold);font-weight:600}
  .clients .c{font-family:var(--serif);font-size:18px;color:rgba(255,255,255,.92);font-style:italic}
  .clients .dot{color:rgba(255,255,255,.3)}

  .quote-panel{background:#fff;border-bottom:1px solid var(--line)}
  .quote-panel .row{max-width:1140px;margin:0 auto;padding:24px;display:grid;grid-template-columns:1.1fr repeat(4,.7fr);gap:18px;align-items:center}
  .quote-panel h2{font-size:26px;color:var(--navy)}
  .quote-panel p{margin-top:6px;color:var(--muted);font-size:15px;line-height:1.5}
  .quote-panel .mini{background:var(--paper);border:1px solid var(--line);border-radius:6px;padding:16px;min-height:112px}
  .quote-panel .mini b{display:block;color:var(--navy);font-size:15px;margin-bottom:6px}
  .quote-panel .mini span{display:block;color:var(--muted);font-size:13.5px;line-height:1.45}
  .wow-strip{background:var(--navy);color:#fff}
  .wow-strip .row{max-width:1140px;margin:0 auto;padding:24px;display:grid;grid-template-columns:1.2fr repeat(3,1fr);gap:18px;align-items:center}
  .wow-strip h2{font-size:26px;color:#fff}
  .wow-strip p{color:rgba(255,255,255,.72);font-size:14.5px;margin-top:6px}
  .wow-strip .point{border-left:2px solid var(--gold);padding-left:14px;color:rgba(255,255,255,.88);font-size:14px}
  .wow-strip .point b{display:block;color:var(--gold);font-size:13px;text-transform:uppercase;letter-spacing:.12em;margin-bottom:4px}

  section{padding:88px 0}
  .head{max-width:700px;margin:0 auto 52px;text-align:center}
  .head h2{font-size:40px;margin-top:12px}
  .head p{margin-top:14px;color:var(--muted);font-size:17px}

  .stats{background:var(--paper2)}
  .stats .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:24px;text-align:center}
  .stats .n{font-family:var(--serif);font-size:48px;font-weight:700;color:var(--navy);line-height:1}
  .stats .n .gold{color:var(--gold)}
  .stats .l{margin-top:8px;font-size:14px;color:var(--muted);letter-spacing:.04em}

  .svc{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
  .svc .card{background:#fff;border:1px solid var(--line);border-radius:4px;padding:30px 26px;transition:.25s}
  .svc .card:hover{transform:translateY(-4px);box-shadow:0 18px 40px rgba(12,26,43,.1);border-color:var(--gold)}
  .svc .ic{width:48px;height:48px;border-radius:50%;background:var(--paper2);display:flex;align-items:center;justify-content:center;font-size:23px;margin-bottom:16px}
  .svc h3{font-size:21px}
  .svc p{margin-top:8px;color:var(--muted);font-size:15px}

  .feature{background:var(--navy);color:#fff}
  .feature .grid{display:grid;grid-template-columns:1fr 1fr;gap:54px;align-items:center}
  .feature img{width:100%;height:430px;object-fit:cover;border-radius:3px;border:1px solid rgba(198,162,83,.3)}
  .feature h2{font-size:38px;color:#fff;margin-top:14px}
  .feature p{margin-top:18px;color:rgba(255,255,255,.8);font-size:16.5px}
  .feature ul{margin-top:22px;list-style:none;display:grid;gap:12px}
  .feature li{display:flex;gap:12px;align-items:flex-start;color:rgba(255,255,255,.9)}
  .feature li::before{content:"\2713";color:var(--gold);font-weight:700}

  /* before / after */
  .ba-wrap{display:grid;grid-template-columns:1fr 1fr;gap:26px}
  .ba-block .cap{font-family:var(--serif);font-size:20px;margin-bottom:12px;display:flex;align-items:center;gap:10px}
  .ba-block .cap::before{content:"";width:22px;height:2px;background:var(--gold);display:inline-block}
  .ba{position:relative;overflow:hidden;border-radius:4px;border:1px solid var(--line);aspect-ratio:1/1;cursor:ew-resize;user-select:none;touch-action:none;background:#ddd}
  .ba .bimg{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;pointer-events:none}
  .ba .before{clip-path:inset(0 calc(100% - var(--p,50%)) 0 0)}
  .ba .tag{position:absolute;top:12px;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#fff;background:rgba(12,26,43,.7);padding:5px 10px;border-radius:30px;pointer-events:none}
  .ba .tag.a{right:12px}.ba .tag.b{left:12px}
  .ba .handle{position:absolute;top:0;bottom:0;left:var(--p,50%);width:3px;background:var(--gold);transform:translateX(-50%);pointer-events:none}
  .ba .handle::after{content:"\2194";position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:42px;height:42px;border-radius:50%;background:var(--gold);color:#1a130a;display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:700;box-shadow:0 4px 14px rgba(0,0,0,.3)}
  .storyline{margin:10px 0 0;color:var(--muted);font-size:14.5px;line-height:1.5}
  .case-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin-top:34px}
  .case{border:1px solid var(--line);background:#fff;border-radius:6px;padding:22px}
  .case .k{font-size:11px;text-transform:uppercase;letter-spacing:.18em;color:var(--gold);font-weight:700}
  .case h3{font-size:21px;margin-top:8px;color:var(--navy)}
  .case p{margin-top:8px;color:var(--muted);font-size:14.5px}

  /* video */
  .vids{background:var(--paper2)}
  .vid-grid{display:grid;grid-template-columns:1fr 1fr;gap:26px}
  .vid-block{background:#fff;border:1px solid var(--line);border-radius:6px;overflow:hidden}
  .vid-block video{width:100%;display:block;background:#000;aspect-ratio:16/9;object-fit:cover}
  .vid-block .v-cap{padding:16px 20px}
  .vid-block .v-cap h3{font-size:19px}
  .vid-block .v-cap p{color:var(--muted);font-size:14px;margin-top:4px}

  .reviews{background:#fff}
  .review-hero{display:grid;grid-template-columns:.8fr 1.2fr;gap:26px;align-items:center;margin:0 0 28px;background:var(--paper);border:1px solid var(--line);border-radius:8px;padding:26px}
  .review-hero .score{font-family:var(--serif);font-size:56px;line-height:1;color:var(--navy)}
  .review-hero .stars{color:var(--gold);letter-spacing:3px;margin-top:8px}
  .review-hero p{color:var(--ink);font-size:17px}
  .review-points{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}
  .review-points span{border:1px solid var(--line);background:#fff;border-radius:999px;padding:7px 12px;color:var(--muted);font-size:13px}
  .rev{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
  .rev .q{background:var(--paper);border:1px solid var(--line);border-radius:4px;padding:28px;display:flex;flex-direction:column}
  .rev .stars{color:var(--gold);letter-spacing:3px;font-size:15px}
  .rev p{margin-top:14px;font-size:15.5px;color:var(--ink);flex:1}
  .rev .who{margin-top:18px;font-weight:600;font-size:14px;color:var(--muted)}
  .rev .rev-photo{width:100%;height:190px;object-fit:cover;border-radius:6px;margin-bottom:16px;display:block}
  .rev .who .gv{color:#9aa0a6;font-weight:500}

  .areas{text-align:center;background:var(--paper2)}
  .areas .pills{display:flex;flex-wrap:wrap;justify-content:center;gap:12px;margin-top:8px}
  .areas .pills span{background:#fff;border:1px solid var(--line);border-radius:40px;padding:9px 20px;font-size:14.5px}

  .band{background:var(--navy);color:#fff;text-align:center;position:relative;overflow:hidden}
  .band::before{content:"";position:absolute;inset:0;background:radial-gradient(800px 380px at 50% -30%,rgba(198,162,83,.16),transparent 60%)}
  .band .wrap{position:relative}
  .band h2{font-size:42px;color:#fff}
  .band p{margin-top:14px;color:rgba(255,255,255,.8);font-size:17px}
  .band .cta{margin-top:30px;display:flex;gap:14px;justify-content:center;flex-wrap:wrap}
  .band .meta{margin-top:34px;display:flex;gap:30px;justify-content:center;flex-wrap:wrap;color:rgba(255,255,255,.85);font-size:15px}
  .band .meta a:hover{color:var(--gold)}

  footer{background:#081320;color:rgba(255,255,255,.6);font-size:13.5px}
  footer .row{display:flex;justify-content:space-between;flex-wrap:wrap;gap:14px;padding:30px 24px;max-width:1140px;margin:0 auto}
  footer .social{display:flex;gap:18px}
  footer a:hover{color:var(--gold)}

  #bub{position:fixed;right:20px;bottom:20px;z-index:60;background:var(--gold);color:#1a130a;border:none;border-radius:50px;padding:15px 22px;font-family:var(--sans);font-weight:600;font-size:15px;cursor:pointer;box-shadow:0 12px 30px rgba(12,26,43,.35);display:flex;align-items:center;gap:9px;animation:pop .4s ease}
  @keyframes pop{from{transform:scale(.7);opacity:0}to{transform:scale(1);opacity:1}}
  #bub:hover{background:var(--gold2)}
  #waWidget{position:fixed;left:20px;bottom:20px;z-index:60;width:54px;height:54px;border-radius:50%;background:#25D366;color:#fff;display:flex;align-items:center;justify-content:center;font-size:25px;box-shadow:0 12px 30px rgba(12,26,43,.28);transition:.2s}
  #waWidget:hover{transform:translateY(-2px);background:#1ebe5d}
  #waWidget span{position:absolute;left:62px;white-space:nowrap;background:#102a44;color:#fff;font-size:13px;font-weight:700;padding:8px 11px;border-radius:4px;opacity:0;pointer-events:none;transform:translateX(-6px);transition:.2s}
  #waWidget:hover span{opacity:1;transform:none}
  #chat{position:fixed;right:20px;bottom:20px;z-index:61;width:380px;max-width:calc(100vw - 32px);height:560px;max-height:calc(100vh - 40px);background:#fff;border-radius:14px;box-shadow:0 30px 70px rgba(12,26,43,.4);display:none;flex-direction:column;overflow:hidden}
  #chat header{background:var(--navy);color:#fff;padding:16px 18px;display:flex;align-items:center;gap:12px}
  #chat header .av{width:38px;height:38px;border-radius:50%;background:var(--gold);color:#1a130a;display:flex;align-items:center;justify-content:center;font-family:var(--serif);font-weight:700;font-size:18px}
  #chat header .t{font-weight:600;font-size:15px;line-height:1.2}
  #chat header .t small{display:block;color:rgba(255,255,255,.65);font-weight:400;font-size:12px}
  #chat header .x{margin-left:auto;background:none;border:none;color:rgba(255,255,255,.8);font-size:22px;cursor:pointer}
  #msgs{flex:1;min-height:0;overflow-y:auto;padding:18px;background:var(--paper);display:flex;flex-direction:column;gap:10px}
  #progress{background:#fff;border-bottom:1px solid var(--line);padding:11px 12px;display:grid;grid-template-columns:repeat(5,1fr);gap:6px}
  #progress span{height:8px;border-radius:999px;background:var(--paper2);position:relative;overflow:hidden}
  #progress span::after{content:attr(data-label);position:absolute;left:0;top:10px;font-size:10px;color:var(--muted)}
  #progress span.done{background:var(--gold)}
  #progressText{font-size:11px;color:var(--muted);background:#fff;padding:0 12px 10px;border-bottom:1px solid var(--line)}
  .m{max-width:82%;padding:11px 14px;border-radius:14px;font-size:14.5px;line-height:1.5;white-space:pre-wrap}
  .m.bot{background:#fff;border:1px solid var(--line);align-self:flex-start;border-bottom-left-radius:4px}
  .m.me{background:var(--navy);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
  .m.typing{color:var(--muted);font-style:italic}
  #quick{display:flex;gap:8px;flex-wrap:wrap;padding:10px 12px 0;background:var(--paper)}
  #quick button{border:1px solid var(--line);background:#fff;color:var(--navy);border-radius:999px;padding:8px 11px;font-family:var(--sans);font-size:12.5px;font-weight:600;cursor:pointer}
  #quick button:hover{border-color:var(--gold);color:#1a130a}
  #cbar{display:flex;gap:8px;padding:12px;border-top:1px solid var(--line);background:#fff}
  #inp{flex:1;border:1px solid var(--line);border-radius:24px;padding:12px 16px;font-size:16px;font-family:var(--sans);outline:none;min-width:0}
  #inp:focus{border-color:var(--gold)}
  #cbar button{background:var(--gold);border:none;border-radius:50%;width:44px;height:44px;cursor:pointer;font-size:18px;color:#1a130a;flex:none}
  #attach{width:44px;height:44px;border-radius:50%;background:var(--paper2);border:1px solid var(--line);display:flex;align-items:center;justify-content:center;cursor:pointer;flex:none;font-size:20px;color:var(--navy)}
  #attach:hover{border-color:var(--gold);background:#fff}
  #attach.busy{opacity:.45;pointer-events:none}
  #fileInp{display:none}
  .m.photo-msg{padding:5px;background:var(--navy)}
  .m.photo-msg img{display:block;max-width:190px;width:100%;border-radius:10px}

  /* lightbox */
  #lb{position:fixed;inset:0;background:rgba(8,15,25,.92);z-index:80;display:none;align-items:center;justify-content:center;padding:30px}
  #lb img{max-width:92vw;max-height:88vh;border-radius:4px;border:1px solid rgba(198,162,83,.4)}
  #lb .close{position:absolute;top:20px;right:26px;color:#fff;font-size:34px;cursor:pointer}


  .leaflet-sec{background:#fff;padding:72px 0}
  .leaflet-grid{display:grid;grid-template-columns:1fr .78fr;gap:44px;align-items:center}
  .leaflet-grid h2{font-size:36px;margin-top:12px;color:var(--navy)}
  .leaflet-grid p{margin-top:16px;color:var(--muted);font-size:16.5px}
  .mini-list{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px}
  .mini-list span{background:var(--paper);border:1px solid var(--line);border-radius:50px;padding:9px 14px;font-size:14px;color:var(--ink)}
  .leaflet-card{background:var(--paper);border:1px solid var(--line);border-radius:8px;padding:10px;box-shadow:0 18px 45px rgba(12,26,43,.10)}
  .leaflet-card img{width:100%;height:auto;border-radius:4px}

  /* google badge */
  .gbadge{display:inline-flex;align-items:center;gap:14px;background:#fff;border:1px solid var(--line);border-radius:50px;padding:11px 24px;margin:0 0 34px;box-shadow:0 8px 24px rgba(12,26,43,.06)}
  .gbadge .g{font-family:var(--serif);font-weight:700;font-size:24px;background:conic-gradient(from -30deg,#ea4335,#fbbc05,#34a853,#4285f4,#ea4335);-webkit-background-clip:text;background-clip:text;color:transparent}
  .gbadge .stars{color:var(--gold);letter-spacing:2px;font-size:14px}
  .gbadge b{color:var(--navy)}
  .gbadge .txt{font-size:14px;color:var(--muted)}

  /* how it works */
  .how{background:#fff}
  .steps{display:grid;grid-template-columns:repeat(3,1fr);gap:24px}
  .step{text-align:center;padding:10px}
  .step .num{width:66px;height:66px;border-radius:50%;background:var(--navy);color:var(--gold);font-family:var(--serif);font-size:26px;font-weight:700;display:flex;align-items:center;justify-content:center;margin:0 auto 18px;border:2px solid var(--gold)}
  .step h3{font-size:21px}
  .step p{margin-top:8px;color:var(--muted);font-size:15px}

  /* faq */
  .faq{background:var(--paper)}
  .faq .list{max-width:760px;margin:0 auto;display:grid;gap:12px}
  .faq details{background:#fff;border:1px solid var(--line);border-radius:6px;padding:0 22px;transition:.2s}
  .faq details[open]{border-color:var(--gold)}
  .faq summary{list-style:none;cursor:pointer;padding:18px 0;font-weight:600;font-size:16.5px;display:flex;justify-content:space-between;align-items:center;gap:14px}
  .faq summary::-webkit-details-marker{display:none}
  .faq summary::after{content:"+";color:var(--gold);font-size:24px;font-weight:400;flex:none;line-height:1}
  .faq details[open] summary::after{content:"\2212"}
  .faq details p{padding:0 0 18px;color:var(--muted);font-size:15px}

  /* sticky mobile bar */
  #mobar{display:none}

  /* our story */
  .story{background:var(--paper2)}
  .sgrid{display:grid;grid-template-columns:.9fr 1.1fr;gap:54px;align-items:center}
  .sgrid img{width:100%;height:430px;object-fit:cover;border-radius:3px;border:1px solid var(--line)}
  .story h2{font-size:38px;margin-top:12px}
  .story p{margin-top:16px;color:var(--ink);font-size:16.5px}
  .story .sig{margin-top:22px;font-family:var(--serif);font-style:italic;font-size:20px;color:var(--navy)}
  .story .sig span{display:block;font-family:var(--sans);font-style:normal;font-size:13px;color:var(--muted);letter-spacing:.04em;margin-top:2px}

  /* why choose us */
  .why{background:var(--navy);color:#fff}
  .why .head h2{color:#fff}
  .why .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
  .why .card{background:rgba(255,255,255,.04);border:1px solid rgba(198,162,83,.25);border-radius:6px;padding:26px;transition:.2s}
  .why .card:hover{border-color:var(--gold);background:rgba(198,162,83,.07)}
  .why .ic{width:46px;height:46px;border-radius:50%;background:rgba(198,162,83,.15);display:flex;align-items:center;justify-content:center;font-size:22px;color:var(--gold);margin-bottom:14px}
  .why h3{font-size:19px;color:#fff}
  .why p{margin-top:7px;color:rgba(255,255,255,.72);font-size:14.5px}

  /* gallery */
  .more{background:#fff}
  .grid-g{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
  .grid-g a{display:block;overflow:hidden;border-radius:3px;border:1px solid var(--line);cursor:pointer;aspect-ratio:1/1}
  .grid-g img{width:100%;height:100%;object-fit:cover;transition:.4s}
  .grid-g a:hover img{transform:scale(1.08)}

  @media(max-width:860px){
    nav .links{display:none}
    .burger{display:flex}
    .hero .inner{grid-template-columns:1fr;padding:60px 24px 68px}
    .hero h1{font-size:42px}
    .hero .frame{display:none}
    .hero-proof{grid-template-columns:1fr}
    .stats .grid{grid-template-columns:1fr 1fr;gap:30px 24px}
    .svc{grid-template-columns:1fr 1fr}
    .feature .grid{grid-template-columns:1fr;gap:30px}
    .feature img{height:300px;order:-1}
    .ba-wrap,.vid-grid,.rev{grid-template-columns:1fr}
    .steps{grid-template-columns:1fr;gap:16px}
    .sgrid{grid-template-columns:1fr;gap:28px}
    .sgrid img{height:300px;order:-1}
    .why .grid{grid-template-columns:1fr 1fr}
    .grid-g{grid-template-columns:1fr 1fr}
    .case-grid,.review-hero{grid-template-columns:1fr}
    .leaflet-grid{grid-template-columns:1fr;gap:26px}
    .leaflet-grid h2{font-size:30px}
    .quote-panel .row{grid-template-columns:1fr 1fr}
    .wow-strip .row{grid-template-columns:1fr 1fr}
    .quote-panel .intro{grid-column:1/-1}
    section{padding:60px 0}
    .head h2,.band h2,.feature h2{font-size:30px}
  }
  @media(max-width:560px){
    .svc{grid-template-columns:1fr}
    .hero h1{font-size:35px}
    .stats .n{font-size:40px}
    body{padding-bottom:74px}
    .clients .row{padding:14px 16px;gap:6px 14px}
    .clients .lbl{width:100%;text-align:center;margin-bottom:2px}
    .clients .c{font-size:15px}
    .quote-panel .row{grid-template-columns:1fr}
    .wow-strip .row{grid-template-columns:1fr}
    #chat{height:100dvh;max-height:100dvh;width:100vw;max-width:100vw;right:0;bottom:0;border-radius:0;padding-bottom:env(safe-area-inset-bottom)}
    #bub{display:none!important}
    #waWidget{right:14px;left:auto;bottom:calc(78px + env(safe-area-inset-bottom));width:50px;height:50px;font-size:23px}
    #waWidget span{display:none}
    #mobar{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:59;background:var(--navy);padding:10px 12px;gap:10px;padding-bottom:calc(10px + env(safe-area-inset-bottom));border-top:1px solid rgba(198,162,83,.3)}
    #mobar a,#mobar button{flex:1;text-align:center;padding:13px;border-radius:4px;font-weight:600;font-size:15px;font-family:var(--sans);border:none;cursor:pointer}
    #mobar a{background:transparent;color:#fff;border:1px solid rgba(255,255,255,.45)}
    #mobar button{background:var(--gold);color:#1a130a}
  }
</style>
</head>
<body>

<nav>
  <div class="row">
    <a class="brand" href="#top">
      <img src="https://pastillespainting.co.uk/wp-content/uploads/2024/03/pastilles-logo-cropped-300x237.png" alt="Pastilles logo">
      <span class="nm">PASTILLES<small>PAINTING &amp; DECORATING</small></span>
    </a>
    <div class="links">
      <a href="#services">Services</a>
      <a href="#spray">Spray Finishing</a>
      <a href="#work">Our Work</a>
      <a href="#reviews">Reviews</a>
      <a class="call" href="tel:+447360094318">📞 <span>07360 094 318</span></a>
      <button class="btn" onclick="openChat()">Free Quote</button>
    </div>
    <button class="burger" onclick="toggleNav()" aria-label="Menu"><span></span><span></span><span></span></button>
  </div>
  <div id="mobnav">
    <a href="#services" onclick="toggleNav()">Services</a>
    <a href="#spray" onclick="toggleNav()">Spray Finishing</a>
    <a href="#work" onclick="toggleNav()">Our Work</a>
    <a href="#reviews" onclick="toggleNav()">Reviews</a>
    <a href="tel:+447360094318">📞 Call 07360 094 318</a>
    <a href="#" onclick="toggleNav();openChat();return false;">Get a Free Quote</a>
  </div>
</nav>

<header class="hero" id="top">
  <div class="inner">
    <div>
      <div class="eyebrow">Est. 1998 · Southampton · Hampshire · Surrey</div>
      <h1>Exceptional finishes.<br><em>Timeless spaces.</em></h1>
      <p class="lead">A trusted family-run team of painters &amp; decorators, specialising in flawless HVLP spray finishes and beautiful interior &amp; exterior work — delivered tidily, carefully, and on time.</p>
      <div class="cta">
        <button class="btn" onclick="openChat()">Get a Free Quote</button>
        <a class="btn outline" href="tel:+447360094318">Call Sam</a>
      </div>
      <div class="hero-proof">
        <span><b>Spray finish specialists</b>Factory-smooth doors, stairs and trim</span>
        <span><b>Clean working</b>Protected floors, tidy rooms, no drama</span>
        <span><b>Free quote</b>Photos, area, budget and timing captured fast</span>
      </div>
      <div class="est"><b>★★★★★</b> &nbsp;Loved by homeowners across Hampshire &amp; Surrey · Tidy, insured &amp; reliable</div>
    </div>
    <div class="frame">
      <img src="/static/stair-after.jpg" alt="Beautifully painted staircase by Pastilles">
      <div class="finish-card"><b>Premium prep. Premium finish.</b><span>Every quote starts with the details that affect finish quality: surface condition, photos, access and timing.</span></div>
      <div class="badge">EST 1998<small>FAMILY RUN</small></div>
    </div>
  </div>
</header>

<div class="clients">
  <div class="row">
    <span class="lbl">Trusted by</span>
    <span class="c">Butlins</span><span class="dot">•</span>
    <span class="c">Fareham Shopping Centre</span><span class="dot">•</span>
    <span class="c">Poole Dolphin Centre</span><span class="dot">•</span>
    <span class="c">RDL Commercial</span>
  </div>
</div>

<section class="stats">
  <div class="wrap">
    <div class="grid">
      <div class="reveal"><div class="n" data-count="1998" data-plain="1">1998</div><div class="l">Established</div></div>
      <div class="reveal"><div class="n"><span data-count="25">0</span><span class="gold">+</span></div><div class="l">Years' experience</div></div>
      <div class="reveal"><div class="n"><span data-count="500">0</span><span class="gold">+</span></div><div class="l">Projects completed</div></div>
      <div class="reveal"><div class="n"><span data-count="5">0</span><span class="gold">★</span></div><div class="l">Customer rated</div></div>
    </div>
  </div>
</section>

<section class="quote-panel" aria-label="Fast quote details">
  <div class="row">
    <div class="intro reveal">
      <div class="eyebrow">Free quote assistant</div>
      <h2>Tell Sam what needs transforming in under a minute.</h2>
      <p>The assistant captures the job, photos or visit preference, area, rough budget and confirmed phone number, then sends the enquiry straight through for a free, no-obligation quote.</p>
    </div>
    <div class="mini reveal"><b>1. What needs doing?</b><span>Room, exterior, staircase, shop unit, woodwork, wallpaper or spray finish.</span></div>
    <div class="mini reveal"><b>2. Photos or visit</b><span>Send photos by WhatsApp or email, or ask Sam to arrange a look.</span></div>
    <div class="mini reveal"><b>3. Area &amp; budget</b><span>Town/postcode, timing and a rough budget if you have one.</span></div>
    <div class="mini reveal"><b>4. Confirm phone</b><span>Name plus confirmed phone number so Sam can reply quickly.</span></div>
  </div>
</section>

<section class="wow-strip">
  <div class="row">
    <div>
      <div class="eyebrow">Why this feels different</div>
      <h2>A quote flow built for real decorating jobs</h2>
      <p>Instead of a basic contact form, Pastilles now captures the details Sam needs before he calls.</p>
    </div>
    <div class="point"><b>Photos</b>Customers can attach room photos directly in chat.</div>
    <div class="point"><b>Priority</b>Urgency is scored in the lead email so hot jobs stand out.</div>
    <div class="point"><b>Follow-up</b>Name and confirmed phone are required before sending.</div>
  </div>
</section>

<section class="leaflet-sec">
  <div class="wrap leaflet-grid">
    <div class="reveal">
      <div class="eyebrow">High-end decorating</div>
      <h2>Precision finishes, detailed woodwork and specialist spray applications</h2>
      <p>Pastilles focus on clean, careful preparation and a premium finish across full home redecoration, staircases, feature walls, exterior painting and wallpaper hanging.</p>
      <div class="mini-list">
        <span>Full home redecoration</span><span>Specialist spray finishes</span><span>Detailed woodwork</span><span>Exterior painting</span>
      </div>
    </div>
    <div class="leaflet-card reveal">
      <img src="/static/pastilles-leaflet.jpg" alt="Pastilles painting and decorating leaflet">
    </div>
  </div>
</section>


<section id="services">
  <div class="wrap">
    <div class="head reveal">
      <div class="eyebrow">What we do</div>
      <h2>Painting &amp; decorating, done properly</h2>
      <p>From a single room to a full home or commercial project — interior and exterior, with a finish you'll be proud of.</p>
    </div>
    <div class="svc">
      <div class="card reveal"><div class="ic">🎨</div><h3>Interior Painting</h3><p>Walls, ceilings, woodwork and more — clean lines and a smooth, durable finish throughout your home.</p></div>
      <div class="card reveal"><div class="ic">🏠</div><h3>Exterior Painting</h3><p>Weather-proof finishes for walls, render, fascias and windows that protect and lift your property.</p></div>
      <div class="card reveal"><div class="ic">💨</div><h3>HVLP Spray Finishing</h3><p>Our speciality — a flawless, factory-smooth finish on doors, skirting, architrave and staircases.</p></div>
      <div class="card reveal"><div class="ic">🏢</div><h3>Commercial Work</h3><p>Shops, centres and larger projects — Butlins, Fareham Shopping Centre &amp; more — to a high standard.</p></div>
      <div class="card reveal"><div class="ic">🪵</div><h3>Sanding &amp; Varnishing</h3><p>Bringing woodwork, stairs and furniture back to life with careful prep and rich finishes.</p></div>
      <div class="card reveal"><div class="ic">🖼️</div><h3>Wallpapering</h3><p>Feature walls and full rooms hung neatly, with crisp seams and a designer finish.</p></div>
    </div>
  </div>
</section>

<section class="feature" id="spray">
  <div class="wrap">
    <div class="grid">
      <img class="reveal" src="/static/int-after.jpg" alt="Flawless interior finish by Pastilles">
      <div class="reveal">
        <div class="eyebrow">Our speciality</div>
        <h2>The flawless HVLP spray finish</h2>
        <p>We specialise in HVLP (high-volume, low-pressure) spraying — far less overspray than traditional systems, and a beautifully smooth, even finish you simply can't get by brush alone.</p>
        <ul>
          <li>Glass-smooth doors, skirting, architrave &amp; staircases</li>
          <li>Less mess, less overspray, cleaner edges</li>
          <li>A hard-wearing, premium, factory-quality look</li>
          <li>Careful masking and prep — your home protected throughout</li>
        </ul>
        <div style="margin-top:28px"><button class="btn" onclick="openChat()">Ask about spray finishing</button></div>
      </div>
    </div>
  </div>
</section>

<section class="story">
  <div class="wrap">
    <div class="sgrid">
      <img class="reveal" src="/static/ext-after.jpg" alt="Pastilles exterior painting work">
      <div class="reveal">
        <div class="eyebrow">Our story</div>
        <h2>A family name you can trust</h2>
        <p>Pastilles was founded back in 1998 by Sam's late mother, Sue. Years on, Sam proudly carries on the family business — keeping the same care, the same standards, and the same friendly approach that earned Pastilles its reputation across Hampshire and Surrey.</p>
        <p>That heritage means every job, big or small, is done the way it should be: tidy, careful, and finished to a standard we'd be proud of in our own homes.</p>
        <div class="sig">Sam<span>Owner · Pastilles Painting &amp; Decorating</span></div>
      </div>
    </div>
  </div>
</section>

<section id="work">
  <div class="wrap">
    <div class="head reveal">
      <div class="eyebrow">Our work</div>
      <h2>Drag to see the transformation</h2>
      <p>Real Pastilles projects — slide across each image to reveal the before and after.</p>
    </div>
    <div class="ba-wrap">
      <div class="ba-block reveal">
        <div class="cap">Staircase &amp; Hallway</div>
        <div class="ba" data-ba>
          <img class="bimg after" src="/static/stair-after.jpg" alt="After">
          <img class="bimg before" src="/static/stair-before.jpg" alt="Before">
          <span class="tag b">Before</span><span class="tag a">After</span><span class="handle"></span>
        </div>
        <p class="storyline">Spray-finished woodwork and a clean hallway refresh, with the staircase turned into the centrepiece instead of an afterthought.</p>
      </div>
      <div class="ba-block reveal">
        <div class="cap">Exterior Repaint</div>
        <div class="ba" data-ba>
          <img class="bimg after" src="/static/ext-after.jpg" alt="After">
          <img class="bimg before" src="/static/ext-before.jpg" alt="Before">
          <span class="tag b">Before</span><span class="tag a">After</span><span class="handle"></span>
        </div>
        <p class="storyline">Weathered exterior lifted with careful prep, durable coatings and a sharper first impression from the street.</p>
      </div>
      <div class="ba-block reveal">
        <div class="cap">Interior Transformation</div>
        <div class="ba" data-ba>
          <img class="bimg after" src="/static/int-after.jpg" alt="After">
          <img class="bimg before" src="/static/int-before.jpg" alt="Before">
          <span class="tag b">Before</span><span class="tag a">After</span><span class="handle"></span>
        </div>
        <p class="storyline">A tired room brought back with crisp cutting-in, smooth walls and properly finished details around the edges.</p>
      </div>
      <div class="ba-block reveal">
        <div class="cap">Commercial Changing Rooms</div>
        <div class="ba" data-ba>
          <img class="bimg after" src="/static/commercial-room-after.jpg" alt="After">
          <img class="bimg before" src="/static/commercial-room-before.jpg" alt="Before">
          <span class="tag b">Before</span><span class="tag a">After</span><span class="handle"></span>
        </div>
        <p class="storyline">A practical commercial space refreshed quickly, cleanly and with the kind of finish that keeps facilities looking cared for.</p>
      </div>
      <div class="ba-block reveal">
        <div class="cap">Outside Wall Painting</div>
        <div class="ba" data-ba>
          <img class="bimg after" src="/static/portfolio/outside-wall-painting.jpg" alt="After">
          <img class="bimg before" src="/static/portfolio/outside-wall-painting-before.jpg" alt="Before">
          <span class="tag b">Before</span><span class="tag a">After</span><span class="handle"></span>
        </div>
        <p class="storyline">A tired exterior wall sharpened up with careful prep, clean edges and a fresh protective finish.</p>
      </div>
      <div class="ba-block reveal">
        <div class="cap">Fareham Shopping Centre</div>
        <div class="ba" data-ba>
          <img class="bimg after" src="/static/portfolio/fareham-shopping-center-2-after.jpeg" alt="After">
          <img class="bimg before" src="/static/portfolio/fareham-shopping-center-2-before.jpeg" alt="Before">
          <span class="tag b">Before</span><span class="tag a">After</span><span class="handle"></span>
        </div>
        <p class="storyline">Commercial detail work at Fareham Shopping Centre, finished neatly around real site conditions and public-facing surfaces.</p>
      </div>
      <div class="ba-block reveal">
        <div class="cap">Butlins Commercial Ceiling</div>
        <div class="ba" data-ba>
          <img class="bimg after" src="/static/portfolio/butlins-after.jpeg" alt="After">
          <img class="bimg before" src="/static/portfolio/butlins-before.jpeg" alt="Before">
          <span class="tag b">Before</span><span class="tag a">After</span><span class="handle"></span>
        </div>
        <p class="storyline">A large-scale commercial ceiling package showing Pastilles can handle tougher, darker finishes and awkward access.</p>
      </div>
    </div>
    <div class="case-grid reveal">
      <div class="case"><div class="k">Domestic</div><h3>Hallways that sell the home</h3><p>High-traffic areas need tough prep and neat woodwork. Pastilles turns first impressions into a reason to book a viewing.</p></div>
      <div class="case"><div class="k">Spray finishing</div><h3>Factory-smooth details</h3><p>Doors, skirting, architrave and stairs get the crisp sprayed look clients notice immediately in person and in photos.</p></div>
      <div class="case"><div class="k">Commercial</div><h3>Reliable larger projects</h3><p>From shopping centres to leisure spaces, Sam's team can work tidily around real deadlines and busy environments.</p></div>
    </div>
  </div>
</section>

<section class="vids">
  <div class="wrap">
    <div class="head reveal">
      <div class="eyebrow">On the job</div>
      <h2>See the work in action</h2>
      <p>A look at our commercial projects — flawless finishes, start to finish.</p>
    </div>
    <div class="vid-grid">
      <div class="vid-block reveal">
        <video controls preload="metadata" playsinline><source src="https://pastillespainting.co.uk/wp-content/uploads/2024/05/Poole-Dolphin-Centre.mp4" type="video/mp4"></video>
        <div class="v-cap"><h3>Poole Dolphin Centre</h3><p>Commercial shop refurbishment project</p></div>
      </div>
      <div class="vid-block reveal">
        <video controls preload="metadata" playsinline><source src="https://pastillespainting.co.uk/wp-content/uploads/2024/05/RDL-Commercial.mp4" type="video/mp4"></video>
        <div class="v-cap"><h3>RDL Commercial Ltd</h3><p>Hand-rolled finish, commercial unit</p></div>
      </div>
    </div>
  </div>
</section>

<section class="why">
  <div class="wrap">
    <div class="head reveal">
      <div class="eyebrow">Peace of mind</div>
      <h2>Why homeowners choose Pastilles</h2>
    </div>
    <div class="grid">
      <div class="card reveal"><div class="ic">👪</div><h3>Family-run since 1998</h3><p>Three generations of craftsmanship and a name people trust across the region.</p></div>
      <div class="card reveal"><div class="ic">💨</div><h3>Spray-finish specialists</h3><p>A flawless, factory-smooth HVLP finish you simply can't get by brush alone.</p></div>
      <div class="card reveal"><div class="ic">🛡️</div><h3>Fully insured</h3><p>Every job fully insured and carried out with care, start to finish.</p></div>
      <div class="card reveal"><div class="ic">🧹</div><h3>Tidy &amp; respectful</h3><p>We protect your home, work cleanly, and leave the place spotless.</p></div>
      <div class="card reveal"><div class="ic">💬</div><h3>Free, honest quotes</h3><p>Clear, fair pricing with no pressure and no obligation — ever.</p></div>
      <div class="card reveal"><div class="ic">⭐</div><h3>5-star rated</h3><p>32+ five-star Google reviews and a lot of happy, repeat customers.</p></div>
    </div>
  </div>
</section>

<section class="more">
  <div class="wrap">
    <div class="head reveal">
      <div class="eyebrow">Gallery</div>
      <h2>More of our work</h2>
      <p>Tap any image to take a closer look.</p>
    </div>
    <div class="grid-g">
      <a onclick="openLB(this)"><img src="/static/stair-after.jpg" alt="Finished stairwell"></a>
      <a onclick="openLB(this)"><img src="/static/ext-after.jpg" alt="Exterior repaint after"></a>
      <a onclick="openLB(this)"><img src="/static/int-after.jpg" alt="Interior decorating after"></a>
      <a onclick="openLB(this)"><img src="/static/commercial-lockers-after.jpg" alt="Commercial changing room repaint"></a>
      <a onclick="openLB(this)"><img src="/static/commercial-room-after.jpg" alt="Commercial interior repaint"></a>
      <a onclick="openLB(this)"><img src="/static/portfolio/outside-wall-painting.jpg" alt="Outside wall repaint after"></a>
      <a onclick="openLB(this)"><img src="/static/portfolio/fareham-shopping-center-2-after.jpeg" alt="Fareham Shopping Centre painted detail"></a>
      <a onclick="openLB(this)"><img src="/static/portfolio/butlins-after.jpeg" alt="Butlins commercial ceiling repaint"></a>
      <a onclick="openLB(this)"><img src="/static/portfolio/photo-2-1.webp" alt="Pastilles portfolio project detail"></a>
      <a onclick="openLB(this)"><img src="/static/portfolio/photo-3-1.webp" alt="Pastilles residential decorating detail"></a>
      <a onclick="openLB(this)"><img src="/static/portfolio/photo-2.webp" alt="Pastilles commercial decorating finish"></a>
      <a onclick="openLB(this)"><img src="/static/portfolio/photo-3.webp" alt="Pastilles finished decorating work"></a>
    </div>
  </div>
</section>

<section class="reviews" id="reviews">
  <div class="wrap">
    <div class="head reveal">
      <div class="eyebrow">Kind words</div>
      <h2>What our customers say</h2>
      <p>Years of tidy, careful work and a lot of happy, repeat customers across Hampshire and Surrey.</p>
    </div>
    <div style="text-align:center">
      <div class="gbadge reveal"><span class="g">G</span><span><span class="stars">★★★★★</span> &nbsp;<b>5.0</b> <span class="txt">· 32+ Google reviews</span></span></div>
    </div>
    <div class="review-hero reveal">
      <div><div class="score">5.0</div><div class="stars">★★★★★</div></div>
      <div>
        <p>"Fantastic communication throughout, tidy work, fair pricing and a really high quality finish."</p>
        <div class="review-points"><span>Clean working</span><span>Clear communication</span><span>High-end finish</span><span>Repeat customers</span></div>
      </div>
    </div>
    <div class="rev">
      <div class="q reveal"><div class="stars">★★★★★</div><p>"They recently sprayed our living room and the results are absolutely flawless. The finish is incredibly smooth and looks highly professional. The team was punctual, tidy and respectful of our home throughout. I cannot recommend their spraying enough!"</p><div class="who">— Mel Harbut <span class="gv">· Google</span></div></div>
      <div class="q reveal"><img class="rev-photo" src="/static/review-kitchen.jpg" alt="Kitchen redecorated by Pastilles"><div class="stars">★★★★★</div><p>"Sam and Sarah redecorated our home after we'd had builders in to repair a leak. They completed the work before we needed to move back in and did a great job. We would use them again."</p><div class="who">— Louise Hannam <span class="gv">· Google</span></div></div>
      <div class="q reveal"><img class="rev-photo" src="/static/review-door.jpg" alt="Front door sprayed anthracite grey by Pastilles"><div class="stars">★★★★★</div><p>"Painted our front door anthracite grey. Very happy with the result. Excellent attention to detail — highly recommended."</p><div class="who">— Oliver Goodley <span class="gv">· Google</span></div></div>
      <div class="q reveal"><div class="stars">★★★★★</div><p>"Sam and his team painted our small toilet room — we're useless with gloss paint! They took genuine pride in it and even came back to add another coat because they felt it needed it. A real pleasure to have them around."</p><div class="who">— Katie Hall-May <span class="gv">· Google</span></div></div>
      <div class="q reveal"><div class="stars">★★★★★</div><p>"Asked Sam to repair and repaint the wall and bar of our family restaurant. Having been knocked down by covid, Sam was very kind and understanding to accommodate our budget. Fantastic workmanship — both wall and bar look beautiful."</p><div class="who">— Mahesh Sharma <span class="gv">· Google</span></div></div>
      <div class="q reveal"><div class="stars">★★★★★</div><p>"Absolutely fantastic service. Managed to fit me in at short notice. Great timekeeping and communication. Painting looks great and very professional. Dust sheets and prep used throughout. Highly recommended."</p><div class="who">— Stacey Axton <span class="gv">· Google</span></div></div>
    </div>
  </div>
</section>

<section class="areas">
  <div class="wrap">
    <div class="head reveal" style="margin-bottom:8px">
      <div class="eyebrow">Where we work</div>
      <h2>Proudly covering Hampshire &amp; Surrey</h2>
    </div>
    <div class="pills reveal">
      <span>Southampton</span><span>Winchester</span><span>Eastleigh</span><span>Fareham</span>
      <span>Havant</span><span>Hampshire</span><span>Surrey</span><span>&amp; surrounding areas</span>
    </div>
  </div>
</section>

<section class="how">
  <div class="wrap">
    <div class="head reveal">
      <div class="eyebrow">Simple &amp; stress-free</div>
      <h2>How it works</h2>
      <p>From first hello to a flawless finish — getting started couldn't be easier.</p>
    </div>
    <div class="steps">
      <div class="step reveal"><div class="num">1</div><h3>Get in touch</h3><p>Tell our assistant about your project, or give Sam a quick call. Takes a minute.</p></div>
      <div class="step reveal"><div class="num">2</div><h3>Your free quote</h3><p>Sam takes a look and sends a clear, fair, no-obligation quote — no pressure at all.</p></div>
      <div class="step reveal"><div class="num">3</div><h3>We transform your space</h3><p>Tidy, careful work and a beautiful, lasting finish you'll be proud of.</p></div>
    </div>
  </div>
</section>

<section class="faq">
  <div class="wrap">
    <div class="head reveal">
      <div class="eyebrow">Good to know</div>
      <h2>Frequently asked questions</h2>
    </div>
    <div class="list reveal">
      <details><summary>Do you offer free quotes?</summary><p>Always. Every quote is completely free and no-obligation — just tell us about the job and Sam will get back to you.</p></details>
      <details><summary>What areas do you cover?</summary><p>Southampton and across Hampshire and Surrey — including Winchester, Eastleigh, Fareham, Havant and surrounding areas.</p></details>
      <details><summary>What is HVLP spray finishing?</summary><p>It's our speciality — a high-volume, low-pressure spray system that gives a beautifully smooth, factory-quality finish on doors, skirting, architrave and staircases, with far less overspray than traditional methods.</p></details>
      <details><summary>Are you insured?</summary><p>Yes — we're fully insured, tidy and reliable, and we treat your home with care from start to finish.</p></details>
      <details><summary>Do you take on commercial work?</summary><p>Absolutely. We've completed large commercial projects including Butlins, Fareham Shopping Centre and Poole Dolphin Centre, alongside all our domestic work.</p></details>
      <details><summary>How do I get started?</summary><p>Tap “Get a Free Quote” to chat with our assistant, or call Sam on 07360 094 318 — we'll take it from there.</p></details>
    </div>
  </div>
</section>

<section class="band">
  <div class="wrap">
    <div class="eyebrow">Get started</div>
    <h2>Ready for a fresh, flawless finish?</h2>
    <p>Tell our assistant about your project and Sam will get back to you with a free, no-obligation quote.</p>
    <div class="cta">
      <button class="btn" onclick="openChat()">Get a Free Quote</button>
      <a class="btn outline" href="tel:+447360094318">Call 07360 094 318</a>
    </div>
    <div class="meta">
      <a href="tel:+447360094318">📞 07360 094 318</a>
      <a href="mailto:pastillespainting@gmail.com">✉️ pastillespainting@gmail.com</a>
    </div>
  </div>
</section>

<footer>
  <div class="row">
    <div>© <span id="yr"></span> Pastilles Painting &amp; Decorating · Family-run since 1998 · Southampton, Hampshire &amp; Surrey</div>
    <div class="social">
      <a href="https://www.facebook.com/pastillespaintinganddecorating/" target="_blank" rel="noopener">Facebook</a>
      <a href="https://www.instagram.com/pastillespaintinganddecorating/" target="_blank" rel="noopener">Instagram</a>
      <a href="https://www.tiktok.com/@pastilles_painting_deco2" target="_blank" rel="noopener">TikTok</a>
    </div>
  </div>
</footer>

<button id="bub" onclick="openChat()">💬 Free Quote</button>
<a id="waWidget" href="https://wa.me/447360094318?text=Hi%20Sam%2C%20I%27d%20like%20a%20free%20quote%20for%20painting%20or%20decorating." target="_blank" rel="noopener" aria-label="WhatsApp Sam">☎<span>WhatsApp Sam</span></a>

<div id="mobar">
  <a href="tel:+447360094318">📞 Call Sam</a>
  <button onclick="openChat()">💬 Free Quote</button>
</div>

<div id="chat">
  <header>
    <div class="av">P</div>
    <div class="t">Pastilles Assistant<small>Typically replies in seconds</small></div>
    <button class="x" onclick="closeChat()">×</button>
  </header>
  <div id="progress" aria-label="Quote progress">
    <span data-label="Job"></span><span data-label="Photos"></span><span data-label="Area"></span><span data-label="Budget"></span><span data-label="Contact"></span>
  </div>
  <div id="progressText">Quote progress: tell us the job, photos, area, budget and contact.</div>
  <div id="msgs"></div>
  <div id="quick" aria-label="Quick replies">
    <button type="button" onclick="quickAsk('I need interior painting')">Interior painting</button>
    <button type="button" onclick="quickAsk('I need exterior painting')">Exterior painting</button>
    <button type="button" onclick="quickAsk('I want a spray finish quote')">Spray finish</button>
  </div>
  <div id="cbar">
    <label id="attach" title="Attach photos of the job" aria-label="Attach photos of the job">
      <input id="fileInp" type="file" accept="image/*" multiple onchange="handleFiles(this)">
      📎
    </label>
    <input id="inp" placeholder="Type your message…" autocomplete="off" onkeydown="if(event.key==='Enter')sendMsg()">
    <button onclick="sendMsg()">➤</button>
  </div>
</div>

<div id="lb" onclick="this.style.display='none'"><span class="close">×</span><img src="" alt=""></div>

<script>
  document.getElementById('yr').textContent = new Date().getFullYear();

  function toggleNav(){var m=document.getElementById('mobnav');m.style.display=(m.style.display==='block')?'none':'block';}

  function openLB(a){var img=a.querySelector('img');var lb=document.getElementById('lb');lb.querySelector('img').src=img.src;lb.style.display='flex';}

  /* reveal on scroll */
  (function(){
    var els=document.querySelectorAll('.reveal');
    if(!('IntersectionObserver' in window)){els.forEach(function(e){e.classList.add('in')});return;}
    var io=new IntersectionObserver(function(en){en.forEach(function(x){if(x.isIntersecting){x.target.classList.add('in');io.unobserve(x.target);}})},{threshold:.12});
    els.forEach(function(e){io.observe(e)});
    setTimeout(function(){els.forEach(function(e){e.classList.add('in')})},2500);
  })();

  /* count-up stats */
  (function(){
    var done=false;
    function run(){
      if(done)return;done=true;
      document.querySelectorAll('[data-count]').forEach(function(el){
        var target=+el.getAttribute('data-count');var plain=el.getAttribute('data-plain');var t0=null;var dur=1400;
        function step(ts){if(!t0)t0=ts;var p=Math.min((ts-t0)/dur,1);var val=Math.floor(p*target);
          el.textContent=plain?val:val.toLocaleString();if(p<1)requestAnimationFrame(step);else el.textContent=plain?target:target.toLocaleString();}
        requestAnimationFrame(step);
      });
    }
    var s=document.querySelector('.stats');
    if('IntersectionObserver' in window){var io=new IntersectionObserver(function(en){en.forEach(function(x){if(x.isIntersecting)run();})},{threshold:.4});io.observe(s);}
    else run();
    setTimeout(run,3000);
  })();

  /* before/after sliders */
  (function(){
    document.querySelectorAll('[data-ba]').forEach(function(ba){
      var before=ba.querySelector('.before');
      function set(x){var r=ba.getBoundingClientRect();var p=((x-r.left)/r.width)*100;p=Math.max(0,Math.min(100,p));
        ba.style.setProperty('--p',p+'%');}
      var drag=false;
      ba.addEventListener('pointerdown',function(e){drag=true;ba.setPointerCapture(e.pointerId);set(e.clientX);});
      ba.addEventListener('pointermove',function(e){if(drag)set(e.clientX);});
      ba.addEventListener('pointerup',function(){drag=false;});
      ba.addEventListener('pointercancel',function(){drag=false;});
      ba.style.setProperty('--p','50%');
    });
  })();

  /* lightbox for videos? no - keep simple */

  /* chat */
  var chatMessages=[];var started=false;
  function userText(){return chatMessages.filter(function(m){return m.role==='user'}).map(function(m){return m.content}).join(' ').toLowerCase();}
  function hasPhone(t){return /(?:\+44|0)\d[\d\s\-.]{8,11}/.test(t);}
  function hasName(){for(var i=0;i<chatMessages.length;i++){var m=chatMessages[i];if(m.role==='assistant'&&/name/i.test(m.content)){for(var j=i+1;j<chatMessages.length;j++){if(chatMessages[j].role==='user'&&!/photo|^\s*(yes|yeah|yep|correct|right)\s*$/i.test(chatMessages[j].content)){return true;}}}}return /\b(my name is|i am|i'm|im|this is)\s+[a-z]/i.test(userText());}
  function updateProgress(){
    var t=userText();
    var done=[
      t.length>8,
      /photo|visit|attached/.test(t),
      /southampton|winchester|eastleigh|fareham|havant|hampshire|surrey|postcode|area/.test(t),
      /£|\bbudget\b|\bno budget\b|\bnot sure\b|around \d|about \d/.test(t),
      hasPhone(t)&&hasName()
    ];
    document.querySelectorAll('#progress span').forEach(function(el,i){el.classList.toggle('done',!!done[i]);});
    var count=done.filter(Boolean).length;
    var txt=document.getElementById('progressText');
    if(txt)txt.textContent='Quote progress: '+count+'/5 details captured';
  }
  function openChat(){
    document.getElementById('chat').style.display='flex';
    document.getElementById('bub').style.display='none';
    document.body.style.overflow='hidden';
    if(!started){started=true;botSay("Hi there! 👋 Welcome to Pastilles. I can help with a free quote for any painting or decorating work — interior, exterior or our specialist spray finishing. What are you looking to get done?");}
    if(window.innerWidth>560){setTimeout(function(){document.getElementById('inp').focus()},200);}
  }
  function closeChat(){document.getElementById('chat').style.display='none';document.getElementById('bub').style.display='flex';document.body.style.overflow='';}
  function add(t,c){var m=document.createElement('div');m.className='m '+c;m.textContent=t;var b=document.getElementById('msgs');b.appendChild(m);b.scrollTop=b.scrollHeight;return m;}
  function botSay(t){add(t,'bot');chatMessages.push({role:'assistant',content:t});updateProgress();}
  function quickAsk(t){document.getElementById('inp').value=t;sendMsg();}
  function addImage(src){var m=document.createElement('div');m.className='m me photo-msg';var img=document.createElement('img');img.src=src;img.alt='Attached job photo';m.appendChild(img);var b=document.getElementById('msgs');b.appendChild(m);b.scrollTop=b.scrollHeight;}
  function resizeImage(file){
    return new Promise(function(resolve,reject){
      var reader=new FileReader();
      reader.onload=function(){
        var img=new Image();
        img.onload=function(){
          var max=1600,w=img.naturalWidth,h=img.naturalHeight;
          if(Math.max(w,h)>max){if(w>=h){h=Math.round(h*max/w);w=max;}else{w=Math.round(w*max/h);h=max;}}
          var canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;
          var ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,w,h);ctx.drawImage(img,0,0,w,h);
          resolve(canvas.toDataURL('image/jpeg',.82));
        };
        img.onerror=function(){reject(new Error('decode'));};
        img.src=reader.result;
      };
      reader.onerror=function(){reject(new Error('read'));};
      reader.readAsDataURL(file);
    });
  }
  async function handleFiles(input){
    var files=Array.from(input.files||[]);input.value='';if(!files.length)return;
    var attach=document.getElementById('attach');attach.classList.add('busy');
    for(var i=0;i<files.length;i++){
      var file=files[i];
      if(!file.type||file.type.indexOf('image/')!==0){add("That doesn't look like a photo — please choose an image.",'bot');continue;}
      try{
        var dataUrl=await resizeImage(file);
        addImage(dataUrl);
        chatMessages.push({role:'user',content:'(Customer attached a photo of the job)'});
        updateProgress();
        var res=await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({image:dataUrl})});
        var data=await res.json();
        var reply=(data&&data.reply)?data.reply:"Thanks, got the photo.";
        add(reply,'bot');chatMessages.push({role:'assistant',content:reply});updateProgress();
      }catch(e){
        add("Sorry, I couldn't upload that photo. Try a JPG or PNG, or send it to Sam by WhatsApp.",'bot');
      }
    }
    attach.classList.remove('busy');
  }
  async function sendMsg(){
    var inp=document.getElementById('inp');var t=inp.value.trim();if(!t)return;
    inp.value='';add(t,'me');chatMessages.push({role:'user',content:t});
    updateProgress();
    var q=document.getElementById('quick');if(q)q.style.display='none';
    var typing=add('typing…','bot typing');
    try{
      var ctrl=new AbortController();var to=setTimeout(function(){ctrl.abort()},18000);
      var r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({messages:chatMessages}),signal:ctrl.signal});
      clearTimeout(to);
      if(!r.ok){throw new Error('Chat request failed: '+r.status);}
      var d=await r.json();typing.remove();
      var reply=(d&&d.reply)?d.reply:"Thanks! Pop your name and the best number or email and Sam will be in touch with a free quote.";
      add(reply,'bot');chatMessages.push({role:'assistant',content:reply});updateProgress();
    }catch(e){
      typing.remove();
      var fallback="Thanks for your message! Pop your name and the best number or email here and Sam will get back to you with a free quote — or call him directly on 07360 094 318.";
      add(fallback,'bot');chatMessages.push({role:'assistant',content:fallback});updateProgress();
    }
  }
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
