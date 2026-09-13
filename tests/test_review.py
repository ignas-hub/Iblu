"""The analyst read (stage 2).

These tests exist mainly to protect two mission principles that are easy to
erode: silence must never be reported as presence, and work_type must never be
inferred — only what Ignas tapped counts.
"""

from __future__ import annotations

import pytest

from iblu_keeper import db
from iblu_keeper.tools import review

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URL not set — database tests skipped by design",
)


# --- no database ----------------------------------------------------------


def test_mock_mode_is_inert(monkeypatch):
    class _Mock:
        use_mock = True
        dry_run = True

    monkeypatch.setattr(review, "settings", _Mock())

    def boom(*_a, **_k):
        raise AssertionError("mock mode must never open a database connection")

    monkeypatch.setattr(review.db, "get_conn", boom)
    assert review.review("7d") == {"status": "mock"}
    assert "mock" in review.as_markdown({"status": "mock"})


def test_share_sums_to_about_a_hundred():
    out = review._share({"blt": 7, "deadlift": 3})
    assert [o["key"] for o in out] == ["blt", "deadlift"]
    assert sum(o["share_pct"] for o in out) == 100


def test_share_of_nothing_is_empty_not_a_division_error():
    assert review._share({}) == []


def test_markdown_warns_when_coverage_is_thin():
    """A confident split over two observed days is the flattery we forbid."""
    thin = {
        "coverage": {"signals": 35, "days_with_signals": 2, "days_in_window": 7},
        "by_venture": [{"key": "blt", "signals": 35, "share_pct": 100}],
        "by_work_type": [],
        "inbound": {"share_pct": 57, "signals_in_threads_i_did_not_start": 20},
        "recurring": [],
        "pings": {"sent": 0, "answered": 0, "answer_rate_pct": None, "target_pct": 80},
    }
    assert "Thin coverage" in review.as_markdown(thin)

    full = dict(thin, coverage={"signals": 400, "days_with_signals": 6, "days_in_window": 7})
    assert "Thin coverage" not in review.as_markdown(full)


def test_markdown_says_so_when_no_work_type_was_tapped():
    """Empty must read as 'not asked yet', never as 'no work happened'."""
    data = {
        "coverage": {"signals": 10, "days_with_signals": 5, "days_in_window": 7},
        "by_venture": [], "by_work_type": [],
        "inbound": {"share_pct": 0, "signals_in_threads_i_did_not_start": 0},
        "recurring": [],
        "pings": {"sent": 0, "answered": 0, "answer_rate_pct": None, "target_pct": 80},
    }
    assert "no pings answered" in review.as_markdown(data)


def test_markdown_scores_the_ping_habit_against_its_target():
    base = {
        "coverage": {"signals": 10, "days_with_signals": 5, "days_in_window": 7},
        "by_venture": [], "by_work_type": [],
        "inbound": {"share_pct": 0, "signals_in_threads_i_did_not_start": 0},
        "recurring": [],
    }
    good = review.as_markdown(dict(base, pings={"sent": 10, "answered": 9, "answer_rate_pct": 90, "target_pct": 80}))
    bad = review.as_markdown(dict(base, pings={"sent": 10, "answered": 4, "answer_rate_pct": 40, "target_pct": 80}))
    assert "on target" in good and "below target" in bad


# --- database -------------------------------------------------------------


@requires_db
def test_review_shape_against_the_real_database(monkeypatch):
    class _Live:
        use_mock = False
        dry_run = False

    monkeypatch.setattr(review, "settings", _Live())
    out = review.review("7d")

    assert set(out) >= {
        "window", "coverage", "by_venture", "by_source", "by_work_type",
        "top_threads", "recurring", "inbound", "pings",
    }
    assert out["coverage"]["days_in_window"] == 7

    # Mission: attention, not location. Nothing may imply a duration we never
    # measured — no field named for time, and the disclaimer must be present.
    forbidden = {"hours", "minutes", "duration", "time_spent", "elapsed"}
    def _keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from _keys(v)
        elif isinstance(node, list):
            for item in node:
                yield from _keys(item)
    assert not (forbidden & set(_keys(out))), "a field implies measured time"
    assert "not hours" in out["coverage"]["note"]
    for thread in out["top_threads"]:
        assert thread["started_by"] in {"me", "other", "unknown"}


