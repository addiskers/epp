"""
EPP Composites Support Helpline — the FastAPI app.

One process serves four things: the Plivo telephony webhooks (the inbound helpline itself),
a browser-mic test socket for the Agents page, the admin JSON API (eo_api), and the built
React admin SPA at /admin. Runs as ONE uvicorn worker: the pre-warm cache and the live-call
gauge are in-process.
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import sys
import time
from urllib.parse import quote, urlparse

from dotenv import load_dotenv


def _utf8_console():
    """Make stdout/stderr UTF-8 before anything logs. A Windows console defaults to cp1252,
    which cannot encode the arrows and dashes in our log messages; logging then swallows the
    error and prints "--- Logging error ---" INSTEAD of the line."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_utf8_console()

# Load .env before app modules are imported: store.py and eo_db.py resolve DATA_DIR at import time.
load_dotenv()

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from gemini_live import GeminiLive
from hallucination_guard import HallucinationGuard
from plivo_handler import PlivoMediaBridge

import agent_tools
import audit
import campaign_runner
import campaigns
import eo_api
import eo_auth
import eo_db
import known_caller
import languages
import prompt_render
import store
import tickets
from recorder import CallRecorder

logging.basicConfig(level=logging.INFO)
logging.getLogger("gemini_live").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("MODEL", "gemini-3.1-flash-live-preview")


# ---------------------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------------------
def handle_end_call(**kwargs):
    """No-op tool result; the actual hangup is driven by the 'end_call' event
    emitted from gemini_live once this tool fires."""
    return {"success": True, "instruction": "Call ending; do not speak further."}


def tool_mapping(recorder: CallRecorder, outbound=False):
    """The helpline tools, closed over THIS call's recorder so a ticket links to its call
    record. recorder.call_meta is read at call time, after recorder.open() ran. Outbound
    (campaign) calls also get record_outcome."""
    mapping = {
        "create_ticket": lambda **kw: tickets.create_from_tool(kw, recorder.call_meta),
        "lookup_ticket": lambda **kw: tickets.lookup(kw.get("ticket_number")),
        # may only touch tickets create_ticket returned on THIS call (the recorder keeps the list)
        "update_ticket": lambda **kw: tickets.update_from_tool(
            kw, recorder.call_meta, list((recorder.call or {}).get("ticket_ids") or [])),
        "end_call": handle_end_call,
    }
    if outbound:
        mapping["record_outcome"] = lambda **kw: campaigns.handle_record_outcome(kw, recorder.call_meta)
    return mapping


# ---------------------------------------------------------------------------------------
# Call context: the intake agent + routing table, rendered per call
# ---------------------------------------------------------------------------------------
# The agent row, categories, departments and tool declarations are the same for every call,
# so they are cached briefly: a burst of inbound calls must not re-read the routing table
# under the DB lock on the answer-webhook path (dead air).
_CTX_CACHE_TTL_S = 60.0
_static_cache = {"at": 0.0, "value": None}


def invalidate_ctx_cache():
    """Called after an agent / routing edit so the next call renders the new configuration."""
    _static_cache["at"] = 0.0
    _static_cache["value"] = None


def _static_context():
    now = time.monotonic()
    cached = _static_cache["value"]
    if cached is not None and (now - _static_cache["at"]) < _CTX_CACHE_TTL_S:
        return cached
    langs = languages.enabled()
    categories = eo_db.list_categories(active_only=True)
    value = {
        "agent": eo_db.intake_agent(),
        "categories": categories,
        "departments": eo_db.list_departments(active_only=True),
        "langs": langs,
        "tools": agent_tools.build_tools(categories, langs),
    }
    _static_cache["at"] = now
    _static_cache["value"] = value
    return value


