"""Migration + context-tool tests.

Everything that needs Postgres is SKIPPED (never failed) when DATABASE_URL is
unset, so `pytest` stays green on a laptop with no database — acceptance A9.
"""

from __future__ import annotations

import uuid

import pytest

from iblu_keeper import db

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URL not set — database tests skipped by design",
)


@pytest.fixture
def live_ctx(monkeypatch):
    """The context tools with mock mode off (the suite pins DRY_RUN=true)."""
    from iblu_keeper.tools import context as ctx

    class _Live:
        use_mock = False
        dry_run = False

    monkeypatch.setattr(ctx, "settings", _Live())
    return ctx

EXPECTED_TABLES = {
    "ventures",
    "work_types",
    "signals",
    "context_entries",
    "context_brief",
    "pings",
    "calendar_seen",
    "collector_state",
    "schema_migrations",
}


# --- no database required -------------------------------------------------


def test_migration_files_are_discovered_and_ordered():
    files = db._migration_files()
    assert files, "expected at least one migration in db/migrations/"
    assert [f.name for f in files] == sorted(f.name for f in files)
    assert files[0].stem == "001_phase2_recording"


def test_migration_001_creates_every_expected_table():
    sql = (db.MIGRATIONS_DIR / "001_phase2_recording.sql").read_text()
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE {table} " in sql or f"CREATE TABLE IF NOT EXISTS {table} " in sql


class _NoDsn:
    database_url = ""


def test_db_operations_without_dsn_raise_a_clear_error(monkeypatch):
    """A missing DATABASE_URL must fail loudly, never silently no-op."""
    monkeypatch.setattr(db, "settings", _NoDsn())
    db.close_pool()
    with pytest.raises(db.DatabaseNotConfigured) as exc:
        db._require_dsn()
    assert "DATABASE_URL" in str(exc.value)


def test_healthcheck_reports_unconfigured_without_dsn(monkeypatch):
    monkeypatch.setattr(db, "settings", _NoDsn())
    db.close_pool()
    assert db.healthcheck() == {
        "configured": False,
        "ok": False,
        "error": "DATABASE_URL not set",
    }


# --- database required ----------------------------------------------------


@requires_db
def test_schema_is_fully_migrated():
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    assert EXPECTED_TABLES <= {r["tablename"] for r in rows}


@requires_db
def test_migrate_is_idempotent():
    assert db.migrate() == []  # already applied; a second run changes nothing
    assert db.status()["pending"] == []


@requires_db
def test_reference_data_seeded():
    with db.get_conn() as conn:
        ventures = {r["code"] for r in conn.execute("SELECT code FROM ventures").fetchall()}
        work_types = {r["code"] for r in conn.execute("SELECT code FROM work_types").fetchall()}
    assert {"blt", "choco", "deadlift", "jakusi", "family", "personal"} == ventures
    assert {"sales", "client", "delivery", "people", "finance", "build", "admin", "life"} == work_types


@requires_db
def test_log_search_and_supersede_roundtrip(live_ctx):
    ctx = live_ctx

    marker = f"pytest-{uuid.uuid4().hex[:12]}"

    first = ctx.log_entry(type="fact", content=f"{marker} original", venture="blt")
    assert first["id"]

    found = ctx.search_entries(query=marker)
    assert found["count"] == 1
    assert found["items"][0]["id"] == first["id"]

    # A correction supersedes rather than deletes (plan D9).
    second = ctx.log_entry(
        type="correction",
        content=f"{marker} corrected",
        venture="blt",
        supersedes=first["id"],
    )
    found = ctx.search_entries(query=marker)
    assert found["count"] == 1, "superseded entry must not be returned"
    assert found["items"][0]["id"] == second["id"]

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT superseded_by FROM context_entries WHERE id = %s", (first["id"],)
        ).fetchone()
        assert str(row["superseded_by"]) == second["id"]
        conn.execute(
            "DELETE FROM context_entries WHERE content LIKE %s", (f"{marker}%",)
        )


@requires_db
def test_invalid_codes_are_rejected_with_the_valid_list(live_ctx):
    ctx = live_ctx

    with pytest.raises(ctx.ValidationError) as exc:
        ctx.log_entry(type="fact", content="x", venture="nope")
    assert "deadlift" in str(exc.value)

    with pytest.raises(ctx.ValidationError) as exc:
        ctx.log_entry(type="not_a_type", content="x")
    assert "work_log" in str(exc.value)


@requires_db
def test_get_summary_shape(live_ctx):
    ctx = live_ctx

    out = ctx.get_summary("1d")
    assert set(out) >= {"window", "since", "signals", "pings", "work_log"}
    assert set(out["signals"]) == {"total", "by_source", "by_venture"}
