"""Google Calendar tools, acting as Ignas via his own OAuth token.

No service account is involved, despite what this line used to say. IBLU
authenticates as Ignas himself with an ordinary OAuth refresh token, one per
Workspace — no service-account key, no domain-wide delegation (HANDOFF §3.1).
That is a deliberate security choice: a delegated service account holds a
downloadable key that can impersonate ANY user in the Workspace, where a user
token can act only as the one person who approved it and can be revoked by
them.

Phase 1 exposes event creation. In dry-run / no-credentials mode this returns a
deterministic mock confirmation.
"""

from __future__ import annotations

import logging

from ..config import settings

logger = logging.getLogger("iblu_keeper.tools.calendar")


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
        logger.warning("MOCK create_event '%s' — NOT actually created (DRY_RUN).", title)
        return {
            "_mock": True,
            "id": "MOCK_EVENT_1",
            "html_link": "https://calendar.google.com/event?eid=MOCK",
            "title": title,
            "start": start,
            "end": end,
            "description": description or "",
            "status": "not_created_mock",
            "note": "MOCK MODE — event was NOT created. Set DRY_RUN=false.",
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
    event_id = event.get("id")
    if not event_id:
        raise RuntimeError(
            f"Calendar insert returned no event id (response={event!r}); "
            "treating as failure rather than reporting a false success."
        )
    logger.info("create_event '%s' created (id=%s)", title, event_id)
    return {
        "id": event_id,
        "html_link": event.get("htmlLink"),
        "title": event.get("summary", title),
        "start": event.get("start", {}).get("dateTime", start),
        "end": event.get("end", {}).get("dateTime", end),
        "status": "created",
    }


def add_label(event_id: str, label_id: str, calendar_id: str = "primary") -> dict:
    """Attach a custom event label to an existing Calendar event.

    Requires the label to be defined at the calendar level first (via
    Calendar UI or Calendars API — the latter would need a wider OAuth
    scope than we currently request). Passing ``label_id=""`` removes the
    current label from the event.

    Uses ``eventLabelVersion=1``; with this flag Calendar processes the
    ``eventLabelId`` field on the event body and ignores the legacy
    ``colorId``.
    """
    if settings.use_mock:
        logger.warning("MOCK add_label event=%s label=%s (DRY_RUN)", event_id, label_id)
        return {
            "_mock": True,
            "id": event_id,
            "event_label_id": label_id,
            "status": "not_labeled_mock",
            "note": "MOCK MODE — event was NOT labeled. Set DRY_RUN=false.",
        }

    service = _service()
    updated = service.events().patch(
        calendarId=calendar_id,
        eventId=event_id,
        body={"eventLabelId": label_id},
        eventLabelVersion=1,
    ).execute()
    logger.info(
        "add_label event=%s label=%r  → %r",
        event_id, label_id, updated.get("eventLabelId"),
    )
    return {
        "id": updated.get("id", event_id),
        "calendar_id": calendar_id,
        "event_label_id": updated.get("eventLabelId", ""),
        "title": updated.get("summary", ""),
        "html_link": updated.get("htmlLink"),
        "status": "labeled" if label_id else "label_cleared",
    }