def _resolve_call_context(caller="", agent_id=None):
    """The agent, rendered prompt, opening trigger and tools for one call.

    NEVER raises: on any failure it falls back to an identity-free prompt so the call still
    connects and the caller hears an apology instead of silence."""
    ctx = {"agent": None, "system_instruction": None, "trigger": "", "tools": None, "missing": [],
           "known_caller": None}
    try:
        st = _static_context()
        agent = eo_db.get_agent(agent_id) if agent_id else None
        if agent_id and not agent:
            logger.warning("Call context: agent %r not found; using the intake agent", agent_id)
        agent = agent or st["agent"]
        ctx["agent"] = agent
        # A returning caller is greeted by name in their language and not re-interrogated.
        profile = known_caller.lookup(caller) if caller else None
        ctx["known_caller"] = profile
        if profile:
            logger.info("Known caller %s: %s (%s), %d open ticket(s), language=%s", caller,
                        profile["name"], profile.get("caller_type") or "?", len(profile["open_tickets"]),
                        profile.get("language") or "-")
        rendered = prompt_render.render_prompt(
            agent, caller_phone=caller, categories=st["categories"],
            departments=st["departments"], langs=st["langs"],
            extra=known_caller.placeholders(profile), known=profile is not None)
        ctx["system_instruction"] = rendered["system_instruction"]
        ctx["trigger"] = rendered["trigger"]
        ctx["missing"] = rendered["missing"]
        ctx["tools"] = st["tools"]
    except Exception:
        logger.exception("Call context resolution failed; connecting with a generic prompt")
    return ctx


def _resolve_ws_test_context(websocket):
    """Agent + rendered prompt for a browser-mic test call on /ws. /ws is unauthenticated,
    so the agent is resolved ONLY from a short-lived signed token minted by the Agents tab."""
    token = websocket.query_params.get("test_token") or ""
    if not token:
        return None
    try:
        claims = eo_auth.verify_test_token(token)
    except Exception as exc:
        logger.warning("/ws: rejecting test token (%s)", exc)
        return None
    if not claims:
        logger.warning("/ws: test token invalid or expired")
        return None
    return _resolve_call_context(caller="", agent_id=claims.get("agent_id"))


# ---------------------------------------------------------------------------------------
# Live-call bookkeeping
# ---------------------------------------------------------------------------------------
# Live transcript watchers (the dashboard's live panel) and the calls currently connected.
live_watchers: set = set()
_active_calls: dict = {}


def active_calls():
    """The calls connected right now, for the dashboard."""
    return [dict(v, call_sid=k) for k, v in _active_calls.items()]


# Global cap on simultaneous live calls — inbound helpline calls AND campaign dials share it,
# so a campaign can never starve the helpline. Read once; the runner reads live_room().
def _max_live_calls():
    try:
        return max(1, int(os.getenv("MAX_LIVE_CALLS", "10")))
    except (TypeError, ValueError):
        return 10


MAX_LIVE_CALLS = _max_live_calls()


def live_room() -> int:
    """How many more calls may start right now (>= 0)."""
    return max(0, MAX_LIVE_CALLS - len(_active_calls))


def audit_call_outcome(call):
    """One audit row for a phone call that ended WITHOUT a ticket, so the audit log answers
    "ticket created / not created" for every call (the created ones are logged by tickets.py).
    Browser tests are skipped. Never raises."""
    try:
        call = call or {}
        if not call.get("id") or call.get("source") == "browser":
            return
        if call.get("ticket_id") or call.get("ticket_ids"):
            return
        if call.get("lookup_ticket_ids"):
            reason = "status_inquiry"
        elif call.get("outcome"):
            reason = f"outcome:{call['outcome']}"
        else:
            reason = "no_ticket"
        audit.log("call_no_ticket", user=audit.AGENT, target=f"call:{call['id']}",
                  detail={"phone": call.get("caller") or "", "reason": reason,
                          "duration_seconds": int(call.get("duration_seconds") or 0),
                          "source": call.get("source") or ""})
    except Exception:
        logger.debug("call_no_ticket audit failed", exc_info=True)


# The last time the voice model refused or dropped a session (spending cap, bad key, quota).
# A caller then hears silence; the dashboard and the browser test show this so nobody spends
# an afternoon debugging the phone line.
_GEMINI_ERROR_TTL_S = 600.0
_last_gemini_error: dict = {}


def note_gemini_error(message):
    _last_gemini_error.update({"at": time.time(), "error": str(message or "unknown error")[:300]})
    logger.error("VOICE MODEL ERROR (callers hear silence until fixed): %s", _last_gemini_error["error"])


