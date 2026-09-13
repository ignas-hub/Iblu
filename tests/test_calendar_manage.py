"""calendar_manage unit tests — pure logic, no Google APIs and no database.

Google is never actually reached: mutating/reading actions go through a fake
`service.events()` object, and `settings` is monkeypatched per the frozen-
dataclass rule in conftest.py (substitute the module object, never patch
attributes on the real `settings`).
"""

from __future__ import annotations

import pytest

from iblu_keeper.tools import calendar_manage as CM

TZ = "Europe/Zagreb"


class _LiveSettings:
    use_mock = False
    dry_run = False
    iblu_timezone = TZ


class _MockSettings:
    use_mock = True
    dry_run = True
    iblu_timezone = TZ


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(CM, "settings", _LiveSettings())
    return _LiveSettings()


# --- fake Google Calendar service -------------------------------------------


class _Request:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeEvents:
    def __init__(self, items=None, get_result=None):
        self.items = items or []
        self.get_result = get_result
        self.calls: dict = {}

    def list(self, **kwargs):
        self.calls["list"] = kwargs
        return _Request({"items": self.items})

    def get(self, **kwargs):
        self.calls["get"] = kwargs
        return _Request(self.get_result)

    def patch(self, **kwargs):
        self.calls.setdefault("patch", []).append(kwargs)
        body = kwargs.get("body") or {}
        merged = dict(self.get_result or {})
        merged.update(body)
        merged["id"] = kwargs.get("eventId")
        return _Request(merged)

    def delete(self, **kwargs):
        self.calls["delete"] = kwargs
        return _Request({})


class _FakeService:
    def __init__(self, events: _FakeEvents):
        self._events = events

    def events(self):
        return self._events


def _install_fake_service(monkeypatch, events: _FakeEvents) -> _FakeService:
    service = _FakeService(events)
    monkeypatch.setattr(CM, "_service", lambda: service)
    return service


def _touched_google(monkeypatch):
    """Fail the test if anything calls `_service()`."""

    def _boom():
        raise AssertionError("mock mode must never call _service()")

    monkeypatch.setattr(CM, "_service", _boom)


def _dt_event(start, end, summary="Meeting", status="confirmed", attendees=None):
    ev = {"start": {"dateTime": start}, "end": {"dateTime": end}, "summary": summary, "status": status}
    if attendees is not None:
        ev["attendees"] = attendees
    return ev


def _all_day_event(date_str, summary="Off"):
    d = date_str
    from datetime import date as _date, timedelta as _td

    next_day = (_date.fromisoformat(d) + _td(days=1)).isoformat()
    return {"start": {"date": d}, "end": {"date": next_day}, "summary": summary, "status": "confirmed"}


# --- date/datetime parsing ---------------------------------------------------


def test_parses_bare_date_as_midnight():
    tz = CM.ZoneInfo(TZ)
    parsed = CM._parse_datetime("2026-09-14", tz)
    assert (parsed.hour, parsed.minute, parsed.second) == (0, 0, 0)
    assert parsed.tzinfo is not None


def test_parses_naive_datetime_as_local():
    tz = CM.ZoneInfo(TZ)
    parsed = CM._parse_datetime("2026-09-14T10:30:00", tz)
    assert (parsed.hour, parsed.minute) == (10, 30)


def test_parses_z_suffixed_datetime_and_converts():
    tz = CM.ZoneInfo(TZ)
    parsed = CM._parse_datetime("2026-09-14T10:00:00Z", tz)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() is not None


def test_bad_input_raises_calendar_error_naming_the_expectation():
    tz = CM.ZoneInfo(TZ)
    with pytest.raises(CM.CalendarError, match="ISO date"):
        CM._parse_datetime("not-a-date", tz)
    with pytest.raises(CM.CalendarError):
        CM._parse_datetime("", tz)
    with pytest.raises(CM.CalendarError):
        CM._parse_datetime(None, tz)  # type: ignore[arg-type]


