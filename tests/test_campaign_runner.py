"""campaign_runner.py — retry math (IST next-day fix), reap routing by outcome, dial
claiming, fair rotation under a shared live-call budget, the kill switch."""
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import campaign_runner
import campaigns
import eo_db
import store

IST = ZoneInfo("Asia/Kolkata")

CAMPAIGN = {
    "id": 7, "status": "live", "campaign_type": "intake",
    "callback_delay_hours": 2, "callback_max_per_day": 3, "callback_days": 2,
    "call_start_min": 600, "call_end_min": 1260,   # 10:00-21:00 IST
}


def _cc(**over):
    cc = {"id": 42, "campaign_id": 7, "phone": "+919000000000", "name": "Rohan",
          "call_status": "calling", "attempts": 1, "day_attempts": 1,
          "created_at": datetime.now(timezone.utc).isoformat(),
          "last_attempt_at": datetime.now(timezone.utc).isoformat()}
    cc.update(over)
    return cc


def _capture_cc_update(monkeypatch):
    calls = []
    monkeypatch.setattr(eo_db, "cc_update", lambda cc_id, **fields: calls.append((cc_id, fields)))
    return calls


# --------------------------------------------------------------------------- retry math
def test_apply_failure_same_day_retry_after_delay_hours(monkeypatch):
    calls = _capture_cc_update(monkeypatch)
    now = datetime(2026, 7, 9, 10, 0, tzinfo=timezone.utc)
    campaign_runner._apply_failure(_cc(), CAMPAIGN, now, error="no answer")
    _, fields = calls[0]
    assert fields["call_status"] == "pending" and fields["last_error"] == "no answer"
    nxt = datetime.fromisoformat(fields["next_attempt_at"])
    assert abs((nxt - now).total_seconds() - 2 * 3600) < 2


def test_apply_failure_day_quota_resumes_next_ist_day_at_window_start(monkeypatch):
    calls = _capture_cc_update(monkeypatch)
    now = datetime(2026, 7, 9, 0, 30, tzinfo=timezone.utc)      # 06:00 IST
    campaign_runner._apply_failure(_cc(day_attempts=3), CAMPAIGN, now, error="no answer")
    _, fields = calls[0]
    assert fields["call_status"] == "pending"
    nxt_ist = datetime.fromisoformat(fields["next_attempt_at"]).astimezone(IST)
    assert nxt_ist.date() == datetime(2026, 7, 10).date()
    assert (nxt_ist.hour, nxt_ist.minute) == (10, 0)


def test_apply_failure_exhausted_marks_failed(monkeypatch):
    calls = _capture_cc_update(monkeypatch)
    now = datetime(2026, 7, 9, 10, 0, tzinfo=timezone.utc)
    campaign_runner._apply_failure(_cc(attempts=6), CAMPAIGN, now, error="no answer")   # 3/day × 2 days
    _, fields = calls[0]
    assert fields["call_status"] == "failed" and fields["next_attempt_at"] is None


# ------------------------------------------------------------------------------ reaping
def _run_reap(monkeypatch, rec, cc):
    calls = _capture_cc_update(monkeypatch)
    monkeypatch.setattr(eo_db, "cc_by_status", lambda cid, st: [cc])

    async def fake_find(campaign_contact_id, since_iso=None):
        assert campaign_contact_id == cc["id"]
        return rec
    monkeypatch.setattr(store, "find_campaign_call", fake_find)
    asyncio.run(campaign_runner._reap_calling(CAMPAIGN, datetime.now(timezone.utc)))
    return calls


def test_reap_not_reachable_and_callback_retry_not_done(monkeypatch):
    for outcome in ("not_reachable", "callback"):
        rec = {"id": "call1", "ended_at": "2026-07-09T10:00:00+00:00", "outcome": outcome}
        calls = _run_reap(monkeypatch, rec, _cc())
        assert calls[0][1] == {"outcome": outcome, "last_call_id": "call1"}
        fields = calls[1][1]
        assert fields["call_status"] == "pending" and fields["last_error"] == outcome
        assert fields["next_attempt_at"] is not None


