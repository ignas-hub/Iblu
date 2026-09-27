"""python -m iblu_keeper.jobs.audit — measuring the artifacts, not the functions.

Every invariant here fires on a fake and stays quiet on a fake — the same
discipline `test_sensecheck.py` and `test_watchdog.py` use, for the same
reason: the whole point of this job is that the 746-function-level tests can
be green while the artifact is wrong. DB-backed checks (the trickier
`jsonb_array_elements_text` lateral joins) additionally run against a real
Postgres inside a rolled-back transaction, skipped without `DATABASE_URL`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from iblu_keeper.jobs import audit as A

UTC = timezone.utc
DAY = date(2026, 9, 15)


class _Conn:
    """Answers each query by matching a fragment of its SQL — see
    `test_sensecheck.py`'s `_Conn` for the same pattern."""

    def __init__(self, answers: dict[str, list] | None = None):
        self.answers = answers or {}
        self.executed: list[str] = []

    def execute(self, sql, args=()):
        flat = " ".join(sql.split())
        self.executed.append(flat)
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

    def rollback(self):
        pass


def _kinds(findings):
    return {f["kind"] for f in findings}


# ---------------------------------------------------------------------------
# 1. structural invariants
# ---------------------------------------------------------------------------


def test_a_clean_window_produces_nothing():
    assert A.audit_blocks(_Conn(), DAY)["findings"] == []


def test_overlapping_blocks_are_an_error():
    conn = _Conn({"a.starts_at < b.ends_at": [{"a_id": 1, "b_id": 2, "local_date": DAY}]})
    [f] = A._check_overlaps(conn, DAY)
    assert f["kind"] == "blocks_overlap" and f["severity"] == "error"
    assert str(DAY) in f["summary"]


def test_no_overlap_is_silent():
    assert A._check_overlaps(_Conn(), DAY) == []


def test_a_block_below_the_floor_is_flagged():
    conn = _Conn({"interval '15 minutes'": [{"id": 9, "local_date": DAY}]})
    [f] = A._check_below_floor(conn, DAY)
    assert f["kind"] == "block_below_floor"
    assert f["evidence"]["block_ids"] == [9]


def test_no_sliver_is_silent():
    assert A._check_below_floor(_Conn(), DAY) == []


def test_a_block_straddling_midnight_is_flagged():
    # Filed under 2026-09-15 but actually runs 23:58 -> 00:15 local — the
    # exact bug `_clip_to_day` exists to prevent (HANDOFF §22).
    tz = A._tz()
    starts = datetime(2026, 9, 15, 23, 58, tzinfo=tz).astimezone(UTC)
    ends = datetime(2026, 9, 16, 0, 15, tzinfo=tz).astimezone(UTC)
    conn = _Conn({"FROM blocks": [{"id": 5, "local_date": DAY, "starts_at": starts, "ends_at": ends}]})
    [f] = A._check_date_mismatch(conn, DAY)
    assert f["kind"] == "block_date_mismatch"


def test_a_block_ending_exactly_at_local_midnight_is_legal():
    tz = A._tz()
    starts = datetime(2026, 9, 15, 23, 0, tzinfo=tz).astimezone(UTC)
    ends = datetime(2026, 9, 16, 0, 0, tzinfo=tz).astimezone(UTC)
    conn = _Conn({"FROM blocks": [{"id": 5, "local_date": DAY, "starts_at": starts, "ends_at": ends}]})
    assert A._check_date_mismatch(conn, DAY) == []


def test_a_block_entirely_inside_its_day_is_legal():
    tz = A._tz()
    starts = datetime(2026, 9, 15, 9, 0, tzinfo=tz).astimezone(UTC)
    ends = datetime(2026, 9, 15, 10, 0, tzinfo=tz).astimezone(UTC)
    conn = _Conn({"FROM blocks": [{"id": 5, "local_date": DAY, "starts_at": starts, "ends_at": ends}]})
    assert A._check_date_mismatch(conn, DAY) == []


