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
    # Narrowed 2026-09-13: the reconstructed day makes minutes a real
    # measurement, so `out["minutes"]` is legitimate — but ONLY there, and only
    # with its own disclaimer. Counts must still never be dressed as time.
    forbidden = {"hours", "duration", "time_spent", "elapsed"}
    def _keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from _keys(v)
        elif isinstance(node, list):
            for item in node:
                yield from _keys(item)

    counts_only = {k: v for k, v in out.items() if k != "minutes"}
    keys = set(_keys(counts_only))
    assert not (forbidden & keys), "a field implies measured time"
    assert "minutes" not in keys, "counts must not be reported as minutes"
    assert "not hours" in out["coverage"]["note"]
    if out.get("minutes"):
        # Whatever else it says, it must say where the number came from.
        assert "not a clock" in out["minutes"]["note"]
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


# --- gains / one_removal (plan §1.7) ---------------------------------------


def test_gains_reads_all_three_kinds_and_merges_tapped_entries(monkeypatch):
    """learned <- decisions/corrections, progressed <- fact blocks + threads
    gone quiet, experienced <- lived family/personal blocks — plus whatever
    Session 8 has already tapped into `gain`-tagged entries, if any exist."""
    from contextlib import contextmanager
    from datetime import date, datetime, timezone

    class _Live:
        use_mock = False
        dry_run = False

    monkeypatch.setattr(review, "settings", _Live())

    truth = {
        "since": "2026-09-07T00:00:00+00:00",
        "until": "2026-09-11T18:00:00+00:00",
        "recurring": [
            {"name": "Old thread", "counterpart": "Bella", "signals": 4, "venture": "blt",
             "started_by": "other", "first": "2026-09-07T09:00:00+00:00",
             "last": "2026-09-07T10:00:00+00:00"},
        ],
    }

    tapped_row = {
        "content": "tapped gain", "meta": {"kind": "experienced"},
        "occurred_at": None, "created_at": datetime(2026, 9, 8, tzinfo=timezone.utc),
        "venture": "family",
    }
    decision_row = {
        "content": "Decided to switch accountant", "occurred_at": None,
        "created_at": datetime(2026, 9, 8, tzinfo=timezone.utc), "venture": "personal",
    }
    fact_block_row = {
        "local_date": date(2026, 9, 9), "venture": "deadlift",
        "reasoning": "45 min · 3 gmail · during \"Machina\"",
    }
    lived_block_row = {
        "local_date": date(2026, 9, 9), "venture": "family",
        "reasoning": "60 min · present with Leo",
    }

    class _Conn:
        def execute(self, sql, params=None):
            s = " ".join(sql.split())

            class _R:
                @staticmethod
                def fetchall():
                    if "'gain' = ANY(tags)" in s:
                        return [tapped_row]
                    if "type IN ('decision', 'correction')" in s:
                        return [decision_row]
                    if "confidence = 'fact'" in s:
                        return [fact_block_row]
                    if "venture IN ('family', 'personal')" in s:
                        return [lived_block_row]
                    return []
            return _R()

    @contextmanager
    def _conn():
        yield _Conn()

    monkeypatch.setattr(review.db, "get_conn", _conn)

    out = review.gains(truth)

    assert {"date": "2026-09-08", "text": "Decided to switch accountant",
            "venture": "personal", "source": "decision"} in out["learned"]
    assert any(i["source"] == "block:fact" for i in out["progressed"])
    # the recurring thread went quiet >=2 days before `until` -> likely closed
    assert any(i["source"] == "thread:likely_closed" for i in out["progressed"])
    assert any(i["source"] == "tapped" for i in out["experienced"])
    assert any(i["source"] == "block:present" for i in out["experienced"])


def test_gains_is_inert_in_mock_mode(monkeypatch):
    class _Mock:
        use_mock = True

    monkeypatch.setattr(review, "settings", _Mock())

    def boom(*_a, **_k):
        raise AssertionError("mock mode must never open a database connection")

    monkeypatch.setattr(review.db, "get_conn", boom)
    out = review.gains({"since": "2026-09-07T00:00:00+00:00", "until": "2026-09-11T18:00:00+00:00"})
    assert out["learned"] == out["progressed"] == out["experienced"] == []


