"""Day reconstruction — the clustering and attention rules, without a database.

`cluster_signals` and `build` are deliberately pure: given signals and intents
they return blocks. That is what makes a reconstruction arguable, and it is
what these tests pin down.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

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


def intent(start: datetime, end: datetime, venture=None, title="Meeting", event_id="e1", **kw):
    return B.Interval(start=start, end=end, event_id=event_id, title=title, venture=venture, **kw)


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


# --- a cluster of ONLY calendar-edit signals is not evidence of a stretch of
#     attention (real case: three edits to "Go school" at 07:05, 2026-09-16,
#     became a 30-minute `present` block for ten seconds of calendar admin) -


def test_a_cluster_of_only_calendar_signals_produces_no_block():
    rows = [signal(n, at(7, 5), source="calendar") for n in range(3)]
    assert B.cluster_signals(rows) == []
    assert B.build(B.cluster_signals(rows), []) == []


def test_a_calendar_signal_still_joins_a_cluster_something_else_formed():
    rows = [
        signal(0, at(7, 5), source="calendar"),
        signal(1, at(7, 6), source="calendar"),
        signal(2, at(7, 7), source="calendar"),
        signal(3, at(7, 10), source="gmail"),
    ]
    [c] = B.cluster_signals(rows)
    assert c.signal_ids == [0, 1, 2, 3]
    [block] = B.build(B.cluster_signals(rows), [])
    assert block["evidence"] == [0, 1, 2, 3]


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


def test_two_overlapping_calendar_events_do_not_both_claim_the_same_minutes():
    """Found by the sense-check on 2026-09-14: "Emory hosting" 15:45-16:00 and
    "dinner with emory" 15:45-17:30 each emitted an ambiguous block for 15:45,
    so the day double-counted itself. Ignas double-books constantly."""
    intents = [
        intent(at(15, 45), at(16, 0), title="Emory hosting", event_id="a"),
        intent(at(15, 45), at(17, 30), title="dinner with emory", event_id="b"),
    ]
    blocks = B.build([], intents)
    for first, second in zip(blocks, blocks[1:]):
        assert first["ends_at"] <= second["starts_at"], "two blocks claim the same minute"


def test_the_longer_of_two_overlapping_events_wins_the_span():
    intents = [
        intent(at(15, 45), at(16, 0), title="short", event_id="a"),
        intent(at(15, 45), at(17, 30), title="long", event_id="b"),
    ]
    [block] = B.build([], intents)
    assert block["intent_title"] == "long"


def test_a_partially_overlapping_event_still_reports_the_part_that_is_its_own():
    intents = [
        intent(at(9, 0), at(10, 0), title="first", event_id="a"),
        intent(at(9, 30), at(11, 0), title="second", event_id="b"),
    ]
    blocks = B.build([], intents)
    titles = [b["intent_title"] for b in blocks]
    assert "first" in titles and "second" in titles
    for first, second in zip(blocks, blocks[1:]):
        assert first["ends_at"] <= second["starts_at"]


# --- label thresholds instead of bare majorities (item C) ------------------


def test_a_mixed_cluster_gets_no_project_when_nothing_has_a_real_majority():
    """The Email Writer smear: a 135-minute block of "invoices, ITIN/company
    setup, a client thread" got tagged project 'email-writer' because that was
    the single most common Chat space among many unrelated ones — a plurality
    of one, not agreement."""
    rows = [
        signal(1, at(9, 0), project="invoices"),
        signal(2, at(9, 10), project="itin-setup"),
        signal(3, at(9, 20), project="client-thread"),
        signal(4, at(9, 30), project="email-writer"),
    ]
    [block] = B.build(B.cluster_signals(rows), [])
    assert block["project"] is None


def test_project_below_coverage_threshold_is_none_even_with_full_agreement():
    """One signal naming a project out of three who bothered to is not the
    same as three out of thirty — LABEL_COVERAGE, not just LABEL_AGREEMENT."""
    rows = [
        signal(1, at(9, 0), project="machina"),
        signal(2, at(9, 10)),
        signal(3, at(9, 20)),
    ]
    [block] = B.build(B.cluster_signals(rows), [])
    assert block["project"] is None


def test_project_assigned_when_it_clears_both_thresholds():
    rows = [
        signal(1, at(9, 0), project="machina"),
        signal(2, at(9, 10), project="machina"),
        signal(3, at(9, 20)),
    ]
    [block] = B.build(B.cluster_signals(rows), [])
    assert block["project"] == "machina"


def test_venture_is_none_when_it_is_an_exact_tie():
    """VENTURE_AGREEMENT is 'more than 50%' — an exact tie is not a winner."""
    rows = [signal(1, at(9, 0), venture="blt"), signal(2, at(9, 10), venture="choco")]
    [block] = B.build(B.cluster_signals(rows), [])
    assert block["venture"] is None


def test_thresholds_are_applied_per_slice_not_per_cluster():
    """A cluster cut at an intent boundary must not let one slice's evidence
    decide another slice's label."""
    rows = [
        signal(1, at(9, 40), project="machina"),
        signal(2, at(9, 45), project="machina"),
        signal(3, at(10, 5), project="other-thing"),
    ]
    intents = [intent(at(10, 0), at(11, 0), venture="blt")]
    blocks = B.build(B.cluster_signals(rows), intents)
    before = [b for b in blocks if b["ends_at"] <= at(10, 0)]
    after = [b for b in blocks if b["starts_at"] >= at(10, 0)]
    assert before and before[0]["project"] == "machina"
    # One lone signal in the after-slice: it has 100% agreement of the votes
    # that exist, but that is not what breaks it — there is nothing here that
    # should leak "machina" across the boundary.
    assert after and after[0]["project"] != "machina"


