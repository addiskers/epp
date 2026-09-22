"""known_caller.py — recognise a returning caller from their number and hand the agent what
we already know, so it greets them by name instead of asking everything again."""
import pytest

import epp_seeds
import known_caller
import prompt_render as pr
import tickets


@pytest.fixture
def db(fresh_eo_db):
    fresh_eo_db.init()
    import main
    main.invalidate_ctx_cache()
    return fresh_eo_db


def _ticket(phone, name="Shivi Patel", **over):
    body = {"caller_type": "employee", "language": "gu", "caller_name": name, "contact_number": phone,
            "employee_id": "E77", "plant_location": "Halol", "description": "Washroom water not working.",
            "category": "Facility Complaint", "high_priority_reason": "none"}
    body.update(over)
    return tickets.create_from_tool(body, {})


def test_unknown_number_gives_no_profile(db):
    assert known_caller.lookup("+919000000009") is None
    assert known_caller.lookup("") is None


def test_profile_from_the_latest_ticket_with_open_tickets_listed(db):
    old = _ticket("+916355412603", name="Shivi", language="hi", category="Salary")
    tickets.change_status(db.get_ticket(old["ticket_id"]), "closed", {"id": 1, "role": "admin"})
    new = _ticket("+916355412603", name="Shivi Patel", language="gu")
    p = known_caller.lookup("6355412603")                      # bare number normalises to E.164
    assert p["name"] == "Shivi Patel" and p["first_name"] == "Shivi"
    assert p["caller_type"] == "employee" and p["language"] == "gu" and p["language_name"] == "Gujarati"
    assert p["details"]["employee_id"] == "E77" and p["details"]["plant_location"] == "Halol"
    assert [t["ticket_id"] for t in p["open_tickets"]] == [new["ticket_id"]]     # closed one excluded
    assert p["open_tickets"][0]["status_label"] == "Open"


def test_contacts_pool_fills_gaps_but_ticket_data_wins(db):
    db.add_contact("Shivi from sheet", "+916355412603", caller_type="vendor", notes="Halol vendor")
    assert known_caller.lookup("+916355412603")["name"] == "Shivi from sheet"
    assert known_caller.lookup("+916355412603")["caller_type"] == "vendor"
    _ticket("+916355412603", name="Shivi Patel")
    p = known_caller.lookup("+916355412603")
    assert p["name"] == "Shivi Patel" and p["caller_type"] == "employee"


def test_block_and_trigger_render_for_a_known_caller(db):
    _ticket("+916355412603")
    p = known_caller.lookup("+916355412603")
    extra = known_caller.placeholders(p)
    assert extra["known_caller_first_name"] == "Shivi" and extra["known_caller_language"] == "Gujarati"
    block = extra["known_caller_block"]
    assert block.startswith("## WHAT WE ALREADY KNOW ABOUT THIS CALLER")
    assert "Shivi Patel" in block and "employee" in block and "E77" in block and "Halol" in block
    assert "Open" in block and "Facility Complaint" in block
    assert "confirm" in block.lower() and "not them" in block.lower()
    seed = next(s for s in epp_seeds.SEEDS if s["slug"] == "epp_intake")
    assert pr.validate_template(seed["known_caller_trigger_template"]) == []
    r = pr.render_prompt(seed, caller_phone="+916355412603", extra=extra, known=True)
    assert "Shivi" in r["trigger"] and "Gujarati" in r["trigger"]
    assert "## WHAT WE ALREADY KNOW ABOUT THIS CALLER" in r["system_instruction"]


def test_unknown_caller_keeps_the_normal_opening(db):
    seed = next(s for s in epp_seeds.SEEDS if s["slug"] == "epp_intake")
    extra = known_caller.placeholders(None)
    assert extra["known_caller_block"] == ""
    r = pr.render_prompt(seed, caller_phone="+919000000009", extra=extra, known=False)
    assert "THE OPENING" in r["trigger"]
    assert "## WHAT WE ALREADY KNOW" not in r["system_instruction"]


def test_call_context_uses_the_known_caller_trigger(db):
    import main
    _ticket("+916355412603")
    ctx = main._resolve_call_context(caller="+916355412603")
    assert "Shivi" in ctx["trigger"] and "## WHAT WE ALREADY KNOW" in ctx["system_instruction"]
    ctx = main._resolve_call_context(caller="+919000000009")
    assert "THE OPENING" in ctx["trigger"]
