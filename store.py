"""
JSON file persistence for call records — one file per call.

Each call is stored at  DATA_DIR/calls/<call_id>.json  (human-readable). One file per
call means each live call owns its own file, so there is no concurrent-writer contention.
Writes are atomic (temp file + os.replace).

A lightweight in-memory index (everything except the transcript/tool_calls arrays) is kept
so list/summary endpoints don't re-read every file. Full records are only read from disk
for the call-detail view.

All public functions are async and run blocking disk I/O in a thread executor so they
never block the FastAPI event loop.
"""

import asyncio
import copy
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")
CALLS_DIR = os.path.join(DATA_DIR, "calls")
RECORDINGS_DIR = os.path.join(DATA_DIR, "recordings")


def recording_path(key: str) -> str:
    """Absolute path of a call's audio recording (WAV), keyed by call_sid. Ensures the dir."""
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    return os.path.join(RECORDINGS_DIR, f"{key}.wav")


def has_recording(key: str) -> bool:
    return bool(key) and os.path.isfile(os.path.join(RECORDINGS_DIR, f"{key}.wav"))


# call_id -> lightweight meta (full record minus transcript/tool_calls)
_INDEX = {}
_LOCK = threading.Lock()

# Heavy fields excluded from the in-memory index.
_HEAVY_FIELDS = ("transcript", "tool_calls")


def _meta_from_call(call):
    return copy.deepcopy({k: v for k, v in call.items() if k not in _HEAVY_FIELDS})


def _path(call_id):
    return os.path.join(CALLS_DIR, f"{call_id}.json")


# Sync internals (run inside executor)

def _init_sync():
    os.makedirs(CALLS_DIR, exist_ok=True)
    with _LOCK:
        _INDEX.clear()
        for name in os.listdir(CALLS_DIR):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(CALLS_DIR, name), "r", encoding="utf-8") as f:
                    call = json.load(f)
                _INDEX[call["id"]] = _meta_from_call(call)
            except Exception as e:
                logger.warning(f"Skipping unreadable call file {name}: {e}")
    logger.info(f"Call store initialized at {CALLS_DIR} ({len(_INDEX)} calls)")


