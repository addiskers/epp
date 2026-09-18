"""Campaign agents: three seeded scripts, outbound triggers, placeholders, per-type tools."""
import agent_tools
import epp_seeds
import prompt_render as pr


def test_three_agents_are_seeded_with_outbound_triggers(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    slugs = sorted(a["slug"] for a in db.list_agents())
    assert slugs == ["epp_announcement", "epp_followup", "epp_intake"]
    intake = db.get_agent_by_slug("epp_intake")
    assert "calling from" in intake["outbound_trigger_template"]
    assert db.list_agents()[0]["slug"] == "epp_intake"                  # first row stays the intake agent
    assert epp_seeds.AGENT_FOR_TYPE == {"intake": "epp_intake", "followup": "epp_followup",
                                        "announcement": "epp_announcement"}


def test_outbound_trigger_survives_a_reset_and_an_older_db(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    a = db.get_agent_by_slug("epp_intake")
    db.update_agent(a["id"], outbound_trigger_template="custom")
    assert db.get_agent_by_slug("epp_intake")["outbound_trigger_template"] == "custom"
    db.refresh_seed_agent("epp_intake")
    assert db.get_agent_by_slug("epp_intake")["outbound_trigger_template"] == epp_seeds.INTAKE_OUTBOUND_TRIGGER
    # a v2 database (agents table without the column) is migrated on init()
    conn = db.get_conn()
    conn.execute("DROP TABLE agents")
    conn.execute("CREATE TABLE agents (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, "
                 "description TEXT NOT NULL DEFAULT '', prompt_template TEXT NOT NULL, trigger_template TEXT NOT NULL DEFAULT '', "
                 "voice_name TEXT NOT NULL DEFAULT '', speech_language_code TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, "
                 "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()
    db.init()
    assert "outbound_trigger_template" in db.get_agent_by_slug("epp_intake")


def test_every_seed_uses_only_known_placeholders():
    for seed in epp_seeds.SEEDS:
        for key in ("prompt_template", "trigger_template", "outbound_trigger_template"):
            assert pr.validate_template(seed.get(key, "")) == [], (seed["slug"], key)


def test_followup_prompt_renders_ticket_facts_without_gaps():
    seed = next(s for s in epp_seeds.SEEDS if s["slug"] == "epp_followup")
    r = pr.render_prompt(seed, outbound=True, extra={
        "caller_name": "Rahul", "ticket_id": "EPP-2026-000001",
        "ticket_id_spoken": "E, P, P — two zero two six — zero zero zero zero zero one",
        "ticket_status": "Under Review", "ticket_category": "Salary", "ticket_department": "Finance",
        "ticket_created_spoken": "18 September 2026"})
    assert r["missing"] == []
    assert "Under Review" in r["system_instruction"] and "zero zero zero zero zero one" in r["system_instruction"]
    assert "Rahul" in r["trigger"]
    for must in ("Never promise an outcome", "SPEAK TO A PERSON", "record_outcome", "has_update"):
        assert must in r["system_instruction"], must


def test_announcement_prompt_carries_the_message():
    seed = next(s for s in epp_seeds.SEEDS if s["slug"] == "epp_announcement")
    r = pr.render_prompt(seed, outbound=True, extra={"campaign_message": "The Halol plant is closed on Monday.",
                                                    "campaign_name": "Holiday notice"})
    assert r["missing"] == [] and "closed on Monday" in r["system_instruction"]
    for must in ("Never promise an outcome", "SPEAK TO A PERSON", "acknowledged", "create_ticket"):
        assert must in r["system_instruction"], must


def test_inbound_render_ignores_the_outbound_trigger():
    seed = next(s for s in epp_seeds.SEEDS if s["slug"] == "epp_intake")
    assert "THE OPENING" in pr.render_prompt(seed)["trigger"]
    assert "calling from" in pr.render_prompt(seed, outbound=True)["trigger"]
    # an agent with no outbound trigger falls back to its inbound one
    assert pr.render_prompt({"prompt_template": "x", "trigger_template": "[in]"}, outbound=True)["trigger"] == "[in]"


def test_tools_per_campaign_type():
    assert [t["name"] for t in agent_tools.build_tools()] == ["create_ticket", "lookup_ticket", "end_call"]
    out = agent_tools.build_tools(campaign_type="followup")
    assert [t["name"] for t in out] == ["create_ticket", "lookup_ticket", "record_outcome", "end_call"]
    enum = out[2]["parameters"]["properties"]["outcome_status"]["enum"]
    assert enum == ["confirmed", "has_update", "callback", "not_reachable", "wrong_number"]
    assert agent_tools.record_outcome_declaration("intake")["parameters"]["properties"]["outcome_status"]["enum"][0] == "no_concern"
    assert agent_tools.record_outcome_declaration("announcement")["parameters"]["properties"]["outcome_status"]["enum"][0] == "acknowledged"
    assert agent_tools.record_outcome_declaration("bogus")["parameters"]["properties"]["outcome_status"]["enum"][0] == "no_concern"
    assert "record_outcome" in agent_tools.COMPLETION_TOOLS
