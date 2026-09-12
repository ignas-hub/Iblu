"""Postgres access for Phase 2 ("recording v1").

A single lazily-created `psycopg` connection pool built from `DATABASE_URL`,
plus a file-based migration runner. See docs/plans/2026-09-14-recording-v1.md
§4 (schema) and §5 (code layout).

Design notes:
  * psycopg 3 directly, no ORM (plan D10).
  * Nothing here connects at import time — importing this module on a machine
    with no database (developer laptop, CI) is always safe. `is_configured()`
    is the cheap guard callers use before touching the DB.
  * All timestamps are `timestamptz` and handled in UTC; local-time scheduling
    happens in the pings layer, never here.

CLI:
    python -m iblu_keeper.db migrate     # apply pending migrations
    python -m iblu_keeper.db status      # show applied / pending
"""

from __future__ import annotations

import logging
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

logger = logging.getLogger("iblu_keeper.db")

# db/migrations/ lives at the repo root; this file is src/iblu_keeper/db.py.
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


class DatabaseNotConfigured(RuntimeError):
    """Raised when a DB operation is attempted without DATABASE_URL set.

    Deliberately loud: a missing database must never degrade into silently
    writing nothing or returning fake rows (see DEBUG_FINDINGS.md).
    """


def is_configured() -> bool:
    """True when DATABASE_URL is set. Does not open a connection."""
    return bool(settings.database_url)


def _require_dsn() -> str:
    if not settings.database_url:
        raise DatabaseNotConfigured(
            "DATABASE_URL is not set — refusing to guess a database. "
            "Set it in .env (see .env.example)."
        )
    return settings.database_url


def get_pool() -> ConnectionPool:
    """Return the process-wide connection pool, creating it on first use."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                dsn = _require_dsn()
                _pool = ConnectionPool(
                    dsn,
                    min_size=1,
                    max_size=4,
                    timeout=10.0,
                    max_idle=300.0,
                    kwargs={"row_factory": dict_row},
                    open=True,
                    name="iblu",
                )
                logger.info("db: connection pool opened (max_size=4)")
    return _pool


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """Yield a pooled connection inside a transaction.

    Commits on clean exit, rolls back on exception. Rows come back as dicts.
    """
    with get_pool().connection() as conn:
        yield conn


def close_pool() -> None:
    """Close the pool (tests, shutdown). Safe to call when never opened."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None
            logger.info("db: connection pool closed")


# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------


def _migration_files() -> list[Path]:
    """All migration files, ordered by filename (NNN_ prefix sorts naturally)."""
    if not MIGRATIONS_DIR.is_dir():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def applied_versions(conn: psycopg.Connection) -> set[str]:
    """Versions already recorded in schema_migrations.

    Returns an empty set when the table does not exist yet — that is the
    expected state before the first migration runs, not an error.
    """
    exists = conn.execute(
        "SELECT to_regclass('public.schema_migrations') AS t"
    ).fetchone()["t"]
    if exists is None:
        return set()
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r["version"] for r in rows}


def pending_migrations() -> list[Path]:
    """Migration files whose version is not yet recorded as applied."""
    with get_conn() as conn:
        done = applied_versions(conn)
    return [p for p in _migration_files() if p.stem not in done]


def migrate() -> list[str]:
    """Apply every pending migration in order. Returns versions applied.

    Each file runs in its own transaction: a failing migration leaves the
    database exactly as it was before that file. Each migration is expected to
    record itself in schema_migrations (the 001 file does); if it does not, the
    runner records it so the file is not re-applied.
    """
    files = _migration_files()
    if not files:
        logger.warning("db: no migration files found in %s", MIGRATIONS_DIR)
        return []

    applied: list[str] = []
    with get_conn() as conn:
        done = applied_versions(conn)

    for path in files:
        version = path.stem
        if version in done:
            logger.debug("db: %s already applied, skipping", version)
            continue

        sql = path.read_text(encoding="utf-8")
        logger.info("db: applying migration %s", version)
        with get_conn() as conn:
            conn.execute(sql)
            # Belt and braces: if the file forgot to record itself, do it here.
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s) "
                "ON CONFLICT (version) DO NOTHING",
                (version,),
            )
        applied.append(version)
        logger.info("db: migration %s applied", version)

    if not applied:
        logger.info("db: schema already up to date (%d migrations)", len(files))
    return applied


def status() -> dict:
    """Summary of migration state, for the CLI and server_health."""
    files = _migration_files()
    with get_conn() as conn:
        done = applied_versions(conn)
    return {
        "migrations_dir": str(MIGRATIONS_DIR),
        "applied": sorted(done),
        "pending": [p.stem for p in files if p.stem not in done],
    }


def healthcheck() -> dict:
    """Cheap liveness probe used by server_health / the /health endpoint."""
    if not is_configured():
        return {"configured": False, "ok": False, "error": "DATABASE_URL not set"}
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT current_database() AS db, "
                "(SELECT count(*) FROM schema_migrations) AS migrations"
            ).fetchone()
        return {
            "configured": True,
            "ok": True,
            "database": row["db"],
            "migrations_applied": row["migrations"],
        }
    except Exception as exc:  # pragma: no cover - depends on live DB
        logger.warning("db: healthcheck failed: %s", exc)
        return {"configured": True, "ok": False, "error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m iblu_keeper.db <command>`."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "status"

    try:
        if command == "migrate":
            applied = migrate()
            print(
                f"applied: {', '.join(applied)}" if applied else "nothing to apply"
            )
        elif command == "status":
            st = status()
            print(f"applied: {', '.join(st['applied']) or '(none)'}")
            print(f"pending: {', '.join(st['pending']) or '(none)'}")
        else:
            print(f"unknown command: {command!r} (expected 'migrate' or 'status')")
            return 2
    except DatabaseNotConfigured as exc:
        print(f"error: {exc}")
        return 1
    finally:
        close_pool()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