def test_bad_hhmm_raises_calendar_error():
    with pytest.raises(CM.CalendarError, match="HH:MM"):
        CM._parse_hhmm("not-a-time")


def test_days_must_be_positive():
    tz = CM.ZoneInfo(TZ)
    with pytest.raises(CM.CalendarError, match="days"):
        CM._window(None, 0, tz)


def test_work_start_must_precede_work_end(live, monkeypatch):
    _install_fake_service(monkeypatch, _FakeEvents(items=[]))
    with pytest.raises(CM.CalendarError, match="work_start"):
        CM.find_slots(start="2026-09-14", days=1, work_start="18:00", work_end="09:00")


# --- free-slot computation ---------------------------------------------------


def test_empty_day_is_one_full_working_slot(live, monkeypatch):
    _install_fake_service(monkeypatch, _FakeEvents(items=[]))
    slots = CM.find_slots(start="2026-09-14", days=1, minutes=30, work_start="09:00", work_end="18:00")
    assert len(slots) == 1
    assert slots[0]["start"].startswith("2026-09-14T09:00:00")
    assert slots[0]["end"].startswith("2026-09-14T18:00:00")
    assert slots[0]["minutes"] == 9 * 60


def test_one_meeting_mid_day_splits_into_two_slots(live, monkeypatch):
    items = [_dt_event("2026-09-14T12:00:00+02:00", "2026-09-14T13:00:00+02:00")]
    _install_fake_service(monkeypatch, _FakeEvents(items=items))
    slots = CM.find_slots(start="2026-09-14", days=1, minutes=30)
    assert len(slots) == 2
    assert slots[0]["end"].startswith("2026-09-14T12:00:00")
    assert slots[1]["start"].startswith("2026-09-14T13:00:00")
    # chronological
    assert slots[0]["start"] < slots[1]["start"]


def test_back_to_back_meetings_leave_no_slot(live, monkeypatch):
    items = [
        _dt_event("2026-09-14T09:00:00+02:00", "2026-09-14T13:30:00+02:00"),
        _dt_event("2026-09-14T13:30:00+02:00", "2026-09-14T18:00:00+02:00"),
    ]
    _install_fake_service(monkeypatch, _FakeEvents(items=items))
    slots = CM.find_slots(start="2026-09-14", days=1, minutes=15)
    assert slots == []


def test_slot_exactly_equal_to_requested_minutes_is_included(live, monkeypatch):
    # 09:00-17:30 busy, leaving exactly 30 minutes free (17:30-18:00).
    items = [_dt_event("2026-09-14T09:00:00+02:00", "2026-09-14T17:30:00+02:00")]
    _install_fake_service(monkeypatch, _FakeEvents(items=items))
    slots = CM.find_slots(start="2026-09-14", days=1, minutes=30)
    assert len(slots) == 1
    assert slots[0]["minutes"] == 30


def test_all_day_event_does_not_block_slots(live, monkeypatch):
    items = [_all_day_event("2026-09-14")]
    _install_fake_service(monkeypatch, _FakeEvents(items=items))
    slots = CM.find_slots(start="2026-09-14", days=1, minutes=30)
    assert len(slots) == 1
    assert slots[0]["minutes"] == 9 * 60


def test_declined_event_does_not_block_slots(live, monkeypatch):
    items = [
        _dt_event(
            "2026-09-14T12:00:00+02:00",
            "2026-09-14T13:00:00+02:00",
            attendees=[{"email": "ignas@blanklabel.team", "self": True, "responseStatus": "declined"}],
        )
    ]
    _install_fake_service(monkeypatch, _FakeEvents(items=items))
    slots = CM.find_slots(start="2026-09-14", days=1, minutes=30)
    assert len(slots) == 1
    assert slots[0]["minutes"] == 9 * 60


def test_weekend_is_skipped_by_default(live, monkeypatch):
    # 2026-09-12 is a Saturday, 2026-09-13 Sunday, 2026-09-14 Monday.
    _install_fake_service(monkeypatch, _FakeEvents(items=[]))
    slots = CM.find_slots(start="2026-09-12", days=3, minutes=30)
    days_present = {s["start"][:10] for s in slots}
    assert days_present == {"2026-09-14"}


