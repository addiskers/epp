"""Campaign service: validation and creation, the per-call context for an outbound dial,
and the record_outcome tool handler. The runner (campaign_runner) drives the dialing.

Three campaign types, each selecting an agent row, a rendered prompt and a tool set:

  intake        — outbound version of the helpline; a concern becomes a ticket
  followup      — calls the person behind a ticket, reads its status, takes an update
  announcement  — reads an admin-written message, records the response
"""

import logging
import os
from datetime import datetime, timezone

import agent_tools
import calling_window
import eo_db
import epp_seeds
import prompt_render
import tickets
from tickets import _clean_phone

logger = logging.getLogger(__name__)

TYPE_LABEL = {"intake": "Intake round", "followup": "Ticket follow-up", "announcement": "Announcement"}


class CampaignError(ValueError):
    pass


def max_active() -> int:
    """How many campaigns may be scheduled/live at once. One cap for the service, the runner
    and the API, so the two entry points can never disagree."""
    try:
        return max(1, int(os.getenv("EPP_MAX_ACTIVE_CAMPAIGNS") or os.getenv("EO_MAX_ACTIVE_CAMPAIGNS") or "6"))
    except (TypeError, ValueError):
        return 6


def outcome_values(campaign_type) -> list:
    return [o["value"] for o in epp_seeds.OUTCOMES.get(campaign_type) or epp_seeds.OUTCOMES["intake"]]


