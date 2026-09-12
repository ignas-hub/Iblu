"""Tying the ping pieces together for one tick.

`schedule` decides whether to ask, `compose` decides what, `deliver` puts it on
the phone, `answers` records what comes back. This module is the only place
that knows about all four, and the only place that writes a `pings` row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from .. import db
from ..config import settings
from . import schedule
from .compose import compose
from .deliver import DeliveryError, send

logger = logging.getLogger("iblu_keeper.pings.runner")

WINDOWS = {"midday": "ping_midday", "evening": "ping_evening"}


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.iblu_timezone)


def _todays_events(tz: ZoneInfo, day) -> list[schedule.Event]:
    """Today's primary calendar, reduced to what scheduling needs.

    All-day, cancelled and declined events are ignored: none of them means
    "Ignas is in a room right now".
    """
    from ..google_auth import build_service

    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    svc = build_service("calendar", "v3")
    raw = (
        svc.events()
        .list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=(start + timedelta(days=1)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=100,
        )
        .execute()
        .get("items", [])
        or []
    )

    events: list[schedule.Event] = []
    for item in raw:
        if item.get("status") == "cancelled":
            continue
        start_raw = (item.get("start") or {}).get("dateTime")
        end_raw = (item.get("end") or {}).get("dateTime")
        if not start_raw or not end_raw:
            continue  # all-day: not a reason to stay quiet
        if any(a.get("self") and a.get("responseStatus") == "declined"
               for a in item.get("attendees", []) or []):
            continue
        events.append(
            schedule.Event(
                start=datetime.fromisoformat(start_raw).astimezone(tz),
                end=datetime.fromisoformat(end_raw).astimezone(tz),
                summary=item.get("summary"),
                attendee_count=len(item.get("attendees") or []),
            )
        )
    return events


def _already_handled(conn: psycopg.Connection, kind: str, day) -> bool:
    row = conn.execute(
        "SELECT status FROM pings WHERE kind = %s AND local_date = %s",
        (kind, day),
    ).fetchone()
    return row is not None and row["status"] != "pending"


def _midday_sent_at(conn: psycopg.Connection, day) -> datetime | None:
    row = conn.execute(
        "SELECT sent_at FROM pings WHERE kind = 'midday' AND local_date = %s",
        (day,),
    ).fetchone()
    return row["sent_at"] if row else None


def _window_signals(conn: psycopg.Connection, frm: datetime, to: datetime) -> list[dict]:
    return conn.execute(
        "SELECT id, source, occurred_at, counterpart, container, subject, "
        "       snippet, ask_snippet, initiator, venture, work_type "
        "FROM signals WHERE occurred_at >= %s AND occurred_at <= %s "
        "ORDER BY occurred_at",
        (frm, to),
    ).fetchall()


def _taxonomy(conn: psycopg.Connection) -> tuple[list[dict], list[dict]]:
    ventures = [dict(r) for r in conn.execute(
        "SELECT code, label FROM ventures WHERE active ORDER BY sort_order").fetchall()]
    work_types = [dict(r) for r in conn.execute(
        "SELECT code, label FROM work_types ORDER BY sort_order").fetchall()]
    return ventures, work_types


def run_one(kind: str, *, dry: bool = False, force: bool = False) -> str:
    """Consider (and maybe send) one ping. Returns a short status for the log."""
    tz = _tz()
    now = datetime.now(tz)
    day = now.date()

    if not force and not schedule.is_ping_day(day, settings.ping_day_set):
        return f"{kind}:not-a-ping-day"

    window_raw = getattr(settings, WINDOWS[kind], None) if kind in WINDOWS else None
    if window_raw:
        window_start, window_end = schedule.window_bounds(window_raw, day, tz)
    else:  # 'test' has no window of its own
        window_start, window_end = now, now

    with db.get_conn() as conn:
        handled = False if force else _already_handled(conn, kind, day)

        if not force:
            try:
                events = _todays_events(tz, day)
            except Exception as exc:  # noqa: BLE001
                logger.warning("pings: calendar unavailable (%s); assuming free", exc)
                events = []
            decision = schedule.decide(now, window_start, window_end, events, handled)
            if not decision.send:
                logger.info("pings: %s not sent — %s", kind, decision.reason)
                return f"{kind}:{decision.reason.split(':')[0].replace(' ', '-')}"
        else:
            events = []
            decision = schedule.Decision(True, "forced", forced=True)

        covers_from, covers_to = schedule.coverage(
            "midday" if kind == "midday" else "evening",
            now, day, tz,
            midday_sent_at=_midday_sent_at(conn, day) if kind == "evening" else None,
        )
        if kind == "test":
            covers_from = now - timedelta(hours=24)

        signals = _window_signals(conn, covers_from, covers_to)
        ventures, work_types = _taxonomy(conn)
        questions, composer = compose(
            signals, events, covers_from, covers_to, ventures, work_types
        )

        if dry:
            import json
            print(json.dumps(
                {"kind": kind, "composer": composer, "signals": len(signals),
                 "covers": [covers_from.isoformat(), covers_to.isoformat()],
                 "questions": questions.model_dump()},
                indent=2, default=str,
            ))
            return f"{kind}:dry:{composer}"

        # The questions snapshot goes in BEFORE sending: the tap links embed the
        # ping id, so the row has to exist for a tap to be resolvable.
        ping_id = conn.execute(
            """
            INSERT INTO pings
                (kind, local_date, window_start, window_end, covers_from,
                 covers_to, status, questions, composer, meta)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
            ON CONFLICT (kind, local_date) WHERE kind IN ('midday','evening')
            DO UPDATE SET questions = EXCLUDED.questions,
                          composer = EXCLUDED.composer,
                          covers_to = EXCLUDED.covers_to
            RETURNING id
            """,
            (
                kind, day, window_start, window_end, covers_from, covers_to,
                Jsonb(questions.model_dump()["questions"]), composer,
                Jsonb({"forced": decision.forced, "reason": decision.reason}),
            ),
        ).fetchone()["id"]

        from ..tools.chat import get_backend

        try:
            self_id = get_backend()._ensure_self_id()
        except Exception:  # noqa: BLE001 - the mention is nice-to-have
            self_id = ""

        try:
            refs = send(ping_id, kind, questions, self_id)
        except DeliveryError as exc:
            logger.error("pings: %s delivery failed: %s", kind, exc)
            conn.execute(
                "UPDATE pings SET status = 'failed', "
                "meta = meta || %s WHERE id = %s",
                (Jsonb({"error": str(exc)[:500]}), ping_id),
            )
            return f"{kind}:failed"

        conn.execute(
            "UPDATE pings SET status = 'sent', sent_at = now(), "
            "chat_message_ref = %s, chat_thread_ref = %s WHERE id = %s",
            (refs["message_ref"], refs["thread_ref"], ping_id),
        )

    logger.info("pings: %s sent (ping %s, composer=%s)", kind, ping_id, composer)
    return f"{kind}:sent"


def run_pings(*, dry: bool = False, force_kind: str | None = None) -> str:
    """Decide and send for this tick. Returns the log fragment."""
    if force_kind:
        return run_one(force_kind, dry=dry, force=True)

    if not settings.ping_enabled:
        return "off"
    if not settings.can_ping:
        logger.warning(
            "pings: PING_ENABLED=true but configuration is incomplete "
            "(webhook / signing secret / public base url) — not sending"
        )
        return "misconfigured"

    tz = _tz()
    if not schedule.is_ping_day(datetime.now(tz).date(), settings.ping_day_set):
        return "not-a-ping-day"

    notes = [run_one(kind, dry=dry) for kind in ("midday", "evening")]
    # Collapse the common "nothing to do yet" cases into one quiet word so the
    # journal line stays readable across ~78 ticks a day.
    interesting = [n for n in notes if not n.endswith(("before-the-window", "already"))]
    return ",".join(interesting) or "waiting"
