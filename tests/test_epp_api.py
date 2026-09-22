"""eo_api.py through the real app — auth + throttle, department scoping, edits + audit,
exports, and the routing config that the next call picks up."""

import pytest
from fastapi.testclient import TestClient

import eo_auth
import tickets

ADMIN_PASS = "secret-pass-1"


@pytest.fixture
def client(fresh_eo_db, monkeypatch):
    monkeypatch.setenv("EPP_ADMIN_USER", "admin")
    monkeypatch.setenv("EPP_ADMIN_PASS", ADMIN_PASS)
    monkeypatch.setenv("EPP_ANALYSIS_ENABLED", "false")
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


def test_login_throttles_after_repeated_failures(client, fresh_eo_db):
    for _ in range(5):
        assert client.post("/api/epp/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    r = client.post("/api/epp/login", json={"username": "admin", "password": ADMIN_PASS})
    assert r.status_code == 429
    actions = [a["action"] for a in fresh_eo_db.list_audit()["items"]]
    assert actions.count("login_failed") == 5


def test_me_and_password_change_are_audited(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    assert client.get("/api/epp/me", headers=h).json()["user"]["role"] == "admin"
    assert client.post("/api/epp/me/password", headers=h, json={"current": "nope", "new": "abcdefgh1"}).status_code == 400
    assert client.post("/api/epp/me/password", headers=h, json={"current": ADMIN_PASS, "new": "short"}).status_code == 400
    assert client.post("/api/epp/me/password", headers=h, json={"current": ADMIN_PASS, "new": "new-pass-123"}).status_code == 200
    assert "password_changed" in [a["action"] for a in fresh_eo_db.list_audit()["items"]]
    _login(client, "admin", "new-pass-123")


def test_department_user_sees_only_their_departments_tickets(client, fresh_eo_db):
    admin_h = _login(client, "admin", ADMIN_PASS)
    fin = tickets.create_from_tool(EMPLOYEE, {})["ticket_id"]                             # Finance
    hr = tickets.create_from_tool(dict(EMPLOYEE, category="Leave"), {})["ticket_id"]      # HR
    hr_h = _make_dept_user(client, admin_h, fresh_eo_db, "hr1", "HR")

    assert client.get("/api/epp/tickets", headers=admin_h).json()["total"] == 2
    r = client.get("/api/epp/tickets", headers=hr_h).json()
    assert r["total"] == 1 and r["items"][0]["ticket_id"] == hr
    assert client.get(f"/api/epp/tickets/{hr}", headers=hr_h).status_code == 200
    assert client.get(f"/api/epp/tickets/{fin}", headers=hr_h).status_code == 404
    assert client.get(f"/api/epp/tickets/{fin}/transcript", headers=hr_h).status_code == 404
    assert client.get(f"/api/epp/tickets/{fin}", headers=admin_h).status_code == 200
    # the summary is scoped too, and hides call/cost data from a department user
    s = client.get("/api/epp/summary", headers=hr_h).json()
    assert s["tickets"]["total"] == 1 and "calls" not in s
    s = client.get("/api/epp/summary", headers=admin_h).json()
    assert s["tickets"]["total"] == 2 and "calls" in s and "live_calls" in s


def test_department_user_needs_a_department(client):
    admin_h = _login(client, "admin", ADMIN_PASS)
    r = client.post("/api/epp/users", headers=admin_h,
                    json={"username": "x", "password": "dept-pass-123", "role": "dept_user"})
    assert r.status_code == 400
    r = client.post("/api/epp/users", headers=admin_h,
                    json={"username": "x", "password": "short", "role": "admin"})
    assert r.status_code == 400


def test_status_assignment_notes_and_audit(client, fresh_eo_db):
    admin_h = _login(client, "admin", ADMIN_PASS)
    tid = tickets.create_from_tool(dict(EMPLOYEE, category="Leave"), {})["ticket_id"]
    hr_h = _make_dept_user(client, admin_h, fresh_eo_db, "hr1", "HR")

    r = client.post(f"/api/epp/tickets/{tid}/status", headers=hr_h, json={"status": "under_review", "note": "on it"})
    assert r.status_code == 200 and r.json()["status"] == "under_review"
    r = client.post(f"/api/epp/tickets/{tid}/status", headers=hr_h, json={"status": "bogus"})
    assert r.status_code == 400
    r = client.post(f"/api/epp/tickets/{tid}/note", headers=hr_h, json={"note": "Spoke to Rahul."})
    assert r.status_code == 200
    # a department user may pick a person, but not move the ticket to another department
    fin = _dept_id(fresh_eo_db, "Finance")
    assert client.post(f"/api/epp/tickets/{tid}/assign", headers=hr_h, json={"department_id": fin}).status_code == 403
    r = client.post(f"/api/epp/tickets/{tid}/assign", headers=admin_h, json={"department_id": fin})
    assert r.status_code == 200 and r.json()["assigned_department"] == "Finance"
    assert client.get(f"/api/epp/tickets/{tid}", headers=hr_h).status_code == 404   # moved away
    # reopen is admin-only
    client.post(f"/api/epp/tickets/{tid}/status", headers=admin_h, json={"status": "resolved"})
    r = client.post(f"/api/epp/tickets/{tid}/status", headers=admin_h, json={"status": "open"})
    assert r.status_code == 200
    detail = client.get(f"/api/epp/tickets/{tid}", headers=admin_h).json()
    assert [e["kind"] for e in detail["events"]][:3] == ["created", "assigned", "status"]
    actions = [a["action"] for a in fresh_eo_db.list_audit()["items"]]
    for a in ("ticket_status", "ticket_note", "ticket_assigned", "user_created", "login"):
        assert a in actions, a


def test_transcript_and_recording_views_are_audited(client, fresh_eo_db):
    admin_h = _login(client, "admin", ADMIN_PASS)
    tid = tickets.create_from_tool(EMPLOYEE, {"call_id": "nocall", "call_sid": "nosid"})["ticket_id"]
    r = client.get(f"/api/epp/tickets/{tid}/transcript", headers=admin_h)
    assert r.status_code == 200 and r.json()["messages"] == []
    assert client.get(f"/api/epp/tickets/{tid}/audio", headers=admin_h).status_code == 404
    rows = [a for a in fresh_eo_db.list_audit()["items"] if a["action"] == "transcript_viewed"]
    assert rows and rows[0]["target"] == f"ticket:{tid}"


def test_csv_export_and_filters(client):
    admin_h = _login(client, "admin", ADMIN_PASS)
    tickets.create_from_tool(EMPLOYEE, {})
    tickets.create_from_tool(dict(EMPLOYEE, category="Harassment", high_priority_reason="harassment"), {})
    r = client.get("/api/epp/tickets.csv", headers=admin_h)
    assert r.status_code == 200 and r.text.startswith("ticket_id,created_at,status,priority")
    assert r.text.count("\n") == 3
    assert client.get("/api/epp/tickets?priority=high", headers=admin_h).json()["total"] == 1
    assert client.get("/api/epp/tickets?escalated=1", headers=admin_h).json()["total"] == 1
    assert client.get("/api/epp/tickets?q=rahul", headers=admin_h).json()["total"] == 2
    assert client.get("/api/epp/tickets?caller_type=vendor", headers=admin_h).json()["total"] == 0


def test_routing_config_changes_where_the_next_ticket_goes(client, fresh_eo_db):
    admin_h = _login(client, "admin", ADMIN_PASS)
    cats = client.get("/api/epp/categories", headers=admin_h).json()
    salary = next(c for c in cats["items"] if c["caller_type"] == "employee" and c["name"] == "Salary")
    assert salary["department_name"] == "Finance"
    hr_id = _dept_id(fresh_eo_db, "HR")
    r = client.patch(f"/api/epp/categories/{salary['id']}", headers=admin_h,
                     json={"department_id": hr_id, "keywords": "salary, wages, pagar"})
    assert r.status_code == 200 and r.json()["department_name"] == "HR"
    t = fresh_eo_db.get_ticket(tickets.create_from_tool(EMPLOYEE, {})["ticket_id"])
    assert t["assigned_department"] == "HR"
    # 'Other' is protected; a department user may not touch config at all
    other = next(c for c in cats["items"] if c["caller_type"] == "employee" and c["name"] == "Other")
    assert client.delete(f"/api/epp/categories/{other['id']}", headers=admin_h).status_code == 400
    hr_h = _make_dept_user(client, admin_h, fresh_eo_db, "hr1", "HR")
    assert client.patch(f"/api/epp/categories/{salary['id']}", headers=hr_h, json={"active": False}).status_code == 403
    assert client.get("/api/epp/audit", headers=hr_h).status_code == 403
    assert client.get("/api/epp/users", headers=hr_h).status_code == 403
    # departments: create + duplicate code
    r = client.post("/api/epp/departments", headers=admin_h, json={"name": "Legal", "code": "legal"})
    assert r.status_code == 201 and r.json()["code"] == "LEGAL"
    assert client.post("/api/epp/departments", headers=admin_h, json={"name": "Legal 2", "code": "LEGAL"}).status_code == 409


def test_agent_editor_rejects_unknown_placeholders_and_dead_tools(client, fresh_eo_db):
    admin_h = _login(client, "admin", ADMIN_PASS)
    agent = client.get("/api/epp/agents", headers=admin_h).json()["items"][0]
    r = client.patch(f"/api/epp/agents/{agent['id']}", headers=admin_h, json={"prompt_template": "Hi {helpline_nam}"})
    assert r.status_code == 400 and "helpline_nam" in r.json()["detail"]
    r = client.patch(f"/api/epp/agents/{agent['id']}", headers=admin_h, json={"prompt_template": "call record_outcome"})
    assert r.status_code == 400
    r = client.patch(f"/api/epp/agents/{agent['id']}", headers=admin_h, json={"prompt_template": "Hi {helpline_name}"})
    assert r.status_code == 200 and r.json()["prompt_template"] == "Hi {helpline_name}"
    p = client.post(f"/api/epp/agents/{agent['id']}/preview", headers=admin_h, json={}).json()
    assert p["system_instruction"].startswith("Hi EPP Composites") and p["missing"] == []
    assert [t["name"] for t in p["tools"]] == ["create_ticket", "lookup_ticket", "update_ticket", "end_call"]
    r = client.post(f"/api/epp/agents/{agent['id']}/reset", headers=admin_h)
    assert r.status_code == 200 and "Thank you for calling {helpline_name}" in r.json()["prompt_template"]
    tok = client.post(f"/api/epp/agents/{agent['id']}/test-token", headers=admin_h).json()["token"]
    assert eo_auth.verify_test_token(tok)["agent_id"] == agent["id"]
    assert eo_auth.verify_token(tok) is None                       # a test token is not a session


def test_manual_ticket_creation(client):
    admin_h = _login(client, "admin", ADMIN_PASS)
    r = client.post("/api/epp/tickets", headers=admin_h,
                    json={"caller_type": "vendor", "caller_name": "Jay", "vendor_code": "V1",
                          "description": "Invoice 12 unpaid.", "category": "Invoice Query"})
    assert r.status_code == 201
    t = r.json()
    assert t["source"] == "manual" and t["assigned_department"] == "Finance"
    assert client.post("/api/epp/tickets", headers=admin_h, json={"caller_type": "vendor"}).status_code == 400


def test_unauthenticated_and_live_token(client):
    assert client.get("/api/epp/tickets").status_code == 401
    admin_h = _login(client, "admin", ADMIN_PASS)
    tok = client.post("/api/epp/live/token", headers=admin_h).json()["token"]
    assert eo_auth.verify_live_token(tok)["role"] == "admin"
    assert client.get("/healthz").json()["ok"] is True
