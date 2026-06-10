"""Google Calendar tools via the delegated service account.

Phase 1 exposes event creation. In dry-run / no-credentials mode this returns a
deterministic mock confirmation.
"""

from __future__ import annotations

from ..config import settings


def _service():
    from ..google_auth import build_service

    return build_service("calendar", "v3")


def create_event(
    title: str,
    start: str,
    end: str,
    description: str | None = None,
) -> dict:
    """Create a calendar event.

    start/end: RFC 3339 timestamps, e.g. "2026-06-11T14:00:00+03:00".
    Returns {id, html_link, title, start, end}.
    """
    if settings.use_mock:
        return {
            "id": "MOCK_EVENT_1",
            "html_link": "https://calendar.google.com/event?eid=MOCK",
            "title": title,
            "start": start,
            "end": end,
            "description": description or "",
            "mock": True,
        }

    service = _service()
    body = {
        "summary": title,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if description:
        body["description"] = description

    event = service.events().insert(calendarId="primary", body=body).execute()
    return {
        "id": event.get("id"),
        "html_link": event.get("htmlLink"),
        "title": event.get("summary", title),
        "start": event.get("start", {}).get("dateTime", start),
        "end": event.get("end", {}).get("dateTime", end),
    }
