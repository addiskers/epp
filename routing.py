"""Department mapping and priority detection.

Deliberately dependency-light: no DB import, no I/O. Callers pass the category rows they
already fetched (eo_db.list_categories), which keeps SQLite off the tool path and makes
every rule here unit-testable with plain dicts.

Three questions are answered here:

* resolve_category(caller_type, name, categories) -> the category row the agent meant,
  falling back to that caller type's "Other" so a ticket is never left without a department.
* detect_priority(reason, category, description) -> ("high"|"medium", escalation_flag, why).
  Three independent nets: the reason the agent chose, the category's own high_priority
  flag, and a keyword scan of the description. Any one of them is enough.
* classify_by_keywords(caller_type, text, categories) -> a best-effort category name from
  the keywords configured on each row, for the post-call pass when the live agent
  recorded nothing.
"""

import json
import re

CALLER_TYPES = ("customer", "vendor", "employee")

PRIORITY_REASONS = (
    "none", "safety_incident", "harassment", "violence", "threat",
    "security_incident", "medical_emergency", "serious_misconduct",
)

PRIORITIES = ("high", "medium", "low")

# Keyword safety net for the description. English plus the common Hinglish forms — the
# transcript is usually romanised. Kept deliberately specific: "fire" alone would match
# "fired from my job", so it is "fire broke out" / "caught fire" etc.
_REASON_KEYWORDS = {
    "safety_incident": (
        r"accident", r"injur", r"caught fire", r"fire broke", r"fire in the", r"aag lag",
        r"chemical (leak|spill)", r"gas leak", r"explosion", r"blast", r"unsafe",
        r"fell (from|off|down)", r"gir gaya", r"electric shock", r"no helmet", r"no safety",
    ),
    "harassment": (
        r"harass", r"molest", r"sexual", r"inappropriate(ly)? touch", r"chhed", r"badtameez",
        r"vulgar", r"lewd", r"stalk",
    ),
    "violence": (
        r"assault", r"beat(en| me| up)", r"hit me", r"slapped", r"punched", r"violence",
        r"violent", r"maarpeet", r"maara", r"physically attack",
    ),
    "threat": (
        r"threat", r"dhamki", r"will (kill|hurt|harm)", r"jaan se", r"blackmail", r"intimidat",
    ),
    "security_incident": (
        r"theft", r"stolen", r"chori", r"break[- ]?in", r"burglar", r"security breach",
        r"unauthori[sz]ed (entry|access)", r"trespass", r"data leak",
    ),
    "medical_emergency": (
        r"medical emergency", r"ambulance", r"unconscious", r"collapsed", r"heart attack",
        r"bleeding", r"not breathing", r"behosh", r"hospital(i[sz]ed)? (right now|urgent)",
    ),
    "serious_misconduct": (
        r"fraud", r"bribe", r"corrupt", r"rishwat", r"embezzl", r"forg(ed|ery)",
        r"gross misconduct", r"drunk on duty", r"nasha",
    ),
}
_REASON_RES = {k: re.compile("|".join(v), re.I) for k, v in _REASON_KEYWORDS.items()}


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def normalize_caller_type(value):
    v = _norm(value)
    if v in CALLER_TYPES:
        return v
    aliases = {"client": "customer", "buyer": "customer", "supplier": "vendor",
               "staff": "employee", "worker": "employee", "employ": "employee"}
    for k, ct in aliases.items():
        if k in v:
            return ct
    for ct in CALLER_TYPES:
        if ct in v:
            return ct
    return ""


def _keywords(row):
    raw = row.get("keywords")
    if isinstance(raw, list):
        return [str(k) for k in raw if str(k).strip()]
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(k) for k in parsed if str(k).strip()] if isinstance(parsed, list) else []


def resolve_category(caller_type, name, categories):
    """The category row for (caller_type, name), else that type's 'Other', else None.

    Matching is case/space-insensitive and tolerant of the model paraphrasing slightly
    ('PF issue' -> 'PF', 'delivery delayed' -> 'Delivery Delay') via a token-overlap
    fallback, so a near miss still lands on the right department."""
    ct = normalize_caller_type(caller_type)
    rows = [c for c in (categories or [])
            if _norm(c.get("caller_type")) == ct and int(c.get("active", 1) or 0)]
    want = _norm(name)
    if want:
        for c in rows:
            if _norm(c.get("name")) == want:
                return c
        want_tokens = set(re.findall(r"[a-z0-9]+", want))
        best, best_score = None, 0
        for c in rows:
            toks = set(re.findall(r"[a-z0-9]+", _norm(c.get("name"))))
            if not toks or _norm(c.get("name")) == "other":
                continue
            score = len(toks & want_tokens)
            if score > best_score or (score == best_score and score and best
                                      and len(toks) < len(set(re.findall(r"[a-z0-9]+", _norm(best.get("name")))))):
                best, best_score = c, score
        if best is not None and best_score > 0:
            return best
    for c in rows:
        if _norm(c.get("name")) == "other":
            return c
    return None


def detect_priority(reason, category, description):
    """-> (priority, escalation_flag, escalation_reason).

    High when the agent named a reason, the category is flagged high-priority, or the
    description trips the keyword net. Never returns 'low' — that is a human decision."""
    reason = _norm(reason).replace(" ", "_")
    if reason and reason != "none" and reason in PRIORITY_REASONS:
        return "high", 1, reason
    if category and int(category.get("high_priority", 0) or 0):
        return "high", 1, f"category:{category.get('name')}"
    text = str(description or "")
    for key, rx in _REASON_RES.items():
        m = rx.search(text)
        if m:
            return "high", 1, f"{key}:{m.group(0).lower()}"
    return "medium", 0, ""


def keyword_reason(description):
    """The first high-priority reason the keyword net finds in `description`, or ''."""
    text = str(description or "")
    for key, rx in _REASON_RES.items():
        if rx.search(text):
            return key
    return ""


def classify_by_keywords(caller_type, text, categories):
    """Best-effort category NAME from the keywords configured per row. None when nothing hits."""
    ct = normalize_caller_type(caller_type)
    hay = _norm(text)
    if not hay:
        return None
    best, best_hits = None, 0
    for c in categories or []:
        if _norm(c.get("caller_type")) != ct or not int(c.get("active", 1) or 0):
            continue
        hits = sum(1 for k in _keywords(c) if _norm(k) and _norm(k) in hay)
        if hits > best_hits:
            best, best_hits = c, hits
    return best.get("name") if best else None


def max_priority(a, b):
    """The more urgent of two priorities (post-call analysis may raise, never lower)."""
    order = {"high": 0, "medium": 1, "low": 2}
    a, b = _norm(a), _norm(b)
    if a not in order:
        return b if b in order else "medium"
    if b not in order:
        return a
    return a if order[a] <= order[b] else b