def test_one_removal_prefers_a_named_person_over_automation():
    truth = {
        "recurring": [
            {"name": "Womanizer gifting", "counterpart": "Bella", "signals": 5,
             "venture": "blt", "started_by": "other"},
        ],
    }
    out = review.one_removal(truth)
    assert out == {
        "available": True, "kind": "person", "project": "Womanizer gifting",
        "step": "hand the Womanizer gifting thread to Bella",
    }


def test_one_removal_falls_back_to_automation_without_a_counterpart():
    truth = {
        "recurring": [
            {"name": "Scout scripts", "counterpart": None, "signals": 6,
             "venture": "blt", "started_by": "other"},
        ],
    }
    out = review.one_removal(truth)
    assert out["kind"] == "automation"
    assert out["project"] == "Scout scripts"


def test_one_removal_names_nothing_when_evidence_is_thin():
    out = review.one_removal({"recurring": []})
    assert out["available"] is False
    assert "insufficient evidence" in out["reason"]


def test_one_removal_prefers_threads_ignas_did_not_start():
    """A thread he started himself is not the 'someone else's' delegation
    candidate the mission is looking for — a thread others started, even
    with fewer touches, is a better candidate for handing off."""
    truth = {
        "recurring": [
            {"name": "His own thread", "counterpart": "Marko", "signals": 9,
             "venture": "blt", "started_by": "me"},
            {"name": "Their thread", "counterpart": "Diana", "signals": 3,
             "venture": "choco", "started_by": "other"},
        ],
    }
    out = review.one_removal(truth)
    assert out["project"] == "Their thread"


def test_stage_truth_is_none_in_mock_mode(monkeypatch):
    class _Mock:
        use_mock = True

    monkeypatch.setattr(review, "settings", _Mock())
    from datetime import datetime, timezone

    assert review.stage_truth(datetime.now(timezone.utc), datetime.now(timezone.utc)) is None


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


class _NoLLM:
    """Settings stub that guarantees `compose_gains_llm` bails out before ever
    building an Anthropic client — the box this suite runs on has a real
    `ANTHROPIC_API_KEY` in `.env` (it's a live personal assistant), so any
    test exercising `weekly.compose_review` must neutralise it explicitly or
    risk a genuine network call."""

    anthropic_api_key = ""
    iblu_timezone = "Europe/Zagreb"


def test_weekly_says_so_when_the_week_was_too_quiet(monkeypatch):
    """A review built on five signals must not read like a finding."""
    from iblu_keeper.jobs import weekly

    monkeypatch.setattr(weekly, "settings", _NoLLM())
    monkeypatch.setattr(
        weekly.review_tools, "review",
        lambda w, **_kw: {
            "coverage": {"signals": 5, "days_with_signals": 1, "days_in_window": 7},
            "by_venture": [], "by_work_type": [],
            "inbound": {"share_pct": 0, "signals_in_threads_i_did_not_start": 0},
            "recurring": [],
            "pings": {"sent": 0, "answered": 0, "answer_rate_pct": None, "target_pct": 80},
            "since": "2026-09-06T00:00:00+00:00",
            "until": "2026-09-13T00:00:00+00:00",
        },
    )
    text, _ = weekly.compose_review("7d")
    assert "too little to draw a conclusion" in text

    monkeypatch.setattr(
        weekly.review_tools, "review",
        lambda w, **_kw: {
            "coverage": {"signals": 400, "days_with_signals": 6, "days_in_window": 7},
            "by_venture": [{"key": "blt", "signals": 400, "share_pct": 100}],
            "by_work_type": [], "recurring": [],
            "inbound": {"share_pct": 10, "signals_in_threads_i_did_not_start": 40},
            "pings": {"sent": 10, "answered": 9, "answer_rate_pct": 90, "target_pct": 80},
            "since": "2026-09-06T00:00:00+00:00",
            "until": "2026-09-13T00:00:00+00:00",
        },
    )
    text, _ = weekly.compose_review("7d")
    assert "too little to draw a conclusion" not in text


