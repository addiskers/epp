"""The Super admin page: the service provider picks the client's tabs, and clears the test
data before go-live (backed up first, refused while a call is live)."""
import os

import pytest
from fastapi.testclient import TestClient

import eo_auth
import store

ADMIN_PASS = "secret-pass-1"
EMPLOYEE = {"caller_type": "employee", "language": "en", "caller_name": "Rahul", "employee_id": "E1",
            "description": "Salary not paid for two months.", "category": "Salary", "high_priority_reason": "none"}


@pytest.fixture
def client(fresh_eo_db, monkeypatch, tmp_path):
    monkeypatch.setenv("EPP_ADMIN_USER", "admin")
    monkeypatch.setenv("EPP_ADMIN_PASS", ADMIN_PASS)
    monkeypatch.setenv("EPP_ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("EPP_SUPERADMIN_USERS", "admin")
    monkeypatch.delenv("EPP_HIDDEN_PAGES", raising=False)
    monkeypatch.setattr(store, "CALLS_DIR", str(tmp_path / "calls"))
    monkeypatch.setattr(store, "RECORDINGS_DIR", str(tmp_path / "recordings"))
    eo_auth.reset_throttle()
    import main
    main.invalidate_ctx_cache()
    with TestClient(main.app) as c:
        yield c
    with store._LOCK:
        store._INDEX.clear()
    eo_auth.reset_throttle()


def _login(c, username, password):
    r = c.post("/api/epp/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _client_admin(c, super_h):
    r = c.post("/api/epp/users", headers=super_h,
               json={"username": "client", "name": "Client Admin", "password": "client-pass-1", "role": "admin"})
    assert r.status_code == 200, r.text
    return _login(c, "client", "client-pass-1")


def test_only_the_service_provider_gets_in(client):
    super_h = _login(client, "admin", ADMIN_PASS)
    client_h = _client_admin(client, super_h)
    for method, path, body in (("get", "/api/epp/superadmin", None),
                               ("put", "/api/epp/superadmin/pages", {"hidden_pages": []}),
                               ("post", "/api/epp/superadmin/reset-data", {"confirm": "DELETE", "parts": ["audit"]})):
        r = getattr(client, method)(path, headers=client_h, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (path, r.status_code)
    assert client.get("/api/epp/superadmin", headers=super_h).status_code == 200
    assert client.get("/api/epp/me", headers=client_h).json()["ui"]["superadmin"] is False
    assert client.get("/api/epp/me", headers=super_h).json()["ui"]["superadmin"] is True


def test_the_super_admin_picks_the_clients_tabs(client, fresh_eo_db):
    super_h = _login(client, "admin", ADMIN_PASS)
    client_h = _client_admin(client, super_h)
    # default: the .env list (unset → the three outbound pages)
    assert client.get("/api/epp/me", headers=client_h).json()["ui"]["hidden_pages"] == ["campaigns", "contacts", "scheduler"]
    r = client.put("/api/epp/superadmin/pages", headers=super_h,
                   json={"hidden_pages": ["Audit", "agents", "bogus", "campaigns"]})
    assert r.status_code == 200 and r.json()["source"] == "db"
    assert r.json()["client_hidden_pages"] == ["campaigns", "agents", "audit"]
    ui = client.get("/api/epp/me", headers=client_h).json()["ui"]
    assert ui["hidden_pages"] == ["campaigns", "agents", "audit"]
    me = client.get("/api/epp/me", headers=super_h).json()["ui"]
    assert me["hidden_pages"] == [] and me["client_hidden_pages"] == ["campaigns", "agents", "audit"]
    # an explicit empty list shows every tab; reset goes back to .env
    client.put("/api/epp/superadmin/pages", headers=super_h, json={"hidden_pages": []})
    assert client.get("/api/epp/me", headers=client_h).json()["ui"]["hidden_pages"] == []
    assert client.put("/api/epp/superadmin/pages", headers=super_h, json={"hidden_pages": "audit"}).status_code == 400
    r = client.put("/api/epp/superadmin/pages", headers=super_h, json={"reset": True})
    assert r.json()["source"] == "env"
    assert client.get("/api/epp/me", headers=client_h).json()["ui"]["hidden_pages"] == ["campaigns", "contacts", "scheduler"]
    actions = [a["action"] for a in fresh_eo_db.list_audit()["items"]]
    assert "client_pages_updated" in actions and "client_pages_reset" in actions


def _seed_test_data(client, h, db):
    t1 = client.post("/api/epp/tickets", headers=h, json=EMPLOYEE).json()["ticket_id"]
    client.post("/api/epp/tickets", headers=h, json=EMPLOYEE)
    store._save_sync({"id": "call-t1", "call_sid": "sid-t1", "source": "plivo_inbound", "caller": "+919904240078",
                      "started_at": "2026-09-22T10:00:00+00:00", "duration_seconds": 90, "status": "completed",
                      "ticket_id": t1, "transcript": [], "tool_calls": []})
    with open(store.recording_path("sid-t1"), "wb") as f:
        f.write(b"RIFF")
    db._exec("INSERT INTO contacts (name, phone, created_at, updated_at) VALUES ('A', '+919000000001', 'x', 'x')")
    db._exec("INSERT INTO campaigns (name, campaign_type, start_at, created_at, updated_at) "
             "VALUES ('C', 'intake', 'x', 'x', 'x')")
    return t1


def test_reset_is_refused_without_the_word_or_during_a_call(client, fresh_eo_db):
    import main
    h = _login(client, "admin", ADMIN_PASS)
    _seed_test_data(client, h, fresh_eo_db)
    r = client.post("/api/epp/superadmin/reset-data", headers=h, json={"confirm": "delete", "parts": ["tickets"]})
    assert r.status_code == 400 and "DELETE" in r.json()["detail"]
    r = client.post("/api/epp/superadmin/reset-data", headers=h, json={"confirm": "DELETE", "parts": []})
    assert r.status_code == 400
    main._active_calls["sid-live"] = {"caller": "+91", "started_at": 0, "call_id": "x", "source": "plivo_inbound"}
    try:
        r = client.post("/api/epp/superadmin/reset-data", headers=h, json={"confirm": "DELETE", "parts": ["tickets"]})
        assert r.status_code == 409
    finally:
        main._active_calls.pop("sid-live", None)
    assert fresh_eo_db.data_counts()["tickets"] == 2


def test_full_reset_backs_up_then_starts_clean(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    _seed_test_data(client, h, fresh_eo_db)
    before = client.get("/api/epp/superadmin", headers=h).json()["counts"]
    assert before == {"tickets": 2, "calls": 1, "recordings": 1, "audit": before["audit"], "contacts": 1, "campaigns": 1}
    assert before["audit"] > 0
    r = client.post("/api/epp/superadmin/reset-data", headers=h,
                    json={"confirm": "DELETE", "parts": ["tickets", "calls", "audit", "outbound"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["after"] == {"tickets": 0, "calls": 0, "recordings": 0, "audit": 1, "contacts": 0, "campaigns": 0}
    # the backup holds everything that was removed
    backup = d["backup"]
    assert os.path.isfile(os.path.join(backup, "epp.db"))
    assert os.path.isfile(os.path.join(backup, "calls", "call-t1.json"))
    assert os.path.isfile(os.path.join(backup, "recordings", "sid-t1.wav"))
    import sqlite3
    with sqlite3.connect(os.path.join(backup, "epp.db")) as b:
        assert b.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 2
    # the only audit row is the reset itself, naming who did it
    rows = fresh_eo_db.list_audit()["items"]
    assert [a["action"] for a in rows] == ["data_reset"] and rows[0]["username"] == "admin"
    # users, departments, categories and the agent survive; numbering restarts
    assert client.get("/api/epp/me", headers=h).status_code == 200
    assert fresh_eo_db.list_departments() and fresh_eo_db.list_categories() and fresh_eo_db.intake_agent()
    new = client.post("/api/epp/tickets", headers=h, json=EMPLOYEE).json()["ticket_id"]
    assert new.endswith("-000001")
    assert client.get("/api/epp/calls", headers=h).json()["total"] == 0


def test_partial_reset_touches_only_what_was_ticked(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    _seed_test_data(client, h, fresh_eo_db)
    r = client.post("/api/epp/superadmin/reset-data", headers=h, json={"confirm": "DELETE", "parts": ["audit"]})
    assert r.status_code == 200
    after = r.json()["after"]
    assert after["tickets"] == 2 and after["calls"] == 1 and after["contacts"] == 1 and after["audit"] == 1
    assert os.path.isfile(os.path.join(store.CALLS_DIR, "call-t1.json"))
    assert os.path.isfile(os.path.join(r.json()["backup"], "epp.db"))        # the database is always backed up
