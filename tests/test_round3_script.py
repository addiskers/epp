"""Client review round 3 — the script and bookkeeping fixes that reach the phone.

The Gujarati loop: the agent asked "which language?", the caller said Gujarati, and the agent
asked the same question again in Gujarati. The opening now says "Thank you for calling", the
language question is asked once and latched, a recognised caller is never made to recite a
reference number we already hold, and every call leaves an audit row saying whether a ticket
was created."""
import pytest

import audit
import epp_seeds
import known_caller
import tickets


@pytest.fixture
def db(fresh_eo_db):
    fresh_eo_db.init()
    return fresh_eo_db


# ------------------------------------------------------------------ the script
def test_opening_thanks_the_caller_and_asks_the_language_once():
    t = epp_seeds.INTAKE_PROMPT
    assert '"Thank you for calling {helpline_name}. Please tell me your preferred language' in t
    assert "Welcome to" not in t
    assert "asked ONCE, in THE OPENING, and never again" in t
    assert 'Never ask "which language" a second time' in t
    assert "follow them silently" in t
    assert "Check on EVERY turn" not in t                     # the sentence the model over-applied
    assert "never ask it again" in epp_seeds.INTAKE_TRIGGER
    assert '"Thank you for calling {helpline_name}. Am I speaking' in epp_seeds.INTAKE_KNOWN_CALLER_TRIGGER


def test_status_inquiry_uses_the_known_ticket_instead_of_asking_for_it():
    t = epp_seeds.INTAKE_PROMPT
    section = t.split("## STATUS INQUIRY")[1].split("## COLLECT")[0]
    assert "do NOT ask for the reference number" in section
    assert "never make them recite" in section.lower() or "Never make them recite" in section
    assert 'Otherwise ask: "Do you have your reference number?"' in section


def test_known_caller_rules_hand_the_agent_the_ticket_number():
    profile = {"phone": "+919904240078", "name": "Shreya Patel", "first_name": "Shreya",
               "caller_type": "customer", "language": "gu", "language_name": "Gujarati",
               "details": {"company_name": "Acme"}, "tickets_total": 1, "source": "ticket",
               "open_tickets": [{"ticket_id": "EPP-2026-000007", "status_label": "Under Review",
                                 "category": "Product Defect", "created_spoken": "the third of September"}]}
    block = known_caller.placeholders(profile)["known_caller_block"]
    assert "call lookup_ticket yourself with the ticket number listed above" in block
    assert "never make them recite a number you already hold" in block
    assert "EPP-2026-000007" in block


def test_old_scripts_are_flagged_for_each_new_rule(db):
    agent = db.intake_agent()
    assert db.stale_agent_reasons(agent) == []
    old = (agent["prompt_template"]
           .replace("Thank you for calling", "Welcome to")
           .replace("asked ONCE, in THE OPENING", "asked in THE OPENING")
           .replace("make them recite", "always ask for"))
    db.update_agent(agent["id"], prompt_template=old)
    reasons = " | ".join(db.stale_agent_reasons(db.intake_agent()))
    assert "Welcome to" in reasons and "second time" in reasons and "recite" in reasons


# ------------------------------------------------------------------ phone numbers
@pytest.mark.parametrize("raw, want", [
    ("98765 43210", "+919876543210"),
    ("098765 43210", "+919876543210"),            # domestic trunk prefix
    ("0 98765-43210", "+919876543210"),
    ("+91 98765 43210", "+919876543210"),
    ("919876543210", "+919876543210"),
    ("+44 20 7946 0958", "+442079460958"),
    ("98765", ""),                                # not a phone number
    ("", ""),
])
def test_clean_phone_accepts_every_way_people_type_a_number(raw, want):
    assert tickets._clean_phone(raw) == want


def test_clean_phone_falls_back_to_the_calling_number():
    assert tickets._clean_phone("", "919904240078") == "+919904240078"
    assert tickets._clean_phone("12", "+919904240078") == ""     # a slip is not the caller's number


# ------------------------------------------------------------------ audit rows per call
CALL = {"call_id": "call-a1", "call_sid": "sid-a1", "caller": "+919904240078"}
BODY = {"caller_type": "customer", "language": "gu", "caller_name": "Shreya", "company_name": "Acme",
        "description": "Smoke from a machine in the production department.", "category": "Product Defect",
        "high_priority_reason": "none"}


def _rows(db, action):
    return [a for a in db.list_audit(limit=50)["items"] if a["action"] == action]


def test_agent_created_ticket_is_in_the_audit_log(db):
    res = tickets.create_from_tool(BODY, CALL)
    assert res["ok"]
    rows = _rows(db, "ticket_created")
    assert len(rows) == 1
    row = rows[0]
    assert row["target"] == f"ticket:{res['ticket_id']}" and row["username"] == audit.AGENT["username"]
    assert '"source": "voice"' in row["detail"] and '"phone": "+919904240078"' in row["detail"]


def test_post_call_created_ticket_says_so(db):
    res = tickets.create_from_tool(BODY, dict(CALL, source="post_call"))
    assert res["ok"]
    assert '"source": "post_call"' in _rows(db, "ticket_created")[0]["detail"]


def test_a_call_without_a_ticket_gets_its_own_row(db):
    import main
    main.audit_call_outcome({"id": "call-b1", "source": "plivo_inbound", "caller": "+919904240078",
                             "duration_seconds": 61, "ticket_ids": [], "lookup_ticket_ids": ["EPP-2026-000001"]})
    main.audit_call_outcome({"id": "call-b2", "source": "plivo_campaign", "caller": "+919904240078",
                             "duration_seconds": 20, "ticket_ids": [], "outcome": "no_concern"})
    main.audit_call_outcome({"id": "call-b3", "source": "plivo_inbound", "caller": "+919904240078",
                             "duration_seconds": 5, "ticket_ids": []})
    # these must NOT write a row: a browser test, a call that produced a ticket, an empty record
    main.audit_call_outcome({"id": "call-b4", "source": "browser", "ticket_ids": []})
    main.audit_call_outcome({"id": "call-b5", "source": "plivo_inbound", "ticket_ids": ["EPP-2026-000009"]})
    main.audit_call_outcome({})
    main.audit_call_outcome(None)
    rows = _rows(db, "call_no_ticket")
    by_target = {r["target"]: r["detail"] for r in rows}
    assert set(by_target) == {"call:call-b1", "call:call-b2", "call:call-b3"}
    assert '"reason": "status_inquiry"' in by_target["call:call-b1"]
    assert '"reason": "outcome:no_concern"' in by_target["call:call-b2"]
    assert '"reason": "no_ticket"' in by_target["call:call-b3"] and '"duration_seconds": 5' in by_target["call:call-b3"]
