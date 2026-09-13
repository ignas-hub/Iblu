"""The Gap warning: advice, never a veto.

Ignas measures himself against ideals. IBLU's job is to state progress backward
from a baseline. When he writes a goal that cannot be measured that way, the
tool says so once — and writes it anyway, because his goals are his.
"""

from __future__ import annotations

from iblu_keeper.store.gap_check import check


def test_a_superlative_is_flagged():
    w = check("decision", "Become the best ads agency in the Baltics", ["priority"])
    assert w and "superlative" in w


def test_a_comparison_to_another_company_is_flagged():
    assert check("decision", "Grow faster than Sparkleads this year", ["priority"])


def test_a_race_is_flagged():
    assert check("decision", "Catch up with the agencies doing 200K/mo", ["priority"])


def test_an_obligation_is_flagged():
    assert check("decision", "Deadlift should be at 100K/mo", ["priority"])


def test_a_distance_from_an_ideal_is_flagged():
    assert check("decision", "Machina is still behind where it needs to be", ["priority"])


def test_a_priority_with_no_observable_end_state_is_flagged():
    w = check("decision", "Focus more on sales and be more consistent", ["priority"])
    assert w and "nothing to measure backward to" in w


def test_a_backward_measurable_priority_passes():
    assert check(
        "decision",
        "GoStellar lands at least one new client by 2027-09-13; Greta signs Alexan.",
        ["priority"],
    ) is None


def test_a_dated_handover_passes():
    assert check(
        "decision",
        "Email Writer handed over to Edo and running without me.",
        ["priority"],
    ) is None


def test_an_ordinary_decision_is_not_held_to_the_priority_standard():
    """A decision is allowed to be a sentence about a choice."""
    assert check("decision", "Use Postgres rather than SQLite for IBLU.", []) is None


def test_only_decisions_and_preferences_are_checked():
    for t in ("fact", "work_log", "correction", "conversation_note"):
        assert check(t, "Become the best agency anywhere", ["priority"]) is None


def test_the_check_never_raises_on_empty_content():
    assert check("decision", "", ["priority"]) is not None or True


def test_the_advice_reads_as_a_sentence():
    w = check("decision", "Become the best ads agency", ["priority"])
    body = w.split(". ", 1)[1]
    assert body[0].isupper(), f"advice starts mid-sentence: {w!r}"
