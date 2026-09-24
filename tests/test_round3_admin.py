"""Client review round 3 — the admin pages: one Save on the ticket page (all-or-nothing),
users edited through a form that is checked before it is written, hidden pages from the
server flag, names on the call log and no model cost anywhere the client looks."""
import pytest
from fastapi.testclient import TestClient

import eo_auth
import store

ADMIN_PASS = "secret-pass-1"


@pytest.fixture
def client(fresh_eo_db, monkeypatch):
    monkeypatch.setenv("EPP_ADMIN_USER", "admin")
    monkeypatch.setenv("EPP_ADMIN_PASS", ADMIN_PASS)
    monkeypatch.setenv("EPP_ANALYSIS_ENABLED", "false")
    monkeypatch.delenv("EPP_HIDDEN_PAGES", raising=False)
    eo_auth.reset_throttle()
    import main
    main.invalidate_ctx_cache()
    with TestClient(main.app) as c:
        yield c
    eo_auth.reset_throttle()


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


EMPLOYEE = {"caller_type": "employee", "language": "en", "caller_name": "Rahul", "employee_id": "E1",
            "description": "Salary not paid for two months.", "category": "Salary", "high_priority_reason": "none"}


def _ticket(client, h, body=EMPLOYEE):
    r = client.post("/api/epp/tickets", headers=h, json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ hidden pages
def test_ui_config_comes_from_the_env(client, monkeypatch):
    h = _login(client, "admin", ADMIN_PASS)
    assert client.get("/api/epp/me", headers=h).json()["ui"]["hidden_pages"] == ["campaigns", "contacts", "scheduler"]
    monkeypatch.setenv("EPP_HIDDEN_PAGES", " Agents, audit ,bogus")
    assert client.get("/api/epp/me", headers=h).json()["ui"]["hidden_pages"] == ["agents", "audit"]
    monkeypatch.setenv("EPP_HIDDEN_PAGES", "")
    assert client.get("/api/epp/me", headers=h).json()["ui"]["hidden_pages"] == []
    r = client.post("/api/epp/login", json={"username": "admin", "password": ADMIN_PASS})
    assert r.json()["ui"] == {"hidden_pages": [], "client_hidden_pages": [], "superadmin": False}


# ------------------------------------------------------------------ one Save on the ticket page
def test_one_save_applies_every_field(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    tid = _ticket(client, h)["ticket_id"]
    r = client.post(f"/api/epp/tickets/{tid}/update", headers=h,
                    json={"category": "PF", "priority": "high", "status": "under_review", "note": "checking with HR"})
    assert r.status_code == 200, r.text
    t = client.get(f"/api/epp/tickets/{tid}", headers=h).json()
    assert t["category"] == "PF" and t["assigned_department"] == "HR"
    assert t["priority"] == "high" and t["status"] == "under_review"
    kinds = [e["kind"] for e in t["events"]]
    for k in ("reclassified", "priority", "status"):
        assert k in kinds, kinds
    assert [e["kind"] for e in t["events"] if e["note"] == "checking with HR"] == ["status"]
    assert "ticket_updated" in [a["action"] for a in fresh_eo_db.list_audit()["items"]]


def test_a_bad_field_means_nothing_is_saved(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    tid = _ticket(client, h)["ticket_id"]
    r = client.post(f"/api/epp/tickets/{tid}/update", headers=h,
                    json={"category": "PF", "priority": "high", "status": "bogus"})
    assert r.status_code == 400 and "status" in r.json()["detail"].lower()
    t = client.get(f"/api/epp/tickets/{tid}", headers=h).json()
    assert t["category"] == "Salary" and t["priority"] == "medium" and t["status"] == "open"
    assert [e["kind"] for e in t["events"]] == ["created", "assigned", "note"]
    # closed → escalated is not a move the machine allows
    assert client.post(f"/api/epp/tickets/{tid}/update", headers=h, json={"status": "closed"}).status_code == 200
    r = client.post(f"/api/epp/tickets/{tid}/update", headers=h, json={"status": "escalated", "note": "x"})
    assert r.status_code == 400
    assert client.post(f"/api/epp/tickets/{tid}/update", headers=h, json={}).status_code == 400


def test_department_user_can_note_and_assign_but_not_reclassify(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    tid = _ticket(client, h)["ticket_id"]                    # Salary → Finance
    dh = _make_dept_user(client, h, fresh_eo_db, "fin1", "Finance")
    r = client.post(f"/api/epp/tickets/{tid}/update", headers=dh, json={"category": "PF"})
    assert r.status_code == 400 and "admin" in r.json()["detail"].lower()
    r = client.post(f"/api/epp/tickets/{tid}/update", headers=dh, json={"department_id": _dept_id(fresh_eo_db, "HR")})
    assert r.status_code == 400
    r = client.post(f"/api/epp/tickets/{tid}/update", headers=dh, json={"note": "called the employee"})
    assert r.status_code == 200, r.text
    events = client.get(f"/api/epp/tickets/{tid}", headers=dh).json()["events"]
    assert any(e["kind"] == "note" and e["note"] == "called the employee" for e in events)
    # same-as-current values are simply ignored, not an error
    r = client.post(f"/api/epp/tickets/{tid}/update", headers=dh, json={"status": "open", "priority": "medium", "note": "again"})
    assert r.status_code == 200


# ------------------------------------------------------------------ users
def test_user_edit_is_checked_before_it_is_written(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    _make_dept_user(client, h, fresh_eo_db, "fin1", "Finance")
    users = client.get("/api/epp/users", headers=h).json()["items"]
    uid = next(u["id"] for u in users if u["username"] == "fin1")
    r = client.patch(f"/api/epp/users/{uid}", headers=h, json={"name": "Finance One"})
    assert r.status_code == 200 and r.json()["user"]["name"] == "Finance One"
    # a department user must keep a department
    assert client.patch(f"/api/epp/users/{uid}", headers=h, json={"department_id": None}).status_code == 400
    # a bad department in the same body as a disable → nothing is written
    r = client.patch(f"/api/epp/users/{uid}", headers=h, json={"active": False, "department_id": 999999})
    assert r.status_code == 400
    u = next(u for u in client.get("/api/epp/users", headers=h).json()["items"] if u["id"] == uid)
    assert u["active"] and u["name"] == "Finance One"
    # promoting to admin drops the department requirement
    assert client.patch(f"/api/epp/users/{uid}", headers=h, json={"role": "admin", "department_id": None}).status_code == 200
    # the audit row names what changed, never the password itself
    row = [a for a in fresh_eo_db.list_audit()["items"] if a["action"] == "user_updated"][-1]
    assert "Finance One" in row["detail"]
    r = client.patch(f"/api/epp/users/{uid}", headers=h, json={"password": "new-pass-123"})
    assert r.status_code == 200
    assert "new-pass-123" not in fresh_eo_db.list_audit()["items"][0]["detail"]
    assert _login(client, "fin1", "new-pass-123")


# ------------------------------------------------------------------ call logs
def test_call_logs_carry_the_name_and_hide_the_cost(client):
    h = _login(client, "admin", ADMIN_PASS)
    store._save_sync({"id": "c-name-1", "call_sid": "sid-n1", "source": "plivo_inbound", "caller": "+919904240078",
                      "caller_name": "Shreya Patel", "started_at": "2026-09-22T10:00:00+00:00", "duration_seconds": 90,
                      "status": "completed", "ticket_id": "EPP-2026-000001", "gemini_cost_usd": 0.12, "tokens": {},
                      "gemini_model": "m", "transcript": [], "tool_calls": []})
    items = client.get("/api/epp/calls?q=shreya", headers=h).json()["items"]
    assert [c["id"] for c in items] == ["c-name-1"]
    assert items[0]["caller_name"] == "Shreya Patel"
    assert not ({"gemini_cost_usd", "tokens", "gemini_model"} & set(items[0]))
    d = client.get("/api/epp/calls/c-name-1", headers=h).json()
    assert "gemini_cost_usd" not in d and d["caller_name"] == "Shreya Patel"
    s = client.get("/api/epp/summary", headers=h).json()["calls"]
    assert "total_cost_usd" not in s and "avg_cost_per_call" not in s
    assert all("cost_usd" not in day for day in s["by_day"])


def test_recorder_keeps_the_name_the_agent_heard():
    from recorder import CallRecorder
    r = CallRecorder(model="m")
    r.call = {"tool_calls": [], "ticket_ids": [], "caller_name": ""}
    r._record_tool({"name": "create_ticket", "args": {"caller_name": "Shyam", "language": "gu"},
                    "result": {"ok": True, "ticket_id": "EPP-2026-000001"}})
    assert r.call["caller_name"] == "Shyam"
    r._record_tool({"name": "update_ticket", "args": {"ticket_id": "EPP-2026-000001", "caller_name": "Shreya"},
                    "result": {"ok": True}})
    assert r.call["caller_name"] == "Shreya"
    r._record_tool({"name": "create_ticket", "args": {"caller_name": "Not given"},
                    "result": {"ok": True, "ticket_id": "EPP-2026-000002"}})
    assert r.call["caller_name"] == "Shreya"


def test_backfill_fills_names_from_tickets_once(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CALLS_DIR", str(tmp_path / "calls"))
    store._init_sync()
    store._save_sync({"id": "old1", "ticket_id": "EPP-2026-000005", "transcript": [], "tool_calls": []})
    store._save_sync({"id": "old2", "ticket_id": None, "transcript": [], "tool_calls": []})
    asked = []

    def name_for(cid):
        asked.append(cid)
        return "Meera" if cid == "old1" else ""

    try:
        assert store._backfill_names_sync(name_for) == 2
        assert store._INDEX["old1"]["caller_name"] == "Meera" and store._INDEX["old2"]["caller_name"] == ""
        assert asked == ["old1"]                           # no ticket → no lookup
        assert store._backfill_names_sync(name_for) == 0    # nothing left to do on the next boot
        assert store._matches(store._INDEX["old1"], {"q": "meera"})
    finally:
        store._INDEX.clear()
