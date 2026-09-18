"""
Outbound Plivo calls, used for one thing on the helpline: the Agents page's "Call me" test,
which rings an operator's phone so they can hear the intake script exactly as a caller
would. `place_call()` runs the blocking Plivo SDK call in a thread executor and returns a
structured dict — it never raises for normal API errors. `hangup_call()` is the bridge's
safety net for a stream that ended without the carrier leg dropping.
"""

import asyncio
import logging
import os
from urllib.parse import quote

logger = logging.getLogger(__name__)


def _base_url(base_url=None, request=None):
    """Resolve the public https base Plivo must reach for /plivo/answer."""
    public = (os.getenv("PUBLIC_URL", "") or "").rstrip("/")
    if public:
        return public
    if base_url:
        return base_url.rstrip("/")
    if request is not None:
        host = request.headers.get("host", "localhost")
        proto = "https" if request.headers.get("x-forwarded-proto") == "https" else request.url.scheme
        return f"{proto}://{host}"
    return ""


def from_numbers():
    """Configured caller-ID numbers, in order (PLIVO_FROM_NUMBER accepts a comma-separated list)."""
    raw = os.getenv("PLIVO_FROM_NUMBER", "") or ""
    return [n.strip() for n in raw.split(",") if n.strip()]


def _place_call_sync(to_number, answer_url, from_number):
    import plivo
    client = plivo.RestClient(os.getenv("PLIVO_AUTH_ID"), os.getenv("PLIVO_AUTH_TOKEN"))
    resp = client.calls.create(from_=from_number, to_=to_number, answer_url=answer_url,
                               answer_method="GET")
    return getattr(resp, "request_uuid", None) or (
        resp.get("request_uuid") if isinstance(resp, dict) else None)


async def place_call(to_number, *, base_url=None, request=None, agent_id=None):
    """Place one outbound test call that bridges to the intake agent.

    Returns {"success": True, "call_uuid": ..., "to": ..., "from": ...} or {"error": "..."}.
    `agent_id` (an int) picks which agent row speaks; the default is the intake agent."""
    if not to_number:
        return {"error": "Missing 'to' number"}
    base = _base_url(base_url=base_url, request=request)
    if not base:
        return {"error": "No PUBLIC_URL configured; cannot build answer_url"}
    if not os.getenv("PLIVO_AUTH_ID") or not os.getenv("PLIVO_AUTH_TOKEN"):
        return {"error": "Plivo credentials not configured"}
    numbers = from_numbers()
    if not numbers:
        return {"error": "PLIVO_FROM_NUMBER not configured"}
    answer_url = f"{base}/plivo/answer?caller={quote(to_number)}&test=1"
    if agent_id not in (None, ""):
        try:
            answer_url += f"&agent={int(agent_id)}"
        except (TypeError, ValueError):
            logger.warning("place_call: ignoring non-numeric agent_id=%r", agent_id)
    from_number = numbers[0]
    try:
        loop = asyncio.get_running_loop()
        request_uuid = await loop.run_in_executor(None, _place_call_sync, to_number, answer_url, from_number)
        logger.info(f"Outbound test call initiated: {request_uuid} to {to_number} from {from_number}")
        return {"success": True, "call_uuid": request_uuid, "to": to_number, "from": from_number}
    except Exception as e:
        logger.error(f"Failed to initiate Plivo call to {to_number}: {e}")
        return {"error": str(e)}


def _hangup_sync(call_uuid):
    import plivo
    client = plivo.RestClient(os.getenv("PLIVO_AUTH_ID"), os.getenv("PLIVO_AUTH_TOKEN"))
    client.calls.delete(call_uuid)


async def hangup_call(call_uuid, provider=None):
    """Hang up a live call by its Plivo CallUUID. Best-effort; never raises."""
    if not call_uuid:
        return {"error": "no call_uuid"}
    if not os.getenv("PLIVO_AUTH_ID") or not os.getenv("PLIVO_AUTH_TOKEN"):
        return {"error": "Plivo credentials not configured"}
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _hangup_sync, call_uuid)
        logger.info(f"Hung up call {call_uuid}")
        return {"ok": True}
    except Exception as e:
        logger.warning(f"Hangup failed for {call_uuid}: {e}")
        return {"error": str(e)}
