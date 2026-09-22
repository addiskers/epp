"""Audit log: who did what, to which record, from where.

One helper, called from the API layer for every action that matters — logins (and
failed logins), user and configuration changes, ticket status / assignment / notes, and
every transcript or recording view. Reading a grievance transcript is itself an event
worth recording under a data-privacy regime.

Never raises: an audit failure must not turn a successful action into an error.
"""

import json
import logging

import eo_db

logger = logging.getLogger(__name__)

# The actor for rows the voice agent writes on its own (a ticket registered on a call, a call
# that ended without one). No user id: it is not a login.
AGENT = {"id": None, "username": "helpline-agent"}


def _ip(request):
    if request is None:
        return ""
    try:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else ""
    except Exception:
        return ""


def log(action, user=None, target="", detail=None, request=None):
    """Record one audit row. `detail` is any JSON-serialisable value (kept small)."""
    try:
        payload = ""
        if detail not in (None, "", {}):
            payload = json.dumps(detail, ensure_ascii=False, default=str)[:2000]
        eo_db.add_audit(
            user_id=(user or {}).get("id"),
            username=(user or {}).get("username") or "",
            action=str(action or "")[:80],
            target=str(target or "")[:200],
            detail=payload,
            ip=_ip(request)[:64],
        )
    except Exception:
        logger.debug("audit log write failed", exc_info=True)
