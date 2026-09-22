"""The Subscription page: minutes bought vs used (per phone call, rounded up), ₹ at the
configured rate, the period and the licence — .env defaults with a stored override that
only the service provider's usernames may change."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import eo_auth
import subscription

IST = ZoneInfo("Asia/Kolkata")
PLAN = {"name": "Starter", "minutes": 100, "start": "2026-09-01", "end": "2026-11-30",
        "licence_valid_till": "2026-12-31", "rate_inr_per_min": 4.5}
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=IST)


def _m(source, started, secs):
    return {"source": source, "started_at": started, "duration_seconds": secs}


def test_usage_counts_phone_calls_rounded_up_inside_the_period():
    metas = [
        _m("plivo_inbound", "2026-09-22T04:30:00+00:00", 61),     # 10:00 IST → 2 min
        _m("plivo_campaign", "2026-09-22T05:00:00+00:00", 60),    # 1 min
        _m("plivo", "2026-09-22T05:10:00+00:00", 0),              # a 0 s "Call me" test: a call, 0 min
        _m("browser", "2026-09-22T05:20:00+00:00", 600),          # browser mic test: free
        _m("plivo_inbound", "2026-08-31T18:29:00+00:00", 600),    # 23:59 IST on 31 Aug → before the start
        _m("plivo_inbound", "2026-08-31T18:31:00+00:00", 120),    # 00:01 IST on 1 Sep → in
        _m("plivo_inbound", "2026-12-01T00:00:00+00:00", 120),    # after the end
        _m("plivo_inbound", "garbage", 120),                      # unreadable → skipped
    ]
    u = subscription.usage(PLAN, metas, now=NOW)
    assert u["calls"] == 4 and u["seconds"] == 241
    assert u["minutes_used"] == 5 and u["minutes_left"] == 95 and u["pct_used"] == 5
    assert u["amount_inr"] == 22.5 and u["rate_inr_per_min"] == 4.5
    assert u["period"]["days_left"] == 69 and u["period"]["active"] is True
    assert u["licence"]["status"] == "valid" and u["licence"]["days_left"] == 100


def test_licence_period_and_cap_states():
    assert subscription.usage(dict(PLAN, licence_valid_till="2026-09-25"), [], NOW)["licence"] == \
        {"valid_till": "2026-09-25", "days_left": 3, "status": "expiring"}
    assert subscription.usage(dict(PLAN, licence_valid_till="2026-09-21"), [], NOW)["licence"]["status"] == "expired"
    assert subscription.usage(dict(PLAN, licence_valid_till=""), [], NOW)["licence"] == \
        {"valid_till": "", "days_left": None, "status": "unset"}
    assert subscription.usage(dict(PLAN, start="2026-10-01"), [], NOW)["period"]["active"] is False
    no_cap = subscription.usage(dict(PLAN, minutes=0), [_m("plivo", "2026-09-22T05:00:00+00:00", 30)], NOW)
    assert no_cap["minutes_used"] == 1 and no_cap["minutes_left"] is None and no_cap["pct_used"] is None
    assert no_cap["amount_inr"] == 4.5
    over = subscription.usage(dict(PLAN, minutes=1), [_m("plivo", "2026-09-22T05:00:00+00:00", 500)], NOW)
    assert over["minutes_left"] == 0 and over["pct_used"] == 100


def test_validate_rejects_bad_values_and_cleans_good_ones():
    for bad in ({"minutes": "abc"}, {"minutes": "10.5"}, {"minutes": "-1"},
                {"start": "2026-10-01", "end": "2026-09-01"}, {"licence_valid_till": "31/12/2026"},
                {"rate_inr_per_min": "-1"}, {"rate_inr_per_min": "four"}):
        with pytest.raises(ValueError):
            subscription.validate(bad)
    clean = subscription.validate({"name": " Starter ", "minutes": "5000", "start": "2026-09-01",
                                   "rate_inr_per_min": "4.50", "end": "", "licence_valid_till": None})
    assert clean == {"name": "Starter", "minutes": 5000, "start": "2026-09-01", "rate_inr_per_min": 4.5}


def test_plan_merges_env_defaults_and_the_stored_override(fresh_eo_db, monkeypatch):
    monkeypatch.setenv("EPP_PLAN_NAME", "Env plan")
    monkeypatch.setenv("EPP_PLAN_MINUTES", "1000")
    monkeypatch.setenv("EPP_RATE_INR_PER_MIN", "3")
    for k in ("EPP_PLAN_START", "EPP_PLAN_END", "EPP_LICENCE_VALID_TILL"):
        monkeypatch.delenv(k, raising=False)
    p = subscription.plan()
    assert p["name"] == "Env plan" and p["minutes"] == 1000 and p["rate_inr_per_min"] == 3.0 and p["source"] == "env"
    fresh_eo_db.set_setting("plan", {"minutes": 5000, "end": "2026-12-31", "bogus": 1})
    p = subscription.plan()
    assert p["minutes"] == 5000 and p["name"] == "Env plan" and p["end"] == "2026-12-31"
    assert p["source"] == "db" and "bogus" not in p


# ------------------------------------------------------------------ the API
ADMIN_PASS = "secret-pass-1"


@pytest.fixture
def client(fresh_eo_db, monkeypatch):
    monkeypatch.setenv("EPP_ADMIN_USER", "admin")
    monkeypatch.setenv("EPP_ADMIN_PASS", ADMIN_PASS)
    monkeypatch.setenv("EPP_ANALYSIS_ENABLED", "false")
    monkeypatch.delenv("EPP_SUPERADMIN_USERS", raising=False)
    eo_auth.reset_throttle()
    import main
    main.invalidate_ctx_cache()
    with TestClient(main.app) as c:
        yield c
    eo_auth.reset_throttle()


def _login(c, username, password):
    r = c.post("/api/epp/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_admins_read_only_the_service_provider_edits(client, fresh_eo_db, monkeypatch):
    h = _login(client, "admin", ADMIN_PASS)
    r = client.get("/api/epp/subscription", headers=h)
    assert r.status_code == 200 and r.json()["can_edit"] is False and "usage" in r.json()
    assert client.put("/api/epp/subscription", headers=h, json={"minutes": 5000}).status_code == 403

    monkeypatch.setenv("EPP_SUPERADMIN_USERS", "Admin, someone-else")
    assert client.get("/api/epp/subscription", headers=h).json()["can_edit"] is True
    r = client.put("/api/epp/subscription", headers=h, json={"minutes": 5000, "rate_inr_per_min": "4.5", "name": "Starter"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["plan"]["minutes"] == 5000 and d["source"] == "db" and d["usage"]["minutes_included"] == 5000
    assert fresh_eo_db.get_setting("plan")["minutes"] == 5000
    assert client.put("/api/epp/subscription", headers=h, json={"minutes": "x"}).status_code == 400
    assert client.put("/api/epp/subscription", headers=h, json={}).status_code == 400
    assert client.put("/api/epp/subscription", headers=h, json={"reset": True}).json()["source"] == "env"
    actions = [a["action"] for a in fresh_eo_db.list_audit()["items"]]
    assert "subscription_updated" in actions and "subscription_reset" in actions


def test_department_typos_are_named_at_boot(fresh_eo_db, caplog):
    db = fresh_eo_db
    db.init()
    mnt = next(d for d in db.list_departments() if d["code"] == "MNT")
    db.update_department(mnt["id"], name="Maintainance")
    db.create_department("Legal", "LEGAL", 99)                 # a genuinely new one is not flagged
    typos = db.department_typos()
    assert [(d["name"], s) for d, s in typos] == [("Maintainance", "Maintenance")]
    with caplog.at_level("WARNING"):
        db.init()
    assert "did you mean 'Maintenance'" in caplog.text