def _parse_iso(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _whole(v, name, lo, hi, default):
    if v in (None, ""):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise CampaignError(f"{name} must be a whole number")
    if f != int(f) or not (lo <= int(f) <= hi):
        raise CampaignError(f"{name} must be a whole number between {lo} and {hi}")
    return int(f)


# ---------------------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------------------
def create(user, body) -> dict:
    """Validate a create payload, insert the campaign and its recipients. Raises CampaignError."""
    body = body or {}
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 160:
        raise CampaignError("Campaign name is required (max 160 characters)")
    ctype = str(body.get("campaign_type") or "").strip().lower()
    if ctype not in eo_db.CAMPAIGN_TYPES:
        raise CampaignError("campaign_type must be intake, followup or announcement")
    message = str(body.get("message") or "").strip()
    if ctype == "announcement" and not message:
        raise CampaignError("An announcement needs a message")
    if len(message) > 4000:
        raise CampaignError("Message is too long (max 4000 characters)")
    start = _parse_iso(body.get("start_at"))
    if not start:
        raise CampaignError("A valid start date/time is required")
    now = datetime.now(timezone.utc)
    if (now - start).total_seconds() > 120:
        raise CampaignError("Start time is in the past. Pick the current time or later.")
    delay_h = _whole(body.get("callback_delay_hours"), "Retry delay (hours)", 0, 720, 4)
    max_day = _whole(body.get("callback_max_per_day"), "Attempts per day", 1, 10, 3)
    days = _whole(body.get("callback_days"), "Retry days", 1, 10, 1)
    start_min = calling_window.hhmm_to_min(body.get("call_start") or "09:00", 540)
    end_min = calling_window.hhmm_to_min(body.get("call_end") or "21:00", 1260)

    rows, seen = [], set()
    if ctype == "followup":
        ids = [str(t).strip() for t in (body.get("ticket_ids") or []) if str(t).strip()]
        if not ids:
            raise CampaignError("Pick at least one ticket")
        for tid in ids:
            t = eo_db.get_ticket(tid)
            if not t:
                raise CampaignError(f"Ticket {tid} not found")
            phone = _clean_phone(t.get("contact_number"))
            if len(phone) < 11:
                raise CampaignError(f"Ticket {tid} has no contact number")
            if phone in seen:
                continue
            seen.add(phone)
            rows.append({"phone": phone, "name": t.get("caller_name") or "", "contact_id": None, "ticket_id": tid})
    else:
        contacts = [c for c in eo_db.get_contacts_by_ids(body.get("contact_ids") or [])
                    if c.get("status") == "valid"]
        for c in contacts:
            if c["phone"] in seen:
                continue
            seen.add(c["phone"])
            rows.append({"phone": c["phone"], "name": c.get("name") or "", "contact_id": c["id"], "ticket_id": None})
        if not rows:
            raise CampaignError("Select at least one valid contact")

    active = eo_db.active_campaigns()
    if len(active) >= max_active():
        raise CampaignError(f"{len(active)} campaigns are already scheduled or live (limit {max_active()}). "
                            "Cancel one or raise EPP_MAX_ACTIVE_CAMPAIGNS.")
    status = "live" if start <= now else "scheduled"
    cid = eo_db.create_campaign(name, ctype, start.astimezone(timezone.utc).isoformat(), user["id"],
                                message=message, status=status, callback_delay_hours=delay_h,
                                callback_max_per_day=max_day, callback_days=days,
                                call_start_min=start_min, call_end_min=end_min)
    eo_db.add_campaign_contacts(cid, rows)
    logger.info("Campaign %s '%s' created: type=%s recipients=%d status=%s", cid, name, ctype, len(rows), status)
    return eo_db.get_campaign_full(cid)


# ---------------------------------------------------------------------------------------
# Per-call context for a campaign dial
# ---------------------------------------------------------------------------------------
def _ticket_extra(t):
    if not t:
        return {}
    dept = t.get("assigned_department") or ""
    return {
        "caller_name": t.get("caller_name") or "",
        "ticket_id": t["ticket_id"],
        "ticket_id_spoken": tickets.spoken_ticket_id(t["ticket_id"]),
        "ticket_status": tickets.STATUS_LABEL.get(t.get("status"), t.get("status") or ""),
        "ticket_category": t.get("category") or "",
        "ticket_department": f", with the {dept} department" if dept else "",
        "ticket_created_spoken": tickets.spoken_date(t.get("created_at")),
    }


def call_context(campaign_id, cc_id, caller=""):
    """Everything a campaign dial's Live session needs: agent, rendered prompt, outbound
    trigger, tools. Falls back to the inbound context on any failure — a generic call beats
    a dropped one. Never raises."""
    import main
    campaign = eo_db.get_campaign(campaign_id) if campaign_id else None
    cc = eo_db.get_campaign_contact(cc_id) if cc_id else None
    if not campaign or not cc or int(cc.get("campaign_id") or 0) != int(campaign["id"]):
        ctx = main._resolve_call_context(caller=caller)
        ctx.update({"campaign": None, "cc": None, "ticket": None})
        return ctx
    ctx = {"agent": None, "system_instruction": None, "trigger": "", "tools": None, "missing": [],
           "campaign": campaign, "cc": cc, "ticket": None}
    try:
        st = main._static_context()
        ctype = campaign["campaign_type"]
        agent = eo_db.get_agent_by_slug(epp_seeds.AGENT_FOR_TYPE.get(ctype, "epp_intake")) or st["agent"]
        ticket = eo_db.get_ticket(cc["ticket_id"]) if cc.get("ticket_id") else None
        extra = {"campaign_name": campaign["name"], "campaign_message": campaign.get("message") or "",
                 "caller_name": cc.get("name") or ""}
        extra.update(_ticket_extra(ticket))
        rendered = prompt_render.render_prompt(
            agent, caller_phone=caller or cc.get("phone"), categories=st["categories"],
            departments=st["departments"], langs=st["langs"], extra=extra, outbound=True)
        ctx.update(agent=agent, ticket=ticket, system_instruction=rendered["system_instruction"],
                   trigger=rendered["trigger"], missing=rendered["missing"],
                   tools=agent_tools.build_tools(st["categories"], st["langs"], campaign_type=ctype))
    except Exception:
        logger.exception("campaign call context failed; using the inbound context")
        ctx.update(main._resolve_call_context(caller=caller))
    return ctx


# ---------------------------------------------------------------------------------------
# record_outcome tool handler
# ---------------------------------------------------------------------------------------
_MACHINE = ("voicemail", "voice mail", "answering machine", "machine")

_CLOSING_INSTRUCTION = (
    "SYSTEM NOTE — the outcome is saved; never mention it. If you have not yet said goodbye, "
    "say ONE short warm goodbye now and call end_call. If you already said it, produce NO audio."
)


def handle_record_outcome(args, call_meta):
    """record_outcome tool handler for outbound calls. Never raises; an outcome outside the
    campaign type's vocabulary is coerced to a recoverable one rather than dropped."""
    args = args or {}
    call_meta = call_meta or {}
    try:
        campaign = eo_db.get_campaign(call_meta.get("campaign_id")) or {}
        ctype = campaign.get("campaign_type") or "intake"
        allowed = outcome_values(ctype)
        status = str(args.get("outcome_status") or "").strip().lower()
        note = str(args.get("note") or "").strip()[:2000]
        if status not in allowed:
            hay = status + " " + note.lower()
            status = "not_reachable" if any(m in hay for m in _MACHINE) else "callback"
        cc = (eo_db.get_campaign_contact(call_meta.get("campaign_contact_id"))
              if call_meta.get("campaign_contact_id") else None)
        if ctype == "followup" and cc and cc.get("ticket_id") and status == "has_update" and note:
            eo_db.add_ticket_event(cc["ticket_id"], "note", actor_type="ai", actor_name="Follow-up call",
                                   note=note)
        logger.info("record_outcome: campaign=%s cc=%s -> %s", call_meta.get("campaign_id"),
                    call_meta.get("campaign_contact_id"), status)
        return {"ok": True, "outcome_status": status, "note": note,
                "callback_time_text": str(args.get("callback_time_text") or "").strip()[:200],
                "instruction": _CLOSING_INSTRUCTION}
    except Exception as e:
        logger.exception("record_outcome failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "instruction": "Say one short goodbye and call end_call."}