def test_weekly_review_sections_are_gains_truth_removal_in_order(monkeypatch):
    """Plan §1.7: the ordering is binding — gains come first, always."""
    from iblu_keeper.jobs import weekly

    monkeypatch.setattr(weekly, "settings", _NoLLM())
    monkeypatch.setattr(
        weekly.review_tools, "review",
        lambda w, **_kw: {
            "coverage": {"signals": 50, "days_with_signals": 5, "days_in_window": 5},
            "untracked": {"share_pct": 0, "note": ""},
            "by_venture": [{"key": "blt", "signals": 50, "share_pct": 100}],
            "by_work_type": [],
            "inbound": {"share_pct": 20, "signals_in_threads_i_did_not_start": 10},
            "recurring": [
                {"name": "Ante contract", "counterpart": "Ante", "signals": 4,
                 "venture": "blt", "started_by": "other",
                 "first": "2026-09-08T09:00:00+00:00", "last": "2026-09-08T10:00:00+00:00"},
            ],
            "pings": {"sent": 8, "answered": 7, "answer_rate_pct": 88, "target_pct": 80},
            "since": "2026-09-07T00:00:00+00:00",
            "until": "2026-09-11T18:00:00+00:00",
        },
    )
    text, data = weekly.compose_review("7d")

    i_gains = text.index("## Gains")
    i_truth = text.index("## Truth")
    i_removal = text.index("## One removal")
    assert i_gains < i_truth < i_removal, "gains must come first, always"
    # The deterministic template must itself pass the language gate.
    from iblu_keeper.jobs.review_language import validate_language
    assert validate_language(text) == []


def test_weekly_review_names_exactly_one_removal(monkeypatch):
    """§1.6/§1.7: one project, one step. Never a list."""
    from iblu_keeper.jobs import weekly

    monkeypatch.setattr(weekly, "settings", _NoLLM())
    monkeypatch.setattr(
        weekly.review_tools, "review",
        lambda w, **_kw: {
            "coverage": {"signals": 30, "days_with_signals": 4, "days_in_window": 5},
            "untracked": {"share_pct": 20, "note": ""},
            "by_venture": [], "by_work_type": [],
            "inbound": {"share_pct": 0, "signals_in_threads_i_did_not_start": 0},
            "recurring": [
                {"name": "Womanizer gifting", "counterpart": "Bella", "signals": 5,
                 "venture": "blt", "started_by": "other",
                 "first": "2026-09-08T09:00:00+00:00", "last": "2026-09-08T10:00:00+00:00"},
                {"name": "Noshinku gifting", "counterpart": "Zara", "signals": 3,
                 "venture": "blt", "started_by": "other",
                 "first": "2026-09-09T09:00:00+00:00", "last": "2026-09-09T10:00:00+00:00"},
            ],
            "pings": {"sent": 0, "answered": 0, "answer_rate_pct": None, "target_pct": 80},
            "since": "2026-09-07T00:00:00+00:00",
            "until": "2026-09-11T18:00:00+00:00",
        },
    )
    text, data = weekly.compose_review("7d")

    removal_section = text.split("## One removal", 1)[1]
    # Exactly one candidate is named — the second recurring thread never
    # appears in the removal section at all.
    assert "Womanizer gifting" in removal_section
    assert "Noshinku gifting" not in removal_section
    assert data["removal"]["available"] is True
    assert data["removal"]["project"] == "Womanizer gifting"


def test_weekly_review_names_no_removal_when_evidence_is_thin(monkeypatch):
    """Never invent a removal just to have one to show."""
    from iblu_keeper.jobs import weekly

    monkeypatch.setattr(weekly, "settings", _NoLLM())
    monkeypatch.setattr(
        weekly.review_tools, "review",
        lambda w, **_kw: {
            "coverage": {"signals": 12, "days_with_signals": 3, "days_in_window": 5},
            "untracked": {"share_pct": 40, "note": ""},
            "by_venture": [], "by_work_type": [],
            "inbound": {"share_pct": 0, "signals_in_threads_i_did_not_start": 0},
            "recurring": [],
            "pings": {"sent": 0, "answered": 0, "answer_rate_pct": None, "target_pct": 80},
            "since": "2026-09-07T00:00:00+00:00",
            "until": "2026-09-11T18:00:00+00:00",
        },
    )
    text, data = weekly.compose_review("7d")

    assert data["removal"]["available"] is False
    removal_section = text.split("## One removal", 1)[1].strip()
    assert "insufficient evidence" in removal_section.lower()


