"""campaigns.py — create/validate, per-call context, record_outcome; recorder + store hooks."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import campaigns
import store
import tickets
from recorder import CallRecorder

ADMIN = {"id": 1, "username": "admin", "name": "Admin", "role": "admin"}
FUTURE = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


@pytest.fixture
def db(fresh_eo_db, monkeypatch):
    monkeypatch.delenv("EPP_MAX_ACTIVE_CAMPAIGNS", raising=False)
    fresh_eo_db.init()
    import main
    main.invalidate_ctx_cache()
    return fresh_eo_db


def _contact(db, phone, name="A"):
    return db.add_contact(name, phone)[0]


def _ticket(phone="9876543210", **over):
    body = {"caller_type": "employee", "language": "en", "caller_name": "Rahul", "contact_number": phone,
            "description": "Salary pending two months.", "category": "Salary", "high_priority_reason": "none"}
    body.update(over)
    return tickets.create_from_tool(body, {})


def test_create_intake_campaign_from_contacts(db):
    c1, c2 = _contact(db, "+919000000001"), _contact(db, "+919000000002", "B")
    c = campaigns.create(ADMIN, {"name": "Round", "campaign_type": "intake", "contact_ids": [c1, c2, c1],
                                 "start_at": FUTURE, "call_start": "10:00", "call_end": "18:30"})
    assert c["status"] == "scheduled" and c["contact_count"] == 2       # deduped by phone
    assert (c["call_start_min"], c["call_end_min"]) == (600, 1110)
    rows = db.list_campaign_contacts(c["id"])["items"]
    assert {r["phone"] for r in rows} == {"+919000000001", "+919000000002"}
    assert rows[0]["contact_id"] == c1


def test_create_followup_campaign_from_tickets(db):
    t = _ticket()
    no_phone = _ticket(phone="", caller_type="vendor", caller_name="Jay", category="Invoice Query",
                       description="Invoice unpaid.")
    assert db.get_ticket(no_phone["ticket_id"])["contact_number"] == ""
    c = campaigns.create(ADMIN, {"name": "FU", "campaign_type": "followup", "ticket_ids": [t["ticket_id"]],
                                 "start_at": FUTURE})
    row = db.list_campaign_contacts(c["id"])["items"][0]
    assert row["ticket_id"] == t["ticket_id"] and row["phone"] == "+919876543210" and row["name"] == "Rahul"
    with pytest.raises(campaigns.CampaignError):
        campaigns.create(ADMIN, {"name": "FU2", "campaign_type": "followup",
                                 "ticket_ids": [no_phone["ticket_id"]], "start_at": FUTURE})
    with pytest.raises(campaigns.CampaignError):
        campaigns.create(ADMIN, {"name": "FU3", "campaign_type": "followup", "ticket_ids": [], "start_at": FUTURE})


def test_create_validation(db):
    cid = _contact(db, "+919000000001")
    bad = [
        {"name": "", "campaign_type": "intake", "contact_ids": [cid], "start_at": FUTURE},
        {"name": "x", "campaign_type": "party", "contact_ids": [cid], "start_at": FUTURE},
        {"name": "x", "campaign_type": "announcement", "contact_ids": [cid], "start_at": FUTURE, "message": ""},
        {"name": "x", "campaign_type": "intake", "contact_ids": [], "start_at": FUTURE},
        {"name": "x", "campaign_type": "intake", "contact_ids": [cid], "start_at": "2020-01-01T00:00:00Z"},
        {"name": "x", "campaign_type": "intake", "contact_ids": [cid], "start_at": "not a date"},
        {"name": "x", "campaign_type": "intake", "contact_ids": [cid], "start_at": FUTURE, "callback_max_per_day": 0},
    ]
    for body in bad:
        with pytest.raises(campaigns.CampaignError):
            campaigns.create(ADMIN, body)
    now_ok = campaigns.create(ADMIN, {"name": "now", "campaign_type": "intake", "contact_ids": [cid],
                                      "start_at": datetime.now(timezone.utc).isoformat()})
    assert now_ok["status"] == "live"                                     # "start now" goes live at once


def test_active_cap(db, monkeypatch):
    monkeypatch.setenv("EPP_MAX_ACTIVE_CAMPAIGNS", "1")
    cid = _contact(db, "+919000000001")
    campaigns.create(ADMIN, {"name": "a", "campaign_type": "intake", "contact_ids": [cid], "start_at": FUTURE})
    with pytest.raises(campaigns.CampaignError):
        campaigns.create(ADMIN, {"name": "b", "campaign_type": "intake", "contact_ids": [cid], "start_at": FUTURE})
    assert campaigns.max_active() == 1
    monkeypatch.setenv("EPP_MAX_ACTIVE_CAMPAIGNS", "junk")
    assert campaigns.max_active() == 6


def test_call_context_per_type(db):
    cid = _contact(db, "+919000000001", "Rahul")
    ann = campaigns.create(ADMIN, {"name": "Notice", "campaign_type": "announcement", "contact_ids": [cid],
                                   "start_at": FUTURE, "message": "Plant closed on Monday."})
    cc = db.list_campaign_contacts(ann["id"])["items"][0]
    ctx = campaigns.call_context(ann["id"], cc["id"], caller="+919000000001")
    assert ctx["agent"]["slug"] == "epp_announcement"
    assert "Plant closed on Monday." in ctx["system_instruction"]
    assert "calling from" in ctx["trigger"] and ctx["missing"] == []
    assert [t["name"] for t in ctx["tools"]] == ["create_ticket", "lookup_ticket", "update_ticket", "record_outcome", "end_call"]
    assert ctx["campaign"]["id"] == ann["id"] and ctx["cc"]["id"] == cc["id"]

    t = _ticket(phone="9000000001", language="hi")
    fu = campaigns.create(ADMIN, {"name": "FU", "campaign_type": "followup", "ticket_ids": [t["ticket_id"]], "start_at": FUTURE})
    cc = db.list_campaign_contacts(fu["id"])["items"][0]
    ctx = campaigns.call_context(fu["id"], cc["id"])
    assert ctx["agent"]["slug"] == "epp_followup" and ctx["missing"] == []
    assert "Finance" in ctx["system_instruction"] and "Open" in ctx["system_instruction"]
    assert "Rahul" in ctx["trigger"] and ctx["ticket"]["ticket_id"] == t["ticket_id"]

    intake = campaigns.create(ADMIN, {"name": "R", "campaign_type": "intake", "contact_ids": [cid], "start_at": FUTURE})
    cc = db.list_campaign_contacts(intake["id"])["items"][0]
    ctx = campaigns.call_context(intake["id"], cc["id"])
    assert ctx["agent"]["slug"] == "epp_intake" and "calling from" in ctx["trigger"]
    assert "record_outcome" in [x["name"] for x in ctx["tools"]]

    # unknown ids never raise: the inbound context comes back
    ctx = campaigns.call_context(999, 999)
    assert ctx["agent"]["slug"] == "epp_intake" and ctx["campaign"] is None
    assert "record_outcome" not in [x["name"] for x in ctx["tools"]]


def test_record_outcome_writes_followup_note_on_the_ticket(db):
    t = _ticket(phone="9000000001")
    fu = campaigns.create(ADMIN, {"name": "FU", "campaign_type": "followup", "ticket_ids": [t["ticket_id"]], "start_at": FUTURE})
    cc = db.list_campaign_contacts(fu["id"])["items"][0]
    meta = {"call_id": "c1", "campaign_id": fu["id"], "campaign_contact_id": cc["id"]}
    res = campaigns.handle_record_outcome({"outcome_status": "has_update", "note": "HR called him yesterday."}, meta)
    assert res["ok"] is True and res["outcome_status"] == "has_update" and "goodbye" in res["instruction"]
    events = db.list_ticket_events(t["ticket_id"])
    assert events[-1]["kind"] == "note" and "HR called him" in events[-1]["note"] and events[-1]["actor_type"] == "ai"
    n_before = len(events)
    confirmed = campaigns.handle_record_outcome({"outcome_status": "confirmed"}, meta)
    assert confirmed["outcome_status"] == "confirmed" and len(db.list_ticket_events(t["ticket_id"])) == n_before
    bad = campaigns.handle_record_outcome({"outcome_status": "party"}, meta)
    assert bad["ok"] is True and bad["outcome_status"] == "callback"      # coerced, never lost
    machine = campaigns.handle_record_outcome({"outcome_status": "voicemail", "note": "answering machine"}, meta)
    assert machine["outcome_status"] == "not_reachable"
    # no campaign at all: falls back to the intake vocabulary rather than failing
    assert campaigns.handle_record_outcome({"outcome_status": "no_concern"}, {})["outcome_status"] == "no_concern"


def test_ticket_created_on_a_campaign_call_is_marked(db):
    cid = _contact(db, "+919000000001")
    c = campaigns.create(ADMIN, {"name": "R", "campaign_type": "intake", "contact_ids": [cid], "start_at": FUTURE})
    cc = db.list_campaign_contacts(c["id"])["items"][0]
    res = tickets.create_from_tool(
        {"caller_type": "employee", "language": "en", "caller_name": "A", "description": "Salary pending.",
         "category": "Salary", "high_priority_reason": "none"},
        {"call_id": "x", "call_sid": "s", "caller": "+919000000001", "campaign_id": c["id"], "campaign_contact_id": cc["id"]})
    t = db.get_ticket(res["ticket_id"])
    assert t["source"] == "campaign"
    assert f"campaign #{c['id']}" in db.list_ticket_events(t["ticket_id"])[0]["note"]


def test_recorder_captures_outcome_and_campaign_ids(db, monkeypatch):
    monkeypatch.setenv("EPP_ANALYSIS_ENABLED", "false")

    async def run():
        r = CallRecorder(model="t")
        await r.open(source="plivo_campaign", campaign_id=5, campaign_contact_id=9, caller="+919000000001")
        assert r.call_meta["campaign_id"] == 5 and r.call_meta["campaign_contact_id"] == 9
        r._record_tool({"type": "tool_call", "name": "record_outcome", "args": {},
                        "result": {"ok": True, "outcome_status": "acknowledged", "note": "fine",
                                   "callback_time_text": "tomorrow 10am"}})
        r._record_tool({"type": "tool_call", "name": "record_outcome", "args": {}, "result": {"ok": False}})
        await r.close()
        return r.call, await store.find_campaign_call(9), await store.find_campaign_call(9, since_iso="2999-01-01")
    call, found, none = asyncio.run(run())
    assert call["campaign_id"] == 5 and call["campaign_contact_id"] == 9
    assert call["outcome"] == "acknowledged" and call["outcome_note"] == "fine"
    assert call["callback_time_text"] == "tomorrow 10am"
    assert found and found["id"] == call["id"] and none is None
    assert asyncio.run(store.list_calls({"campaign_id": 5}))["total"] >= 1
    assert asyncio.run(store.list_calls({"campaign_id": 6}))["total"] == 0


def test_followup_calls_skip_post_call_analysis(db, monkeypatch):
    scheduled = []
    monkeypatch.setattr(tickets, "schedule_post_call", lambda cid: scheduled.append(cid))
    t = _ticket(phone="9000000001")
    fu = campaigns.create(ADMIN, {"name": "FU", "campaign_type": "followup", "ticket_ids": [t["ticket_id"]], "start_at": FUTURE})
    cc = db.list_campaign_contacts(fu["id"])["items"][0]

    async def run():
        r = CallRecorder(model="t")
        await r.open(source="plivo_campaign", campaign_id=fu["id"], campaign_contact_id=cc["id"])
        await r.close()
        r2 = CallRecorder(model="t")
        await r2.open(source="plivo_inbound")
        await r2.close()
        return r.call["campaign_type"], r2.call["id"]
    ctype, inbound_id = asyncio.run(run())
    assert ctype == "followup" and scheduled == [inbound_id]


def test_live_room_counts_active_calls(monkeypatch):
    import main
    monkeypatch.setattr(main, "MAX_LIVE_CALLS", 3)
    monkeypatch.setattr(main, "_active_calls", {"a": {}, "b": {}})
    assert main.live_room() == 1
    monkeypatch.setattr(main, "_active_calls", {"a": {}, "b": {}, "c": {}, "d": {}})
    assert main.live_room() == 0
