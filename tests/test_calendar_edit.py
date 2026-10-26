"""calendar_edit — request shapes, refusals, and the two promises:
attendees are not emailed by default, and cancel never deletes.
"""

from __future__ import annotations

import inspect

import pytest
from _fake_google import Recorder

from iblu_keeper.tools import calendar_edit as CE
from iblu_keeper.tools.calendar_manage import CalendarError

TZ = "Europe/Zagreb"

EVENT = {
    "id": "e1", "summary": "Standup", "htmlLink": "https://cal/e1", "status": "confirmed",
    "start": {"dateTime": "2026-09-14T10:00:00+02:00"},
    "end": {"dateTime": "2026-09-14T10:30:00+02:00"},
    "attendees": [
        {"email": "boss@x.com", "responseStatus": "accepted", "organizer": True},
        {"email": "me@x.com", "responseStatus": "needsAction", "self": True},
        {"email": "peer@x.com", "responseStatus": "tentative"},
    ],
    "organizer": {"email": "boss@x.com"},
}


class _Live:
    use_mock = False
    dry_run = False
    iblu_timezone = TZ


@pytest.fixture
def live(monkeypatch):
    rec = Recorder({"events.get": EVENT, "events.patch": EVENT})
    monkeypatch.setattr(CE, "settings", _Live())
    monkeypatch.setattr(CE, "_service", lambda account=None: rec)
    return rec


# --- refusals --------------------------------------------------------------


def test_unknown_action_is_refused():
    """A typo must not land on a default that edits something."""
    with pytest.raises(CalendarError, match="unknown action"):
        CE.calendar_edit("delete", event_id="e1")


@pytest.mark.parametrize("action", ["get", "update", "cancel", "rsvp", "move", "instances"])
def test_event_actions_require_event_id(action):
    """events.get(eventId=None) is a 404 from Google, not a sentence a caller can act on."""
    with pytest.raises(CalendarError, match="event_id"):
        CE.calendar_edit(action)


def test_invalid_send_updates_is_refused():
    """A misspelt value would be sent as-is and Google 400s — or worse, a typo becomes 'all'."""
    with pytest.raises(CalendarError, match="send_updates"):
        CE.calendar_edit("update", event_id="e1", title="x", send_updates="everyone")


def test_mock_mode_never_builds_a_service(monkeypatch):
    """DRY_RUN must not reach Calendar, and the status must say nothing happened."""
    def boom(account=None):
        raise AssertionError("service built in mock mode")

    monkeypatch.setattr(CE, "_service", boom)
    out = CE.calendar_edit("cancel", event_id="e1")
    assert out["_mock"] is True
    assert out["status"] == "not_cancelled_mock"


# --- get -------------------------------------------------------------------


def test_get_returns_attendees_and_organizer(live):
    """The compact view hides who is invited; the whole point of `get` is seeing them."""
    out = CE.calendar_edit("get", event_id="e1")
    assert live.calls_to("events.get") == [{"calendarId": "primary", "eventId": "e1"}]
    assert out["summary"] == "Standup"
    assert out["organizer"] == "boss@x.com"
    assert [a["email"] for a in out["attendees"]] == ["boss@x.com", "me@x.com", "peer@x.com"]
    assert out["attendees"][1]["self"] is True
    assert out["self_response"] == "needsAction"


# --- update ----------------------------------------------------------------


def test_update_sends_only_given_fields_and_send_updates_none_by_default(live):
    """Omitting sendUpdates lets Google email every attendee about a title fix."""
    CE.calendar_edit("update", event_id="e1", title="Standup (moved)")
    assert live.calls_to("events.patch") == [{
        "calendarId": "primary", "eventId": "e1",
        "body": {"summary": "Standup (moved)"}, "sendUpdates": "none",
    }]


def test_update_passes_send_updates_all_when_asked(live):
    """'all' is the explicit go; it must reach Google unchanged."""
    out = CE.calendar_edit("update", event_id="e1", location="Room 2", send_updates="all")
    assert live.calls_to("events.patch")[0]["sendUpdates"] == "all"
    assert out["send_updates"] == "all"
    assert out["fields"] == ["location"]


def test_update_naive_time_gets_the_account_timezone(live):
    """'14:00' with no offset must become Zagreb time, not UTC."""
    CE.calendar_edit("update", event_id="e1", start="2026-09-14T14:00", end="2026-09-15")
    body = live.calls_to("events.patch")[0]["body"]
    assert body["start"] == {"dateTime": "2026-09-14T14:00:00+02:00"}
    assert body["end"] == {"date": "2026-09-15"}


def test_update_attendees_merges_with_the_current_list(live):
    """patch replaces the attendees array; sending only the new one drops everyone else."""
    CE.calendar_edit("update", event_id="e1", add_attendees="new@x.com, peer@x.com", remove_attendees=["boss@x.com"])
    body = live.calls_to("events.patch")[0]["body"]
    assert body["attendees"] == [
        {"email": "me@x.com", "responseStatus": "needsAction", "self": True},
        {"email": "peer@x.com", "responseStatus": "tentative"},
        {"email": "new@x.com"},
    ]