def test_weekly_review_gains_llm_failure_falls_back_to_deterministic_lines(monkeypatch, caplog):
    """No key configured -> compose_gains_llm bails before any network call;
    the section must still render from the raw evidence, not go missing."""
    import logging as _logging
    from iblu_keeper.jobs import weekly

    monkeypatch.setattr(weekly, "settings", _NoLLM())

    truth = {
        "coverage": {"signals": 20, "days_with_signals": 4, "days_in_window": 5},
        "untracked": {"share_pct": 0, "note": ""},
        "by_venture": [], "by_work_type": [],
        "inbound": {"share_pct": 0, "signals_in_threads_i_did_not_start": 0},
        "recurring": [],
        "pings": {"sent": 0, "answered": 0, "answer_rate_pct": None, "target_pct": 80},
        "since": "2026-09-07T00:00:00+00:00",
        "until": "2026-09-11T18:00:00+00:00",
    }
    monkeypatch.setattr(weekly.review_tools, "review", lambda w, **_kw: truth)
    monkeypatch.setattr(
        weekly.review_tools, "gains",
        lambda t: {"learned": [{"date": "2026-09-08", "text": "decision logged", "venture": "blt"}],
                   "progressed": [], "experienced": []},
    )

    with caplog.at_level(_logging.WARNING):
        text, _ = weekly.compose_review("7d")

    assert "decision logged" in text
    assert "ANTHROPIC_API_KEY is not set" in caplog.text


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
                    if "actor = 'me'" in sql:
                        return calendar_rows
                    return []
                @staticmethod
                def fetchone():
                    return {"n": 0}
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


def test_group_demand_never_enters_the_attention_split(monkeypatch):
    """Other people's mail landing on a group Ignas works is demand, not his
    attention. Counting it would inflate the venture split with work he did not
    do — the exact flattery the mission forbids."""
    from datetime import datetime, timezone

    class _Live:
        use_mock = False
        dry_run = False

    monkeypatch.setattr(review, "settings", _Live())
    now = datetime.now(timezone.utc)
    seen: list[str] = []

    mine = [{
        "id": 1, "source": "gmail", "occurred_at": now, "counterpart": "a@b.com",
        "container": "t1", "subject": "mine", "initiator": "me",
        "venture": "blt", "work_type": None, "project": None, "actor": "me",
    }]

    class _Conn:
        def execute(self, sql, params=None):
            seen.append(" ".join(sql.split()))
            class _R:
                @staticmethod
                def fetchall():
                    if "actor = 'other'" in sql:
                        return [{"venture": "choco", "counterpart": "Diana", "n": 31}]
                    return mine if "FROM signals" in sql else []
                @staticmethod
                def fetchone():
                    return {"n": 31}
            return _R()

    from contextlib import contextmanager

    @contextmanager
    def _conn():
        yield _Conn()

    monkeypatch.setattr(review.db, "get_conn", _conn)
    out = review.review("7d")

    # the signals query must be filtered to actor='me'
    assert any("actor = 'me'" in q for q in seen), "attention query is not filtered"
    assert out["coverage"]["signals"] == 1, "demand must not be counted as attention"
    assert out["by_venture"] == [{"key": "blt", "signals": 1, "share_pct": 100}]
    # but the demand is reported
    assert out["inbound_demand"]["signals"] == 31
    assert out["inbound_demand"]["top_senders"][0]["who"] == "Diana"
    assert "Demand, not your attention" in review.as_markdown(out)


# --- the one removal names a person, or admits there isn't one -------------


