"""Ping scheduling rules (plan §7.1) — pure logic, no clock, no network."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from iblu_keeper.pings import schedule as s

TZ = ZoneInfo("Europe/Zagreb")
DAY = date(2026, 9, 14)  # a Monday


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=TZ)


def ev(start_h, start_m, end_h, end_m, summary="Meeting", attendees=2) -> s.Event:
    return s.Event(at(start_h, start_m), at(end_h, end_m), summary, attendees)


# --- windows --------------------------------------------------------------


def test_parse_window():
    assert s.parse_window("12:30-14:00") == (time(12, 30), time(14, 0))
    assert s.parse_window(" 17:00 - 18:30 ") == (time(17, 0), time(18, 30))


@pytest.mark.parametrize("bad", ["", "12:30", "14:00-12:30", "12:30-12:30", "noon-two", None])
def test_parse_window_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        s.parse_window(bad)


def test_window_bounds_are_timezone_aware():
    start, end = s.window_bounds("12:30-14:00", DAY, TZ)
    assert (start.hour, start.minute) == (12, 30)
    assert (end.hour, end.minute) == (14, 0)
    assert start.tzinfo is TZ


def test_ping_days():
    weekdays = frozenset({"MON", "TUE", "WED", "THU", "FRI"})
    assert s.is_ping_day(date(2026, 9, 14), weekdays) is True     # Monday
    assert s.is_ping_day(date(2026, 9, 19), weekdays) is False    # Saturday
    assert s.is_ping_day(date(2026, 9, 20), weekdays) is False    # Sunday


# --- "is this a decent moment to interrupt?" ------------------------------


def test_free_when_the_day_is_empty():
    ok, why = s.free_now(at(13, 0), [])
    assert ok and why == ""


def test_not_free_during_a_meeting():
    ok, why = s.free_now(at(13, 0), [ev(12, 30, 13, 30, "Opera sync")])
    assert not ok and "in progress" in why and "Opera sync" in why


def test_not_free_in_the_settling_minutes_after_a_meeting():
    events = [ev(12, 0, 13, 0)]
    assert s.free_now(at(13, 2), events)[0] is False   # 2 min after
    assert s.free_now(at(13, 4, ), events)[0] is False  # 4 min after
    assert s.free_now(at(13, 5), events)[0] is True    # grace elapsed


def test_not_free_just_before_the_next_meeting():
    events = [ev(14, 0, 15, 0)]
    assert s.free_now(at(13, 51), events)[0] is False  # 9 min before
    assert s.free_now(at(13, 50), events)[0] is True   # exactly 10 min before


def test_back_to_back_meetings_leave_no_free_moment():
    events = [ev(12, 0, 13, 0), ev(13, 0, 14, 0)]
    for minute in range(0, 60, 7):
        assert s.free_now(at(12, minute), events)[0] is False


# --- the decision ---------------------------------------------------------


def _bounds():
    return s.window_bounds("12:30-14:00", DAY, TZ)


def test_nothing_before_the_window_opens():
    start, end = _bounds()
    d = s.decide(at(11, 0), start, end, [], already_handled=False)
    assert not d.send and "before the window" in d.reason


def test_sends_at_the_first_free_moment():
    start, end = _bounds()
    d = s.decide(at(12, 30), start, end, [], already_handled=False)
    assert d.send and not d.forced


def test_waits_while_busy_then_sends_when_free():
    start, end = _bounds()
    busy = [ev(12, 0, 13, 0, "standup")]
    assert s.decide(at(12, 45), start, end, busy, already_handled=False).send is False
    assert s.decide(at(13, 10), start, end, busy, already_handled=False).send is True


def test_sends_at_the_window_edge_even_if_busy():
    """A booked-solid day must still get asked — the window end is the backstop."""
    start, end = _bounds()
    solid = [ev(12, 0, 18, 0, "all-day workshop")]
    d = s.decide(at(14, 0), start, end, solid, already_handled=False)
    assert d.send and d.forced and "window closing" in d.reason


def test_never_twice_in_a_day():
    start, end = _bounds()
    d = s.decide(at(13, 0), start, end, [], already_handled=True)
    assert not d.send and "already" in d.reason


# --- what period the questions cover --------------------------------------


def test_midday_covers_the_morning():
    frm, to = s.coverage("midday", at(13, 0), DAY, TZ)
    assert (frm.hour, frm.minute) == (6, 0)
    assert to == at(13, 0)


def test_evening_starts_where_midday_left_off():
    midday_sent = at(12, 40)
    frm, to = s.coverage("evening", at(17, 30), DAY, TZ, midday_sent_at=midday_sent)
    assert frm == midday_sent and to == at(17, 30)


def test_evening_falls_back_when_no_midday_ping_was_sent():
    frm, _ = s.coverage("evening", at(17, 30), DAY, TZ, midday_sent_at=None)
    assert (frm.hour, frm.minute) == (13, 0)


def test_the_two_windows_never_overlap_in_coverage():
    midday_sent = at(12, 40)
    _, midday_to = s.coverage("midday", midday_sent, DAY, TZ)
    evening_from, _ = s.coverage("evening", at(17, 30), DAY, TZ, midday_sent_at=midday_sent)
    assert evening_from >= midday_to


# --- daylight saving ------------------------------------------------------


def test_windows_follow_local_time_across_a_dst_change():
    """Zagreb leaves DST on 2026-10-25; 12:30 local must stay 12:30 local."""
    before = s.window_bounds("12:30-14:00", date(2026, 10, 23), TZ)[0]
    after = s.window_bounds("12:30-14:00", date(2026, 10, 26), TZ)[0]
    assert before.hour == after.hour == 12
    assert before.utcoffset() != after.utcoffset()