def test_update_with_nothing_to_change_is_refused(live):
    """An empty patch is a no-op that still looks like a success."""
    with pytest.raises(CalendarError, match="at least one"):
        CE.calendar_edit("update", event_id="e1")
    assert live.calls_to("events.patch") == []


# --- cancel ----------------------------------------------------------------


def test_cancel_sets_status_cancelled_and_never_deletes(live):
    """events.delete removes the tombstone calendar_seen relies on (HANDOFF §13)."""
    out = CE.calendar_edit("cancel", event_id="e1")
    assert live.calls_to("events.patch") == [{
        "calendarId": "primary", "eventId": "e1", "body": {"status": "cancelled"}, "sendUpdates": "none",
    }]
    assert "events.delete" not in live.paths()
    assert out["status"] == "cancelled"
    assert "events().delete" not in inspect.getsource(CE)


# --- rsvp ------------------------------------------------------------------


def test_rsvp_patches_only_my_attendee_entry(live):
    """Changing anyone else's responseStatus would answer on their behalf."""
    out = CE.calendar_edit("rsvp", event_id="e1", response="accepted", comment="See you there")
    body = live.calls_to("events.patch")[0]["body"]
    assert body == {"attendees": [
        {"email": "boss@x.com", "responseStatus": "accepted", "organizer": True},
        {"email": "me@x.com", "responseStatus": "accepted", "self": True, "comment": "See you there"},
        {"email": "peer@x.com", "responseStatus": "tentative"},
    ]}
    assert live.calls_to("events.patch")[0]["sendUpdates"] == "none"
    assert out["status"] == "responded"
    assert out["response"] == "accepted"


def test_rsvp_refuses_an_unknown_response_and_an_event_without_me(live):
    """'maybe' is not a Google status; and a self-only event has nothing to answer."""
    with pytest.raises(CalendarError, match="response must be"):
        CE.calendar_edit("rsvp", event_id="e1", response="maybe")
    live.responses["events.get"] = {**EVENT, "attendees": []}
    with pytest.raises(CalendarError, match="not on this event"):
        CE.calendar_edit("rsvp", event_id="e1", response="accepted")
    assert live.calls_to("events.patch") == []


# --- move / instances / quick_add / list_range -------------------------------


def test_move_calls_events_move_with_destination(live):
    """A move is its own endpoint; patching calendarId does nothing."""
    live.responses["events.move"] = {**EVENT, "id": "e1"}
    out = CE.calendar_edit("move", event_id="e1", destination_calendar_id="work@group.calendar.google.com")
    assert live.calls_to("events.move") == [{
        "calendarId": "primary", "eventId": "e1",
        "destination": "work@group.calendar.google.com", "sendUpdates": "none",
    }]
    assert out["calendar_id"] == "work@group.calendar.google.com"
    assert out["from_calendar_id"] == "primary"
    assert out["status"] == "moved"


def test_instances_expands_from_now_and_drops_cancelled(live):
    """Without events.instances a weekly series is one event pinned to its first date."""
    live.responses["events.instances"] = {"items": [
        {**EVENT, "id": "e1_20260914"},
        {**EVENT, "id": "e1_20260921", "status": "cancelled"},
    ]}
    out = CE.calendar_edit("instances", event_id="e1", start="2026-09-14", limit=5)
    kw = live.calls_to("events.instances")[0]
    assert kw["eventId"] == "e1" and kw["maxResults"] == 5
    assert kw["timeMin"] == "2026-09-14T00:00:00+02:00"
    assert [i["id"] for i in out["instances"]] == ["e1_20260914"]


def test_quick_add_passes_the_sentence_and_send_updates(live):
    """quickAdd is a query parameter, not a body; and attendees parsed from it must not be emailed by default."""
    live.responses["events.quickAdd"] = {**EVENT, "id": "q1", "summary": "Lunch with Marta"}
    out = CE.calendar_edit("quick_add", text="Lunch with Marta Friday 1pm")
    assert live.calls_to("events.quickAdd") == [{
        "calendarId": "primary", "text": "Lunch with Marta Friday 1pm", "sendUpdates": "none",
    }]
    assert out["status"] == "created"
    assert out["summary"] == "Lunch with Marta"


def test_list_range_queries_single_ordered_instances_in_the_window(live):
    """Without singleEvents/orderBy the list is unexpanded and unsorted; the query must reach Google."""
    live.responses["events.list"] = {"items": [EVENT]}
    out = CE.calendar_edit("list_range", start="2026-09-14", end="2026-09-16", query="standup", limit=50)
    assert live.calls_to("events.list") == [{
        "calendarId": "primary",
        "timeMin": "2026-09-14T00:00:00+02:00", "timeMax": "2026-09-16T00:00:00+02:00",
        "singleEvents": True, "orderBy": "startTime", "maxResults": 50, "q": "standup",
    }]
    assert out["count"] == 1
    assert out["events"][0]["summary"] == "Standup"


def test_list_range_requires_both_ends_in_order(live):
    """An inverted window returns nothing and reads as 'free all day'."""
    with pytest.raises(CalendarError, match="start and end"):
        CE.calendar_edit("list_range", start="2026-09-14")
    with pytest.raises(CalendarError, match="after start"):
        CE.calendar_edit("list_range", start="2026-09-16", end="2026-09-14")
