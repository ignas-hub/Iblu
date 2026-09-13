"""Reading and reshaping the calendar Ignas already has, not just adding to it.

`tools/calendar.py` (Phase 1) can only create events — useful, but it means
IBLU still cannot answer "what's on today", "find me 30 minutes tomorrow", or
"push that call back an hour" without a human opening the calendar app. This
module is those five verbs.

It is deliberately ONE tool with an `action` param, not five separate MCP
tools — see `tools/repo.py`'s docstring: every new `@mcp.tool` is a permanent
manual permission click in the connector UI, so verbs that belong together
share one entry point.

`find_slot` computes free time itself from `events().list()` rather than
calling the Calendar freeBusy API. freeBusy needs no wider a scope in theory,
but this server currently only ever calls `events.*`, and reusing that same
surface means no new consent screen, no new scope to explain in HANDOFF.md,
and one less thing that can be half-authorized on a token refresh.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..config import settings

logger = logging.getLogger("iblu_keeper.calendar_manage")

_MOCK = {"status": "mock"}

ACTIONS = ("list", "find_slot", "create", "update", "move", "delete")


class CalendarError(ValueError):
    """Bad input to a `manage()` action. The message always names what was expected."""


def _service():
    from ..google_auth import build_service

    return build_service("calendar", "v3")


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.iblu_timezone)


# --- parsing ----------------------------------------------------------------


def _parse_datetime(value: str, tz: ZoneInfo) -> datetime:
    """Parse a caller-supplied ISO date or datetime string into an aware `tz` time.

    A bare date ("2026-09-14") means midnight that day — callers naming "the
    14th" almost never mean 00:00:00 precisely, but treating it as anything
    else would require guessing which other time they meant. A datetime with
    no offset is assumed to already be local (`tz`); one with an offset or a
    trailing "Z" is converted. Anything else is a caller mistake, and the
    error names exactly what was expected so the mistake is fixable without
    reading this source.
    """
    if not isinstance(value, str) or not value.strip():
        raise CalendarError(
            f"expected an ISO date (YYYY-MM-DD) or datetime (YYYY-MM-DDTHH:MM:SS), got {value!r}"
        )
    text = value.strip()
    try:
        d = date.fromisoformat(text)
        return datetime(d.year, d.month, d.day, tzinfo=tz)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalendarError(
            f"expected an ISO date (YYYY-MM-DD) or datetime (YYYY-MM-DDTHH:MM:SS), got {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _parse_hhmm(value: str) -> time:
    """Parse "HH:MM" (working-hours boundary). Never a full ISO time — that's `_parse_datetime`."""
    try:
        hour_str, minute_str = value.split(":")
        return time(int(hour_str), int(minute_str))
    except (ValueError, AttributeError) as exc:
        raise CalendarError(f"expected a time as HH:MM, got {value!r}") from exc


def _window_start(start: str | None, tz: ZoneInfo) -> datetime:
    """Midnight of `start`'s date (default: today), in `tz`.

    `list` and `find_slot` both reason in whole days — "today", "the next 3
    days" — so the window always begins at midnight even when a caller passes
    a specific time; a day either is or isn't in scope, there is no partial
    first day.
    """
    if start is None:
        now = datetime.now(tz)
        return datetime(now.year, now.month, now.day, tzinfo=tz)
    parsed = _parse_datetime(start, tz)
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=tz)


def _window(start: str | None, days: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    if days < 1:
        raise CalendarError(f"days must be >= 1, got {days!r}")
    begin = _window_start(start, tz)
    return begin, begin + timedelta(days=days)


def _edge(edge: dict, tz: ZoneInfo) -> tuple[datetime, bool]:
    """Turn a Google event's `start`/`end` object into (aware datetime, is_all_day)."""
    if "date" in edge:
        d = date.fromisoformat(edge["date"])
        return datetime(d.year, d.month, d.day, tzinfo=tz), True
    raw = edge.get("dateTime")
    if raw is None:
        raise CalendarError(f"event has neither 'date' nor 'dateTime': {edge!r}")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz), False


