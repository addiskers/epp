"""/plivo/answer must read the call parameters however Plivo sends them: GET puts them in
the query string, POST (the console's default) puts them in the form body. When they were
missed, the media stream connected with no call context and the agent spoke the 374-char
fallback prompt ("our systems are down") on every real inbound call."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(fresh_eo_db, monkeypatch):
    monkeypatch.setenv("EPP_ANALYSIS_ENABLED", "false")
    import main
    monkeypatch.setattr(main, "GEMINI_API_KEY", None)      # no pre-warm → no network
    main.invalidate_ctx_cache()
    main._pending_call_meta.clear()
    with TestClient(main.app) as c:
        yield c


def _check(client, xml, uuid):
    import main
    assert xml.startswith("<?xml") and f"media-stream?call={uuid}" in xml
    assert "X-Caller=%2B919904240078" in xml
    meta = main._pending_call_meta[uuid]
    assert meta["caller"] == "+919904240078" and meta["direction"] == "inbound"
    assert len(meta["ctx"]["system_instruction"]) > 5000          # the real script, not the fallback
    assert "Welcome to EPP Composites" in meta["ctx"]["system_instruction"]
    # the intake agent's own opening trigger, not prompt_render.DEFAULT_TRIGGER
    assert "THE OPENING" in meta["ctx"]["trigger"] and "Say your opening line now" not in meta["ctx"]["trigger"]
    assert [t["name"] for t in meta["ctx"]["tools"]] == ["create_ticket", "lookup_ticket", "update_ticket", "end_call"]


def test_post_form_body_the_plivo_console_default(client):
    r = client.post("/plivo/answer",
                    data={"From": "+919904240078", "To": "+918031829020", "Direction": "inbound",
                          "CallUUID": "uuid-post-1", "CallStatus": "in-progress"})
    assert r.status_code == 200
    _check(client, r.text, "uuid-post-1")


def test_get_query_string(client):
    r = client.get("/plivo/answer", params={"From": "+919904240078", "Direction": "inbound",
                                            "CallUUID": "uuid-get-1"})
    assert r.status_code == 200
    _check(client, r.text, "uuid-get-1")


def test_query_string_wins_over_form_for_our_own_test_dials(client):
    """Outbound test dials put ?caller= on the URL we built; Plivo adds its own From (our number)
    to the body. The URL must win, and the call must be treated as a test, not an inbound call."""
    r = client.post("/plivo/answer?caller=%2B919904240078&test=1",
                    data={"From": "+918031829020", "Direction": "outbound", "CallUUID": "uuid-test-1"})
    assert r.status_code == 200
    import main
    meta = main._pending_call_meta["uuid-test-1"]
    assert meta["caller"] == "+919904240078" and meta["direction"] == "test"


def test_media_stream_link_never_depends_on_the_gemini_key(client):
    """Without ?call= the media stream cannot find its context. It must be on the URL whenever
    Plivo gave us a CallUUID, key or no key (the key only gates the pre-warm)."""
    r = client.post("/plivo/answer", data={"From": "+919904240078", "CallUUID": "uuid-nokey"})
    assert "media-stream?call=uuid-nokey" in r.text