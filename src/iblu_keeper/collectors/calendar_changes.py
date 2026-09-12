"""Collector: calendar changes (plan §8.3).

The calendar records *intent*; changes to it record intent being renegotiated —
a meeting moved, a block cancelled, something dropped in at short notice. Each
change is diffed against a fingerprint baseline in `calendar_seen`.

The first run seeds that baseline silently and emits no signals: without this,
every event already in the calendar would look like it was created the moment
IBLU first looked.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from ..config import settings
from . import get_cursor, insert_signal, set_state
from .venture_hints import infer

logger = logging.getLogger("iblu_keeper.collectors.calendar_changes")

NAME = "calendar_changes"
CALENDAR_ID = "primary"
LOOK_BACK = timedelta(days=1)
LOOK_AHEAD = timedelta(days=7)
MAX_RESULTS = 250
FRESHLY_CREATED = timedelta(hours=24)


def _service():
    from ..google_auth import build_service

    return build_service("calendar", "v3")


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _edge(node: dict) -> str | None:
    """An event's start/end, whether timed (`dateTime`) or all-day (`date`)."""
    return (node or {}).get("dateTime") or (node or {}).get("date")


def _self_response(event: dict) -> str | None:
    for attendee in event.get("attendees", []) or []:
        if attendee.get("self"):
            return attendee.get("responseStatus")
    return None


def _payload(event: dict) -> dict:
    """The compact subset we fingerprint and diff on."""
    return {
        "start": _edge(event.get("start")),
        "end": _edge(event.get("end")),
        "summary": event.get("summary"),
        "status": event.get("status"),
        "self_response": _self_response(event),
        "attendees": sorted(
            a.get("email", "") for a in (event.get("attendees") or []) if a.get("email")
        ),
    }


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _human_time(raw: str | None) -> str:
    dt = _parse(raw)
    if dt is None:
        return "?"
    if len(str(raw)) == 10:  # a bare date: all-day
        return dt.strftime("%a %d %b (all day)")
    return dt.strftime("%a %H:%M")


def _describe(before: dict, after: dict) -> str:
    """A human diff: 'Tue 14:00–15:00 → Wed 10:00–11:00'."""
    parts: list[str] = []
    if before.get("start") != after.get("start") or before.get("end") != after.get("end"):
        parts.append(
            f"{_human_time(before.get('start'))}–{_human_time(before.get('end'))}"
            f" → {_human_time(after.get('start'))}–{_human_time(after.get('end'))}"
        )
    if before.get("summary") != after.get("summary"):
        parts.append(f"renamed {before.get('summary')!r} → {after.get('summary')!r}")
    if before.get("self_response") != after.get("self_response"):
        parts.append(
            f"my response {before.get('self_response')} → {after.get('self_response')}"
        )
    if before.get("attendees") != after.get("attendees"):
        delta = len(after.get("attendees") or []) - len(before.get("attendees") or [])
        parts.append(f"attendees {delta:+d}")
    return "; ".join(parts) or "changed"


def _classify(before: dict, after: dict) -> str:
    if after.get("status") == "cancelled" or after.get("self_response") == "declined":
        return "cancelled"
    if before.get("start") != after.get("start") or before.get("end") != after.get("end"):
        return "moved"
    return "changed"


def collect(conn: psycopg.Connection, *, dry: bool = False) -> int:
    """Diff the calendar against the stored baseline and record what moved."""
    me = settings.google_user_email
    seeded = get_cursor(conn, NAME) == "seeded"
    now = datetime.now(timezone.utc)

    events = (
        _service()
        .events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=(now - LOOK_BACK).isoformat(),
            timeMax=(now + LOOK_AHEAD).isoformat(),
            singleEvents=True,
            showDeleted=True,
            maxResults=MAX_RESULTS,
        )
        .execute()
        .get("items", [])
        or []
    )

    baseline = {
        row["event_id"]: row
        for row in conn.execute(
            "SELECT event_id, fingerprint, payload FROM calendar_seen "
            "WHERE calendar_id = %s",
            (CALENDAR_ID,),
        ).fetchall()
    }

    inserted = 0
    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue

        payload = _payload(event)
        fingerprint = _fingerprint(payload)
        known = baseline.get(event_id)
        updated = _parse(event.get("updated")) or now

        if known is None:
            created = _parse(event.get("created"))
            is_new = created is not None and (now - created) <= FRESHLY_CREATED
            if seeded and is_new and not dry:
                venture, project = infer(
                    account=me,
                    counterpart=" ".join(payload["attendees"]),
                    subject=payload["summary"],
                )
                if insert_signal(
                    conn,
                    {
                        "source": "calendar",
                        "kind": "created",
                        "account": me,
                        "occurred_at": updated,
                        "actor": "me",
                        "counterpart": ", ".join(payload["attendees"][:3]) or None,
                        "container": CALENDAR_ID,
                        "subject": payload["summary"],
                        "snippet": f"{_human_time(payload['start'])}–{_human_time(payload['end'])}",
                        "venture": venture,
                        "project": project,
                        "source_ref": f"{event_id}:{fingerprint}",
                        "meta": {"after": payload, "attendee_count": len(payload["attendees"])},
                    },
                ):
                    inserted += 1
        elif known["fingerprint"] != fingerprint:
            before = known["payload"]
            if seeded and not dry:
                venture, project = infer(
                    account=me,
                    counterpart=" ".join(payload["attendees"]),
                    subject=payload["summary"],
                )
                if insert_signal(
                    conn,
                    {
                        "source": "calendar",
                        "kind": _classify(before, payload),
                        "account": me,
                        "occurred_at": updated,
                        "actor": "me",
                        "counterpart": ", ".join(payload["attendees"][:3]) or None,
                        "container": CALENDAR_ID,
                        "subject": payload["summary"],
                        "snippet": _describe(before, payload),
                        "venture": venture,
                        "project": project,
                        "source_ref": f"{event_id}:{fingerprint}",
                        "meta": {"before": before, "after": payload},
                    },
                ):
                    inserted += 1

        if not dry:
            conn.execute(
                """
                INSERT INTO calendar_seen
                    (calendar_id, event_id, updated, fingerprint, payload, last_seen_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (calendar_id, event_id) DO UPDATE SET
                    updated = EXCLUDED.updated,
                    fingerprint = EXCLUDED.fingerprint,
                    payload = EXCLUDED.payload,
                    last_seen_at = now()
                """,
                (CALENDAR_ID, event_id, updated, fingerprint, Jsonb(payload)),
            )

    if not dry:
        set_state(conn, NAME, watermark=now, cursor="seeded", error=None)

    if not seeded:
        logger.info(
            "%s: seeded baseline with %d event(s), no signals emitted (first run)",
            NAME,
            len(events),
        )
        return 0

    logger.info("%s: %d change(s) across %d event(s)", NAME, inserted, len(events))
    return inserted