# --- zero-signal slice inside an intent (item D) ---------------------------


def test_zero_signal_slice_inside_an_intent_is_ambiguous_not_present():
    """11:00-11:15 during "Alexan Ignas Emory sync" had evidence=[] yet
    attention=present, inherited from the surrounding cluster widened by
    TAIL/flooring and then cut at the intent boundary. That inheritance is
    only honest when nothing claims the time; here the intent does."""
    clusters = B.cluster_signals([signal(1, at(10, 5), venture="blt")])
    intents = [intent(at(10, 10), at(10, 15), venture="family", title="Alexan Ignas Emory sync",
                       event_id="sync1")]
    blocks = B.build(clusters, intents)
    tail = [b for b in blocks if b["starts_at"] == at(10, 10)]
    assert len(tail) == 1
    [b] = tail
    assert b["evidence"] == []
    assert b["attention"] == "ambiguous"
    assert b["venture"] == "family"
    assert b["work_type"] is None
    assert b["project"] is None
    assert 'was on the calendar' in b["reasoning"]


def test_a_zero_signal_slice_not_inside_any_intent_keeps_todays_behaviour():
    rows = [signal(n, at(9, n * 10)) for n in range(6)]
    [block] = B.build(B.cluster_signals(rows), [])
    assert block["attention"] == "present"


# --- context intents never create blocks or cause displacement (real-data
#     follow-up: shared family calendars, whereabouts markers, travel
#     markers) -------------------------------------------------------------


def test_a_spouse_created_family_event_is_context_only():
    """A shared family calendar holds other people's plans too — every event
    on it becoming Ignas's intent would mark his work 'displaced' whenever
    his wife has an appointment."""
    iv = intent(at(10, 0), at(11, 0), venture="family", title="Greta nicoj",
                event_id="g1", is_context=True, needs_commitment_check=True)
    clusters = B.cluster_signals([signal(1, at(10, 5), venture="blt")])
    blocks = B.build(clusters, [iv])
    assert all(b["intent_event_id"] != "g1" for b in blocks)
    assert all(b["attention"] != "displaced" for b in blocks)
    [block] = blocks
    assert block["attention"] == "present"


def test_an_ignas_commitment_family_event_causes_displacement():
    iv = intent(at(10, 0), at(11, 0), venture="family", title="Futbolas",
                event_id="f1", is_context=False, needs_commitment_check=True)
    clusters = B.cluster_signals([signal(1, at(10, 5), venture="blt")])
    block, *_ = B.build(clusters, [iv])
    assert block["attention"] == "displaced"


def test_a_very_long_event_produces_no_block():
    """"Ignas LT Fri 12h - Sun 17h" is a three-day travel marker; taken as an
    intent it would claim every waking hour of three days."""
    iv = intent(at(7, 0), at(20, 0), venture="family", title="Ignas LT Fri-Sun",
                event_id="long1", is_context=True, is_context_locked=True)
    blocks = B.build([], [iv])
    assert blocks == []


