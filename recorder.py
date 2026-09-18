"""
CallRecorder — taps the live event stream of a single call, accumulates the transcript +
real token usage, computes cost, persists via store.py, and hands the finished call to the
post-call analysis.

One instance per call. It is fed the SAME events that already drive the live viewer, so
it never changes call behavior. Every persistence call is guarded so a storage failure
can never break an in-progress call.
"""

import logging
import uuid
from datetime import datetime, timezone

import languages
import pricing
import store

logger = logging.getLogger(__name__)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _in_bucket(mod):
    m = str(mod).upper()
    if "AUDIO" in m:
        return "audio_in"
    if "IMAGE" in m or "VIDEO" in m:
        return "imgvid_in"
    return "text_in"


def _out_bucket(mod):
    m = str(mod).upper()
    if "AUDIO" in m:
        return "audio_out"
    return "text_out"


# Romanised Hindi / Gujarati markers: the transcription usually comes back in Latin script, so a
# script-only check would label every Hindi call "en". Two distinct hits in one turn is the bar.
_HINDI_ROMAN_WORDS = frozenset(
    "haan nahi nahin theek thik kya aap hai hain bolo boliye mein karo karenge achha accha ji "
    "kaun kab kahan kaise kitne kyun batao dekhte pakka mera meri mujhe hua nikala samasya shikayat".split())
_GUJARATI_ROMAN_WORDS = frozenset(
    "chhe che nathi shu kem kyare ketla aavu aavish aavo majama saru bhai ben hoon karu maru mane".split())


def _infer_language(text):
    """Best-effort language guess for ONE caller turn: Unicode script first, then romanised
    Hindi/Gujarati word hits, else Latin -> "en"."""
    if not text:
        return None
    script = languages.script_of(text)
    if script:
        return script
    latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    if latin:
        words = set(w for w in text.lower().split() if w.isalpha())
        if len(words & _GUJARATI_ROMAN_WORDS) >= 2:
            return "gu"
        if len(words & _HINDI_ROMAN_WORDS) >= 2:
            return "hi"
        return "en"
    return "unknown"