def test_a_chat_space_is_not_a_person_to_hand_work_to():
    """The naive read produced "hand the Email Writer thread to Email Writer"."""
    from iblu_keeper.tools import review

    truth = {"recurring": [
        {"name": "Email Writer", "counterpart": "Email Writer",
         "signals": 18, "started_by": "other"},
    ]}
    out = review.one_removal(truth)
    assert out["kind"] == "automation"


def test_an_unnamed_dm_partner_is_not_a_person_to_hand_work_to():
    from iblu_keeper.tools import review

    truth = {"recurring": [
        {"name": "a DM", "counterpart": "users/106454719402311288628",
         "signals": 4, "started_by": "other"},
    ]}
    assert review.one_removal(truth)["kind"] == "automation"


def test_a_real_counterpart_is_named():
    from iblu_keeper.tools import review

    truth = {"recurring": [
        {"name": "Opera renewals", "counterpart": "Ante Cetinic",
         "signals": 9, "started_by": "other"},
    ]}
    out = review.one_removal(truth)
    assert out["kind"] == "person" and "Ante Cetinic" in out["step"]


def test_exactly_one_removal_is_ever_named():
    from iblu_keeper.tools import review

    truth = {"recurring": [
        {"name": f"thread {i}", "counterpart": f"Person {i}",
         "signals": 5, "started_by": "other"} for i in range(5)
    ]}
    out = review.one_removal(truth)
    assert out["available"] and isinstance(out["step"], str)
    assert "project" in out and isinstance(out["project"], str)


# --- the week in minutes (plan §3.4) --------------------------------------


class _BlocksConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        rows = self._rows

        class _Cur:
            def fetchall(self):
                return rows

        return _Cur()


def _block(minutes, venture="blt", work_type="build", attention="present",
           confidence="inferred"):
    return {
        "venture": venture, "work_type": work_type, "attention": attention,
        "confidence": confidence, "intent_title": None, "minutes": minutes,
    }


def test_minutes_are_none_until_a_day_has_been_reconstructed():
    """A confident zero would be a lie; None makes the caller fall back."""
    from datetime import datetime, timezone
    from iblu_keeper.tools import review

    now = datetime.now(timezone.utc)
    assert review.minutes_from_blocks(_BlocksConn([]), now, now) is None


def test_unattributed_minutes_are_untracked_not_folded_into_a_venture():
    from datetime import datetime, timezone
    from iblu_keeper.tools import review

    now = datetime.now(timezone.utc)
    out = review.minutes_from_blocks(
        _BlocksConn([
            _block(60, venture="blt"),
            _block(180, venture=None, work_type=None, attention="ambiguous"),
        ]),
        now, now,
    )
    assert out["untracked"]["minutes"] == 180
    assert out["untracked"]["share_pct"] == 75
    assert [b["key"] for b in out["by_venture"]] == ["blt"]


def test_confirmed_and_guessed_minutes_are_never_added_together_silently():
    from datetime import datetime, timezone
    from iblu_keeper.tools import review

    now = datetime.now(timezone.utc)
    out = review.minutes_from_blocks(
        _BlocksConn([_block(60, confidence="fact"), _block(60, confidence="inferred")]),
        now, now,
    )
    split = {b["key"]: b["share_pct"] for b in out["by_confidence"]}
    assert split == {"fact": 50, "inferred": 50}


def test_displaced_minutes_are_reported_on_their_own():
    from datetime import datetime, timezone
    from iblu_keeper.tools import review

    now = datetime.now(timezone.utc)
    out = review.minutes_from_blocks(
        _BlocksConn([_block(60), _block(30, attention="displaced")]), now, now
    )
    assert out["displaced"]["minutes"] == 30


def test_minutes_tolerate_the_decimal_postgres_returns():
    """EXTRACT() comes back as Decimal; mixing it with float raised a TypeError."""
    from decimal import Decimal
    from datetime import datetime, timezone
    from iblu_keeper.tools import review

    now = datetime.now(timezone.utc)
    out = review.minutes_from_blocks(_BlocksConn([_block(Decimal("60"))]), now, now)
    assert out["total_minutes"] == 60