def test_reap_final_outcomes_mark_done(monkeypatch):
    for outcome in ("no_concern", "confirmed", "has_update", "acknowledged", "declined", "wrong_number"):
        rec = {"id": "call2", "ended_at": "2026-07-09T10:00:00+00:00", "outcome": outcome}
        calls = _run_reap(monkeypatch, rec, _cc())
        assert len(calls) == 1
        assert calls[0][1]["call_status"] == "done" and calls[0][1]["outcome"] == outcome


def test_reap_a_call_that_created_a_ticket_is_done_even_without_an_outcome(monkeypatch):
    rec = {"id": "call3", "ended_at": "2026-07-09T10:00:00+00:00", "outcome": None,
           "ticket_id": "EPP-2026-000009", "outcome_note": ""}
    calls = _run_reap(monkeypatch, rec, _cc())
    assert calls[0][1]["call_status"] == "done" and calls[0][1]["outcome"] == "ticket_created"


def test_reap_answered_call_with_nothing_recorded_is_done_as_answered(monkeypatch):
    rec = {"id": "call4", "ended_at": "2026-07-09T10:00:00+00:00", "outcome": None, "ticket_id": None}
    calls = _run_reap(monkeypatch, rec, _cc())
    assert calls[0][1]["call_status"] == "done" and calls[0][1]["outcome"] == "answered"


def test_reap_note_rides_onto_the_remark_never_over_a_human_edit(monkeypatch):
    rec = {"id": "c", "ended_at": "x", "outcome": "no_concern", "outcome_note": "Said all fine."}
    assert _run_reap(monkeypatch, rec, _cc())[0][1]["remark"] == "Said all fine."
    assert "remark" not in _run_reap(monkeypatch, rec, _cc(remark="operator note"))[0][1]


def test_reap_still_on_the_call_leaves_it_alone(monkeypatch):
    rec = {"id": "c", "ended_at": None, "outcome": None}
    assert _run_reap(monkeypatch, rec, _cc()) == []


