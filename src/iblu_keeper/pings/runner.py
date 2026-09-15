"""Tying the ping pieces together for one tick.

`schedule` decides whether to ask, `compose` decides what, `deliver` puts it on
the phone, `answers` records what comes back. This module is the only place
that knows about all four, and the only place that writes a `pings` row.
"""

from __future__ import annotations

import logging
from collections import Counter
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


# 'failed' is retryable: a webhook outage or an undeployed tap route must not
# cost the whole day's ping (plan §7.4 — retry next tick, same row).
RETRYABLE_STATUSES = ("pending", "failed")


def _already_handled(conn: psycopg.Connection, kind: str, day) -> bool:
    row = conn.execute(
        "SELECT status FROM pings WHERE kind = %s AND local_date = %s",
        (kind, day),
    ).fetchone()
    return row is not None and row["status"] not in RETRYABLE_STATUSES


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


def _day_blocks(conn: psycopg.Connection, day) -> list[dict]:
    """The day's current reconstruction, for the split/gap cards (plan §3.3).

    The analyst runs on its own timer (`iblu-analyst.timer`), not on every
    ping tick, so this may legitimately be empty — before it has run for
    today, or if it never runs (mock/dev). Either way split/gap simply do
    not fire rather than guessing; nothing here invents a block.
    """
    try:
        from ..analyst.blocks import live_blocks

        return live_blocks(conn, day)
    except Exception as exc:  # noqa: BLE001 - a stale block set beats no ping
        logger.warning("pings: blocks unavailable for %s (%s)", day, exc)
        return []


# What a venture is CALLED to Ignas, not what it is keyed as. The card showed
# "Experienced: time with personal" — "Experienced" is the internal kind name
# and "personal" is a primary key, and neither should ever have reached his
# phone. `personal` is his own tooling, so "time with personal" was not even
# wrong in an interesting way; it was meaningless.
VENTURE_WORDS = {
    "blt": "Blank Label",
    "choco": "Choco",
    "deadlift": "Deadlift",
    "gostellar": "GoStellar",
    "jakusi": "the house",
    "family": "the family",
    "personal": "your own tools",
}

# Non-work time, named by where it went. Never by a venture CODE: "Time on your
# own projects" told Ignas nothing he could confirm or deny.
LIVED_WORDS = {
    "family": "Time with the family",
    "jakusi": "Time on the house",
}


def _lived_words(block: dict) -> str:
    """What to call a stretch of non-work time on a button."""
    named = LIVED_WORDS.get(block.get("venture"))
    if named:
        return named
    project = _project_name(block.get("project"))
    return f"Time on {project}" if project else "Time away from work"

# A Chat button is about this wide. The gains validator refuses an option the
# SCHEMA truncated, because a sentence with its ending removed cannot be
# checked — so anything built here is shortened deliberately and carries its
# full text alongside for validation.
LABEL_FIT = 38


def _fit(text: str) -> str:
    """Shorten to a button, at a word boundary, without an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= LABEL_FIT:
        return text
    cut = text[:LABEL_FIT]
    space = cut.rfind(" ")
    return (cut[:space] if space > LABEL_FIT // 2 else cut).rstrip(" ,.;:—-")


def _first_clause(content: str) -> str:
    """The decision itself, not the paragraph explaining it."""
    text = " ".join((content or "").split())
    for stop in (" — ", ". ", "; ", ", "):
        head = text.split(stop)[0]
        if 12 <= len(head) <= LABEL_FIT:
            return head
    return text


def _project_name(code: str | None) -> str | None:
    """A registered project's human name, falling back to its code."""
    if not code:
        return None
    return {"email-writer": "Email Writer", "bd-global": "the BD hire",
            "iblu": "Iblu", "machina": "Machina", "radovi": "Radovi",
            "accounting-app": "the accounting bot"}.get(code, code)