def gemini_status() -> dict:
    """{} when healthy or the last error is stale; else {"error", "at" (ISO)}."""
    if not _last_gemini_error or (time.time() - _last_gemini_error.get("at", 0)) > _GEMINI_ERROR_TTL_S:
        return {}
    from datetime import datetime, timezone
    return {"error": _last_gemini_error["error"],
            "at": datetime.fromtimestamp(_last_gemini_error["at"], timezone.utc).isoformat()}


# Metadata stashed at /plivo/answer keyed by CallUUID; Plivo drops <Stream extraHeaders> on
# bidirectional streams.
_pending_call_meta: dict = {}

# Gemini pre-warm: sessions opened at /plivo/answer time so the ~1s connect handshake
# overlaps Plivo's stream setup instead of adding to the caller's opening dead air.
_PREWARM_TTL_S = 20.0
_prewarm_sessions: dict = {}


def _claim_prewarm(call_uuid: str):
    entry = _prewarm_sessions.pop(call_uuid or "", None)
    if not entry:
        return None
    if entry.get("call_uuid") != call_uuid:
        logger.error("PREWARM MISMATCH: session was warmed for %r but claimed by %r; "
                     "discarding and cold-connecting", entry.get("call_uuid"), call_uuid)
        return None
    logger.info(f"Claimed pre-warmed Gemini session for call {call_uuid} "
                f"(age {time.monotonic() - entry['at']:.1f}s)")
    return entry["handle"]


async def _prewarm_gemini(call_uuid: str, ctx=None):
    """Open the Live session early so its handshake overlaps Plivo's stream setup. The prompt
    and tools are frozen into the session HERE, before the media stream exists."""
    ctx = ctx or {}
    agent = ctx.get("agent") or {}
    g = GeminiLive(
        api_key=GEMINI_API_KEY, model=MODEL, input_sample_rate=16000,
        tools=[{"function_declarations": ctx.get("tools") or agent_tools.build_tools()}],
        system_instruction=ctx.get("system_instruction"),
        voice_name=agent.get("voice_name"),
        speech_language_code=agent.get("speech_language_code"),
    )
    try:
        handle = await g.open_connection()
    except Exception as e:
        logger.warning(f"Gemini pre-warm failed for {call_uuid} (call will cold-connect): {e}")
        return
    _prewarm_sessions[call_uuid] = {"handle": handle, "at": time.monotonic(), "call_uuid": call_uuid}
    await asyncio.sleep(_PREWARM_TTL_S)
    entry = _prewarm_sessions.pop(call_uuid, None)
    if entry:
        logger.info(f"Pre-warmed Gemini session for {call_uuid} unused after {_PREWARM_TTL_S:.0f}s; closing")
        try:
            await entry["handle"].__aexit__(None, None, None)
        except Exception:
            pass


def _remember_call_meta(call_uuid, caller, direction="", trigger="", context=None,
                        campaign_id=None, campaign_contact_id=None, caller_name=""):
    if not call_uuid:
        return
    _pending_call_meta[call_uuid] = {
        "caller": caller or "", "caller_name": caller_name or "", "direction": direction or "",
        "trigger": trigger or "", "ctx": context or {}, "answered_at": time.monotonic(),
        "campaign_id": campaign_id, "campaign_contact_id": campaign_contact_id,
    }
    if len(_pending_call_meta) > 200:
        for k in list(_pending_call_meta)[:50]:
            _pending_call_meta.pop(k, None)


def _resolve_identity(call_id, header_caller, header_name):
    """(caller, first_name) for a live media stream. Plivo may drop extraHeaders on
    bidirectional streams, so fall back to the metadata stashed at /plivo/answer time."""
    meta = _pending_call_meta.get(call_id or "", {})
    answered_at = meta.get("answered_at")
    if answered_at:
        logger.info(f"ANSWER-TO-STREAM: {time.monotonic() - answered_at:.2f}s from the "
                    f"/plivo/answer webhook to media-stream start (call={call_id})")
    return header_caller or meta.get("caller") or "", ""