def test_compact_intent_marks_an_event_over_twelve_hours_as_locked_context():
    from zoneinfo import ZoneInfo

    event = {
        "id": "e1",
        "summary": "Ignas LT Fri 12h - Sun 17h",
        "start": {"dateTime": "2026-09-11T12:00:00+02:00"},
        "end": {"dateTime": "2026-09-13T17:00:00+02:00"},
    }
    iv = B._compact_intent(
        event, calendar_id="fam", account="blt", default_venture="family",
        needs_commitment_check=True, tz=ZoneInfo("Europe/Zagreb"),
        window=(at(0, 0) - timedelta(days=10), at(0, 0) + timedelta(days=10)),
    )
    assert iv.is_context_locked is True
    assert iv.is_context is True


# --- family-presence inference (item: "assume the family event happened") --


def test_quiet_family_span_is_inferred_present():
    outside = signal(0, at(7, 0), venture="blt")
    clusters = B.cluster_signals([outside])
    fam = intent(at(10, 0), at(10, 40), venture="family", title="Futbolas", event_id="fut1")
    blocks = B.build(clusters, [fam])
    [b] = [x for x in blocks if x["intent_event_id"] == "fut1"]
    assert b["attention"] == "present"
    assert b["venture"] == "family"
    assert b["confidence"] == "inferred"
    assert "assumed it happened" in b["reasoning"]


def test_family_span_mostly_covered_by_work_is_not_inferred():
    """"If most of the time I was on Claude Code or email, maybe it didn't
    happen." FAMILY_INFERENCE_MAX_WORK_SHARE."""
    outside = signal(0, at(7, 0), venture="blt")
    work1 = signal(1, at(10, 0), venture="blt")
    work2 = signal(2, at(10, 25), venture="blt")
    clusters = B.cluster_signals([outside, work1, work2])
    fam = intent(at(10, 0), at(11, 10), venture="family", title="Futbolas", event_id="fut3")
    blocks = B.build(clusters, [fam])
    fam_blocks = [b for b in blocks if b["intent_event_id"] == "fut3"]
    remainder = [b for b in fam_blocks if b["attention"] != "displaced"]
    assert remainder, "expected an ambiguous remainder to still exist"
    assert all(b["attention"] == "ambiguous" for b in remainder)


def test_the_three_part_family_example_breaks_down_correctly():
    """Ignas's own illustration: quiet, then working, then quiet again — the
    Secretary calendar should show all three, not one flat 'ambiguous'."""
    outside = signal(0, at(7, 0), venture="blt")
    work1 = signal(1, at(11, 0), venture="blt")
    work2 = signal(2, at(11, 10), venture="blt")
    clusters = B.cluster_signals([outside, work1, work2])
    fam = intent(at(10, 0), at(13, 0), venture="family", title="Futbolas", event_id="fut4")
    blocks = B.build(clusters, [fam])
    fam_blocks = sorted(
        (b for b in blocks if b["intent_event_id"] == "fut4"),
        key=lambda b: b["starts_at"],
    )
    assert [b["attention"] for b in fam_blocks] == ["present", "displaced", "present"]
    assert fam_blocks[0]["venture"] == "family" and "assumed" in fam_blocks[0]["reasoning"]
    assert fam_blocks[1]["venture"] == "blt"
    assert fam_blocks[2]["venture"] == "family" and "assumed" in fam_blocks[2]["reasoning"]


def test_a_short_quiet_gap_is_not_inferred_as_family():
    """FAMILY_INFERENCE_MIN_MINUTES: a gap between two bursts of chat is not
    family time."""
    outside = signal(0, at(7, 0), venture="blt")
    clusters = B.cluster_signals([outside])
    fam = intent(at(10, 0), at(10, 15), venture="family", title="Futbolas", event_id="fut5")
    blocks = B.build(clusters, [fam])
    [b] = [x for x in blocks if x["intent_event_id"] == "fut5"]
    assert b["attention"] == "ambiguous"


def test_no_signal_outside_the_family_span_leaves_it_ambiguous():
    """A quiet afternoon and a dead collector look identical unless there is
    a signal somewhere else that day proving the recorder was running."""
    fam = intent(at(10, 0), at(10, 40), venture="family", title="Futbolas", event_id="fut6")
    blocks = B.build([], [fam])
    [b] = blocks
    assert b["attention"] == "ambiguous"


