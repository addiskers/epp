"""
SQLite storage for the EPP helpline: users, agents, departments, the category ->
department mapping, tickets, ticket events, the audit log, and the ticket number
sequence. Calls stay as JSON files (store.py); a ticket links to its call by call_id.

Sync sqlite3 (WAL, check_same_thread=False) guarded by a lock — every query here is tiny,
so this stays off the event loop's critical path without an async driver. Lives under
DATA_DIR next to the JSON call store.

The module keeps its historical name (it started life as the EO admin DB) because a rename
would touch every import for no user-visible gain.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Bumped whenever the schema changes shape. Stamped into PRAGMA user_version.
SCHEMA_VERSION = 2

_DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
_DB_PATH = os.path.join(_DATA_DIR, "epp.db")

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

ROLES = ("admin", "dept_user")
CALLER_TYPES = ("customer", "vendor", "employee")
TICKET_STATUSES = ("open", "under_review", "escalated", "resolved", "closed")
PRIORITIES = ("high", "medium", "low")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    name          TEXT,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'dept_user',   -- admin | dept_user
    department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS departments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    code       TEXT NOT NULL UNIQUE,
    active     INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_type   TEXT NOT NULL,                       -- customer | vendor | employee
    name          TEXT NOT NULL,
    department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    keywords      TEXT NOT NULL DEFAULT '[]',          -- JSON list, for the post-call fallback classifier
    high_priority INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(caller_type, name)
);
CREATE INDEX IF NOT EXISTS idx_categories_type ON categories(caller_type, active, sort_order);

CREATE TABLE IF NOT EXISTS agents (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    slug                 TEXT NOT NULL UNIQUE,
    description          TEXT NOT NULL DEFAULT '',
    prompt_template      TEXT NOT NULL,
    trigger_template     TEXT NOT NULL DEFAULT '',
    voice_name           TEXT NOT NULL DEFAULT '',
    speech_language_code TEXT NOT NULL DEFAULT '',
    active               INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_sequences (
    year     INTEGER PRIMARY KEY,
    next_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id              TEXT PRIMARY KEY,           -- EPP-2026-000001
    call_id                TEXT,                       -- store.py call record id
    call_sid               TEXT,
    caller_type            TEXT NOT NULL,
    caller_name            TEXT NOT NULL DEFAULT '',
    company_name           TEXT NOT NULL DEFAULT '',
    vendor_code            TEXT NOT NULL DEFAULT '',
    employee_id            TEXT NOT NULL DEFAULT '',
    caller_department      TEXT NOT NULL DEFAULT '',
    plant_location         TEXT NOT NULL DEFAULT '',
    contact_number         TEXT NOT NULL DEFAULT '',
    language               TEXT NOT NULL DEFAULT '',
    category               TEXT NOT NULL DEFAULT '',
    category_original      TEXT NOT NULL DEFAULT '',   -- what the live agent chose, if later reclassified
    subcategory            TEXT NOT NULL DEFAULT '',
    priority               TEXT NOT NULL DEFAULT 'medium',
    escalation_flag        INTEGER NOT NULL DEFAULT 0,
    escalation_reason      TEXT NOT NULL DEFAULT '',
    description            TEXT NOT NULL DEFAULT '',
    ai_summary             TEXT NOT NULL DEFAULT '',
    intent                 TEXT NOT NULL DEFAULT '',
    sentiment_score        REAL,
    sentiment_label        TEXT NOT NULL DEFAULT '',
    escalation_flags       TEXT NOT NULL DEFAULT '[]', -- JSON list from the post-call analysis
    assigned_department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    assigned_user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    status                 TEXT NOT NULL DEFAULT 'open',
    source                 TEXT NOT NULL DEFAULT 'voice',  -- voice | post_call | manual
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    resolved_at            TEXT,
    closed_at              TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_dept ON tickets(assigned_department_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_call ON tickets(call_id);
CREATE INDEX IF NOT EXISTS idx_tickets_priority ON tickets(priority, escalation_flag);

CREATE TABLE IF NOT EXISTS ticket_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id     TEXT NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    actor_type    TEXT NOT NULL DEFAULT 'system',      -- system | ai | user
    actor_user_id INTEGER,
    actor_name    TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL,                       -- created | status | assigned | reclassified | note | ai_analysis | priority
    from_value    TEXT NOT NULL DEFAULT '',
    to_value      TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_id, id);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    username   TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at DESC);
"""


