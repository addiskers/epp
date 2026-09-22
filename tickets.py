"""The ticketing service.

Everything a ticket does lives here: numbering, the two Gemini tool handlers
(create_ticket / lookup_ticket), the status machine, the event log, the spoken forms the
agent reads out, and the post-call refinement that runs once the transcript is final.

The tool handlers are SYNC (GeminiLive runs them in a thread executor) and never raise:
a failure comes back as {"ok": false, "error": ...} with an instruction telling the agent
what to say, because a stack trace mid-call is a dead line for the caller.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import eo_db
import languages
import routing

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

STATUSES = eo_db.TICKET_STATUSES
STATUS_LABEL = {
    "open": "Open", "under_review": "Under Review", "escalated": "Escalated",
    "resolved": "Resolved", "closed": "Closed",
}
# Allowed moves. Reopening a resolved/closed ticket is admin-only (see change_status).
TRANSITIONS = {
    "open":         {"under_review", "escalated", "resolved", "closed"},
    "under_review": {"open", "escalated", "resolved", "closed"},
    "escalated":    {"under_review", "resolved", "closed"},
    "resolved":     {"closed", "open"},
    "closed":       {"open"},
}
_REOPEN = {("resolved", "open"), ("closed", "open")}

_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_SPOKEN_DIGIT = {d: w for w, d in _DIGIT_WORDS.items() if w not in ("oh", "o")}


class TicketError(ValueError):
    pass


def prefix() -> str:
    raw = (os.getenv("EPP_TICKET_PREFIX") or "EPP").strip().upper()
    return re.sub(r"[^A-Z0-9]", "", raw) or "EPP"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------------------
# Spoken forms
# ---------------------------------------------------------------------------------------
def spoken_ticket_id(ticket_id: str) -> str:
    """'EPP-2026-000001' -> 'E, P, P — two zero two six — zero zero zero zero zero one'.

    Letters and digits one at a time, with a long pause between the three parts: this is
    the string the caller writes down, so it is paced for a pen, not for reading."""
    parts = str(ticket_id or "").split("-")
    spoken = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            spoken.append(" ".join(_SPOKEN_DIGIT[d] for d in part))
        else:
            spoken.append(", ".join(ch.upper() for ch in part if ch.isalnum()))
    return " — ".join(spoken)


def spoken_date(iso: str) -> str:
    try:
        d = datetime.fromisoformat(str(iso)).astimezone(_IST)
    except (TypeError, ValueError):
        return ""
    return d.strftime("%d %B %Y").lstrip("0")


def normalize_ticket_number(text, now=None):
    """Whatever the caller said -> the canonical id, or None.

    Accepts 'EPP-2026-000123', 'epp 2026 123', 'EPP2026000123', '2026-123', '2026000123',
    '000123', '123', and spoken digits ('one two three'). A bare sequence number is taken
    to be this year's."""
    s = str(text or "").strip().lower()
    if not s:
        return None
    # spoken digits -> digits (English only; the model usually hands us digits anyway)
    words = re.findall(r"[a-z]+|\d+", s)
    if words and any(w in _DIGIT_WORDS for w in words):
        s = " ".join(_DIGIT_WORDS.get(w, w) for w in words)
        s = re.sub(r"(?<=\d) (?=\d)", "", s)
    groups = re.findall(r"\d+", s)
    if not groups:
        return None
    year_now = (now or datetime.now(_IST)).year
    digits = "".join(groups)
    if len(groups) >= 2 and len(groups[0]) == 4 and groups[0].startswith("20"):
        year, seq = int(groups[0]), "".join(groups[1:])
    elif len(digits) == 10 and digits.startswith("20"):
        year, seq = int(digits[:4]), digits[4:]
    elif len(digits) > 6 and len(digits) <= 10 and digits[:4].startswith("20") and int(digits[:4]) <= year_now + 1:
        year, seq = int(digits[:4]), digits[4:]
    else:
        year, seq = year_now, digits
    try:
        n = int(seq)
    except ValueError:
        return None
    if n <= 0 or n > 999999 or year < 2000 or year > 2100:
        return None
    return f"{prefix()}-{year}-{n:06d}"