def test_family_inference_does_not_reach_past_the_data_horizon():
    outside = signal(0, at(7, 0), venture="blt")
    clusters = B.cluster_signals([outside])
    fam = intent(at(10, 0), at(10, 40), venture="family", title="Futbolas", event_id="fut7")
    blocks = B.build(clusters, [fam], horizon=at(10, 20))
    [b] = [x for x in blocks if x["intent_event_id"] == "fut7"]
    assert b["attention"] == "ambiguous"


def test_family_inference_applies_fully_before_the_horizon():
    outside = signal(0, at(7, 0), venture="blt")
    clusters = B.cluster_signals([outside])
    fam = intent(at(10, 0), at(10, 40), venture="family", title="Futbolas", event_id="fut8")
    blocks = B.build(clusters, [fam], horizon=at(11, 0))
    [b] = [x for x in blocks if x["intent_event_id"] == "fut8"]
    assert b["attention"] == "present" and b["venture"] == "family"


def test_a_work_meeting_with_no_signals_stays_ambiguous_not_inferred():
    """Unchanged: family-presence inference never applies to a non-family
    intent."""
    outside = signal(0, at(7, 0), venture="blt")
    clusters = B.cluster_signals([outside])
    meeting = intent(at(10, 0), at(10, 40), venture="blt", title="BLT standup", event_id="wm1")
    blocks = B.build(clusters, [meeting])
    [b] = [x for x in blocks if x["intent_event_id"] == "wm1"]
    assert b["attention"] == "ambiguous"


# --- the third attendance state: `maybe` ("sometimes I go, sometimes I
#     don't") — never a commitment, but its silence is still read carefully -


def maybe_intent(start, end, *, title="Futbolas", event_id="fut-maybe"):
    """A `maybe` family intent exactly as `classify_missing` would leave one:
    `is_context=True` (never a commitment), `attendance='maybe'`."""
    return intent(
        start, end, venture="family", title=title, event_id=event_id,
        needs_commitment_check=True, is_context=True, attendance="maybe",
    )


def test_a_maybe_intent_quiet_span_is_inferred_present_assumed_you_went():
    outside = signal(0, at(7, 0), venture="blt")
    clusters = B.cluster_signals([outside])
    fam = maybe_intent(at(10, 0), at(10, 40), event_id="fut-m1")
    blocks = B.build(clusters, [fam])
    [b] = [x for x in blocks if x["intent_event_id"] == "fut-m1"]
    assert b["attention"] == "present"
    assert b["venture"] == "family"
    assert b["confidence"] == "inferred"
    assert "assumed you went" in b["reasoning"]


def test_a_maybe_intent_full_of_work_is_present_not_displaced_and_claims_no_family():
    outside = signal(0, at(7, 0), venture="blt")
    work1 = signal(1, at(10, 0), venture="blt")
    work2 = signal(2, at(10, 25), venture="blt")
    clusters = B.cluster_signals([outside, work1, work2])
    fam = maybe_intent(at(10, 0), at(11, 10), event_id="fut-m2")
    blocks = B.build(clusters, [fam])
    # A `maybe` intent adds no cut points, so the work signals inside it are
    # ordinary evidence-based blocks claimed by no intent at all — never
    # `intent_event_id == "fut-m2"` (that's the whole point: nothing here is
    # attributed to the family event).
    assert all(b.get("intent_event_id") != "fut-m2" for b in blocks)
    work_blocks = [b for b in blocks if b["venture"] == "blt" and b.get("evidence")]
    assert work_blocks, "the work signals should still produce ordinary blocks"
    # never displaced — a `maybe` intent can never cause displacement.
    assert all(b["attention"] == "present" for b in work_blocks)
    assert all(b["attention"] != "displaced" for b in blocks)
    # and no family time claimed anywhere in the span.
    assert not any(
        b["venture"] == "family" and at(10, 0) <= b["starts_at"] < at(11, 10)
        for b in blocks
    )


def test_a_maybe_intent_never_produces_an_ambiguous_block_of_its_own():
    """No evidence anywhere that day -> guard-rail (c) (watched elsewhere)
    fails, so nothing is claimed — and, unlike a real commitment, nothing
    `ambiguous` is left behind either."""
    fam = maybe_intent(at(10, 0), at(10, 40), event_id="fut-m3")
    blocks = B.build([], [fam])
    assert blocks == []


