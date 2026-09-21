"""Post-call transcript analysis with a Gemini text model.

Runs once per call after the recorder has persisted the final transcript. Produces the
summary, intent, sentiment, escalation flags and an independent classification that
tickets.apply_analysis writes onto the ticket — or, when the live agent never registered
one (a dropped call), enough structure to create it from the transcript.

Everything here is best-effort: one retry, then None. A failed analysis leaves a valid
ticket without a summary; it never touches the live call path.
"""

import asyncio
import json
import logging
import os
import re

import languages
import routing

logger = logging.getLogger(__name__)


def model_id() -> str:
    return (os.getenv("EPP_ANALYSIS_MODEL") or "gemini-3.6-flash").strip()


_FIELDS_DOC = """Return ONLY a JSON object with exactly these keys:
{
  "summary": "3-5 sentence neutral summary of the caller's concern in English, in the third person",
  "intent": "one short phrase, e.g. 'salary delayed', 'delivery status', 'harassment complaint', 'status inquiry'",
  "is_status_inquiry": true|false,
  "caller_type": "customer" | "vendor" | "employee" | "",
  "category": "one category name from the list for that caller type, or 'Other'",
  "category_confidence": 0.0-1.0,
  "subcategory": "short free text refinement or ''",
  "priority": "high" | "medium",
  "escalation_flags": [zero or more of: "safety_incident","harassment","violence","threat","security_incident","medical_emergency","serious_misconduct"],
  "sentiment_score": -1.0 (very negative) to 1.0 (very positive),
  "sentiment_label": "negative" | "neutral" | "positive",
  "language": "ISO code of the language the caller mostly spoke",
  "caller_name": "", "contact_number": "", "company_name": "", "vendor_code": "",
  "employee_id": "", "caller_department": "", "plant_location": "",
  "description": "the concern in full, in English, third person, with every date, name, number and detail the caller gave",
  "should_create_ticket": true|false
}
Rules: never invent details; leave a field '' when the caller did not say it. should_create_ticket is true only
when the caller described a genuine concern to register (not merely a status inquiry or a wrong number).
priority is high only for safety incidents, harassment, violence, threats, security incidents, medical
emergencies or serious misconduct. Output JSON only, no prose, no code fences."""


def build_prompt(call, categories, ticket=None) -> str:
    by_type = {}
    for c in categories or []:
        if int(c.get("active", 1) or 0):
            by_type.setdefault(str(c.get("caller_type")), []).append(str(c.get("name")))
    cat_lines = "\n".join(f"- {ct}: {', '.join(names)}" for ct, names in by_type.items()) or "- (none configured)"
    transcript = "\n".join(
        f"{'Caller' if t.get('role') == 'user' else 'Agent'}: {t.get('text', '')}"
        for t in (call or {}).get("transcript") or [])
    known = ""
    if ticket:
        known = (f"\nThe live agent already registered ticket {ticket.get('ticket_id')} as caller_type="
                 f"{ticket.get('caller_type')}, category='{ticket.get('category')}', priority={ticket.get('priority')}. "
                 "Judge independently from the transcript; disagree only when the transcript clearly supports it.\n")
    langs = ", ".join(f"{l['code']}={l['name']}" for l in languages.LANGUAGES)
    return (
        "You are the quality reviewer for an inbound support and grievance helpline run by "
        f"{os.getenv('EPP_COMPANY_NAME') or 'EPP Composites'}. Callers are customers, vendors or employees.\n"
        f"Category list per caller type:\n{cat_lines}\n"
        f"Language codes: {langs}\n{known}\n"
        f"TRANSCRIPT (may be in any Indian language, often romanised):\n{transcript}\n\n{_FIELDS_DOC}"
    )


async def _generate(prompt: str) -> str:
    """The one network call. Monkeypatched in tests."""
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    cfg = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2)
    resp = await client.aio.models.generate_content(model=model_id(), contents=prompt, config=cfg)
    return resp.text or ""


def parse(text: str) -> dict | None:
    """Tolerant parse: strips code fences, finds the outermost object, validates and clamps."""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(s[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for k in ("summary", "intent", "category", "subcategory", "caller_name", "contact_number",
              "company_name", "vendor_code", "employee_id", "caller_department", "plant_location",
              "description"):
        out[k] = str(data.get(k) or "").strip()
    out["caller_type"] = routing.normalize_caller_type(data.get("caller_type"))
    try:
        out["category_confidence"] = max(0.0, min(1.0, float(data.get("category_confidence") or 0)))
    except (TypeError, ValueError):
        out["category_confidence"] = 0.0
    out["priority"] = "high" if str(data.get("priority") or "").strip().lower() == "high" else "medium"
    flags = data.get("escalation_flags") or []
    if isinstance(flags, str):
        flags = [flags]
    out["escalation_flags"] = [str(f).strip().lower() for f in flags
                               if str(f).strip().lower() in routing.PRIORITY_REASONS
                               and str(f).strip().lower() != "none"]
    if out["escalation_flags"]:
        out["priority"] = "high"
    try:
        out["sentiment_score"] = max(-1.0, min(1.0, float(data.get("sentiment_score"))))
    except (TypeError, ValueError):
        out["sentiment_score"] = None
    label = str(data.get("sentiment_label") or "").strip().lower()
    if label not in ("negative", "neutral", "positive"):
        sc = out["sentiment_score"]
        label = "" if sc is None else ("negative" if sc < -0.2 else "positive" if sc > 0.2 else "neutral")
    out["sentiment_label"] = label
    out["language"] = languages.normalize(data.get("language"))
    out["is_status_inquiry"] = bool(data.get("is_status_inquiry"))
    out["should_create_ticket"] = bool(data.get("should_create_ticket"))
    return out


_last_error = ""


def last_error() -> str:
    """The most recent failure reason (e.g. a spending-cap or auth error), for the call record."""
    return _last_error


async def analyze(call, categories, ticket=None) -> dict | None:
    global _last_error
    prompt = build_prompt(call, categories, ticket)
    _last_error = ""
    for attempt in range(2):
        try:
            text = await _generate(prompt)
            result = parse(text)
            if result:
                return result
            _last_error = "the analysis model returned unparseable output"
            logger.warning("post-call analysis returned unparseable output (attempt %d)", attempt + 1)
        except Exception as e:
            _last_error = f"{type(e).__name__}: {e}"[:300]
            logger.warning("post-call analysis failed (attempt %d): %s", attempt + 1, e)
        await asyncio.sleep(1.0)
    return None
