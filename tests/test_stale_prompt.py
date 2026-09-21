"""A deployed database keeps the agent prompt it was seeded with; seeding never overwrites an
operator's text. After a prompt fix ships, the old text keeps speaking until someone resets it —
so boot must say so, loudly, naming the agent and the fix it is missing."""
import logging

import epp_seeds


def test_fresh_seed_is_not_stale(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    assert db.stale_agent_reasons(db.intake_agent()) == []
    assert db.warn_about_stale_agents() == []


def test_old_prompt_text_is_flagged_with_a_reason(fresh_eo_db, caplog):
    db = fresh_eo_db
    db.init()
    agent = db.intake_agent()
    old = agent["prompt_template"].replace("say_now", "spoken_ticket_id")   # what the previous build shipped
    old = old.replace("## CORRECTIONS", "## OLD SECTION")
    db.update_agent(agent["id"], prompt_template=old)
    reasons = db.stale_agent_reasons(db.intake_agent())
    assert any("invent" in r.lower() or "say_now" in r for r in reasons)
    assert any("correction" in r.lower() for r in reasons)
    with caplog.at_level(logging.WARNING):
        stale = db.warn_about_stale_agents()
    assert [a["slug"] for a in stale] == ["epp_intake"]
    assert "STALE AGENT PROMPT" in caplog.text and "Reset to shipped script" in caplog.text


def test_an_operators_own_edit_that_keeps_the_fixes_is_not_stale(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    agent = db.intake_agent()
    db.update_agent(agent["id"], prompt_template=agent["prompt_template"] + "\n\n## HOUSE STYLE\nSay 'ji' often.")
    assert db.stale_agent_reasons(db.intake_agent()) == []


def test_every_shipped_seed_passes_its_own_check():
    import eo_db
    for seed in epp_seeds.SEEDS:
        assert eo_db.stale_agent_reasons(seed) == [], seed["slug"]