def _duration(start, end) -> str:
    if not start or not end:
        return ""
    minutes = int((end - start).total_seconds() // 60)
    if minutes >= 90:
        return f" — {round(minutes / 60)}h"
    return f" — {minutes} min" if minutes else ""


def _local_day_bounds(day):
    """The UTC instants bracketing a Zagreb calendar day.

    `blocks.local_date` is already a local date, so block queries are fine.
    `context_entries.occurred_at` is a TIMESTAMPTZ, and `::date` truncates in
    the session timezone — UTC here — which put anything logged between
    midnight and 02:00 local on the previous day.
    """
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.iblu_timezone)
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _gain_evidence(conn: psycopg.Connection, day) -> dict[str, list[dict]]:
    """Evidence for the gains card (plan §4.1) — dated, already-happened only.

    One option per kind, at most: **learned** (a decision/correction logged
    today), **progressed** (a `fact` block today on a project belonging to a
    venture with a current yearly priority — plan §1.4's `current_priorities`,
    read lazily since `store.governance` is being built in parallel),
    **experienced** (a `present` block today on `family` or `personal`).
    """
    evidence: dict[str, list[dict]] = {"learned": [], "progressed": [], "experienced": []}

    learned = conn.execute(
        "SELECT id, content FROM context_entries "
        "WHERE type IN ('decision', 'correction') "
        # `occurred_at::date` truncates in the SESSION timezone, which is UTC,
        # while `day` is a Zagreb date. Anything logged between midnight and
        # 02:00 local falls on the previous UTC date and vanished from that
        # evening's gains. Compare against an explicit local-day range instead.
        "AND occurred_at >= %s AND occurred_at < %s "
        "AND superseded_by IS NULL "
        # A priority, a baseline and the gain rules are the measuring stick,
        # not progress against it. Without this the evening card offered
        # "Learned: YEARLY TOP PRIORITY — Jakusi" the day he wrote it down.
        "AND (source_ref IS NULL OR source_ref !~ '^(priority|baseline|gain):') "
        "AND NOT ('test' = ANY(tags)) "
        "ORDER BY occurred_at DESC LIMIT 1",
        _local_day_bounds(day),
    ).fetchone()
    if learned:
        text = _first_clause(learned["content"])
        evidence["learned"].append({
            "label": _fit(text),
            # The FULL sentence, so the gains validator judges what he actually
            # decided rather than the 40 characters that fit on a button.
            "source_text": learned["content"],
            "evidence_ids": [str(learned["id"])],
        })

    priority_ventures: list[str] = []
    try:
        from ..store import governance

        priority_ventures = [p["venture"] for p in governance.current_priorities(conn) if p.get("venture")]
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - "progressed" still works, just unfiltered
        logger.warning("pings: current_priorities unavailable (%s)", exc)

    progressed_sql = (
        "SELECT id, venture, project, starts_at, ends_at FROM blocks "
        "WHERE local_date = %s AND confidence = 'fact' AND superseded_by IS NULL "
        "AND venture IS NOT NULL"
    )
    progressed_args: tuple = (day,)
    if priority_ventures:
        progressed_sql += " AND venture = ANY(%s)"
        progressed_args = (day, priority_ventures)
    progressed = conn.execute(
        progressed_sql + " ORDER BY starts_at DESC LIMIT 1", progressed_args
    ).fetchone()
    if progressed:
        what = _project_name(progressed.get("project")) or VENTURE_WORDS.get(
            progressed["venture"], progressed["venture"]
        )
        span = _duration(progressed.get("starts_at"), progressed.get("ends_at"))
        evidence["progressed"].append({
            "label": _fit(f"Moved {what} forward{span}"),
            "source_text": f"Moved {what} forward{span}",
            "evidence_ids": [str(progressed["id"])],
        })

    # Plan §4.1 says an experienced gain is a "present family or LIFE block".
    # The first version read that as `venture IN ('family','personal')` — but
    # in IBLU's taxonomy `personal` is the venture "Own tooling & infra (IBLU,
    # accounting bot, servers)" and `life` is a WORK TYPE, "Non-work: family,
    # home, health". They are opposites. So 30 minutes of building IBLU — 25
    # messages in a chat called "Claude questions" — was offered to Ignas as
    # something he had experienced, and he quite reasonably said he did not
    # understand what it meant.
    # A stage move is the other kind of progress plan §4.1 names, and unlike a
    # thread "looking closed" it is a fact: he confirmed it by tapping, and
    # `project_stage_history` recorded who changed it and when.
    if not evidence["progressed"]:
        try:
            moved = conn.execute(
                "SELECT h.id, h.project, h.to_stage, s.label "
                "  FROM project_stage_history h "
                "  LEFT JOIN stages s ON s.code = h.to_stage "
                " WHERE h.changed_at >= %s AND h.changed_at < %s "
                " ORDER BY h.changed_at DESC LIMIT 1",
                _local_day_bounds(day),
            ).fetchone()
        except Exception as exc:  # noqa: BLE001 — the registry may not exist yet
            logger.debug("pings: stage history unavailable (%s)", exc)
            moved = None
        if moved:
            name = _project_name(moved["project"]) or moved["project"]
            text = f"{name} reached '{moved['label'] or moved['to_stage']}'"
            evidence["progressed"].append({
                "label": _fit(text), "source_text": text,
                "evidence_ids": [str(moved["id"])],
            })

    experienced = conn.execute(
        "SELECT id, venture, work_type, project, starts_at, ends_at FROM blocks "
        "WHERE local_date = %s AND attention = 'present' AND superseded_by IS NULL "
        "AND (venture = 'family' OR work_type = 'life') "
        "ORDER BY starts_at DESC LIMIT 1",
        (day,),
    ).fetchone()
    if experienced:
        text = _lived_words(experienced)
        span = _duration(experienced.get("starts_at"), experienced.get("ends_at"))
        text = f"{text}{span}"
        evidence["experienced"].append({
            "label": _fit(text),
            "source_text": text,
            "evidence_ids": [str(experienced["id"])],
        })

    return evidence


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
                return f"{kind}:{decision.code}"
        else:
            events = []
            decision = schedule.Decision(True, "forced", "forced", forced=True)

        covers_from, covers_to = schedule.coverage(
            "midday" if kind == "midday" else "evening",
            now, day, tz,
            midday_sent_at=_midday_sent_at(conn, day) if kind == "evening" else None,
        )
        if kind == "test":
            covers_from = now - timedelta(hours=24)

        signals = _window_signals(conn, covers_from, covers_to)
        ventures, work_types = _taxonomy(conn)
        day_blocks = _day_blocks(conn, day)
        gain_evidence = _gain_evidence(conn, day) if kind in ("evening", "test") else None
        top_venture = next(iter(Counter(
            b["venture"] for b in day_blocks if b.get("venture")
        ).most_common(1)), (None,))[0]

        questions, composer = compose(
            signals, events, covers_from, covers_to, ventures, work_types,
            kind=kind, blocks=day_blocks, gain_evidence=gain_evidence,
            top_venture=top_venture,
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
            -- The question snapshot is only replaced if the previous attempt
            -- never actually reached Chat. A webhook POST can time out client
            -- side AFTER the card was delivered; the ping is then marked
            -- 'failed' and retried, and overwriting `questions` in place would
            -- silently repoint the tap tokens on the card already on his phone.
            -- Option keys are drawn from a tiny vocabulary (A-E) and qids
            -- repeat daily, so a stale tap would usually resolve to SOME
            -- option in the new snapshot — the wrong one, recorded as if he
            -- had chosen it. Keeping the delivered snapshot means a stale tap
            -- still means exactly what he saw.
            DO UPDATE SET questions = CASE
                              WHEN pings.chat_message_ref IS NULL
                              THEN EXCLUDED.questions
                              ELSE pings.questions
                          END,
                          composer  = CASE
                              WHEN pings.chat_message_ref IS NULL
                              THEN EXCLUDED.composer
                              ELSE pings.composer
                          END,
                          covers_to = EXCLUDED.covers_to,
                          status    = 'pending'
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
    interesting = [n for n in notes if not n.endswith((":early", ":done"))]
    return ",".join(interesting) or "waiting"