def test_a_maybe_intent_short_remainder_is_not_claimed_as_family():
    outside = signal(0, at(7, 0), venture="blt")
    clusters = B.cluster_signals([outside])
    fam = maybe_intent(at(10, 0), at(10, 15), event_id="fut-m4")
    blocks = B.build(clusters, [fam])
    assert all(b.get("intent_event_id") != "fut-m4" for b in blocks)


def test_a_maybe_intent_density_guard_still_applies():
    outside = signal(0, at(7, 0), venture="blt")
    work1 = signal(1, at(10, 0), venture="blt")
    work2 = signal(2, at(10, 25), venture="blt")
    clusters = B.cluster_signals([outside, work1, work2])
    fam = maybe_intent(at(10, 0), at(11, 10), event_id="fut-m5")
    blocks = B.build(clusters, [fam])
    fam_blocks = [b for b in blocks if b["intent_event_id"] == "fut-m5"]
    remainder = [b for b in fam_blocks if not b.get("evidence")]
    assert not any(b["venture"] == "family" for b in remainder)


# --- per-day attendance answers override the classifier (yes / no / part) --


def test_apply_attendance_answers_maps_yes_no_and_part(monkeypatch):
    class _FakeRowsConn:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, sql, params=None):
            return self

        def fetchall(self):
            return self._rows

        def rollback(self):
            pass

    from iblu_keeper.analyst.intents import instance_key

    on = date(2026, 9, 15)
    iv_yes = intent(at(17, 0), at(18, 30), venture="family", title="Futbolas",
                     event_id="e-yes", calendar_id="cal1", needs_commitment_check=True,
                     is_context=True, attendance="maybe")
    iv_no = intent(at(9, 0), at(9, 30), venture="family", title="Greta nicoj",
                    event_id="e-no", calendar_id="cal1", needs_commitment_check=True,
                    is_context=True)
    iv_part = intent(at(15, 0), at(16, 0), venture="family", title="Roditeljsku sastanak",
                      event_id="e-part", calendar_id="cal1", needs_commitment_check=True,
                      is_context=True, attendance="maybe")

    rows = [
        {"instance_key": instance_key("cal1", "e-yes", on), "attended": "yes"},
        {"instance_key": instance_key("cal1", "e-no", on), "attended": "no"},
        {"instance_key": instance_key("cal1", "e-part", on), "attended": "part"},
    ]
    conn = _FakeRowsConn(rows)
    out = B._apply_attendance_answers(conn, on, [iv_yes, iv_no, iv_part])
    assert out is not None

    assert iv_yes.attendance == "his" and iv_yes.is_context is False
    assert iv_yes.attendance_answer == "yes"

    assert iv_no.attendance == "not_his" and iv_no.is_context is True
    assert iv_no.attendance_answer == "no"

    assert iv_part.attendance == "maybe" and iv_part.is_context is True
    assert iv_part.attendance_answer == "part"


def test_confirmed_yes_causes_displacement_and_a_fact_family_remainder():
    outside = signal(0, at(7, 0), venture="blt")
    work1 = signal(1, at(10, 5), venture="blt")
    clusters = B.cluster_signals([outside, work1])
    fam = intent(
        at(10, 0), at(10, 40), venture="family", title="Futbolas", event_id="fut-y1",
        needs_commitment_check=True, is_context=False, attendance="his",
        attendance_answer="yes",
    )
    blocks = B.build(clusters, [fam])
    fam_blocks = sorted(
        (b for b in blocks if b["intent_event_id"] == "fut-y1"),
        key=lambda b: b["starts_at"],
    )
    assert [b["attention"] for b in fam_blocks] == ["displaced", "present"]
    assert fam_blocks[0]["venture"] == "blt"
    assert fam_blocks[1]["venture"] == "family"
    assert fam_blocks[1]["confidence"] == "fact"
    assert "you said you went" in fam_blocks[1]["reasoning"]


def test_confirmed_yes_skips_the_density_and_watched_elsewhere_guard_rails():
    """No other signal anywhere that day (guard-rail (c) would normally
    fail) — a confirmed 'yes' does not need it."""
    fam = intent(
        at(10, 0), at(10, 40), venture="family", title="Futbolas", event_id="fut-y2",
        needs_commitment_check=True, is_context=False, attendance="his",
        attendance_answer="yes",
    )
    blocks = B.build([], [fam])
    [b] = blocks
    assert b["attention"] == "present"
    assert b["venture"] == "family"
    assert b["confidence"] == "fact"