def init() -> None:
    """Create tables (idempotent) and seed departments, categories and the intake agent."""
    conn = get_conn()
    with _lock:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        conn.commit()
    _seed_departments()
    _seed_categories()
    _seed_agents()


def schema_version() -> int:
    r = get_conn().execute("PRAGMA user_version").fetchone()
    return int(r[0]) if r else 0


# Generic helpers
def _rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_conn()
    with _lock:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _one(sql: str, params: tuple = ()) -> dict | None:
    rows = _rows(sql, params)
    return rows[0] if rows else None


def _exec(sql: str, params: tuple = ()) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


# ---------------------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------------------
def _seed_departments() -> None:
    import epp_seeds
    conn = get_conn()
    now = _now()
    with _lock:
        for order, (code, name) in enumerate(epp_seeds.DEPARTMENTS):
            if conn.execute("SELECT 1 FROM departments WHERE code = ?", (code,)).fetchone():
                continue
            conn.execute("INSERT INTO departments (name, code, active, sort_order, created_at, updated_at) "
                         "VALUES (?,?,1,?,?,?)", (name, code, order, now, now))
        conn.commit()


def _seed_categories() -> None:
    import epp_seeds
    conn = get_conn()
    now = _now()
    with _lock:
        codes = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM departments").fetchall()}
        for order, (ct, name, dept_code, high, kws) in enumerate(epp_seeds.CATEGORIES):
            if conn.execute("SELECT 1 FROM categories WHERE caller_type = ? AND name = ?",
                            (ct, name)).fetchone():
                continue
            conn.execute(
                "INSERT INTO categories (caller_type, name, department_id, keywords, high_priority, "
                "active, sort_order, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?,?)",
                (ct, name, codes.get(dept_code), json.dumps(list(kws)), int(high), order, now, now))
        conn.commit()


def _seed_agents() -> None:
    import epp_seeds
    conn = get_conn()
    now = _now()
    with _lock:
        for seed in epp_seeds.SEEDS:
            if conn.execute("SELECT 1 FROM agents WHERE slug = ?", (seed["slug"],)).fetchone():
                continue
            conn.execute(
                "INSERT INTO agents (name, slug, description, prompt_template, trigger_template, "
                "voice_name, speech_language_code, active, created_at, updated_at) "
                "VALUES (?,?,?,?,?,'','',1,?,?)",
                (seed["name"], seed["slug"], seed.get("description", ""),
                 seed["prompt_template"], seed.get("trigger_template", ""), now, now))
        conn.commit()


def refresh_seed_agent(slug: str) -> bool:
    """Overwrite a seeded agent's prompt with the shipped text (operator-invoked reset)."""
    import epp_seeds
    for seed in epp_seeds.SEEDS:
        if seed["slug"] == slug:
            n = _exec("UPDATE agents SET prompt_template = ?, trigger_template = ?, updated_at = ? "
                      "WHERE slug = ?",
                      (seed["prompt_template"], seed.get("trigger_template", ""), _now(), slug))
            return bool(n is not None)
    return False


# ---------------------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------------------
def count_users() -> int:
    r = _one("SELECT COUNT(*) c FROM users")
    return int(r["c"]) if r else 0


def create_user(username: str, name: str, password_hash: str, password_salt: str,
                role: str = "dept_user", department_id=None) -> int:
    now = _now()
    return _exec(
        "INSERT INTO users (username, name, password_hash, password_salt, role, department_id, "
        "active, created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
        (username, name, password_hash, password_salt, role,
         int(department_id) if department_id not in (None, "") else None, now, now),
    )


def get_user_by_username(username: str) -> dict | None:
    return _one("SELECT * FROM users WHERE username = ?", (username,))


def get_user(user_id: int) -> dict | None:
    return _one("SELECT * FROM users WHERE id = ?", (user_id,))


