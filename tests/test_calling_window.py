"""calling_window.py — calling-hours math for the campaign dialer."""
import calling_window as cw


def test_in_call_window_normal_window():
    assert cw.in_call_window(540, 1260, now_min=600) is True
    assert cw.in_call_window(540, 1260, now_min=300) is False
    assert cw.in_call_window(540, 1260, now_min=1260) is False   # end is exclusive


def test_in_call_window_overnight_wrap():
    assert cw.in_call_window(1260, 540, now_min=100) is True
    assert cw.in_call_window(1260, 540, now_min=800) is False


def test_no_restriction_cases():
    assert cw.in_call_window(None, 540, now_min=0) is True
    assert cw.in_call_window(600, 600, now_min=0) is True


def test_windows_from_env_and_campaign(monkeypatch):
    monkeypatch.setenv("EPP_CALL_WINDOW_START", "10:30")
    monkeypatch.setenv("EPP_CALL_WINDOW_END", "18:00")
    assert cw.global_window() == (630, 1080)
    assert cw.campaign_window({"call_start_min": 600, "call_end_min": "1200"}) == (600, 1200)
    assert cw.campaign_window({"call_start_min": None, "call_end_min": "junk"}) == (540, 1260)
    assert cw.campaign_window(None) == (630, 1080)
    assert cw.hhmm_to_min("25:70") == (1 * 60 + 10)
    assert cw.hhmm_to_min("nope", default=7) == 7


def test_now_ist_min_is_in_range():
    assert 0 <= cw.now_ist_min() < 1440
