"""The log of what IBLU noticed was wrong with itself.

Two properties matter more than the rest and are tested first: recording an
observation must never break the thing being observed, and a rule's finding
must never be confusable with a model's suspicion.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from iblu_keeper.store import observations as obs

UTC = timezone.utc


class _FakeConn:
    """Records the SQL it was given and returns canned rows."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.sql: list[str] = []
        self.args: list[tuple] = []

    def execute(self, sql, args=()):
        self.sql.append(" ".join(sql.split()))
        self.args.append(args)
        rows = self.rows

        class _Cur:
            def fetchone(self_inner):
                return rows[0] if rows else None

            def fetchall(self_inner):
                return rows

        return _Cur()


# --- the two properties that matter ---------------------------------------


def test_recording_never_raises_even_with_no_database(monkeypatch):
    """A watchdog that can take down what it watches is worse than none."""

    class _Live:
        use_mock = False

    monkeypatch.setattr(obs, "logger", obs.logger)
    monkeypatch.setattr("iblu_keeper.config.settings", _Live(), raising=False)
    import iblu_keeper.db as db

    monkeypatch.setattr(db, "is_configured", lambda: False)
    assert obs.record_safe(source="tick", kind="x", summary="y") is None


def test_recording_swallows_a_broken_database(monkeypatch):
    import iblu_keeper.db as db

    monkeypatch.setattr(db, "is_configured", lambda: True)

    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db, "get_conn", _boom)
    assert obs.record_safe(source="tick", kind="x", summary="y") is None


def test_a_rule_and_a_model_are_different_witnesses():
    for bad in ("guess", "human", "", None):
        with pytest.raises(ValueError, match="detected_by"):
            obs.record(_FakeConn([{"id": 1}]), source="tick", kind="k",
                       summary="s", detected_by=bad)


def test_an_unknown_severity_is_refused():
    with pytest.raises(ValueError, match="severity"):
        obs.record(_FakeConn([{"id": 1}]), source="tick", kind="k",
                   summary="s", severity="catastrophic")


# --- deduplication ---------------------------------------------------------


def test_the_same_problem_bumps_rather_than_duplicating():
    conn = _FakeConn([{"id": 7}])
    obs.record(conn, source="tick", kind="k", summary="s")
    sql = conn.sql[0]
    assert "ON CONFLICT (fingerprint) WHERE status <> 'resolved'" in sql
    assert "occurrences = observations.occurrences + 1" in sql


def test_a_fingerprint_ignores_the_wording():
    """The same problem described differently is the same problem."""
    a = obs.fingerprint("sensecheck", "llm", "thin_evidence", date(2026, 9, 13))
    b = obs.fingerprint("sensecheck", "llm", "thin_evidence", date(2026, 9, 13))
    c = obs.fingerprint("sensecheck", "llm", "thin_evidence", date(2026, 9, 12))
    assert a == b and a != c


def test_a_fingerprint_survives_a_none():
    assert obs.fingerprint("a", None, "b") == obs.fingerprint("a", None, "b")


# --- resolving -------------------------------------------------------------


def test_resolving_never_deletes():
    conn = _FakeConn([{"id": 3}])
    assert obs.resolve(conn, 3, "fixed in 1dd5a80") is True
    assert "UPDATE observations" in conn.sql[0]
    assert "DELETE" not in conn.sql[0]


def test_resolving_an_already_resolved_row_changes_nothing():
    assert obs.resolve(_FakeConn([]), 3, "again") is False


# --- the document a later session reads ------------------------------------


def _row(**over):
    base = {
        "id": 1, "first_seen_at": datetime(2026, 9, 13, tzinfo=UTC),
        "last_seen_at": datetime(2026, 9, 13, 12, tzinfo=UTC), "occurrences": 1,
        "source": "sensecheck", "kind": "thin_evidence", "severity": "warn",
        "detected_by": "llm", "summary": "a block labelled from very little",
        "detail": "why", "evidence": {"date": "2026-09-13"},
    }
    base.update(over)
    return base


def test_the_document_distinguishes_a_fact_from_a_lead():
    text = obs.as_markdown([_row(detected_by="llm")])
    assert "never a conclusion to act on blindly" in text
    assert "llm" in text


def test_an_empty_document_does_not_claim_everything_works():
    text = obs.as_markdown([])
    assert "nothing has run yet" in text


def test_a_repeat_says_how_often():
    text = obs.as_markdown([_row(occurrences=14)])
    assert "seen 14x" in text


def test_the_document_says_how_to_close_something():
    assert "--resolve" in obs.as_markdown([_row()])


# --- leads that stop recurring stop being shown (2026-09-15) --------------


def test_an_unrepeated_llm_lead_ages_out():
    conn = _FakeConn([{"id": 1}, {"id": 2}])
    assert obs.age_out_llm_leads(conn) == 2
    sql = conn.sql[0]
    assert "detected_by = 'llm'" in sql
    assert "severity <> 'error'" in sql
    assert "status = 'resolved'" in sql
    assert "DELETE" not in sql


def test_ageing_out_says_not_reproduced_rather_than_fixed():
    """Nobody checked. That is the honest claim, and it is why this only
    applies to leads and never to rule findings."""
    conn = _FakeConn([{"id": 1}])
    obs.age_out_llm_leads(conn)
    resolution = conn.args[0][0]
    assert "not reproduced" in resolution
    assert "fixed" not in resolution.lower()


def test_an_error_never_ages_out():
    """An error means IBLU is not recording. It goes away when it is fixed."""
    conn = _FakeConn([])
    obs.age_out_llm_leads(conn)
    assert "severity <> 'error'" in conn.sql[0]
