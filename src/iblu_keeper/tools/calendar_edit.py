"""The calendar verbs `calendar_manage` stopped short of.

`tools/calendar_manage.py` lists, finds slots, creates, updates summary/time/
place, moves by minutes and deletes. It cannot show one event in full with
its attendees, change who is invited, answer an invitation, cancel without
deleting, move an event to another calendar, expand a recurring series, take
a sentence like "lunch with Marta Friday 1pm", or list a range with a query.
Each of those was a request in conversation. Ignas, 2026-10-26: "It's crazy
how many times I have to ask to add tools." This module is the calendar part
of the batch that ends that.

It is ONE MCP tool with an `action` parameter, in a new module rather than a
wider `calendar_manage` because changing an existing tool's schema resets it
to "Ask" in his client (HANDOFF §12.3) — a new tool costs one click, an
edited one costs the same click *and* loses the Allow it already had.

Two deliberate choices:

* **Attendees are not emailed unless asked.** Every action that Google would
  notify people about takes ``send_updates`` and it defaults to ``"none"``.
  Passing ``"all"`` or ``"externalOnly"`` is the explicit "go".
* **`cancel` never calls `events.delete`.** It patches ``status=cancelled``,
  which keeps the tombstone Calendar uses for sync and that
  `calendar_seen` depends on (HANDOFF §13). `calendar_manage` already owns
  the hard delete for the cases that truly need it.

Scope: `calendar.events` only (verified 2026-10-26). `freebusy` and
`calendarList` are 403 under this token and are not implemented; `quickAdd`,
`move`, `instances` and attendee patches all sit inside `calendar.events`.

Times are ISO-8601. A value with no offset is read in `settings.iblu_timezone`;
a bare date (YYYY-MM-DD) is an all-day edge.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import resolve_account, settings
from .calendar_manage import CalendarError, _compact_event, _parse_datetime

logger = logging.getLogger("iblu_keeper.tools.calendar_edit")

ACTIONS = ("get", "update", "cancel", "rsvp", "move", "instances", "quick_add", "list_range")
SEND_UPDATES = ("none", "all", "externalOnly")
RESPONSES = ("accepted", "declined", "tentative")

_MOCK_STATUS = {
    "get": "not_read_mock",
    "update": "not_updated_mock",
    "cancel": "not_cancelled_mock",
    "rsvp": "not_responded_mock",
    "move": "not_moved_mock",
    "instances": "not_read_mock",
    "quick_add": "not_created_mock",
    "list_range": "not_read_mock",
}

_NEEDS_EVENT = ("get", "update", "cancel", "rsvp", "move", "instances")


def _service(account: str | None = None):
    from ..google_auth import build_service

    return build_service("calendar", "v3", account=account)


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.iblu_timezone)


def _when(value: str) -> dict:
    """An event edge for the API: {"date": …} for a bare date, else {"dateTime": …}
    carrying the account timezone's offset when the caller gave none."""
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return {"date": text}
        except ValueError:
            pass
    return {"dateTime": _parse_datetime(text, _tz()).isoformat()}


def _attendee_view(a: dict) -> dict:
    return {
        "email": a.get("email", ""),
        "name": a.get("displayName", ""),
        "response": a.get("responseStatus", ""),
        "optional": bool(a.get("optional", False)),
        "organizer": bool(a.get("organizer", False)),
        "self": bool(a.get("self", False)),
        **({"comment": a["comment"]} if a.get("comment") else {}),
    }


def _full_event(event: dict, tz: ZoneInfo) -> dict:
    """Everything a person asks about one event, on top of the compact view."""
    view = _compact_event(event, tz)
    view.update({
        "description": event.get("description", ""),
        "status": event.get("status", ""),
        "attendees": [_attendee_view(a) for a in event.get("attendees", []) or []],
        "organizer": (event.get("organizer") or {}).get("email", ""),
        "creator": (event.get("creator") or {}).get("email", ""),
        "recurrence": event.get("recurrence", []) or [],
        "recurring_event_id": event.get("recurringEventId"),
        "hangout_link": event.get("hangoutLink", ""),
        "visibility": event.get("visibility", "default"),
        "transparency": event.get("transparency", "opaque"),
        "created": event.get("created", ""),
        "updated": event.get("updated", ""),
    })
    return view


def _check_send_updates(send_updates: str) -> str:
    if send_updates not in SEND_UPDATES:
        raise CalendarError(
            f"send_updates must be one of {', '.join(SEND_UPDATES)}, got {send_updates!r}"
        )
    return send_updates


