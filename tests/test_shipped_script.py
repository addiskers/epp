"""Shipped-script auto-apply. Before this, seeding never touched an existing agent row, so
every deploy left the OLD script talking until someone clicked "Reset to shipped script" —
the fixes for the Gujarati loop and the new opening would never have reached the phone.

Rule: a row still carrying the shipped text it was last given is untouched → it takes the
new shipped text (backed up first). A row an operator edited is left alone (the STALE warning
says if it is behind). Rows from before the tracking column existed are classified once."""
import copy

import pytest

import eo_db
import epp_seeds


@pytest.fixture
def db(fresh_eo_db):
    fresh_eo_db.init()
    return fresh_eo_db


def _newer_seeds(extra="\n\n## NEW RULE\nAlways say 'ji'."):
    seeds = copy.deepcopy(epp_seeds.SEEDS)
    seeds[0]["prompt_template"] = seeds[0]["prompt_template"] + extra
    return seeds


def _versions(db, agent_id):
    return db.list_agent_versions(agent_id)


def test_fresh_rows_are_stamped_and_reported_as_shipped(db):
    for a in db.list_agents():
        assert a["shipped_hash"] and a["shipped_hash"] == db.script_hash(a)
        assert db.agent_script_state(a) == "shipped"


def test_untouched_row_takes_the_next_shipped_script(db, monkeypatch, caplog):
    agent = db.intake_agent()
    monkeypatch.setattr(epp_seeds, "SEEDS", _newer_seeds())
    with caplog.at_level("INFO"):
        db.init()
    now = db.intake_agent()
    assert now["prompt_template"].endswith("Always say 'ji'.")
    assert now["shipped_hash"] == db.script_hash(epp_seeds.SEEDS[0]) != agent["shipped_hash"]
    assert db.agent_script_state(now) == "shipped"
    v = _versions(db, agent["id"])
    assert len(v) == 1 and v[0]["reason"] == "auto_update" and v[0]["shipped_hash"] == agent["shipped_hash"]
    assert "shipped script updated" in caplog.text
    # the full previous text is recoverable
    assert db.get_agent_version(v[0]["id"])["prompt_template"] == agent["prompt_template"]
    db.init()                                                   # idempotent: nothing more happens
    assert len(_versions(db, agent["id"])) == 1


def test_edited_row_is_left_alone_and_flagged_when_behind(db, monkeypatch, caplog):
    agent = db.intake_agent()
    db.update_agent(agent["id"], prompt_template=agent["prompt_template"] + "\n## HOUSE STYLE\nSay ji.")
    assert db.agent_script_state(db.intake_agent()) == "edited"
    monkeypatch.setattr(epp_seeds, "SEEDS", _newer_seeds())
    db.init()
    kept = db.intake_agent()
    assert kept["prompt_template"].endswith("Say ji.") and "NEW RULE" not in kept["prompt_template"]
    assert _versions(db, agent["id"]) == []
    # if the edit drops a required fragment, boot still warns
    db.update_agent(agent["id"], prompt_template=kept["prompt_template"].replace("say_now", "spoken"))
    with caplog.at_level("WARNING"):
        db.init()
    assert "STALE AGENT PROMPT" in caplog.text


def test_reset_restores_the_shipped_text_backs_up_and_re_arms(db):
    agent = db.intake_agent()
    db.update_agent(agent["id"], prompt_template="MY OWN SCRIPT {helpline_name}")
    assert db.refresh_seed_agent("epp_intake", by="admin")
    now = db.intake_agent()
    assert now["prompt_template"] == epp_seeds.INTAKE_PROMPT
    assert db.agent_script_state(now) == "shipped"
    v = _versions(db, agent["id"])
    assert v[0]["reason"] == "reset" and v[0]["replaced_by"] == "admin"
    assert db.get_agent_version(v[0]["id"])["prompt_template"] == "MY OWN SCRIPT {helpline_name}"
    assert db.refresh_seed_agent("no_such_agent") is False