def _resolve_trigger(call_id):
    return _pending_call_meta.get(call_id or "", {}).get("trigger") or ""


# ---------------------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        await store.init()
        await store.sweep_stale()
    except Exception as e:
        logger.error(f"Call store init failed: {e}")
    try:
        eo_db.init()
        eo_auth.seed_admin()
    except Exception as e:
        logger.error(f"Database init failed: {e}")
    try:
        await store.backfill_caller_names(
            lambda cid: next((t.get("caller_name") for t in eo_db.tickets_by_call(cid) if t.get("caller_name")), ""))
    except Exception as e:
        logger.warning(f"caller_name backfill skipped: {e}")
    logger.info("EPP helpline ready: model=%s languages=%s plivo=%s public_url=%s",
                MODEL, ",".join(languages.enabled_codes()),
                "ready" if os.getenv("PLIVO_AUTH_ID") and os.getenv("PLIVO_FROM_NUMBER") else "NOT configured",
                os.getenv("PUBLIC_URL") or "(unset)")
    runner = None
    try:
        runner = asyncio.create_task(campaign_runner.run_loop())
    except Exception as e:
        logger.error(f"Failed to start the campaign runner: {e}")
    try:
        yield
    finally:
        if runner:
            runner.cancel()
            try:
                await runner
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(lifespan=_lifespan)

