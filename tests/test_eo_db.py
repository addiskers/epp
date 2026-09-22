"""eo_db.py — schema v2, seeds, ticket numbering, scoped listing, stats, audit."""

import epp_seeds


def test_fresh_schema_has_every_table_and_is_stamped(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    conn = eo_db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert {"users", "departments", "categories", "agents", "tickets", "ticket_events",
            "audit_log", "ticket_sequences", "settings", "agent_versions"} <= tables
    assert eo_db.schema_version() == eo_db.SCHEMA_VERSION
    eo_db.init()                                     # idempotent
    assert eo_db.schema_version() == eo_db.SCHEMA_VERSION


def test_departments_and_categories_are_seeded_once(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    assert len(eo_db.list_departments()) == len(epp_seeds.DEPARTMENTS)
    assert len(eo_db.list_categories()) == len(epp_seeds.CATEGORIES)
    eo_db.init()
    assert len(eo_db.list_departments()) == len(epp_seeds.DEPARTMENTS)
    assert len(eo_db.list_categories()) == len(epp_seeds.CATEGORIES)
    # every category resolved its department code
    assert all(c["department_id"] for c in eo_db.list_categories())
    # the requirements mapping: a few spot checks
    by = {(c["caller_type"], c["name"]): c["department_name"] for c in eo_db.list_categories()}
    assert by[("employee", "Salary")] == "Finance"
    assert by[("employee", "PF")] == "HR"
    assert by[("employee", "Safety Concern")] == "EHS"
    assert by[("customer", "Delivery Delay")] == "Logistics"
    assert by[("vendor", "Purchase Order Issue")] == "Procurement"
    assert by[("employee", "ERP Access")] == "IT"


def test_high_priority_categories_are_flagged(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    flagged = {c["name"] for c in eo_db.list_categories() if c["high_priority"]}
    assert {"Harassment", "Safety Concern", "Workplace Misconduct"} <= flagged
    assert "Salary" not in flagged


def test_intake_agent_is_seeded_once_and_edits_survive(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    assert [a["slug"] for a in eo_db.list_agents()] == ["epp_intake", "epp_followup", "epp_announcement"]
    agent = eo_db.intake_agent()
    eo_db.update_agent(agent["id"], prompt_template="MY EDITED PROMPT")
    eo_db.init()
    assert len(eo_db.list_agents()) == 3
    assert eo_db.intake_agent()["prompt_template"] == "MY EDITED PROMPT"
    # an explicit reset restores the shipped text
    assert eo_db.refresh_seed_agent("epp_intake")
    assert eo_db.intake_agent()["prompt_template"] == epp_seeds.INTAKE_PROMPT


def test_ticket_numbers_are_sequential_per_year(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    assert eo_db.next_ticket_id("EPP", 2026) == "EPP-2026-000001"
    assert eo_db.next_ticket_id("EPP", 2026) == "EPP-2026-000002"
    assert eo_db.next_ticket_id("EPP", 2027) == "EPP-2027-000001"
    assert eo_db.next_ticket_id("EPP", 2026) == "EPP-2026-000003"


def _ticket(eo_db, tid, dept_name, **over):
    dept = next(d for d in eo_db.list_departments() if d["name"] == dept_name)
    fields = dict(caller_type="employee", caller_name="A", description="d", category="x",
                  priority="medium", assigned_department_id=dept["id"], status="open")
    fields.update(over)
    eo_db.create_ticket(tid, **fields)
    return eo_db.get_ticket(tid)


def test_list_tickets_scopes_filters_and_searches(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    _ticket(eo_db, "EPP-2026-000001", "HR", caller_name="Rahul Verma", priority="high", escalation_flag=1)
    _ticket(eo_db, "EPP-2026-000002", "Finance", caller_name="Priya", caller_type="vendor")
    _ticket(eo_db, "EPP-2026-000003", "HR", status="closed", description="printer broken")
    hr = next(d for d in eo_db.list_departments() if d["name"] == "HR")["id"]

    assert eo_db.list_tickets()["total"] == 3
    assert eo_db.list_tickets(scope_department_id=hr)["total"] == 2
    assert eo_db.list_tickets(scope_department_id=-1)["total"] == 0          # dept user with no dept sees nothing
    assert eo_db.list_tickets(status="open,closed", scope_department_id=hr)["total"] == 2
    assert eo_db.list_tickets(status="closed")["total"] == 1
    assert eo_db.list_tickets(priority="high")["total"] == 1
    assert eo_db.list_tickets(escalated=True)["items"][0]["ticket_id"] == "EPP-2026-000001"
    assert eo_db.list_tickets(caller_type="vendor")["total"] == 1
    assert eo_db.list_tickets(q="rahul")["total"] == 1
    assert eo_db.list_tickets(q="000002")["total"] == 1
    assert eo_db.list_tickets(q="printer")["total"] == 1
    # priority sort puts high first
    assert eo_db.list_tickets(sort="priority", direction="asc")["items"][0]["priority"] == "high"
    assert eo_db.list_tickets(limit=1)["items"] and eo_db.list_tickets(limit=1)["total"] == 3


def test_ticket_stats_counts_open_high_and_by_department(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    _ticket(eo_db, "EPP-2026-000001", "HR", priority="high", escalation_flag=1)
    _ticket(eo_db, "EPP-2026-000002", "Finance")
    _ticket(eo_db, "EPP-2026-000003", "HR", status="resolved")
    s = eo_db.ticket_stats()
    assert s["total"] == 3 and s["open"] == 2 and s["high_open"] == 1 and s["today"] == 3
    assert s["by_department"] == {"HR": 2, "Finance": 1}
    assert s["by_status"]["resolved"] == 1
    assert [t["ticket_id"] for t in s["recent_escalated"]] == ["EPP-2026-000001"]
    hr = next(d for d in eo_db.list_departments() if d["name"] == "HR")["id"]
    assert eo_db.ticket_stats(scope_department_id=hr)["total"] == 2


def test_users_carry_a_department_and_roles(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    hr = next(d for d in eo_db.list_departments() if d["name"] == "HR")["id"]
    uid = eo_db.create_user("hr1", "HR One", "h", "s", role="dept_user", department_id=hr)
    row = next(u for u in eo_db.list_users() if u["id"] == uid)
    assert row["role"] == "dept_user" and row["department_name"] == "HR"
    eo_db.set_user_department(uid, None)
    assert eo_db.get_user(uid)["department_id"] is None


def test_audit_log_lists_and_filters(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    eo_db.add_audit(user_id=1, username="admin", action="login", ip="1.2.3.4")
    eo_db.add_audit(user_id=1, username="admin", action="ticket_status", target="ticket:EPP-2026-000001")
    assert eo_db.list_audit()["total"] == 2
    assert eo_db.list_audit(action="login")["total"] == 1
    assert eo_db.list_audit(q="000001")["items"][0]["action"] == "ticket_status"
    assert eo_db.audit_actions() == ["login", "ticket_status"]
