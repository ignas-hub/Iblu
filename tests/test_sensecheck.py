"""IBLU checking its own work.

The rules pass must catch things that cannot be true; the LLM pass must not
cry wolf. Both are tested against fakes — no database, no network.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from iblu_keeper.analyst import sensecheck as S

UTC = timezone.utc
DAY = date(2026, 9, 15)


class _Conn:
    """Answers each query by matching a fragment of its SQL."""

    def __init__(self, answers: dict[str, list]):
        self.answers = answers

    def execute(self, sql, args=()):
        flat = " ".join(sql.split())
        rows = []
        for fragment, value in self.answers.items():
            if fragment in flat:
                rows = value
                break

        class _Cur:
            def fetchone(self_inner):
                return rows[0] if rows else None

            def fetchall(self_inner):
                return rows

        return _Cur()


def _kinds(found):
    return {f["kind"] for f in found}


def test_a_clean_day_produces_nothing():
    """An empty list is the correct answer most days."""
    found = S.run_rules(_Conn({}), DAY)
    assert found == []


def test_overlapping_blocks_are_an_error_not_a_warning():
    found = S.run_rules(
        _Conn({"a.starts_at < b.ends_at": [{"a": 1, "b": 2, "starts_at": None, "ends_at": None}]}),
        DAY,
    )
    [f] = [f for f in found if f["kind"] == "blocks_overlap"]
    assert f["severity"] == "error"
    assert f["detected_by"] == "rule"


def test_a_confirmed_block_replaced_by_a_guess_is_an_error():
    """A tap is truth. If a reconstruction superseded one, something regressed."""
    found = S.run_rules(
        _Conn({"confidence = 'fact' AND superseded_by IS NOT NULL": [{"id": 9}]}), DAY
    )
    [f] = [f for f in found if f["kind"] == "fact_block_superseded"]
    assert f["severity"] == "error"


def test_a_mostly_unknown_day_is_flagged_but_not_called_an_error():
    """Unobserved time is a real thing; it is worth checking, not alarming."""
    found = S.run_rules(
        _Conn({"sum(EXTRACT(EPOCH": [{"total": 600, "unknown": 590}]}), DAY
    )
    [f] = [f for f in found if f["kind"] == "day_almost_entirely_unknown"]
    assert f["severity"] == "warn"
    assert "silence is never presence" in f["detail"]


def test_a_merely_quiet_day_is_not_flagged():
    found = S.run_rules(
        _Conn({"sum(EXTRACT(EPOCH": [{"total": 600, "unknown": 400}]}), DAY
    )
    assert "day_almost_entirely_unknown" not in _kinds(found)


def test_a_collector_error_is_reported_with_its_own_fingerprint():
    conn = _Conn({"SELECT name, watermark, last_run_at, last_error FROM collector_state": [
        {"name": "gmail_sent:choco", "watermark": None, "last_run_at": None,
         "last_error": "invalid_grant"},
    ]})
    [f] = [f for f in S.run_rules(conn, DAY) if f["kind"] == "collector_error"]
    assert "gmail_sent:choco" in f["summary"]
    assert f["severity"] == "error"


def test_a_watermark_that_stopped_moving_is_flagged():
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    conn = _Conn({"SELECT name, watermark, last_run_at, last_error FROM collector_state": [
        {"name": "chat_sent", "watermark": now - timedelta(days=5),
         "last_run_at": now, "last_error": None},
    ]})
    [f] = [f for f in S.run_rules(conn, DAY) if f["kind"] == "watermark_not_advancing"]
    assert "5 days behind" in f["summary"]


def test_a_weekend_gap_is_not_a_stalled_watermark():
    """The threshold is longer than a weekend so Monday is not a false alarm."""
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    conn = _Conn({"SELECT name, watermark, last_run_at, last_error FROM collector_state": [
        {"name": "chat_sent", "watermark": now - timedelta(hours=60),
         "last_run_at": now, "last_error": None},
    ]})
    assert "watermark_not_advancing" not in _kinds(S.run_rules(conn, DAY))


# --- the LLM pass's guard rails -------------------------------------------


def test_an_invented_kind_becomes_other_rather_than_its_own_bucket():
    """Free-text kinds meant the same problem, reworded, opened a new row."""
    import json

    payload = {"findings": [
        {"kind": "some_new_slug_it_made_up", "severity": "warn",
         "summary": "something", "detail": "because"},
    ]}
    out = _parse(payload)
    assert out[0]["kind"] == "other"


def test_a_known_kind_is_kept():
    out = _parse({"findings": [
        {"kind": "thin_evidence", "severity": "warn", "summary": "s", "detail": "d"},
    ]})
    assert out[0]["kind"] == "thin_evidence"


def test_an_llm_finding_is_never_labelled_as_a_rule():
    out = _parse({"findings": [{"kind": "other", "summary": "s"}]})
    assert out[0]["detected_by"] == "llm"


def test_an_unknown_severity_falls_back_to_info():
    out = _parse({"findings": [{"kind": "other", "summary": "s", "severity": "critical"}]})
    assert out[0]["severity"] == "info"


def test_a_finding_with_no_summary_is_dropped():
    out = _parse({"findings": [{"kind": "other", "summary": "  "}, {"kind": "other", "summary": "ok"}]})
    assert len(out) == 1


def test_at_most_five_findings_are_kept():
    out = _parse({"findings": [{"kind": "other", "summary": f"s{i}"} for i in range(20)]})
    assert len(out) == 5


def _parse(payload):
    """Drive `run_llm`'s post-processing without the API call."""
    import json
    from unittest.mock import MagicMock

    import iblu_keeper.analyst.sensecheck as mod

    class _Key:
        anthropic_api_key = "sk-test"
        iblu_check_model = "claude-opus-5"
        iblu_timezone = "Europe/Zagreb"

    block = MagicMock()
    block.type = "text"
    block.text = json.dumps(payload)
    response = MagicMock()
    response.content = [block]

    real_settings = mod.settings
    mod.settings = _Key()
    try:
        import sys
        import types

        fake = types.ModuleType("anthropic")
        fake.Anthropic = lambda **_: MagicMock(
            messages=MagicMock(create=MagicMock(return_value=response))
        )
        sys.modules["anthropic"] = fake
        mod._snapshot = lambda conn, on: "snapshot"
        return mod.run_llm(_Conn({}), DAY)
    finally:
        mod.settings = real_settings


def test_a_calendar_block_labelled_without_evidence_is_an_error():
    """"Go pickup Emory" became 420 minutes of blt/client once."""
    conn = _Conn({"jsonb_array_length(evidence) = 0": [
        {"id": 7, "starts_at": None, "venture": "blt", "work_type": "client",
         "project": None, "intent_title": "Go pickup Emory"},
    ]})
    [f] = [f for f in S.run_rules(conn, DAY) if f["kind"] == "labelled_without_evidence"]
    assert f["severity"] == "error"
    assert f["detected_by"] == "rule"
