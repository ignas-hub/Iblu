"""Putting the reconstructed day on the Secretary calendar.

One calendar on blanklabel.team holds every venture (HANDOFF.md §15), because
the point is to see the whole week in one place — BLT next to Deadlift next to
Choco — and three calendars would hide exactly the comparison that matters.

The intent calendar is never touched. IBLU only ever writes to
`SECRETARY_CALENDAR_ID`, and only ever events it created itself: every mirror
event carries `extendedProperties.private.iblu = "block"` and the block id, and
the database keeps the event id, so a re-mirror can find its own work without
guessing from titles.
"""

from __future__ import annotations

import logging
from datetime import date

from ..config import settings

logger = logging.getLogger("iblu_keeper.analyst.mirror")

# Google's calendar palette. Attention is the thing worth seeing at a glance in
# a month view, so it — not the venture — picks the colour.
COLOR_BY_ATTENTION = {
    "present": "10",    # basil — green
    "displaced": "11",  # tomato — red
    "ambiguous": "8",   # graphite — grey
}

MARK = {"present": "", "displaced": "⟂ ", "ambiguous": "? "}


def _service():
    from ..tools import calendar_manage

    return calendar_manage._service()


def _title(block: dict) -> str:
    """What the block is called in a month view, in a glance's worth of words.

    An ambiguous block is named after the intent it failed to account for —
    "Womanizer alignment" says what was supposed to happen, where
    "unattributed" says nothing at all.
    """
    if block["attention"] == "ambiguous":
        return MARK["ambiguous"] + (block.get("intent_title") or "unaccounted")

    parts = [p for p in (block.get("venture"), block.get("work_type")) if p]
    label = " · ".join(parts) if parts else "unattributed"
    if block.get("project"):
        label += f" ({block['project']})"
    if block["attention"] == "displaced" and block.get("intent_title"):
        label += f" (not {block['intent_title']})"
    return MARK["displaced" if block["attention"] == "displaced" else "present"] + label


def _body(block: dict) -> dict:
    description = block.get("reasoning") or ""
    if block["confidence"] == "inferred":
        description += "\n\nInferred, not confirmed."
    if block["attention"] == "ambiguous":
        description += "\nNothing was recorded here — it is unknown, not idle."
    return {
        "summary": _title(block),
        "description": description.strip(),
        "start": {"dateTime": block["starts_at"].isoformat()},
        "end": {"dateTime": block["ends_at"].isoformat()},
        "colorId": COLOR_BY_ATTENTION[block["attention"]],
        "transparency": "transparent",  # a record of the past never blocks time
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {
            "private": {"iblu": "block", "block_id": str(block["id"])}
        },
    }


def _clear(conn, service, calendar_id: str, on: date) -> int:
    """Remove the mirror events of a previous run for this day.

    Only events IBLU recorded in `blocks.calendar_event_id` are touched, so a
    human's own entry on this calendar — or a stale row whose event is already
    gone (404) — can never turn into a deletion of something else.
    """
    rows = conn.execute(
        """
        SELECT id, calendar_event_id FROM blocks
         WHERE local_date = %s AND calendar_event_id IS NOT NULL
        """,
        (on,),
    ).fetchall()

    removed = 0
    for row in rows:
        try:
            service.events().delete(
                calendarId=calendar_id, eventId=row["calendar_event_id"]
            ).execute()
            removed += 1
        except Exception as exc:
            # Already deleted by hand is the normal case, not an error.
            logger.info("mirror: could not delete %s (%s)", row["calendar_event_id"], exc)
        conn.execute(
            "UPDATE blocks SET calendar_event_id = NULL WHERE id = %s", (row["id"],)
        )
    return removed


def mirror_day(conn, on: date, *, dry: bool = False) -> dict:
    """Make the Secretary calendar show exactly today's live blocks."""
    calendar_id = settings.secretary_calendar_id
    if not calendar_id:
        return {"skipped": "SECRETARY_CALENDAR_ID is not set"}
    if settings.use_mock:
        raise RuntimeError("refusing to mirror in mock mode")

    from .blocks import live_blocks

    blocks = live_blocks(conn, on)
    if dry:
        return {"date": on.isoformat(), "would_write": len(blocks), "dry": True}

    service = _service()
    removed = _clear(conn, service, calendar_id, on)

    written = 0
    for block in blocks:
        created = (
            service.events()
            .insert(calendarId=calendar_id, body=_body(block))
            .execute()
        )
        conn.execute(
            "UPDATE blocks SET calendar_event_id = %s WHERE id = %s",
            (created.get("id"), block["id"]),
        )
        written += 1

    return {
        "date": on.isoformat(),
        "calendar_id": calendar_id,
        "removed": removed,
        "written": written,
    }
