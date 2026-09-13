"""Day reconstruction — the clustering and attention rules, without a database.

`cluster_signals` and `build` are deliberately pure: given signals and intents
they return blocks. That is what makes a reconstruction arguable, and it is
what these tests pin down.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from iblu_keeper.analyst import blocks as B

UTC = timezone.utc


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 15, hour, minute, tzinfo=UTC)


def signal(n: int, when: datetime, venture="blt", confidence="inferred",
           work_type="build", source="gmail", project=None) -> dict:
    return {
        "id": n,
        "source": source,
        "occurred_at": when,
        "venture": venture,
        "venture_confidence": confidence,
        "work_type": work_type,
        "project": project,
    }


def intent(start: datetime, end: datetime, venture=None, title="Meeting", event_id="e1"):
    return B.Interval(start=start, end=end, event_id=event_id, title=title, venture=venture)


# --- clustering -----------------------------------------------------------


def test_single_signal_becomes_one_floored_block():
    [c] = B.cluster_signals([signal(1, at(9, 7))])
    assert c.start == at(9, 0)
    assert c.end == at(9, 30)  # 09:07 + 10 min tail, ceiled
    assert c.signal_ids == [1]


def test_signals_within_the_gap_are_one_cluster():
    rows = [signal(1, at(9, 0)), signal(2, at(9, 20)), signal(3, at(9, 40))]
    clusters = B.cluster_signals(rows)
    assert len(clusters) == 1
    assert clusters[0].signal_ids == [1, 2, 3]


def test_a_long_silence_splits_the_cluster():
    rows = [signal(1, at(9, 0)), signal(2, at(11, 0))]
    clusters = B.cluster_signals(rows)
    assert len(clusters) == 2


def test_clusters_that_touch_after_flooring_are_merged():
    # 26 min apart, so two clusters — but 10:00 + tail ceils to 10:15 and
    # 10:26 floors to 10:15, so the two stretches meet and are one stretch.
    rows = [signal(1, at(10, 0)), signal(2, at(10, 26))]
    clusters = B.cluster_signals(rows)
    assert len(clusters) == 1
    assert clusters[0].signal_ids == [1, 2]


def test_no_signals_no_clusters():
    assert B.cluster_signals([]) == []


# --- attention ------------------------------------------------------------


def test_work_with_nothing_claiming_the_time_is_present():
    [block] = B.build(B.cluster_signals([signal(1, at(9, 0))]), [])
    assert block["attention"] == "present"
    assert block["intent_event_id"] is None


def test_evidence_for_another_venture_during_an_intent_is_displaced():
    clusters = B.cluster_signals([signal(1, at(10, 5), venture="deadlift")])
    intents = [intent(at(10, 0), at(11, 0), venture="blt", title="BLT standup")]
    block, *rest = B.build(clusters, intents)
    assert block["attention"] == "displaced"
    # The rest of the meeting produced nothing, so it is unknown, not present.
    assert [b["attention"] for b in rest] == ["ambiguous"]
    assert block["venture"] == "deadlift"
    assert 'calendar said "BLT standup"' in block["reasoning"]


def test_evidence_agreeing_with_the_intent_is_present():
    clusters = B.cluster_signals([signal(1, at(10, 5), venture="blt")])
    intents = [intent(at(10, 0), at(11, 0), venture="blt")]
    block, *_ = B.build(clusters, intents)
    assert block["attention"] == "present"


def test_an_intent_that_produced_nothing_is_ambiguous_not_idle():
    [block] = B.build([], [intent(at(14, 0), at(15, 0), venture="blt", title="Football")])
    assert block["attention"] == "ambiguous"
    assert block["evidence"] == []
    assert block["confidence"] == "inferred"
    assert "nothing recorded" in block["reasoning"]


def test_silence_outside_an_intent_produces_no_block_at_all():
    """The core rule: unobserved time is unknown, and inventing a block for it
    would be inventing a fact."""
    blocks = B.build([], [])
    assert blocks == []


def test_an_intent_is_split_into_the_part_that_happened_and_the_part_that_did_not():
    """The mission's own example: a long intent becomes present + ambiguous."""
    clusters = B.cluster_signals([signal(1, at(14, 5), venture="deadlift")])
    intents = [intent(at(14, 0), at(17, 0), venture="blt", title="Football")]
    blocks = B.build(clusters, intents)
    attentions = [b["attention"] for b in blocks]
    assert "displaced" in attentions and "ambiguous" in attentions
    # The whole intent is accounted for, with nothing double-counted.
    assert blocks[0]["starts_at"] == at(14, 0)
    assert blocks[-1]["ends_at"] == at(17, 0)
    for prev, nxt in zip(blocks, blocks[1:]):
        assert prev["ends_at"] == nxt["starts_at"]


