"""eo_db.py — contacts pool, campaigns, campaign_contacts (schema v3)."""
from datetime import datetime, timezone

PAST = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()


def _now():
    return datetime.now(timezone.utc).isoformat()


def test_schema_v3_has_campaign_tables(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    tables = {r[0] for r in db.get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"contacts", "campaigns", "campaign_contacts"} <= tables
    assert db.schema_version() == 3
    assert db.CAMPAIGN_TYPES == ("intake", "followup", "announcement")


def test_contacts_upsert_by_phone(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    cid, created = db.add_contact("Rahul", "+919876543210", caller_type="employee")
    assert created is True
    cid2, created2 = db.add_contact("", "+919876543210", notes="second")
    assert (cid2, created2) == (cid, False)
    row = db.get_contacts_by_ids([cid])[0]
    assert row["name"] == "Rahul" and row["notes"] == "second"        # blank name never wipes
    added, updated = db.bulk_upsert_contacts([("Meera", "+919000000001", "valid", {"caller_type": "customer"}),
                                              ("", "+919876543210", "valid", {})])
    assert (added, updated) == (1, 1)
    assert db.list_contacts(q="meera")["total"] == 1
    assert db.list_contacts(caller_type="employee")["total"] == 1
    assert db.contact_by_phone("+919000000001")["name"] == "Meera"
    assert db.delete_contacts([cid]) == 1
    assert db.list_contacts()["total"] == 1


def test_campaign_lifecycle_and_progress(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    cid = db.create_campaign("Round 1", "intake", PAST, created_by=1)
    n = db.add_campaign_contacts(cid, [{"phone": "+919000000001", "name": "A", "contact_id": None, "ticket_id": None},
                                       {"phone": "+919000000002", "name": "B", "contact_id": None, "ticket_id": "EPP-2026-000001"}])
    assert n == 2
    c = db.get_campaign_full(cid)
    assert c["campaign_type"] == "intake" and c["contact_count"] == 2 and c["status"] == "scheduled"
    assert c["progress"] == {"pending": 2}
    assert db.promote_due_campaigns(_now()) == 1
    assert [x["id"] for x in db.live_campaigns()] == [cid]
    due = db.cc_pending_due(cid, _now(), 10)
    assert len(due) == 2 and due[1]["ticket_id"] == "EPP-2026-000001"
    db.cc_update(due[0]["id"], call_status="calling", attempts=1)
    assert [x["id"] for x in db.cc_by_status(cid, "calling")] == [due[0]["id"]]
    assert db.cc_open_count(cid) == 2
    db.cc_update(due[0]["id"], call_status="done", outcome="no_concern")
    db.cc_update(due[1]["id"], call_status="failed")
    assert db.cc_open_count(cid) == 0
    assert db.campaign_progress(cid) == {"done": 1, "failed": 1}
    assert db.get_campaign_full(cid)["outcomes"] == {"no_concern": 1}
    assert db.list_campaign_contacts(cid, status="done")["total"] == 1
    assert db.list_campaign_contacts(cid, q="+919000000002")["items"][0]["name"] == "B"
    assert db.cancel_campaign(cid) is True
    assert db.cancel_campaign(cid) is False                              # already cancelled
    assert db.list_campaigns(status="cancelled")["total"] == 1


def test_cancel_campaign_cancels_pending_contacts(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    cid = db.create_campaign("R", "intake", PAST, created_by=1, status="live")
    db.add_campaign_contacts(cid, [{"phone": "+919000000001"}, {"phone": "+919000000002"}])
    a, _b = db.cc_pending_due(cid, _now(), 10)
    db.cc_update(a["id"], call_status="done", outcome="no_concern")
    assert db.cancel_campaign(cid) is True
    assert db.campaign_progress(cid) == {"done": 1, "cancelled": 1}


def test_cc_upcoming_lists_attempted_contacts_open_first(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    cid = db.create_campaign("R", "announcement", PAST, created_by=1, message="hi", status="live")
    db.add_campaign_contacts(cid, [{"phone": "+919000000001", "name": "A"}, {"phone": "+919000000002", "name": "B"}])
    a, b = db.cc_pending_due(cid, _now(), 10)
    db.cc_update(a["id"], attempts=1, call_status="done", outcome="acknowledged")
    db.cc_update(b["id"], attempts=1, call_status="pending", next_attempt_at="2030-01-01T00:00:00+00:00")
    rows = db.cc_upcoming()["items"]
    assert [r["id"] for r in rows] == [b["id"], a["id"]]
    assert rows[0]["campaign_name"] == "R" and rows[0]["campaign_type"] == "announcement"
