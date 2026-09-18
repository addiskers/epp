"""tickets.py — numbering, spoken forms, the two tool handlers, the status machine,
and the post-call refinement."""

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import analysis
import store
import tickets

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 20, 8, 0, tzinfo=IST)


@pytest.fixture
def db(fresh_eo_db, monkeypatch):
    monkeypatch.delenv("EPP_TICKET_PREFIX", raising=False)
    fresh_eo_db.init()
    return fresh_eo_db


def _dept(db, name):
    return next(d for d in db.list_departments() if d["name"] == name)


EMPLOYEE = {
    "caller_type": "employee", "language": "hi", "caller_name": "Rahul Verma",
    "contact_number": "", "employee_id": "E1042", "caller_department": "Moulding",
    "plant_location": "Halol", "description": "Rahul has not received his salary for August and September.",
    "category": "Salary", "subcategory": "two months pending", "high_priority_reason": "none",
}
CALL = {"call_id": "call1", "call_sid": "sid1", "caller": "+919876543210"}


# --------------------------------------------------------------------------- spoken forms
def test_spoken_ticket_id_is_letter_by_letter_digit_by_digit():
    assert tickets.spoken_ticket_id("EPP-2026-000001") == \
        "E, P, P — two zero two six — zero zero zero zero zero one"


@pytest.mark.parametrize("raw,expected", [
    ("EPP-2026-000123", "EPP-2026-000123"),
    ("epp 2026 123", "EPP-2026-000123"),
    ("EPP2026000123", "EPP-2026-000123"),
    ("2026-123", "EPP-2026-000123"),
    ("2026000123", "EPP-2026-000123"),
    ("000123", "EPP-2026-000123"),
    ("123", "EPP-2026-000123"),
    ("one two three", "EPP-2026-000123"),
    ("E P P two zero two five zero zero zero zero one two", "EPP-2025-000012"),
    ("EPP-2025-000009", "EPP-2025-000009"),
    ("", None), ("no digits here", None), ("0", None), ("9999999", None),
])
def test_normalise_ticket_number(raw, expected):
    assert tickets.normalize_ticket_number(raw, now=NOW) == expected


# ------------------------------------------------------------------------ create_ticket
def test_create_ticket_routes_salary_to_finance_and_links_the_call(db):
    res = tickets.create_from_tool(EMPLOYEE, CALL)
    assert res["ok"] is True
    assert res["ticket_id"].startswith("EPP-") and res["ticket_id"].endswith("-000001")
    assert res["spoken_ticket_id"].startswith("E, P, P")
    assert res["assigned_department"] == "Finance" and res["priority"] == "medium"
    assert "CONFIRMATION LINE" in res["instruction"]
    t = db.get_ticket(res["ticket_id"])
    assert t["caller_type"] == "employee" and t["language"] == "hi"
    assert t["contact_number"] == "+919876543210"            # defaulted to the caller id
    assert t["call_id"] == "call1" and t["call_sid"] == "sid1"
    assert t["status"] == "open" and t["source"] == "voice"
    assert t["assigned_department_id"] == _dept(db, "Finance")["id"]
    kinds = [e["kind"] for e in db.list_ticket_events(res["ticket_id"])]
    assert kinds == ["created", "assigned"]


def test_create_ticket_uses_the_number_the_caller_gave(db):
    res = tickets.create_from_tool(dict(EMPLOYEE, contact_number="98765 00000"), CALL)
    assert db.get_ticket(res["ticket_id"])["contact_number"] == "+919876500000"


def test_harassment_is_high_priority_and_goes_to_hr(db):
    res = tickets.create_from_tool(dict(EMPLOYEE, category="Harassment",
                                        description="Her supervisor makes lewd comments every day.",
                                        high_priority_reason="harassment"), CALL)
    t = db.get_ticket(res["ticket_id"])
    assert t["priority"] == "high" and t["escalation_flag"] == 1 and t["escalation_reason"] == "harassment"
    assert t["assigned_department"] == "HR"


def test_keyword_net_raises_priority_even_when_the_agent_said_none(db):
    res = tickets.create_from_tool(dict(EMPLOYEE, category="Supervisor Complaint",
                                        description="My supervisor threatened to kill me if I complain again."), CALL)
    t = db.get_ticket(res["ticket_id"])
    assert t["priority"] == "high" and t["escalation_reason"].startswith("threat:")


