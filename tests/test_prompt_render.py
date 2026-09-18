"""prompt_render.py — placeholder resolution, spoken forms, and the cleanup that stops a
missing value from being read aloud as punctuation."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import epp_seeds
import prompt_render as pr

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 20, 8, 0, tzinfo=IST)

CATEGORIES = [
    {"caller_type": "employee", "name": "Salary", "active": 1},
    {"caller_type": "employee", "name": "PF", "active": 1},
    {"caller_type": "customer", "name": "Delivery Delay", "active": 1},
    {"caller_type": "customer", "name": "Retired", "active": 0},
    {"caller_type": "vendor", "name": "Payment Issue", "active": 1},
]
DEPARTMENTS = [{"name": "Finance", "active": 1}, {"name": "HR", "active": 1}, {"name": "Old", "active": 0}]


# --------------------------------------------------------------------------- spoken forms
@pytest.mark.parametrize("raw,expected", [
    ("19:00", "seven in the evening"),
    ("10:30", "half past ten in the morning"),
    ("13:00", "one in the afternoon"),
    ("18:45", "quarter to seven in the evening"),
])
def test_times_are_spoken_never_digits(raw, expected):
    assert pr._spoken_time(raw) == expected


def test_dates_are_spoken():
    assert pr._spoken_date("2026-09-19") == "the nineteenth of September"
    assert pr._spoken_date("nonsense") == ""


def test_phone_numbers_are_spoken_digit_by_digit_in_groups_of_five():
    assert pr.spoken_phone("+919876543210") == "nine eight seven six five, four three two one zero"
    assert pr.spoken_phone("9876543210") == "nine eight seven six five, four three two one zero"
    assert pr.spoken_phone("+1 978 571 5824") == "one nine seven eight five, seven one five eight two, four"
    assert pr.spoken_phone("") == ""


# ----------------------------------------------------------------------------- context
def test_build_context_carries_the_helpline_identity_and_lists(monkeypatch):
    monkeypatch.setenv("EPP_COMPANY_NAME", "EPP Composites")
    monkeypatch.delenv("EPP_HELPLINE_NAME", raising=False)
    monkeypatch.delenv("EPP_ENABLED_LANGUAGES", raising=False)
    ctx = pr.build_context(caller_phone="+919876543210", categories=CATEGORIES,
                           departments=DEPARTMENTS, now=NOW)
    assert ctx["company_name"] == "EPP Composites"
    assert ctx["helpline_name"] == "EPP Composites Support Helpline"
    assert ctx["today_spoken"] == "the twentieth of September"
    assert ctx["today_iso"] == "2026-09-20"
    assert ctx["caller_phone"] == "+919876543210"
    assert ctx["caller_phone_spoken"].startswith("nine eight")
    assert ctx["language_list"].startswith("English, Hindi") and ctx["language_list"].endswith("and Assamese")
    assert ctx["department_list"] == "Finance, HR"                        # inactive dropped
    assert ctx["category_list"] == ("- Customer: Delivery Delay\n"
                                    "- Vendor: Payment Issue\n"
                                    "- Employee: Salary, PF")


def test_a_blank_extra_never_shadows_a_real_value():
    ctx = pr.build_context(caller_phone="+919876543210", now=NOW, extra={"caller_phone": ""})
    assert ctx["caller_phone"] == "+919876543210"
    ctx2 = pr.build_context(now=NOW, extra={"company_name": "Sample Co"})
    assert ctx2["company_name"] == "Sample Co"


# ------------------------------------------------------------------------- missing values
def test_missing_placeholder_blanks_and_is_reported_never_raises():
    out, missing = pr.render("Welcome to {helpline_name} run by {company_name}.", {"helpline_name": "H"})
    assert missing == ["company_name"]
    assert "{company_name}" not in out and "run by ." not in out


def test_a_line_that_lost_its_only_fact_is_dropped_whole():
    out, _ = pr.render("- Company: {company_name}\n- Caller: {caller_phone}", {"company_name": "EPP"})
    assert "- Company: EPP" in out
    assert "Caller" not in out


def test_dangling_prepositions_are_removed():
    out, _ = pr.render("Registered at {now_time} by {helpline_name}.", {})
    assert "at by" not in out and "at ." not in out


def test_author_prose_is_never_rewritten():
    template = ("## WHO YOU ARE SPEAKING TO\n"
                "Branch on their reply:\n"
                "- Their number: {caller_phone_spoken}\n"
                "Call from {helpline_name}.")
    out, _ = pr.render(template, {"caller_phone_spoken": "nine nine"})
    assert "## WHO YOU ARE SPEAKING TO" in out
    assert "Branch on their reply:" in out
    assert "- Their number: nine nine" in out


def test_fully_resolved_text_is_unchanged():
    out, missing = pr.render("Full: {helpline_name} at {now_time}.", {"helpline_name": "H", "now_time": "ten"})
    assert missing == [] and out == "Full: H at ten."


# --------------------------------------------------------------------- unknown placeholders
def test_unknown_placeholder_is_an_authoring_bug_not_missing_data():
    assert pr.validate_template("Hello {helpline_nam} and {company_name}") == ["helpline_nam"]
    assert pr.validate_template("Hello {company_name}") == []
    with pytest.raises(pr.PromptRenderError):
        pr.render("Hello {helpline_nam}", {}, strict=True)


def test_unknown_placeholder_does_not_break_a_live_call():
    out, _ = pr.render("Hello {helpline_nam}!", {})
    assert "{helpline_nam}" not in out


def test_shipped_agent_uses_only_known_placeholders():
    for seed in epp_seeds.SEEDS:
        assert pr.validate_template(seed["prompt_template"]) == [], seed["slug"]
        assert pr.validate_template(seed["trigger_template"]) == [], seed["slug"]


# ------------------------------------------------------------------------- render_prompt
def test_render_prompt_returns_instruction_and_trigger():
    agent = {"slug": "t", "prompt_template": "You answer for {helpline_name}.",
             "trigger_template": "[Say welcome to {helpline_name}.]"}
    r = pr.render_prompt(agent, now=NOW, extra={"helpline_name": "The Line"})
    assert r["system_instruction"] == "You answer for The Line."
    assert r["trigger"] == "[Say welcome to The Line.]"
    assert r["missing"] == []


def test_an_agent_with_no_trigger_still_gets_one():
    r = pr.render_prompt({"prompt_template": "x", "trigger_template": ""}, now=NOW)
    assert r["trigger"] == pr.DEFAULT_TRIGGER


def test_render_prompt_never_raises_on_missing_rows():
    r = pr.render_prompt({"prompt_template": "Categories:\n{category_list}\nDepts: {department_list}"}, now=NOW)
    assert r["system_instruction"]
    assert set(r["missing"]) == {"category_list", "department_list"}


# ------------------------------------------------- the shipped intake agent: requirements
def test_intake_prompt_carries_the_verbatim_greeting_and_confirmation_line():
    t = epp_seeds.INTAKE_PROMPT
    assert '"Welcome to {helpline_name}.' in t
    assert "Your concern has been successfully registered. Your reference number is" in t
    assert "forwarded to the concerned department for review and action" in t
    assert "Please explain your concern in detail" in t
    assert "Do you have your reference number?" in t


def test_intake_prompt_never_promises_outcomes_or_invents_status():
    t = epp_seeds.INTAKE_PROMPT
    assert "Never promise an outcome" in t
    assert "Never invent a ticket status" in t
    assert "NONE of these is a reason to end the call" in t
    assert "SPEAK TO A PERSON" in t
    for bad in ("record_rsvp", "wedding", "guest", "RSVP"):
        assert bad not in t, bad
    # record_outcome is only for OUTBOUND (campaign) calls; the inbound flow never mentions it
    inbound_part = t.split("## OUTBOUND CALLS")[0]
    assert "record_outcome" not in inbound_part


def test_intake_prompt_asks_every_required_detail_per_caller_type():
    t = epp_seeds.INTAKE_PROMPT
    assert "Customer: their full name; their company's name; their contact number." in t
    assert "Vendor: their full name; their vendor code" in t
    assert "Employee: their full name; their employee code or employee ID; their department" in t
    for q in ("When did this start?", "Has this happened before?", "Have you reported this before",
              "Is anyone else involved?", "Is this affecting operations, your work, or anyone's safety?"):
        assert q in t, q


def test_intake_prompt_renders_with_no_gaps_against_the_seeds(monkeypatch):
    monkeypatch.delenv("EPP_ENABLED_LANGUAGES", raising=False)
    cats = [{"caller_type": ct, "name": n, "active": 1} for ct, n, *_ in epp_seeds.CATEGORIES]
    deps = [{"name": n, "active": 1} for _, n in epp_seeds.DEPARTMENTS]
    r = pr.render_prompt(epp_seeds.SEEDS[0], caller_phone="+919876543210", categories=cats,
                         departments=deps, now=NOW)
    assert r["missing"] == []
    assert "Welcome to EPP Composites Support Helpline." in r["system_instruction"]
    assert "- Employee: Salary, Payslip" in r["system_instruction"]
    assert "Punjabi, Odia and Assamese" in r["system_instruction"]
