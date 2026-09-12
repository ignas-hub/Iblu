"""Mock-mode guarantees for the context tools.

DRY_RUN=true must never reach Postgres and never write a row — the whole point
of DEBUG_FINDINGS.md. These tests need no database at all.
"""

from __future__ import annotations

import pytest

from iblu_keeper.tools import context as ctx


@pytest.fixture
def mock_mode(monkeypatch):
    """Mock mode on, and a landmine where the database connection would be."""

    class _Mock:
        use_mock = True
        dry_run = True

    monkeypatch.setattr(ctx, "settings", _Mock())

    def boom(*_args, **_kwargs):
        raise AssertionError("mock mode must never open a database connection")

    monkeypatch.setattr(ctx.db, "get_conn", boom)
    return ctx


def test_log_entry_is_a_noop_in_mock_mode(mock_mode):
    assert mock_mode.log_entry(type="fact", content="anything") == {"status": "mock"}


def test_search_is_a_noop_in_mock_mode(mock_mode):
    assert mock_mode.search_entries(query="anything") == {"status": "mock"}


def test_summary_is_a_noop_in_mock_mode(mock_mode):
    assert mock_mode.get_summary("1d") == {"status": "mock"}


def test_log_conversation_is_a_noop_in_mock_mode(mock_mode):
    assert mock_mode.log_conversation("c", "user", "hi") == {"status": "mock"}


# --- pure helpers, no mode dependence ------------------------------------


@pytest.mark.parametrize(
    "window,seconds",
    [("1d", 86400), ("12h", 43200), ("2w", 1209600), (" 3 d ", 259200)],
)
def test_parse_window(window, seconds):
    assert ctx._parse_window(window).total_seconds() == seconds


@pytest.mark.parametrize("bad", ["", "1", "d", "1m", "tomorrow", None])
def test_parse_window_rejects_garbage(bad):
    with pytest.raises(ctx.ValidationError):
        ctx._parse_window(bad)


def test_parse_ts_normalises_to_utc():
    out = ctx._parse_ts("2026-09-14T09:30:00Z", "occurred_at")
    assert out.tzinfo is not None and out.utcoffset().total_seconds() == 0


def test_parse_ts_rejects_garbage():
    with pytest.raises(ctx.ValidationError):
        ctx._parse_ts("not a date", "occurred_at")