# --- Calendar API plumbing ---------------------------------------------------


def _fetch_events(service, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict]:
    """Raw events in [time_min, time_max). `singleEvents=True` expands recurring
    series into individual instances — without it a weekly standup shows up as
    one event pinned to its very first occurrence, which is useless for either
    "what's on today" or free-slot search.
    """
    response = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return response.get("items", [])


def _self_response(event: dict) -> str | None:
    for attendee in event.get("attendees", []):
        if attendee.get("self"):
            return attendee.get("responseStatus")
    return None


def _is_declined(event: dict) -> bool:
    return _self_response(event) == "declined"


def _compact_event(event: dict, tz: ZoneInfo) -> dict:
    start, all_day = _edge(event["start"], tz)
    end, _ = _edge(event["end"], tz)
    attendees = event.get("attendees", [])
    return {
        "id": event.get("id"),
        "summary": event.get("summary", ""),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "all_day": all_day,
        "attendee_count": len(attendees),
        "self_response": _self_response(event),
        "location": event.get("location", ""),
        "is_self_block": len(attendees) == 0,
        "html_link": event.get("htmlLink"),
    }


# --- action: list -------------------------------------------------------


def list_events(
    start: str | None = None,
    days: int = 1,
    calendar_id: str = "primary",
) -> dict:
    """Compact agenda for a window of days. Cancelled events are dropped —
    Calendar keeps a tombstone around for sync purposes, not for a human to see.
    """
    tz = _tz()
    time_min, time_max = _window(start, days, tz)
    items = _fetch_events(_service(), calendar_id, time_min, time_max)
    events = [_compact_event(e, tz) for e in items if e.get("status") != "cancelled"]
    return {
        "calendar_id": calendar_id,
        "start": time_min.isoformat(),
        "days": days,
        "count": len(events),
        "events": events,
    }


# --- action: find_slot ---------------------------------------------------


