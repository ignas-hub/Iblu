"""Memory / context tools — Phase 2 ("recording v1"), backed by Postgres.

Two stores, deliberately separate (plan D1):

  * `signals`         — raw observations written by the collectors. Never read
                        directly into a brief; they are evidence, not memory.
  * `context_entries` — durable facts, decisions, preferences and quiz answers
                        (`type='work_log'`). This is the memory layer.

Corrections supersede, they never delete (plan D9): writing a correction sets
`superseded_by` on the row it replaces, and searches exclude superseded rows.

Mock mode (DRY_RUN=true) returns `{"status": "mock"}` from every function and
never opens a database connection — a mock row must never reach the database
(see DEBUG_FINDINGS.md).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from psycopg.types.json import Jsonb

from .. import db
from ..config import settings
from ..store import governance

logger = logging.getLogger("iblu_keeper.context")

ENTRY_TYPES = (
    "work_log",
    "fact",
    "preference",
    "decision",
    "correction",
    "conversation_note",
)
ENTRY_SOURCES = ("claude", "ping", "chat_reply", "analyst")

_MOCK = {"status": "mock"}
_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([hdw])\s*$", re.IGNORECASE)


class ValidationError(ValueError):
    """Bad tool input. The message always lists what *is* valid."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _parse_window(window: str) -> timedelta:
    """'1d' / '12h' / '2w' -> timedelta. Raises ValidationError otherwise."""
    m = _WINDOW_RE.match(window or "")
    if not m:
        raise ValidationError(
            f"invalid window {window!r} — expected forms like '12h', '1d', '2w'"
        )
    n, unit = int(m.group(1)), m.group(2).lower()
    return {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


def _parse_ts(value: Any, field: str) -> datetime | None:
    """Accept a datetime or an ISO-8601 string; always return tz-aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValidationError(
                f"invalid {field}={value!r} — expected ISO-8601, e.g. "
                "'2026-09-14T09:30:00Z'"
            ) from exc
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _valid_codes(conn, table: str) -> list[str]:
    rows = conn.execute(f"SELECT code FROM {table} ORDER BY sort_order").fetchall()
    return [r["code"] for r in rows]


def _check_code(conn, table: str, field: str, value: str | None) -> None:
    """Validate venture / work_type against its table, naming valid codes."""
    if value is None:
        return
    codes = _valid_codes(conn, table)
    if value not in codes:
        raise ValidationError(
            f"unknown {field}={value!r} — valid codes: {', '.join(codes)}"
        )


def _row_out(row: dict) -> dict:
    """Normalise a context_entries row for a tool response."""
    out = dict(row)
    for key in ("id", "superseded_by"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("created_at", "occurred_at", "expires_at", "last_referenced_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    return out


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------


def log_entry(
    type: str,
    content: str,
    importance: int = 3,
    tags: Sequence[str] | None = None,
    venture: str | None = None,
    work_type: str | None = None,
    project: str | None = None,
    source: str = "claude",
    source_ref: str | None = None,
    occurred_at: Any = None,
    meta: dict | None = None,
    supersedes: str | None = None,
) -> dict:
    """Write one durable entry. Returns `{id, created_at}`.

    `supersedes` — id of an entry this one replaces; that row's `superseded_by`
    is set to the new id (corrections never delete, plan D9).
    """
    if settings.use_mock:
        logger.info("context.log_entry: mock mode, not writing")
        return dict(_MOCK)

    if type not in ENTRY_TYPES:
        raise ValidationError(
            f"unknown type={type!r} — valid types: {', '.join(ENTRY_TYPES)}"
        )
    if source not in ENTRY_SOURCES:
        raise ValidationError(
            f"unknown source={source!r} — valid sources: {', '.join(ENTRY_SOURCES)}"
        )
    if not content or not content.strip():
        raise ValidationError("content must not be empty")
    if not 1 <= int(importance) <= 5:
        raise ValidationError(f"importance={importance} out of range — expected 1..5")

    occurred = _parse_ts(occurred_at, "occurred_at")
    tag_list = [str(t) for t in (tags or [])]

    with db.get_conn() as conn:
        _check_code(conn, "ventures", "venture", venture)
        _check_code(conn, "work_types", "work_type", work_type)

        row = conn.execute(
            """
            INSERT INTO context_entries
                (type, content, importance, tags, venture, work_type, project,
                 source, source_ref, occurred_at, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (
                type,
                content.strip(),
                int(importance),
                tag_list,
                venture,
                work_type,
                project,
                source,
                source_ref,
                occurred,
                Jsonb(meta or {}),
            ),
        ).fetchone()

        if supersedes:
            updated = conn.execute(
                "UPDATE context_entries SET superseded_by = %s "
                "WHERE id = %s AND superseded_by IS NULL RETURNING id",
                (row["id"], supersedes),
            ).fetchone()
            if updated is None:
                logger.warning(
                    "context.log_entry: supersedes=%s matched no open entry", supersedes
                )

    logger.info("context.log_entry: wrote %s entry %s", type, row["id"])
    out = {"id": str(row["id"]), "created_at": row["created_at"].isoformat()}

    # The entry is already written. This only ever adds a line of advice — a
    # warning that could block would make IBLU a grader, and it measures
    # instead. See store/gap_check.py.
    from ..store.gap_check import check as _gap_check

    warning = _gap_check(type, content, tag_list)
    if warning:
        out["gap_warning"] = warning
    return out


def log_conversation(
    conversation: str,
    role: str,
    text: str,
    source: str = "chat",
) -> dict:
    """DEPRECATED — use `log_entry`. Kept so existing callers keep working.

    Now persists a real `conversation_note` entry instead of only logging.
    """
    if settings.use_mock:
        return dict(_MOCK)
    return log_entry(
        type="conversation_note",
        content=text,
        importance=2,
        tags=["conversation", role],
        source="claude",
        source_ref=conversation,
        meta={"conversation": conversation, "role": role, "origin": source},
    )


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------


def search_entries(
    query: str | None = None,
    type: str | None = None,
    tags: Sequence[str] | None = None,
    venture: str | None = None,
    since: Any = None,
    limit: int = 20,
) -> dict:
    """Full-text + filtered search over durable entries (superseded excluded).

    Bumps `last_referenced_at` on every row returned, so the future compactor
    can tell which memories actually get used.
    """
    if settings.use_mock:
        return dict(_MOCK)

    if type is not None and type not in ENTRY_TYPES:
        raise ValidationError(
            f"unknown type={type!r} — valid types: {', '.join(ENTRY_TYPES)}"
        )
    limit = max(1, min(int(limit), 200))
    since_ts = _parse_ts(since, "since")

    where = ["superseded_by IS NULL"]
    params: list[Any] = []
    if query:
        where.append(
            "to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)"
        )
        params.append(query)
    if type:
        where.append("type = %s")
        params.append(type)
    if tags:
        where.append("tags && %s")
        params.append([str(t) for t in tags])
    if venture:
        where.append("venture = %s")
        params.append(venture)
    if since_ts:
        where.append("COALESCE(occurred_at, created_at) >= %s")
        params.append(since_ts)

    sql = f"""
        SELECT id, type, content, importance, tags, venture, work_type, project,
               source, source_ref, occurred_at, created_at, meta
        FROM context_entries
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(occurred_at, created_at) DESC
        LIMIT %s
    """

    with db.get_conn() as conn:
        _check_code(conn, "ventures", "venture", venture)
        rows = conn.execute(sql, (*params, limit)).fetchall()
        if rows:
            conn.execute(
                "UPDATE context_entries SET last_referenced_at = now() "
                "WHERE id = ANY(%s)",
                ([r["id"] for r in rows],),
            )

    return {"count": len(rows), "items": [_row_out(r) for r in rows]}


def get_summary(window: str = "1d") -> dict:
    """What the recorder saw in `window`: signal counts, pings, work_log.

    No LLM involved — this is a straight roll-up. Signal *counts* are reported,
    never signal rows: observations stay out of the memory surface (D1).
    """
    if settings.use_mock:
        return dict(_MOCK)

    delta = _parse_window(window)
    since = datetime.now(timezone.utc) - delta

    with db.get_conn() as conn:
        by_source = {
            r["source"]: r["n"]
            for r in conn.execute(
                "SELECT source, count(*) AS n FROM signals "
                "WHERE occurred_at >= %s GROUP BY source ORDER BY source",
                (since,),
            ).fetchall()
        }
        by_venture = {
            (r["venture"] or "unknown"): r["n"]
            for r in conn.execute(
                "SELECT venture, count(*) AS n FROM signals "
                "WHERE occurred_at >= %s GROUP BY venture ORDER BY n DESC",
                (since,),
            ).fetchall()
        }
        pings = conn.execute(
            """
            SELECT p.kind, p.local_date, p.status, p.composer, p.sent_at,
                   (SELECT count(*) FROM context_entries e
                     WHERE e.source = 'ping'
                       AND e.source_ref LIKE 'ping:' || p.id || ':%%'
                       AND e.superseded_by IS NULL) AS answered_questions
            FROM pings p
            WHERE p.window_start >= %s
            ORDER BY p.window_start DESC
            """,
            (since,),
        ).fetchall()
        work_log = conn.execute(
            """
            SELECT id, type, content, venture, work_type, project, source,
                   occurred_at, created_at
            FROM context_entries
            WHERE superseded_by IS NULL
              AND COALESCE(occurred_at, created_at) >= %s
            ORDER BY COALESCE(occurred_at, created_at) DESC
            LIMIT 100
            """,
            (since,),
        ).fetchall()

    return {
        "window": window,
        "since": since.isoformat(),
        "signals": {
            "total": sum(by_source.values()),
            "by_source": by_source,
            "by_venture": by_venture,
        },
        "pings": [
            {
                "kind": p["kind"],
                "local_date": p["local_date"].isoformat(),
                "status": p["status"],
                "composer": p["composer"],
                "sent_at": p["sent_at"].isoformat() if p["sent_at"] else None,
                "answered_questions": p["answered_questions"],
            }
            for p in pings
        ],
        "work_log": [_row_out(r) for r in work_log],
    }


def get_context(window: str = "1d") -> dict:
    """Everything an LLM needs before it decides anything: mission first.

    Read mission, priorities and baselines; measure backward from the
    baselines; judge everything else against the priorities.

    Order matters. The mission is what every other field is judged against, so
    it comes first and is never omitted — a summary read without it is just
    activity data. Priorities and baselines follow immediately, ahead of the
    brief and the summary, because they are the standard the rest of the
    payload is measured against, not more activity data themselves (plan
    §1.4).

    `mission_stale` compares docs/MISSION.md on disk with the runtime copy in
    the database: True when they differ, False when they match, and None when
    the file could not be read. None is not False — it means "could not check",
    and claiming the copy is current when we never looked would be the same
    class of lie as returning mock data silently.
    """
    if settings.use_mock:
        return dict(_MOCK)

    mission, sha = db.load_mission()

    on_disk = db.read_mission_file()
    if on_disk is None:
        stale = None
    else:
        stale = db.mission_sha(on_disk) != sha

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT content FROM context_brief WHERE id = 1"
        ).fetchone()
        priorities = governance.current_priorities(conn)
        baselines = governance.current_baselines(conn)
        rules = governance.gain_rules(conn)
    brief = (row["content"] if row else "") or ""

    return {
        "mission": mission,
        "mission_sha": sha,
        "mission_stale": stale,
        "priorities": priorities,
        "baselines": baselines,
        "gain_rules": rules,
        "brief": brief,
        "summary": get_summary(window),
    }


def reference_data() -> dict:
    """The venture / work_type taxonomy — for tool errors and the composer."""
    if settings.use_mock:
        return dict(_MOCK)
    with db.get_conn() as conn:
        ventures = conn.execute(
            "SELECT code, label FROM ventures WHERE active ORDER BY sort_order"
        ).fetchall()
        work_types = conn.execute(
            "SELECT code, label FROM work_types ORDER BY sort_order"
        ).fetchall()
    return {
        "ventures": [dict(r) for r in ventures],
        "work_types": [dict(r) for r in work_types],
    }
