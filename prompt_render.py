"""Render the intake agent's prompt template for one call.

Deliberately dependency-light: no eo_db import, no I/O. The caller passes the rows it
already fetched (categories, departments), which keeps SQLite off the connect path and
makes every rule here unit-testable in isolation.

Two outputs per call, and the split matters:

* ``system_instruction`` — frozen into the Live session at connect time. Carries the
  persona, the language list, the category list, and what we know about the caller.
* ``trigger`` — a text turn sent once the media stream opens. This is what makes the
  model start speaking at all (the Live API emits no audio until it receives input).

Missing-data policy: on the live call path an unresolved placeholder becomes an empty
string and is reported in ``missing``; it NEVER raises. An *unknown* placeholder (a typo
like ``{helpline_nam}``) is an authoring bug and is rejected when the agent is saved.
"""

import logging
import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import languages

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_PLACEHOLDER_RE = re.compile(r"\{([a-z0-9_]+)\}")

_ORDINALS = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh",
    8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh", 12: "twelfth", 13: "thirteenth",
    14: "fourteenth", 15: "fifteenth", 16: "sixteenth", 17: "seventeenth", 18: "eighteenth",
    19: "nineteenth", 20: "twentieth", 21: "twenty-first", 22: "twenty-second",
    23: "twenty-third", 24: "twenty-fourth", 25: "twenty-fifth", 26: "twenty-sixth",
    27: "twenty-seventh", 28: "twenty-eighth", 29: "twenty-ninth", 30: "thirtieth",
    31: "thirty-first",
}

_HOURS = {0: "twelve", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
          7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve"}

_MINUTES = {5: "five", 10: "ten", 15: "quarter", 20: "twenty", 25: "twenty-five",
            30: "half", 35: "twenty-five", 40: "twenty", 45: "quarter", 50: "ten", 55: "five"}

_DIGIT_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
                "6": "six", "7": "seven", "8": "eight", "9": "nine"}


class PromptRenderError(ValueError):
    """Raised only by strict rendering (agent save / preview), never on a live call."""


# Every placeholder an agent template may use. Single source of truth for the save-time
# validator AND the UI's placeholder palette, so the two can never drift apart.
KNOWN_PLACEHOLDERS = frozenset({
    # derived
    "today_spoken", "today_iso", "now_time",
    # the caller
    "caller_phone", "caller_phone_spoken",
    # the helpline
    "company_name", "helpline_name", "language_list", "category_list", "department_list",
    # outbound campaign calls
    "campaign_name", "campaign_message", "caller_name",
    # follow-up calls: the ticket being followed up
    "ticket_id", "ticket_id_spoken", "ticket_status", "ticket_category", "ticket_department",
    "ticket_created_spoken",
})


def company_name():
    return (os.getenv("EPP_COMPANY_NAME") or "EPP Composites").strip()


def helpline_name():
    return (os.getenv("EPP_HELPLINE_NAME") or f"{company_name()} Support Helpline").strip()


def _spoken_date(value):
    """'2026-09-19' -> 'the nineteenth of September'. Blank on anything unparseable."""
    d = _as_date(value)
    if not d:
        return ""
    return f"the {_ORDINALS.get(d.day, str(d.day))} of {d.strftime('%B')}"


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).strip()[:10]).date()
    except (TypeError, ValueError):
        return None