def _busy_intervals(items: list[dict], tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """Busy (start, end) pairs, sorted and merged. All-day events and events the
    self attendee declined don't actually block the calendar, so both are
    dropped rather than treated as busy time.
    """
    busy = []
    for event in items:
        if event.get("status") == "cancelled":
            continue
        if _is_declined(event):
            continue
        start, all_day = _edge(event["start"], tz)
        if all_day:
            continue
        end, _ = _edge(event["end"], tz)
        if end > start:
            busy.append((start, end))

    busy.sort(key=lambda pair: pair[0])
    merged: list[tuple[datetime, datetime]] = []
    for s, e in busy:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def find_slots(
    start: str | None = None,
    days: int = 1,
    minutes: int = 30,
    calendar_id: str = "primary",
    work_start: str = "09:00",
    work_end: str = "18:00",
    include_weekends: bool = False,
) -> list[dict]:
    """Free windows of at least `minutes`, chronological, clipped to working hours.

    Deliberately reasons purely from `events().list()` (see module docstring) —
    never `freeBusy` — so this never needs a wider OAuth scope than the create/
    read/patch calls the rest of this file already makes.
    """
    if minutes < 1:
        raise CalendarError(f"minutes must be >= 1, got {minutes!r}")
    tz = _tz()
    window_start, window_end = _window(start, days, tz)
    w_start = _parse_hhmm(work_start)
    w_end = _parse_hhmm(work_end)
    if w_start >= w_end:
        raise CalendarError(
            f"work_start must be before work_end, got {work_start!r}..{work_end!r}"
        )

    items = _fetch_events(_service(), calendar_id, window_start, window_end)
    busy = _busy_intervals(items, tz)
    duration = timedelta(minutes=minutes)

    slots: list[tuple[datetime, datetime]] = []
    day = window_start
    while day < window_end:
        if include_weekends or day.weekday() < 5:  # Mon=0 .. Sun=6
            day_start = datetime.combine(day.date(), w_start, tzinfo=tz)
            day_end = datetime.combine(day.date(), w_end, tzinfo=tz)
            cursor = day_start
            for busy_start, busy_end in busy:
                if busy_end <= day_start or busy_start >= day_end:
                    continue  # doesn't overlap today's working hours at all
                clipped_start = max(busy_start, day_start)
                clipped_end = min(busy_end, day_end)
                if clipped_start - cursor >= duration:
                    slots.append((cursor, clipped_start))
                if clipped_end > cursor:
                    cursor = clipped_end
            if day_end - cursor >= duration:
                slots.append((cursor, day_end))
        day += timedelta(days=1)

    return [
        {
            "start": s.isoformat(),
            "end": e.isoformat(),
            "minutes": int((e - s).total_seconds() // 60),
        }
        for s, e in slots
    ]


# --- action: update -------------------------------------------------------


def update_event(
    event_id: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    calendar_id: str = "primary",
) -> dict:
    """Patch an existing event. Only the fields the caller actually passed are
    sent — `events().patch` (unlike `update`) already merges rather than
    replaces, but building the body from only-what-changed also means the
    audit trail (this function's log line) says exactly what changed.
    """
    if settings.use_mock:
        logger.warning("MOCK update_event id=%s — NOT actually updated (DRY_RUN).", event_id)
        return {
            "_mock": True,
            "id": event_id,
            "calendar_id": calendar_id,
            "status": "not_updated_mock",
            "note": "MOCK MODE — event was NOT updated. Set DRY_RUN=false.",
        }

    tz = _tz()
    body: dict = {}
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if start is not None:
        body["start"] = {"dateTime": _parse_datetime(start, tz).isoformat()}
    if end is not None:
        body["end"] = {"dateTime": _parse_datetime(end, tz).isoformat()}
    if not body:
        raise CalendarError(
            "update requires at least one of summary, start, end, description, location"
        )

    updated = (
        _service()
        .events()
        .patch(calendarId=calendar_id, eventId=event_id, body=body)
        .execute()
    )
    logger.info("update_event id=%s fields=%s", event_id, sorted(body))
    return {
        "id": updated.get("id", event_id),
        "calendar_id": calendar_id,
        "summary": updated.get("summary", ""),
        "start": updated.get("start", {}).get("dateTime") or updated.get("start", {}).get("date"),
        "end": updated.get("end", {}).get("dateTime") or updated.get("end", {}).get("date"),
        "html_link": updated.get("htmlLink"),
        "status": "updated",
    }


# --- action: move -------------------------------------------------------


def move_event(
    event_id: str,
    minutes: int | None = None,
    start: str | None = None,
    calendar_id: str = "primary",
) -> dict:
    """Shift an event, keeping its duration. Exactly one of `minutes` (relative,
    can be negative) or `start` (absolute new start) must be given — mixing
    both would leave it ambiguous which one wins.
    """
    if (minutes is None) == (start is None):
        raise CalendarError("move requires exactly one of minutes or start")

    if settings.use_mock:
        logger.warning("MOCK move_event id=%s — NOT actually moved (DRY_RUN).", event_id)
        return {
            "_mock": True,
            "id": event_id,
            "calendar_id": calendar_id,
            "status": "not_moved_mock",
            "note": "MOCK MODE — event was NOT moved. Set DRY_RUN=false.",
        }

    tz = _tz()
    service = _service()
    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    old_start, all_day = _edge(event["start"], tz)
    old_end, _ = _edge(event["end"], tz)
    duration = old_end - old_start

    new_start = old_start + timedelta(minutes=minutes) if minutes is not None else _parse_datetime(start, tz)
    new_end = new_start + duration

    if all_day:
        body = {
            "start": {"date": new_start.date().isoformat()},
            "end": {"date": new_end.date().isoformat()},
        }
    else:
        body = {
            "start": {"dateTime": new_start.isoformat()},
            "end": {"dateTime": new_end.isoformat()},
        }

    updated = service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()
    logger.info("move_event id=%s -> %s", event_id, new_start.isoformat())
    return {
        "id": updated.get("id", event_id),
        "calendar_id": calendar_id,
        "start": updated.get("start", {}).get("dateTime") or updated.get("start", {}).get("date"),
        "end": updated.get("end", {}).get("dateTime") or updated.get("end", {}).get("date"),
        "html_link": updated.get("htmlLink"),
        "status": "moved",
    }


# --- action: delete -------------------------------------------------------


def delete_event(event_id: str, calendar_id: str = "primary") -> dict:
    """Delete an event outright. There is no undo, hence no ambiguity in mock mode:
    when in doubt, this simply does not call Google.
    """
    if settings.use_mock:
        logger.warning("MOCK delete_event id=%s — NOT actually deleted (DRY_RUN).", event_id)
        return {
            "_mock": True,
            "id": event_id,
            "calendar_id": calendar_id,
            "status": "not_deleted_mock",
            "note": "MOCK MODE — event was NOT deleted. Set DRY_RUN=false.",
        }

    _service().events().delete(calendarId=calendar_id, eventId=event_id).execute()
    logger.info("delete_event id=%s", event_id)
    return {"id": event_id, "calendar_id": calendar_id, "status": "deleted"}


# --- dispatch -------------------------------------------------------------


def create_event(
    summary: str,
    start: str,
    end: str,
    description: str | None = None,
    location: str | None = None,
    calendar_id: str = "primary",
) -> dict:
    """Create an event. Delegates to the original tool so there is one writer."""
    from .calendar import create_event as _create

    return _create(title=summary, start=start, end=end, description=description)


def manage(
    action: str,
    start: str | None = None,
    days: int = 1,
    calendar_id: str = "primary",
    minutes: int | None = None,
    work_start: str = "09:00",
    work_end: str = "18:00",
    include_weekends: bool = False,
    event_id: str | None = None,
    summary: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
) -> dict:
    """Dispatch across the five calendar-management actions: list, find_slot,
    update, move, delete. One tool with an `action` param — see the module
    docstring for why this isn't five separate MCP tools.
    """
    if settings.use_mock:
        return dict(_MOCK)

    if action == "list":
        return list_events(start=start, days=days, calendar_id=calendar_id)

    if action == "find_slot":
        slots = find_slots(
            start=start,
            days=days,
            minutes=30 if minutes is None else minutes,
            calendar_id=calendar_id,
            work_start=work_start,
            work_end=work_end,
            include_weekends=include_weekends,
        )
        return {
            "calendar_id": calendar_id,
            "minutes": 30 if minutes is None else minutes,
            "count": len(slots),
            "slots": slots,
        }

    if action == "update":
        if not event_id:
            raise CalendarError("update requires event_id")
        return update_event(
            event_id,
            summary=summary,
            start=start,
            end=end,
            description=description,
            location=location,
            calendar_id=calendar_id,
        )

    if action == "move":
        if not event_id:
            raise CalendarError("move requires event_id")
        return move_event(event_id, minutes=minutes, start=start, calendar_id=calendar_id)

    if action == "create":
        if not (summary and start and end):
            raise CalendarError(
                "create requires summary, start and end "
                "(e.g. start='2026-09-15T14:00', end='2026-09-15T15:00')"
            )
        return create_event(summary, start, end, description, location, calendar_id)
    if action == "delete":
        if not event_id:
            raise CalendarError("delete requires event_id")
        return delete_event(event_id, calendar_id=calendar_id)

    raise CalendarError(
        f"unknown action {action!r} — expected one of {', '.join(ACTIONS)}"
    )
