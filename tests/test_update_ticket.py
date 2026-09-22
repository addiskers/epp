"""update_ticket — the agent can correct a ticket it registered on THIS call (a misheard name,
a wrong number, something the caller added after the number was read out)."""
import pytest

import agent_tools
import hallucination_guard as hg
import tickets


@pytest.fixture
def db(fresh_eo_db):
    fresh_eo_db.init()
    return fresh_eo_db


CALL = {"call_id": "call1", "call_sid": "sid1", "caller": "+919904240078"}
BODY = {"caller_type": "customer", "language": "en", "caller_name": "Shyam", "company_name": "Acme",
        "description": "Smoke from a machine in the production department.", "category": "Product Defect",
        "high_priority_reason": "none"}


def test_tool_is_declared_for_every_call_type():
    for tools in (agent_tools.build_tools(), agent_tools.build_tools(campaign_type="intake")):
        names = [t["name"] for t in tools]
        assert "update_ticket" in names and names[-1] == "end_call"
    props = agent_tools.UPDATE_TICKET_DECLARATION["parameters"]["properties"]
    assert {"ticket_id", "caller_name", "contact_number", "additional_details"} <= set(props)


def test_correct_a_name_on_this_calls_ticket(db):
    tid = tickets.create_from_tool(BODY, CALL)["ticket_id"]
    res = tickets.update_from_tool({"ticket_id": tid, "caller_name": "Shreya"}, CALL, [tid])
    assert res["ok"] is True and res["changed"] == ["caller_name"]
    assert "Shreya" in res["instruction"]
    t = db.get_ticket(tid)
    assert t["caller_name"] == "Shreya"
    ev = db.list_ticket_events(tid)[-1]
    assert ev["kind"] == "corrected" and ev["from_value"] == "Shyam" and ev["to_value"] == "Shreya"


def test_spoken_number_variants_and_additional_details(db):
    tid = tickets.create_from_tool(BODY, CALL)["ticket_id"]
    res = tickets.update_from_tool({"ticket_id": "E P P 2026 1", "contact_number": "98765 43210",
                                    "additional_details": "The machine is on line two."}, CALL, [tid])
    assert res["ok"] is True and sorted(res["changed"]) == ["additional_details", "contact_number"]
    t = db.get_ticket(tid)
    assert t["contact_number"] == "+919876543210"
    assert t["description"].endswith("The machine is on line two.")


def test_only_tickets_from_this_call_may_be_changed(db):
    other = tickets.create_from_tool(BODY, {"call_id": "someone-else"})["ticket_id"]
    res = tickets.update_from_tool({"ticket_id": other, "caller_name": "Mallory"}, CALL, [])
    assert res["ok"] is False and "this call" in res["instruction"]
    assert db.get_ticket(other)["caller_name"] == "Shyam"
    missing = tickets.update_from_tool({"ticket_id": "EPP-2026-000999", "caller_name": "X"}, CALL, ["EPP-2026-000999"])
    assert missing["ok"] is False


def test_blank_and_unknown_fields_are_ignored(db):
    tid = tickets.create_from_tool(BODY, CALL)["ticket_id"]
    res = tickets.update_from_tool({"ticket_id": tid, "caller_name": "", "priority": "low", "status": "closed"}, CALL, [tid])
    assert res["ok"] is False and "nothing" in res["instruction"].lower()
    t = db.get_ticket(tid)
    assert t["caller_name"] == "Shyam" and t["status"] == "open" and t["priority"] == "medium"


def test_guard_accepts_a_successful_update_as_proof_of_a_ticket():
    g = hg.HallucinationGuard()
    g.on_tool_call("update_ticket", {"ok": True, "ticket_id": "EPP-2026-000001"})
    assert g.ticket_ok is True


def test_blank_name_is_refused_at_creation(db):
    res = tickets.create_from_tool(dict(BODY, caller_name="  "), CALL)
    assert res["ok"] is False and "name" in res["instruction"].lower() and "Not given" in res["instruction"]
    ok = tickets.create_from_tool(dict(BODY, caller_name="Not given"), CALL)
    assert ok["ok"] is True
