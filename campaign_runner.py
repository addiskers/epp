"""
Campaign runner — paced outbound dialing for the helpline's campaigns.

A single in-process asyncio task (one uvicorn worker). It:

  1. Promotes `scheduled` campaigns to `live` when their start time arrives.
  2. For each live campaign, PACES outbound dials: at most EPP_CAMPAIGN_MAX_PER_TICK new
     dials per tick and at most EPP_CAMPAIGN_MAX_CONCURRENT calls "in flight" per campaign,
     inside the global MAX_LIVE_CALLS budget it shares with inbound helpline calls — it
     never blasts the whole list at once, and it can never starve the helpline.
  3. Reaps in-flight ('calling') contacts by their call record. Answered → `done` (with
     the recorded outcome); `callback` / `not_reachable` → retried per the campaign's
     settings; no record within the ring window → no-answer, retried; retries exhausted →
     `failed`.
  4. Marks a campaign `completed` once no contact is pending or calling.

Gated by an admin kill switch (set_override) plus EPP_CAMPAIGN_RUNNER_ENABLED, and it will
not dial when Plivo credentials are absent.
"""

import asyncio
import logging
import os
from datetime import datetime, time as dtime, timedelta, timezone

import calling_window
import campaigns
import dialer
import eo_db
import store

logger = logging.getLogger(__name__)

# Outcomes meaning "no live person gave us an answer" — retried like a ring-out.
_UNANSWERED_OUTCOMES = frozenset({"callback", "not_reachable"})

# In-memory admin override (None = follow env). Set via the admin toggle endpoint.
_enabled_override = None


def _cfg_int(name, default):
    """EPP_* first, the legacy EO_* spelling second, then the default."""
    for key in (name, name.replace("EPP_", "EO_", 1)):
        raw = os.getenv(key)
        if raw not in (None, ""):
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    return default


def _max_active_campaigns():
    return campaigns.max_active()


def set_override(value):
    """True/False to force on/off, None to follow the env var."""
    global _enabled_override
    _enabled_override = value