def test_unknown_category_falls_back_to_other_and_admin(db):
    res = tickets.create_from_tool(dict(EMPLOYEE, category="Something Weird"), CALL)
    t = db.get_ticket(res["ticket_id"])
    assert t["category"] == "Other" and t["assigned_department"] == "Admin"


def test_near_miss_category_lands_on_the_right_row(db):
    res = tickets.create_from_tool(dict(EMPLOYEE, category="PF withdrawal"), CALL)
    t = db.get_ticket(res["ticket_id"])
    assert t["category"] == "PF" and t["assigned_department"] == "HR"


def test_customer_and_vendor_fields_are_kept(db):
    res = tickets.create_from_tool({"caller_type": "customer", "language": "en", "caller_name": "Meera",
                                    "company_name": "Acme Pipes", "description": "Order 4432 is ten days late.",
                                    "category": "Delivery Delay", "high_priority_reason": "none"}, CALL)
    t = db.get_ticket(res["ticket_id"])
    assert t["company_name"] == "Acme Pipes" and t["assigned_department"] == "Logistics"
    res = tickets.create_from_tool({"caller_type": "supplier", "language": "gu", "caller_name": "Jay",
                                    "vendor_code": "V-77", "description": "Invoice 881 unpaid for 90 days.",
                                    "category": "Payment Issue", "high_priority_reason": "none"}, CALL)
    t = db.get_ticket(res["ticket_id"])
    assert t["caller_type"] == "vendor" and t["vendor_code"] == "V-77" and t["assigned_department"] == "Finance"


def test_bad_input_never_raises(db):
    assert tickets.create_from_tool(dict(EMPLOYEE, caller_type="martian"), CALL)["ok"] is False
    assert tickets.create_from_tool(dict(EMPLOYEE, description=""), CALL)["ok"] is False
    assert db.list_tickets()["total"] == 0


def test_ticket_prefix_is_configurable(db, monkeypatch):
    monkeypatch.setenv("EPP_TICKET_PREFIX", "epp-x")
    res = tickets.create_from_tool(EMPLOYEE, CALL)
    assert res["ticket_id"].startswith("EPPX-")


# ------------------------------------------------------------------------ lookup_ticket
def test_lookup_returns_only_what_the_db_holds(db):
    tid = tickets.create_from_tool(EMPLOYEE, CALL)["ticket_id"]
    r = tickets.lookup("epp 2026 1" if tid.endswith("2026-000001") else tid)
    assert r["found"] is True and r["ticket_id"] == tid
    assert r["status"] == "open" and r["status_spoken"] == "Open"
    assert r["assigned_department"] == "Finance"
    tickets.change_status(db.get_ticket(tid), "under_review", user={"id": 1, "role": "admin"})
    assert tickets.lookup(tid)["status_spoken"] == "Under Review"
    missing = tickets.lookup("EPP-2026-000999")
    assert missing["found"] is False and "Never guess" in missing["instruction"]
    assert tickets.lookup("")["found"] is False


# ------------------------------------------------------------------------ status machine
ADMIN = {"id": 1, "username": "admin", "name": "Admin", "role": "admin"}
DEPT = {"id": 2, "username": "hr1", "name": "HR One", "role": "dept_user", "department_id": 2}


def test_status_transitions_and_reopen_rules(db):
    tid = tickets.create_from_tool(EMPLOYEE, CALL)["ticket_id"]
    t = tickets.change_status(db.get_ticket(tid), "under_review", DEPT, "looking into it")
    assert t["status"] == "under_review"
    t = tickets.change_status(t, "resolved", DEPT)
    assert t["status"] == "resolved" and t["resolved_at"]
    with pytest.raises(tickets.TicketError):
        tickets.change_status(t, "open", DEPT)                     # reopen is admin-only
    t = tickets.change_status(t, "open", ADMIN, "reopened on appeal")
    assert t["status"] == "open" and t["resolved_at"] is None
    with pytest.raises(tickets.TicketError):
        tickets.change_status(t, "open", ADMIN)                    # same status
    with pytest.raises(tickets.TicketError):
        tickets.change_status(t, "bogus", ADMIN)
    with pytest.raises(tickets.TicketError):
        tickets.change_status(dict(t, status="closed"), "escalated", ADMIN)   # not an allowed move
    t = tickets.change_status(t, "closed", ADMIN)
    assert t["closed_at"] and t["resolved_at"]
    kinds = [(e["kind"], e["from_value"], e["to_value"]) for e in db.list_ticket_events(tid) if e["kind"] == "status"]
    assert kinds[0] == ("status", "open", "under_review") and kinds[-1] == ("status", "open", "closed")