def _pre_v4(db, prompt):
    """Make the intake row look like a database from before shipped_hash existed."""
    agent = db.intake_agent()
    db._exec("UPDATE agents SET prompt_template = ?, shipped_hash = '' WHERE id = ?", (prompt, agent["id"]))
    return agent["id"]


def test_pre_v4_row_equal_to_shipped_is_just_stamped(db):
    aid = _pre_v4(db, epp_seeds.INTAKE_PROMPT)
    db.init()
    a = db.get_agent(aid)
    assert a["shipped_hash"] == db.script_hash(epp_seeds.SEEDS[0]) and _versions(db, aid) == []


def test_pre_v4_row_behind_the_shipped_script_is_updated_with_a_backup(db, caplog):
    """This is the deployed helpline: seeded months ago, never edited, missing every fix since."""
    old = epp_seeds.INTAKE_PROMPT.replace("Thank you for calling", "Welcome to").replace("say_now", "spoken_ticket_id")
    aid = _pre_v4(db, old)
    with caplog.at_level("INFO"):
        db.init()
    a = db.get_agent(aid)
    assert a["prompt_template"] == epp_seeds.INTAKE_PROMPT and db.agent_script_state(a) == "shipped"
    v = _versions(db, aid)
    assert len(v) == 1 and v[0]["reason"] == "bootstrap_stale"
    assert db.get_agent_version(v[0]["id"])["prompt_template"] == old
    assert "shipped script applied" in caplog.text


def test_pre_v4_customised_row_is_frozen_and_never_auto_applied(db, monkeypatch):
    custom = epp_seeds.INTAKE_PROMPT + "\n## HOUSE STYLE\nSay ji."      # current + own additions
    aid = _pre_v4(db, custom)
    db.init()
    a = db.get_agent(aid)
    assert a["prompt_template"] == custom and a["shipped_hash"] == "edited"
    assert db.agent_script_state(a) == "frozen" and _versions(db, aid) == []
    monkeypatch.setattr(epp_seeds, "SEEDS", _newer_seeds())
    db.init()
    assert db.get_agent(aid)["prompt_template"] == custom                # still theirs
    assert db.refresh_seed_agent("epp_intake")                          # reset re-arms
    assert db.agent_script_state(db.get_agent(aid)) == "shipped"


def test_v3_database_gains_the_column(fresh_eo_db):
    """An existing epp.db has an agents table without shipped_hash; init() must ALTER before
    the seeder writes it."""
    db = fresh_eo_db
    conn = db.get_conn()
    conn.executescript(db.SCHEMA.replace("    shipped_hash         TEXT NOT NULL DEFAULT '',      -- hash of the shipped script last applied; '' = pre-v4 row\n", ""))
    assert "shipped_hash" not in {r["name"] for r in conn.execute("PRAGMA table_info(agents)")}
    db.init()
    assert "shipped_hash" in {r["name"] for r in conn.execute("PRAGMA table_info(agents)")}
    assert db.agent_script_state(db.intake_agent()) == "shipped"


def test_settings_round_trip(fresh_eo_db):
    db = fresh_eo_db                                    # no init(): the table is created lazily
    assert db.get_setting("nothing") is None and db.get_setting("nothing", 5) == 5
    db.set_setting("plan", {"minutes": 5000, "rate": 4.5}, updated_by="aditya")
    db.set_setting("flag", False)
    assert db.get_setting("plan") == {"minutes": 5000, "rate": 4.5}
    assert db.get_setting("flag") is False
    db.set_setting("plan", {"minutes": 6000})
    assert db.get_setting("plan") == {"minutes": 6000}
    everything = db.all_settings()
    assert set(everything) == {"plan", "flag"} and everything["plan"]["updated_by"] == ""
    db.delete_setting("plan")
    assert db.get_setting("plan") is None
    db.init()                                           # the real schema does not clobber it
    assert db.get_setting("flag") is False