def test_weekend_is_included_when_asked(live, monkeypatch):
    _install_fake_service(monkeypatch, _FakeEvents(items=[]))
    slots = CM.find_slots(start="2026-09-12", days=3, minutes=30, include_weekends=True)
    days_present = {s["start"][:10] for s in slots}
    assert days_present == {"2026-09-12", "2026-09-13", "2026-09-14"}


def test_slots_are_clipped_to_working_hours(live, monkeypatch):
    # meeting spills past the working day close; slot should stop at work_end.
    items = [_dt_event("2026-09-14T17:00:00+02:00", "2026-09-14T20:00:00+02:00")]
    _install_fake_service(monkeypatch, _FakeEvents(items=items))
    slots = CM.find_slots(start="2026-09-14", days=1, minutes=30, work_start="09:00", work_end="18:00")
    assert len(slots) == 1
    assert slots[0]["end"].startswith("2026-09-14T17:00:00")


# --- list ---------------------------------------------------------------


def test_list_skips_cancelled_and_reports_self_block(live, monkeypatch):
    items = [
        _dt_event("2026-09-14T09:00:00+02:00", "2026-09-14T09:30:00+02:00", summary="Focus block"),
        _dt_event(
            "2026-09-14T10:00:00+02:00", "2026-09-14T10:30:00+02:00",
            summary="Cancelled", status="cancelled",
        ),
        _dt_event(
            "2026-09-14T11:00:00+02:00", "2026-09-14T11:30:00+02:00", summary="Sync",
            attendees=[{"email": "a@x.com"}, {"email": "ignas@blanklabel.team", "self": True, "responseStatus": "accepted"}],
        ),
    ]
    _install_fake_service(monkeypatch, _FakeEvents(items=items))
    result = CM.list_events(start="2026-09-14", days=1)
    assert result["count"] == 2
    focus, sync = result["events"]
    assert focus["is_self_block"] is True
    assert focus["attendee_count"] == 0
    assert sync["attendee_count"] == 2
    assert sync["self_response"] == "accepted"
    assert sync["is_self_block"] is False


def test_list_marks_all_day_events(live, monkeypatch):
    items = [_all_day_event("2026-09-14", summary="Conference")]
    _install_fake_service(monkeypatch, _FakeEvents(items=items))
    result = CM.list_events(start="2026-09-14", days=1)
    assert result["events"][0]["all_day"] is True


# --- move ---------------------------------------------------------------


def test_move_by_minutes_preserves_duration(live, monkeypatch):
    get_result = {
        "id": "evt1",
        "start": {"dateTime": "2026-09-14T10:00:00+02:00"},
        "end": {"dateTime": "2026-09-14T11:00:00+02:00"},
    }
    events = _FakeEvents(get_result=get_result)
    _install_fake_service(monkeypatch, events)
    out = CM.move_event("evt1", minutes=90)
    patch_body = events.calls["patch"][0]["body"]
    new_start = CM.datetime.fromisoformat(patch_body["start"]["dateTime"])
    new_end = CM.datetime.fromisoformat(patch_body["end"]["dateTime"])
    assert (new_end - new_start) == CM.timedelta(hours=1)
    assert new_start.hour == 11 and new_start.minute == 30
    assert out["status"] == "moved"


def test_move_to_absolute_start_preserves_duration(live, monkeypatch):
    get_result = {
        "id": "evt1",
        "start": {"dateTime": "2026-09-14T10:00:00+02:00"},
        "end": {"dateTime": "2026-09-14T11:30:00+02:00"},
    }
    events = _FakeEvents(get_result=get_result)
    _install_fake_service(monkeypatch, events)
    CM.move_event("evt1", start="2026-09-15T08:00:00")
    patch_body = events.calls["patch"][0]["body"]
    new_start = CM.datetime.fromisoformat(patch_body["start"]["dateTime"])
    new_end = CM.datetime.fromisoformat(patch_body["end"]["dateTime"])
    assert (new_end - new_start) == CM.timedelta(hours=1, minutes=30)
    assert new_start.hour == 8