def test_assign_department_and_user(db):
    tid = tickets.create_from_tool(EMPLOYEE, CALL)["ticket_id"]
    hr = _dept(db, "HR")
    uid = db.create_user("hr1", "HR One", "h", "s", role="dept_user", department_id=hr["id"])
    t = tickets.assign(db.get_ticket(tid), department_id=hr["id"], user_id=uid, user=ADMIN)
    assert t["assigned_department"] == "HR" and t["assigned_user_name"] == "HR One"
    t = tickets.assign(t, user_id="", user=ADMIN)                  # unassign the person
    assert t["assigned_user_id"] is None
    with pytest.raises(tickets.TicketError):
        tickets.assign(t, department_id=999999, user=ADMIN)
    with pytest.raises(tickets.TicketError):
        tickets.assign(t, user=ADMIN)


def test_reclassify_reroutes_and_keeps_the_original(db):
    tid = tickets.create_from_tool(EMPLOYEE, CALL)["ticket_id"]
    t = tickets.reclassify(db.get_ticket(tid), "Safety Concern", ADMIN, "it was really about safety")
    assert t["category"] == "Safety Concern" and t["category_original"] == "Salary"
    assert t["assigned_department"] == "EHS"
    assert t["priority"] == "high"                                  # flagged category raises it
    with pytest.raises(tickets.TicketError):
        tickets.reclassify(t, "Delivery Delay", ADMIN)              # a customer category


def test_notes_and_priority(db):
    tid = tickets.create_from_tool(EMPLOYEE, CALL)["ticket_id"]
    t = tickets.add_note(db.get_ticket(tid), "Called back, no answer.", DEPT)
    assert db.list_ticket_events(tid)[-1]["note"] == "Called back, no answer."
    with pytest.raises(tickets.TicketError):
        tickets.add_note(t, "   ", DEPT)
    t = tickets.set_priority(t, "high", ADMIN)
    assert t["priority"] == "high" and t["escalation_flag"] == 1
    t = tickets.set_priority(t, "low", ADMIN)
    assert t["priority"] == "low"


# --------------------------------------------------------------------- post-call analysis
RESULT = {
    "summary": "Rahul reports salary unpaid for two months.", "intent": "salary delayed",
    "category": "Salary", "category_confidence": 0.9, "subcategory": "", "priority": "medium",
    "escalation_flags": [], "sentiment_score": -0.5, "sentiment_label": "negative",
    "caller_name": "", "contact_number": "", "company_name": "", "vendor_code": "",
    "employee_id": "", "caller_department": "", "plant_location": "", "description": "",
    "language": "hi", "caller_type": "employee", "is_status_inquiry": False, "should_create_ticket": True,
}


def test_apply_analysis_writes_summary_and_raises_priority_never_lowers(db):
    tid = tickets.create_from_tool(dict(EMPLOYEE, category="Harassment", high_priority_reason="harassment"), CALL)["ticket_id"]
    t = tickets.apply_analysis(db.get_ticket(tid), dict(RESULT, priority="medium"))
    assert t["priority"] == "high"                                  # not lowered
    assert t["ai_summary"] == RESULT["summary"] and t["sentiment_label"] == "negative"
    assert t["sentiment_score"] == -0.5
    tid2 = tickets.create_from_tool(EMPLOYEE, CALL)["ticket_id"]
    t2 = tickets.apply_analysis(db.get_ticket(tid2), dict(RESULT, priority="high", escalation_flags=["threat"]))
    assert t2["priority"] == "high" and t2["escalation_flag"] == 1 and t2["escalation_reason"] == "threat"
    assert json.loads(t2["escalation_flags"]) == ["threat"]
    assert [e["kind"] for e in db.list_ticket_events(tid2)][-2:] == ["ai_analysis", "priority"]


