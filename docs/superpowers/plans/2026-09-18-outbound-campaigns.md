# Outbound Campaigns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin upload or pick numbers and have the helpline agent call them, in three modes: intake round, ticket follow-up, announcement.

**Architecture:** Port the 7x campaign dialer (`campaign_runner`, calling-window helpers, contact importer) from the sibling `7x/` source and re-point it at a `campaign_type` column. Each type selects an agent row, a rendered prompt, and a tool set; the existing Plivo bridge, recorder and ticket service are reused unchanged. Admin-only API under `/api/epp`, three new SPA pages.

**Tech Stack:** FastAPI, SQLite (sync sqlite3 under a lock), Gemini Live, Plivo, React 18 + Vite, pytest.

**Spec:** `docs/superpowers/specs/2026-09-18-outbound-campaigns-design.md`

## Global Constraints

- App root is the repo root (`main.py` at top level). Tests run with `python -m pytest tests -q` from the repo root; `tests/conftest.py` isolates `DATA_DIR`.
- Never add a Claude co-author trailer to commits. Commit messages are plain.
- Env vars for the campaign runner use the `EPP_` prefix (`EPP_CAMPAIGN_RUNNER_ENABLED`, `EPP_CAMPAIGN_MAX_PER_TICK`, `EPP_CAMPAIGN_MAX_CONCURRENT`, `EPP_CAMPAIGN_NOANSWER_SECONDS`, `EPP_CAMPAIGN_DIAL_TIMEOUT`, `EPP_CAMPAIGN_POLL_INTERVAL`, `EPP_MAX_ACTIVE_CAMPAIGNS`, `EPP_CALL_WINDOW_START`, `EPP_CALL_WINDOW_END`, `MAX_LIVE_CALLS`).
- Campaign types: exactly `intake`, `followup`, `announcement`.
- Outcome vocabularies (spec): intake `no_concern|callback|not_reachable|wrong_number`; followup `confirmed|has_update|callback|not_reachable|wrong_number`; announcement `acknowledged|declined|callback|not_reachable|wrong_number`.
- `callback` and `not_reachable` retry; `wrong_number` and everything else close the contact as `done`.
- Campaigns are admin-only. Every create, cancel, Call now, import, delete and toggle writes an audit row.
- Source for ported code: `../7x/gemini-live-api-examples/gemini-live-genai-python-sdk/` (referred to below as `7x/`).

---

## File map

| File | Responsibility |
|---|---|
| `eo_db.py` (modify) | schema v3: `contacts`, `campaigns`, `campaign_contacts`; query functions for each |
| `calling_window.py` (create) | pure calling-hours math, ported from `7x/callbacks.py` (window functions only) |
| `contacts_import.py` (create) | xlsx/csv parsing + phone normalisation, ported from `7x/eo_import.py` minus wedding columns |
| `epp_seeds.py` (modify) | outbound trigger for `epp_intake`; two new agents `epp_followup`, `epp_announcement`; `OUTCOMES` per type |
| `prompt_render.py` (modify) | new placeholders for follow-up and announcement calls |
| `agent_tools.py` (modify) | `record_outcome` declaration per campaign type; `build_tools(..., campaign_type=)` |
| `campaigns.py` (create) | campaign service: create/validate, `record_outcome` handler, follow-up note on the ticket, context for a campaign call |
| `campaign_runner.py` (create) | ported dial loop |
| `store.py` (modify) | `find_campaign_call(campaign_contact_id, since_iso)` |
| `recorder.py` (modify) | `campaign_id`/`campaign_contact_id` on the record; capture `record_outcome` |
| `dialer.py` (modify) | `campaign_id`, `campaign_contact_id` on the answer URL |
| `main.py` (modify) | campaign call context in `/plivo/answer`; `tool_mapping` with `record_outcome`; runner startup; `live_room()` |
| `eo_api.py` (modify) | contacts, campaigns, scheduler routes |
| `admin/src/pages/Contacts.jsx`, `Campaigns.jsx`, `CampaignDetail.jsx`, `CreateCampaign.jsx`, `Scheduler.jsx` (create) | pages |
| `admin/src/components/ContactUpload.jsx`, `ContactsTable.jsx`, `RemarkCell.jsx` (create/restore) | shared pieces |
| `admin/src/App.jsx`, `components/Layout.jsx` (modify) | routes + nav |
| `.env.example`, `README.md`, `DEPLOY.md` (modify) | config + docs |
| `tests/test_campaign_db.py`, `test_calling_window.py`, `test_contacts_import.py`, `test_campaign_agents.py`, `test_campaigns.py`, `test_campaign_runner.py`, `test_campaign_api.py` (create) | tests |

---

### Task 1: Schema v3 and campaign tables

**Files:**
- Modify: `eo_db.py` (SCHEMA, `SCHEMA_VERSION`, new query functions appended before the audit section)
- Test: `tests/test_campaign_db.py`

**Interfaces:**
- Produces: `CAMPAIGN_TYPES`, `add_contact(name, phone, caller_type="", notes="", source="manual", status="valid", created_by=None) -> (id, created)`, `bulk_upsert_contacts(rows, source="upload", created_by=None) -> (added, updated)`, `list_contacts(q=None, caller_type=None, status=None, limit=25, offset=0) -> {"items","total"}`, `get_contacts_by_ids(ids)`, `delete_contacts(ids) -> int`, `contact_by_phone(phone)`, `create_campaign(name, campaign_type, start_at, created_by, *, message="", status="scheduled", callback_delay_hours=4, callback_max_per_day=3, callback_days=1, call_start_min=540, call_end_min=1260) -> id`, `add_campaign_contacts(campaign_id, rows) -> int` (rows: dicts with phone, name, contact_id, ticket_id), `get_campaign(id)`, `get_campaign_full(id)` (+progress), `list_campaigns(q, status, limit, offset)`, `active_campaigns()`, `live_campaigns()`, `promote_due_campaigns(now_iso) -> int`, `set_campaign_status(id, status)`, `cancel_campaign(id) -> bool`, `campaign_progress(id) -> {status: n}`, `cc_pending_due(campaign_id, now_iso, limit)`, `cc_by_status(campaign_id, status)`, `list_campaign_contacts(campaign_id, status=None, q=None, limit=500, offset=0)`, `cc_open_count(campaign_id)`, `get_campaign_contact(cc_id)`, `cc_update(cc_id, **fields)`, `cc_upcoming(limit=200)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_campaign_db.py
"""eo_db.py — contacts pool, campaigns, campaign_contacts (schema v3)."""
from datetime import datetime, timezone

PAST = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()


def test_schema_v3_has_campaign_tables(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    tables = {r[0] for r in db.get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"contacts", "campaigns", "campaign_contacts"} <= tables
    assert db.schema_version() == 3
    assert db.CAMPAIGN_TYPES == ("intake", "followup", "announcement")


def test_contacts_upsert_by_phone(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    cid, created = db.add_contact("Rahul", "+919876543210", caller_type="employee")
    assert created is True
    cid2, created2 = db.add_contact("", "+919876543210", notes="second")
    assert (cid2, created2) == (cid, False)
    row = db.get_contacts_by_ids([cid])[0]
    assert row["name"] == "Rahul" and row["notes"] == "second"        # blank name never wipes
    added, updated = db.bulk_upsert_contacts([("Meera", "+919000000001", "valid", {"caller_type": "customer"}),
                                              ("", "+919876543210", "valid", {})])
    assert (added, updated) == (1, 1)
    assert db.list_contacts(q="meera")["total"] == 1
    assert db.list_contacts(caller_type="employee")["total"] == 1
    assert db.contact_by_phone("+919000000001")["name"] == "Meera"
    assert db.delete_contacts([cid]) == 1
    assert db.list_contacts()["total"] == 1


def test_campaign_lifecycle_and_progress(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    cid = db.create_campaign("Round 1", "intake", PAST, created_by=1)
    n = db.add_campaign_contacts(cid, [{"phone": "+919000000001", "name": "A", "contact_id": None, "ticket_id": None},
                                       {"phone": "+919000000002", "name": "B", "contact_id": None, "ticket_id": "EPP-2026-000001"}])
    assert n == 2
    c = db.get_campaign_full(cid)
    assert c["campaign_type"] == "intake" and c["contact_count"] == 2 and c["status"] == "scheduled"
    assert c["progress"] == {"pending": 2}
    assert db.promote_due_campaigns(datetime.now(timezone.utc).isoformat()) == 1
    assert [x["id"] for x in db.live_campaigns()] == [cid]
    due = db.cc_pending_due(cid, datetime.now(timezone.utc).isoformat(), 10)
    assert len(due) == 2 and due[1]["ticket_id"] == "EPP-2026-000001"
    db.cc_update(due[0]["id"], call_status="calling", attempts=1)
    assert [x["id"] for x in db.cc_by_status(cid, "calling")] == [due[0]["id"]]
    assert db.cc_open_count(cid) == 2
    db.cc_update(due[0]["id"], call_status="done", outcome="no_concern")
    db.cc_update(due[1]["id"], call_status="failed")
    assert db.cc_open_count(cid) == 0
    assert db.campaign_progress(cid) == {"done": 1, "failed": 1}
    assert db.list_campaign_contacts(cid, status="done")["total"] == 1
    assert db.list_campaign_contacts(cid, q="+919000000002")["items"][0]["name"] == "B"
    assert db.cancel_campaign(cid) is True
    assert db.cancel_campaign(cid) is False                              # already cancelled
    assert db.list_campaigns(status="cancelled")["total"] == 1


def test_cc_upcoming_lists_attempted_contacts_open_first(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    cid = db.create_campaign("R", "announcement", PAST, created_by=1, message="hi", status="live")
    db.add_campaign_contacts(cid, [{"phone": "+919000000001", "name": "A"}, {"phone": "+919000000002", "name": "B"}])
    a, b = db.cc_pending_due(cid, datetime.now(timezone.utc).isoformat(), 10)
    db.cc_update(a["id"], attempts=1, call_status="done", outcome="acknowledged")
    db.cc_update(b["id"], attempts=1, call_status="pending", next_attempt_at="2030-01-01T00:00:00+00:00")
    rows = db.cc_upcoming()["items"]
    assert [r["id"] for r in rows] == [b["id"], a["id"]]
    assert rows[0]["campaign_name"] == "R" and rows[0]["campaign_type"] == "announcement"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_campaign_db.py -q`
