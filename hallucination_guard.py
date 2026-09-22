"""Catch the agent announcing a reference number that no tool produced.

The failure this guards against, seen on the first live test round: the model spoke "Your
concern has been successfully registered. Your reference number is …" (once with a category
name, once with the example number from a tool description) without ever calling
create_ticket. No ticket existed, and the caller went away with an invented number.

The prompt now forbids it and the tool result hands the model the exact sentence, but a
prompt is advice; this is the check. Both call paths run `check()` on every completed agent
turn: if the turn *announces* a reference (a statement, not a question asking for one) and
no create_ticket / lookup_ticket has succeeded on this call, the model is told, in a text
turn, to register properly now. Capped per call so it can never loop.
"""

import logging
import re

logger = logging.getLogger(__name__)

_DIGIT_WORDS = r"zero|one|two|three|four|five|six|seven|eight|nine|shunya|ek|do|teen|char|paanch|chhe|saat|aath|nau"

# An announcement of a reference, in the languages the helpline speaks (the English loanword
# is usually kept even in Hindi/Gujarati speech; the transcription may render it in either script).
_ANNOUNCE_RE = re.compile(
    r"(reference|ticket|complaint|case|shikayat|shikaayat)\s*(number|no\.?|id|num|nambar)"
    r"|\bregistered\b|register (ho|kar) (gay|diy|liy)|darj (ho|kar)"
    r"|रेफरेंस|रेफ़रेंस|रेफरन्स|टिकट (नंबर|नम्बर)|रजिस्टर|दर्ज|नोंदणी"
    r"|રેફરન્સ|ટિકિટ|નોંધ|રજીસ્ટર",
    re.I,
)

# The agent ASKING the caller for a reference (a status inquiry) is legitimate.
_ASKING_RE = re.compile(
    r"(do you have|have you got|could you|can you|may i have|please (tell|give|share|read)|what is your"
    r"|kya aap|aapke paas|batayein|bata sakt|tamari pase|tamaru|શું તમારી|क्या आप|आपके पास)",
    re.I,
)


def _prefix_re():
    """The ticket prefix spoken letter by letter or as a word, followed by a digit or a spoken
    digit within a few words: 'T K T two zero…', 'TKT-2026-000123', 'EPP 2026 123'."""
    try:
        import tickets
        prefix = tickets.prefix()
    except Exception:
        prefix = "EPP"
    letters = r"[\s,.\-]*".join(re.escape(ch) for ch in prefix)
    return re.compile(rf"\b{letters}\b[\s,.\-—]*(?:\d|{_DIGIT_WORDS})", re.I)


def spoke_a_reference(text: str) -> bool:
    """True when this agent turn announces a reference number or a completed registration."""
    t = (text or "").strip()
    if not t:
        return False
    has_evidence = bool(_prefix_re().search(t))
    announced = bool(_ANNOUNCE_RE.search(t))
    if not announced and not has_evidence:
        return False
    if has_evidence:
        return True
    # Announced the words without a number: only a statement counts, never a question for one.
    if _ASKING_RE.search(t) or t.endswith("?"):
        return False
    return True


NUDGE = (
    "[STOP. You have NOT called create_ticket on this call — no ticket exists, and any reference "
    "number you said is invented. Say to the caller, in their language: \"One moment, let me "
    "register that properly.\" Then call create_ticket NOW with everything you collected, wait for "
    "its result, and read ONLY the say_now it returns. Do not apologise at length; do not repeat the "
    "old number.]"
)


class HallucinationGuard:
    """Per-call state: has a real ticket been created (or looked up) yet, and how many times
    have we already pushed back."""

    MAX_NUDGES = 2

    def __init__(self, call_label=""):
        self.ticket_ok = False
        self.nudged = 0
        self.call_label = call_label

    def on_tool_call(self, name, result):
        """Feed every tool_call event. A successful create_ticket or lookup_ticket makes any
        spoken number legitimate for the rest of the call."""
        if not isinstance(result, dict):
            return
        if name in ("create_ticket", "update_ticket") and result.get("ok"):
            self.ticket_ok = True
        elif name == "lookup_ticket" and result.get("found"):
            self.ticket_ok = True

    def check(self, turn_text):
        """Run on a COMPLETED agent turn. Returns the nudge text to inject, or None."""
        if self.ticket_ok or self.nudged >= self.MAX_NUDGES:
            return None
        if not spoke_a_reference(turn_text):
            return None
        self.nudged += 1
        logger.warning("HALLUCINATION GUARD %s: agent announced a reference with no ticket (nudge %d/%d): %r",
                       self.call_label or "", self.nudged, self.MAX_NUDGES, (turn_text or "").strip()[-160:])
        return NUDGE