def test_a_day_summing_past_its_own_wallclock_is_flagged():
    conn = _Conn({"sum(EXTRACT(EPOCH": [{"local_date": DAY, "minutes": 1500}]})
    [f] = A._check_day_exceeds_wallclock(conn, DAY)
    assert f["kind"] == "day_minutes_exceed_wallclock"
    assert f["severity"] == "error"


def test_a_day_within_its_wallclock_is_silent():
    conn = _Conn({"sum(EXTRACT(EPOCH": [{"local_date": DAY, "minutes": 900}]})
    assert A._check_day_exceeds_wallclock(conn, DAY) == []


def test_present_with_no_evidence_and_no_excuse_is_a_violation():
    conn = _Conn({
        "attention = 'present' AND jsonb_array_length(evidence) = 0": [
            {"id": 7, "local_date": DAY, "venture": "blt", "reasoning": "15 min · unexplained"},
        ],
        "source IN ('ping', 'human')": [],
    })
    [f] = A._check_present_without_evidence(conn, DAY)
    assert f["kind"] == "present_without_evidence" and f["severity"] == "error"


def test_the_family_inference_is_legal_regardless_of_its_reasoning_text():
    """The judge may rewrite the 'why' of any block (HANDOFF §18) — a real
    family-inference block was found with reasoning matching neither
    deterministic phrase at all, so legality must not depend on the text
    when venture='family'."""
    conn = _Conn({
        "attention = 'present' AND jsonb_array_length(evidence) = 0": [
            {"id": 7, "local_date": DAY, "venture": "family",
             "reasoning": "30 min · on the calendar with no work signals during it — inferred, not recorded"},
        ],
        "source IN ('ping', 'human')": [],
    })
    assert A._check_present_without_evidence(conn, DAY) == []


def test_a_continuation_slice_with_the_deterministic_text_is_legal():
    conn = _Conn({
        "attention = 'present' AND jsonb_array_length(evidence) = 0": [
            {"id": 7, "local_date": DAY, "venture": None, "reasoning": "15 min · part of the surrounding stretch"},
        ],
        "source IN ('ping', 'human')": [],
    })
    assert A._check_present_without_evidence(conn, DAY) == []


def test_a_continuation_slice_on_a_confirmed_day_is_legal_even_if_reworded():
    conn = _Conn({
        "attention = 'present' AND jsonb_array_length(evidence) = 0": [
            {"id": 7, "local_date": DAY, "venture": None, "reasoning": "15 min · adjacency to the preceding stretch"},
        ],
        "source IN ('ping', 'human')": [{"local_date": DAY}],
    })
    assert A._check_present_without_evidence(conn, DAY) == []


def test_evidence_from_actor_other_taints_a_block():
    conn = _Conn({
        "JOIN signals s ON s.id = e.sid::bigint": [
            {"block_id": 3, "local_date": DAY, "signal_id": 99},
        ],
    })
    [f] = A._check_evidence_tainted(conn, DAY)
    assert f["kind"] == "evidence_tainted" and f["severity"] == "error"
    assert f["evidence"]["block_ids"] == [3]


def test_no_tainted_evidence_is_silent():
    assert A._check_evidence_tainted(_Conn(), DAY) == []


def test_work_type_or_project_with_no_evidence_is_flagged():
    conn = _Conn({"work_type IS NOT NULL OR project IS NOT NULL": [{"id": 4, "local_date": DAY}]})
    [f] = A._check_labelled_without_evidence(conn, DAY)
    assert f["kind"] == "work_type_or_project_without_evidence"


def test_a_fact_block_with_only_inferred_evidence_is_flagged():
    conn = _Conn({"venture_confidence = 'fact'": [{"id": 8, "local_date": DAY}]})
    [f] = A._check_fact_without_fact_evidence(conn, DAY)
    assert f["kind"] == "fact_confidence_without_fact_evidence"


def test_a_stale_mirror_event_is_flagged():
    conn = _Conn({"calendar_event_id IS NOT NULL": [{"id": 2, "local_date": DAY}]})
    [f] = A._check_stale_mirror(conn, DAY)
    assert f["kind"] == "stale_mirror_event"


