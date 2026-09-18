"""recorder.py — ticket capture from tool results, language labelling, post-call handoff."""

import asyncio

import recorder as recorder_mod
import tickets
from recorder import CallRecorder


def _rec_with_call(**over):
    r = CallRecorder(model="test")
    r.call = {"id": "c1", "call_sid": "sid1", "caller": "+919000000001", "source": "plivo_inbound",
              "ticket_id": None, "ticket_ids": [], "lookup_ticket_ids": [],
              "transcript": [], "tool_calls": []}
    r.call.update(over)
    return r


def _tool(name, args, result):
    return {"type": "tool_call", "name": name, "args": args, "result": result}


def test_create_ticket_result_is_captured_on_the_call():
    r = _rec_with_call()
    r._record_tool(_tool("create_ticket", {"language": "hi", "caller_type": "employee"},
                         {"ok": True, "ticket_id": "EPP-2026-000007"}))
    assert r.call["ticket_id"] == "EPP-2026-000007"
    assert r.call["ticket_ids"] == ["EPP-2026-000007"]
    assert r.call["ticket_language"] == "hi"
    assert r.call["tool_calls"][0]["name"] == "create_ticket"


def test_a_failed_create_ticket_records_nothing():
    r = _rec_with_call()
    r._record_tool(_tool("create_ticket", {}, {"ok": False, "error": "boom"}))
    assert r.call["ticket_id"] is None and r.call["ticket_ids"] == []


def test_second_ticket_on_the_same_call_is_appended():
    r = _rec_with_call()
    r._record_tool(_tool("create_ticket", {}, {"ok": True, "ticket_id": "EPP-2026-000001"}))
    r._record_tool(_tool("create_ticket", {}, {"ok": True, "ticket_id": "EPP-2026-000002"}))
    assert r.call["ticket_id"] == "EPP-2026-000002"
    assert r.call["ticket_ids"] == ["EPP-2026-000001", "EPP-2026-000002"]


def test_lookup_ids_are_kept_only_when_found():
    r = _rec_with_call()
    r._record_tool(_tool("lookup_ticket", {"ticket_number": "5"}, {"found": True, "ticket_id": "EPP-2026-000005"}))
    r._record_tool(_tool("lookup_ticket", {"ticket_number": "9"}, {"found": False}))
    assert r.call["lookup_ticket_ids"] == ["EPP-2026-000005"]
    assert r.call["ticket_id"] is None


def test_call_meta_links_a_ticket_to_its_call():
    r = _rec_with_call()
    assert r.call_meta == {"call_id": "c1", "call_sid": "sid1", "caller": "+919000000001",
                           "campaign_id": None, "campaign_contact_id": None}
    assert CallRecorder().call_meta == {"call_id": None, "call_sid": None, "caller": None,
                                        "campaign_id": None, "campaign_contact_id": None}


def test_language_inference_covers_every_script_and_romanised_hindi():
    assert recorder_mod._infer_language("என் சம்பளம் வரவில்லை") == "ta"
    assert recorder_mod._infer_language("మా ఆర్డర్ రాలేదు") == "te"
    assert recorder_mod._infer_language("ਮੇਰੀ ਤਨਖਾਹ") == "pa"
    assert recorder_mod._infer_language("મારો પગાર") == "gu"
    assert recorder_mod._infer_language("मेरी सैलरी नहीं आई") == "hi"
    assert recorder_mod._infer_language("haan ji, mera salary nahi aaya") == "hi"
    assert recorder_mod._infer_language("hello, my salary has not come") == "en"
    assert recorder_mod._infer_language("...") == "unknown"


def test_call_language_prefers_what_the_caller_chose_on_the_ticket():
    r = _rec_with_call()
    for text in ("hello", "hello yes", "i want to complain"):
        r._accumulate_turn("user", text)
        r._flush_turn()
    assert r._final_language() == "en"
    r.call["ticket_language"] = "mr"                      # chose Marathi on the ticket
    assert r._final_language() == "mr"


def test_call_language_falls_back_to_the_dominant_non_english_turn():
    r = _rec_with_call()
    for text in ("hello", "haan ji, theek hai, aa jaunga", "nahi bacche nahi aayenge"):
        r._accumulate_turn("user", text)
        r._flush_turn()
    assert r.call["language"] == "en"            # first-turn label, until close()
    assert r._final_language() == "hi"


def test_close_hands_the_call_to_post_call_analysis(monkeypatch):
    scheduled = []
    monkeypatch.setattr(tickets, "schedule_post_call", lambda cid: scheduled.append(cid))

    async def run():
        r = CallRecorder(model="test")
        await r.open(source="browser")
        r._accumulate_turn("user", "my salary is late")
        await r.close()
        return r.call["id"], r.call["status"], r.call["transcript"]
    cid, status, transcript = asyncio.run(run())
    assert scheduled == [cid]
    assert status == "completed" and transcript[0]["text"] == "my salary is late"