def test_apply_analysis_reclassifies_only_when_confident_and_no_human_decided(db):
    tid = tickets.create_from_tool(EMPLOYEE, CALL)["ticket_id"]
    # low confidence: keep the agent's category
    t = tickets.apply_analysis(db.get_ticket(tid), dict(RESULT, category="PF", category_confidence=0.5))
    assert t["category"] == "Salary"
    # confident: reclassify and re-route
    t = tickets.apply_analysis(t, dict(RESULT, category="PF", category_confidence=0.9))
    assert t["category"] == "PF" and t["category_original"] == "Salary" and t["assigned_department"] == "HR"
    # a human moved it: the AI must not move it back
    t = tickets.assign(t, department_id=_dept(db, "Finance")["id"], user=ADMIN)
    t = tickets.apply_analysis(t, dict(RESULT, category="Salary", category_confidence=0.99))
    assert t["category"] == "PF" and t["assigned_department"] == "Finance"


def test_apply_analysis_fills_blank_caller_fields_only(db):
    tid = tickets.create_from_tool(dict(EMPLOYEE, employee_id="", plant_location=""), CALL)["ticket_id"]
    t = tickets.apply_analysis(db.get_ticket(tid), dict(RESULT, employee_id="E9", plant_location="Halol",
                                                        caller_name="Someone Else"))
    assert t["employee_id"] == "E9" and t["plant_location"] == "Halol"
    assert t["caller_name"] == "Rahul Verma"                        # never overwritten


def _call_record(call_id, turns):
    return {"id": call_id, "call_sid": f"sid-{call_id}", "source": "plivo_inbound", "caller": "+919876543210",
            "started_at": "2026-09-20T02:30:00+00:00", "status": "completed", "ticket_id": None,
            "ticket_ids": [], "transcript": [{"role": r, "text": t, "ts": "x"} for r, t in turns],
            "tool_calls": []}


def test_post_call_creates_a_ticket_when_the_agent_did_not(db, monkeypatch):
    async def fake(prompt):
        return json.dumps(dict(RESULT, description="Rahul (E1042) at Halol has had no salary for two months.",
                               caller_name="Rahul Verma", employee_id="E1042"))
    monkeypatch.setattr(analysis, "_generate", fake)
    monkeypatch.setenv("EPP_ANALYSIS_ENABLED", "true")

    async def run():
        await store.save_call(_call_record("dropped1", [
            ("gemini", "Welcome to EPP Composites Support Helpline."),
            ("user", "Hindi. I am an employee, Rahul Verma, E1042, Halol plant. My salary has not come for two months."),
        ]))
        return await tickets.post_call("dropped1")
    t = asyncio.run(run())
    assert t and t["source"] == "post_call" and t["category"] == "Salary"
    assert t["caller_name"] == "Rahul Verma" and t["employee_id"] == "E1042"
    assert t["ai_summary"] == RESULT["summary"]
    call = asyncio.run(store.load_call("dropped1"))
    assert call["ticket_id"] == t["ticket_id"] and call["analysis"]["intent"] == "salary delayed"


def test_post_call_refines_the_existing_ticket_instead_of_duplicating(db, monkeypatch):
    async def fake(prompt):
        return json.dumps(dict(RESULT, priority="high", escalation_flags=["safety_incident"]))
    monkeypatch.setattr(analysis, "_generate", fake)

    async def run():
        call = _call_record("live1", [("user", "I am an employee and there was an accident on line two today, one worker injured")])
        await store.save_call(call)
        res = tickets.create_from_tool(EMPLOYEE, {"call_id": "live1", "call_sid": "sid-live1", "caller": "+919876543210"})
        return res["ticket_id"], await tickets.post_call("live1")
    tid, t = asyncio.run(run())
    assert t["ticket_id"] == tid and db.list_tickets()["total"] == 1
    assert t["priority"] == "high" and t["escalation_reason"] == "safety_incident"


def test_post_call_skips_short_calls_and_disabled_analysis(db, monkeypatch):
    called = []

    async def fake(prompt):
        called.append(prompt)
        return json.dumps(RESULT)
    monkeypatch.setattr(analysis, "_generate", fake)

    async def run():
        await store.save_call(_call_record("short1", [("user", "hello?")]))
        a = await tickets.post_call("short1")
        monkeypatch.setenv("EPP_ANALYSIS_ENABLED", "false")
        await store.save_call(_call_record("off1", [("user", "one two three four five six seven eight nine ten")]))
        b = await tickets.post_call("off1")
        return a, b
    assert asyncio.run(run()) == (None, None)
    assert called == []