def _spoken_time(value):
    """'19:00' -> 'seven in the evening'; '10:30' -> 'half past ten in the morning'."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    m = re.match(r"^(\d{1,2})[:.](\d{2})", raw)
    if not m:
        return raw
    hour24, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour24 <= 23 and 0 <= minute <= 59):
        return raw
    if hour24 < 12:
        part = "in the morning"
    elif hour24 < 16:
        part = "in the afternoon"
    elif hour24 < 20:
        part = "in the evening"
    else:
        part = "at night"
    hour12 = hour24 % 12
    spoken_hour = _HOURS[hour12 if hour12 else 12]
    if minute == 0:
        return f"{spoken_hour} {part}"
    if minute == 15:
        return f"quarter past {spoken_hour} {part}"
    if minute == 30:
        return f"half past {spoken_hour} {part}"
    if minute == 45:
        nxt = _HOURS[(hour12 + 1) % 12 if (hour12 + 1) % 12 else 12]
        return f"quarter to {nxt} {part}"
    if minute in _MINUTES and minute < 30:
        return f"{_MINUTES[minute]} past {spoken_hour} {part}"
    if minute in _MINUTES and minute > 30:
        nxt = _HOURS[(hour12 + 1) % 12 if (hour12 + 1) % 12 else 12]
        return f"{_MINUTES[minute]} to {nxt} {part}"
    return f"{spoken_hour} {minute} {part}"


def spoken_phone(phone):
    """'+919876543210' -> 'nine eight seven six five, four three two one zero'.

    The country code is dropped for a +91 number (the caller knows their own number; the
    agent only needs to confirm it) and the digits are grouped in fives so the read-back
    has a natural breath in it."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return ""
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    words = [_DIGIT_WORDS[d] for d in digits]
    groups = [" ".join(words[i:i + 5]) for i in range(0, len(words), 5)]
    return ", ".join(groups)


def _category_list(categories):
    """One line per caller type, e.g. 'Employee: Salary, Payslip, ...'. Empty if no rows."""
    by_type = {}
    for c in categories or []:
        if not int(c.get("active", 1) or 0):
            continue
        ct = str(c.get("caller_type") or "").strip().lower()
        name = str(c.get("name") or "").strip()
        if ct and name:
            by_type.setdefault(ct, [])
            if name not in by_type[ct]:
                by_type[ct].append(name)
    lines = []
    for ct in ("customer", "vendor", "employee"):
        names = by_type.get(ct)
        if names:
            lines.append(f"- {ct.capitalize()}: " + ", ".join(names))
    return "\n".join(lines)


def _department_list(departments):
    names = [str(d.get("name") or "").strip() for d in (departments or [])
             if int(d.get("active", 1) or 0) and str(d.get("name") or "").strip()]
    return ", ".join(names)


def _merge(dst, src):
    """Layer src over dst, skipping empty values so a blank never shadows a real one."""
    for key, val in (src or {}).items():
        if val in (None, ""):
            continue
        dst[key] = str(val)


def build_context(*, caller_phone=None, categories=None, departments=None, langs=None,
                  now=None, extra=None):
    """Flatten everything the prompt may reference into one {placeholder: str} map."""
    now = now or datetime.now(_IST)
    today = now.date() if hasattr(now, "date") else None
    ctx = {}
    _merge(ctx, {
        "today_spoken": _spoken_date(today),
        "today_iso": today.isoformat() if today else "",
        "now_time": _spoken_time(now.strftime("%H:%M")) if hasattr(now, "strftime") else "",
        "caller_phone": str(caller_phone or "").strip(),
        "caller_phone_spoken": spoken_phone(caller_phone),
        "company_name": company_name(),
        "helpline_name": helpline_name(),
        "language_list": languages.spoken_list(langs),
        "category_list": _category_list(categories),
        "department_list": _department_list(departments),
    })
    _merge(ctx, extra)
    return ctx


# Prepositions/conjunctions that are left dangling when the value after them blanks out.
_DANGLING = r"(?:at|on|in|from|to|by|for|with|and|near|until|till)"