class CallRecorder:
    def __init__(self, model=None):
        self.model = model
        self.call = None          # the persisted dict (None until open)
        self._cur_role = None     # 'user' | 'gemini' currently buffering
        self._cur_text = []
        self._usage = []          # list of per-event usage snapshots
        self._lang_decided = False
        self._lang_counts = {}    # per-turn language tally -> call label at close()
        self._closed = False
        self._started_ts = None

    # Lifecycle

    async def open(self, source, call_sid=None, caller=None):
        try:
            call_id = uuid.uuid4().hex[:16]
            if not call_sid:
                call_sid = "web-" + uuid.uuid4().hex[:12]
            self._started_ts = datetime.now(timezone.utc)
            self.call = {
                "id": call_id,
                "call_sid": call_sid,
                "source": source,                 # 'plivo_inbound' | 'plivo' | 'browser'
                "caller": caller,
                "started_at": self._started_ts.isoformat(),
                "ended_at": None,
                "duration_seconds": 0,
                "language": None,
                "status": "in_progress",
                "ticket_id": None,                # the (last) ticket registered on this call
                "ticket_ids": [],
                "lookup_ticket_ids": [],
                "gemini_model": self.model,
                "tokens": pricing._empty_tokens(),
                "gemini_cost_usd": 0.0,
                "transcript": [],
                "tool_calls": [],
            }
            await store.save_call(self.call)
            logger.info(f"Recording call {call_id} ({source}, sid={call_sid})")
        except Exception as e:
            logger.warning(f"CallRecorder.open failed: {e}")
            self.call = None

    @property
    def call_meta(self):
        """What the ticket tools need to link a ticket to this call."""
        c = self.call or {}
        return {"call_id": c.get("id"), "call_sid": c.get("call_sid"), "caller": c.get("caller")}

    async def on_event(self, event):
        if self.call is None:
            return
        try:
            etype = event.get("type")
            if etype in ("user", "gemini"):
                self._accumulate_turn(etype, event.get("text", ""))
            elif etype == "tool_call":
                self._flush_turn()
                self._record_tool(event)
            elif etype == "usage":
                self._accumulate_usage(event)
            elif etype in ("turn_complete", "interrupted"):
                self._flush_turn()
        except Exception as e:
            logger.warning(f"CallRecorder.on_event failed: {e}")

    async def close(self, status="completed"):
        if self.call is None or self._closed:
            return
        self._closed = True
        try:
            self._flush_turn()
            ended = datetime.now(timezone.utc)
            self.call["ended_at"] = ended.isoformat()
            if self._started_ts:
                self.call["duration_seconds"] = max(0, int((ended - self._started_ts).total_seconds()))
            if self.call.get("status") == "in_progress":
                self.call["status"] = status
            lang = self._final_language()
            if lang:
                self.call["language"] = lang

            self.call["tokens"] = self._finalize_tokens()
            self.call["gemini_cost_usd"] = pricing.compute_gemini_cost(self.call["tokens"])

            await store.save_call(self.call)
            logger.info(
                f"Call {self.call['id']} closed: {self.call['duration_seconds']}s, "
                f"ticket={self.call.get('ticket_id') or '-'}, gemini=${self.call['gemini_cost_usd']:.6f}"
            )
            self._schedule_analysis()
        except Exception as e:
            logger.warning(f"CallRecorder.close failed: {e}")

    def _schedule_analysis(self):
        """Hand the finished call to the post-call analysis (summary, sentiment, refine or
        auto-create the ticket). Guarded: an analysis failure can never affect the call."""
        try:
            import tickets
            tickets.schedule_post_call(self.call["id"])
        except Exception as e:
            logger.warning(f"post-call analysis not scheduled: {e}")

    # Internals

    def _accumulate_turn(self, role, text):
        if not text:
            return
        if self._cur_role and self._cur_role != role:
            self._flush_turn()
        self._cur_role = role
        self._cur_text.append(text)

    def _flush_turn(self):
        if not self._cur_role or not self._cur_text:
            self._cur_role = None
            self._cur_text = []
            return
        text = "".join(self._cur_text).strip()
        if text:
            self.call["transcript"].append(
                {"role": self._cur_role, "text": text, "ts": _now_iso()}
            )
            if self._cur_role == "user":
                lang = _infer_language(text)
                if lang and lang != "unknown":
                    self._lang_counts[lang] = self._lang_counts.get(lang, 0) + 1
                if lang and not self._lang_decided:
                    self.call["language"] = lang
                    self._lang_decided = True
        self._cur_role = None
        self._cur_text = []

    def _final_language(self):
        """Call-level language: the language the caller CHOSE (recorded on the ticket) wins;
        otherwise the most frequent non-English caller turn, else English."""
        chosen = self.call.get("ticket_language")
        if chosen:
            return chosen
        c = dict(self._lang_counts)
        en = c.pop("en", 0)
        if c:
            return max(c.items(), key=lambda kv: kv[1])[0]
        if en:
            return "en"
        return self.call.get("language")

    def _record_tool(self, event):
        name = event.get("name")
        result = event.get("result")
        args = event.get("args") or {}
        self.call["tool_calls"].append({
            "name": name,
            "args": args,
            "result": result,
            "ts": _now_iso(),
        })
        if name == "create_ticket" and isinstance(result, dict) and result.get("ok"):
            tid = result.get("ticket_id")
            self.call["ticket_id"] = tid
            ids = self.call.get("ticket_ids") or []
            if tid and tid not in ids:
                self.call["ticket_ids"] = ids + [tid]
            lang = languages.normalize(args.get("language"))
            if lang:
                self.call["ticket_language"] = lang
        elif name == "lookup_ticket" and isinstance(result, dict) and result.get("found"):
            tid = result.get("ticket_id")
            ids = self.call.get("lookup_ticket_ids") or []
            if tid and tid not in ids:
                self.call["lookup_ticket_ids"] = ids + [tid]

    def _accumulate_usage(self, event):
        snap = {
            "total": int(event.get("total") or 0),
            "thoughts": int(event.get("thoughts") or 0),
            "in": {}, "out": {},
        }
        for mod, cnt in (event.get("prompt_by_modality") or []):
            b = _in_bucket(mod)
            snap["in"][b] = snap["in"].get(b, 0) + int(cnt or 0)
        for mod, cnt in (event.get("response_by_modality") or []):
            b = _out_bucket(mod)
            snap["out"][b] = snap["out"].get(b, 0) + int(cnt or 0)
        self._usage.append(snap)

    def _finalize_tokens(self):
        """Reconcile per-event usage snapshots into final token buckets. A cumulative series
        is strictly non-decreasing, so if the `total` sequence never drops we take the LAST
        snapshot; otherwise we SUM the increments."""
        tokens = pricing._empty_tokens()
        if not self._usage:
            return tokens
        totals = [e["total"] for e in self._usage]
        non_decreasing = all(totals[i] <= totals[i + 1] for i in range(len(totals) - 1))
        cumulative = non_decreasing and len(self._usage) > 1 and totals[-1] > 0
        chosen = [self._usage[-1]] if cumulative else self._usage
        for snap in chosen:
            for b, c in snap["in"].items():
                tokens[b] += c
            for b, c in snap["out"].items():
                tokens[b] += c
            tokens["thoughts"] += snap["thoughts"]
        bucket_sum = (tokens["text_in"] + tokens["audio_in"] + tokens["imgvid_in"]
                      + tokens["text_out"] + tokens["audio_out"] + tokens["thoughts"])
        reported_total = totals[-1] if cumulative else sum(totals)
        tokens["total"] = max(bucket_sum, reported_total)
        return tokens