def list_users() -> list[dict]:
    return _rows(
        "SELECT u.id, u.username, u.name, u.role, u.active, u.department_id, u.created_at, "
        "d.name AS department_name FROM users u LEFT JOIN departments d ON d.id = u.department_id "
        "ORDER BY u.created_at DESC")


def set_user_active(user_id: int, active: bool) -> None:
    _exec("UPDATE users SET active = ?, updated_at = ? WHERE id = ?", (1 if active else 0, _now(), user_id))


def set_user_department(user_id: int, department_id) -> None:
    _exec("UPDATE users SET department_id = ?, updated_at = ? WHERE id = ?",
          (int(department_id) if department_id not in (None, "") else None, _now(), int(user_id)))


def set_user_role(user_id: int, role: str) -> None:
    _exec("UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (role, _now(), int(user_id)))


def update_user_password(user_id: int, password_hash: str, password_salt: str) -> None:
    _exec("UPDATE users SET password_hash = ?, password_salt = ?, updated_at = ? WHERE id = ?",
          (password_hash, password_salt, _now(), int(user_id)))


# ---------------------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------------------
def list_departments(active_only=False) -> list[dict]:
    where = "WHERE active = 1" if active_only else ""
    return _rows(f"SELECT * FROM departments {where} ORDER BY sort_order, id")


def get_department(department_id) -> dict | None:
    if department_id in (None, ""):
        return None
    return _one("SELECT * FROM departments WHERE id = ?", (int(department_id),))


def get_department_by_code(code: str) -> dict | None:
    return _one("SELECT * FROM departments WHERE code = ?", (str(code or "").strip().upper(),))


def create_department(name: str, code: str, sort_order: int = 0) -> int:
    now = _now()
    return _exec("INSERT INTO departments (name, code, active, sort_order, created_at, updated_at) "
                 "VALUES (?,?,1,?,?,?)", (name, code.strip().upper(), int(sort_order), now, now))


def update_department(department_id: int, **fields) -> None:
    sets, params = [], []
    for key in ("name", "code", "active", "sort_order"):
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = ?")
            params.append(fields[key])
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    _exec(f"UPDATE departments SET {', '.join(sets)} WHERE id = ?", tuple(params) + (int(department_id),))


def department_names(ids=None) -> dict:
    rows = _rows("SELECT id, name FROM departments")
    return {r["id"]: r["name"] for r in rows}


# ---------------------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------------------
def list_categories(active_only=False, caller_type=None) -> list[dict]:
    where, params = [], []
    if active_only:
        where.append("c.active = 1")
    if caller_type:
        where.append("c.caller_type = ?")
        params.append(str(caller_type))
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    return _rows(
        "SELECT c.*, d.name AS department_name, d.code AS department_code FROM categories c "
        f"LEFT JOIN departments d ON d.id = c.department_id {wsql} "
        "ORDER BY CASE c.caller_type WHEN 'employee' THEN 0 WHEN 'customer' THEN 1 ELSE 2 END, "
        "c.sort_order, c.id", tuple(params))


def get_category(category_id) -> dict | None:
    if category_id in (None, ""):
        return None
    return _one("SELECT c.*, d.name AS department_name FROM categories c "
                "LEFT JOIN departments d ON d.id = c.department_id WHERE c.id = ?", (int(category_id),))


def create_category(caller_type: str, name: str, department_id=None, keywords=None,
                    high_priority=0, sort_order=0) -> int:
    now = _now()
    return _exec(
        "INSERT INTO categories (caller_type, name, department_id, keywords, high_priority, active, "
        "sort_order, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?,?)",
        (caller_type, name, int(department_id) if department_id not in (None, "") else None,
         json.dumps(list(keywords or [])), int(bool(high_priority)), int(sort_order), now, now))


def update_category(category_id: int, **fields) -> None:
    sets, params = [], []
    for key in ("caller_type", "name", "department_id", "keywords", "high_priority", "active", "sort_order"):
        if key in fields:
            val = fields[key]
            if key == "keywords":
                val = json.dumps(list(val or []))
            elif key == "department_id":
                val = int(val) if val not in (None, "") else None
            elif val is None:
                continue
            sets.append(f"{key} = ?")
            params.append(val)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    _exec(f"UPDATE categories SET {', '.join(sets)} WHERE id = ?", tuple(params) + (int(category_id),))


def delete_category(category_id: int) -> int:
    return _exec("DELETE FROM categories WHERE id = ?", (int(category_id),))


# ---------------------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------------------
AGENT_FIELDS = ("name", "slug", "description", "prompt_template", "trigger_template",
                "voice_name", "speech_language_code", "active")


def get_agent(agent_id) -> dict | None:
    if agent_id in (None, ""):
        return None
    return _one("SELECT * FROM agents WHERE id = ?", (int(agent_id),))


def get_agent_by_slug(slug: str) -> dict | None:
    return _one("SELECT * FROM agents WHERE slug = ?", (slug,))


def list_agents(active_only=False) -> list[dict]:
    where = "WHERE active = 1" if active_only else ""
    return _rows(f"SELECT * FROM agents {where} ORDER BY id")


def update_agent(agent_id: int, **fields) -> int:
    sets, params = [], []
    for key in AGENT_FIELDS:
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = ?")
            params.append(fields[key])
    if not sets:
        return 0
    sets.append("updated_at = ?")
    params.append(_now())
    return _exec(f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", tuple(params) + (int(agent_id),))


def intake_agent() -> dict | None:
    """The agent every inbound call speaks as: the active seeded intake agent, else the
    first active agent, else None (GeminiLive then uses its identity-free fallback)."""
    a = get_agent_by_slug("epp_intake")
    if a and int(a.get("active", 1)):
        return a
    rows = list_agents(active_only=True)
    return rows[0] if rows else a


# ---------------------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------------------
def next_ticket_id(prefix: str, year: int) -> str:
    """Allocate the next EPP-YYYY-NNNNNN under the DB lock (atomic per process; the app
    runs one worker, so this is the only allocator)."""
    conn = get_conn()
    with _lock:
        row = conn.execute("SELECT next_seq FROM ticket_sequences WHERE year = ?", (int(year),)).fetchone()
        if row is None:
            seq = 1
            conn.execute("INSERT INTO ticket_sequences (year, next_seq) VALUES (?, ?)", (int(year), 2))
        else:
            seq = int(row["next_seq"])
            conn.execute("UPDATE ticket_sequences SET next_seq = ? WHERE year = ?", (seq + 1, int(year)))
        conn.commit()
    return f"{prefix}-{int(year)}-{seq:06d}"


TICKET_FIELDS = (
    "call_id", "call_sid", "caller_type", "caller_name", "company_name", "vendor_code",
    "employee_id", "caller_department", "plant_location", "contact_number", "language",
    "category", "category_original", "subcategory", "priority", "escalation_flag",
    "escalation_reason", "description", "ai_summary", "intent", "sentiment_score",
    "sentiment_label", "escalation_flags", "assigned_department_id", "assigned_user_id",
    "status", "source", "resolved_at", "closed_at",
)


def create_ticket(ticket_id: str, **fields) -> str:
    now = _now()
    cols, vals = ["ticket_id", "created_at", "updated_at"], [ticket_id, now, now]
    for key in TICKET_FIELDS:
        if key in fields and fields[key] is not None:
            val = fields[key]
            if key == "escalation_flags" and not isinstance(val, str):
                val = json.dumps(list(val))
            cols.append(key)
            vals.append(val)
    _exec(f"INSERT INTO tickets ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(vals))
    return ticket_id


def update_ticket(ticket_id: str, **fields) -> int:
    sets, params = [], []
    for key in TICKET_FIELDS:
        if key in fields:
            val = fields[key]
            if key == "escalation_flags" and not isinstance(val, str):
                val = json.dumps(list(val or []))
            if val is None and key not in ("assigned_department_id", "assigned_user_id",
                                           "sentiment_score", "resolved_at", "closed_at"):
                continue
            sets.append(f"{key} = ?")
            params.append(val)
    if not sets:
        return 0
    sets.append("updated_at = ?")
    params.append(_now())
    return _exec(f"UPDATE tickets SET {', '.join(sets)} WHERE ticket_id = ?", tuple(params) + (ticket_id,))


_TICKET_SELECT = (
    "SELECT t.*, d.name AS assigned_department, u.name AS assigned_user_name, u.username AS assigned_username "
    "FROM tickets t LEFT JOIN departments d ON d.id = t.assigned_department_id "
    "LEFT JOIN users u ON u.id = t.assigned_user_id ")


def get_ticket(ticket_id: str) -> dict | None:
    return _one(_TICKET_SELECT + "WHERE t.ticket_id = ?", (str(ticket_id),))


def ticket_by_call(call_id: str) -> dict | None:
    """The most recent ticket created on a call (a call may register more than one)."""
    if not call_id:
        return None
    return _one(_TICKET_SELECT + "WHERE t.call_id = ? ORDER BY t.created_at DESC LIMIT 1", (str(call_id),))


def tickets_by_call(call_id: str) -> list[dict]:
    if not call_id:
        return []
    return _rows(_TICKET_SELECT + "WHERE t.call_id = ? ORDER BY t.created_at", (str(call_id),))


_TICKET_SORTS = {"created_at", "updated_at", "priority", "status", "caller_type", "category", "ticket_id"}


def list_tickets(q=None, status=None, priority=None, department_id=None, caller_type=None,
                 escalated=None, date_from=None, date_to=None, assigned_user_id=None,
                 scope_department_id=None, sort="created_at", direction="desc",
                 limit=50, offset=0) -> dict:
    """scope_department_id: a dept_user's own department — rows outside it are invisible."""
    where, params = [], []
    if scope_department_id is not None:
        where.append("t.assigned_department_id = ?")
        params.append(int(scope_department_id))
    if q:
        like = f"%{q}%"
        where.append("(t.ticket_id LIKE ? OR t.caller_name LIKE ? OR t.contact_number LIKE ? "
                     "OR t.company_name LIKE ? OR t.employee_id LIKE ? OR t.vendor_code LIKE ? "
                     "OR t.description LIKE ? OR t.ai_summary LIKE ?)")
        params += [like] * 8
    if status:
        vals = [s.strip() for s in str(status).split(",") if s.strip()]
        where.append(f"t.status IN ({','.join('?' * len(vals))})")
        params += vals
    if priority:
        where.append("t.priority = ?")
        params.append(str(priority))
    if department_id not in (None, ""):
        where.append("t.assigned_department_id = ?")
        params.append(int(department_id))
    if caller_type:
        where.append("t.caller_type = ?")
        params.append(str(caller_type))
    if escalated is not None:
        where.append("t.escalation_flag = ?")
        params.append(1 if escalated else 0)
    if assigned_user_id not in (None, ""):
        where.append("t.assigned_user_id = ?")
        params.append(int(assigned_user_id))
    if date_from:
        where.append("substr(t.created_at, 1, 10) >= ?")
        params.append(str(date_from)[:10])
    if date_to:
        where.append("substr(t.created_at, 1, 10) <= ?")
        params.append(str(date_to)[:10])
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    col = sort if sort in _TICKET_SORTS else "created_at"
    if col == "priority":
        order = "CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"
    else:
        order = f"t.{col}"
    dir_sql = "ASC" if str(direction).lower() == "asc" else "DESC"
    total = _one(f"SELECT COUNT(*) c FROM tickets t {wsql}", tuple(params))["c"]
    sql = _TICKET_SELECT + f"{wsql} ORDER BY {order} {dir_sql}, t.created_at DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
    rows = _rows(sql, tuple(params))
    return {"items": rows, "total": int(total)}


def ticket_stats(scope_department_id=None, days=7) -> dict:
    """Everything the dashboard tiles and breakdowns need, in a handful of grouped queries."""
    where, params = [], []
    if scope_department_id is not None:
        where.append("assigned_department_id = ?")
        params.append(int(scope_department_id))
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    andsql = (" AND " + " AND ".join(where)) if where else ""

    def grouped(col):
        rows = _rows(f"SELECT {col} AS k, COUNT(*) n FROM tickets {wsql} GROUP BY {col}", tuple(params))
        return {str(r["k"] if r["k"] is not None else ""): int(r["n"]) for r in rows}

    by_dept_rows = _rows(
        "SELECT COALESCE(d.name, 'Unassigned') AS k, COUNT(*) n FROM tickets t "
        f"LEFT JOIN departments d ON d.id = t.assigned_department_id "
        f"{wsql.replace('assigned_department_id', 't.assigned_department_id')} GROUP BY k ORDER BY n DESC",
        tuple(params))
    open_statuses = "('open','under_review','escalated')"
    today = datetime.now(timezone.utc).date().isoformat()
    tot = _one(f"SELECT COUNT(*) c FROM tickets {wsql}", tuple(params))["c"]
    open_n = _one(f"SELECT COUNT(*) c FROM tickets WHERE status IN {open_statuses}{andsql}", tuple(params))["c"]
    high_open = _one(f"SELECT COUNT(*) c FROM tickets WHERE status IN {open_statuses} AND priority = 'high'{andsql}",
                     tuple(params))["c"]
    today_n = _one(f"SELECT COUNT(*) c FROM tickets WHERE substr(created_at,1,10) = ?{andsql}",
                   (today, *params))["c"]
    by_day = _rows(
        f"SELECT substr(created_at,1,10) AS d, COUNT(*) n FROM tickets "
        f"WHERE created_at >= date('now', ?){andsql} GROUP BY d ORDER BY d",
        (f"-{int(days)} days", *params))
    recent_escalated = _rows(
        _TICKET_SELECT + f"WHERE t.escalation_flag = 1 AND t.status IN {open_statuses}"
        f"{andsql.replace('assigned_department_id', 't.assigned_department_id')} "
        "ORDER BY t.created_at DESC LIMIT 8", tuple(params))
    return {
        "total": int(tot), "open": int(open_n), "high_open": int(high_open), "today": int(today_n),
        "by_status": grouped("status"), "by_priority": grouped("priority"),
        "by_caller_type": grouped("caller_type"), "by_category": grouped("category"),
        "by_department": {r["k"]: int(r["n"]) for r in by_dept_rows},
        "by_day": [{"date": r["d"], "tickets": int(r["n"])} for r in by_day],
        "recent_escalated": recent_escalated,
    }


# Ticket events
def add_ticket_event(ticket_id: str, kind: str, actor_type="system", actor_user_id=None,
                     actor_name="", from_value="", to_value="", note="") -> int:
    return _exec(
        "INSERT INTO ticket_events (ticket_id, actor_type, actor_user_id, actor_name, kind, "
        "from_value, to_value, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(ticket_id), actor_type, actor_user_id, actor_name or "", kind,
         str(from_value or ""), str(to_value or ""), str(note or ""), _now()))


def list_ticket_events(ticket_id: str) -> list[dict]:
    return _rows("SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY id", (str(ticket_id),))


# ---------------------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------------------
def add_audit(user_id=None, username="", action="", target="", detail="", ip="") -> int:
    return _exec(
        "INSERT INTO audit_log (user_id, username, action, target, detail, ip, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, username or "", action or "", target or "", detail or "", ip or "", _now()))


def list_audit(q=None, action=None, user_id=None, date_from=None, date_to=None,
               limit=100, offset=0) -> dict:
    where, params = [], []
    if q:
        like = f"%{q}%"
        where.append("(username LIKE ? OR action LIKE ? OR target LIKE ? OR detail LIKE ?)")
        params += [like] * 4
    if action:
        where.append("action = ?")
        params.append(str(action))
    if user_id not in (None, ""):
        where.append("user_id = ?")
        params.append(int(user_id))
    if date_from:
        where.append("substr(created_at,1,10) >= ?")
        params.append(str(date_from)[:10])
    if date_to:
        where.append("substr(created_at,1,10) <= ?")
        params.append(str(date_to)[:10])
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    total = _one(f"SELECT COUNT(*) c FROM audit_log {wsql}", tuple(params))["c"]
    rows = _rows(f"SELECT * FROM audit_log {wsql} ORDER BY id DESC LIMIT ? OFFSET ?",
                 tuple(params) + (int(limit), int(offset)))
    return {"items": rows, "total": int(total)}


def audit_actions() -> list[str]:
    return [r["action"] for r in _rows("SELECT DISTINCT action FROM audit_log ORDER BY action")]