def _save_sync(call):
    os.makedirs(CALLS_DIR, exist_ok=True)
    path = _path(call["id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(call, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)
    with _LOCK:
        _INDEX[call["id"]] = _meta_from_call(call)


def _load_sync(call_id):
    try:
        with open(_path(call_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f"Failed to load call {call_id}: {e}")
        return None


# Async public API

async def _run(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


async def init():
    await _run(_init_sync)


def _backfill_names_sync(name_for_call):
    """Records from before `caller_name` existed get it from their ticket, once. A record is
    touched only when the key is missing altogether, so this is a no-op after the first boot."""
    with _LOCK:
        todo = [m["id"] for m in _INDEX.values() if "caller_name" not in m]
    done = 0
    for call_id in todo:
        call = _load_sync(call_id)
        if not call:
            continue
        name = ""
        if call.get("ticket_id"):
            try:
                name = str(name_for_call(call_id) or "").strip()
            except Exception:
                name = ""
        call["caller_name"] = name
        try:
            _save_sync(call)
            done += 1
        except Exception as e:
            logger.warning(f"caller_name backfill: could not save {call_id}: {e}")
    if done:
        logger.info(f"Call store: caller_name filled in on {done} older call record(s)")
    return done


async def backfill_caller_names(name_for_call):
    """`name_for_call(call_id) -> str` (the ticket's caller name). Runs once per boot."""
    return await _run(_backfill_names_sync, name_for_call)


async def save_call(call):
    await _run(_save_sync, call)


async def load_call(call_id):
    return await _run(_load_sync, call_id)


def _date_of(meta):
    s = meta.get("started_at") or ""
    return s[:10]


def _matches(meta, filters):
    src = filters.get("source")
    if src and meta.get("source") != src:
        return False
    since = filters.get("since")
    if since and (meta.get("started_at") or "") < since:
        return False
    if filters.get("with_ticket") is not None:
        want = filters["with_ticket"] in (True, "1", "true", "yes")
        if bool(meta.get("ticket_id")) != want:
            return False
    ids = filters.get("call_ids")
    if ids is not None and meta.get("id") not in ids:
        return False
    cid = filters.get("campaign_id")
    if cid not in (None, "") and str(meta.get("campaign_id") or "") != str(cid):
        return False
    frm = filters.get("from")
    to = filters.get("to")
    d = _date_of(meta)
    if frm and d and d < frm:
        return False
    if to and d and d > to:
        return False
    q = (filters.get("q") or "").strip().lower()
    if q:
        hay = " ".join(str(meta.get(k, "")) for k in
                       ("caller", "caller_name", "call_sid", "language", "status", "source", "ticket_id")).lower()
        if q not in hay:
            # digit-normalized phone match: "98240 18000" / "98240-18000" still hits +919824018000
            q_digits = re.sub(r"\D", "", q)
            digits_hit = bool(q_digits) and q_digits in re.sub(r"\D", "", str(meta.get("caller") or ""))
            if not digits_hit:
                return False
    return True


async def find_campaign_call(campaign_contact_id, since_iso=None):
    """Latest call record for one campaign contact, started at/after since_iso. The campaign
    runner uses it to tell an answered dial from a ring-out. Index-only (no disk)."""
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


async def list_calls(filters=None):
    """Return {items: [...meta], total: n} filtered + paginated, newest first."""
    filters = filters or {}
    with _LOCK:
        metas = list(_INDEX.values())
    rows = [m for m in metas if _matches(m, filters)]
    rows.sort(key=lambda m: m.get("started_at") or "", reverse=True)
    total = len(rows)
    offset = int(filters.get("offset") or 0)
    limit = filters.get("limit")
    if limit is not None:
        rows = rows[offset:offset + int(limit)]
    elif offset:
        rows = rows[offset:]
    return {"items": rows, "total": total}


async def summary(filters=None):
    """Aggregate call volume, duration and (admin-only) cost across the filtered calls."""
    filters = filters or {}
    with _LOCK:
        metas = [m for m in _INDEX.values() if _matches(m, filters)]

    now = datetime.now(timezone.utc)
    month_prefix = now.strftime("%Y-%m")

    total_cost = 0.0
    total_secs = 0
    with_ticket = 0
    by_source = {}
    by_lang = {}
    by_day = {}
    month_calls = 0

    for m in metas:
        g = m.get("gemini_cost_usd") or 0.0
        total_cost += g
        total_secs += m.get("duration_seconds") or 0
        if m.get("ticket_id"):
            with_ticket += 1
        src = m.get("source") or "unknown"
        by_source[src] = by_source.get(src, 0) + 1
        lang = m.get("language") or "unknown"
        by_lang[lang] = by_lang.get(lang, 0) + 1
        d = _date_of(m)
        if d:
            day = by_day.setdefault(d, {"date": d, "calls": 0, "cost_usd": 0.0})
            day["calls"] += 1
            day["cost_usd"] = round(day["cost_usd"] + g, 6)
        if d.startswith(month_prefix):
            month_calls += 1

    total_calls = len(metas)
    return {
        "total_calls": total_calls,
        "by_source": by_source,
        "by_language": by_lang,
        "total_minutes": round(total_secs / 60.0, 2),
        "total_seconds": total_secs,
        "avg_duration_seconds": round(total_secs / total_calls) if total_calls else 0,
        "calls_with_ticket": with_ticket,
        "ticket_rate": round(with_ticket / total_calls, 4) if total_calls else 0.0,
        "total_cost_usd": round(total_cost, 6),
        "avg_cost_per_call": round(total_cost / total_calls, 6) if total_calls else 0.0,
        "this_month": {"calls": month_calls},
        "by_day": sorted(by_day.values(), key=lambda x: x["date"]),
    }


async def sweep_stale(max_age_minutes=30):
    """Mark long-running 'in_progress' records (orphaned by a crash) as abandoned."""
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_minutes * 60
    with _LOCK:
        stale_ids = []
        for cid, m in _INDEX.items():
            if m.get("status") != "in_progress":
                continue
            try:
                started = datetime.fromisoformat(m["started_at"]).timestamp()
            except Exception:
                continue
            if started < cutoff:
                stale_ids.append(cid)
    for cid in stale_ids:
        call = await load_call(cid)
        if call and call.get("status") == "in_progress":
            call["status"] = "abandoned"
            await save_call(call)
            logger.info(f"Marked stale call {cid} as abandoned")
