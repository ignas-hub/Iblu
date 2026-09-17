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


# --- what the evidence can actually support (2026-09-15) ------------------
#
# Found by the sense-check: "Go pickup Emory", "Emory hosting" and "dinner with
# emory" became 420 minutes of blt/client. Every one of those blocks had zero
# evidence — the labels came from the judge reading a calendar title.


def sig(i, subject=None, snippet=None):
    return {"id": i, "subject": subject, "snippet": snippet, "source": "chat"}


def test_a_calendar_title_is_not_evidence_of_what_happened():
    rows = [block(evidence=[], venture=None, work_type=None)]
    out = J.apply_patch(
        rows,
        patch(venture="blt", work_type="client", project="machina",
              reasoning="hosting a client"),
        VENTURES, WORK_TYPES, PROJECTS,
        quality=J.evidence_quality(rows, []),
    )
    assert out[0]["venture"] is None
    assert out[0]["work_type"] is None
    assert out[0]["project"] is None


def test_an_evidence_free_block_may_still_have_its_reasoning_improved():
    rows = [block(evidence=[], venture=None)]
    out = J.apply_patch(
        rows, patch(reasoning="nothing recorded during the pickup"),
        VENTURES, WORK_TYPES, PROJECTS, quality=J.evidence_quality(rows, []),
    )
    assert "nothing recorded during the pickup" in out[0]["reasoning"]


def test_signals_with_no_readable_text_support_a_venture_but_not_a_work_type():
    """Three chats rendering as nothing but the recipient's name show he was
    present. They show nothing about the KIND of work."""
    rows = [block(evidence=[1, 2, 3])]
    signals = [sig(1), sig(2), sig(3)]
    out = J.apply_patch(
        rows, patch(venture="deadlift", work_type="client", project="machina"),
        VENTURES, WORK_TYPES, PROJECTS, quality=J.evidence_quality(rows, signals),
    )
    assert out[0]["venture"] == "deadlift"
    assert out[0]["work_type"] is None
    assert out[0]["project"] is None


def test_one_signal_with_real_text_is_enough_to_label_fully():
    rows = [block(evidence=[1, 2])]
    signals = [sig(1), sig(2, subject="Dokumenti mjesec 08. Blank Label d.o.o.")]
    out = J.apply_patch(
        rows, patch(venture="blt", work_type="client"),
        VENTURES, WORK_TYPES, PROJECTS, quality=J.evidence_quality(rows, signals),
    )
    assert out[0]["venture"] == "blt" and out[0]["work_type"] == "client"


def test_evidence_quality_grades_each_block_on_its_own_signals():
    rows = [block(evidence=[]), block(evidence=[1]), block(evidence=[2])]
    signals = [sig(1), sig(2, snippet="please review the contract")]
    assert J.evidence_quality(rows, signals) == [J.NOTHING, J.VENTURE_ONLY, J.FULL]


def test_whitespace_is_not_readable_content():
    rows = [block(evidence=[1])]
    assert J.evidence_quality(rows, [sig(1, subject="   ", snippet="\n")]) == [J.VENTURE_ONLY]


def test_without_a_quality_list_the_judge_is_unrestricted():
    """Back-compatible: callers that pass no grading get the old behaviour."""
    out = J.apply_patch([block(evidence=[])], patch(venture="blt"),
                        VENTURES, WORK_TYPES, PROJECTS)
    assert out[0]["venture"] == "blt"


# --- the judge may only cite its own block (item E, 2026-09-16) -----------
#
# Found in the diagnosis: a 07:00 block's reasoning said 'Only a calendar
# title ("Go school")' although "Go school" was a 14:45 intent that this
# block had nothing to do with.


def test_reasoning_citing_another_blocks_calendar_title_is_rejected():
    rows = [block(intent_title=None, evidence=[])]
    signals: list[dict] = []
    out = J.apply_patch(
        rows,
        patch(reasoning='Only a calendar title ("Go school")'),
        VENTURES, WORK_TYPES, PROJECTS,
        quality=J.evidence_quality(rows, signals),
        signals=signals,
    )
    assert out[0]["reasoning"] == rows[0]["reasoning"]


def test_reasoning_citing_its_own_intent_title_is_allowed():
    rows = [block(intent_title="Go school", evidence=[])]
    signals: list[dict] = []
    out = J.apply_patch(
        rows,
        patch(reasoning='calendar said "Go school"; nothing recorded'),
        VENTURES, WORK_TYPES, PROJECTS,
        quality=J.evidence_quality(rows, signals),
        signals=signals,
    )
    assert "Go school" in out[0]["reasoning"]


def test_reasoning_citing_its_own_evidence_subject_is_allowed():
    rows = [block(evidence=[1])]
    signals = [sig(1, subject="Dokumenti mjesec 08. Blank Label d.o.o.")]
    out = J.apply_patch(
        rows,
        patch(reasoning='replying to "Dokumenti mjesec 08. Blank Label d.o.o."'),
        VENTURES, WORK_TYPES, PROJECTS,
        quality=J.evidence_quality(rows, signals),
        signals=signals,
    )
    assert "Dokumenti" in out[0]["reasoning"]


def test_without_signals_the_citation_guard_is_skipped():
    """Back-compatible: callers that pass no signals get the old behaviour —
    mirrors the `quality` back-compat rule above."""
    rows = [block(evidence=[])]
    out = J.apply_patch(
        rows, patch(reasoning='calendar said "Someone else\'s meeting"'),
        VENTURES, WORK_TYPES, PROJECTS,
    )
    assert "Someone else's meeting" in out[0]["reasoning"]
