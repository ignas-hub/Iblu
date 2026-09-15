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
        -- clock_timestamp(), not now(): `now()` is the TRANSACTION start time,
        -- and every collector in a tick shares one transaction. It recorded all
        -- of them as having run at the instant the tick began, which made
        -- last_run_at read slightly EARLIER than a watermark taken mid-run.
        VALUES (%s, %s, %s, clock_timestamp(), %s)
        ON CONFLICT (name) DO UPDATE SET
            watermark   = COALESCE(EXCLUDED.watermark, collector_state.watermark),
            cursor      = COALESCE(EXCLUDED.cursor,    collector_state.cursor),
            last_run_at = clock_timestamp(),
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


def _registry() -> list[tuple[str, Callable, str]]:
    """Imported lazily so `import collectors` never pulls in Google clients.

    Each entry is `(name, fn, scope)`. `scope` says what `run_all` iterates
    the collector over:
      * "google" — every configured Google account (`settings.configured_accounts`).
        The Chat backend is cached per account (each resolves its own self-id)
        and `calendar_seen` is namespaced by account (migration 004), so one
        Workspace's baseline can never answer for another's.
      * "slack"  — every configured Slack workspace (`settings.configured_slack`),
        a wholly separate list from the Google accounts above (Blank Label and
        Deadlift are Slack workspaces, not `google_accounts` aliases).
      * "primary" — runs once, against the primary Google account only.
    """
    from .calendar_changes import collect as calendar_collect
    from .chat_sent import collect as chat_collect
    from .gmail_sent import collect as gmail_collect
    from .slack_sent import collect as slack_collect

    return [
        ("gmail_sent", gmail_collect, "google"),
        ("chat_sent", chat_collect, "google"),
        ("calendar_changes", calendar_collect, "google"),
        ("slack_sent", slack_collect, "slack"),
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

    accounts = settings.configured_accounts() or [
        settings.account(settings.primary_alias)
    ]
    primary = settings.primary_alias
    slack_workspaces = settings.configured_slack()

    results: dict[str, int | str] = {}
    for name, fn, scope in _registry():
        if scope == "google":
            targets = accounts
        elif scope == "slack":
            targets = slack_workspaces
        else:
            targets = [settings.account(primary)]

        for target in targets:
            alias = target["alias"]
            if scope == "slack":
                # No "primary" Slack workspace — every alias is namespaced,
                # exactly like a non-primary Google account.
                key = f"{name}:{alias}"
            else:
                key = name if (scope == "primary" or alias == primary) else f"{name}:{alias}"
            try:
                # Each collector runs in its own SAVEPOINT. Without one, a
                # database-level error (a bad venture code violating the
                # foreign key, say) poisons the shared transaction: every later
                # statement raises InFailedSqlTransaction, including the
                # set_state that tries to record the failure, and the final
                # commit silently rolls back EVERY signal the earlier
                # collectors had already inserted — while the tick's log line
                # still reports them as collected. "A failing collector never
                # stops the others" was true only for Python-level errors.
                with conn.transaction():
                    if scope == "google":
                        results[key] = fn(conn, dry=dry, account=target)
                    elif scope == "slack":
                        results[key] = fn(conn, dry=dry, workspace=target)
                    else:
                        results[key] = fn(conn, dry=dry)
            except Exception as exc:  # one account's outage is not the job's
                logger.exception("collector %s failed", key)
                results[key] = f"error: {exc}"
                if not dry:
                    try:
                        # The savepoint rolled back, so the connection is usable
                        # again and this record actually lands.
                        with conn.transaction():
                            set_state(conn, key, error=str(exc)[:500])
                    except Exception:  # pragma: no cover
                        logger.exception("collector %s: could not record last_error", key)
    return results