Expected: FAIL (`CAMPAIGN_TYPES` missing, tables missing).

- [ ] **Step 3: Implement**

In `eo_db.py`, set `SCHEMA_VERSION = 3`, add `CAMPAIGN_TYPES = ("intake", "followup", "announcement")` and `CAMPAIGN_STATUSES = ("scheduled", "live", "completed", "cancelled")` next to `ROLES`, and append to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL UNIQUE,                 -- E.164
    caller_type TEXT NOT NULL DEFAULT '',             -- customer | vendor | employee | ''
    notes       TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'manual',       -- upload | manual
    status      TEXT NOT NULL DEFAULT 'valid',        -- valid | invalid
    created_by  INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name);

CREATE TABLE IF NOT EXISTS campaigns (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    campaign_type        TEXT NOT NULL,               -- intake | followup | announcement
    message              TEXT NOT NULL DEFAULT '',    -- announcement text
    status               TEXT NOT NULL DEFAULT 'scheduled',
    start_at             TEXT NOT NULL,               -- ISO-8601 UTC
    created_by           INTEGER,
    contact_count        INTEGER NOT NULL DEFAULT 0,
    callback_delay_hours INTEGER NOT NULL DEFAULT 4,
    callback_max_per_day INTEGER NOT NULL DEFAULT 3,
    callback_days        INTEGER NOT NULL DEFAULT 1,
    call_start_min       INTEGER NOT NULL DEFAULT 540,
    call_end_min         INTEGER NOT NULL DEFAULT 1260,
    done_count           INTEGER NOT NULL DEFAULT 0,
    failed_count         INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);

CREATE TABLE IF NOT EXISTS campaign_contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id      INTEGER,
    ticket_id       TEXT,
    phone           TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    call_status     TEXT NOT NULL DEFAULT 'pending',  -- pending|calling|done|failed|cancelled
    attempts        INTEGER NOT NULL DEFAULT 0,
    day_attempts    INTEGER NOT NULL DEFAULT 0,
    day_key         TEXT,
    next_attempt_at TEXT,
    last_call_id    TEXT,
    last_attempt_at TEXT,
    last_error      TEXT,
    outcome         TEXT,
    remark          TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cc_campaign ON campaign_contacts(campaign_id);
