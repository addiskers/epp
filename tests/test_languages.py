"""languages.py — the one table behind the tool enum, the prompt and the recorder."""

import languages


def test_all_twelve_required_languages_are_present():
    assert languages.all_codes() == ["en", "hi", "gu", "mr", "bn", "ta", "te", "kn", "ml", "pa", "or", "as"]


def test_normalise_accepts_codes_names_native_and_bcp47():
    assert languages.normalize("hi") == "hi"
    assert languages.normalize("Hindi") == "hi"
    assert languages.normalize("हिन्दी") == "hi"
    assert languages.normalize("ta-IN") == "ta"
    assert languages.normalize("Tamil (India)") == "ta"
    assert languages.normalize("klingon") == ""
    assert languages.normalize("") == ""


def test_enabled_is_filtered_by_env_and_ignores_junk(monkeypatch):
    monkeypatch.setenv("EPP_ENABLED_LANGUAGES", "hi, en,xx")
    assert languages.enabled_codes() == ["en", "hi"]              # table order, junk dropped
    monkeypatch.setenv("EPP_ENABLED_LANGUAGES", "zz")
    assert len(languages.enabled_codes()) == 12                    # nothing valid -> everything
    monkeypatch.delenv("EPP_ENABLED_LANGUAGES")
    assert len(languages.enabled_codes()) == 12


def test_spoken_list_reads_naturally(monkeypatch):
    monkeypatch.setenv("EPP_ENABLED_LANGUAGES", "en,hi,gu")
    assert languages.spoken_list() == "English, Hindi and Gujarati"
    monkeypatch.setenv("EPP_ENABLED_LANGUAGES", "en")
    assert languages.spoken_list() == "English"


def test_script_detection_by_unicode_block():
    assert languages.script_of("ಕನ್ನಡ") == "kn"
    assert languages.script_of("മലയാളം") == "ml"
    assert languages.script_of("ଓଡ଼ିଆ") == "or"
    assert languages.script_of("বাংলা") == "bn"
    assert languages.script_of("hello") == ""
    assert languages.script_of("ok ok தமிழ் ok") == "ta"