def test_confirmed_no_treats_the_instance_as_context():
    outside = signal(0, at(7, 0), venture="blt")
    work1 = signal(1, at(10, 5), venture="blt")
    clusters = B.cluster_signals([outside, work1])
    fam = intent(
        at(10, 0), at(10, 40), venture="family", title="Futbolas", event_id="fut-n1",
        needs_commitment_check=True, is_context=True, attendance="not_his",
        attendance_answer="no",
    )
    blocks = B.build(clusters, [fam])
    assert all(b.get("intent_event_id") != "fut-n1" for b in blocks)
    assert any(
        b["venture"] == "blt" and b["attention"] == "present" for b in blocks
    )


def test_confirmed_part_keeps_maybe_semantics():
    outside = signal(0, at(7, 0), venture="blt")
    clusters = B.cluster_signals([outside])
    fam = intent(
        at(10, 0), at(10, 40), venture="family", title="Futbolas", event_id="fut-p1",
        needs_commitment_check=True, is_context=True, attendance="maybe",
        attendance_answer="part",
    )
    blocks = B.build(clusters, [fam])
    [b] = [x for x in blocks if x["intent_event_id"] == "fut-p1"]
    assert b["attention"] == "present" and b["venture"] == "family"
    assert b["confidence"] == "inferred", "'part' is not a confirmed 'yes'"


# --- displacement now works for family time (item F) ------------------------


def test_family_intent_causes_displacement_for_blt_signals_end_to_end():
    """Once "Go school" is a `family` intent (item B), BLT chat during it must
    produce `attention='displaced'` — the whole point of the fix."""
    clusters = B.cluster_signals([signal(1, at(14, 50), venture="blt")])
    intents = [intent(at(14, 45), at(15, 15), venture="family", title="Go school",
                       event_id="school1")]
    block, *_ = B.build(clusters, intents)
    assert block["attention"] == "displaced"


# --- load_intents: reading every account's primary, plus INTENT_CALENDARS --


class _FakeAccountSettings:
    """Stands in for the module-level `settings` inside `blocks.py` — never
    the frozen `Settings` dataclass itself."""

    def __init__(self, accounts, intent_calendars=(), primary_alias="blt"):
        self._accounts = accounts
        self._intent_calendars = list(intent_calendars)
        self.primary_alias = primary_alias
        self.iblu_timezone = "UTC"

    def configured_accounts(self):
        return self._accounts

    @property
    def intent_calendars_parsed(self):
        return self._intent_calendars


class _FakeVenturesConn:
    """Answers only `SELECT code FROM ventures` — everything `load_intents`
    needs from a connection for calendar-source validation."""

    def __init__(self, ventures=("blt", "choco", "deadlift", "family", "jakusi", "personal")):
        self._ventures = ventures

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return [{"code": v} for v in self._ventures]

    def rollback(self):
        pass