def _clean_phone(raw, fallback=""):
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        digits = re.sub(r"\D", "", str(fallback or ""))
    if not digits:
        return ""
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if str(raw or fallback or "").strip().startswith("+"):
        return "+" + digits
    return "+" + digits


def _s(v, max_len=300):
    return str(v or "").strip()[:max_len]


# ---------------------------------------------------------------------------------------
# Tool handlers (sync; called from GeminiLive's executor)
# ---------------------------------------------------------------------------------------
_CONFIRMATION_INSTRUCTION = (
    "SYSTEM NOTE — the ticket is registered. Say say_now in the caller's language, slowly, exactly as "
    "given (the number letter by letter, digit by digit). Then repeat only the number once, then ask "
    "if there is anything else. Do not promise any outcome. Never say a number other than this one."
)


def confirmation_line(ticket_id: str) -> str:
    """The verbatim confirmation sentence with the real number embedded. Handed to the model in
    the tool result so it never has to compose (or invent) the number itself."""
    return ("Thank you. Your concern has been successfully registered. Your reference number is "
            f"{spoken_ticket_id(ticket_id)}. Your concern will be forwarded to the concerned "
            "department for review and action.")


def create_from_tool(args: dict, call_meta: dict | None = None) -> dict:
    """create_ticket tool handler. Never raises."""
    call_meta = call_meta or {}
    try:
        caller_type = routing.normalize_caller_type(args.get("caller_type"))
        if not caller_type:
            return {"ok": False, "error": "caller_type must be customer, vendor or employee",
                    "instruction": "Ask the caller once more whether they are a Customer, Vendor or Employee, then call create_ticket again."}
        description = _s(args.get("description"), 6000)
        if len(description) < 5:
            return {"ok": False, "error": "description is empty",
                    "instruction": "You have not captured the concern yet. Ask the caller to explain it, then call create_ticket again."}
        if not _s(args.get("caller_name"), 120):
            return {"ok": False, "error": "caller_name is empty",
                    "instruction": "You do not have the caller's name. Ask for it once more, politely. If they "
                                   "decline, call create_ticket again with caller_name set to 'Not given'."}

        categories = eo_db.list_categories(active_only=True)
        cat = routing.resolve_category(caller_type, args.get("category"), categories)
        category_name = cat.get("name") if cat else _s(args.get("category")) or "Other"
        dept_id = cat.get("department_id") if cat else None
        priority, flag, why = routing.detect_priority(args.get("high_priority_reason"), cat, description)

        lang = languages.normalize(args.get("language")) or ""
        now = datetime.now(_IST)
        campaign_id = call_meta.get("campaign_id")
        ticket_id = eo_db.next_ticket_id(prefix(), now.year)
        eo_db.create_ticket(
            ticket_id,
            call_id=call_meta.get("call_id") or "",
            call_sid=call_meta.get("call_sid") or "",
            caller_type=caller_type,
            caller_name=_s(args.get("caller_name"), 120),
            company_name=_s(args.get("company_name"), 200),
            vendor_code=_s(args.get("vendor_code"), 60),
            employee_id=_s(args.get("employee_id"), 60),
            caller_department=_s(args.get("caller_department"), 120),
            plant_location=_s(args.get("plant_location"), 120),
            contact_number=_clean_phone(args.get("contact_number"), call_meta.get("caller")),
            language=lang,
            category=category_name,
            subcategory=_s(args.get("subcategory"), 200),
            priority=priority,
            escalation_flag=flag,
            escalation_reason=why,
            description=description,
            assigned_department_id=dept_id,
            status="open",
            source="campaign" if campaign_id else "voice",
        )
        dept_name = (eo_db.get_department(dept_id) or {}).get("name") if dept_id else ""
        eo_db.add_ticket_event(ticket_id, "created", actor_type="ai", actor_name="Helpline agent",
                               to_value="open",
                               note=f"{caller_type} · {category_name} · {priority}"
                                    + (f" · {why}" if why else "")
                                    + (f" · registered on an outbound call, campaign #{campaign_id}"
                                       if campaign_id else ""))
        if dept_id:
            eo_db.add_ticket_event(ticket_id, "assigned", actor_type="system", to_value=dept_name or str(dept_id),
                                   note=f"routed by category '{category_name}'")
        logger.info("TICKET %s created: %s/%s prio=%s dept=%s call=%s",
                    ticket_id, caller_type, category_name, priority, dept_name, call_meta.get("call_id"))
        return {
            "ok": True,
            "ticket_id": ticket_id,
            "spoken_ticket_id": spoken_ticket_id(ticket_id),
            "say_now": confirmation_line(ticket_id),
            "status": "open",
            "status_spoken": STATUS_LABEL["open"],
            "assigned_department": dept_name or "",
            "priority": priority,
            "instruction": _CONFIRMATION_INSTRUCTION,
        }
    except Exception as e:
        logger.exception("create_ticket failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "instruction": "The ticket could not be registered because of a technical problem. Apologise, "
                               "ask the caller to call again in a few minutes, and do NOT invent a number."}