_public = (os.getenv("PUBLIC_URL") or "").rstrip("/")
app.add_middleware(
    CORSMiddleware,
    # The SPA is served from this same origin; CORS only matters for the Vite dev server.
    allow_origins=[o for o in (_public, "http://localhost:5174", "http://127.0.0.1:5174") if o],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend"), name="static")
app.include_router(eo_api.router)

_ADMIN_DIST = os.path.join(os.path.dirname(__file__), "admin", "dist")
if os.path.isdir(os.path.join(_ADMIN_DIST, "assets")):
    app.mount("/admin/assets", StaticFiles(directory=os.path.join(_ADMIN_DIST, "assets")), name="admin-assets")


@app.get("/admin")
@app.get("/admin/{path:path}")
async def admin_spa(path: str = ""):
    """Serve the React admin SPA; all client routes fall back to index.html."""
    index = os.path.join(_ADMIN_DIST, "index.html")
    if not os.path.isfile(index):
        return HTMLResponse(
            "<h3>The admin SPA is not built yet. Run <code>npm install &amp;&amp; npm run build</code> in <code>admin/</code>.</h3>",
            status_code=503,
        )
    return FileResponse(index)


@app.get("/")
async def root():
    return FileResponse("frontend/index.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "live_calls": len(_active_calls), "gemini": gemini_status() or {"ok": True}}


# ---------------------------------------------------------------------------------------
# Browser-mic test socket (the Agents page)
# ---------------------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connection accepted")

    ctx = _resolve_ws_test_context(websocket) or {}
    ws_agent = ctx.get("agent") or {}

    recorder = CallRecorder(model=MODEL)
    await recorder.open(source="browser")

    audio_input_queue = asyncio.Queue()
    video_input_queue = asyncio.Queue()
    text_input_queue = asyncio.Queue()
    client_disconnected = False

    async def audio_output_callback(data):
        if not client_disconnected:
            try:
                await websocket.send_bytes(data)
            except Exception:
                pass

    async def audio_interrupt_callback():
        pass

    gemini_client = GeminiLive(
        api_key=GEMINI_API_KEY, model=MODEL, input_sample_rate=16000,
        tools=[{"function_declarations": ctx.get("tools") or agent_tools.build_tools()}],
        system_instruction=ctx.get("system_instruction"),
        voice_name=ws_agent.get("voice_name"),
        speech_language_code=ws_agent.get("speech_language_code"),
        tool_mapping=tool_mapping(recorder),
    )

    session_task = None

    async def receive_from_client():
        nonlocal client_disconnected
        try:
            while True:
                message = await websocket.receive()
                if message.get("bytes"):
                    await audio_input_queue.put(message["bytes"])
                elif message.get("text"):
                    text = message["text"]
                    try:
                        payload = json.loads(text)
                        if isinstance(payload, dict) and payload.get("type") == "image":
                            await video_input_queue.put(base64.b64decode(payload["data"]))
                            continue
                    except json.JSONDecodeError:
                        pass
                    await text_input_queue.put(text)
        except WebSocketDisconnect:
            logger.info("WebSocket disconnected")
        except Exception as e:
            logger.error(f"Error receiving from client: {e}")
        finally:
            client_disconnected = True
            if session_task and not session_task.done():
                session_task.cancel()

    receive_task = asyncio.create_task(receive_from_client())

    MAX_RETRIES = 3
    RETRY_DELAYS = [2, 4, 8]
    ending = False
    # Same guard as the phone bridge: a spoken reference number with no ticket behind it is
    # pushed back once the turn completes.
    guard = HallucinationGuard(call_label="browser")
    turn_text = ""

    async def run_session_with_retry():
        nonlocal ending, turn_text
        # The opening trigger: the Live API produces no audio until it receives a turn.
        await text_input_queue.put(ctx.get("trigger") or prompt_render.DEFAULT_TRIGGER)
        for attempt in range(MAX_RETRIES + 1):
            should_retry = False
            try:
                async for event in gemini_client.start_session(
                    audio_input_queue=audio_input_queue,
                    video_input_queue=video_input_queue,
                    text_input_queue=text_input_queue,
                    audio_output_callback=audio_output_callback,
                    audio_interrupt_callback=audio_interrupt_callback,
                ):
                    if not event:
                        continue
                    etype = event.get("type")
                    if etype == "error":
                        note_gemini_error(event.get("error", ""))
                    if etype == "error" and attempt < MAX_RETRIES:
                        error_msg = event.get("error", "")
                        if "exhausted" in error_msg or "quota" in error_msg.lower():
                            delay = RETRY_DELAYS[attempt]
                            logger.warning(f"Quota error, retrying in {delay}s (attempt {attempt+1}/{MAX_RETRIES})")
                            try:
                                await websocket.send_json({"type": "status", "text": "Reconnecting..."})
                            except RuntimeError:
                                return
                            await asyncio.sleep(delay)
                            should_retry = True
                            break
                    if etype == "go_away" and attempt < MAX_RETRIES:
                        logger.info(f"GoAway received, reconnecting (attempt {attempt+1}/{MAX_RETRIES})")
                        try:
                            await websocket.send_json({"type": "status", "text": "Reconnecting..."})
                        except RuntimeError:
                            return
                        await asyncio.sleep(1)
                        should_retry = True
                        break
                    await recorder.on_event(event)
                    try:
                        await websocket.send_json(event)
                    except RuntimeError:
                        return
                    if etype == "gemini":
                        turn_text += " " + (event.get("text") or "")
                    elif etype == "tool_call":
                        guard.on_tool_call(event.get("name"), event.get("result"))
                    elif etype in ("turn_complete", "interrupted"):
                        nudge = guard.check(turn_text) if etype == "turn_complete" else None
                        turn_text = ""
                        if nudge and not ending:
                            await text_input_queue.put(nudge)
                    # Gemini interleaves end_call with the closing's audio: keep draining this
                    # turn and close on its turn_complete (a watchdog bounds the wait).
                    if etype == "end_call":
                        if not ending:
                            ending = True
                            asyncio.get_running_loop().call_later(
                                12.0,
                                lambda: (session_task and not session_task.done()
                                         and session_task.cancel()))
                        continue
                    if ending and etype in ("turn_complete", "interrupted", "error"):
                        return
                if not should_retry:
                    return
            except Exception as e:
                if attempt < MAX_RETRIES:
                    delay = RETRY_DELAYS[attempt]
                    logger.warning(f"Session error, retrying in {delay}s: {e}")
                    await asyncio.sleep(delay)
                else:
                    raise

    try:
        session_task = asyncio.create_task(run_session_with_retry())
        await session_task
    except asyncio.CancelledError:
        logger.info("Gemini session cancelled due to client disconnect")
    except Exception as e:
        import traceback
        logger.error(f"Error in Gemini session: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally:
        receive_task.cancel()
        await recorder.close()
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("connection closed")


# ---------------------------------------------------------------------------------------
# Plivo voice endpoints — the helpline itself
# ---------------------------------------------------------------------------------------
@app.api_route("/plivo/answer", methods=["GET", "POST"])
async def plivo_answer(request: Request):
    """Plivo answer webhook: returns streaming XML when a call connects (inbound helpline
    calls, and the Agents page's outbound test calls)."""
    public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    if public_url:
        parsed = urlparse(public_url)
        host = parsed.netloc
        secure = parsed.scheme == "https"
    else:
        host = request.headers.get("host", "localhost")
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
    ws_url = f"{'wss' if secure else 'ws'}://{host}/plivo/media-stream"

    # Plivo sends the call parameters (From, CallUUID, Direction…) in the query string on a
    # GET application and in the form body on a POST one — the console defaults to POST.
    # Read both; the query string wins because the parameters WE put on an outbound answer
    # URL (?caller=, ?campaign=, ?cc=) must never be shadowed by Plivo's own fields.
    qp = dict(request.query_params)
    if request.method == "POST":
        try:
            for key, value in (await request.form()).items():
                qp.setdefault(key, str(value))
        except Exception as e:
            logger.warning("/plivo/answer: could not read the form body (%s)", e)
    # ?caller= exists only on answer URLs WE built for outbound dials (a test call, or a
    # campaign dial carrying ?campaign=&cc=); a genuine inbound call carries the number in `From`.
    # Plivo's From arrives as "919876543210" (no plus); store and speak it as E.164 like every
    # other number in the system, so call records and the known-caller match line up.
    raw_from = qp.get("From") or qp.get("from") or ""
    caller = qp.get("caller") or (tickets._clean_phone(raw_from) or raw_from)
    call_uuid = qp.get("CallUUID") or qp.get("callUUID") or qp.get("RequestUUID") or ""
    campaign_id = cc_id = None
    try:
        if qp.get("campaign") and qp.get("cc"):
            campaign_id, cc_id = int(qp["campaign"]), int(qp["cc"])
    except (TypeError, ValueError):
        campaign_id = cc_id = None
    direction = "campaign" if campaign_id else ("test" if qp.get("caller") else "inbound")
    logger.info(f"{direction.upper()} call {call_uuid or '-'} to/from {caller or 'unknown'}"
                + (f" (campaign {campaign_id}, contact {cc_id})" if campaign_id else ""))

    if campaign_id:
        call_ctx = campaigns.call_context(campaign_id, cc_id, caller=caller)
    else:
        call_ctx = _resolve_call_context(caller=caller, agent_id=qp.get("agent") or None)
    if call_ctx.get("missing"):
        logger.warning("Call %s: prompt has unresolved placeholders %s", call_uuid or "-", call_ctx["missing"])

    known_name = (call_ctx.get("known_caller") or {}).get("name") or call_ctx.get("caller_name") or ""
    _remember_call_meta(call_uuid, caller, direction=direction, trigger=call_ctx.get("trigger") or "",
                        context=call_ctx, campaign_id=campaign_id, campaign_contact_id=cc_id,
                        caller_name=known_name)
    if call_uuid:
        # The media stream finds THIS call's context by this id; without it the session
        # falls back to the identity-free "systems are down" prompt.
        ws_url += f"?call={quote(call_uuid)}"
        if GEMINI_API_KEY:
            asyncio.create_task(_prewarm_gemini(call_uuid, call_ctx))
    else:
        logger.error("/plivo/answer: no CallUUID in the request — the media stream will connect "
                     "WITHOUT the helpline script. Check the Plivo application's answer method/URL.")
    eh_attr = f' extraHeaders="X-Caller={quote(caller)}"' if caller else ""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        '  <Stream bidirectional="true" keepCallAlive="true" '
        f'contentType="audio/x-mulaw;rate=8000" audioTrack="inbound"{eh_attr}>'
        f'{ws_url}</Stream>\n'
        '</Response>'
    )
    return Response(content=xml, media_type="application/xml")


@app.websocket("/plivo/media-stream")
async def plivo_media_stream(websocket: WebSocket):
    """WebSocket endpoint for Plivo bidirectional audio streaming."""
    await websocket.accept()
    logger.info("Plivo Media Stream WebSocket accepted")
    call_uuid = websocket.query_params.get("call") or ""
    preopened = _claim_prewarm(call_uuid)

    meta = _pending_call_meta.get(call_uuid) or {}
    ctx = meta.get("ctx") or {}
    call_agent = ctx.get("agent") or {}
    direction = meta.get("direction")
    source = {"test": "plivo", "campaign": "plivo_campaign"}.get(direction, "plivo_inbound")
    is_campaign = bool(meta.get("campaign_id"))

    recorder = CallRecorder(model=MODEL)
    gemini_client = GeminiLive(
        api_key=GEMINI_API_KEY, model=MODEL, input_sample_rate=16000,
        tools=[{"function_declarations": ctx.get("tools") or agent_tools.build_tools()}],
        system_instruction=ctx.get("system_instruction"),
        voice_name=call_agent.get("voice_name"),
        speech_language_code=call_agent.get("speech_language_code"),
        tool_mapping=tool_mapping(recorder, outbound=is_campaign),
    )

    async def broadcast_event(event):
        """Persist via the recorder AND fan out to the live watchers."""
        etype = event.get("type")
        if etype == "call_start":
            m = _pending_call_meta.pop(event.get("call_sid") or "", {})
            caller = m.get("caller") or event.get("caller") or ""
            await recorder.open(source=source, call_sid=event.get("call_sid") or None, caller=caller,
                                campaign_id=m.get("campaign_id") or meta.get("campaign_id"),
                                campaign_contact_id=m.get("campaign_contact_id") or meta.get("campaign_contact_id"),
                                caller_name=m.get("caller_name") or meta.get("caller_name") or "")
            sid = event.get("call_sid") or ""
            if sid:
                _active_calls[sid] = {"caller": caller, "started_at": time.time(),
                                      "call_id": (recorder.call or {}).get("id"), "source": source}
            event = dict(event, call_id=(recorder.call or {}).get("id"), caller=caller, source=source)
        elif etype == "call_end":
            _active_calls.pop(event.get("call_sid") or recorder.call_meta.get("call_sid") or "", None)
            await recorder.close()
            event = dict(event, call_id=(recorder.call or {}).get("id"),
                         ticket_id=(recorder.call or {}).get("ticket_id"))
            audit_call_outcome(recorder.call)
        else:
            await recorder.on_event(event)
            if etype == "error":
                note_gemini_error(event.get("error", ""))
            if etype == "tool_call":
                # Never fan the caller's details out to a watcher: only the fact and the id.
                res = event.get("result") or {}
                event = {"type": "tool_call", "name": event.get("name"),
                         "result": {k: res.get(k) for k in ("ok", "found", "ticket_id", "status") if k in res}}
        dead = set()
        for watcher in live_watchers:
            try:
                await watcher.send_json(event)
            except Exception:
                dead.add(watcher)
        live_watchers.difference_update(dead)

    bridge = PlivoMediaBridge(
        websocket=websocket,
        gemini_client=gemini_client,
        text_trigger=prompt_render.DEFAULT_TRIGGER,
        on_event=broadcast_event,
        resolve_identity=_resolve_identity,
        resolve_trigger=_resolve_trigger,
        preopened=preopened,
    )
    try:
        await bridge.run()
    except Exception as e:
        import traceback
        logger.error(f"Plivo bridge error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally:
        _active_calls.pop(bridge.call_id or "", None)
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------------------
# Live transcript watchers (the dashboard) — token-gated
# ---------------------------------------------------------------------------------------
@app.websocket("/live/ws")
async def live_ws(websocket: WebSocket):
    token = websocket.query_params.get("token") or ""
    if not eo_auth.verify_live_token(token):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    live_watchers.add(websocket)
    logger.info(f"Live watcher connected ({len(live_watchers)} total)")
    try:
        await websocket.send_json({"type": "active_calls", "calls": active_calls()})
        while True:
            await websocket.receive_text()  # keep alive
    except Exception:
        pass
    finally:
        live_watchers.discard(websocket)
        logger.info(f"Live watcher disconnected ({len(live_watchers)} total)")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
