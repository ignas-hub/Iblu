"""The mission layer (session 4).

docs/MISSION.md is the source of truth; context_brief.mission is the runtime
copy. These tests cover the sha-based idempotency, the staleness signal, and
the fact that the composer carries the mission into its system prompt.

DB-backed tests skip (never fail) without DATABASE_URL.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from iblu_keeper import db

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URL not set — database tests skipped by design",
)


# --- no database required -------------------------------------------------


def test_mission_file_exists_and_is_the_source_of_truth():
    text = db.read_mission_file()
    assert text is not None
    assert text.startswith("# IBLU — Mission")
    # The four things IBLU must become are the load-bearing part.
    assert "knows the truth about where Ignas's attention goes" in text
    assert "Silence is never presence" in text


def test_sha_is_stable_and_change_sensitive():
    assert db.mission_sha("abc") == db.mission_sha("abc")
    assert db.mission_sha("abc") != db.mission_sha("abd")
    assert len(db.mission_sha("abc")) == 64


def test_unreadable_mission_file_returns_none_not_empty_string():
    """None means 'could not check' — it must never be confused with ''."""
    assert db.read_mission_file(Path("/nonexistent/MISSION.md")) is None


def test_seed_refuses_an_empty_or_missing_file(tmp_path):
    empty = tmp_path / "MISSION.md"
    empty.write_text("   \n")
    with pytest.raises(ValueError):
        db.seed_mission(empty)
    with pytest.raises(FileNotFoundError):
        db.seed_mission(tmp_path / "nope.md")


def test_get_context_is_inert_in_mock_mode(monkeypatch):
    from iblu_keeper.tools import context as ctx

    class _Mock:
        use_mock = True
        dry_run = True

    monkeypatch.setattr(ctx, "settings", _Mock())

    def boom(*_a, **_k):
        raise AssertionError("mock mode must never open a database connection")

    monkeypatch.setattr(ctx.db, "get_conn", boom)
    assert ctx.get_context("1d") == {"status": "mock"}


def test_server_instructions_lead_with_the_mission():
    """Connect-time tokens are paid every session — four lines, not the lot."""
    from iblu_keeper import server

    instructions = server.mcp.instructions
    assert instructions.startswith("WHAT IBLU MUST BECOME:")
    assert "Full mission: call `get_context`." in instructions
    # The full mission must NOT be inlined (M4).
    assert "Silence is never presence" not in instructions
    # The existing voice / freshness protocol survives below it.
    assert "FRESHNESS PROTOCOL" in instructions


def test_composer_prompt_carries_the_mission(monkeypatch, caplog):
    """The questions must be judged against what IBLU is for (M3)."""
    from iblu_keeper.pings import compose

    captured = {}

    class _Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop here — we only wanted the assembled prompt")

    class _Client:
        def __init__(self, **_kw):
            self.messages = _Messages()

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    # Settings is a frozen dataclass: substitute the module-level object.
    monkeypatch.setattr(compose, "settings", _StubSettings())
    monkeypatch.setattr(db, "load_mission", lambda: ("MISSION TEXT HERE", "abc123def456" + "0" * 52))

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError):
            compose.compose_llm([], [], _now(), _now(), [], [])

    assert "MISSION TEXT HERE" in captured["system"]
    assert "mission sha=abc123def456 loaded" in caplog.text
    # and never a sampling parameter — sonnet-5 returns 400
    assert "temperature" not in captured


def test_composer_continues_without_a_mission(monkeypatch, caplog):
    """Monday must not depend on the mission having been seeded."""
    from iblu_keeper.pings import compose

    captured = {}

    class _Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop")

    class _Client:
        def __init__(self, **_kw):
            self.messages = _Messages()

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    monkeypatch.setattr(compose, "settings", _StubSettings())
    monkeypatch.setattr(db, "load_mission", lambda: ("", None))

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError):
            compose.compose_llm([], [], _now(), _now(), [], [])

    assert "mission EMPTY — continuing" in caplog.text
    assert captured["system"].startswith("You write a 3-question tap-quiz")


class _StubSettings:
    """Stand-in for the frozen Settings dataclass (rule: substitute, never patch)."""

    use_mock = False
    dry_run = False
    anthropic_api_key = "test-key-not-real"
    iblu_llm_model = "claude-sonnet-5"


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# --- database required ----------------------------------------------------


@requires_db
def test_seed_is_idempotent_by_sha(tmp_path):
    original = db.read_mission_file()

    changed_first, sha_first = db.seed_mission()
    changed_again, sha_again = db.seed_mission()
    assert changed_again is False, "re-seeding an unchanged file must not rewrite"
    assert sha_first == sha_again

    # A changed file re-seeds, and the sha moves with it.
    edited = tmp_path / "MISSION.md"
    edited.write_text(original + "\n<!-- test edit -->\n")
    changed, sha_edited = db.seed_mission(edited)
    assert changed is True and sha_edited != sha_first

    # Put the real mission back.
    db.seed_mission()
    assert db.load_mission()[1] == sha_first


@requires_db
def test_get_context_returns_the_mission_first(monkeypatch):
    from iblu_keeper.tools import context as ctx

    class _Live:
        use_mock = False
        dry_run = False

    monkeypatch.setattr(ctx, "settings", _Live())
    db.seed_mission()

    out = ctx.get_context("1d")
    assert list(out)[:3] == ["mission", "mission_sha", "mission_stale"]
    assert out["mission"].startswith("# IBLU — Mission")
    assert out["mission_stale"] is False
    assert set(out["summary"]) >= {"window", "signals", "pings", "work_log"}


@requires_db
def test_mission_stale_is_true_false_or_none(monkeypatch, tmp_path):
    from iblu_keeper.tools import context as ctx

    class _Live:
        use_mock = False
        dry_run = False

    monkeypatch.setattr(ctx, "settings", _Live())
    db.seed_mission()
    assert ctx.get_context("1d")["mission_stale"] is False

    # File differs from the seeded copy -> stale.
    monkeypatch.setattr(db, "read_mission_file", lambda *_a, **_k: "something else")
    assert ctx.get_context("1d")["mission_stale"] is True

    # File unreadable -> None, which is NOT False.
    monkeypatch.setattr(db, "read_mission_file", lambda *_a, **_k: None)
    assert ctx.get_context("1d")["mission_stale"] is None