_UPDATABLE = ("caller_name", "contact_number", "company_name", "vendor_code", "employee_id",
              "caller_department", "plant_location")


def update_from_tool(args: dict, call_meta: dict | None, created_ids) -> dict:
    """update_ticket tool handler: correct or extend a ticket registered on THIS call.

    `created_ids` are the ticket ids the recorder saw create_ticket return on this call — the
    only tickets the agent may touch. Never raises."""
    args = args or {}
    created_ids = set(created_ids or [])
    try:
        tid = normalize_ticket_number(args.get("ticket_id"))
        if not tid or tid not in created_ids:
            return {"ok": False, "error": "not a ticket from this call",
                    "instruction": "You can only correct a ticket you registered on this call. If the caller "
                                   "wants to change an older ticket, note it as a new concern instead."}
        ticket = eo_db.get_ticket(tid)
        if not ticket:
            return {"ok": False, "error": "ticket not found",
                    "instruction": "That ticket could not be found. Continue without changing it."}
        fields, changed, summary = {}, [], []
        for key in _UPDATABLE:
            val = _s(args.get(key), 200)
            if not val:
                continue
            if key == "contact_number":
                val = _clean_phone(val)
            if val == (ticket.get(key) or ""):
                continue
            fields[key] = val
            changed.append(key)
            summary.append(f"{key} is now {val}")
            eo_db.add_ticket_event(tid, "corrected", actor_type="ai", actor_name="Helpline agent",
                                   from_value=ticket.get(key) or "", to_value=val,
                                   note=f"{key.replace('_', ' ')} corrected on the call")
        addition = _s(args.get("additional_details"), 2000)
        if addition:
            fields["description"] = (ticket.get("description") or "").rstrip() + "\n\nAdded later on the call: " + addition
            changed.append("additional_details")
            summary.append("the concern now includes the additional details")
            eo_db.add_ticket_event(tid, "note", actor_type="ai", actor_name="Helpline agent",
                                   note="Added on the call: " + addition)
        if not changed:
            return {"ok": False, "error": "nothing to change",
                    "instruction": "Nothing was changed — the values you passed are blank or already on the "
                                   "ticket. Continue the call."}
        eo_db.update_ticket(tid, **fields)
        logger.info("TICKET %s updated on call %s: %s", tid, (call_meta or {}).get("call_id"), ", ".join(changed))
        return {"ok": True, "ticket_id": tid, "changed": changed,
                "instruction": "SYSTEM NOTE — updated: " + "; ".join(summary) + ". Confirm ONCE in one short "
                               "sentence in the caller's language, then continue. Do not read the reference "
                               "number again unless they ask."}
    except Exception as e:
        logger.exception("update_ticket failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "instruction": "The correction could not be saved. Apologise briefly and continue."}


def lookup(ticket_number) -> dict:
    """lookup_ticket tool handler. Never raises; never invents."""
    try:
        tid = normalize_ticket_number(ticket_number)
        row = eo_db.get_ticket(tid) if tid else None
        if not row:
            return {"found": False, "ticket_id": tid or "",
                    "instruction": "No ticket with that number exists. Say so, ask them to check the number "
                                   "once, and offer to register the concern afresh. Never guess a status."}
        status = row.get("status") or "open"
        return {
            "found": True,
            "ticket_id": row["ticket_id"],
            "spoken_ticket_id": spoken_ticket_id(row["ticket_id"]),
            "status": status,
            "status_spoken": STATUS_LABEL.get(status, status),
            "created_at_spoken": spoken_date(row.get("created_at")),
            "assigned_department": row.get("assigned_department") or "",
            "instruction": "Tell the caller ONLY this status (and the department if given), in their "
                           "language. Do not add what will happen next or when.",
        }
    except Exception as e:
        logger.exception("lookup_ticket failed")
        return {"found": False, "error": f"{type(e).__name__}: {e}",
                "instruction": "The status could not be checked because of a technical problem. Apologise "
                               "and ask them to call again in a few minutes."}


# ---------------------------------------------------------------------------------------
# Status machine and edits (used by the API)
# ---------------------------------------------------------------------------------------
def _actor(user):
    user = user or {}
    return {"actor_type": "user" if user else "system", "actor_user_id": user.get("id"),
            "actor_name": user.get("name") or user.get("username") or ""}


def change_status(ticket: dict, new_status: str, user=None, note="") -> dict:
    new_status = str(new_status or "").strip().lower()
    cur = ticket.get("status") or "open"
    if new_status not in STATUSES:
        raise TicketError(f"Unknown status '{new_status}'")
    if new_status == cur:
        raise TicketError(f"Ticket is already {STATUS_LABEL[cur]}")
    if new_status not in TRANSITIONS.get(cur, set()):
        raise TicketError(f"Cannot move a ticket from {STATUS_LABEL[cur]} to {STATUS_LABEL[new_status]}")
    if (cur, new_status) in _REOPEN and (user or {}).get("role") != "admin":
        raise TicketError("Only an admin can reopen a resolved or closed ticket")
    fields = {"status": new_status}
    now = _now_iso()
    if new_status == "resolved":
        fields["resolved_at"] = now
    elif new_status == "closed":
        fields["closed_at"] = now
        if not ticket.get("resolved_at"):
            fields["resolved_at"] = now
    elif new_status == "open":
        fields["resolved_at"] = None
        fields["closed_at"] = None
    eo_db.update_ticket(ticket["ticket_id"], **fields)
    eo_db.add_ticket_event(ticket["ticket_id"], "status", from_value=cur, to_value=new_status,
                           note=note or "", **_actor(user))
    return eo_db.get_ticket(ticket["ticket_id"])


def assign(ticket: dict, department_id=None, user_id=None, user=None, note="") -> dict:
    fields = {}
    events = []
    if department_id is not None:
        dep = eo_db.get_department(department_id) if department_id != "" else None
        if department_id != "" and not dep:
            raise TicketError("Department not found")
        fields["assigned_department_id"] = dep["id"] if dep else None
        old = ticket.get("assigned_department") or ""
        events.append(("assigned", old, dep["name"] if dep else ""))
    if user_id is not None:
        u = eo_db.get_user(int(user_id)) if user_id != "" else None
        if user_id != "" and not u:
            raise TicketError("User not found")
        fields["assigned_user_id"] = u["id"] if u else None
        old = ticket.get("assigned_user_name") or ticket.get("assigned_username") or ""
        events.append(("assigned_user", old, (u or {}).get("name") or (u or {}).get("username") or ""))
    if not fields:
        raise TicketError("Nothing to assign")
    eo_db.update_ticket(ticket["ticket_id"], **fields)
    for kind, old, new in events:
        eo_db.add_ticket_event(ticket["ticket_id"], kind, from_value=old, to_value=new,
                               note=note or "", **_actor(user))
    return eo_db.get_ticket(ticket["ticket_id"])


def add_note(ticket: dict, note: str, user=None) -> dict:
    note = str(note or "").strip()
    if not note:
        raise TicketError("Note is empty")
    if len(note) > 2000:
        raise TicketError("Note is too long (max 2000 characters)")
    eo_db.add_ticket_event(ticket["ticket_id"], "note", note=note, **_actor(user))
    eo_db.update_ticket(ticket["ticket_id"], status=ticket.get("status"))   # bumps updated_at
    return eo_db.get_ticket(ticket["ticket_id"])


def set_priority(ticket: dict, priority: str, user=None, note="") -> dict:
    priority = str(priority or "").strip().lower()
    if priority not in eo_db.PRIORITIES:
        raise TicketError("Priority must be high, medium or low")
    cur = ticket.get("priority") or "medium"
    if priority == cur:
        return ticket
    fields = {"priority": priority}
    if priority == "high":
        fields["escalation_flag"] = 1
        fields["escalation_reason"] = ticket.get("escalation_reason") or "manual"
    eo_db.update_ticket(ticket["ticket_id"], **fields)
    eo_db.add_ticket_event(ticket["ticket_id"], "priority", from_value=cur, to_value=priority,
                           note=note or "", **_actor(user))
    return eo_db.get_ticket(ticket["ticket_id"])


def reclassify(ticket: dict, category_name: str, user=None, note="") -> dict:
    """Manual or AI reclassification: changes the category AND re-routes to that category's
    department, keeping the first category in category_original for the record."""
    categories = eo_db.list_categories(active_only=True)
    cat = routing.resolve_category(ticket.get("caller_type"), category_name, categories)
    # resolve_category falls back to 'Other' for an unknown name — fine for a live call, but a
    # deliberate reclassification to a name that does not exist for this caller type is a mistake.
    if not cat or (cat["name"] == "Other" and str(category_name or "").strip().lower() != "other"):
        raise TicketError("Category not found for this caller type")
    old = ticket.get("category") or ""
    if cat["name"] == old:
        return ticket
    fields = {"category": cat["name"]}
    if not ticket.get("category_original"):
        fields["category_original"] = old
    if cat.get("department_id"):
        fields["assigned_department_id"] = cat["department_id"]
    if int(cat.get("high_priority", 0) or 0) and ticket.get("priority") != "high":
        fields["priority"] = "high"
        fields["escalation_flag"] = 1
        fields["escalation_reason"] = f"category:{cat['name']}"
    eo_db.update_ticket(ticket["ticket_id"], **fields)
    eo_db.add_ticket_event(ticket["ticket_id"], "reclassified", from_value=old, to_value=cat["name"],
                           note=note or "", **_actor(user))
    if cat.get("department_id") and cat["department_id"] != ticket.get("assigned_department_id"):
        eo_db.add_ticket_event(ticket["ticket_id"], "assigned",
                               from_value=ticket.get("assigned_department") or "",
                               to_value=cat.get("department_name") or "",
                               note=f"re-routed by category '{cat['name']}'", **_actor(user))
    return eo_db.get_ticket(ticket["ticket_id"])


# ---------------------------------------------------------------------------------------
# Post-call refinement
# ---------------------------------------------------------------------------------------
def analysis_enabled() -> bool:
    return (os.getenv("EPP_ANALYSIS_ENABLED", "true") or "").strip().lower() not in ("0", "false", "no", "off")


def _transcript_text(call) -> str:
    return "\n".join(f"{'Caller' if t.get('role') == 'user' else 'Agent'}: {t.get('text', '')}"
                     for t in (call or {}).get("transcript") or [])


def _caller_words(call) -> int:
    return sum(len(str(t.get("text") or "").split())
               for t in (call or {}).get("transcript") or [] if t.get("role") == "user")


def _was_reassigned_by_hand(ticket_id) -> bool:
    return any(e.get("actor_type") == "user" and e.get("kind") in ("assigned", "reclassified")
               for e in eo_db.list_ticket_events(ticket_id))


def apply_analysis(ticket: dict, result: dict, categories=None) -> dict:
    """Write the post-call findings onto a ticket. Raises priority, never lowers it;
    reclassifies only on a confident disagreement and never over a human's decision."""
    categories = categories if categories is not None else eo_db.list_categories(active_only=True)
    fields = {
        "ai_summary": _s(result.get("summary"), 2000),
        "intent": _s(result.get("intent"), 200),
        "sentiment_label": _s(result.get("sentiment_label"), 20),
        "escalation_flags": [str(f) for f in (result.get("escalation_flags") or []) if str(f).strip()],
    }
    try:
        fields["sentiment_score"] = max(-1.0, min(1.0, float(result.get("sentiment_score"))))
    except (TypeError, ValueError):
        pass
    if not ticket.get("subcategory") and result.get("subcategory"):
        fields["subcategory"] = _s(result.get("subcategory"), 200)
    # Fill caller fields the live agent left blank (never overwrite what the caller confirmed).
    for key in ("caller_name", "company_name", "vendor_code", "employee_id", "caller_department",
                "plant_location"):
        if not ticket.get(key) and result.get(key):
            fields[key] = _s(result.get(key), 200)
    if not ticket.get("contact_number") and result.get("contact_number"):
        fields["contact_number"] = _clean_phone(result.get("contact_number"))
    eo_db.update_ticket(ticket["ticket_id"], **fields)
    eo_db.add_ticket_event(ticket["ticket_id"], "ai_analysis", actor_type="ai", actor_name="Post-call analysis",
                           note=fields["ai_summary"] or "(no summary)")

    # Priority: raise only.
    wanted = routing.max_priority(ticket.get("priority"), result.get("priority"))
    if wanted == "high" and (ticket.get("priority") or "") != "high":
        why = ", ".join(fields["escalation_flags"]) or routing.keyword_reason(result.get("summary") or "") or "post_call"
        eo_db.update_ticket(ticket["ticket_id"], priority="high", escalation_flag=1, escalation_reason=why)
        eo_db.add_ticket_event(ticket["ticket_id"], "priority", actor_type="ai", actor_name="Post-call analysis",
                               from_value=ticket.get("priority") or "", to_value="high", note=why)
    elif fields["escalation_flags"] and not int(ticket.get("escalation_flag") or 0):
        eo_db.update_ticket(ticket["ticket_id"], escalation_flag=1,
                            escalation_reason=", ".join(fields["escalation_flags"]))

    # Category: reclassify on a confident disagreement, unless a person already decided.
    try:
        conf = float(result.get("category_confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    new_cat = routing.resolve_category(ticket.get("caller_type"), result.get("category"), categories)
    if (new_cat and new_cat.get("name") != (ticket.get("category") or "") and conf >= 0.7
            and new_cat.get("name") != "Other" and not _was_reassigned_by_hand(ticket["ticket_id"])):
        current = eo_db.get_ticket(ticket["ticket_id"])
        reclassify(current, new_cat["name"], user=None,
                   note=f"post-call analysis (confidence {conf:.2f})")
    return eo_db.get_ticket(ticket["ticket_id"])


async def post_call(call_id: str):
    """Run once the call record is final: summarise, classify, and either refine the
    ticket the live agent created or create one from the transcript when it did not.
    Returns the ticket (or None). Never raises."""
    call = None
    try:
        import analysis
        import store
        call = await store.load_call(call_id)
        if not call:
            return None
        if not analysis_enabled():
            await _mark_analysis(store, call, "skipped", "EPP_ANALYSIS_ENABLED is off")
            return None
        if _caller_words(call) < 8:
            logger.info("post-call %s: too little caller speech to analyse", call_id)
            await _mark_analysis(store, call, "skipped", "too little caller speech to analyse")
            return None
        categories = eo_db.list_categories(active_only=True)
        existing = eo_db.tickets_by_call(call_id)
        ticket = existing[-1] if existing else None
        result = await analysis.analyze(call, categories, ticket)
        if not result:
            await _mark_analysis(store, call, "failed",
                                 analysis.last_error() or "the analysis model returned nothing usable")
            return None

        if ticket is None and result.get("should_create_ticket") and _s(result.get("description"), 6000):
            caller_type = routing.normalize_caller_type(result.get("caller_type"))
            if caller_type:
                res = create_from_tool(
                    {
                        "caller_type": caller_type,
                        "language": result.get("language") or call.get("language") or "",
                        "caller_name": result.get("caller_name") or "",
                        "contact_number": result.get("contact_number") or "",
                        "company_name": result.get("company_name") or "",
                        "vendor_code": result.get("vendor_code") or "",
                        "employee_id": result.get("employee_id") or "",
                        "caller_department": result.get("caller_department") or "",
                        "plant_location": result.get("plant_location") or "",
                        "description": result.get("description") or "",
                        "category": result.get("category") or "Other",
                        "subcategory": result.get("subcategory") or "",
                        "high_priority_reason": (result.get("escalation_flags") or ["none"])[0],
                    },
                    {"call_id": call.get("id"), "call_sid": call.get("call_sid"), "caller": call.get("caller")},
                )
                if res.get("ok"):
                    eo_db.update_ticket(res["ticket_id"], source="post_call")
                    eo_db.add_ticket_event(res["ticket_id"], "note", actor_type="ai", actor_name="Post-call analysis",
                                           note="Created from the transcript: the call ended before the agent registered it.")
                    ticket = eo_db.get_ticket(res["ticket_id"])
                    logger.info("post-call %s: created %s from the transcript", call_id, res["ticket_id"])

        if ticket is not None:
            ticket = apply_analysis(ticket, result, categories)

        call["analysis"] = {
            "status": "done",
            "summary": result.get("summary"), "intent": result.get("intent"),
            "sentiment_score": result.get("sentiment_score"), "sentiment_label": result.get("sentiment_label"),
            "escalation_flags": result.get("escalation_flags") or [],
            "is_status_inquiry": bool(result.get("is_status_inquiry")),
            "should_create_ticket": bool(result.get("should_create_ticket")),
            "analysed_at": _now_iso(),
        }
        if ticket is not None:
            call["ticket_id"] = ticket["ticket_id"]
            ids = call.get("ticket_ids") or []
            if ticket["ticket_id"] not in ids:
                call["ticket_ids"] = ids + [ticket["ticket_id"]]
        await store.save_call(call)
        return ticket
    except Exception as e:
        logger.exception("post-call analysis failed for %s", call_id)
        if call is not None:
            try:
                import store
                await _mark_analysis(store, call, "failed", f"{type(e).__name__}: {e}")
            except Exception:
                pass
        return None


async def _mark_analysis(store, call, status, reason):
    """Leave a visible trace on the call record when the post-call pass could not run — the
    admin's call drawer shows it, so a missing ticket has a stated reason instead of silence."""
    call["analysis"] = {"status": status, "error": str(reason or "")[:500], "analysed_at": _now_iso()}
    await store.save_call(call)


def schedule_post_call(call_id: str):
    """Fire-and-forget from the recorder's close(). Safe when no loop is running."""
    if not analysis_enabled() or not call_id:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(post_call(call_id))
