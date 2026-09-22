"""Recognise a returning caller from the number they are calling from.

The helpline already holds what a caller told us last time — on their tickets (name, caller
type, employee id / company / vendor code, plant, language) and in the contacts pool. On an
inbound call the number is matched against both, and the agent is handed a short section of
facts plus a personalised opening, so a known caller hears "Am I speaking with Shivi?" in the
language they used last time instead of being asked everything again.

Privacy rule baked into the block: a phone number is not an identity. The agent confirms it
is the same person before using any of it, and treats a "no" as a brand-new caller.

Pure read-side helper: never raises (an unknown number, a DB hiccup → None → normal opening).
"""

import logging

import eo_db
import languages
import tickets

logger = logging.getLogger(__name__)

_OPEN = ("open", "under_review", "escalated")
_DETAIL_LABELS = (
    ("company_name", "Company"),
    ("vendor_code", "Vendor code"),
    ("employee_id", "Employee ID"),
    ("caller_department", "Department"),
    ("plant_location", "Plant / location"),
)


def lookup(phone):
    """Profile for `phone`, or None when we have never spoken to this number.

    The latest ticket is the authority (it carries what the caller confirmed most recently);
    the contacts pool fills anything the tickets do not have."""
    try:
        e164 = tickets._clean_phone(phone)
        if len(e164) < 11:
            return None
        rows = eo_db.tickets_by_phone(e164, limit=10)
        contact = eo_db.contact_by_phone(e164)
        if not rows and not contact:
            return None
        latest = rows[0] if rows else {}
        name = (latest.get("caller_name") or "").strip()
        if not name or name.lower() == "not given":
            name = ((contact or {}).get("name") or "").strip()
        if not name:
            return None                              # nothing to greet them with
        caller_type = latest.get("caller_type") or (contact or {}).get("caller_type") or ""
        language = latest.get("language") or ""
        details = {}
        for key, _label in _DETAIL_LABELS:
            val = (latest.get(key) or "").strip()
            if val:
                details[key] = val
        if contact and contact.get("notes"):
            details["notes"] = contact["notes"]
        open_tickets = [{
            "ticket_id": t["ticket_id"],
            "status_label": tickets.STATUS_LABEL.get(t.get("status"), t.get("status") or ""),
            "category": t.get("category") or "",
            "created_spoken": tickets.spoken_date(t.get("created_at")),
        } for t in rows if t.get("status") in _OPEN]
        return {
            "phone": e164,
            "name": name,
            "first_name": name.split()[0],
            "caller_type": caller_type,
            "language": language,
            "language_name": languages.name_of(language) if language else "",
            "details": details,
            "open_tickets": open_tickets,
            "tickets_total": len(rows),
            "source": "ticket" if rows else "contact",
        }
    except Exception:
        logger.exception("known-caller lookup failed for %r", phone)
        return None


def placeholders(profile):
    """The prompt placeholders for a call. Every key is present (blank when unknown) so the
    renderer never reports them missing and the block line simply disappears."""
    if not profile:
        return {"known_caller_name": "", "known_caller_first_name": "", "known_caller_type": "",
                "known_caller_language": "", "known_caller_block": ""}
    lang_name = profile.get("language_name") or "English"
    lines = ["## WHAT WE ALREADY KNOW ABOUT THIS CALLER",
             "The number they are calling from matches a previous caller:",
             f"- Name: {profile['name']}"]
    if profile.get("caller_type"):
        lines.append(f"- They are a(n): {profile['caller_type']}")
    for key, label in _DETAIL_LABELS:
        if profile["details"].get(key):
            lines.append(f"- {label}: {profile['details'][key]}")
    if profile["details"].get("notes"):
        lines.append(f"- Note on file: {profile['details']['notes']}")
    lines.append(f"- Last spoke to us in: {lang_name}")
    if profile["open_tickets"]:
        for t in profile["open_tickets"][:3]:
            lines.append(f"- Open ticket: {t['ticket_id']} ({t['status_label']}, {t['category']}, "
                         f"registered {t['created_spoken']}) — spoken as "
                         f"{tickets.spoken_ticket_id(t['ticket_id'])}")
    else:
        lines.append("- Open tickets: none")
    lines.append(
        "RULES: Your opening greets them by first name and asks if it is them. If they confirm, do NOT "
        "ask again for the details above — confirm them in ONE short breath (\"I have you as an "
        f"{profile.get('caller_type') or 'caller'}"
        + (f" at {profile['details']['plant_location']}" if profile["details"].get("plant_location") else "")
        + " — still correct?\") and go straight to their concern. If they have open tickets, offer to give "
        "the status of one, or to register something new; a status still comes ONLY from lookup_ticket. "
        "If it is NOT them (a different person on this number), say \"no problem\" and treat them as a new "
        "caller from CALLER TYPE onwards: never read out the other person's name, details or tickets."
    )
    return {
        "known_caller_name": profile["name"],
        "known_caller_first_name": profile["first_name"],
        "known_caller_type": profile.get("caller_type") or "",
        "known_caller_language": lang_name,
        "known_caller_block": "\n".join(lines),
    }
