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
    """A second run changes nothing — and a FIRST run must never happen here.

    Found 2026-09-13: with `DATABASE_URL` set, this test called `db.migrate()`
    unconditionally, so simply running `pytest` on the server APPLIED whatever
    migration happened to be sitting untracked in the working tree. A test
    suite that alters a live schema as a side effect is a trap: the schema
    changes before anyone has read the migration, and nothing in the output
    says so. If something is pending, that is a deployment decision — skip and
    let a human run `python -m iblu_keeper.db migrate`.
    """
    pending = db.status()["pending"]
    if pending:
        pytest.skip(
            f"migrations pending ({', '.join(pending)}) — a test must never "
            f"apply them to a live database; run `python -m iblu_keeper.db migrate`"
        )
    assert db.migrate() == []
    assert db.status()["pending"] == []


@requires_db
def test_reference_data_seeded():
    with db.get_conn() as conn:
        ventures = {r["code"] for r in conn.execute("SELECT code FROM ventures").fetchall()}
        work_types = {r["code"] for r in conn.execute("SELECT code FROM work_types").fetchall()}
    # A subset, not an equality: the mission says adding a venture is one row,
    # not a migration, so ventures legitimately appear at runtime (gostellar was
    # added 2026-09-13 when Ignas set its yearly priority). What migration 001
    # seeded must still be there.
    assert {"blt", "choco", "deadlift", "jakusi", "family", "personal"} <= ventures
    # Work types are a closed taxonomy — a new one would change the meaning of
    # every previous answer, so this one stays an equality.
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


# --- migration 005: blocks ------------------------------------------------


def test_migration_005_creates_blocks_and_admits_slack():
    sql = (db.MIGRATIONS_DIR / "005_blocks.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS blocks " in sql
    # Slack joins the existing three sources rather than replacing them.
    for source in ("gmail", "chat", "calendar", "slack"):
        assert f"'{source}'" in sql


def test_blocks_attention_is_three_valued():
    """'ambiguous' must stay a legal value: silence is never presence, and a
    two-valued column would force every unobserved stretch into a lie."""
    sql = (db.MIGRATIONS_DIR / "005_blocks.sql").read_text()
    assert "attention IN ('present','displaced','ambiguous')" in sql


def test_blocks_supersede_rather_than_delete():
    sql = (db.MIGRATIONS_DIR / "005_blocks.sql").read_text()
    assert "superseded_by" in sql
    assert "ON DELETE CASCADE" not in sql


def test_migrate_only_rejects_an_unknown_version():
    """`--only` exists so a reviewed migration can be applied without dragging
    along a draft that another session is still writing (2026-09-17)."""
    import pytest as _pytest

    if not db.is_configured():
        _pytest.skip("DATABASE_URL not set")
    with _pytest.raises(ValueError, match="no migration named"):
        db.migrate(only="999_does_not_exist")


def test_migrate_only_on_an_applied_version_does_nothing():
    import pytest as _pytest

    if not db.is_configured():
        _pytest.skip("DATABASE_URL not set")
    assert db.migrate(only="001_phase2_recording") == []
