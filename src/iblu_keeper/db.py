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
    python -m iblu_keeper.db migrate       # apply pending migrations
    python -m iblu_keeper.db status        # show applied / pending
    python -m iblu_keeper.db seed-mission  # copy docs/MISSION.md into the DB
"""

from __future__ import annotations

import hashlib
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
REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
# docs/MISSION.md is the source of truth; context_brief.mission is the
# runtime copy (decision M2).
MISSION_FILE = REPO_ROOT / "docs" / "MISSION.md"

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


# --------------------------------------------------------------------------
# Mission
# --------------------------------------------------------------------------


def mission_sha(text: str) -> str:
    """sha256 of the mission text. Used to detect drift, never for security."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_mission_file(path: Path | None = None) -> str | None:
    """The mission as it exists on disk, or None when it cannot be read.

    None is meaningful: `get_context` reports `mission_stale=None` rather than
    claiming the DB copy is current when we simply could not check.
    """
    target = path or MISSION_FILE
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("mission: cannot read %s: %s", target, exc)
        return None


def seed_mission(path: Path | None = None) -> tuple[bool, str]:
    """Copy docs/MISSION.md into context_brief. Returns `(changed, sha)`.

    Idempotent by sha (M2): the row is rewritten only when the file's digest
    differs from the stored one, so running this on every deploy is free and
    `mission_seeded_at` means "when the text last actually changed".
    """
    target = path or MISSION_FILE
    text = read_mission_file(target)
    if text is None:
        raise FileNotFoundError(f"mission file not readable: {target}")
    if not text.strip():
        raise ValueError(f"mission file is empty: {target}")

    sha = mission_sha(text)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT mission_sha FROM context_brief WHERE id = 1"
        ).fetchone()
        if row is not None and row["mission_sha"] == sha:
            return False, sha
        conn.execute(
            """
            INSERT INTO context_brief (id, mission, mission_sha, mission_seeded_at)
            VALUES (1, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET
                mission = EXCLUDED.mission,
                mission_sha = EXCLUDED.mission_sha,
                mission_seeded_at = now()
            """,
            (text, sha),
        )
    return True, sha


def load_mission() -> tuple[str, str | None]:
    """The runtime mission copy: `(text, sha)`. Empty string when unseeded."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT mission, mission_sha FROM context_brief WHERE id = 1"
        ).fetchone()
    if row is None:
        return "", None
    return row["mission"] or "", row["mission_sha"]


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
        elif command == "seed-mission":
            # Never print the mission text — only whether it moved, and the sha.
            if settings.use_mock:
                print("refusing to run in mock mode")
                return 1
            path = None
            if "--file" in args:
                path = Path(args[args.index("--file") + 1])
            changed, sha = seed_mission(path)
            print(f"mission {'seeded' if changed else 'unchanged'} sha={sha[:12]}")
        elif command == "status":
            st = status()
            print(f"applied: {', '.join(st['applied']) or '(none)'}")
            print(f"pending: {', '.join(st['pending']) or '(none)'}")
        else:
            print(
                f"unknown command: {command!r} — expected "
                "'migrate', 'status' or 'seed-mission'"
            )
            return 2
    except (DatabaseNotConfigured, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    finally:
        close_pool()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