# ---------------------------------------------------------------------------
# 2. question audit
# ---------------------------------------------------------------------------


def _ping(id_, qid, text, *, options=None, covers_from=None, covers_to=None):
    return {
        "id": id_, "kind": "midday", "local_date": DAY,
        "covers_from": covers_from or datetime(2026, 9, 15, 8, 0, tzinfo=UTC),
        "covers_to": covers_to or datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        "window_start": datetime(2026, 9, 15, 10, 30, tzinfo=UTC),
        "window_end": datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
        "questions": [{"qid": qid, "text": text, "options": options or []}],
    }


def test_a_question_citing_only_other_signals_is_flagged():
    conn = _Conn({
        "FROM pings": [_ping(1, "sink", "PandaDoc thread — was that yours to do?",
                             options=[{"payload": {"signal_ids": [10]}}])],
        "FROM signals WHERE id = ANY": [{"id": 10, "occurred_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC), "actor": "other"}],
    })
    result = A.audit_questions(conn, DAY)
    assert "question_evidence_all_other" in _kinds(result["findings"])


def test_a_question_with_his_own_evidence_is_not_flagged():
    conn = _Conn({
        "FROM pings": [_ping(1, "sink", "Machina thread with Edo — was that yours to do?",
                             options=[{"payload": {"signal_ids": [10]}}])],
        "FROM signals WHERE id = ANY": [{"id": 10, "occurred_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC), "actor": "me"}],
    })
    result = A.audit_questions(conn, DAY)
    assert "question_evidence_all_other" not in _kinds(result["findings"])


def test_a_named_question_with_no_evidence_at_all_is_flagged():
    conn = _Conn({
        "FROM pings": [_ping(1, "sink", 'The Temu contract thread with Giedre — was that yours to do?')],
    })
    result = A.audit_questions(conn, DAY)
    assert "question_no_evidence_named_thread" in _kinds(result["findings"])


def test_the_generic_gains_card_is_never_flagged_for_missing_evidence():
    """gains/body_mind are fixed templates, never about a specific thread —
    an incidental capitalised word ('Iblu') must not trip this check."""
    conn = _Conn({
        "FROM pings": [_ping(1, "gains", "What actually moved today? Tap any that happened.")],
    })
    result = A.audit_questions(conn, DAY)
    assert "question_no_evidence_named_thread" not in _kinds(result["findings"])


def test_a_utc_time_that_is_not_the_local_time_of_anything_is_flagged():
    """The exact bug that reached him (HANDOFF §25): a signal at 15:10 local
    (13:10 UTC) written into the question text as the raw UTC clock reading,
    '13:10', instead of its local rendering."""
    conn = _Conn({
        "FROM pings": [_ping(1, "sink", "Opera thread — 13:10-13:41, was that yours to do?",
                             options=[{"payload": {"signal_ids": [10]}}])],
        "FROM signals WHERE id = ANY": [
            # 13:10 UTC == 15:10 Europe/Zagreb (DST) — the text's "13:10" is
            # the raw UTC reading, not this signal's local time.
            {"id": 10, "occurred_at": datetime(2026, 9, 15, 13, 10, tzinfo=UTC), "actor": "me"},
        ],
    })
    result = A.audit_questions(conn, DAY)
    [f] = [f for f in result["findings"] if f["kind"] == "question_time_mismatch"]
    assert "13:10" in f["evidence"]["bad_times"]
    assert "13:41" in f["evidence"]["bad_times"]


def test_a_time_that_matches_the_local_rendering_of_a_signal_is_legal():
    # 2026-09-15 11:10 UTC == 13:10 Europe/Zagreb (DST) — writing "13:10" is
    # correct, not a leak.
    conn = _Conn({
        "FROM pings": [_ping(1, "sink", "Opera thread — 13:10-13:41, was that yours to do?",
                             options=[{"payload": {"signal_ids": [10]}}])],
        "FROM signals WHERE id = ANY": [
            {"id": 10, "occurred_at": datetime(2026, 9, 15, 11, 10, tzinfo=UTC), "actor": "me"},
            {"id": 11, "occurred_at": datetime(2026, 9, 15, 11, 41, tzinfo=UTC), "actor": "me"},
        ],
    })
    result = A.audit_questions(conn, DAY)
    assert "question_time_mismatch" not in _kinds(result["findings"])


def test_a_time_matching_the_ping_window_bound_is_legal():
    conn = _Conn({
        "FROM pings": [_ping(
            1, "sink", "Nothing recorded between 10:00 and 12:00. What were you doing?",
            covers_from=datetime(2026, 9, 15, 8, 0, tzinfo=UTC),
            covers_to=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        )],
    })
    result = A.audit_questions(conn, DAY)
    assert "question_time_mismatch" not in _kinds(result["findings"])


def test_a_bare_qid_leaks_as_jargon():
    conn = _Conn({"FROM pings": [_ping(1, "sink", "Ante thread — 3 msgs. Biggest sink?")]})
    result = A.audit_questions(conn, DAY)
    [f] = [f for f in result["findings"] if f["kind"] == "question_internal_code_leak"]
    assert "sink" in f["evidence"]["codes"]


def test_a_hyphenated_project_code_leaks_bare():
    conn = _Conn({
        "FROM pings": [_ping(1, "sink", "docs: describes the page with email-writer — was that yours?")],
        "SELECT code FROM projects": [{"code": "email-writer"}],
    })
    result = A.audit_questions(conn, DAY)
    [f] = [f for f in result["findings"] if f["kind"] == "question_internal_code_leak"]
    assert "email-writer" in f["evidence"]["codes"]


def test_a_venture_code_like_blt_is_never_flagged():
    """BLT is Ignas's own shorthand for his company (used throughout HANDOFF.md)
    — flagging it would just be noise, not a real leak."""
    conn = _Conn({"FROM pings": [_ping(1, "sink", "BLT/leads automation dominated your morning.")]})
    result = A.audit_questions(conn, DAY)
    assert "question_internal_code_leak" not in _kinds(result["findings"])


def test_the_gain_kind_colon_prefix_leaks():
    conn = _Conn({"FROM pings": [_ping(1, "gains", "Experienced: time with personal")]})
    result = A.audit_questions(conn, DAY)
    [f] = [f for f in result["findings"] if f["kind"] == "question_internal_code_leak"]
    assert "experienced:" in f["evidence"]["codes"]


def test_answer_rate_and_correction_rate_are_computed():
    conn = _Conn({
        "FROM pings": [_ping(1, "sink", "Ante thread — was that yours to do?")],
        "FROM context_entries": [
            {"ping_id": "1", "qid": "sink", "n": 2, "any_superseded": True},
        ],
    })
    result = A.audit_questions(conn, DAY)
    assert result["total_questions"] == 1
    assert result["answered"] == 1
    assert result["answer_rate"] == 1.0
    assert result["corrected"] == 1
    assert result["corrected_rate"] == 1.0


def test_an_unanswered_question_does_not_count_as_corrected():
    conn = _Conn({"FROM pings": [_ping(1, "sink", "Ante thread — was that yours to do?")]})
    result = A.audit_questions(conn, DAY)
    assert result["answered"] == 0
    assert result["corrected"] == 0
    assert result["corrected_rate"] is None  # nothing answered — no denominator to divide by


def test_no_pings_in_the_window_is_the_honest_empty_case():
    result = A.audit_questions(_Conn(), DAY)
    assert result["total_questions"] == 0
    assert result["answer_rate"] is None
    assert result["corrected_rate"] is None


# ---------------------------------------------------------------------------
# 3. shadow-calendar accuracy
# ---------------------------------------------------------------------------


def test_a_ping_confirmed_block_matching_the_analyst_is_a_match():
    conn = _Conn({
        "source = 'ping'": [{
            "id": 20, "local_date": DAY,
            "starts_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC),
            "ends_at": datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
            "venture": "blt", "attention": "present",
        }],
        "WHERE superseded_by = %s": [{"id": 19, "source": "analyst", "venture": "blt", "attention": "present"}],
    })
    [case] = A._ping_confirmed_cases(conn, DAY)
    assert case["venture_match"] and case["attention_match"]


def test_a_ping_confirmed_block_disagreeing_with_the_analyst_is_a_mismatch():
    conn = _Conn({
        "source = 'ping'": [{
            "id": 20, "local_date": DAY,
            "starts_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC),
            "ends_at": datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
            "venture": "deadlift", "attention": "present",
        }],
        "WHERE superseded_by = %s": [{"id": 19, "source": "analyst", "venture": "blt", "attention": "displaced"}],
    })
    [case] = A._ping_confirmed_cases(conn, DAY)
    assert not case["venture_match"] and not case["attention_match"]


def test_a_ping_confirmed_block_with_no_predecessor_is_skipped_not_guessed():
    conn = _Conn({
        "source = 'ping'": [{
            "id": 20, "local_date": DAY,
            "starts_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC),
            "ends_at": datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
            "venture": "blt", "attention": "present",
        }],
    })
    assert A._ping_confirmed_cases(conn, DAY) == []


def test_empty_confirmed_spans_says_so_rather_than_a_percentage():
    result = {"days": 14, "since": "2026-09-01", "sections": {
        "accuracy": {"confirmed_spans": 3, "venture_matched": 3, "attention_matched": 3,
                     "cases": [], "coverage": {"per_day": {}, "overall": {
                         "any_evidence_share": None, "present_thin_evidence_share": None}}},
    }}
    report = A.format_report(result)
    assert "not enough confirmed spans yet (3)" in report
    assert "100%" not in report  # never a percentage from a denominator this small


def test_enough_confirmed_spans_prints_a_percentage():
    result = {"days": 14, "since": "2026-09-01", "sections": {
        "accuracy": {"confirmed_spans": 8, "venture_matched": 6, "attention_matched": 8,
                     "cases": [], "coverage": {"per_day": {}, "overall": {
                         "any_evidence_share": None, "present_thin_evidence_share": None}}},
    }}
    report = A.format_report(result)
    assert "venture agreement: 6/8 (75%)" in report


# ---------------------------------------------------------------------------
# --days / --no-record / the CLI shape
# ---------------------------------------------------------------------------


def test_no_record_writes_no_observations(monkeypatch):
    calls = []
    from iblu_keeper.store import observations as obs

    monkeypatch.setattr(obs, "record", lambda conn, **kw: calls.append(kw) or 1)
    conn = _Conn({
        "a.starts_at < b.ends_at": [{"a_id": 1, "b_id": 2, "local_date": DAY}],
    })
    result = A.run(conn, days=14, record=False, sections=("blocks",))
    assert result["findings_total"] >= 1
    assert calls == []
    assert result["recorded"] == 0


def test_record_true_writes_every_finding(monkeypatch):
    calls = []
    from iblu_keeper.store import observations as obs

    monkeypatch.setattr(obs, "record", lambda conn, **kw: calls.append(kw) or 1)
    conn = _Conn({
        "a.starts_at < b.ends_at": [{"a_id": 1, "b_id": 2, "local_date": DAY}],
    })
    result = A.run(conn, days=14, record=True, sections=("blocks",))
    assert len(calls) == result["findings_total"] == result["recorded"]


def test_sections_can_be_run_independently():
    conn = _Conn({"a.starts_at < b.ends_at": [{"a_id": 1, "b_id": 2, "local_date": DAY}]})
    result = A.run(conn, days=14, record=False, sections=("blocks",))
    assert set(result["sections"]) == {"blocks"}


def test_one_line_summary_never_raises_on_the_empty_case():
    result = A.run(_Conn(), days=14, record=False, sections=("blocks", "questions", "accuracy"))
    line = A.one_line_summary(result)
    assert line.startswith("audit:")
    assert "no observations recorded" in line


# ---------------------------------------------------------------------------
# DB-backed: the trickier SQL (jsonb lateral joins), against a real Postgres,
# rolled back — see tests/test_tap_roundtrip.py for why a fake is not enough
# proof for the behaviour that IS the SQL.
# ---------------------------------------------------------------------------

from iblu_keeper import db  # noqa: E402

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URL not set — database tests skipped by design",
)


def _insert_signal(conn, *, occurred_at, actor="me", excluded_reason=None,
                    venture_confidence="inferred", source_ref=None):
    import uuid

    row = conn.execute(
        """
        INSERT INTO signals (source, kind, account, occurred_at, actor,
                              venture_confidence, source_ref, excluded_reason)
        VALUES ('gmail', 'sent', 'ignas@blanklabel.team', %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (occurred_at, actor, venture_confidence,
         source_ref or f"audit-test-{uuid.uuid4()}", excluded_reason),
    ).fetchone()
    return row["id"]


def _insert_block(conn, *, evidence, confidence="inferred", attention="present",
                   source="analyst", superseded_by=None, calendar_event_id=None):
    from psycopg.types.json import Jsonb

    now = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
    row = conn.execute(
        """
        INSERT INTO blocks (local_date, starts_at, ends_at, attention,
                             confidence, evidence, source, superseded_by,
                             calendar_event_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (DAY, now, now + timedelta(minutes=30), attention, confidence,
         Jsonb(evidence), source, superseded_by, calendar_event_id),
    ).fetchone()
    return row["id"]


@requires_db
def test_evidence_tainted_against_real_sql():
    import psycopg

    with db.get_conn() as conn:
        with conn.transaction() as tx:
            sig_id = _insert_signal(
                conn, occurred_at=datetime(2026, 9, 15, 9, 0, tzinfo=UTC), actor="other",
            )
            block_id = _insert_block(conn, evidence=[sig_id])
            found = A._check_evidence_tainted(conn, DAY)
            assert block_id in {i for f in found for i in f["evidence"]["block_ids"]}
            raise psycopg.Rollback(tx)


@requires_db
def test_fact_without_fact_evidence_against_real_sql():
    import psycopg

    with db.get_conn() as conn:
        with conn.transaction() as tx:
            sig_id = _insert_signal(
                conn, occurred_at=datetime(2026, 9, 15, 9, 0, tzinfo=UTC),
                venture_confidence="inferred",
            )
            block_id = _insert_block(conn, evidence=[sig_id], confidence="fact", source="analyst")
            found = A._check_fact_without_fact_evidence(conn, DAY)
            assert any(block_id in f["evidence"]["block_ids"] for f in found)
            raise psycopg.Rollback(tx)


@requires_db
def test_fact_with_real_fact_evidence_is_not_flagged_against_real_sql():
    import psycopg

    with db.get_conn() as conn:
        with conn.transaction() as tx:
            sig_id = _insert_signal(
                conn, occurred_at=datetime(2026, 9, 15, 9, 0, tzinfo=UTC),
                venture_confidence="fact",
            )
            block_id = _insert_block(conn, evidence=[sig_id], confidence="fact", source="analyst")
            found = A._check_fact_without_fact_evidence(conn, DAY)
            assert not any(block_id in f["evidence"]["block_ids"] for f in found)
            raise psycopg.Rollback(tx)


@requires_db
def test_stale_mirror_against_real_sql():
    import psycopg

    with db.get_conn() as conn:
        with conn.transaction() as tx:
            old_id = _insert_block(conn, evidence=[], calendar_event_id="evt-123")
            new_id = _insert_block(conn, evidence=[])
            conn.execute("UPDATE blocks SET superseded_by = %s WHERE id = %s", (new_id, old_id))
            found = A._check_stale_mirror(conn, DAY)
            assert any(old_id in f["evidence"]["block_ids"] for f in found)
            raise psycopg.Rollback(tx)