def test_a_cluster_is_cut_at_the_intent_boundary():
    rows = [signal(1, at(9, 50), venture="deadlift"), signal(2, at(10, 10), venture="deadlift")]
    intents = [intent(at(10, 0), at(11, 0), venture="blt")]
    blocks = B.build(B.cluster_signals(rows), intents)
    assert [b["attention"] for b in blocks][:2] == ["present", "displaced"]
    assert blocks[0]["ends_at"] == at(10, 0) == blocks[1]["starts_at"]


def test_an_ambiguous_remainder_shorter_than_the_floor_is_dropped():
    clusters = B.cluster_signals([signal(1, at(10, 0))])   # covers 10:00-10:15
    intents = [intent(at(10, 0), at(10, 20), venture="blt")]
    blocks = B.build(clusters, intents)
    assert [b["attention"] for b in blocks] == ["present"]


# --- attribution ----------------------------------------------------------


def test_venture_is_the_majority_of_the_clusters_signals():
    rows = [
        signal(1, at(9, 0), venture="blt"),
        signal(2, at(9, 10), venture="blt"),
        signal(3, at(9, 20), venture="choco"),
    ]
    [block] = B.build(B.cluster_signals(rows), [])
    assert block["venture"] == "blt"


def test_confidence_is_fact_only_when_every_vote_agrees_and_one_was_a_fact():
    agreed = [signal(1, at(9, 0), venture="blt", confidence="fact"),
              signal(2, at(9, 10), venture="blt", confidence="inferred")]
    [block] = B.build(B.cluster_signals(agreed), [])
    assert block["confidence"] == "fact"

    disputed = [signal(1, at(9, 0), venture="blt", confidence="fact"),
                signal(2, at(9, 10), venture="choco", confidence="fact")]
    [block] = B.build(B.cluster_signals(disputed), [])
    assert block["confidence"] == "inferred"


def test_inferred_only_signals_never_become_a_fact():
    rows = [signal(1, at(9, 0), venture="blt"), signal(2, at(9, 10), venture="blt")]
    [block] = B.build(B.cluster_signals(rows), [])
    assert block["confidence"] == "inferred"


def test_adjacent_identical_blocks_are_merged_and_keep_all_evidence():
    rows = [signal(1, at(9, 0)), signal(2, at(9, 20)), signal(3, at(9, 40))]
    [block] = B.build(B.cluster_signals(rows), [])
    assert block["evidence"] == [1, 2, 3]


def test_reasoning_names_the_evidence_it_used():
    rows = [signal(1, at(9, 0), source="gmail"), signal(2, at(9, 10), source="slack")]
    [block] = B.build(B.cluster_signals(rows), [])
    assert "1 gmail" in block["reasoning"] and "1 slack" in block["reasoning"]


# --- day bounds -----------------------------------------------------------


def test_day_bounds_are_a_local_day_expressed_in_utc():
    from zoneinfo import ZoneInfo

    start, end = B.day_bounds(B.date(2026, 9, 15), ZoneInfo("Europe/Zagreb"))
    assert end - start == timedelta(days=1)
    assert start.astimezone(ZoneInfo("Europe/Zagreb")).hour == 0


# --- refusals -------------------------------------------------------------


def test_reconstruct_refuses_in_mock_mode():
    """Mock mode means every signal and every calendar event is fabricated."""
    with pytest.raises(RuntimeError, match="mock mode"):
        B.reconstruct(None, B.date(2026, 9, 15))


def test_mirror_refuses_in_mock_mode(monkeypatch):
    from iblu_keeper.analyst import mirror

    class _Live:
        use_mock = True
        secretary_calendar_id = "cal@group.calendar.google.com"

    monkeypatch.setattr(mirror, "settings", _Live())
    with pytest.raises(RuntimeError, match="mock mode"):
        mirror.mirror_day(None, B.date(2026, 9, 15))


