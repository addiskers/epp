"""Calling-hours math for the campaign dialer — pure functions, stdlib only (zoneinfo).

No I/O, no Plivo, no app imports — safe to unit-test in isolation. Windows are expressed
as minutes since midnight in the calling timezone (default Asia/Kolkata), so "09:00-21:00"
is (540, 1260). An overnight window (end <= start) wraps past midnight.
"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def tz():
    try:
        return ZoneInfo(os.getenv("EPP_CALL_TZ") or os.getenv("CALLBACK_TZ") or "Asia/Kolkata")
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def hhmm_to_min(s, default=0):
    """'09:00' -> 540. Returns `default` on bad input."""
    try:
        hh, mm = str(s).split(":")
        return (int(hh) % 24) * 60 + (int(mm) % 60)
    except Exception:
        return default


def now_ist_min():
    """Current minutes-since-midnight in the calling timezone."""
    n = datetime.now(timezone.utc).astimezone(tz())
    return n.hour * 60 + n.minute


def in_call_window(start_min, end_min, now_min=None):
    """True if now is inside [start, end). Handles an overnight window (end <= start).
    No restriction when either bound is None or start == end."""
    if start_min is None or end_min is None:
        return True
    s, e = int(start_min), int(end_min)
    if s == e:
        return True
    m = now_ist_min() if now_min is None else int(now_min)
    return (s <= m < e) if s < e else (m >= s or m < e)


def global_window():
    """Default calling window (start_min, end_min) — env-tunable."""
    return (hhmm_to_min(os.getenv("EPP_CALL_WINDOW_START") or os.getenv("EO_CALL_WINDOW_START") or "09:00", 540),
            hhmm_to_min(os.getenv("EPP_CALL_WINDOW_END") or os.getenv("EO_CALL_WINDOW_END") or "21:00", 1260))


def campaign_window(campaign) -> tuple:
    """(start_min, end_min) for a campaign row, with the standard 09:00-21:00 defaults.
    The ONE place that parses call_start_min/call_end_min — the runner's resume math and
    the admin's 'Waiting for calling hours' label must never disagree."""
    def _m(v, default):
        try:
            return int(v) if v is not None else default
        except (TypeError, ValueError):
            return default
    if not campaign:
        return global_window()
    return (_m(campaign.get("call_start_min"), 540), _m(campaign.get("call_end_min"), 1260))