def test_reap_no_record_past_ring_window_is_no_answer(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    calls = _run_reap(monkeypatch, None, _cc(last_attempt_at=old))
    fields = calls[0][1]
    assert fields["last_error"] == "no answer" and fields["call_status"] in ("pending", "failed")


def test_reap_no_record_inside_ring_window_waits(monkeypatch):
    assert _run_reap(monkeypatch, None, _cc()) == []


# ------------------------------------------------------------------------------ dialing
def test_dial_one_claims_first_and_passes_campaign_and_contact_ids(fresh_eo_db, monkeypatch):
    db = fresh_eo_db
    db.init()
    cid = db.create_campaign("C", "intake", "2020-01-01T00:00:00+00:00", 1, status="live")
    db.add_campaign_contacts(cid, [{"phone": "+919000000001", "name": "A"}])
    cc = db.cc_by_status(cid, "pending")[0]
    seen = {}

    async def fake(phone, **kw):
        # by the time we dial, the contact is already claimed
        assert db.get_campaign_contact(cc["id"])["call_status"] == "calling"
        seen.update(kw, phone=phone)
        return {"success": True, "call_uuid": "u1"}
    monkeypatch.setattr(campaign_runner.dialer, "place_call", fake)
    asyncio.run(campaign_runner._dial_one(cc, db.get_campaign(cid), datetime.now(timezone.utc)))
    assert seen["campaign_id"] == cid and seen["campaign_contact_id"] == cc["id"] and seen["phone"] == "+919000000001"
    row = db.get_campaign_contact(cc["id"])
    assert row["call_status"] == "calling" and row["attempts"] == 1 and row["last_call_id"] == "u1"


def test_dial_failure_schedules_a_retry(fresh_eo_db, monkeypatch):
    db = fresh_eo_db
    db.init()
    cid = db.create_campaign("C", "intake", "2020-01-01T00:00:00+00:00", 1, status="live")
    db.add_campaign_contacts(cid, [{"phone": "+919000000001"}])
    cc = db.cc_by_status(cid, "pending")[0]

    async def fake(phone, **kw):
        return {"error": "carrier down"}
    monkeypatch.setattr(campaign_runner.dialer, "place_call", fake)
    asyncio.run(campaign_runner._dial_one(cc, db.get_campaign(cid), datetime.now(timezone.utc)))
    row = db.get_campaign_contact(cc["id"])
    assert row["call_status"] == "pending" and row["last_error"] == "carrier down" and row["next_attempt_at"]


def test_dial_contact_now_reactivates_and_dials(fresh_eo_db, monkeypatch):
    db = fresh_eo_db
    db.init()
    for k in ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"):
        monkeypatch.setenv(k, "t")
    monkeypatch.setenv("PLIVO_FROM_NUMBER", "+10000000000")
    cid = db.create_campaign("C", "intake", "2999-01-01T00:00:00+00:00", 1)     # scheduled far ahead
    db.add_campaign_contacts(cid, [{"phone": "+919000000001"}])
    cc = db.cc_by_status(cid, "pending")[0]
    dialled = []

    async def fake(phone, **kw):
        dialled.append(phone)
        return {"success": True, "call_uuid": "u"}
    monkeypatch.setattr(campaign_runner.dialer, "place_call", fake)
    assert asyncio.run(campaign_runner.dial_contact_now(cid, cc["id"])) == {"ok": True}
    assert dialled == ["+919000000001"] and db.get_campaign(cid)["status"] == "live"
    assert "error" in asyncio.run(campaign_runner.dial_contact_now(cid, cc["id"]))     # already calling
    assert "error" in asyncio.run(campaign_runner.dial_contact_now(999, cc["id"]))


def test_process_campaign_completes_when_nothing_is_open(fresh_eo_db, monkeypatch):
    db = fresh_eo_db
    db.init()
    cid = db.create_campaign("C", "intake", "2020-01-01T00:00:00+00:00", 1, status="live")
    db.add_campaign_contacts(cid, [{"phone": "+919000000001"}])
    cc = db.cc_by_status(cid, "pending")[0]
    db.cc_update(cc["id"], call_status="done", outcome="no_concern")
    asyncio.run(campaign_runner._process_campaign(db.get_campaign(cid), datetime.now(timezone.utc)))
    assert db.get_campaign(cid)["status"] == "completed"


def test_process_campaign_respects_calling_hours(fresh_eo_db, monkeypatch):
    db = fresh_eo_db
    db.init()
    for k in ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"):
        monkeypatch.setenv(k, "t")
    monkeypatch.setenv("PLIVO_FROM_NUMBER", "+10000000000")
    # a one-minute window that is never "now": start == end + 1 → effectively closed unless now hits it
    import calling_window
    monkeypatch.setattr(calling_window, "now_ist_min", lambda: 300)      # 05:00 IST
    cid = db.create_campaign("C", "intake", "2020-01-01T00:00:00+00:00", 1, status="live",
                             call_start_min=540, call_end_min=1260)
    db.add_campaign_contacts(cid, [{"phone": "+919000000001"}])
    dialled = []

    async def fake(phone, **kw):
        dialled.append(phone)
        return {"success": True, "call_uuid": "u"}
    monkeypatch.setattr(campaign_runner.dialer, "place_call", fake)
    asyncio.run(campaign_runner._process_campaign(db.get_campaign(cid), datetime.now(timezone.utc)))
    assert dialled == []


# ---------------------------------------------------------------------- fair rotation
def test_rotation_gives_each_campaign_the_first_claim_in_turn(monkeypatch):
    campaigns_ = [{"id": 1}, {"id": 2}, {"id": 3}]
    seen_first = []
    for tick in range(6):
        monkeypatch.setattr(campaign_runner, "_tick_count", tick)
        seen_first.append(campaign_runner._fair_order(campaigns_)[0]["id"])
    assert seen_first == [1, 2, 3, 1, 2, 3]
    monkeypatch.setattr(campaign_runner, "_tick_count", 7)
    assert campaign_runner._fair_order([{"id": 9}]) == [{"id": 9}]
    assert campaign_runner._fair_order([]) == []


def _six_campaign_world(db):
    db.init()
    past = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    for i in range(1, 7):
        cid = db.create_campaign(f"C{i}", "intake", past, 1, status="live", call_start_min=0, call_end_min=1439)
        db.add_campaign_contacts(cid, [{"phone": f"+919{i}{n:07d}", "name": f"G{n}"} for n in range(20)])


def _run_ticks(db, monkeypatch, ticks=10, rotate=True):
    import main
    monkeypatch.setattr(main, "MAX_LIVE_CALLS", 2)
    monkeypatch.setattr(main, "_active_calls", {})
    monkeypatch.setenv("EPP_CAMPAIGN_MAX_PER_TICK", "5")
    for k in ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"):
        monkeypatch.setenv(k, "t")
    monkeypatch.setenv("PLIVO_FROM_NUMBER", "+10000000000")
    campaign_runner.set_override(True)
    if not rotate:
        monkeypatch.setattr(campaign_runner, "_fair_order", lambda cs: cs)
    dialled = []

    async def fake_place_call(phone, **kw):
        dialled.append(kw.get("campaign_id"))
        main._active_calls[phone] = {}          # the call connects and HOLDS a slot
        return {"success": True, "call_uuid": "u"}
    monkeypatch.setattr(campaign_runner.dialer, "place_call", fake_place_call)

    async def run():
        for _ in range(ticks):
            await campaign_runner._tick()
            main._active_calls.clear()          # last tick's calls hang up, freeing the slots
    asyncio.run(run())
    campaign_runner.set_override(None)
    return dialled


def test_a_fixed_order_starves_the_tail_campaigns(fresh_eo_db, monkeypatch):
    _six_campaign_world(fresh_eo_db)
    dialled = _run_ticks(fresh_eo_db, monkeypatch, rotate=False)
    assert {1, 2, 3, 4, 5, 6} - set(dialled)


def test_rotation_lets_every_campaign_dial(fresh_eo_db, monkeypatch):
    _six_campaign_world(fresh_eo_db)
    dialled = _run_ticks(fresh_eo_db, monkeypatch, rotate=True)
    assert set(dialled) == {1, 2, 3, 4, 5, 6}


def test_kill_switch_and_shared_cap(monkeypatch):
    monkeypatch.delenv("EPP_CAMPAIGN_RUNNER_ENABLED", raising=False)
    campaign_runner.set_override(None)
    assert campaign_runner.is_enabled() is True
    monkeypatch.setenv("EPP_CAMPAIGN_RUNNER_ENABLED", "false")
    assert campaign_runner.is_enabled() is False
    campaign_runner.set_override(True)
    assert campaign_runner.is_enabled() is True
    campaign_runner.set_override(None)
    monkeypatch.setenv("EPP_MAX_ACTIVE_CAMPAIGNS", "4")
    assert campaigns.max_active() == campaign_runner._max_active_campaigns() == 4


def test_tick_does_nothing_when_disabled(fresh_eo_db, monkeypatch):
    db = fresh_eo_db
    db.init()
    campaign_runner.set_override(False)
    cid = db.create_campaign("C", "intake", "2020-01-01T00:00:00+00:00", 1)     # due, but not promoted
    asyncio.run(campaign_runner._tick())
    assert db.get_campaign(cid)["status"] == "scheduled"
    campaign_runner.set_override(None)


def test_reap_a_ticket_beats_a_recorded_no_concern_and_links_the_row(monkeypatch):
    """The agent recorded no_concern AND a ticket was created on the call (by the agent or the
    post-call pass). The ticket is the truth: the row must say ticket_created and carry the id."""
    rec = {"id": "c9", "ended_at": "x", "outcome": "no_concern", "ticket_id": "EPP-2026-000006", "outcome_note": ""}
    calls = _run_reap(monkeypatch, rec, _cc())
    assert len(calls) == 1
    fields = calls[0][1]
    assert fields["call_status"] == "done" and fields["outcome"] == "ticket_created"
    assert fields["ticket_id"] == "EPP-2026-000006"
    # a follow-up row already carries its ticket id — never overwrite it
    calls = _run_reap(monkeypatch, rec, _cc(ticket_id="EPP-2026-000001"))
    assert "ticket_id" not in calls[0][1]
