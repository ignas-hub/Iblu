"""What the LLM judge is allowed to change — and, mostly, what it is not.

The judge relabels a day it did not build. Every rule here exists because the
alternative is a model that can quietly invent an hour of work, a venture that
does not exist, or a certainty nobody earned.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from iblu_keeper.analyst import judge as J

UTC = timezone.utc
VENTURES = ["blt", "deadlift", "choco"]
WORK_TYPES = ["build", "client", "sales"]
PROJECTS = ["machina", "email-writer"]


def block(**over):
    base = {
        "starts_at": datetime(2026, 9, 15, 9, tzinfo=UTC),
        "ends_at": datetime(2026, 9, 15, 10, tzinfo=UTC),
        "venture": "blt",
        "work_type": None,
        "project": None,
        "attention": "present",
        "confidence": "inferred",
        "evidence": [1],
        "reasoning": "60 min · 3 gmail",
        "untracked": False,
    }
    base.update(over)
    return base


def patch(**over):
    return {"blocks": [{"i": 0, **over}]}


def test_it_may_correct_the_venture_and_the_reasoning():
    [out] = J.apply_patch(
        [block()], patch(venture="deadlift", reasoning="Machina approvals"),
        VENTURES, WORK_TYPES, PROJECTS,
    )
    assert out["venture"] == "deadlift"
    # The duration stays IBLU's; the judge supplies only the "why".
    assert out["reasoning"] == "60 min · Machina approvals"


def test_it_may_not_move_a_boundary():
    """A model that can reshape the timeline can invent an hour of work."""
    moved = datetime(2026, 9, 15, 7, tzinfo=UTC)
    [out] = J.apply_patch(
        [block()], patch(starts_at=moved, ends_at=moved),
        VENTURES, WORK_TYPES, PROJECTS,
    )
    assert out["starts_at"] == datetime(2026, 9, 15, 9, tzinfo=UTC)
    assert out["ends_at"] == datetime(2026, 9, 15, 10, tzinfo=UTC)


def test_it_may_not_upgrade_confidence():
    """Only a tap makes a block a fact."""
    [out] = J.apply_patch(
        [block()], patch(confidence="fact"), VENTURES, WORK_TYPES, PROJECTS
    )
    assert out["confidence"] == "inferred"


def test_it_may_not_change_attention():
    [out] = J.apply_patch(
        [block(attention="ambiguous")], patch(attention="present"),
        VENTURES, WORK_TYPES, PROJECTS,
    )
    assert out["attention"] == "ambiguous"


def test_an_invented_venture_rejects_the_whole_response():
    with pytest.raises(ValueError, match="invented venture"):
        J.apply_patch([block()], patch(venture="nowhere"), VENTURES, WORK_TYPES, PROJECTS)


def test_an_invented_work_type_rejects_the_whole_response():
    with pytest.raises(ValueError, match="invented work type"):
        J.apply_patch([block()], patch(work_type="vibes"), VENTURES, WORK_TYPES, PROJECTS)


def test_an_invented_project_rejects_the_whole_response():
    with pytest.raises(ValueError, match="invented project"):
        J.apply_patch([block()], patch(project="brand-new"), VENTURES, WORK_TYPES, PROJECTS)


def test_any_project_is_allowed_when_no_registry_exists_yet():
    [out] = J.apply_patch([block()], patch(project="whatever"), VENTURES, WORK_TYPES, [])
    assert out["project"] == "whatever"


def test_an_index_that_does_not_exist_rejects_the_whole_response():
    with pytest.raises(ValueError, match="does not exist"):
        J.apply_patch([block()], {"blocks": [{"i": 7, "venture": "blt"}]},
                      VENTURES, WORK_TYPES, PROJECTS)


def test_a_malformed_payload_is_rejected():
    with pytest.raises(ValueError, match="no 'blocks' list"):
        J.apply_patch([block()], {"oops": True}, VENTURES, WORK_TYPES, PROJECTS)


def test_an_untracked_stretch_stays_untracked():
    """The one rule that keeps the record worth having: guessing what an
    unobserved hour was would make every other number unbelievable."""
    [out] = J.apply_patch(
        [block(untracked=True, venture=None)],
        patch(venture="blt", work_type="build", reasoning="probably BLT work"),
        VENTURES, WORK_TYPES, PROJECTS,
    )
    assert out["venture"] is None
    assert out["work_type"] is None
    assert out["reasoning"] == "60 min · 3 gmail"


def test_reasoning_is_truncated_rather_than_trusted():
    [out] = J.apply_patch(
        [block()], patch(reasoning="x" * 500), VENTURES, WORK_TYPES, PROJECTS
    )
    assert out["reasoning"].startswith("60 min · ")
    assert len(out["reasoning"].split(" · ", 1)[1]) == J.MAX_REASONING


def test_a_block_the_judge_ignores_is_left_alone():
    rows = [block(), block(venture="choco")]
    out = J.apply_patch(rows, patch(venture="deadlift"), VENTURES, WORK_TYPES, PROJECTS)
    assert out[1] == rows[1]


def test_judge_without_an_api_key_is_a_silent_no_op(monkeypatch):
    class _NoKey:
        anthropic_api_key = ""

    monkeypatch.setattr(J, "settings", _NoKey())
    rows = [block()]
    out, used = J.judge(rows, [], ventures=VENTURES, work_types=WORK_TYPES,
                        projects=PROJECTS, tz=UTC)
    assert out == rows and used is False


def test_judge_swallows_an_api_failure_and_keeps_the_computed_day(monkeypatch):
    class _Key:
        anthropic_api_key = "sk-test"

    monkeypatch.setattr(J, "settings", _Key())
    monkeypatch.setattr(J, "_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rows = [block()]
    out, used = J.judge(rows, [], ventures=VENTURES, work_types=WORK_TYPES,
                        projects=PROJECTS, tz=UTC)
    assert out == rows and used is False
