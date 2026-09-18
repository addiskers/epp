"""The languages the helpline can take a call in.

One table, used by three things that must agree: the tool enum the agent records the
caller's language with, the LANGUAGE section of the intake prompt, and the recorder's
script-based language guess. EPP_ENABLED_LANGUAGES narrows the set without a code change
(a language that fails the live-call check is switched off in .env, not deleted here).
"""

import os

LANGUAGES = [
    {"code": "en", "name": "English",   "native": "English",   "bcp47": "en-IN"},
    {"code": "hi", "name": "Hindi",     "native": "हिन्दी",     "bcp47": "hi-IN"},
    {"code": "gu", "name": "Gujarati",  "native": "ગુજરાતી",    "bcp47": "gu-IN"},
    {"code": "mr", "name": "Marathi",   "native": "मराठी",      "bcp47": "mr-IN"},
    {"code": "bn", "name": "Bengali",   "native": "বাংলা",      "bcp47": "bn-IN"},
    {"code": "ta", "name": "Tamil",     "native": "தமிழ்",      "bcp47": "ta-IN"},
    {"code": "te", "name": "Telugu",    "native": "తెలుగు",     "bcp47": "te-IN"},
    {"code": "kn", "name": "Kannada",   "native": "ಕನ್ನಡ",      "bcp47": "kn-IN"},
    {"code": "ml", "name": "Malayalam", "native": "മലയാളം",     "bcp47": "ml-IN"},
    {"code": "pa", "name": "Punjabi",   "native": "ਪੰਜਾਬੀ",     "bcp47": "pa-IN"},
    {"code": "or", "name": "Odia",      "native": "ଓଡ଼ିଆ",      "bcp47": "or-IN"},
    {"code": "as", "name": "Assamese",  "native": "অসমীয়া",    "bcp47": "as-IN"},
]

_BY_CODE = {l["code"]: l for l in LANGUAGES}
_BY_NAME = {l["name"].lower(): l for l in LANGUAGES}
_BY_NATIVE = {l["native"]: l for l in LANGUAGES}

# Unicode script blocks -> language code. Devanagari is shared by Hindi and Marathi, and
# the Bengali block by Bengali and Assamese; the recorder labels the common one and the
# ticket's own `language` (chosen by the caller) wins over this guess anyway.
_SCRIPT_RANGES = (
    (0x0900, 0x097F, "hi"),   # Devanagari
    (0x0980, 0x09FF, "bn"),   # Bengali / Assamese
    (0x0A00, 0x0A7F, "pa"),   # Gurmukhi
    (0x0A80, 0x0AFF, "gu"),   # Gujarati
    (0x0B00, 0x0B7F, "or"),   # Odia
    (0x0B80, 0x0BFF, "ta"),   # Tamil
    (0x0C00, 0x0C7F, "te"),   # Telugu
    (0x0C80, 0x0CFF, "kn"),   # Kannada
    (0x0D00, 0x0D7F, "ml"),   # Malayalam
)


def all_codes():
    return [l["code"] for l in LANGUAGES]


def enabled():
    """The enabled languages, in table order. Unknown codes in the env are ignored; an
    empty or unparseable setting means all of them."""
    raw = (os.getenv("EPP_ENABLED_LANGUAGES") or "").strip()
    if not raw:
        return list(LANGUAGES)
    wanted = {c.strip().lower() for c in raw.split(",") if c.strip()}
    out = [l for l in LANGUAGES if l["code"] in wanted]
    return out or list(LANGUAGES)


def enabled_codes():
    return [l["code"] for l in enabled()]


def by_code(code):
    return _BY_CODE.get(str(code or "").strip().lower())


def normalize(value):
    """A language code, English name, or native name -> the code. '' when unknown."""
    s = str(value or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low in _BY_CODE:
        return low
    if low in _BY_NAME:
        return _BY_NAME[low]["code"]
    if s in _BY_NATIVE:
        return _BY_NATIVE[s]["code"]
    # "hi-IN", "Hindi (India)"
    head = low.split("-")[0].split("(")[0].strip()
    if head in _BY_CODE:
        return head
    if head in _BY_NAME:
        return _BY_NAME[head]["code"]
    return ""


def name_of(code):
    l = by_code(code)
    return l["name"] if l else str(code or "")


def spoken_list(langs=None):
    """'English, Hindi, Gujarati, Marathi, Bengali, Tamil, Telugu, Kannada, Malayalam, Punjabi, Odia and Assamese'."""
    names = [l["name"] for l in (langs or enabled())]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def script_of(text):
    """Language code implied by the Unicode script of `text`, or '' when it is Latin/other.
    Counts characters per block so a stray symbol cannot outvote a sentence."""
    counts = {}
    for ch in text or "":
        o = ord(ch)
        for lo, hi, code in _SCRIPT_RANGES:
            if lo <= o <= hi:
                counts[code] = counts.get(code, 0) + 1
                break
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]
