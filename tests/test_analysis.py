"""analysis.py — tolerant parsing and the retry-once contract."""

import asyncio
import json

import analysis

GOOD = {
    "summary": "An employee reports two months of unpaid salary.", "intent": "salary delayed",
    "is_status_inquiry": False, "caller_type": "Employee", "category": "Salary",
    "category_confidence": 0.92, "subcategory": "", "priority": "medium", "escalation_flags": [],
    "sentiment_score": -0.6, "sentiment_label": "negative", "language": "Hindi",
    "caller_name": "Rahul", "contact_number": "", "company_name": "", "vendor_code": "",
    "employee_id": "E123", "caller_department": "", "plant_location": "Halol",
    "description": "Rahul (E123) at Halol has not received salary for two months.",
    "should_create_ticket": True,
}


def test_parse_strips_fences_and_normalises():
    text = "```json\n" + json.dumps(GOOD) + "\n```"
    r = analysis.parse(text)
    assert r["caller_type"] == "employee" and r["language"] == "hi"
    assert r["category_confidence"] == 0.92 and r["priority"] == "medium"
    assert r["sentiment_label"] == "negative" and r["should_create_ticket"] is True


def test_parse_finds_the_object_inside_prose_and_clamps():
    data = dict(GOOD, category_confidence=7, sentiment_score=-9, sentiment_label="weird",
                escalation_flags="harassment", priority="medium")
    r = analysis.parse("Here you go: " + json.dumps(data) + " thanks")
    assert r["category_confidence"] == 1.0
    assert r["sentiment_score"] == -1.0 and r["sentiment_label"] == "negative"
    assert r["escalation_flags"] == ["harassment"]
    assert r["priority"] == "high"                      # a flag forces high


def test_parse_rejects_garbage():
    assert analysis.parse("") is None
    assert analysis.parse("not json at all") is None
    assert analysis.parse("[1,2,3]") is None


def test_analyze_retries_once_then_gives_up(monkeypatch):
    calls = []

    async def flaky(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return json.dumps(GOOD)

    async def instant(_):
        return None
    monkeypatch.setattr(analysis, "_generate", flaky)
    monkeypatch.setattr(asyncio, "sleep", instant)
    r = asyncio.run(analysis.analyze({"transcript": [{"role": "user", "text": "hi"}]}, []))
    assert r and r["category"] == "Salary" and len(calls) == 2

    calls.clear()

    async def broken(prompt):
        calls.append(prompt)
        return "nope"
    monkeypatch.setattr(analysis, "_generate", broken)
    assert asyncio.run(analysis.analyze({"transcript": []}, [])) is None
    assert len(calls) == 2


def test_prompt_lists_categories_per_type_and_the_existing_ticket():
    cats = [{"caller_type": "employee", "name": "Salary", "active": 1},
            {"caller_type": "vendor", "name": "Payment Issue", "active": 1}]
    p = analysis.build_prompt({"transcript": [{"role": "user", "text": "salary nahi aayi"}]}, cats,
                              {"ticket_id": "EPP-2026-000001", "caller_type": "employee",
                               "category": "Salary", "priority": "medium"})
    assert "- employee: Salary" in p and "- vendor: Payment Issue" in p
    assert "Caller: salary nahi aayi" in p
    assert "already registered ticket EPP-2026-000001" in p
