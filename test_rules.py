"""Minimal self-check for the scheduling rules. Run: python test_rules.py"""
from datetime import datetime, timedelta
import pytz

from cyride_notifier import evaluate_shift_rules, is_real_shift

TZ = pytz.timezone("America/Chicago")


def dt(day, hm):
    return TZ.localize(datetime.strptime(f"2026-07-{day:02d} {hm}", "%Y-%m-%d %H:%M"))


def test_empty_week_is_valid():
    ok, reason = evaluate_shift_rules(dt(6, "08:00"), dt(6, "12:00"), [])
    assert ok, reason


def test_overlap_rejected():
    existing = [(dt(6, "08:00"), dt(6, "12:00"))]
    ok, reason = evaluate_shift_rules(dt(6, "10:00"), dt(6, "14:00"), existing)
    assert not ok and "Overlap" in reason


def test_more_than_six_shifts_a_week_rejected():
    # 6 existing shifts already this week, a 7th (even non-overlapping) must fail.
    existing = [(dt(6 + i, "08:00"), dt(6 + i, "09:00")) for i in range(6)]
    ok, reason = evaluate_shift_rules(dt(13, "08:00"), dt(13, "09:00"), existing)
    assert not ok and "6 shifts" in reason


def test_over_10_5_hours_in_a_day_rejected():
    ok, reason = evaluate_shift_rules(dt(6, "06:00"), dt(6, "17:00"), [])  # 11h
    assert not ok and "10.5 hours" in reason


def test_10_5_hours_exactly_is_allowed():
    # 6h + 30min break + 4.5h = 10.5h total, no stretch over 6h.
    existing = [(dt(6, "06:00"), dt(6, "12:00"))]
    ok, reason = evaluate_shift_rules(dt(6, "12:30"), dt(6, "17:00"), existing)
    assert ok, reason


def test_over_16_hour_spread_rejected():
    existing = [(dt(6, "05:00"), dt(6, "07:00"))]
    ok, reason = evaluate_shift_rules(dt(6, "20:30"), dt(6, "21:30"), existing)  # 16.5h spread
    assert not ok and "16 hours" in reason


def test_six_hours_without_break_rejected():
    ok, reason = evaluate_shift_rules(dt(6, "06:00"), dt(6, "12:31"), [])  # 6h31m straight
    assert not ok and "6 hours" in reason


def test_short_gap_still_counts_as_one_stretch():
    # 4h + 10min gap (<30min) + 3h = 7h effective straight work, should fail.
    existing = [(dt(6, "06:00"), dt(6, "10:00"))]
    ok, reason = evaluate_shift_rules(dt(6, "10:10"), dt(6, "13:10"), existing)
    assert not ok and "6 hours" in reason


def test_thirty_minute_gap_resets_break_counter():
    # 6h + exactly 30min break + 4h = fine on the break rule (10h total, within 10.5).
    existing = [(dt(6, "06:00"), dt(6, "12:00"))]
    ok, reason = evaluate_shift_rules(dt(6, "12:30"), dt(6, "16:30"), existing)
    assert ok, reason


def test_less_than_9_hour_overnight_break_rejected():
    prev_day = [(dt(6, "14:00"), dt(6, "20:00"))]
    ok, reason = evaluate_shift_rules(dt(7, "04:00"), dt(7, "08:00"), prev_day)  # 8h gap
    assert not ok and "9 hour break" in reason


def test_nine_hour_overnight_break_allowed():
    prev_day = [(dt(6, "14:00"), dt(6, "20:00"))]
    ok, reason = evaluate_shift_rules(dt(7, "05:00"), dt(7, "09:00"), prev_day)  # 9h gap
    assert ok, reason


def test_is_real_shift_filters_placeholders():
    assert not is_real_shift({"run": "XXX", "hours": 0.0, "overtime": False})
    assert is_real_shift({"run": "A1W", "start": "12:12", "end": "15:27", "route": "Aqua"})


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} checks passed.")
