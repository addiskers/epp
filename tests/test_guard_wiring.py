"""The hallucination guard is wired into the phone bridge: a completed agent turn that
announces a reference with no ticket queues the nudge; a real create_ticket silences it."""
import asyncio

from plivo_handler import PlivoMediaBridge


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _run_events(events):
    class FakeGemini:
        async def start_session(self, **kw):
            for ev in events:
                yield ev

    async def run():
        b = PlivoMediaBridge(FakeWS(), gemini_client=None, text_trigger="[go]")
        b.gemini = FakeGemini()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._spoke_since_user = True
        await b._gemini_loop()
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return msgs, b._guard.nudged
    return asyncio.run(run())


def test_invented_reference_is_pushed_back_on_the_phone(monkeypatch):
    monkeypatch.setenv("EPP_TICKET_PREFIX", "TKT")
    msgs, nudged = _run_events([
        {"type": "gemini", "text": "Your concern has been successfully registered. "},
        {"type": "gemini", "text": "Your reference number is T K T two zero two six zero zero zero one two three."},
        {"type": "turn_complete"},
    ])
    assert nudged == 1 and any("create_ticket" in m for m in msgs)


def test_real_ticket_is_never_pushed_back(monkeypatch):
    monkeypatch.setenv("EPP_TICKET_PREFIX", "TKT")
    msgs, nudged = _run_events([
        {"type": "tool_call", "name": "create_ticket", "args": {},
         "result": {"ok": True, "ticket_id": "TKT-2026-000004"}},
        {"type": "gemini", "text": "Your reference number is T, K, T — two zero two six — zero zero zero zero zero four."},
        {"type": "turn_complete"},
    ])
    assert nudged == 0 and not any("create_ticket" in m for m in msgs)


def test_asking_for_a_reference_is_fine(monkeypatch):
    monkeypatch.setenv("EPP_TICKET_PREFIX", "TKT")
    msgs, nudged = _run_events([
        {"type": "gemini", "text": "Do you have your reference number?"},
        {"type": "turn_complete"},
    ])
    assert nudged == 0 and msgs == []