@requires_db
def test_work_type_comes_only_from_tapped_answers(monkeypatch):
    """signals.work_type is inferred and must never reach the work-type split."""
    class _Live:
        use_mock = False
        dry_run = False

    monkeypatch.setattr(review, "settings", _Live())

    with db.get_conn() as conn:
        tapped = conn.execute(
            "SELECT count(*) AS n FROM context_entries "
            "WHERE source IN ('ping','chat_reply') AND work_type IS NOT NULL "
            "AND superseded_by IS NULL"
        ).fetchone()["n"]

    out = review.review("7d")
    if tapped == 0:
        assert out["by_work_type"] == [], (
            "with no tapped answers the work-type split must be empty, "
            "not filled from inferred signal data"
        )


# --- the weekly job -------------------------------------------------------


def test_weekly_refuses_in_mock_mode(monkeypatch):
    from iblu_keeper.jobs import weekly

    class _Mock:
        use_mock = True
        dry_run = True
        secretary_webhook_url = "https://example.invalid"

    monkeypatch.setattr(weekly, "settings", _Mock())
    assert "mock mode" in weekly._guard()


def test_weekly_refuses_without_a_webhook(monkeypatch):
    from iblu_keeper.jobs import weekly

    class _NoHook:
        use_mock = False
        dry_run = False
        secretary_webhook_url = ""

    monkeypatch.setattr(weekly, "settings", _NoHook())
    monkeypatch.setattr(weekly.db, "is_configured", lambda: True)
    assert "SECRETARY_WEBHOOK_URL" in weekly._guard()


def test_weekly_says_so_when_the_week_was_too_quiet(monkeypatch):
    """A review built on five signals must not read like a finding."""
    from iblu_keeper.jobs import weekly

    monkeypatch.setattr(
        weekly.review_tools, "review",
        lambda w: {
            "coverage": {"signals": 5, "days_with_signals": 1, "days_in_window": 7},
            "by_venture": [], "by_work_type": [],
            "inbound": {"share_pct": 0, "signals_in_threads_i_did_not_start": 0},
            "recurring": [],
            "pings": {"sent": 0, "answered": 0, "answer_rate_pct": None, "target_pct": 80},
            "since": "2026-09-06T00:00:00+00:00",
        },
    )
    text, _ = weekly.compose_review("7d")
    assert "too little to draw a conclusion" in text

    monkeypatch.setattr(
        weekly.review_tools, "review",
        lambda w: {
            "coverage": {"signals": 400, "days_with_signals": 6, "days_in_window": 7},
            "by_venture": [{"key": "blt", "signals": 400, "share_pct": 100}],
            "by_work_type": [], "recurring": [],
            "inbound": {"share_pct": 10, "signals_in_threads_i_did_not_start": 40},
            "pings": {"sent": 10, "answered": 9, "answer_rate_pct": 90, "target_pct": 80},
            "since": "2026-09-06T00:00:00+00:00",
        },
    )
    text, _ = weekly.compose_review("7d")
    assert "too little to draw a conclusion" not in text


def test_calendar_signals_are_never_clustered_as_threads(monkeypatch):
    """All calendar signals share container='primary'.

    Clustering them would merge every unrelated event into one bogus "thread"
    named after whichever happened first — which is exactly what it did: seven
    separate diary changes were reported as "IBLU recorder self-test — 7x".
    """
    from datetime import datetime, timezone

    class _Live:
        use_mock = False
        dry_run = False

    monkeypatch.setattr(review, "settings", _Live())
    now = datetime.now(timezone.utc)

    calendar_rows = [
        {"id": i, "source": "calendar", "occurred_at": now, "counterpart": None,
         "container": "primary", "subject": f"Event {i}", "initiator": None,
         "venture": "blt", "work_type": None, "project": None}
        for i in range(7)
    ]

    class _Conn:
        def execute(self, sql, params=None):
            class _R:
                @staticmethod
                def fetchall():
                    return calendar_rows if "FROM signals" in sql else []
            return _R()

    from contextlib import contextmanager

    @contextmanager
    def _conn():
        yield _Conn()

    monkeypatch.setattr(review.db, "get_conn", _conn)
    out = review.review("7d")

    assert out["top_threads"] == [], "calendar changes must not appear as threads"
    assert out["recurring"] == [], "a diary change is not something 'touched repeatedly'"
    # but they are still counted
    assert out["by_source"] == [{"key": "calendar", "signals": 7, "share_pct": 100}]
    assert out["coverage"]["signals"] == 7
