"""Campaign API — contacts, campaigns, scheduler; admin-only; audited."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import campaign_runner
import eo_auth

ADMIN_PASS = "secret-pass-1"
FUTURE = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


@pytest.fixture
def client(fresh_eo_db, monkeypatch):
    monkeypatch.setenv("EPP_ADMIN_USER", "admin")
    monkeypatch.setenv("EPP_ADMIN_PASS", ADMIN_PASS)
    monkeypatch.setenv("EPP_ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("EPP_CAMPAIGN_RUNNER_ENABLED", "false")   # no background dialing in tests
    monkeypatch.delenv("EPP_MAX_ACTIVE_CAMPAIGNS", raising=False)
    eo_auth.reset_throttle()
    campaign_runner.set_override(None)
    import main
    main.invalidate_ctx_cache()
    with TestClient(main.app) as c:
        yield c
    eo_auth.reset_throttle()
    campaign_runner.set_override(None)


def _login(c, username, password):
    r = c.post("/api/epp/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _dept_id(db, name):
    return next(d for d in db.list_departments() if d["name"] == name)["id"]


def _make_dept_user(c, admin_h, db, username, dept_name, password="dept-pass-123"):
    r = c.post("/api/epp/users", headers=admin_h,
               json={"username": username, "name": username.upper(), "password": password,
                     "role": "dept_user", "department_id": _dept_id(db, dept_name)})
    assert r.status_code == 200, r.text
    return _login(c, username, password)


def test_contacts_crud_and_import(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    r = client.post("/api/epp/contacts", headers=h, json={"name": "Rahul", "phone": "9876543210", "caller_type": "employee"})
    assert r.status_code == 200 and r.json()["phone"] == "+919876543210" and r.json()["created"] is True
    assert client.post("/api/epp/contacts", headers=h, json={"phone": "12"}).status_code == 400
    csv = b"Name,Phone,Type\nMeera,9000000001,customer\nbad,9.1E+11,\n"
    r = client.post("/api/epp/contacts/import", headers=h, files={"file": ("c.csv", csv, "text/csv")})
    assert r.status_code == 200
    assert r.json()["added"] == 1 and r.json()["rejected"] == 1
    assert client.get("/api/epp/contacts?q=meera", headers=h).json()["total"] == 1
    assert client.get("/api/epp/contacts?caller_type=employee", headers=h).json()["total"] == 1
    t = client.get("/api/epp/contacts/template", headers=h)
    assert t.status_code == 200 and "spreadsheet" in t.headers["content-type"]
    ids = [c["id"] for c in client.get("/api/epp/contacts", headers=h).json()["items"]]
    assert client.post("/api/epp/contacts/delete", headers=h, json={"ids": ids}).json()["deleted"] == 2
    acts = [a["action"] for a in fresh_eo_db.list_audit()["items"]]
    for a in ("contact_added", "contacts_imported", "contacts_deleted"):
        assert a in acts, a


def test_campaign_create_detail_cancel_and_audit(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    cid = client.post("/api/epp/contacts", headers=h, json={"name": "A", "phone": "9000000001"}).json()["id"]
    body = {"name": "Notice", "campaign_type": "announcement", "message": "Plant closed Monday.",
            "contact_ids": [cid], "start_at": FUTURE}
    r = client.post("/api/epp/campaigns", headers=h, json=body)
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["contact_count"] == 1 and c["status"] == "scheduled" and c["campaign_type"] == "announcement"
    assert client.post("/api/epp/campaigns", headers=h, json={**body, "message": ""}).status_code == 400
    d = client.get(f"/api/epp/campaigns/{c['id']}", headers=h).json()
    assert d["progress"] == {"pending": 1} and d["tickets_created"] == 0
    recips = client.get(f"/api/epp/campaigns/{c['id']}/contacts", headers=h).json()["items"]
    assert recips[0]["display_status"] == "Queued" and recips[0]["display_variant"] == "amber"
    lst = client.get("/api/epp/campaigns?status=scheduled", headers=h).json()
    assert lst["total"] == 1 and lst["items"][0]["progress"] == {"pending": 1}
    assert client.get("/api/epp/campaigns/999", headers=h).status_code == 404
    assert client.post(f"/api/epp/campaigns/{c['id']}/cancel", headers=h).status_code == 200
    assert client.post(f"/api/epp/campaigns/{c['id']}/cancel", headers=h).status_code == 400
    acts = [a["action"] for a in fresh_eo_db.list_audit()["items"]]
    assert "campaign_created" in acts and "campaign_cancelled" in acts


def test_recipient_actions(client, fresh_eo_db, monkeypatch):
    h = _login(client, "admin", ADMIN_PASS)
    cid = client.post("/api/epp/contacts", headers=h, json={"name": "A", "phone": "9000000001"}).json()["id"]
    c = client.post("/api/epp/campaigns", headers=h, json={"name": "R", "campaign_type": "intake",
                                                           "contact_ids": [cid], "start_at": FUTURE}).json()
    cc = client.get(f"/api/epp/campaigns/{c['id']}/contacts", headers=h).json()["items"][0]
    r = client.patch(f"/api/epp/campaigns/{c['id']}/contacts/{cc['id']}/remark", headers=h, json={"remark": "VIP"})
    assert r.status_code == 200 and fresh_eo_db.get_campaign_contact(cc["id"])["remark"] == "VIP"
    # cancel the pending retry, then call-now revives it (Plivo stubbed)
    assert client.post(f"/api/epp/campaigns/{c['id']}/contacts/{cc['id']}/cancel", headers=h).status_code == 200
    assert fresh_eo_db.get_campaign_contact(cc["id"])["call_status"] == "cancelled"
    assert client.post(f"/api/epp/campaigns/{c['id']}/contacts/{cc['id']}/cancel", headers=h).status_code == 400
    dialled = []

    async def fake(phone, **kw):
        dialled.append(phone)
        return {"success": True, "call_uuid": "u"}
    monkeypatch.setattr(campaign_runner.dialer, "place_call", fake)
    for k in ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"):
        monkeypatch.setenv(k, "t")
    monkeypatch.setenv("PLIVO_FROM_NUMBER", "+10000000000")
    assert client.post(f"/api/epp/campaigns/{c['id']}/contacts/{cc['id']}/retry", headers=h).status_code == 200
    assert dialled == ["+919000000001"]
    assert client.post(f"/api/epp/campaigns/{c['id']}/contacts/{cc['id']}/retry", headers=h).status_code == 400  # calling
    acts = [a["action"] for a in fresh_eo_db.list_audit()["items"]]
    assert "campaign_call_now" in acts and "campaign_contact_cancelled" in acts


def test_campaign_routes_are_admin_only(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    dept = _make_dept_user(client, h, fresh_eo_db, "hr1", "HR")
    for path in ("/api/epp/contacts", "/api/epp/campaigns", "/api/epp/scheduler/queue",
                 "/api/epp/campaign-types", "/api/epp/contacts/template"):
        assert client.get(path, headers=dept).status_code == 403, path
    assert client.post("/api/epp/campaigns", headers=dept, json={}).status_code == 403
    assert client.get("/api/epp/campaigns").status_code == 401


def test_scheduler_toggle_and_queue(client):
    h = _login(client, "admin", ADMIN_PASS)
    r = client.post("/api/epp/scheduler/toggle", headers=h, json={"enabled": False})
    assert r.json()["enabled"] is False and campaign_runner.is_enabled() is False
    q = client.get("/api/epp/scheduler/queue", headers=h).json()
    assert q["scheduler_enabled"] is False and q["items"] == [] and q["active_campaigns"] == 0
    r = client.post("/api/epp/scheduler/toggle", headers=h, json={})       # no body → flip
    assert r.json()["enabled"] is True


def test_campaign_types_lists_outcomes(client):
    h = _login(client, "admin", ADMIN_PASS)
    items = client.get("/api/epp/campaign-types", headers=h).json()["items"]
    assert [i["type"] for i in items] == ["intake", "followup", "announcement"]
    assert items[1]["outcomes"][0]["value"] == "confirmed" and items[0]["label"] == "Intake round"


def test_calls_can_be_filtered_by_campaign(client):
    h = _login(client, "admin", ADMIN_PASS)
    assert client.get("/api/epp/calls?campaign_id=1", headers=h).json()["total"] == 0


def test_recipient_call_endpoint_returns_the_call_record(client, fresh_eo_db):
    import asyncio
    import store
    h = _login(client, "admin", ADMIN_PASS)
    cid = client.post("/api/epp/contacts", headers=h, json={"name": "A", "phone": "9000000001"}).json()["id"]
    c = client.post("/api/epp/campaigns", headers=h, json={"name": "R", "campaign_type": "intake",
                                                           "contact_ids": [cid], "start_at": FUTURE}).json()
    cc = client.get(f"/api/epp/campaigns/{c['id']}/contacts", headers=h).json()["items"][0]
    assert client.get(f"/api/epp/campaigns/{c['id']}/contacts/{cc['id']}/call", headers=h).status_code == 404
    asyncio.run(store.save_call({"id": "rec1", "call_sid": "s1", "source": "plivo_campaign", "caller": "+919000000001",
                                 "started_at": "2026-09-22T05:00:00+00:00", "status": "completed",
                                 "campaign_id": c["id"], "campaign_contact_id": cc["id"],
                                 "transcript": [{"role": "user", "text": "hi", "ts": "x"}], "tool_calls": []}))
    r = client.get(f"/api/epp/campaigns/{c['id']}/contacts/{cc['id']}/call", headers=h)
    assert r.status_code == 200 and r.json()["id"] == "rec1" and r.json()["messages"][0]["text"] == "hi"
    assert client.get(f"/api/epp/campaigns/999/contacts/{cc['id']}/call", headers=h).status_code == 404