def gevent(event_id, title, start, end, *, ical_uid=None, attendees=None):
    body = {
        "id": event_id,
        "summary": title,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    if ical_uid:
        body["iCalUID"] = ical_uid
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    return body


def test_load_intents_dedupes_the_same_meeting_on_two_calendars(monkeypatch):
    from iblu_keeper import google_auth
    from iblu_keeper.tools import calendar_manage

    monkeypatch.setattr(
        B, "settings", _FakeAccountSettings([{"alias": "blt"}, {"alias": "choco"}])
    )
    ev = gevent("abc", "Sync", at(10, 0), at(11, 0), ical_uid="uid-1")
    monkeypatch.setattr(google_auth, "build_service", lambda *a, **k: object())
    monkeypatch.setattr(calendar_manage, "_fetch_events", lambda *a, **k: [ev])

    intents = B.load_intents(_FakeVenturesConn(), at(0, 0), at(0, 0) + timedelta(days=1))
    assert len(intents) == 1


def test_unreadable_intent_calendar_does_not_lose_the_others(monkeypatch):
    from iblu_keeper import google_auth
    from iblu_keeper.store import observations as obs
    from iblu_keeper.tools import calendar_manage

    monkeypatch.setattr(
        B, "settings",
        _FakeAccountSettings(
            [{"alias": "blt"}],
            intent_calendars=[{"venture": "family", "calendar_id": "shared-fam", "account": "blt"}],
        ),
    )
    good_event = gevent("g1", "Roditelsku sastanak", at(9, 0), at(9, 30))

    def fake_fetch(service, calendar_id, start, end):
        if calendar_id == "shared-fam":
            raise RuntimeError("403 not shared")
        return [good_event]

    monkeypatch.setattr(google_auth, "build_service", lambda *a, **k: object())
    monkeypatch.setattr(calendar_manage, "_fetch_events", fake_fetch)
    recorded = []
    monkeypatch.setattr(obs, "record_safe", lambda **kw: recorded.append(kw) or 1)

    intents = B.load_intents(_FakeVenturesConn(), at(0, 0), at(0, 0) + timedelta(days=1))
    assert [iv.event_id for iv in intents] == ["g1"]
    assert recorded and recorded[0]["kind"] == "intent_calendar_unreadable"


def test_blt_primary_gets_no_default_venture_while_choco_primary_does(monkeypatch):
    from iblu_keeper import google_auth
    from iblu_keeper.tools import calendar_manage

    monkeypatch.setattr(
        B, "settings", _FakeAccountSettings([{"alias": "blt"}, {"alias": "choco"}])
    )
    blt_event = gevent("b1", "Go school", at(9, 0), at(9, 30))
    choco_event = gevent("c1", "Weekly", at(9, 0), at(9, 30))

    def fake_fetch(service, calendar_id, start, end):
        if service == "blt":
            return [blt_event]
        if service == "choco":
            return [choco_event]
        return []

    monkeypatch.setattr(google_auth, "build_service", lambda api, ver, account=None: account)
    monkeypatch.setattr(calendar_manage, "_fetch_events", fake_fetch)

    intents = B.load_intents(_FakeVenturesConn(), at(0, 0), at(0, 0) + timedelta(days=1))
    by_id = {iv.event_id: iv for iv in intents}
    assert by_id["b1"].venture is None
    assert by_id["c1"].venture == "choco"


def test_intent_calendars_entry_with_unknown_venture_is_skipped(monkeypatch):
    from iblu_keeper import google_auth
    from iblu_keeper.tools import calendar_manage

    monkeypatch.setattr(
        B, "settings",
        _FakeAccountSettings(
            [], intent_calendars=[{"venture": "nonexistent", "calendar_id": "cal1", "account": "blt"}],
        ),
    )
    called = []

    def fake_fetch(service, calendar_id, start, end):
        called.append(calendar_id)
        return []

    monkeypatch.setattr(google_auth, "build_service", lambda *a, **k: object())
    monkeypatch.setattr(calendar_manage, "_fetch_events", fake_fetch)

    intents = B.load_intents(_FakeVenturesConn(ventures=("blt", "choco")), at(0, 0), at(0, 0) + timedelta(days=1))
    assert intents == []
    assert called == []


def test_unattributed_work_still_counts_against_family_inference():
    """Work with no clear venture is never marked `displaced` (displacement
    needs a known venture), so a density check that counted only displaced
    slices let an afternoon of unattributable work be claimed as family time."""
    outside = signal(0, at(7, 0), venture="blt")
    work = [signal(n, at(10, 0) + timedelta(minutes=10 * n), venture=None) for n in range(1, 6)]
    clusters = B.cluster_signals([outside, *work])
    fam = intent(at(10, 0), at(11, 10), venture="family", title="Futbolas", event_id="fut9")
    blocks = B.build(clusters, [fam])
    fam_blocks = [b for b in blocks if b["intent_event_id"] == "fut9"]
    assert not any(
        b["attention"] == "present" and b["venture"] == "family" for b in fam_blocks
    ), "a busy span was claimed as family time"


def test_a_confident_analyst_block_is_not_mistaken_for_a_tap():
    """`fact` confidence means the evidence agrees — a git commit in a repo that
    names its venture produces exactly that. Protecting every `fact` block froze
    the analyst's own earlier git-backed blocks as if Ignas had confirmed them."""
    assert B._is_confirmed({"source": "ping", "confidence": "fact"})
    assert B._is_confirmed({"source": "human", "confidence": "inferred"})
    assert not B._is_confirmed({"source": "analyst", "confidence": "fact"})
