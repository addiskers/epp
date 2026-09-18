"""Gemini tool declarations for the helpline agent.

Three tools, built per call so the category enum reflects the current Routing table:

* ``create_ticket`` — registers the concern. BLOCKING: its result (the ticket number)
  must be spoken, so unlike the old silent record_outcome it is allowed to force a turn.
* ``lookup_ticket`` — status of an existing ticket. Also blocking, same reason.
* ``end_call`` — universal.

The category list is operator-editable, so the build is defensive: an empty or malformed
table still produces a valid declaration (with a free-text category) rather than break a
live call.
"""

import logging

import languages
import routing

logger = logging.getLogger(__name__)

END_CALL_DECLARATION = {
    "name": "end_call",
    "description": (
        "Hang up the phone call. Call this ONCE, silently, immediately AFTER you have spoken "
        "your final goodbye, when the conversation is complete. This ends the call."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

LOOKUP_TICKET_DECLARATION = {
    "name": "lookup_ticket",
    "description": (
        "Look up the current status of an existing ticket by its reference number. Call it "
        "once you have repeated the number back to the caller and they confirmed it. Speak ONLY "
        "what it returns; if found is false, say so and never guess a status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ticket_number": {
                "type": "string",
                "description": "The reference number as the caller gave it, e.g. 'EPP-2026-000123', "
                               "'EPP 2026 123' or just '123'. Do not reformat it.",
            },
        },
        "required": ["ticket_number"],
    },
}


def _category_names(categories):
    """Unique category names across every caller type (a JSON-schema enum cannot depend on
    another property). Order: as configured."""
    names = []
    for c in categories or []:
        if not int(c.get("active", 1) or 0):
            continue
        n = str(c.get("name") or "").strip()
        if n and n not in names:
            names.append(n)
    if "Other" not in names:
        names.append("Other")
    return names


def create_ticket_declaration(categories=None, langs=None):
    cats = _category_names(categories)
    lang_codes = [l["code"] for l in (langs or languages.enabled())]
    props = {
        "caller_type": {
            "type": "string", "enum": list(routing.CALLER_TYPES),
            "description": "Who is calling: customer (buys from us), vendor (supplies to us) or "
                           "employee (works with us).",
        },
        "language": {
            "type": "string", "enum": lang_codes,
            "description": "The language the caller chose for the call (ISO code).",
        },
        "caller_name": {"type": "string", "description": "The caller's full name as they gave it."},
        "contact_number": {
            "type": "string",
            "description": "The contact number the caller wants used. Empty if they said to use "
                           "the number they are calling from.",
        },
        "company_name": {"type": "string", "description": "Customer only: their company's name. Empty otherwise."},
        "vendor_code": {"type": "string", "description": "Vendor only: their vendor code if they know it. Empty otherwise."},
        "employee_id": {"type": "string", "description": "Employee only: employee code / ID. Empty otherwise."},
        "caller_department": {"type": "string", "description": "Employee only: the department they work in, if known. Empty otherwise."},
        "plant_location": {"type": "string", "description": "Employee only: their plant or location. Empty otherwise."},
        "description": {
            "type": "string",
            "description": "The concern in full, in English, third person, including dates, names, "
                           "order/invoice numbers and the answers to your follow-up questions.",
        },
        "category": {
            "type": "string", "enum": cats,
            "description": "The ONE category that best fits, from the list for this caller type. "
                           "'Other' if none fits.",
        },
        "subcategory": {
            "type": "string",
            "description": "A short free-text refinement of the category (e.g. 'salary delayed 2 months'). "
                           "Empty if none.",
        },
        "high_priority_reason": {
            "type": "string", "enum": list(routing.PRIORITY_REASONS),
            "description": "'none' for ordinary concerns. Otherwise the reason this is urgent: "
                           "safety_incident, harassment, violence, threat, security_incident, "
                           "medical_emergency or serious_misconduct.",
        },
    }
    return {
        "name": "create_ticket",
        "description": (
            "Register the caller's concern as a ticket AFTER you have read the name and a one-line "
            "summary back and they confirmed it. Returns the ticket number to read out. Call it "
            "exactly once per concern; do not speak while it runs."
        ),
        "parameters": {
            "type": "object",
            "properties": props,
            "required": ["caller_type", "language", "caller_name", "description", "category",
                         "high_priority_reason"],
        },
    }


def build_tools(categories=None, langs=None):
    """The function-declaration list for ``GeminiLive(tools=[{"function_declarations": ...}])``."""
    try:
        create = create_ticket_declaration(categories, langs)
    except Exception:
        logger.exception("create_ticket declaration failed; using a category-free fallback")
        create = create_ticket_declaration([], None)
    return [create, LOOKUP_TICKET_DECLARATION, END_CALL_DECLARATION]


def tool_names(categories=None):
    return [t["name"] for t in build_tools(categories)]


# Tools whose completion means "the caller's task is done" for the bridge's hangup logic.
COMPLETION_TOOLS = ("create_ticket", "lookup_ticket")
