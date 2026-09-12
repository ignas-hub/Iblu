"""Signal collectors — what IBLU observed, for one account.

Each collector exposes `collect(conn, *, dry=False) -> int` and owns a row in
`collector_state` (its watermark, cursor, last run and last error). A failing
collector records its error and does NOT abort the others (plan §6): losing
Chat for an hour must not also cost the day's mail.

Idempotency is the database's job, not the caller's: every signal carries a
native `source_ref` and the table has `UNIQUE (source, source_ref)`, so
re-running a collector over an overlapping window inserts nothing new.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

import psycopg

from ..config import settings

logger = logging.getLogger("iblu_keeper.collectors")

# Re-read this far before the watermark, so a message that arrived while the
# previous run was mid-flight is not missed. UNIQUE(source, source_ref) makes
# the overlap free.
OVERLAP = timedelta(minutes=5)


class Collector(Protocol):
    name: str

    def collect(self, conn: psycopg.Connection, *, dry: bool = False) -> int: ...


def get_watermark(conn: psycopg.Connection, name: str) -> datetime | None:
    """The point this collector last reached, minus the overlap."""
    row = conn.execute(
        "SELECT watermark FROM collector_state WHERE name = %s", (name,)
    ).fetchone()
    if row is None or row["watermark"] is None:
        return None
    return row["watermark"] - OVERLAP


def get_cursor(conn: psycopg.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT cursor FROM collector_state WHERE name = %s", (name,)
    ).fetchone()
    return row["cursor"] if row else None


def set_state(
    conn: psycopg.Connection,
    name: str,
    *,
    watermark: datetime | None = None,
    cursor: str | None = None,
    error: str | None = None,
) -> None:
    """Upsert this collector's state. Only non-None fields are written."""
    conn.execute(
        """
        INSERT INTO collector_state (name, watermark, cursor, last_run_at, last_error)
        VALUES (%s, %s, %s, now(), %s)
        ON CONFLICT (name) DO UPDATE SET
            watermark   = COALESCE(EXCLUDED.watermark, collector_state.watermark),
            cursor      = COALESCE(EXCLUDED.cursor,    collector_state.cursor),
            last_run_at = now(),
            last_error  = EXCLUDED.last_error
        """,
        (name, watermark, cursor, error),
    )


def default_since(watermark: datetime | None, fallback_hours: int = 24) -> datetime:
    """Where to start reading: the watermark, or a bounded first-run window.

    A first run must not try to ingest all of history — it looks back a day.
    """
    if watermark is not None:
        return watermark
    return datetime.now(timezone.utc) - timedelta(hours=fallback_hours)


def insert_signal(conn: psycopg.Connection, row: dict) -> bool:
    """Insert one signal. Returns False when it was already recorded."""
    from psycopg.types.json import Jsonb

    result = conn.execute(
        """
        INSERT INTO signals
            (source, kind, account, occurred_at, actor, initiator, counterpart,
             container, subject, snippet, ask_snippet, length_chars, venture,
             venture_confidence, work_type, project, source_ref, meta)
        VALUES
            (%(source)s, %(kind)s, %(account)s, %(occurred_at)s, %(actor)s,
             %(initiator)s, %(counterpart)s, %(container)s, %(subject)s,
             %(snippet)s, %(ask_snippet)s, %(length_chars)s, %(venture)s,
             %(venture_confidence)s, %(work_type)s, %(project)s,
             %(source_ref)s, %(meta)s)
        ON CONFLICT (source, source_ref) DO NOTHING
        RETURNING id
        """,
        {
            "actor": "me",
            "initiator": None,
            "counterpart": None,
            "container": None,
            "subject": None,
            "snippet": None,
            "ask_snippet": None,
            "length_chars": None,
            "venture": None,
            "venture_confidence": "inferred",
            "work_type": None,
            "project": None,
            **row,
            "meta": Jsonb(row.get("meta") or {}),
        },
    ).fetchone()
    return result is not None


def _registry() -> list[tuple[str, Callable]]:
    """Imported lazily so `import collectors` never pulls in Google clients."""
    from .calendar_changes import collect as calendar_collect
    from .chat_sent import collect as chat_collect
    from .gmail_sent import collect as gmail_collect

    return [
        ("gmail_sent", gmail_collect),
        ("chat_sent", chat_collect),
        ("calendar_changes", calendar_collect),
    ]


def run_all(conn: psycopg.Connection, *, dry: bool = False) -> dict[str, int | str]:
    """Run every collector. One failing collector never stops the others.

    Returns `{name: count}`, or `{name: "error: ..."}` for a collector that
    raised — the caller logs the summary line and exits 0 either way.
    """
    if settings.use_mock:
        raise RuntimeError(
            "refusing to collect in mock mode — DRY_RUN=true would write fake "
            "rows into a real database"
        )

    results: dict[str, int | str] = {}
    for name, fn in _registry():
        try:
            results[name] = fn(conn, dry=dry)
        except Exception as exc:  # one collector's outage is not the job's
            logger.exception("collector %s failed", name)
            results[name] = f"error: {exc}"
            if not dry:
                try:
                    set_state(conn, name, error=str(exc)[:500])
                except Exception:  # pragma: no cover - state write is best effort
                    logger.exception("collector %s: could not record last_error", name)
    return results