def is_enabled():
    if _enabled_override is not None:
        return _enabled_override
    raw = os.getenv("EPP_CAMPAIGN_RUNNER_ENABLED", os.getenv("EO_CAMPAIGN_RUNNER_ENABLED", "true"))
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _plivo_ready():
    return bool(os.getenv("PLIVO_AUTH_ID") and os.getenv("PLIVO_AUTH_TOKEN") and os.getenv("PLIVO_FROM_NUMBER"))


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _apply_failure(cc, campaign, now, error=None):
    """A dial attempt did not reach a live answer (dial error, ring-out, machine, or a
    callback request). Schedule a retry per the campaign settings, or mark failed when
    exhausted. `attempts` and the per-day counter were already bumped at dial time."""
    delay_h = int(campaign.get("callback_delay_hours") or 4)
    max_day = max(1, int(campaign.get("callback_max_per_day") or 3))
    days = max(1, int(campaign.get("callback_days") or 1))
    attempts = int(cc.get("attempts") or 0)
    max_total = max_day * days

    first = _parse(cc.get("created_at")) or now
    days_elapsed = (now.date() - first.date()).days

    fields = {"last_error": (error or "no answer")}
    if attempts >= max_total or days_elapsed >= days:
        fields["call_status"] = "failed"
        fields["next_attempt_at"] = None
    else:
        fields["call_status"] = "pending"
        if int(cc.get("day_attempts") or 0) >= max_day:
            # Daily quota spent → resume next calendar day at window start; date AND time
            # must be computed in the calling tz (UTC math mis-dates near local midnight).
            tz = calling_window.tz()
            start_min = calling_window.campaign_window(campaign)[0]
            now_local = now.astimezone(tz)
            nxt = datetime.combine(now_local.date() + timedelta(days=1),
                                   dtime(start_min // 60, start_min % 60), tzinfo=tz)
        else:
            nxt = now + timedelta(hours=delay_h)
        fields["next_attempt_at"] = _iso(nxt)
    eo_db.cc_update(cc["id"], **fields)


async def _reap_calling(campaign, now):
    ring_window = _cfg_int("EPP_CAMPAIGN_NOANSWER_SECONDS", 90)
    for cc in eo_db.cc_by_status(campaign["id"], "calling"):
        last = _parse(cc.get("last_attempt_at")) or now
        rec = await store.find_campaign_call(cc["id"], since_iso=cc.get("last_attempt_at"))
        if rec:
            if not rec.get("ended_at"):
                continue                     # still on the call
            outcome = rec.get("outcome")
            if not outcome:
                # No record_outcome: a created ticket is the answer; otherwise the person
                # answered and the agent never recorded how it ended.
                outcome = "ticket_created" if rec.get("ticket_id") else "answered"
            if outcome in _UNANSWERED_OUTCOMES:
                eo_db.cc_update(cc["id"], outcome=outcome, last_call_id=rec.get("id"))
                _apply_failure(cc, campaign, now, error=outcome)
            else:
                fields = dict(call_status="done", outcome=outcome, last_call_id=rec.get("id"))
                note = (rec.get("outcome_note") or "").strip()
                if note and not (cc.get("remark") or "").strip():
                    fields["remark"] = note          # never over a human edit
                eo_db.cc_update(cc["id"], **fields)
        elif (now - last).total_seconds() > ring_window:
            _apply_failure(cc, campaign, now, error="no answer")


async def _dial_one(cc, campaign, now):
    today = now.date().isoformat()
    day_attempts = (int(cc.get("day_attempts") or 0) + 1) if cc.get("day_key") == today else 1
    attempts = int(cc.get("attempts") or 0) + 1
    # claim BEFORE dialing so a crash can't double-dial silently
    eo_db.cc_update(cc["id"], call_status="calling", attempts=attempts, day_attempts=day_attempts,
                    day_key=today, last_attempt_at=_iso(now), next_attempt_at=None)
    cc = {**cc, "attempts": attempts, "day_attempts": day_attempts, "day_key": today}
    try:
        res = await asyncio.wait_for(
            dialer.place_call(cc["phone"], base_url=os.getenv("PUBLIC_URL"),
                              campaign_id=campaign["id"], campaign_contact_id=cc["id"]),
            timeout=_cfg_int("EPP_CAMPAIGN_DIAL_TIMEOUT", 60))
    except asyncio.TimeoutError:
        res = {"error": "dial timeout"}
    if res.get("success"):
        eo_db.cc_update(cc["id"], last_call_id=res.get("call_uuid"))
    else:
        _apply_failure(cc, campaign, now, error=res.get("error"))


async def dial_contact_now(campaign_id, cc_id):
    """Admin 'Call now': dial ONE campaign contact immediately, even if the campaign is
    scheduled for later or already finished. Re-activates a non-live campaign so the runner
    then tracks the call (reap / retries), guarded by the active-campaign cap."""
    campaign = eo_db.get_campaign(campaign_id)
    cc = eo_db.get_campaign_contact(cc_id)
    if not campaign or not cc or int(cc.get("campaign_id") or 0) != int(campaign_id):
        return {"error": "not found"}
    if cc.get("call_status") == "calling":
        return {"error": "already calling"}
    if not _plivo_ready():
        return {"error": "Plivo is not configured on the server (PLIVO_* / PUBLIC_URL)"}
    prev = campaign.get("status")
    if prev != "live":
        others = [c for c in eo_db.active_campaigns() if int(c["id"]) != int(campaign_id)]
        cap = _max_active_campaigns()
        if len(others) >= cap:
            return {"error": f"{len(others)} campaigns are already active (limit {cap}) — "
                             f"finish or cancel one first."}
        eo_db.set_campaign_status(campaign_id, "live")
        campaign = {**campaign, "status": "live"}
        logger.info(f"Campaign {campaign_id} re-activated to live via Call-now (was {prev})")
    await _dial_one(cc, campaign, _now())
    logger.info(f"Call-now dialed contact {cc_id} ({cc.get('phone')}) in campaign {campaign_id}")
    return {"ok": True}


def _live_room():
    import main
    return main.live_room()


async def _process_campaign(campaign, now):
    await _reap_calling(campaign, now)
    if eo_db.cc_open_count(campaign["id"]) == 0:
        eo_db.set_campaign_status(campaign["id"], "completed")
        logger.info(f"Campaign {campaign['id']} '{campaign['name']}' completed")
        return
    if not _plivo_ready():
        return                       # nothing to dial with (dev/local) — leave pending
    # Calling-hours hard stop: no new dials outside the window (reap/completion above still run).
    start_min, end_min = calling_window.campaign_window(campaign)
    if not calling_window.in_call_window(start_min, end_min):
        return
    calling = len(eo_db.cc_by_status(campaign["id"], "calling"))
    budget = min(_cfg_int("EPP_CAMPAIGN_MAX_PER_TICK", 2),
                 max(0, _cfg_int("EPP_CAMPAIGN_MAX_CONCURRENT", 5) - calling))
    budget = min(budget, _live_room())          # shared with inbound helpline calls
    if budget <= 0:
        return
    for cc in eo_db.cc_pending_due(campaign["id"], _iso(now), budget):
        await _dial_one(cc, campaign, _now())


# Rotates the order campaigns are served in, so no single campaign can monopolise the
# shared live-call budget tick after tick.
_tick_count = 0


def _fair_order(campaign_list):
    if len(campaign_list) <= 1:
        return campaign_list
    offset = _tick_count % len(campaign_list)
    return campaign_list[offset:] + campaign_list[:offset]


async def _tick():
    global _tick_count
    if not is_enabled():
        return
    now = _now()
    try:
        eo_db.promote_due_campaigns(_iso(now))
    except Exception as e:
        logger.warning(f"promote_due_campaigns failed: {e}")
    live = _fair_order(eo_db.live_campaigns())
    _tick_count += 1
    for campaign in live:
        try:
            await _process_campaign(campaign, _now())
        except Exception as e:
            logger.warning(f"Campaign {campaign.get('id')} tick error: {e}")


async def run_loop():
    interval = _cfg_int("EPP_CAMPAIGN_POLL_INTERVAL", 30)
    logger.info(f"Campaign runner started (interval={interval}s, enabled={is_enabled()}, "
                f"plivo_ready={_plivo_ready()}, per_tick={_cfg_int('EPP_CAMPAIGN_MAX_PER_TICK', 2)}, "
                f"max_concurrent={_cfg_int('EPP_CAMPAIGN_MAX_CONCURRENT', 5)}, "
                f"max_active_campaigns={_max_active_campaigns()})")
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            logger.info("Campaign runner loop cancelled")
            raise
        except Exception as e:
            logger.warning(f"Campaign runner tick error: {e}")
        await asyncio.sleep(interval)