CREATE INDEX IF NOT EXISTS idx_cc_due ON campaign_contacts(call_status, next_attempt_at);
```

Append the functions (adapt from the pre-conversion `eo_db.py` in `7x/`, dropping `wedding_id`/`created_by` scoping):

```python
# ---------------------------------------------------------------------------------------
# Contacts pool
# ---------------------------------------------------------------------------------------
def add_contact(name, phone, caller_type="", notes="", source="manual", status="valid", created_by=None):
    """Upsert by phone. Returns (id, created). A blank name/notes never wipes a stored one."""
    now = _now()
    existing = _one("SELECT id FROM contacts WHERE phone = ?", (phone,))
    if existing:
        _exec("UPDATE contacts SET name = COALESCE(NULLIF(?, ''), name), "
              "caller_type = COALESCE(NULLIF(?, ''), caller_type), notes = COALESCE(NULLIF(?, ''), notes), "
              "status = ?, updated_at = ? WHERE id = ?",
              (name or "", caller_type or "", notes or "", status, now, existing["id"]))
        return existing["id"], False
    cid = _exec("INSERT INTO contacts (name, phone, caller_type, notes, source, status, created_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (name or "", phone, caller_type or "", notes or "", source, status, created_by, now, now))
    return cid, True


def bulk_upsert_contacts(rows, source="upload", created_by=None):
    """rows: (name, phone, status, extra_dict). Returns (added, updated)."""
    rows = list(rows)
    if not rows:
        return 0, 0
    now = _now()
    conn = get_conn()
    with _lock:
        existing = {r["phone"] for r in conn.execute("SELECT phone FROM contacts").fetchall()}
        conn.executemany(
            "INSERT INTO contacts (name, phone, caller_type, notes, source, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET "
            "name = COALESCE(NULLIF(excluded.name, ''), contacts.name), "
            "caller_type = COALESCE(NULLIF(excluded.caller_type, ''), contacts.caller_type), "
            "notes = COALESCE(NULLIF(excluded.notes, ''), contacts.notes), "
            "status = excluded.status, updated_at = excluded.updated_at",
            [(nm or "", ph, (x or {}).get("caller_type") or "", (x or {}).get("notes") or "",
              source, st, created_by, now, now) for (nm, ph, st, x) in rows])
        conn.commit()
    added = sum(1 for r in rows if r[1] not in existing)
    return added, len(rows) - added


def list_contacts(q=None, caller_type=None, status=None, limit=25, offset=0):
    where, params = [], []
    if q:
        where.append("(name LIKE ? OR phone LIKE ? OR notes LIKE ?)")
        params += [f"%{q}%"] * 3
    if caller_type:
        where.append("caller_type = ?"); params.append(caller_type)
    if status:
        where.append("status = ?"); params.append(status)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    total = _one(f"SELECT COUNT(*) c FROM contacts {wsql}", tuple(params))["c"]
    rows = _rows(f"SELECT * FROM contacts {wsql} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                 tuple(params) + (int(limit), int(offset)))
    return {"items": rows, "total": int(total)}


def get_contacts_by_ids(ids):
    ids = [int(i) for i in ids if i]
    if not ids:
        return []
    return _rows(f"SELECT * FROM contacts WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))


def delete_contacts(ids) -> int:
    ids = [int(i) for i in ids if i]
    if not ids:
        return 0
    conn = get_conn()
    with _lock:
        cur = conn.execute(f"DELETE FROM contacts WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
        conn.commit()
        return cur.rowcount


def contact_by_phone(phone):
    return _one("SELECT * FROM contacts WHERE phone = ?", (str(phone or ""),))


# ---------------------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------------------
def create_campaign(name, campaign_type, start_at, created_by, *, message="", status="scheduled",
                    callback_delay_hours=4, callback_max_per_day=3, callback_days=1,
                    call_start_min=540, call_end_min=1260) -> int:
    now = _now()
    return _exec(
        "INSERT INTO campaigns (name, campaign_type, message, status, start_at, created_by, contact_count, "
        "callback_delay_hours, callback_max_per_day, callback_days, call_start_min, call_end_min, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,0,?,?,?,?,?,?,?)",
        (name, campaign_type, message or "", status, start_at, created_by, int(callback_delay_hours),
         int(callback_max_per_day), int(callback_days), int(call_start_min), int(call_end_min), now, now))


def add_campaign_contacts(campaign_id, rows) -> int:
    rows = list(rows)
    now = _now()
    conn = get_conn()
    with _lock:
        conn.executemany(
            "INSERT INTO campaign_contacts (campaign_id, contact_id, ticket_id, phone, name, call_status, attempts, "
            "day_attempts, created_at, updated_at) VALUES (?,?,?,?,?,'pending',0,0,?,?)",
            [(int(campaign_id), r.get("contact_id"), r.get("ticket_id"), r["phone"], r.get("name") or "", now, now)
             for r in rows])
        n = conn.execute("SELECT COUNT(*) FROM campaign_contacts WHERE campaign_id = ?", (int(campaign_id),)).fetchone()[0]
        conn.execute("UPDATE campaigns SET contact_count = ?, updated_at = ? WHERE id = ?", (n, now, int(campaign_id)))
        conn.commit()
    return int(n)


def get_campaign(campaign_id):
    if campaign_id in (None, ""):
        return None
    return _one("SELECT * FROM campaigns WHERE id = ?", (int(campaign_id),))


def campaign_progress(campaign_id) -> dict:
    rows = _rows("SELECT call_status, COUNT(*) n FROM campaign_contacts WHERE campaign_id = ? GROUP BY call_status",
                 (int(campaign_id),))
    return {r["call_status"]: int(r["n"]) for r in rows}


def campaign_outcomes(campaign_id) -> dict:
    rows = _rows("SELECT outcome, COUNT(*) n FROM campaign_contacts WHERE campaign_id = ? AND outcome IS NOT NULL "
                 "GROUP BY outcome", (int(campaign_id),))
    return {r["outcome"]: int(r["n"]) for r in rows}


def get_campaign_full(campaign_id):
    c = get_campaign(campaign_id)
    if not c:
        return None
    c["progress"] = campaign_progress(campaign_id)
    c["outcomes"] = campaign_outcomes(campaign_id)
    c["tickets_created"] = int(_one(
        "SELECT COUNT(*) c FROM tickets WHERE source = 'campaign' AND call_id IN "
        "(SELECT last_call_id FROM campaign_contacts WHERE campaign_id = ? AND last_call_id IS NOT NULL)",
        (int(campaign_id),))["c"])
    return c


def list_campaigns(q=None, status=None, limit=50, offset=0):
    where, params = [], []
    if q:
        where.append("name LIKE ?"); params.append(f"%{q}%")
    if status:
        where.append("status = ?"); params.append(status)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    total = _one(f"SELECT COUNT(*) c FROM campaigns {wsql}", tuple(params))["c"]
    rows = _rows(f"SELECT * FROM campaigns {wsql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                 tuple(params) + (int(limit), int(offset)))
    return {"items": rows, "total": int(total)}


def active_campaigns():
    return _rows("SELECT * FROM campaigns WHERE status IN ('scheduled','live') ORDER BY start_at, id")


def live_campaigns():
    return _rows("SELECT * FROM campaigns WHERE status = 'live' ORDER BY created_at ASC")


def promote_due_campaigns(now_iso) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute("UPDATE campaigns SET status = 'live', updated_at = ? WHERE status = 'scheduled' AND start_at <= ?",
                           (_now(), now_iso))
        conn.commit()
        return cur.rowcount


def set_campaign_status(campaign_id, status):
    _exec("UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), int(campaign_id)))


def cancel_campaign(campaign_id) -> bool:
    conn = get_conn()
    with _lock:
        cur = conn.execute("UPDATE campaigns SET status = 'cancelled', updated_at = ? WHERE id = ? "
                           "AND status IN ('scheduled','live')", (_now(), int(campaign_id)))
        conn.execute("UPDATE campaign_contacts SET call_status = 'cancelled', next_attempt_at = NULL, updated_at = ? "
                     "WHERE campaign_id = ? AND call_status = 'pending'", (_now(), int(campaign_id)))
        conn.commit()
        return cur.rowcount > 0


# Campaign contacts
def cc_pending_due(campaign_id, now_iso, limit):
    return _rows("SELECT * FROM campaign_contacts WHERE campaign_id = ? AND call_status = 'pending' "
                 "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id ASC LIMIT ?",
                 (int(campaign_id), now_iso, int(limit)))


def cc_by_status(campaign_id, status):
    return _rows("SELECT * FROM campaign_contacts WHERE campaign_id = ? AND call_status = ? ORDER BY id ASC",
                 (int(campaign_id), status))


def list_campaign_contacts(campaign_id, status=None, q=None, limit=500, offset=0):
    where, params = ["campaign_id = ?"], [int(campaign_id)]
    if status:
        where.append("call_status = ?"); params.append(status)
    if q:
        where.append("(name LIKE ? OR phone LIKE ? OR ticket_id LIKE ?)"); params += [f"%{q}%"] * 3
    wsql = "WHERE " + " AND ".join(where)
    total = _one(f"SELECT COUNT(*) c FROM campaign_contacts {wsql}", tuple(params))["c"]
    rows = _rows(f"SELECT * FROM campaign_contacts {wsql} ORDER BY id ASC LIMIT ? OFFSET ?",
                 tuple(params) + (int(limit), int(offset)))
    return {"items": rows, "total": int(total)}


def cc_open_count(campaign_id) -> int:
    r = _one("SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id = ? AND call_status IN ('pending','calling')",
             (int(campaign_id),))
    return int(r["c"]) if r else 0


def get_campaign_contact(cc_id):
    return _one("SELECT * FROM campaign_contacts WHERE id = ?", (int(cc_id),))


def cc_update(cc_id, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    _exec(f"UPDATE campaign_contacts SET {cols} WHERE id = ?", tuple(fields.values()) + (int(cc_id),))


def cc_upcoming(limit=200):
    """Contacts dialled at least once: open retries first (soonest next attempt), then history."""
    base = ("FROM campaign_contacts cc JOIN campaigns c ON c.id = cc.campaign_id WHERE cc.attempts > 0")
    total = _one(f"SELECT COUNT(*) n {base}")["n"]
    rows = _rows(
        "SELECT cc.*, c.name AS campaign_name, c.campaign_type, c.status AS campaign_status, "
        "c.start_at AS campaign_start_at, c.callback_max_per_day AS campaign_max_per_day, "
        "c.callback_days AS campaign_days, c.call_start_min AS campaign_call_start_min, "
        f"c.call_end_min AS campaign_call_end_min {base} "
        "ORDER BY (cc.call_status IN ('pending','calling')) DESC, (cc.next_attempt_at IS NULL) DESC, "
        "cc.next_attempt_at ASC, cc.last_attempt_at DESC, cc.id ASC LIMIT ?", (int(limit),))
    return {"items": rows, "total": int(total)}
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_campaign_db.py tests/test_eo_db.py -q`
Expected: all pass (the existing `test_fresh_schema...` asserts `schema_version() == SCHEMA_VERSION`, still true).

- [ ] **Step 5: Commit**

```bash
git add eo_db.py tests/test_campaign_db.py
git commit -m "Campaigns: contacts pool, campaigns and campaign_contacts tables (schema v3)"
```

---

### Task 2: Calling-window helpers

**Files:**
- Create: `calling_window.py`
- Test: `tests/test_calling_window.py`

**Interfaces:**
- Produces: `hhmm_to_min(s, default=0)`, `now_ist_min()`, `in_call_window(start_min, end_min, now_min=None)`, `global_window() -> (start, end)`, `campaign_window(campaign) -> (start, end)`, `tz()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_calling_window.py
import calling_window as cw


def test_in_call_window_normal_window():
    assert cw.in_call_window(540, 1260, now_min=600) is True
    assert cw.in_call_window(540, 1260, now_min=300) is False
    assert cw.in_call_window(540, 1260, now_min=1260) is False   # end is exclusive


def test_in_call_window_overnight_wrap():
    assert cw.in_call_window(1260, 540, now_min=100) is True
    assert cw.in_call_window(1260, 540, now_min=800) is False


def test_no_restriction_cases():
    assert cw.in_call_window(None, 540, now_min=0) is True
    assert cw.in_call_window(600, 600, now_min=0) is True


def test_windows_from_env_and_campaign(monkeypatch):
    monkeypatch.setenv("EPP_CALL_WINDOW_START", "10:30")
    monkeypatch.setenv("EPP_CALL_WINDOW_END", "18:00")
    assert cw.global_window() == (630, 1080)
    assert cw.campaign_window({"call_start_min": 600, "call_end_min": "1200"}) == (600, 1200)
    assert cw.campaign_window({"call_start_min": None, "call_end_min": "junk"}) == (540, 1260)
    assert cw.campaign_window(None) == (630, 1080)
    assert cw.hhmm_to_min("25:70") == (1 * 60 + 10)
    assert cw.hhmm_to_min("nope", default=7) == 7
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_calling_window.py -q` → ImportError.

- [ ] **Step 3: Implement** — copy `hhmm_to_min`, `now_ist_min`, `in_call_window`, `global_window`, `campaign_window` and `_tz` from `7x/callbacks.py` into `calling_window.py`; rename `_tz` → `tz`, read `EPP_CALL_WINDOW_START/END` (fallback `EO_*`), `EPP_CALL_TZ` (fallback `CALLBACK_TZ`, default `Asia/Kolkata`). Drop `compute_due_at`, `_parse_text`, `new_callback_record`.

- [ ] **Step 4: Run tests** — expected PASS.

- [ ] **Step 5: Commit** — `git add calling_window.py tests/test_calling_window.py && git commit -m "Campaigns: calling-hours helpers"`

---

### Task 3: Contact importer

**Files:**
- Create: `contacts_import.py`
- Test: `tests/test_contacts_import.py`
- Modify: `requirements.txt` (add `openpyxl`)

**Interfaces:**
- Produces: `normalize_phone(raw) -> (e164|None, is_valid)`, `parse_upload(filename, data) -> (rows, rejected, total, unknown_headers)` with rows `(name, e164, status, {"caller_type":..., "notes":...})`, `build_template() -> bytes`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_contacts_import.py
import io
from openpyxl import Workbook

import contacts_import as ci


def test_normalize_phone_india_centric():
    assert ci.normalize_phone("9876543210") == ("+919876543210", True)
    assert ci.normalize_phone("09876543210") == ("+919876543210", True)
    assert ci.normalize_phone("+91 98765 43210") == ("+919876543210", True)
    assert ci.normalize_phone("9.17619E+11") == (None, False)          # Excel corruption
    assert ci.normalize_phone("12") == ("+12", False)
    assert ci.normalize_phone("") == (None, False)


def test_csv_with_headers_and_type_column():
    data = b"Name,Phone,Type,Notes\nRahul,9876543210,Employee,Halol\n,9876543210,,\nMeera,+919000000001,customer,\nbad,9.1E+11,,\n"
    rows, rejected, total, unknown = ci.parse_upload("x.csv", data)
    assert total == 4 and rejected == 1 and unknown == []
    assert rows == [("Rahul", "+919876543210", "valid", {"caller_type": "employee", "notes": "Halol"}),
                    ("Meera", "+919000000001", "valid", {"caller_type": "customer", "notes": ""})]


def test_headerless_two_column_sheet_and_xlsx():
    wb = Workbook(); ws = wb.active
    ws.append(["Jay", "9000000002"]); ws.append(["", "9000000003"])
    buf = io.BytesIO(); wb.save(buf)
    rows, rejected, total, unknown = ci.parse_upload("c.xlsx", buf.getvalue())
    assert [r[1] for r in rows] == ["+919000000002", "+919000000003"]
    assert rows[0][0] == "Jay" and rows[1][0] == ""


def test_template_is_a_workbook_with_text_phone_column():
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(ci.build_template()))
    ws = wb.active
    assert [c.value for c in ws[1]] == ["Name", "Phone", "Type", "Notes"]
    assert ws["B2"].number_format == "@"
```

- [ ] **Step 2: Run to verify failure** — ImportError.

- [ ] **Step 3: Implement** — port `7x/eo_import.py`: keep `normalize_phone`, `_parse_xlsx`, `_parse_csv`, `_rows_from_matrix`, `parse_upload`, `build_template`. Replace `_GUEST_HINTS` with `_EXTRA_HINTS = {"caller_type": ("type", "caller type", "category"), "notes": ("note", "remark", "comment", "department", "plant")}`; normalise `caller_type` through `routing.normalize_caller_type`; the extra dict always has both keys (`""` when absent). Template headers `Name, Phone, Type, Notes`, samples `("Rahul Verma", "9876543210", "Employee", "Halol plant")`, `("Meera Shah", "+919812345678", "Customer", "Acme Pipes")`. Add `openpyxl` to `requirements.txt`.

- [ ] **Step 4: Run tests** — PASS.

- [ ] **Step 5: Commit** — `git add contacts_import.py requirements.txt tests/test_contacts_import.py && git commit -m "Campaigns: contact importer"`

---

### Task 4: Campaign agents, outbound trigger, placeholders

**Files:**
- Modify: `epp_seeds.py`, `prompt_render.py`, `eo_db.py` (`agents` table gains `outbound_trigger_template`; seed writes it), `agent_tools.py`
- Test: `tests/test_campaign_agents.py`

**Interfaces:**
- Produces: `epp_seeds.OUTCOMES = {"intake": [...], "followup": [...], "announcement": [...]}` (each a list of `{"value","description"}`), `epp_seeds.AGENT_FOR_TYPE = {"intake": "epp_intake", "followup": "epp_followup", "announcement": "epp_announcement"}`; `prompt_render.KNOWN_PLACEHOLDERS` += `campaign_name, campaign_message, ticket_id, ticket_id_spoken, ticket_status, ticket_category, ticket_department, ticket_created_spoken, caller_name`; `prompt_render.render_prompt(agent, ..., outbound=False)` returns `trigger` from `outbound_trigger_template` when `outbound=True`; `agent_tools.build_tools(categories=None, langs=None, campaign_type=None)`; `agent_tools.record_outcome_declaration(campaign_type)`; `agent_tools.COMPLETION_TOOLS = ("create_ticket", "lookup_ticket", "record_outcome")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_campaign_agents.py
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
    assert epp_seeds.AGENT_FOR_TYPE == {"intake": "epp_intake", "followup": "epp_followup",
                                        "announcement": "epp_announcement"}


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


def test_announcement_prompt_carries_the_message():
    seed = next(s for s in epp_seeds.SEEDS if s["slug"] == "epp_announcement")
    r = pr.render_prompt(seed, outbound=True, extra={"campaign_message": "The Halol plant is closed on Monday.",
                                                    "campaign_name": "Holiday notice"})
    assert r["missing"] == [] and "closed on Monday" in r["system_instruction"]


def test_inbound_render_ignores_the_outbound_trigger():
    seed = next(s for s in epp_seeds.SEEDS if s["slug"] == "epp_intake")
    assert "Welcome" in pr.render_prompt(seed)["trigger"].replace("THE OPENING", "Welcome")
    assert "calling from" in pr.render_prompt(seed, outbound=True)["trigger"]


def test_tools_per_campaign_type():
    assert [t["name"] for t in agent_tools.build_tools()] == ["create_ticket", "lookup_ticket", "end_call"]
    out = agent_tools.build_tools(campaign_type="followup")
    assert [t["name"] for t in out] == ["create_ticket", "lookup_ticket", "record_outcome", "end_call"]
    enum = out[2]["parameters"]["properties"]["outcome_status"]["enum"]
    assert enum == ["confirmed", "has_update", "callback", "not_reachable", "wrong_number"]
    assert agent_tools.record_outcome_declaration("intake")["parameters"]["properties"]["outcome_status"]["enum"][0] == "no_concern"
    assert agent_tools.record_outcome_declaration("announcement")["parameters"]["properties"]["outcome_status"]["enum"][0] == "acknowledged"
    assert "record_outcome" in agent_tools.COMPLETION_TOOLS
```

- [ ] **Step 2: Run to verify failure** — fails on `outbound_trigger_template`, `AGENT_FOR_TYPE`, unknown placeholders.

- [ ] **Step 3: Implement**

`eo_db.py`: add `outbound_trigger_template TEXT NOT NULL DEFAULT ''` to the `agents` DDL, to `AGENT_FIELDS`, to `_seed_agents` (INSERT it), and `refresh_seed_agent` (UPDATE it). Add a one-line migration in `init()` before `executescript`: if the `agents` table exists without the column, `ALTER TABLE agents ADD COLUMN outbound_trigger_template TEXT NOT NULL DEFAULT ''`.

`epp_seeds.py`: add

```python
OUTCOMES = {
    "intake": [
        {"value": "no_concern", "description": "the person had nothing to register right now"},
        {"value": "callback", "description": "a LIVE person asked to be called at another time"},
        {"value": "not_reachable", "description": "voicemail, an answering machine, or no live person"},
        {"value": "wrong_number", "description": "the person says this is not who we asked for"},
    ],
    "followup": [
        {"value": "confirmed", "description": "they heard the status and had nothing to add"},
        {"value": "has_update", "description": "they gave new information — put it in the note"},
        {"value": "callback", "description": "a LIVE person asked to be called at another time"},
        {"value": "not_reachable", "description": "voicemail, an answering machine, or no live person"},
        {"value": "wrong_number", "description": "the person says this is not who we asked for"},
    ],
    "announcement": [
        {"value": "acknowledged", "description": "they heard and understood the message"},
        {"value": "declined", "description": "they did not want to hear it"},
        {"value": "callback", "description": "a LIVE person asked to be called at another time"},
        {"value": "not_reachable", "description": "voicemail, an answering machine, or no live person"},
        {"value": "wrong_number", "description": "the person says this is not who we asked for"},
    ],
}
AGENT_FOR_TYPE = {"intake": "epp_intake", "followup": "epp_followup", "announcement": "epp_announcement"}

INTAKE_OUTBOUND_TRIGGER = (
    "[You are placing an OUTBOUND call; the person has just answered. Do NOT say 'Welcome'. Say, in "
    "English: \"Hello, this is {helpline_name}, calling from {company_name}. I'm calling to check whether "
    "there is any concern, complaint or feedback you would like to register with us today. Which language "
    "would you prefer?\" Then STOP and wait. If they have nothing to register, thank them, call record_outcome "
    "with no_concern, say goodbye and end_call. If a machine answers, record not_reachable and end_call.]"
)
```

Append an `## OUTBOUND CALLS` section to `INTAKE_PROMPT` (after ENDING): "When this is an outbound call (your opening says so), the caller did not ring us. Be brief and respectful of their time. If they have a concern, run the normal flow. If not, call record_outcome with no_concern, thank them and end the call."

Add `FOLLOWUP_PROMPT` (identity + `_VOICE` + LANGUAGE + `## THE TICKET` with the placeholders + `## THE FLOW`: confirm you are speaking to `{caller_name}`; state the reference `{ticket_id_spoken}` and status `{ticket_status}`, department `{ticket_department}`; ask whether they have anything to add; record `has_update` with the addition in the note, else `confirmed`; never promise outcomes; `## ENDING`), `FOLLOWUP_TRIGGER` ("[Outbound follow-up call answered. Say: 'Hello, this is {helpline_name} calling from {company_name}. Am I speaking with {caller_name}?' Then STOP.]"), `ANNOUNCEMENT_PROMPT` (identity + `_VOICE` + LANGUAGE + `## THE MESSAGE\n{campaign_message}` + rules: read it once in the caller's language, answer simple questions from the message only, offer to register a concern via the intake tools if they raise one, record acknowledged/declined, `## ENDING`), `ANNOUNCEMENT_TRIGGER` ("[Outbound call answered. Say: 'Hello, this is {helpline_name} calling from {company_name} with a short message. Which language would you prefer?' Then STOP.]"). Both new prompts must contain "SPEAK TO A PERSON" handling and "Never promise an outcome".

`SEEDS` becomes three dicts each with `slug, name, description, prompt_template, trigger_template, outbound_trigger_template`. For the intake agent `outbound_trigger_template = INTAKE_OUTBOUND_TRIGGER`; for the two new ones `trigger_template` and `outbound_trigger_template` are the same string.

`prompt_render.py`: extend `KNOWN_PLACEHOLDERS` with `campaign_name, campaign_message, ticket_id, ticket_id_spoken, ticket_status, ticket_category, ticket_department, ticket_created_spoken, caller_name`. `render_prompt(agent, *, ..., outbound=False)`: `trig_tpl = agent.get("outbound_trigger_template") if outbound and agent.get("outbound_trigger_template") else agent.get("trigger_template")`.

`agent_tools.py`:

```python
def record_outcome_declaration(campaign_type):
    import epp_seeds
    outcomes = epp_seeds.OUTCOMES.get(campaign_type) or epp_seeds.OUTCOMES["intake"]
    return {
        "name": "record_outcome",
        "description": ("Record how this OUTBOUND call ended, once, silently, right before your goodbye. "
                        "Use it when NO ticket is being created on this call (or in addition to one on a "
                        "follow-up). Outcomes: " + "; ".join(f"{o['value']} = {o['description']}" for o in outcomes)),
        "parameters": {"type": "object", "properties": {
            "outcome_status": {"type": "string", "enum": [o["value"] for o in outcomes]},
            "note": {"type": "string", "description": "What they said, in English, in one or two sentences. Empty if nothing."},
            "callback_time_text": {"type": "string", "description": "When they asked to be called, in their words. Empty otherwise."},
        }, "required": ["outcome_status"]},
    }


def build_tools(categories=None, langs=None, campaign_type=None):
    ...
    tools = [create, LOOKUP_TICKET_DECLARATION]
    if campaign_type:
        tools.append(record_outcome_declaration(campaign_type))
    tools.append(END_CALL_DECLARATION)
    return tools

COMPLETION_TOOLS = ("create_ticket", "lookup_ticket", "record_outcome")
```

- [ ] **Step 4: Run** — `python -m pytest tests/test_campaign_agents.py tests/test_prompt_render.py tests/test_eo_db.py tests/test_epp_api.py -q`. Fix `test_epp_api.test_agent_editor...` if it asserts one agent (it reads `items[0]`; ensure `epp_intake` is first by seeding order — it is).

- [ ] **Step 5: Commit** — `git commit -am "Campaigns: follow-up and announcement agents, outbound triggers, record_outcome tool"` (plus `git add tests/test_campaign_agents.py`).

---

### Task 5: Campaign service — create, validate, outcome handling, call context

**Files:**
- Create: `campaigns.py`
- Modify: `recorder.py` (open signature + outcome capture), `store.py` (`find_campaign_call`), `tickets.py` (`source="campaign"` note), `main.py` (`live_room()`)
- Test: `tests/test_campaigns.py`

**Interfaces:**
- Produces:
  - `campaigns.CampaignError(ValueError)`
  - `campaigns.create(user, body) -> campaign dict` — validates and inserts campaign + contacts.
  - `campaigns.call_context(campaign_id, cc_id, caller="") -> {"agent","system_instruction","trigger","tools","missing","campaign","cc","ticket"}` — never raises (falls back to inbound context).
  - `campaigns.handle_record_outcome(args, call_meta) -> dict` — tool handler; writes a `has_update` note on the follow-up ticket; returns `{"ok", "outcome_status", "instruction"}`.
  - `campaigns.max_active() -> int`.
  - `recorder.CallRecorder.open(source, call_sid=None, caller=None, campaign_id=None, campaign_contact_id=None)`; record gains `campaign_id`, `campaign_contact_id`, `outcome`, `outcome_note`, `callback_time_text`.
  - `store.find_campaign_call(campaign_contact_id, since_iso=None) -> meta|None`.
  - `main.live_room() -> int` = `max(0, MAX_LIVE_CALLS - len(_active_calls))`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_campaigns.py
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import campaigns
import eo_db
import store
import tickets
from recorder import CallRecorder

ADMIN = {"id": 1, "username": "admin", "name": "Admin", "role": "admin"}
FUTURE = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


@pytest.fixture
def db(fresh_eo_db, monkeypatch):
    monkeypatch.delenv("EPP_MAX_ACTIVE_CAMPAIGNS", raising=False)
    fresh_eo_db.init()
    return fresh_eo_db


def _contact(db, phone, name="A"):
    return db.add_contact(name, phone)[0]


def test_create_intake_campaign_from_contacts(db):
    c1, c2 = _contact(db, "+919000000001"), _contact(db, "+919000000002", "B")
    c = campaigns.create(ADMIN, {"name": "Round", "campaign_type": "intake", "contact_ids": [c1, c2, c1],
                                 "start_at": FUTURE, "call_start": "10:00", "call_end": "18:30"})
    assert c["status"] == "scheduled" and c["contact_count"] == 2       # deduped by phone
    assert (c["call_start_min"], c["call_end_min"]) == (600, 1110)
    rows = db.list_campaign_contacts(c["id"])["items"]
    assert {r["phone"] for r in rows} == {"+919000000001", "+919000000002"}


def test_create_followup_campaign_from_tickets(db):
    t = tickets.create_from_tool({"caller_type": "employee", "language": "en", "caller_name": "Rahul",
                                  "contact_number": "9876543210", "description": "Salary pending two months.",
                                  "category": "Salary", "high_priority_reason": "none"}, {})
    no_phone = tickets.create_from_tool({"caller_type": "vendor", "language": "en", "caller_name": "Jay",
                                         "description": "Invoice unpaid.", "category": "Invoice Query",
                                         "high_priority_reason": "none"}, {})
    c = campaigns.create(ADMIN, {"name": "FU", "campaign_type": "followup", "ticket_ids": [t["ticket_id"]],
                                 "start_at": FUTURE})
    row = db.list_campaign_contacts(c["id"])["items"][0]
    assert row["ticket_id"] == t["ticket_id"] and row["phone"] == "+919876543210" and row["name"] == "Rahul"
    with pytest.raises(campaigns.CampaignError):
        campaigns.create(ADMIN, {"name": "FU2", "campaign_type": "followup",
                                 "ticket_ids": [no_phone["ticket_id"]], "start_at": FUTURE})


def test_create_validation(db):
    cid = _contact(db, "+919000000001")
    with pytest.raises(campaigns.CampaignError):
        campaigns.create(ADMIN, {"name": "", "campaign_type": "intake", "contact_ids": [cid], "start_at": FUTURE})
    with pytest.raises(campaigns.CampaignError):
        campaigns.create(ADMIN, {"name": "x", "campaign_type": "party", "contact_ids": [cid], "start_at": FUTURE})
    with pytest.raises(campaigns.CampaignError):
        campaigns.create(ADMIN, {"name": "x", "campaign_type": "announcement", "contact_ids": [cid],
                                 "start_at": FUTURE, "message": ""})
    with pytest.raises(campaigns.CampaignError):
        campaigns.create(ADMIN, {"name": "x", "campaign_type": "intake", "contact_ids": [], "start_at": FUTURE})
    with pytest.raises(campaigns.CampaignError):
        campaigns.create(ADMIN, {"name": "x", "campaign_type": "intake", "contact_ids": [cid], "start_at": "2020-01-01T00:00:00Z"})
    past_ok = campaigns.create(ADMIN, {"name": "now", "campaign_type": "intake", "contact_ids": [cid],
                                       "start_at": datetime.now(timezone.utc).isoformat()})
    assert past_ok["status"] == "live"                                    # "start now" goes live at once


def test_active_cap(db, monkeypatch):
    monkeypatch.setenv("EPP_MAX_ACTIVE_CAMPAIGNS", "1")
    cid = _contact(db, "+919000000001")
    campaigns.create(ADMIN, {"name": "a", "campaign_type": "intake", "contact_ids": [cid], "start_at": FUTURE})
    with pytest.raises(campaigns.CampaignError):
        campaigns.create(ADMIN, {"name": "b", "campaign_type": "intake", "contact_ids": [cid], "start_at": FUTURE})


def test_call_context_per_type(db):
    cid = _contact(db, "+919000000001", "Rahul")
    ann = campaigns.create(ADMIN, {"name": "Notice", "campaign_type": "announcement", "contact_ids": [cid],
                                   "start_at": FUTURE, "message": "Plant closed on Monday."})
    cc = db.list_campaign_contacts(ann["id"])["items"][0]
    ctx = campaigns.call_context(ann["id"], cc["id"], caller="+919000000001")
    assert ctx["agent"]["slug"] == "epp_announcement"
    assert "Plant closed on Monday." in ctx["system_instruction"]
    assert "calling from" in ctx["trigger"] and ctx["missing"] == []
    assert [t["name"] for t in ctx["tools"]] == ["create_ticket", "lookup_ticket", "record_outcome", "end_call"]

    t = tickets.create_from_tool({"caller_type": "employee", "language": "hi", "caller_name": "Rahul",
                                  "contact_number": "9000000001", "description": "Salary pending.",
                                  "category": "Salary", "high_priority_reason": "none"}, {})
    fu = campaigns.create(ADMIN, {"name": "FU", "campaign_type": "followup", "ticket_ids": [t["ticket_id"]], "start_at": FUTURE})
    cc = db.list_campaign_contacts(fu["id"])["items"][0]
    ctx = campaigns.call_context(fu["id"], cc["id"])
    assert ctx["agent"]["slug"] == "epp_followup" and ctx["missing"] == []
    assert "Finance" in ctx["system_instruction"] and "Open" in ctx["system_instruction"]

    # unknown ids never raise: inbound context comes back
    ctx = campaigns.call_context(999, 999)
    assert ctx["agent"]["slug"] == "epp_intake" and "record_outcome" not in [x["name"] for x in ctx["tools"]]


def test_record_outcome_writes_followup_note_on_the_ticket(db):
    t = tickets.create_from_tool({"caller_type": "employee", "language": "en", "caller_name": "Rahul",
                                  "contact_number": "9000000001", "description": "Salary pending.",
                                  "category": "Salary", "high_priority_reason": "none"}, {})
    fu = campaigns.create(ADMIN, {"name": "FU", "campaign_type": "followup", "ticket_ids": [t["ticket_id"]], "start_at": FUTURE})
    cc = db.list_campaign_contacts(fu["id"])["items"][0]
    res = campaigns.handle_record_outcome({"outcome_status": "has_update", "note": "HR called him yesterday."},
                                          {"call_id": "c1", "campaign_id": fu["id"], "campaign_contact_id": cc["id"]})
    assert res["ok"] is True and res["outcome_status"] == "has_update"
    events = db.list_ticket_events(t["ticket_id"])
    assert events[-1]["kind"] == "note" and "HR called him" in events[-1]["note"] and events[-1]["actor_type"] == "ai"
    bad = campaigns.handle_record_outcome({"outcome_status": "party"}, {"campaign_id": fu["id"], "campaign_contact_id": cc["id"]})
    assert bad["ok"] is True and bad["outcome_status"] == "callback"      # coerced, never lost


def test_recorder_captures_outcome_and_campaign_ids():
    async def run():
        r = CallRecorder(model="t")
        await r.open(source="plivo", campaign_id=5, campaign_contact_id=9)
        r._record_tool({"type": "tool_call", "name": "record_outcome", "args": {},
                        "result": {"ok": True, "outcome_status": "acknowledged", "note": "fine",
                                   "callback_time_text": ""}})
        await r.close()
        return r.call, await store.find_campaign_call(9)
    call, found = asyncio.run(run())
    assert call["campaign_id"] == 5 and call["campaign_contact_id"] == 9
    assert call["outcome"] == "acknowledged" and call["outcome_note"] == "fine"
    assert found and found["id"] == call["id"]
    assert asyncio.run(store.find_campaign_call(9, since_iso="2999-01-01")) is None
```

- [ ] **Step 2: Run to verify failure** — ImportError on `campaigns`.

- [ ] **Step 3: Implement**

`store.py`:

```python
async def find_campaign_call(campaign_contact_id, since_iso=None):
    """Latest call record for one campaign contact, started at/after since_iso. Index-only."""
    with _LOCK:
        metas = list(_INDEX.values())
    best = None
    for m in metas:
        if str(m.get("campaign_contact_id") or "") != str(campaign_contact_id):
            continue
        st = m.get("started_at") or ""
        if since_iso and st < since_iso:
            continue
        if best is None or st > (best.get("started_at") or ""):
            best = m
    return best
```
Also add `campaign_id` to `_matches` filters (`filters.get("campaign_id")` → exact match on `meta["campaign_id"]`).

`recorder.py`: `open(..., campaign_id=None, campaign_contact_id=None)` stores both; record gains `"outcome": None, "outcome_note": "", "callback_time_text": ""`; in `_record_tool`, `elif name == "record_outcome" and isinstance(result, dict) and result.get("ok"): self.call["outcome"] = result.get("outcome_status"); self.call["outcome_note"] = result.get("note") or ""; self.call["callback_time_text"] = result.get("callback_time_text") or ""`. In `_schedule_analysis`, skip when `self.call.get("campaign_type") == "followup"` (set `campaign_type` on open when `campaign_id` is given: `eo_db.get_campaign(campaign_id)["campaign_type"]`, guarded).

`tickets.create_from_tool`: if `call_meta.get("campaign_id")`, set `source="campaign"` and append ` · campaign #{id}` to the created event note.

`main.py`: `MAX_LIVE_CALLS = int(os.getenv("MAX_LIVE_CALLS", "10"))`; `def live_room(): return max(0, MAX_LIVE_CALLS - len(_active_calls))`.

`campaigns.py`:

```python
"""Campaign service: validation and creation, the per-call context for an outbound dial,
and the record_outcome tool handler. The runner (campaign_runner) drives dialing."""
import logging, os
from datetime import datetime, timezone

import agent_tools, calling_window, eo_db, epp_seeds, languages, prompt_render, routing, tickets
from tickets import _clean_phone

logger = logging.getLogger(__name__)


class CampaignError(ValueError):
    pass


def max_active() -> int:
    try:
        return max(1, int(os.getenv("EPP_MAX_ACTIVE_CAMPAIGNS", "6")))
    except (TypeError, ValueError):
        return 6


def _parse_iso(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _whole(v, name, lo, hi, default):
    if v in (None, ""):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise CampaignError(f"{name} must be a whole number")
    if f != int(f) or not (lo <= int(f) <= hi):
        raise CampaignError(f"{name} must be a whole number between {lo} and {hi}")
    return int(f)


def create(user, body) -> dict:
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 160:
        raise CampaignError("Campaign name is required (max 160 characters)")
    ctype = str(body.get("campaign_type") or "").strip().lower()
    if ctype not in eo_db.CAMPAIGN_TYPES:
        raise CampaignError("campaign_type must be intake, followup or announcement")
    message = str(body.get("message") or "").strip()
    if ctype == "announcement" and not message:
        raise CampaignError("An announcement needs a message")
    if len(message) > 4000:
        raise CampaignError("Message is too long (max 4000 characters)")
    start = _parse_iso(body.get("start_at"))
    if not start:
        raise CampaignError("A valid start date/time is required")
    now = datetime.now(timezone.utc)
    if (now - start).total_seconds() > 120:
        raise CampaignError("Start time is in the past. Pick the current time or later.")
    delay_h = _whole(body.get("callback_delay_hours"), "Retry delay (hours)", 0, 720, 4)
    max_day = _whole(body.get("callback_max_per_day"), "Attempts per day", 1, 10, 3)
    days = _whole(body.get("callback_days"), "Retry days", 1, 10, 1)
    start_min = calling_window.hhmm_to_min(body.get("call_start") or "09:00", 540)
    end_min = calling_window.hhmm_to_min(body.get("call_end") or "21:00", 1260)

    rows, seen = [], set()
    if ctype == "followup":
        ids = [str(t).strip() for t in (body.get("ticket_ids") or []) if str(t).strip()]
        if not ids:
            raise CampaignError("Pick at least one ticket")
        for tid in ids:
            t = eo_db.get_ticket(tid)
            if not t:
                raise CampaignError(f"Ticket {tid} not found")
            phone = _clean_phone(t.get("contact_number"))
            if len(phone) < 11:
                raise CampaignError(f"Ticket {tid} has no contact number")
            if phone in seen:
                continue
            seen.add(phone)
            rows.append({"phone": phone, "name": t.get("caller_name") or "", "contact_id": None, "ticket_id": tid})
    else:
        contacts = [c for c in eo_db.get_contacts_by_ids(body.get("contact_ids") or []) if c.get("status") == "valid"]
        for c in contacts:
            if c["phone"] in seen:
                continue
            seen.add(c["phone"])
            rows.append({"phone": c["phone"], "name": c.get("name") or "", "contact_id": c["id"], "ticket_id": None})
        if not rows:
            raise CampaignError("Select at least one valid contact")

    active = eo_db.active_campaigns()
    if len(active) >= max_active():
        raise CampaignError(f"{len(active)} campaigns are already scheduled or live (limit {max_active()}). "
                            "Cancel one or raise EPP_MAX_ACTIVE_CAMPAIGNS.")
    status = "live" if start <= now else "scheduled"
    cid = eo_db.create_campaign(name, ctype, start.astimezone(timezone.utc).isoformat(), user["id"],
                                message=message, status=status, callback_delay_hours=delay_h,
                                callback_max_per_day=max_day, callback_days=days,
                                call_start_min=start_min, call_end_min=end_min)
    eo_db.add_campaign_contacts(cid, rows)
    return eo_db.get_campaign_full(cid)


def _ticket_extra(t):
    if not t:
        return {}
    return {
        "caller_name": t.get("caller_name") or "",
        "ticket_id": t["ticket_id"],
        "ticket_id_spoken": tickets.spoken_ticket_id(t["ticket_id"]),
        "ticket_status": tickets.STATUS_LABEL.get(t.get("status"), t.get("status") or ""),
        "ticket_category": t.get("category") or "",
        "ticket_department": t.get("assigned_department") or "",
        "ticket_created_spoken": tickets.spoken_date(t.get("created_at")),
    }


def call_context(campaign_id, cc_id, caller=""):
    """Everything a campaign dial's Live session needs. Falls back to the inbound context on
    any failure — a generic call beats a dropped one."""
    import main
    campaign = eo_db.get_campaign(campaign_id) if campaign_id else None
    cc = eo_db.get_campaign_contact(cc_id) if cc_id else None
    if not campaign or not cc or int(cc.get("campaign_id") or 0) != int(campaign["id"]):
        ctx = main._resolve_call_context(caller=caller)
        ctx.update({"campaign": None, "cc": None, "ticket": None})
        return ctx
    ctx = {"agent": None, "system_instruction": None, "trigger": "", "tools": None, "missing": [],
           "campaign": campaign, "cc": cc, "ticket": None}
    try:
        st = main._static_context()
        ctype = campaign["campaign_type"]
        agent = eo_db.get_agent_by_slug(epp_seeds.AGENT_FOR_TYPE[ctype]) or st["agent"]
        ticket = eo_db.get_ticket(cc["ticket_id"]) if cc.get("ticket_id") else None
        extra = {"campaign_name": campaign["name"], "campaign_message": campaign.get("message") or "",
                 "caller_name": cc.get("name") or ""}
        extra.update(_ticket_extra(ticket))
        rendered = prompt_render.render_prompt(agent, caller_phone=caller or cc.get("phone"),
                                               categories=st["categories"], departments=st["departments"],
                                               langs=st["langs"], extra=extra, outbound=True)
        ctx.update(agent=agent, ticket=ticket, system_instruction=rendered["system_instruction"],
                   trigger=rendered["trigger"], missing=rendered["missing"],
                   tools=agent_tools.build_tools(st["categories"], st["langs"], campaign_type=ctype))
    except Exception:
        logger.exception("campaign call context failed; using the inbound context")
        ctx.update(main._resolve_call_context(caller=caller))
    return ctx


_MACHINE = ("voicemail", "voice mail", "answering machine", "machine")


def handle_record_outcome(args, call_meta):
    """record_outcome tool handler for outbound calls. Never raises."""
    call_meta = call_meta or {}
    try:
        campaign = eo_db.get_campaign(call_meta.get("campaign_id")) or {}
        ctype = campaign.get("campaign_type") or "intake"
        allowed = [o["value"] for o in epp_seeds.OUTCOMES.get(ctype, epp_seeds.OUTCOMES["intake"])]
        status = str(args.get("outcome_status") or "").strip().lower()
        note = str(args.get("note") or "").strip()[:2000]
        if status not in allowed:
            status = "not_reachable" if any(m in (status + " " + note.lower()) for m in _MACHINE) else "callback"
        cc = eo_db.get_campaign_contact(call_meta.get("campaign_contact_id")) if call_meta.get("campaign_contact_id") else None
        if ctype == "followup" and cc and cc.get("ticket_id") and status == "has_update" and note:
            eo_db.add_ticket_event(cc["ticket_id"], "note", actor_type="ai", actor_name="Follow-up call",
                                   note=note)
        return {"ok": True, "outcome_status": status, "note": note,
                "callback_time_text": str(args.get("callback_time_text") or "").strip()[:200],
                "instruction": "SYSTEM NOTE — the outcome is saved; never mention it. If you have not yet said "
                               "goodbye, say ONE short warm goodbye now and call end_call."}
    except Exception as e:
        logger.exception("record_outcome failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "instruction": "Say one short goodbye and call end_call."}
```

- [ ] **Step 4: Run** — `python -m pytest tests/test_campaigns.py tests/test_recorder.py tests/test_tickets.py -q` → PASS.

- [ ] **Step 5: Commit** — `git add campaigns.py store.py recorder.py tickets.py main.py tests/test_campaigns.py && git commit -m "Campaigns: service, per-call context, record_outcome handler"`

---

### Task 6: Dialer, answer webhook, tool mapping, runner startup

**Files:**
- Modify: `dialer.py` (`place_call(..., campaign_id=None, campaign_contact_id=None)`), `main.py` (`/plivo/answer`, `/plivo/media-stream`, `tool_mapping`, lifespan)
- Create: `campaign_runner.py`
- Test: `tests/test_campaign_runner.py`

**Interfaces:**
- `dialer.place_call(to_number, *, base_url=None, request=None, agent_id=None, campaign_id=None, campaign_contact_id=None)` — adds `&campaign=<id>&cc=<id>` to the answer URL.
- `main.tool_mapping(recorder, outbound=False)` — adds `record_outcome` when outbound.
- `campaign_runner`: `set_override(bool|None)`, `is_enabled()`, `_apply_failure(cc, campaign, now, error=None)`, `_reap_calling(campaign, now)`, `_dial_one(cc, campaign, now)`, `dial_contact_now(campaign_id, cc_id)`, `_process_campaign(campaign, now)`, `_fair_order(campaigns)`, `_tick()`, `run_loop()`, `_tick_count`, `_plivo_ready()`.

- [ ] **Step 1: Write the failing tests** — port `7x/tests/test_campaign_runner.py` and `test_campaign_fairness.py` with these changes: `rsvp_outcome_status` → `outcome`, `rsvp_outcome` → `outcome`, `fake_find(campaign_contact_id, since_iso=None)`, `store.find_campaign_call` signature, outcomes `not_reachable`/`callback` retry and `acknowledged`/`no_concern`/`confirmed`/`wrong_number` mark done; `_six_campaign_world` becomes six `intake` campaigns of 20 contacts via `eo_db.create_campaign(f"C{i}", "intake", past, 1, status="live", call_start_min=0, call_end_min=1439)`; the live-slot gauge is `main._active_calls` (add `f"sid{n}"` entries in `fake_place_call` and clear the dict between ticks); env names `EPP_*`; `scheduler.is_enabled` → `campaign_runner.set_override(True)`; the cap test compares `campaigns.max_active()` with `campaign_runner._max_active_campaigns()`.

Add:

```python
def test_dial_one_passes_campaign_and_contact_ids(fresh_eo_db, monkeypatch):
    db = fresh_eo_db; db.init()
    cid = db.create_campaign("C", "intake", "2020-01-01T00:00:00+00:00", 1, status="live")
    db.add_campaign_contacts(cid, [{"phone": "+919000000001", "name": "A"}])
    cc = db.cc_by_status(cid, "pending")[0]
    seen = {}
    async def fake(phone, **kw):
        seen.update(kw, phone=phone); return {"success": True, "call_uuid": "u1"}
    monkeypatch.setattr(campaign_runner.dialer, "place_call", fake)
    asyncio.run(campaign_runner._dial_one(cc, db.get_campaign(cid), datetime.now(timezone.utc)))
    assert seen["campaign_id"] == cid and seen["campaign_contact_id"] == cc["id"] and seen["phone"] == "+919000000001"
    row = db.get_campaign_contact(cc["id"])
    assert row["call_status"] == "calling" and row["attempts"] == 1 and row["last_call_id"] == "u1"
```

- [ ] **Step 2: Run to verify failure** — ImportError.

- [ ] **Step 3: Implement**

`campaign_runner.py`: copy `7x/campaign_runner.py`; replace `import callbacks` → `import calling_window`, drop `import live, scheduler`; `_UNANSWERED_OUTCOMES = frozenset({"callback", "not_reachable"})`; env names `EPP_*` (keep `EO_*` as fallback via a `_cfg_int` that checks both); `_enabled_override` + `set_override` + `is_enabled()` (override wins, else `EPP_CAMPAIGN_RUNNER_ENABLED`); `_reap_calling` uses `store.find_campaign_call(cc["id"], since_iso=cc.get("last_attempt_at"))` and `rec.get("outcome")` (a call that created a ticket but recorded no outcome counts as `done` with outcome `ticket_created`); `_dial_one` calls `dialer.place_call(cc["phone"], base_url=os.getenv("PUBLIC_URL"), campaign_id=campaign["id"], campaign_contact_id=cc["id"])`; the budget uses `main.live_room()` (import lazily inside the function: `import main`); `_tick` checks `is_enabled()`; `_max_active_campaigns` delegates to `campaigns.max_active()`. Remove the callback-reservation line.

`dialer.py`: append `&campaign={int(campaign_id)}&cc={int(campaign_contact_id)}` when both given; drop `&test=1` when a campaign is given.

`main.py`:
- `tool_mapping(recorder, outbound=False)`: add `"record_outcome": lambda **kw: campaigns.handle_record_outcome(kw, recorder.call_meta)` when outbound. `CallRecorder.call_meta` must include `campaign_id` and `campaign_contact_id` (add to the property).
- `/plivo/answer`: read `qp.get("campaign")`, `qp.get("cc")`; when both present, `call_ctx = campaigns.call_context(campaign, cc, caller=caller)`, `direction = "campaign"`, and stash `campaign_id`, `campaign_contact_id` in `_remember_call_meta`.
- `/plivo/media-stream`: `source = "plivo_campaign" if meta.get("direction") == "campaign" else ...`; `recorder.open(..., campaign_id=meta.get("campaign_id"), campaign_contact_id=meta.get("campaign_contact_id"))`; `tool_mapping(recorder, outbound=bool(meta.get("campaign_id")))`.
- lifespan: `app.state.campaign_task = asyncio.create_task(campaign_runner.run_loop())` after DB init; cancel on shutdown.

- [ ] **Step 4: Run** — `python -m pytest tests/test_campaign_runner.py tests/test_campaigns.py -q` → PASS, then the full suite.

- [ ] **Step 5: Commit** — `git add campaign_runner.py dialer.py main.py tests/test_campaign_runner.py && git commit -m "Campaigns: dial loop, answer-webhook context, runner startup"`

---

### Task 7: API routes

**Files:**
- Modify: `eo_api.py` (new section before the audit routes)
- Test: `tests/test_campaign_api.py`

**Interfaces (all admin-only):**
```
GET  /contacts?q&caller_type&limit&offset      POST /contacts {name, phone, caller_type, notes}
POST /contacts/import (multipart file)         GET  /contacts/template
POST /contacts/delete {ids}
GET  /campaigns?q&status&limit&offset          POST /campaigns (campaigns.create body)
GET  /campaigns/{id}                           POST /campaigns/{id}/cancel
GET  /campaigns/{id}/contacts?status&q         POST /campaigns/{id}/contacts/{cc}/retry
POST /campaigns/{id}/contacts/{cc}/cancel      PATCH /campaigns/{id}/contacts/{cc}/remark {remark}
GET  /scheduler/queue                          POST /scheduler/toggle {enabled?}
GET  /campaign-types  -> {"items":[{"type","label","outcomes":[...]}]}
```
`GET /calls` accepts `campaign_id`. Recipient rows carry `display_status`/`display_variant` (port `_contact_display` from `7x/eo_api.py` with `rsvp_outcome` → `outcome` and labels from `epp_seeds.OUTCOMES`).

- [ ] **Step 1: Write the failing tests** (use the `client`/`_login` helpers from `tests/test_epp_api.py`; copy them into this file)

```python
def test_contacts_crud_and_import(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    r = client.post("/api/epp/contacts", headers=h, json={"name": "Rahul", "phone": "9876543210", "caller_type": "employee"})
    assert r.status_code == 200 and r.json()["phone"] == "+919876543210"
    assert client.post("/api/epp/contacts", headers=h, json={"phone": "12"}).status_code == 400
    csv = b"Name,Phone,Type\nMeera,9000000001,customer\n"
    r = client.post("/api/epp/contacts/import", headers=h, files={"file": ("c.csv", csv, "text/csv")})
    assert r.status_code == 200 and r.json()["added"] == 1
    assert client.get("/api/epp/contacts?q=meera", headers=h).json()["total"] == 1
    assert client.get("/api/epp/contacts/template", headers=h).status_code == 200
    ids = [c["id"] for c in client.get("/api/epp/contacts", headers=h).json()["items"]]
    assert client.post("/api/epp/contacts/delete", headers=h, json={"ids": ids}).json()["deleted"] == 2
    acts = [a["action"] for a in fresh_eo_db.list_audit()["items"]]
    assert "contacts_imported" in acts and "contacts_deleted" in acts


def test_campaign_create_detail_cancel_and_audit(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    cid = client.post("/api/epp/contacts", headers=h, json={"name": "A", "phone": "9000000001"}).json()["id"]
    body = {"name": "Notice", "campaign_type": "announcement", "message": "Plant closed Monday.",
            "contact_ids": [cid], "start_at": FUTURE}
    r = client.post("/api/epp/campaigns", headers=h, json=body)
    assert r.status_code == 201
    c = r.json()
    assert c["contact_count"] == 1 and c["status"] == "scheduled"
    assert client.post("/api/epp/campaigns", headers=h, json={**body, "message": ""}).status_code == 400
    d = client.get(f"/api/epp/campaigns/{c['id']}", headers=h).json()
    assert d["progress"] == {"pending": 1}
    recips = client.get(f"/api/epp/campaigns/{c['id']}/contacts", headers=h).json()["items"]
    assert recips[0]["display_status"] == "Waiting — campaign not active"
    assert client.get("/api/epp/campaigns?status=scheduled", headers=h).json()["total"] == 1
    assert client.post(f"/api/epp/campaigns/{c['id']}/cancel", headers=h).status_code == 200
    assert client.post(f"/api/epp/campaigns/{c['id']}/cancel", headers=h).status_code == 400
    acts = [a["action"] for a in fresh_eo_db.list_audit()["items"]]
    assert "campaign_created" in acts and "campaign_cancelled" in acts


def test_campaign_routes_are_admin_only(client, fresh_eo_db):
    h = _login(client, "admin", ADMIN_PASS)
    dept = _make_dept_user(client, h, fresh_eo_db, "hr1", "HR")
    for path in ("/api/epp/contacts", "/api/epp/campaigns", "/api/epp/scheduler/queue", "/api/epp/campaign-types"):
        assert client.get(path, headers=dept).status_code == 403


def test_scheduler_toggle_and_queue(client, monkeypatch):
    import campaign_runner
    h = _login(client, "admin", ADMIN_PASS)
    r = client.post("/api/epp/scheduler/toggle", headers=h, json={"enabled": False})
    assert r.json()["enabled"] is False and campaign_runner.is_enabled() is False
    q = client.get("/api/epp/scheduler/queue", headers=h).json()
    assert q["scheduler_enabled"] is False and q["items"] == []
    campaign_runner.set_override(None)


def test_campaign_types_lists_outcomes(client):
    h = _login(client, "admin", ADMIN_PASS)
    items = client.get("/api/epp/campaign-types", headers=h).json()["items"]
    assert [i["type"] for i in items] == ["intake", "followup", "announcement"]
    assert items[1]["outcomes"][0]["value"] == "confirmed"
```

- [ ] **Step 2: Run to verify failure** — 404s.

- [ ] **Step 3: Implement** — add the routes to `eo_api.py`, each starting with `admin = eo_auth.require_admin(request)`. `POST /campaigns` wraps `campaigns.create` and maps `CampaignError` → 400. `retry` calls `campaign_runner.dial_contact_now` (error → 400). `cancel` on a contact: 409 if `calling`, 400 if not `pending`, else `cc_update(call_status="cancelled", next_attempt_at=None)`. Port `_contact_display`/`_attach_contact_display` with `calling_window` and outcome labels `{"no_concern": "No concern", "confirmed": "Confirmed", "has_update": "Gave an update", "acknowledged": "Acknowledged", "declined": "Declined", "callback": "Callback requested", "not_reachable": "Not reachable", "wrong_number": "Wrong number", "ticket_created": "Ticket created"}` (green for positive, amber for callback/not_reachable, red for wrong_number/declined). Audit actions: `contact_added`, `contacts_imported`, `contacts_deleted`, `campaign_created`, `campaign_cancelled`, `campaign_call_now`, `campaign_contact_cancelled`, `scheduler_toggled`. `GET /calls` passes `campaign_id` into the store filter.

- [ ] **Step 4: Run** — full suite → PASS.

- [ ] **Step 5: Commit** — `git add eo_api.py tests/test_campaign_api.py && git commit -m "Campaigns: admin API"`

---

### Task 8: Admin pages

**Files:**
- Create: `admin/src/pages/Contacts.jsx`, `Campaigns.jsx`, `CampaignDetail.jsx`, `CreateCampaign.jsx`, `Scheduler.jsx`; `admin/src/components/ContactUpload.jsx`, `ContactsTable.jsx`, `RemarkCell.jsx` (copy `RemarkCell` from `7x/`), `TicketPicker.jsx`
- Modify: `admin/src/App.jsx`, `admin/src/components/Layout.jsx`, `admin/src/components/icons.jsx` (add `IconPlus`, `IconCampaigns`, `IconClock`, `IconContacts`), `admin/src/api.js` (restore `uploadFile`, add `CAMPAIGN_TYPE_LABEL`, `minToHHMM`)

- [ ] **Step 1: Routes and nav** — `App.jsx` adds admin-only routes `/contacts`, `/campaigns`, `/campaigns/new`, `/campaigns/:id`, `/scheduler`. `Layout.jsx` `ADMIN_NAV` gains `Contacts`, `Campaigns`, `Scheduler` after `Tickets`.

- [ ] **Step 2: Contacts page** — `ContactUpload` (port from `7x/`, `uploadFile('/contacts/import', file)`, sample → `/contacts/template`), add-contact modal (name, phone, type select, notes), `ContactsTable` (search, type filter, checkbox selection, `selectable`/`selected`/`onToggle`/`onToggleMany`/`onTotal`/`refreshKey` props like 7x), delete selected.

- [ ] **Step 3: Create Campaign wizard** — one page with numbered panels: 1 Type (three radio cards with a one-line description each), 2 Recipients (`ContactsTable` selectable for intake/announcement; `TicketPicker` for followup: table of tickets from `/tickets?status=open,under_review,escalated&limit=200` with search, status filter, department filter, checkbox per row, rows without `contact_number` disabled), 3 Message (announcement only, textarea, live character count, "read aloud in the caller's language" hint), 4 Schedule (start date/time defaulting to now, retry delay/attempts/days, calling hours) — the modal from `7x/CreateCampaign.jsx` minus fatigue and event fields. Submit → `POST /campaigns` → navigate to the detail page. Fixed bottom bar shows selection count.

- [ ] **Step 4: Campaigns list + detail** — list: name (link), type pill, status pill, start, contacts (done/total), retries, created, Cancel action. Detail: stat tiles (Total, Pending, Calling, Done, Failed, Cancelled, Tickets created), outcome breakdown pills from `outcomes`, settings line, recipients table (name, phone, ticket link, last attempt, next due, attempts, status pill with `last_error` title, outcome, remark cell, Call now / Cancel), click a row with attempts > 0 → fetch `/calls?campaign_id=&q=<phone>&limit=1` then `/calls/{id}` and show it in the same modal `CallLogsPage` uses (extract that modal into `components/CallDrawer.jsx` and reuse).

- [ ] **Step 5: Scheduler page** — kill-switch toggle (`POST /scheduler/toggle`), amber banner when off, queue table from `/scheduler/queue` with Call now / Cancel (port `7x/CampaignQueue.jsx`, `rsvp` columns removed, add Campaign type).

- [ ] **Step 6: Build** — `cd admin && npm run build` → exit 0. Open `/admin` locally: create a contact, create an intake campaign scheduled for later, cancel it, check the audit log shows both.

- [ ] **Step 7: Commit** — `git add admin/src && git commit -m "Campaigns: Contacts, Create Campaign, Campaigns, Scheduler pages"`

---

### Task 9: Config and docs

**Files:**
- Modify: `.env.example`, `README.md`, `DEPLOY.md`

- [ ] **Step 1** — `.env.example`: add a `---- Outbound campaigns ----` block: `EPP_CAMPAIGN_RUNNER_ENABLED=true`, `EPP_MAX_ACTIVE_CAMPAIGNS=6`, `EPP_CAMPAIGN_MAX_CONCURRENT=5`, `EPP_CAMPAIGN_MAX_PER_TICK=2`, `EPP_CAMPAIGN_NOANSWER_SECONDS=90`, `EPP_CAMPAIGN_POLL_INTERVAL=30`, `EPP_CALL_WINDOW_START=09:00`, `EPP_CALL_WINDOW_END=21:00`, `MAX_LIVE_CALLS=10` with one-line comments; note that campaign dials share `MAX_LIVE_CALLS` with inbound calls.
- [ ] **Step 2** — README: a "Outbound campaigns" section (three types, contacts, the Scheduler kill switch) and the three new pages in the dashboard table; DEPLOY: the runner log line to look for, the kill switch, and "Campaign calls to Aditya's phone, one per type" on the first-run checklist.
- [ ] **Step 3** — Commit: `git commit -am "Campaigns: config and docs"`.

---

## Self-review

**Spec coverage.** Types → Task 4/5; contacts upload/manual/tickets → Tasks 3/5/8; scripts and placeholders → Task 4; tools → Task 4; data model → Task 1 (+`outbound_trigger_template` in Task 4); call path → Tasks 5/6; runner incl. shared live-call budget and kill switch → Task 6; API and audit → Task 7; pages → Task 8; testing → each task; docs → Task 9. Post-call analysis skipped for follow-ups → Task 5 (`_schedule_analysis`). `source='campaign'` → Task 5.

**Placeholders.** None: every step has code or an exact port instruction naming the source file and the substitutions.

**Type consistency.** `find_campaign_call(campaign_contact_id, since_iso=None)` is used identically in Tasks 5 and 6; `record_outcome` result keys (`ok, outcome_status, note, callback_time_text`) match the recorder capture; `campaigns.max_active()` is the single cap used by the service, the runner and the API; `build_tools(categories, langs, campaign_type=None)` matches its two call sites.