def test_move_requires_exactly_one_of_minutes_or_start(live, monkeypatch):
    with pytest.raises(CM.CalendarError, match="exactly one"):
        CM.move_event("evt1")
    with pytest.raises(CM.CalendarError, match="exactly one"):
        CM.move_event("evt1", minutes=10, start="2026-09-15T08:00:00")


# --- update ---------------------------------------------------------------


def test_update_sends_only_the_fields_given(live, monkeypatch):
    events = _FakeEvents(get_result={"id": "evt1"})
    _install_fake_service(monkeypatch, events)
    CM.update_event("evt1", summary="New title", location="Room 2")
    body = events.calls["patch"][0]["body"]
    assert body == {"summary": "New title", "location": "Room 2"}


def test_update_with_no_fields_raises(live):
    with pytest.raises(CM.CalendarError, match="at least one"):
        CM.update_event("evt1")


def test_update_start_and_end_become_datetime_bodies(live, monkeypatch):
    events = _FakeEvents(get_result={"id": "evt1"})
    _install_fake_service(monkeypatch, events)
    CM.update_event("evt1", start="2026-09-14T09:00:00", end="2026-09-14T10:00:00")
    body = events.calls["patch"][0]["body"]
    assert set(body) == {"start", "end"}
    assert "dateTime" in body["start"] and "dateTime" in body["end"]


# --- delete ---------------------------------------------------------------


def test_delete_calls_events_delete(live, monkeypatch):
    events = _FakeEvents()
    _install_fake_service(monkeypatch, events)
    out = CM.delete_event("evt1")
    assert events.calls["delete"]["eventId"] == "evt1"
    assert out["status"] == "deleted"


# --- mock mode --------------------------------------------------------------


def test_manage_mock_mode_returns_generic_mock_shape_and_never_touches_google(monkeypatch):
    monkeypatch.setattr(CM, "settings", _MockSettings())
    _touched_google(monkeypatch)
    assert CM.manage(action="list") == {"status": "mock"}
    assert CM.manage(action="find_slot") == {"status": "mock"}
    assert CM.manage(action="update", event_id="e1", summary="x") == {"status": "mock"}
    assert CM.manage(action="move", event_id="e1", minutes=10) == {"status": "mock"}
    assert CM.manage(action="delete", event_id="e1") == {"status": "mock"}


def test_update_event_mock_mode_never_touches_google(monkeypatch):
    monkeypatch.setattr(CM, "settings", _MockSettings())
    _touched_google(monkeypatch)
    out = CM.update_event("evt1", summary="x")
    assert out["_mock"] is True
    assert out["status"] == "not_updated_mock"


def test_move_event_mock_mode_never_touches_google(monkeypatch):
    monkeypatch.setattr(CM, "settings", _MockSettings())
    _touched_google(monkeypatch)
    out = CM.move_event("evt1", minutes=15)
    assert out["_mock"] is True
    assert out["status"] == "not_moved_mock"


def test_delete_event_mock_mode_never_touches_google(monkeypatch):
    monkeypatch.setattr(CM, "settings", _MockSettings())
    _touched_google(monkeypatch)
    out = CM.delete_event("evt1")
    assert out["_mock"] is True
    assert out["status"] == "not_deleted_mock"


# --- dispatch errors ---------------------------------------------------------


def test_unknown_action_names_the_valid_ones(live, monkeypatch):
    with pytest.raises(CM.CalendarError) as exc:
        CM.manage(action="teleport")
    assert "list" in str(exc.value) and "delete" in str(exc.value)


def test_manage_mutating_actions_require_event_id(live):
    with pytest.raises(CM.CalendarError, match="event_id"):
        CM.manage(action="update")
    with pytest.raises(CM.CalendarError, match="event_id"):
        CM.manage(action="move")
    with pytest.raises(CM.CalendarError, match="event_id"):
        CM.manage(action="delete")
