"""The handoff from the observation log to the next Claude Code session.

Three things are worth pinning here, and only three:

  * a lead must never reach his phone, and a fact must,
  * the brief must say nothing rather than fail, whatever is broken,
  * a finding nobody has seen in a fortnight must not be presented as current.

Everything else about the brief is prose, and prose is not a contract.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from iblu_keeper.jobs import session_brief, watchdog
from iblu_keeper.store import observations as obs

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _row(**kw) -> dict:
    row = {
        "id": 1,
        "first_seen_at": NOW - timedelta(hours=2),
        "last_seen_at": NOW - timedelta(hours=1),
        "occurrences": 1,
        "source": "sensecheck",
        "kind": "thin_evidence",
        "severity": "warn",
        "detected_by": "llm",
        "summary": "a block rests on nothing",
        "detail": "the rationale admits the chat content is unreadable",
        "evidence": {},
    }
    row.update(kw)
    return row


# --- the split that the whole design rests on -------------------------------


def test_chat_alerts_are_rule_findings_only():
    """An LLM lead must never reach his phone, whatever severity it claims.

    The sense-check's LLM pass picks its own severity, so before this filter a
    lead could call itself an `error` and page him at 04:45 about a block that
    needed a code change. Asserted against the SQL because that is where the
    rule lives; a mocked connection would pass while the query was wrong.
    """
    sql = inspect.getsource(watchdog.alertable)
    assert "detected_by = 'rule'" in sql
    assert "severity = 'error'" in sql


def test_leads_and_facts_are_shown_apart():
    text = session_brief.compose(
        [
            _row(id=1, detected_by="llm", summary="a lead about a block"),
            _row(id=2, detected_by="rule", kind="unit_down", severity="error",
                 summary="iblu-tick.timer is dead"),
        ],
        now=NOW,
    )
    facts_at = text.index("## Facts")
    leads_at = text.index("## Leads")
    assert facts_at < leads_at, "facts come first — they reproduce"
    assert text.index("iblu-tick.timer is dead") < leads_at
    assert text.index("a lead about a block") > leads_at


# --- silence is the contract ------------------------------------------------


def test_nothing_open_prints_nothing():
    assert session_brief.compose([], now=NOW) == ""


def test_a_broken_database_is_silent_not_fatal(monkeypatch):
    """No `.env`, Postgres down, a pending migration — all the same answer.

    A session-start hook that can refuse to let you start working is worse than
    no hook, so `_load` swallows everything.
    """
    def explode():
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr("iblu_keeper.db.is_configured", explode)
    assert session_brief._load() == []


def test_the_hook_entry_point_never_fails(monkeypatch):
    monkeypatch.setattr(session_brief, "_load", lambda: (_ for _ in ()).throw(OSError("nope")))
    assert session_brief.main([]) == 0


def test_hook_output_is_the_shape_claude_code_reads():
    """The real subprocess, as the hook runs it: valid JSON or nothing at all."""
    result = subprocess.run(
        [sys.executable, "-m", "iblu_keeper.jobs.session_brief"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0
    if not result.stdout.strip():
        return                      # nothing open is a legitimate answer
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert payload["hookSpecificOutput"]["additionalContext"].strip()


# --- an old finding is history, not a to-do ---------------------------------


def test_findings_nobody_has_seen_for_a_fortnight_are_not_presented_as_current():
    old = _row(id=7, last_seen_at=NOW - timedelta(days=20))
    assert session_brief.compose([old], now=NOW) == ""

    text = session_brief.compose([old, _row(id=8)], now=NOW)
    assert "1 older than 14d not shown" in text


def test_the_most_persistent_finding_is_shown_first():
    """Seen fifteen times is a defect; seen once may be a day since rebuilt."""
    text = session_brief.compose(
        [
            _row(id=1, detected_by="rule", occurrences=1, summary="happened once"),
            _row(id=2, detected_by="rule", occurrences=15, summary="happened fifteen times"),
        ],
        now=NOW,
    )
    assert text.index("happened fifteen times") < text.index("happened once")


# --- the retirement path that was missing -----------------------------------


def test_transient_facts_are_the_kinds_that_describe_a_moment():
    """`llm_call_failed` is "this call failed" — it cannot be a standing state.

    These four had no way out of the log: `retire_cleared` only knows the
    watchdog's own kinds and `age_out_llm_leads` only touches leads. So a
    13-day-old credit-balance rejection was still being handed to new sessions
    as a current fault.
    """
    assert set(obs.TRANSIENT_FACT_KINDS) == {
        "llm_call_failed", "llm_output_rejected",
        "llm_sensecheck_unavailable", "llm_unavailable",
    }
    # Never a data-quality kind: those retire by not reproducing, which the
    # sense-check checks properly, or not at all.
    assert not {"thin_evidence", "misattributed_venture", "unit_down"} & set(
        obs.TRANSIENT_FACT_KINDS
    )


def test_ageing_out_a_fact_claims_it_stopped_not_that_nobody_looked():
    """The wording is the contract: a fact's absence is evidence, a lead's is not."""
    fact = inspect.getsource(obs.age_out_transient_facts)
    lead = inspect.getsource(obs.age_out_llm_leads)
    assert "has not happened again" in fact
    assert "detected_by = 'rule'" in fact
    assert "not reproduced" in lead
    assert "detected_by = 'llm'" in lead