def test_reasoning_quotes_the_merged_blocks_own_minutes():
    """The line is written after merging — quoting a slice's length would make
    a 75-minute block claim it was 15."""
    rows = [signal(n, at(9, n * 10)) for n in range(6)]   # 09:00 .. 09:50
    [block] = B.build(B.cluster_signals(rows), [])
    minutes = int((block["ends_at"] - block["starts_at"]).total_seconds() // 60)
    assert block["reasoning"].startswith(f"{minutes} min ·")
    assert minutes > 15


def test_an_intent_with_no_venture_of_its_own_never_causes_displacement():
    """A flight is not a claim on his attention; a client meeting is. Only an
    event that identifies a venture can say he was somewhere he shouldn't be."""
    clusters = B.cluster_signals([signal(1, at(10, 5), venture="deadlift")])
    flight = intent(at(10, 0), at(11, 0), venture=None, title="Flight to Warsaw")
    block, *_ = B.build(clusters, flight and [flight])
    assert block["attention"] == "present"
    assert 'during "Flight to Warsaw"' in block["reasoning"]


def test_build_returns_no_internal_keys():
    [block] = B.build(B.cluster_signals([signal(1, at(9, 0))]), [])
    assert not [k for k in block if k.startswith("_")]


def test_evidence_is_not_double_counted_across_slices():
    """Cutting a cluster at an intent boundary must split its evidence, not
    hand the whole cluster's signals to each half."""
    rows = [signal(n, at(9, 40) + timedelta(minutes=10 * n)) for n in range(5)]  # 09:40 .. 10:20
    intents = [intent(at(10, 0), at(11, 0), venture="blt")]
    blocks = B.build(B.cluster_signals(rows), intents)
    ids = [i for b in blocks for i in b["evidence"]]
    assert sorted(ids) == [0, 1, 2, 3, 4]
    assert len(ids) == len(set(ids))


# --- mirror titles ---------------------------------------------------------


def test_an_ambiguous_block_is_named_after_the_intent_it_did_not_account_for():
    from iblu_keeper.analyst import mirror

    [block] = B.build([], [intent(at(10, 0), at(11, 0), title="Womanizer alignment")])
    assert mirror._title(block) == "? Womanizer alignment"


def test_a_displaced_block_names_both_what_he_did_and_what_he_was_meant_to():
    from iblu_keeper.analyst import mirror

    clusters = B.cluster_signals([signal(1, at(10, 5), venture="deadlift")])
    block, *_ = B.build(clusters, [intent(at(10, 0), at(11, 0), venture="blt", title="BLT standup")])
    title = mirror._title(block)
    assert "deadlift" in title and "not BLT standup" in title


def test_a_present_block_is_not_marked():
    from iblu_keeper.analyst import mirror

    [block] = B.build(B.cluster_signals([signal(1, at(9, 0), venture="blt")]), [])
    assert mirror._title(block).startswith("blt")


def test_mirror_events_never_block_time_and_never_notify():
    """The Secretary calendar is a record of the past. It must not make him
    look busy to anyone, and it must not buzz his phone about yesterday."""
    from iblu_keeper.analyst import mirror

    [block] = B.build(B.cluster_signals([signal(1, at(9, 0))]), [])
    block["id"] = 1
    body = mirror._body(block)
    assert body["transparency"] == "transparent"
    assert body["reminders"] == {"useDefault": False, "overrides": []}
    assert body["extendedProperties"]["private"]["iblu"] == "block"


# --- untracked time --------------------------------------------------------


def workday():
    return at(7, 0), at(20, 0)


def test_an_unaccounted_stretch_of_the_workday_becomes_an_untracked_block():
    blocks = B.build(B.cluster_signals([signal(1, at(9, 0))]), [], workday=workday())
    untracked = [b for b in blocks if b["untracked"]]
    assert untracked, "the rest of the workday should be visible as unknown"
    assert all(b["venture"] is None for b in untracked)
    assert all(b["attention"] == "ambiguous" for b in untracked)
    assert all(b["confidence"] == "inferred" for b in untracked)
    assert "nothing recorded, and nothing on the calendar" in untracked[0]["reasoning"]


def test_untracked_blocks_cover_the_workday_and_nothing_outside_it():
    blocks = B.build(B.cluster_signals([signal(1, at(9, 0))]), [], workday=workday())
    assert min(b["starts_at"] for b in blocks) == at(7, 0)
    assert max(b["ends_at"] for b in blocks) == at(20, 0)


def test_a_short_unaccounted_gap_is_not_reported():
    """Twenty minutes between two tasks is a gap between tasks, not a gap in
    the record."""
    rows = [signal(1, at(9, 0)), signal(2, at(9, 40))]   # a 15-minute hole
    blocks = B.build(B.cluster_signals(rows), [], workday=(at(9, 0), at(9, 55)))
    assert not [b for b in blocks if b["untracked"]]


def test_an_untracked_stretch_never_merges_with_an_unaccounted_meeting():
    """Both are 'unknown', but only one of them has a name to ask about."""
    blocks = B.build([], [intent(at(9, 0), at(10, 0), title="Standup")], workday=workday())
    named = [b for b in blocks if not b["untracked"]]
    assert len(named) == 1 and named[0]["intent_title"] == "Standup"


def test_evidence_outside_the_workday_still_produces_a_block():
    """The workday bounds say where silence is worth reporting, not where work
    counts. A 21:30 commit is still work."""
    blocks = B.build(B.cluster_signals([signal(1, at(21, 30))]), [], workday=workday())
    assert any(b["starts_at"] >= at(21, 0) and not b["untracked"] for b in blocks)


# --- confirmed blocks survive a rebuild ------------------------------------


def test_a_confirmed_block_is_carved_out_of_a_rebuild():
    """Acceptance E2: a tapped answer is the truth for its span; a later
    reconstruction only describes what is left."""
    blocks = B.build(B.cluster_signals([signal(1, at(9, 0))]), [], workday=workday())
    carved = B._carve_out(blocks, [(at(10, 0), at(12, 0))])
    for b in carved:
        assert not (b["starts_at"] < at(12, 0) and b["ends_at"] > at(10, 0)), (
            "a rebuild overlapped a confirmed block"
        )


def test_carving_drops_slivers_rather_than_emitting_them():
    blocks = B.build(B.cluster_signals([signal(1, at(9, 0))]), [], workday=workday())
    carved = B._carve_out(blocks, [(at(7, 10), at(19, 50))])
    assert all(b["ends_at"] - b["starts_at"] >= B.FLOOR for b in carved)


def test_carving_nothing_changes_nothing():
    blocks = B.build(B.cluster_signals([signal(1, at(9, 0))]), [], workday=workday())
    assert B._carve_out(blocks, []) == blocks


# --- carving must re-derive, not copy (review finding, 2026-09-13) ---------


def test_carving_re_derives_evidence_for_each_surviving_piece():
    """The first version copied evidence onto both halves, so a 20-minute
    remainder still claimed a signal that fell inside the span just cut out."""
    rows = [signal(n, at(9, 0) + timedelta(minutes=15 * n)) for n in range(4)]  # 09:00..09:45
    blocks = B.build(B.cluster_signals(rows), [])
    carved = B._carve_out(blocks, [(at(9, 15), at(9, 30))], rows)
    for piece in carved:
        for sid in piece["evidence"]:
            when = rows[sid]["occurred_at"]
            assert piece["starts_at"] <= when < piece["ends_at"], (
                f"signal {sid} at {when} claimed by {piece['starts_at']}-{piece['ends_at']}"
            )


def test_carving_re_derives_the_reasoning_minutes():
    """A 20-minute remainder must not read '60 min'."""
    rows = [signal(n, at(9, 0) + timedelta(minutes=10 * n)) for n in range(6)]
    blocks = B.build(B.cluster_signals(rows), [])
    carved = B._carve_out(blocks, [(at(9, 30), at(9, 45))], rows)
    for piece in carved:
        minutes = int((piece["ends_at"] - piece["starts_at"]).total_seconds() // 60)
        assert piece["reasoning"].startswith(f"{minutes} min ·"), piece["reasoning"]


def test_carving_without_the_signals_claims_no_evidence_rather_than_the_wrong_evidence():
    blocks = B.build(B.cluster_signals([signal(1, at(9, 0))]), [])
    carved = B._carve_out(blocks, [(at(9, 5), at(9, 10))])
    assert all(p["evidence"] == [] for p in carved)


# --- a block may not straddle the day it is filed under -------------------


def test_a_late_signal_does_not_push_a_block_into_tomorrow():
    """`cluster_signals` widens by TAIL with no idea where midnight is, so a
    23:58 signal produced a block ending 00:15 — stored under yesterday,
    invisible to tomorrow's rebuild, free to overlap it."""
    day_end = at(0, 0) + timedelta(days=1)
    blocks = B.build(B.cluster_signals([signal(1, at(23, 58))]), [])
    assert max(b["ends_at"] for b in blocks) > day_end, "fixture no longer spills"

    clipped = B._clip_to_day(blocks, (at(0, 0), day_end))
    assert all(b["ends_at"] <= day_end for b in clipped)
    assert all(b["starts_at"] >= at(0, 0) for b in clipped)


def test_clipping_drops_a_remainder_below_the_floor_rather_than_emitting_it():
    blocks = B.build(B.cluster_signals([signal(1, at(23, 58))]), [])
    clipped = B._clip_to_day(blocks, (at(0, 0), at(23, 50)))
    assert all(b["ends_at"] - b["starts_at"] >= B.FLOOR for b in clipped)


def test_clipping_leaves_an_ordinary_block_untouched():
    blocks = B.build(B.cluster_signals([signal(1, at(9, 0))]), [])
    assert B._clip_to_day(blocks, (at(0, 0), at(0, 0) + timedelta(days=1))) == blocks