def _tidy_line(line):
    """Clean one line that actually lost a value. Returns '' if nothing is left to say."""
    out = re.sub(r"[ \t]{2,}", " ", line)
    for _ in range(4):
        before = out
        out = re.sub(rf"\s+{_DANGLING}(?=\s+{_DANGLING}\b)", "", out, flags=re.I)
        out = re.sub(rf"\s+{_DANGLING}\s*(?=[.,;:!?]|$)", "", out, flags=re.I)
        out = re.sub(r"[ \t]+([,.;:!?])", r"\1", out)
        out = re.sub(r"([,;:])(\s*[,;:])+", r"\1", out)
        out = re.sub(r"[ \t]{2,}", " ", out)
        if out == before:
            break
    stripped = out.strip()
    if not stripped:
        return ""
    if re.fullmatch(r"[-*•:,;.\s]+", stripped):
        return ""
    if re.fullmatch(r"[-*•]\s*[A-Za-z][\w' ()/,]{0,60}\s*[:—-]\s*[.,;]?", stripped):
        return ""
    return out.rstrip()


def _tidy(text, touched_lines=None):
    """Clean up what an empty substitution leaves behind. Only lines that ACTUALLY lost a
    placeholder are rewritten; the author's own prose is never touched."""
    lines = []
    for idx, raw in enumerate(text.split("\n")):
        if not raw.strip():
            lines.append("")
            continue
        if touched_lines is not None and idx not in touched_lines:
            lines.append(raw.rstrip())
            continue
        cleaned = _tidy_line(raw)
        if cleaned:
            lines.append(cleaned)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def render(template, ctx, *, strict=False):
    """Substitute {placeholders}. Returns (rendered_text, sorted_missing_names)."""
    template = template or ""
    missing = set()
    unknown = set()
    blanked_offsets = []

    def _sub(match):
        name = match.group(1)
        if name not in KNOWN_PLACEHOLDERS:
            unknown.add(name)
            if strict:
                return match.group(0)
            blanked_offsets.append(match.start())
            return ""
        value = ctx.get(name, "")
        if value in (None, ""):
            missing.add(name)
            blanked_offsets.append(match.start())
            return ""
        return str(value)

    out = _PLACEHOLDER_RE.sub(_sub, template)
    if strict and unknown:
        raise PromptRenderError(
            "unknown placeholder(s): " + ", ".join(f"{{{n}}}" for n in sorted(unknown)))
    if unknown:
        logger.warning("prompt_render: unknown placeholders ignored: %s", sorted(unknown))
    touched = {template.count("\n", 0, off) for off in blanked_offsets}
    return _tidy(out, touched), sorted(missing)


def validate_template(template):
    """Placeholders in a template that are not in the known vocabulary. Empty = valid."""
    return sorted({n for n in _PLACEHOLDER_RE.findall(template or "")
                   if n not in KNOWN_PLACEHOLDERS})


DEFAULT_TRIGGER = (
    "[An inbound call has just connected. The caller has not spoken yet. Say your opening "
    "line now, in English, then STOP and wait for them.]"
)


def render_prompt(agent, *, caller_phone=None, categories=None, departments=None, langs=None,
                  now=None, extra=None, outbound=False):
    """The one function the call path uses. Never raises.

    outbound=True (a campaign dial) uses the agent's outbound trigger when it has one — the
    person did not ring us, so the opening must say who is calling and why."""
    agent = agent or {}
    ctx = build_context(caller_phone=caller_phone, categories=categories,
                        departments=departments, langs=langs, now=now, extra=extra)
    system_instruction, missing_prompt = render(agent.get("prompt_template") or "", ctx)
    trigger_template = agent.get("trigger_template") or ""
    if outbound and (agent.get("outbound_trigger_template") or "").strip():
        trigger_template = agent["outbound_trigger_template"]
    trigger, missing_trigger = render(trigger_template, ctx)
    if not trigger:
        trigger = DEFAULT_TRIGGER
    missing = sorted(set(missing_prompt) | set(missing_trigger))
    if missing:
        logger.warning("prompt_render: agent=%s unresolved placeholders: %s",
                       agent.get("slug") or agent.get("id"), missing)
    return {"system_instruction": system_instruction, "trigger": trigger,
            "missing": missing, "context": ctx}
