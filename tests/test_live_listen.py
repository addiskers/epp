"""/live/listen/{call_sid}: an admin hears one live call. Refusals are sent as close codes the
browser can read (accept-then-close); a listener is written to the audit log."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import eo_auth
from plivo_handler import PlivoMediaBridge

ADMIN_PASS = "secret-pass-1"


@pytest.fixture
def client(fresh_eo_db, monkeypatch):
    monkeypatch.setenv("EPP_ADMIN_USER", "admin")
    monkeypatch.setenv("EPP_ADMIN_PASS", ADMIN_PASS)
    monkeypatch.setenv("EPP_ANALYSIS_ENABLED", "false")
    eo_auth.reset_throttle()
    import main
    main.invalidate_ctx_cache()
    main._bridges.clear()
    with TestClient(main.app) as c:
        yield c
    main._bridges.clear()
    eo_auth.reset_throttle()


def _headers(client):
    r = client.post("/api/epp/login", json={"username": "admin", "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}, r.json()["token"]


def _close_code(client, url):
    with client.websocket_connect(url) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    return e.value.code


def test_refused_without_a_live_token(client):
    h, session = _headers(client)
    assert _close_code(client, "/live/listen/sid1") == 4401
    assert _close_code(client, "/live/listen/sid1?token=garbage") == 4401
    assert _close_code(client, f"/live/listen/sid1?token={session}") == 4401     # a session token is not a live token


def test_unknown_call_is_4404(client):
    h, _ = _headers(client)
    token = client.post("/api/epp/live/token", headers=h).json()["token"]
    assert _close_code(client, f"/live/listen/no-such-call?token={token}") == 4404


def test_listening_starts_with_a_header_and_is_audited(client, fresh_eo_db):
    import main
    h, _ = _headers(client)
    token = client.post("/api/epp/live/token", headers=h).json()["token"]
    bridge = PlivoMediaBridge(None, gemini_client=None, text_trigger="[go]")
    main._bridges["sid1"] = bridge
    main._active_calls["sid1"] = {"caller": "+919904240078", "call_id": "call-x1", "started_at": 0, "source": "plivo_inbound"}
    try:
        with client.websocket_connect(f"/live/listen/sid1?token={token}") as ws:
            first = ws.receive_json()
            assert first == {"type": "listen_start", "call_sid": "sid1", "rate": 8000, "format": "pcm16"}
            assert len(bridge._listeners) == 1
    finally:
        main._active_calls.pop("sid1", None)
    rows = [a for a in fresh_eo_db.list_audit()["items"] if a["action"] == "call_listened"]
    assert len(rows) == 1 and rows[0]["target"] == "call:call-x1" and rows[0]["username"] == "admin"
    assert '"phone": "+919904240078"' in rows[0]["detail"]
