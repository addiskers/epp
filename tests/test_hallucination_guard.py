"""hallucination_guard.py — catch the agent announcing a reference number that no tool
produced, in any of the helpline's languages, and nudge it to call create_ticket."""
import pytest

import hallucination_guard as hg


@pytest.mark.parametrize("text", [
    "Your concern has been successfully registered. Your reference number is Product Quality 004.",
    "your reference number is E P P two zero two six zero zero zero one two three",
    "The ticket number is TKT-2026-000123, please note it down.",
    "aapka complaint number hai TKT 2026 000004",
    "आपका रेफरेंस नंबर है टी के टी दो शून्य दो छह",
    "તમારો રેફરન્સ નંબર છે",
    "Aapki shikayat register ho gayi hai.",
])
def test_reference_phrases_are_detected(text, monkeypatch):
    monkeypatch.setenv("EPP_TICKET_PREFIX", "TKT")
    assert hg.spoke_a_reference(text) is True


@pytest.mark.parametrize("text", [
    "Please tell me your preferred language.",
    "May I have your full name, please?",
    "Do you have your reference number?",                       # asking for one is fine
    "Kya aapke paas reference number hai?",
    "Please explain your concern in detail. Take your time.",
    "",
])
def test_ordinary_turns_are_not_flagged(text, monkeypatch):
    monkeypatch.setenv("EPP_TICKET_PREFIX", "TKT")
    assert hg.spoke_a_reference(text) is False


def test_guard_nudges_until_a_real_ticket_exists_at_most_twice(monkeypatch):
    monkeypatch.setenv("EPP_TICKET_PREFIX", "EPP")
    g = hg.HallucinationGuard()
    assert g.check("Your reference number is EPP 2026 000123.") is not None
    assert g.check("Once again, the reference number is EPP 2026 000123.") is not None
    assert g.check("Your reference number is EPP 2026 000123.") is None       # budget spent
    assert g.nudged == 2


def test_guard_is_silent_after_create_ticket_succeeded():
    g = hg.HallucinationGuard()
    g.on_tool_call("create_ticket", {"ok": True, "ticket_id": "EPP-2026-000001"})
    assert g.ticket_ok is True
    assert g.check("Your reference number is E P P two zero two six zero zero zero zero zero one.") is None
    assert g.nudged == 0


def test_a_failed_create_ticket_does_not_count():
    g = hg.HallucinationGuard()
    g.on_tool_call("create_ticket", {"ok": False, "error": "boom"})
    assert g.ticket_ok is False
    assert g.check("Your reference number is EPP 2026 000123.") is not None


def test_a_successful_lookup_also_legitimises_a_spoken_number():
    """A status inquiry: the agent reads back the number the caller gave, which lookup confirmed."""
    g = hg.HallucinationGuard()
    g.on_tool_call("lookup_ticket", {"found": True, "ticket_id": "EPP-2026-000001", "status": "open"})
    assert g.check("Ticket number EPP 2026 000001 is currently Open.") is None


def test_nudge_text_tells_the_agent_what_to_do():
    g = hg.HallucinationGuard()
    n = g.check("your reference number is TKT 2026 000009")
    assert "create_ticket" in n and "invented" in n and n.startswith("[")
