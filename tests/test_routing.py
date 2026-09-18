"""routing.py — category resolution, priority nets, keyword fallback."""

import routing

CATS = [
    {"caller_type": "employee", "name": "Salary", "department_id": 1, "high_priority": 0, "active": 1,
     "keywords": '["salary", "wages"]'},
    {"caller_type": "employee", "name": "PF", "department_id": 2, "high_priority": 0, "active": 1,
     "keywords": '["pf", "provident fund"]'},
    {"caller_type": "employee", "name": "Harassment", "department_id": 2, "high_priority": 1, "active": 1,
     "keywords": '["harass"]'},
    {"caller_type": "employee", "name": "Supervisor Complaint", "department_id": 2, "high_priority": 0, "active": 1},
    {"caller_type": "employee", "name": "Other", "department_id": 9, "high_priority": 0, "active": 1},
    {"caller_type": "customer", "name": "Delivery Delay", "department_id": 3, "high_priority": 0, "active": 1,
     "keywords": ["delay", "late"]},
    {"caller_type": "customer", "name": "Other", "department_id": 9, "high_priority": 0, "active": 1},
    {"caller_type": "customer", "name": "Gone", "department_id": 3, "high_priority": 0, "active": 0},
]


def test_caller_type_normalisation():
    assert routing.normalize_caller_type("Employee") == "employee"
    assert routing.normalize_caller_type("a vendor") == "vendor"
    assert routing.normalize_caller_type("supplier") == "vendor"
    assert routing.normalize_caller_type("client") == "customer"
    assert routing.normalize_caller_type("") == ""
    assert routing.normalize_caller_type("alien") == ""


def test_resolve_category_exact_near_miss_and_fallback():
    assert routing.resolve_category("employee", "salary", CATS)["name"] == "Salary"
    assert routing.resolve_category("employee", "PF withdrawal", CATS)["name"] == "PF"
    assert routing.resolve_category("customer", "delivery delayed", CATS)["name"] == "Delivery Delay"
    assert routing.resolve_category("employee", "Delivery Delay", CATS)["name"] == "Other"   # wrong type -> that type's Other
    assert routing.resolve_category("employee", "", CATS)["name"] == "Other"
    assert routing.resolve_category("customer", "Gone", CATS)["name"] == "Other"             # inactive
    assert routing.resolve_category("vendor", "Payment", CATS) is None                        # no rows at all


def test_priority_from_the_agents_reason():
    assert routing.detect_priority("harassment", None, "") == ("high", 1, "harassment")
    assert routing.detect_priority("Medical Emergency", None, "") == ("high", 1, "medical_emergency")
    assert routing.detect_priority("none", None, "my salary is late") == ("medium", 0, "")
    assert routing.detect_priority("", None, "") == ("medium", 0, "")
    assert routing.detect_priority("made_up", None, "") == ("medium", 0, "")


def test_priority_from_a_flagged_category():
    cat = routing.resolve_category("employee", "Harassment", CATS)
    assert routing.detect_priority("none", cat, "he keeps calling me names") == ("high", 1, "category:Harassment")


def test_priority_from_the_keyword_net_even_when_the_agent_says_none():
    p, f, why = routing.detect_priority("none", CATS[3], "my supervisor threatened to kill me if I complain")
    assert (p, f) == ("high", 1) and why.startswith("threat:")
    p, f, why = routing.detect_priority("none", None, "there was an accident on line 2, one worker got injured")
    assert (p, f) == ("high", 1) and why.startswith("safety_incident:")
    p, _, why = routing.detect_priority("none", None, "mera supervisor ne mujhe dhamki di")
    assert p == "high" and "threat" in why
    p, _, why = routing.detect_priority("none", None, "someone stole my laptop from the locker, chori ho gayi")
    assert p == "high" and "security_incident" in why


def test_keyword_net_does_not_fire_on_ordinary_complaints():
    assert routing.detect_priority("none", None, "I was fired from my job without notice") == ("medium", 0, "")
    assert routing.detect_priority("none", None, "the delivery is late again") == ("medium", 0, "")
    assert routing.detect_priority("none", None, "my PF has not been deposited") == ("medium", 0, "")


def test_classify_by_keywords_uses_the_configured_row_keywords():
    assert routing.classify_by_keywords("employee", "my wages were cut", CATS) == "Salary"
    assert routing.classify_by_keywords("customer", "the shipment is late and delayed", CATS) == "Delivery Delay"
    assert routing.classify_by_keywords("employee", "nothing relevant", CATS) is None


def test_max_priority_only_raises():
    assert routing.max_priority("medium", "high") == "high"
    assert routing.max_priority("high", "medium") == "high"
    assert routing.max_priority("low", "medium") == "medium"
    assert routing.max_priority("", "medium") == "medium"
    assert routing.max_priority("high", "") == "high"