def _emails(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [e.strip().lower() for e in value if e and e.strip()]


# --- actions -----------------------------------------------------------------


def _get(service, calendar_id: str, event_id: str) -> dict:
    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    return {"status": "ok", "calendar_id": calendar_id, **_full_event(event, _tz())}


def _update(
    service, calendar_id: str, event_id: str, title, start, end, description, location,
    add_attendees, remove_attendees, send_updates: str,
) -> dict:
    body: dict = {}
    if title is not None:
        body["summary"] = title
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if start is not None:
        body["start"] = _when(start)
    if end is not None:
        body["end"] = _when(end)

    adds = _emails(add_attendees)
    removes = set(_emails(remove_attendees))
    if adds or removes:
        # `patch` replaces the attendees array wholesale, so the merge has to
        # happen here against the current list or everyone else drops off.
        current = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        attendees = [
            a for a in (current.get("attendees", []) or [])
            if (a.get("email") or "").lower() not in removes
        ]
        present = {(a.get("email") or "").lower() for a in attendees}
        for e in adds:
            if e not in present:
                attendees.append({"email": e})
                present.add(e)
        body["attendees"] = attendees

    if not body:
        raise CalendarError(
            "update requires at least one of title, start, end, description, location, "
            "add_attendees, remove_attendees"
        )
    updated = (
        service.events()
        .patch(calendarId=calendar_id, eventId=event_id, body=body, sendUpdates=send_updates)
        .execute()
    )
    logger.info("calendar_edit update id=%s fields=%s send_updates=%s", event_id, sorted(body), send_updates)
    return {
        "status": "updated",
        "calendar_id": calendar_id,
        "fields": sorted(body),
        "send_updates": send_updates,
        **_full_event(updated, _tz()),
    }


def _cancel(service, calendar_id: str, event_id: str, send_updates: str) -> dict:
    updated = (
        service.events()
        .patch(calendarId=calendar_id, eventId=event_id, body={"status": "cancelled"}, sendUpdates=send_updates)
        .execute()
    )
    logger.info("calendar_edit cancel id=%s send_updates=%s", event_id, send_updates)
    return {
        "id": updated.get("id", event_id),
        "calendar_id": calendar_id,
        "summary": updated.get("summary", ""),
        "status": "cancelled",
        "send_updates": send_updates,
        "note": "status=cancelled via events.patch — not deleted; the tombstone stays for sync.",
    }


def _my_email(account: str | None) -> str:
    try:
        from .gmail import _from_address

        return (_from_address(account) or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _rsvp(service, calendar_id: str, event_id: str, response: str, comment: str | None, send_updates: str, account) -> dict:
    if response not in RESPONSES:
        raise CalendarError(f"response must be one of {', '.join(RESPONSES)}, got {response!r}")
    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    attendees = [dict(a) for a in (event.get("attendees", []) or [])]
    me = _my_email(account)
    mine = [a for a in attendees if a.get("self")] or [
        a for a in attendees if me and (a.get("email") or "").lower() == me
    ]
    if not mine:
        raise CalendarError(
            "you are not on this event's attendee list — an event you organise with no "
            "invitees has nothing to RSVP to"
        )
    for a in mine:
        a["responseStatus"] = response
        if comment is not None:
            a["comment"] = comment
    updated = (
        service.events()
        .patch(calendarId=calendar_id, eventId=event_id, body={"attendees": attendees}, sendUpdates=send_updates)
        .execute()
    )
    logger.info("calendar_edit rsvp id=%s response=%s", event_id, response)
    return {
        "id": updated.get("id", event_id),
        "calendar_id": calendar_id,
        "summary": updated.get("summary", event.get("summary", "")),
        "response": response,
        "send_updates": send_updates,
        "status": "responded",
    }


def _move(service, calendar_id: str, event_id: str, destination: str, send_updates: str) -> dict:
    moved = (
        service.events()
        .move(calendarId=calendar_id, eventId=event_id, destination=destination, sendUpdates=send_updates)
        .execute()
    )
    logger.info("calendar_edit move id=%s %s -> %s", event_id, calendar_id, destination)
    return {
        "id": moved.get("id", event_id),
        "from_calendar_id": calendar_id,
        "calendar_id": destination,
        "summary": moved.get("summary", ""),
        "html_link": moved.get("htmlLink"),
        "status": "moved",
    }


def _instances(service, calendar_id: str, event_id: str, start: str | None, limit: int) -> dict:
    tz = _tz()
    time_min = _parse_datetime(start, tz) if start else datetime.now(tz)
    resp = (
        service.events()
        .instances(calendarId=calendar_id, eventId=event_id, timeMin=time_min.isoformat(), maxResults=limit)
        .execute()
    )
    items = [
        _compact_event(e, tz)
        for e in resp.get("items", []) or []
        if e.get("status") != "cancelled"
    ]
    return {
        "status": "ok",
        "calendar_id": calendar_id,
        "recurring_event_id": event_id,
        "from": time_min.isoformat(),
        "count": len(items),
        "instances": items,
    }


def _quick_add(service, calendar_id: str, text: str, send_updates: str) -> dict:
    created = (
        service.events()
        .quickAdd(calendarId=calendar_id, text=text, sendUpdates=send_updates)
        .execute()
    )
    if not created.get("id"):
        raise RuntimeError(
            f"Calendar quickAdd returned no event id (response={created!r}); "
            "treating as failure rather than reporting a false success."
        )
    logger.info("calendar_edit quick_add %r -> %s", text, created["id"])
    return {"status": "created", "calendar_id": calendar_id, "text": text, **_compact_event(created, _tz())}


def _list_range(service, calendar_id: str, start: str, end: str, query: str | None, limit: int) -> dict:
    tz = _tz()
    time_min = _parse_datetime(start, tz)
    time_max = _parse_datetime(end, tz)
    if time_max <= time_min:
        raise CalendarError(f"end must be after start, got {start!r}..{end!r}")
    kwargs: dict = {
        "calendarId": calendar_id,
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": limit,
    }
    if query:
        kwargs["q"] = query
    resp = service.events().list(**kwargs).execute()
    items = [
        _compact_event(e, tz)
        for e in resp.get("items", []) or []
        if e.get("status") != "cancelled"
    ]
    return {
        "status": "ok",
        "calendar_id": calendar_id,
        "start": time_min.isoformat(),
        "end": time_max.isoformat(),
        "query": query or "",
        "count": len(items),
        "events": items,
        "next_page_token": resp.get("nextPageToken") or None,
    }


# --- dispatch ----------------------------------------------------------------


def calendar_edit(
    action: str,
    event_id: str | None = None,
    calendar_id: str = "primary",
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    add_attendees: list[str] | str | None = None,
    remove_attendees: list[str] | str | None = None,
    send_updates: str = "none",
    response: str | None = None,
    comment: str | None = None,
    destination_calendar_id: str | None = None,
    text: str | None = None,
    query: str | None = None,
    limit: int = 25,
    account: str | None = None,
) -> dict:
    """Edit and inspect single events. One tool, one `action`.

    Actions:
      get         event_id → the full event: times, attendees and their
                  responses, organizer, recurrence, links.
      update      any of title / start / end / description / location /
                  add_attendees / remove_attendees. `send_updates` defaults
                  to "none" — attendees are NOT emailed unless you pass
                  "all" or "externalOnly".
      cancel      sets status=cancelled (never events.delete). `send_updates`
                  as above.
      rsvp        `response` accepted | declined | tentative, optional
                  `comment` — patches only my own attendee entry.
      move        `destination_calendar_id` — move the event to another
                  calendar.
      instances   recurring event_id → the next `limit` instances (from
                  `start`, default now).
      quick_add   `text` in natural language ("Lunch with Marta Fri 1pm").
      list_range  `start`..`end` (ISO), optional `query` — single instances,
                  chronological, up to `limit`.

    Times are ISO-8601; no offset means `settings.iblu_timezone`; a bare date
    is all-day. ``account`` — ``blt`` (default), ``deadlift``, ``choco``.
    """
    if action not in ACTIONS:
        raise CalendarError(
            f"unknown action {action!r} — expected one of {', '.join(ACTIONS)}"
        )
    if action in _NEEDS_EVENT and not event_id:
        raise CalendarError(f"{action} requires event_id")
    if action == "rsvp" and not response:
        raise CalendarError(f"rsvp requires response: one of {', '.join(RESPONSES)}")
    if action == "move" and not destination_calendar_id:
        raise CalendarError("move requires destination_calendar_id")
    if action == "quick_add" and not (text and text.strip()):
        raise CalendarError("quick_add requires text")
    if action == "list_range" and not (start and end):
        raise CalendarError("list_range requires start and end (ISO date or datetime)")
    if limit < 1:
        raise CalendarError(f"limit must be >= 1, got {limit!r}")
    send_updates = _check_send_updates(send_updates)

    account = resolve_account(account)
    if settings.use_mock:
        return {
            "_mock": True,
            "action": action,
            "id": event_id,
            "calendar_id": calendar_id,
            "status": _MOCK_STATUS[action],
            "note": "MOCK MODE — nothing was read or changed in Calendar. Set DRY_RUN=false.",
        }

    service = _service(account)
    if action == "get":
        return _get(service, calendar_id, event_id)
    if action == "update":
        return _update(
            service, calendar_id, event_id, title, start, end, description, location,
            add_attendees, remove_attendees, send_updates,
        )
    if action == "cancel":
        return _cancel(service, calendar_id, event_id, send_updates)
    if action == "rsvp":
        return _rsvp(service, calendar_id, event_id, response, comment, send_updates, account)
    if action == "move":
        return _move(service, calendar_id, event_id, destination_calendar_id, send_updates)
    if action == "instances":
        return _instances(service, calendar_id, event_id, start, limit)
    if action == "quick_add":
        return _quick_add(service, calendar_id, text.strip(), send_updates)
    return _list_range(service, calendar_id, start, end, query, limit)
