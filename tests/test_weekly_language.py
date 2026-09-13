"""The weekly review's language gate (plan §1.7, context in §7).

Ignas grades himself against ideals he hasn't reached; IBLU measures backward
from a baseline instead. These tests protect that boundary: they check the
validator does not fire on ordinary, honest phrasing (the two false-positive
cases below), and that it does fire on each concrete rejection rule.
"""

from __future__ import annotations

from iblu_keeper.jobs.review_language import validate_language


# --- false positives — these must PASS -------------------------------------


def test_behind_the_scenes_is_not_gap_language():
    text = "## Truth\nThe Recruiter automation runs behind the scenes all week.\n"
    assert validate_language(text) == []


def test_measured_attention_share_is_not_a_goal_percentage():
    text = "## Truth\n38% of the week was BLT, by signal count.\n"
    assert validate_language(text) == []


# --- one case per rejection rule -------------------------------------------


def test_still_away_is_rejected():
    text = "## Truth\nStill 3 clients away from where this needs to be.\n"
    violations = validate_language(text)
    assert any(v.startswith("forward_gap:still_away") for v in violations)


def test_only_of_target_is_rejected():
    text = "## Truth\nOnly answered 4 of the target 5 pings this week.\n"
    violations = validate_language(text)
    assert any(v.startswith("forward_gap:only_of_target") for v in violations)


def test_behind_alone_is_rejected():
    text = "## Truth\nThe Recruiter project is behind where it should be.\n"
    violations = validate_language(text)
    assert any(v.startswith("forward_gap:behind") for v in violations)


def test_should_have_is_rejected():
    text = "## Truth\nHe should have closed that thread earlier in the week.\n"
    violations = validate_language(text)
    assert any(v.startswith("forward_gap:should_have") for v in violations)


def test_not_enough_is_rejected():
    text = "## Truth\nNot enough signals this week to draw a conclusion.\n"
    violations = validate_language(text)
    assert any(v.startswith("forward_gap:not_enough") for v in violations)


def test_percent_of_goal_is_rejected():
    text = "## Truth\nHit only 38% of goal this week.\n"
    violations = validate_language(text)
    assert any(v.startswith("forward_gap:percent_of_goal") for v in violations)


def test_comparison_to_a_company_is_rejected():
    text = "## Truth\nDeadlift moved faster than Choco this week.\n"
    violations = validate_language(text)
    assert any(v.startswith("comparison:other") for v in violations)


def test_comparison_to_a_person_is_rejected():
    text = "## Truth\nHe closed more threads than Marko did.\n"
    violations = validate_language(text)
    assert any(v.startswith("comparison:other") for v in violations)


def test_compared_to_phrase_is_rejected():
    text = "## Truth\nThis week, compared to a typical founder's week, was quiet.\n"
    violations = validate_language(text)
    assert any(v.startswith("comparison:other") for v in violations)


def test_generic_praise_is_rejected():
    for phrase in ("great job", "well done", "amazing", "impressive", "crushing it"):
        text = f"## Gains\nBLT client signed. {phrase.capitalize()}!\n"
        violations = validate_language(text)
        assert any(v.startswith("praise:generic") for v in violations), phrase


def test_future_tense_in_gains_section_is_rejected():
    text = (
        "## Gains\n"
        "Next week I will close the Machina integration.\n\n"
        "## Truth\n"
        "38% of the week was BLT.\n"
    )
    violations = validate_language(text)
    assert any(v.startswith("gains:future_tense_as_gain") for v in violations)


def test_future_tense_outside_gains_section_passes():
    """'One removal' is explicitly a forward step — it must not be rejected
    for describing what happens next; only the Gains section is a promise
    dressed up as evidence."""
    text = (
        "## Gains\n"
        "Machina library shipped 2026-09-10.\n\n"
        "## One removal\n"
        "Hand the Recruiter thread to Edo; he is going to take the next reply.\n"
    )
    assert validate_language(text) == []


def test_clean_text_has_no_violations():
    text = (
        "## Gains\n"
        "Machina library shipped 2026-09-10. Decision logged 2026-09-11: "
        "switch invoicing to the new accountant.\n\n"
        "## Truth\n"
        "38% of the week was BLT, 22% Deadlift. 9/10 pings answered (90%); "
        "target is 80%. Untracked share: 12%.\n\n"
        "## One removal\n"
        "Hand the Womanizer gifting thread to Bella — it has been touched "
        "4 times this week and she already runs the reporting side.\n"
    )
    assert validate_language(text) == []
