"""New ping card types (plan docs/plans/2026-09-13-sessions5-8.md §3.3, §4.1-4.2).

Fakes only — no database, no network, nothing sent. A `FakeConn` stands in for
Postgres wherever `pings.answers.record_tap` needs one; every other test here
exercises a pure function directly.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from iblu_keeper.config import settings
from iblu_keeper.pings import answers, compose, tokens

UTC = timezone.utc
TZ = ZoneInfo(settings.iblu_timezone)  # compose._local() renders in this zone
SECRET = "test-secret-not-the-real-one"


def at(hour: int, minute: int = 0) -> datetime:
    """A local-time instant on the test day — `compose._local()` renders
    exactly `hour:minute` back out, regardless of the configured timezone."""
    return datetime(2026, 9, 15, hour, minute, tzinfo=TZ)


# ---------------------------------------------------------------------------
# tap budget (plan §0, binding)
# ---------------------------------------------------------------------------


def _q(qid: str, n_options: int = 2) -> compose.Question:
    kind = qid if qid in ("gains", "body_mind") else "sink"
    verdict = {"gains": "other", "body_mind": "other"}.get(kind, "planned_mine")
    options = [
        {"key": chr(65 + i), "label": f"opt {i}", "payload": {"kind": kind, "verdict": verdict}}
        for i in range(n_options)
    ]
    # Deliberately generic text: the qid values are internal keys the
    # Question.text validator refuses to leak (JARGON) — this is a fixture
    # for the tap-budget tests, not a wording test.
    return compose.Question.model_validate({"qid": qid, "text": "Was that yours to do?", "options": options})


def test_midday_budget_is_two_questions_total():
    questions = [_q("sink"), _q("work_type"), _q("displaced")]
    kept = compose.enforce_tap_budget("midday", questions)
    assert len(kept) == 2


def test_evening_budget_is_two_attention_one_gains_one_body_mind():
    questions = [_q("sink"), _q("displaced"), _q("split"), _q("gap"), _q("gains"), _q("body_mind")]
    kept = compose.enforce_tap_budget("evening", questions)
    assert len(kept) == 4
    qids = [q.qid for q in kept]
    assert qids.count("gains") == 1
    assert qids.count("body_mind") == 1
    assert sum(1 for q in kept if q.qid in compose._ATTENTION_QIDS) == 2
    # order is preserved: sink and displaced win the two attention slots.
    assert qids == ["sink", "displaced", "gains", "body_mind"]


def test_evening_never_exceeds_four_even_with_many_attention_candidates():
    questions = [_q("sink"), _q("displaced"), _q("split"), _q("gap"), _q("work_type"), _q("gains"), _q("body_mind")]
    kept = compose.enforce_tap_budget("evening", questions)
    assert len(kept) == 4


def test_unknown_kind_falls_back_to_the_midday_cap():
    kept = compose.enforce_tap_budget("something-else", [_q("sink"), _q("displaced"), _q("split")])
    assert len(kept) == 2


def test_compose_never_exceeds_the_evening_budget_end_to_end(monkeypatch):
    """The orchestrator, not just the enforcer — split/gap/gains/body_mind
    all compete for the same 4 slots.

    Forces the deterministic fallback (no `anthropic_api_key`) so this stays
    a no-network test regardless of what the host's own `.env` carries.
    """
    monkeypatch.setattr(compose, "settings", type(
        "S", (), {"anthropic_api_key": "", "iblu_timezone": settings.iblu_timezone},
    )())
    blocks = [
        {"id": 1, "starts_at": at(9), "ends_at": at(11), "venture": "deadlift",
         "work_type": "build", "project": "machina", "attention": "present",
         "confidence": "inferred", "intent_title": None},
        {"id": 2, "starts_at": at(11), "ends_at": at(12), "venture": None,
         "work_type": None, "project": None, "attention": "ambiguous",
         "confidence": "inferred", "intent_title": None},
    ]
    gain_evidence = {
        "learned": [{"label": "Logged a decision", "evidence_ids": ["1"]}],
        "progressed": [{"label": "Moved Machina forward", "evidence_ids": ["2"]}],
        "experienced": [],
    }
    signals = [{
        "id": 1, "source": "chat", "occurred_at": at(9, 5), "counterpart": "Ante",
        "container": "spaces/X", "subject": "Machina", "snippet": "hi",
        "initiator": "other", "venture": "deadlift",
    }]
    qs, composer = compose.compose(
        signals, [], at(7), at(20), [], [],
        kind="evening", blocks=blocks, gain_evidence=gain_evidence,
    )
    assert composer == "fallback"
    assert len(qs.questions) <= 4
    qids = [q.qid for q in qs.questions]
    assert qids.count("gains") <= 1
    assert qids.count("body_mind") <= 1
    assert sum(1 for q in qids if q in compose._ATTENTION_QIDS) <= 2


# ---------------------------------------------------------------------------
# gap — untracked blocks (plan §3.3)
# ---------------------------------------------------------------------------


def test_gap_question_only_fires_for_a_truly_untracked_block():
    tracked = [{"id": 1, "starts_at": at(9), "ends_at": at(10), "venture": "blt",
                "work_type": None, "project": None, "attention": "present",
                "confidence": "fact", "intent_title": None}]
    assert compose.compose_gap_question(tracked, "blt") is None

    with_calendar_title = [{"id": 2, "starts_at": at(9), "ends_at": at(10), "venture": None,
                             "work_type": None, "project": None, "attention": "ambiguous",
                             "confidence": "inferred", "intent_title": "Standup"}]
    assert compose.compose_gap_question(with_calendar_title, "blt") is None, (
        "a block with an intent_title is an unaccounted MEETING, not untracked time"
    )


def test_gap_question_options_and_shape():
    untracked = [{"id": 7, "starts_at": at(11), "ends_at": at(12, 30), "venture": None,
                  "work_type": None, "project": None, "attention": "ambiguous",
                  "confidence": "inferred", "intent_title": None}]
    q = compose.compose_gap_question(untracked, "deadlift")
    assert q["qid"] == "gap"
    assert q["text"] == "11:00–12:30 shows nothing. What was it?"
    labels = [o["label"] for o in q["options"]]
    assert labels == [
        "Meeting / call not in my mail", "Deep work — deadlift", "Personal / life", "Other → reply",
    ]
    for option in q["options"]:
        assert option["payload"]["block_id"] == 7
    # every non-escape option carries something to write; "other" carries
    # nothing but the block id, because the honest answer is a reply.
    assert q["options"][0]["payload"]["work_type"] == "client"
    assert q["options"][1]["payload"]["venture"] == "deadlift"
    assert q["options"][2]["payload"]["venture"] == "family"
    assert q["options"][3]["payload"]["verdict"] == "other"


# ---------------------------------------------------------------------------
# split — block-based confirmation (plan §3.3)
# ---------------------------------------------------------------------------


def test_split_question_confirms_the_busiest_inferred_block():
    blocks = [
        {"id": 1, "starts_at": at(14), "ends_at": at(16), "venture": "deadlift",
         "work_type": "build", "project": "machina", "attention": "present",
         "confidence": "inferred", "intent_title": None},
        {"id": 2, "starts_at": at(9), "ends_at": at(9, 30), "venture": "blt",
         "work_type": None, "project": None, "attention": "present",
         "confidence": "inferred", "intent_title": None},
    ]
    q = compose.compose_split_question(blocks)
    assert q["qid"] == "split"
    assert q["text"] == "14:00–16:00 looks like deadlift · machina — right?"
    assert q["options"][0]["label"] == "Right"
    assert q["options"][0]["payload"]["block_id"] == 1
    assert q["options"][0]["payload"]["venture"] == "deadlift"


def test_split_question_ignores_already_confirmed_blocks():
    blocks = [{"id": 1, "starts_at": at(14), "ends_at": at(16), "venture": "deadlift",
               "work_type": None, "project": None, "attention": "present",
               "confidence": "fact", "intent_title": None}]
    assert compose.compose_split_question(blocks) is None


# ---------------------------------------------------------------------------
# gains — "what moved today?" (plan §4.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Will ship Machina approvals",
    "Plan: close the Opera thread",
    "60% through the legal docs",
    "Going to call Greta",
])
def test_gain_validator_rejects_plans_and_unfinished_goals(text):
    ok, reason = compose.validate_gain_option(text)
    assert ok is False
    assert reason is not None


@pytest.mark.parametrize("text", [
    "Logged the decision to cover Alexan's cost myself",
    "Closed the Womanizer reporting thread",
    "Two hours present with family",
])
def test_gain_validator_accepts_already_happened_things(text):
    ok, reason = compose.validate_gain_option(text)
    assert ok is True
    assert reason is None


def test_a_rejected_gains_label_fails_question_validation():
    """F1: a Plan: option injected into a card must not survive validation —
    it has to fall back rather than ship."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        compose.Question.model_validate({
            "qid": "gains",
            "text": "What moved today?",
            "options": [
                {"key": "A", "label": "Plan: close the Opera thread",
                 "payload": {"kind": "gains", "verdict": "learned", "gain_kind": "learned"}},
                {"key": "B", "label": "Add one → reply",
                 "payload": {"kind": "gains", "verdict": "other"}},
            ],
        })


def test_gains_escape_option_is_exempt_from_the_validator():
    """"Add one → reply" makes no claim of its own — nothing to validate yet."""
    q = compose.Question.model_validate({
        "qid": "gains",
        "text": "What moved today?",
        "multi": True,
        "options": [
            {"key": "A", "label": "Closed the Womanizer reporting thread",
             "payload": {"kind": "gains", "verdict": "learned", "gain_kind": "learned"}},
            {"key": "B", "label": "Add one → reply",
             "payload": {"kind": "gains", "verdict": "other"}},
        ],
    })
    assert q.multi is True


def test_compose_gains_question_offers_only_the_reply_without_evidence():
    """Changed 2026-09-13: it used to return None, so the card vanished on any
    day IBLU had seen nothing — which is exactly the day the question is for.
    IBLU still invents nothing; it just asks."""
    for empty in ({}, None):
        card = compose.compose_gains_question(empty)
        assert card is not None
        assert [o["label"] for o in card["options"]] == ["Add one → reply"]


def test_compose_gains_question_builds_one_option_per_kind_plus_escape():
    evidence = {
        "learned": [{"label": "Closed the Opera thread", "evidence_ids": ["a"]}],
        "progressed": [{"label": "Moved Machina forward", "evidence_ids": ["b"]}],
        "experienced": [],
    }
    q = compose.compose_gains_question(evidence)
    assert q["qid"] == "gains"
    assert q["multi"] is True
    kinds = [o["payload"]["gain_kind"] for o in q["options"] if o["payload"].get("gain_kind")]
    assert kinds == ["learned", "progressed"]
    assert q["options"][-1]["label"] == "Add one → reply"


# --- recording gains taps: independent, not a supersede chain -------------


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _unwrap(value):
    return getattr(value, "obj", value)


class FakeConn:
    """A minimal double for the SQL `pings.answers` needs.

    Dispatches on the SQL's own shape, like `test_governance.py`'s FakeConn —
    good enough to prove supersede/no-supersede and block-correction behaviour
    without a real Postgres.
    """

    def __init__(self, pings: dict[int, dict], blocks: dict[int, dict] | None = None):
        self.pings = pings
        self.blocks = blocks or {}
        self.context_entries: list[dict] = []
        self._next_ce_id = 1
        self._next_block_id = max(self.blocks, default=0) + 1
        self.locks: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()):
        s = " ".join(sql.split())

        if s.startswith("SELECT pg_advisory_xact_lock"):
            # Serialising concurrent taps is Postgres's job; here it is a no-op.
            self.locks.append(params)
            return _Result([])

        if s.startswith("SELECT id, kind, local_date, covers_from, covers_to, questions"):
            (ping_id,) = params
            row = self.pings.get(ping_id)
            return _Result([row] if row else [])

        if s.startswith("SELECT id, local_date, starts_at, ends_at, venture, work_type, project, attention, confidence, evidence, superseded_by FROM blocks"):
            (block_id,) = params
            row = self.blocks.get(block_id)
            return _Result([row] if row else [])

        if s.startswith("INSERT INTO context_entries"):
            content, tags, venture, work_type, project, source_ref, occurred_at, meta = params
            row = {
                "id": self._next_ce_id, "content": content, "tags": tags, "venture": venture,
                "work_type": work_type, "project": project, "source_ref": source_ref,
                "occurred_at": occurred_at, "meta": _unwrap(meta), "superseded_by": None,
            }
            self.context_entries.append(row)
            self._next_ce_id += 1
            return _Result([{"id": row["id"]}])

        if s.startswith("UPDATE context_entries SET superseded_by"):
            new_id, source_ref, exclude_id = params
            hit = []
            for row in self.context_entries:
                if row["source_ref"] == source_ref and row["id"] != exclude_id and row["superseded_by"] is None:
                    row["superseded_by"] = new_id
                    hit.append(row["id"])
            return _Result([{"id": i} for i in hit])

        if s.startswith("INSERT INTO blocks"):
            (local_date, starts_at, ends_at, venture, work_type, project,
             attention, evidence, reasoning) = params
            new_id = self._next_block_id
            self._next_block_id += 1
            row = {
                "id": new_id, "local_date": local_date, "starts_at": starts_at, "ends_at": ends_at,
                "venture": venture, "work_type": work_type, "project": project,
                "attention": attention, "confidence": "fact", "evidence": _unwrap(evidence),
                "reasoning": reasoning, "source": "ping", "superseded_by": None,
            }
            self.blocks[new_id] = row
            return _Result([{"id": new_id}])

        if s.startswith("UPDATE blocks SET superseded_by"):
            new_id, block_id = params
            self.blocks[block_id]["superseded_by"] = new_id
            return _Result([])

        if s.startswith("UPDATE pings SET status"):
            return _Result([])

        raise AssertionError(f"FakeConn got an unexpected query:\n{sql}")


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setattr(answers, "settings", type("S", (), {"ping_signing_secret": SECRET})())


def _make_ping(kind: str, questions: list[dict], ping_id: int = 1) -> dict:
    return {
        "id": ping_id, "kind": kind, "local_date": date(2026, 9, 15),
        "covers_from": at(7), "covers_to": at(20), "questions": questions,
    }


def test_gap_tap_writes_a_fact_block_and_supersedes_the_untracked_one():
    untracked_block = {
        "id": 10, "local_date": date(2026, 9, 15), "starts_at": at(11), "ends_at": at(12, 30),
        "venture": None, "work_type": None, "project": None, "attention": "ambiguous",
        "confidence": "inferred", "evidence": [], "superseded_by": None,
    }
    question = {
        "qid": "gap", "text": "11:00–12:30 shows nothing. What was it?",
        "options": [
            {"key": "B", "label": "Deep work — deadlift",
             "payload": {"kind": "gap", "verdict": "deep_work", "block_id": 10,
                         "venture": "deadlift", "work_type": "build", "attention": "present"}},
            {"key": "D", "label": "Other → reply",
             "payload": {"kind": "gap", "verdict": "other", "block_id": 10}},
        ],
    }
    conn = FakeConn(pings={1: _make_ping("evening", [question])}, blocks={10: untracked_block})

    token = tokens.make_token(1, "gap", "B", SECRET)
    result = answers.record_tap(conn, token)

    # The original is never rewritten — it is pointed at its replacement.
    assert untracked_block["venture"] is None
    assert untracked_block["confidence"] == "inferred"
    assert untracked_block["superseded_by"] == result["block_id"]

    new_block = conn.blocks[result["block_id"]]
    assert new_block["venture"] == "deadlift"
    assert new_block["confidence"] == "fact"
    assert new_block["source"] == "ping"
    assert new_block["attention"] == "present"


def test_a_second_gap_tap_supersedes_the_first_answer_not_the_original():
    untracked_block = {
        "id": 10, "local_date": date(2026, 9, 15), "starts_at": at(11), "ends_at": at(12, 30),
        "venture": None, "work_type": None, "project": None, "attention": "ambiguous",
        "confidence": "inferred", "evidence": [], "superseded_by": None,
    }
    question = {
        "qid": "gap", "text": "11:00–12:30 shows nothing. What was it?",
        "options": [
            {"key": "B", "label": "Deep work — deadlift",
             "payload": {"kind": "gap", "verdict": "deep_work", "block_id": 10,
                         "venture": "deadlift", "work_type": "build", "attention": "present"}},
            {"key": "C", "label": "Personal / life",
             "payload": {"kind": "gap", "verdict": "personal_life", "block_id": 10,
                         "venture": "family", "work_type": "life", "attention": "present"}},
        ],
    }
    conn = FakeConn(pings={1: _make_ping("evening", [question])}, blocks={10: untracked_block})

    first = answers.record_tap(conn, tokens.make_token(1, "gap", "B", SECRET))
    second = answers.record_tap(conn, tokens.make_token(1, "gap", "C", SECRET))

    first_block = conn.blocks[first["block_id"]]
    second_block = conn.blocks[second["block_id"]]

    assert untracked_block["superseded_by"] == first_block["id"], (
        "the untracked block is only ever superseded once, by the first answer"
    )
    assert first_block["superseded_by"] == second_block["id"], (
        "the second tap corrects the first answer, not the original untracked block"
    )
    assert second_block["venture"] == "family"
    assert second_block["superseded_by"] is None


def test_gains_taps_are_independent_not_a_supersede_chain():
    question = {
        "qid": "gains", "text": "What moved today?",
        "options": [
            {"key": "A", "label": "Closed the Opera thread",
             "payload": {"kind": "gains", "verdict": "learned", "gain_kind": "learned",
                         "evidence_ids": ["ce-1"]}},
            {"key": "B", "label": "Moved Machina forward",
             "payload": {"kind": "gains", "verdict": "progressed", "gain_kind": "progressed",
                         "evidence_ids": ["blk-2"]}},
        ],
    }
    conn = FakeConn(pings={1: _make_ping("evening", [question])})

    first = answers.record_tap(conn, tokens.make_token(1, "gains", "A", SECRET))
    second = answers.record_tap(conn, tokens.make_token(1, "gains", "B", SECRET))

    assert first["entry_id"] != second["entry_id"]
    assert len(conn.context_entries) == 2
    for row in conn.context_entries:
        assert row["superseded_by"] is None, "neither gains tap should supersede the other"
        assert "gain" in row["tags"]
    metas = {row["meta"]["kind"] for row in conn.context_entries}
    assert metas == {"learned", "progressed"}


def test_retapping_the_same_gain_option_does_supersede_itself():
    """Only a retap of the SAME gain corrects it — a distinct option never does."""
    question = {
        "qid": "gains", "text": "What moved today?",
        "options": [
            {"key": "A", "label": "Closed the Opera thread",
             "payload": {"kind": "gains", "verdict": "learned", "gain_kind": "learned"}},
        ],
    }
    conn = FakeConn(pings={1: _make_ping("evening", [question])})

    first = answers.record_tap(conn, tokens.make_token(1, "gains", "A", SECRET))
    second = answers.record_tap(conn, tokens.make_token(1, "gains", "A", SECRET))

    rows = {row["id"]: row for row in conn.context_entries}
    assert rows[int(first["entry_id"])]["superseded_by"] == int(second["entry_id"])


# ---------------------------------------------------------------------------
# body / mind — numbers only (plan §4.2)
# ---------------------------------------------------------------------------


def test_body_mind_options_map_to_the_1_to_5_scale():
    q = compose.compose_body_mind_question()
    assert q["qid"] == "body_mind"
    assert "body and mind" in q["text"]
    # Keyed on the verdict, not the wording: the labels were rewritten on
    # 2026-09-15 because "strong · excited" left the reader to work out which
    # word was the body, and pinning them here would fight the next rewrite.
    by_verdict = {o["payload"]["verdict"]: o["payload"] for o in q["options"]}
    assert (by_verdict["strong_excited"]["body"], by_verdict["strong_excited"]["mind"]) == (4, 4)
    assert (by_verdict["strong_tired"]["body"], by_verdict["strong_tired"]["mind"]) == (4, 2)
    assert (by_verdict["weak_excited"]["body"], by_verdict["weak_excited"]["mind"]) == (2, 4)
    assert (by_verdict["weak_exhausted"]["body"], by_verdict["weak_exhausted"]["mind"]) == (2, 2)
    assert q["options"][-1]["label"].startswith("Other → reply")
    assert len(q["options"]) == 5


@pytest.mark.parametrize("text,expected", [
    ("body 1 mind 3", (1, 3)),
    ("Body: 5, Mind: 2", (5, 2)),
    ("mind 4 body 2", (2, 4)),
    ("nothing to see here", None),
])
def test_parse_body_mind_reply(text, expected):
    assert compose.parse_body_mind_reply(text) == expected


def test_body_mind_tap_writes_one_row_with_the_numbers_and_nothing_else():
    question = {
        "qid": "body_mind", "text": "Today — body / mind",
        "options": [
            {"key": "D", "label": "weak · exhausted",
             "payload": {"kind": "body_mind", "verdict": "weak_exhausted", "body": 2, "mind": 2}},
        ],
    }
    conn = FakeConn(pings={1: _make_ping("evening", [question])})

    result = answers.record_tap(conn, tokens.make_token(1, "body_mind", "D", SECRET))

    row = conn.context_entries[0]
    assert row["meta"]["body"] == 2
    assert row["meta"]["mind"] == 2
    assert "health" in row["tags"]
    assert set(row["meta"]) >= {"body", "mind"}


def test_body_mind_reply_supersedes_a_previous_answer():
    """A `body 1 mind 3` reply overrides whatever answered the card before —
    tap or reply — via `answers._classify_reply`, without touching a DB."""
    extra_tags, meta_extra, overrides = answers._classify_reply(_ReplyConn(None), {"id": 1, "kind": "evening"}, "body 1 mind 3")
    assert overrides is True
    assert meta_extra == {"body": 1, "mind": 3}
    assert "health" in extra_tags


def test_a_plain_evening_reply_is_treated_as_a_gain():
    extra_tags, meta_extra, overrides = answers._classify_reply(_ReplyConn(None), {"id": 1, "kind": "evening"}, "Signed the new lease today")
    assert overrides is False
    assert "gain" in extra_tags


def test_a_midday_reply_is_unclassified_as_before():
    extra_tags, meta_extra, overrides = answers._classify_reply(_ReplyConn(None), {"id": 1, "kind": "midday"}, "It was a client call")
    assert extra_tags == ["reply"]
    assert overrides is False


# --- what a card may not say ----------------------------------------------


def test_a_long_label_is_trimmed_to_a_word_not_a_character():
    """A hard slice produced "Learned: YEARLY TOP PRIORITY — Ja", which looks
    broken on a phone rather than shortened."""
    from iblu_keeper.pings.compose import MAX_LABEL, Option

    # Deliberately not a `gains` option: a truncated gain is refused outright
    # (see test_a_truncated_gain_option_is_refused_by_the_schema), so this
    # exercises the trimming itself on a kind where trimming is allowed.
    label = Option(key="A",
                   label="The Secretary calendar now covers every venture at once",
                   payload={"kind": "sink", "verdict": "planned_mine"}).label
    assert len(label) <= MAX_LABEL
    assert label.endswith("…")
    assert not label[:-1].endswith(" ")


def test_a_short_label_is_left_exactly_as_written():
    from iblu_keeper.pings.compose import Option

    o = Option(key="A", label="Planned & mine",
               payload={"kind": "sink", "verdict": "planned_mine"})
    assert o.label == "Planned & mine"


def test_a_quiet_day_still_asks_what_moved():
    """The practice must not fire least on the days it is most for. IBLU can
    only see mail, chat and calendar; most of what moves a day is none of
    those, so "no evidence" is not "nothing happened"."""
    from iblu_keeper.pings.compose import compose_gains_question

    card = compose_gains_question({"learned": [], "progressed": [], "experienced": []})
    assert card is not None
    assert "moved today" in card["text"]
    assert [o["label"] for o in card["options"]] == ["Add one → reply"]


def test_the_gains_card_never_invents_a_gain():
    from iblu_keeper.pings.compose import compose_gains_question

    card = compose_gains_question(None)
    assert all(o["payload"]["verdict"] == "other" for o in card["options"])


# --- the truncation bypass (found in review, 2026-09-13) -------------------


def test_a_gain_is_validated_before_it_is_truncated():
    """The 40-char label cut the disqualifying word off before the validator
    saw it, so a plan passed the gate and would have reached his phone."""
    evidence = {"learned": [{
        "label": "Signed with three new clients this quarter, will "
                 "announce the partnership expansion plan next month",
        "evidence_ids": ["1"],
    }], "progressed": [], "experienced": []}
    card = compose.compose_gains_question(evidence)
    assert [o["label"] for o in card["options"]] == ["Add one → reply"], (
        "a future-tense option survived truncation"
    )


def test_a_truncated_gain_option_is_refused_by_the_schema():
    """The LLM path builds Options directly, so the schema needs the same
    guard: a sentence with its ending removed cannot be checked."""
    with pytest.raises(Exception, match="truncated"):
        compose.Option(
            key="A",
            label="Signed three new clients this quarter and also will "
                  "announce something later",
            payload={"kind": "gains", "verdict": "learned", "gain_kind": "learned"},
        )


def test_a_short_genuine_gain_still_passes():
    o = compose.Option(
        key="A", label="Closed the Womanizer thread",
        payload={"kind": "gains", "verdict": "learned", "gain_kind": "learned"},
    )
    assert o.label == "Closed the Womanizer thread"


# --- the vagueness rule must not eat a named question ---------------------


def test_a_named_thread_survives_even_when_it_says_email_thread():
    """Found in the observation log 2026-09-14: this exact question was
    rejected for containing "email thread", although it names the thread
    twice, and the composer fell back to a generic question all day."""
    q = compose.Question.model_validate({
        "qid": "sink",
        "text": "Binance/Defixolt email thread with Jurgita — you authorized "
                "power of attorney at 15:26. Was that yours to do?",
        "options": [
            {"key": "A", "label": "Planned & mine",
             "payload": {"kind": "sink", "verdict": "planned_mine"}},
            {"key": "B", "label": "One-off, ignore",
             "payload": {"kind": "sink", "verdict": "one_off"}},
        ],
    })
    assert "Binance/Defixolt" in q.text


def test_a_genuinely_unnamed_reference_is_still_rejected():
    with pytest.raises(Exception, match="vague"):
        compose.Question.model_validate({
            "qid": "sink",
            "text": "an email thread took most of it. was that yours to do?",
            "options": [
                {"key": "A", "label": "Planned & mine",
                 "payload": {"kind": "sink", "verdict": "planned_mine"}},
                {"key": "B", "label": "One-off, ignore",
                 "payload": {"kind": "sink", "verdict": "one_off"}},
            ],
        })


def test_a_quoted_subject_counts_as_naming_something():
    assert compose._names_something('the "Noshinku 3PL training" thread')


def test_an_address_counts_as_naming_something():
    assert compose._names_something("a thread with ante@blanklabel.team")


def test_a_sentence_of_only_common_words_names_nothing():
    assert not compose._names_something("some work on a few messages today")


def test_an_article_makes_it_vague_no_matter_what_else_is_named():
    """"Mostly Ante Cetinic work + a gmail thread" names a person, but the
    second referent still points at nothing."""
    with pytest.raises(Exception, match="vague"):
        compose.Question.model_validate({
            "qid": "sink",
            "text": "Mostly Ante Cetinic work + a gmail thread — right?",
            "options": [
                {"key": "A", "label": "Planned & mine",
                 "payload": {"kind": "sink", "verdict": "planned_mine"}},
                {"key": "B", "label": "One-off, ignore",
                 "payload": {"kind": "sink", "verdict": "one_off"}},
            ],
        })


# --- a button must read as English (2026-09-15) ---------------------------


def test_a_gains_option_may_not_lead_with_the_internal_kind_name():
    """"Experienced: time with personal" reached his phone: the kind name
    prefixed to a venture primary key. Neither half meant anything to him."""
    for bad in ("Experienced: time with personal", "Learned: something",
                "Progressed: bd-global"):
        with pytest.raises(Exception, match="internal kind name"):
            compose.Option(key="A", label=bad,
                           payload={"kind": "gains", "verdict": "learned",
                                    "gain_kind": "learned"})


def test_a_plain_english_gain_passes():
    o = compose.Option(key="A", label="Time with the family — 2h",
                       payload={"kind": "gains", "verdict": "experienced",
                                "gain_kind": "experienced"})
    assert o.label == "Time with the family — 2h"


def test_the_full_sentence_is_validated_not_the_shortened_button():
    """The button is a deliberate shortening; the validator must judge what he
    actually did, or a plan could hide past the 38th character."""
    evidence = {"learned": [{
        "label": "Signed with three new clients",
        "source_text": "Signed with three new clients this quarter and will "
                       "announce the expansion plan next month",
        "evidence_ids": ["1"],
    }], "progressed": [], "experienced": []}
    card = compose.compose_gains_question(evidence)
    assert [o["label"] for o in card["options"]] == ["Add one → reply"]


def test_a_shortened_button_whose_full_text_is_a_real_gain_is_kept():
    evidence = {"learned": [{
        "label": "Decided to cover Alexan's cost",
        "source_text": "Decided to cover Alexan's cost from personal funds "
                       "rather than asking Greta to carry it",
        "evidence_ids": ["1"],
    }], "progressed": [], "experienced": []}
    card = compose.compose_gains_question(evidence)
    assert "Decided to cover Alexan's cost" in [o["label"] for o in card["options"]]


def test_every_card_says_what_it_is_asking():
    """Ignas read the evening card on 2026-09-15 and said he did not
    understand it. A question he has to decode is a question he will not
    answer, and the whole experiment rests on him answering."""
    gains = compose.compose_gains_question(None)
    assert "moved" in gains["text"].lower()
    assert "tap" in gains["text"].lower(), "it must say a tap is what it wants"
    assert "more than one" in gains["text"].lower(), "multi-select must be stated"

    body = compose.compose_body_mind_question()
    assert "body" in body["text"].lower() and "mind" in body["text"].lower()
    for option in body["options"][:-1]:
        label = option["label"].lower()
        assert "body" in label and "mind" in label, (
            f"{option['label']!r} leaves the reader to work out which is which"
        )


# --- a reply answers the question he tapped (2026-09-15) ------------------


class _ReplyConn:
    def __init__(self, qid):
        self.qid = qid

    def execute(self, sql, params=()):
        qid = self.qid

        class _Cur:
            def fetchone(self_inner):
                return {"qid": qid} if qid else None

        return _Cur()


def test_words_answering_the_body_mind_card_are_filed_as_health_not_a_gain():
    """He tapped "Other → reply" on body/mind and wrote about sleeping eight
    hours and grinding through. That was filed as something that moved today."""
    tags, meta, overrides = answers._classify_reply(
        _ReplyConn("body_mind"), {"id": 78, "kind": "evening"},
        "ok body since I've slept for 8 hours. But mentally I'm grinding through",
    )
    assert "health" in tags and "gain" not in tags
    assert meta["answers_qid"] == "body_mind"


def test_prose_about_his_body_is_never_scored():
    """Reading a mood out of his sentences is what §4.2 forbids, and the card
    promises Iblu records the numbers rather than interpreting them."""
    _, meta, overrides = answers._classify_reply(
        _ReplyConn("body_mind"), {"id": 78, "kind": "evening"}, "completely wiped out",
    )
    assert meta["body"] is None and meta["mind"] is None
    assert meta["scored"] is False
    assert overrides is False


def test_explicit_numbers_win_whatever_he_tapped():
    tags, meta, overrides = answers._classify_reply(
        _ReplyConn("gains"), {"id": 78, "kind": "evening"}, "body 4 mind 2",
    )
    assert tags == ["health", "reply"] and meta == {"body": 4, "mind": 2}
    assert overrides is True


def test_words_answering_the_gains_card_are_still_a_gain():
    tags, meta, _ = answers._classify_reply(
        _ReplyConn("gains"), {"id": 78, "kind": "evening"}, "closed the Opera thread",
    )
    assert "gain" in tags and meta["answers_qid"] == "gains"


def test_an_unprompted_evening_note_is_read_as_a_gain():
    tags, meta, _ = answers._classify_reply(
        _ReplyConn(None), {"id": 78, "kind": "evening"}, "shipped the thing",
    )
    assert "gain" in tags and meta["answers_qid"] is None


def test_an_attention_question_answered_in_words_is_not_a_gain():
    tags, _, _ = answers._classify_reply(
        _ReplyConn("sink"), {"id": 78, "kind": "evening"}, "it was Ante, not me",
    )
    assert tags == ["attention", "reply"]


def test_the_body_mind_escape_shows_the_number_format():
    q = compose.compose_body_mind_question()
    assert "body 4 mind 2" in q["options"][-1]["label"]


# --- 'life' is a work type; 'personal' is a venture that means tooling ----


class _EvidenceConn:
    """Answers _gain_evidence's three queries by matching their SELECT."""

    def __init__(self, learned=None, progressed=None, experienced=None):
        self.answers = {"FROM context_entries": learned or [],
                        "confidence = 'fact'": progressed or [],
                        "attention = 'present'": experienced or []}
        self.sql: list[str] = []

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        self.sql.append(flat)
        rows = next((v for k, v in self.answers.items() if k in flat), [])

        class _Cur:
            def fetchone(self_inner):
                return rows[0] if rows else None

            def fetchall(self_inner):
                return rows

        return _Cur()


def test_building_iblu_is_not_something_he_experienced():
    """A `personal/build/iblu` block is 25 messages of tooling work. The plan
    says an experienced gain is a family or LIFE block, and `life` is a work
    type meaning "non-work"; `personal` is the venture "Own tooling & infra".
    Reading one as the other offered him his own work as a lived experience."""
    from iblu_keeper.pings.runner import _gain_evidence

    conn = _EvidenceConn()
    _gain_evidence(conn, date(2026, 9, 15))
    experienced_sql = next(q for q in conn.sql if "attention = 'present'" in q)
    assert "venture = 'family' OR work_type = 'life'" in experienced_sql
    assert "venture IN ('family', 'personal')" not in experienced_sql


def test_family_time_is_named_as_family_time():
    from iblu_keeper.pings.runner import _lived_words

    assert _lived_words({"venture": "family"}) == "Time with the family"
    assert _lived_words({"venture": "jakusi"}) == "Time on the house"


def test_unnamed_non_work_time_says_so_rather_than_naming_a_code():
    """"Time on your own projects" was a venture primary key in a sentence."""
    text = _lived_words_for({"venture": "blt", "work_type": "life", "project": None})
    assert text == "Time away from work"
    assert "blt" not in text


def _lived_words_for(block):
    from iblu_keeper.pings.runner import _lived_words

    return _lived_words(block)


def test_a_named_project_is_used_when_there_is_one():
    assert _lived_words_for({"venture": "blt", "work_type": "life",
                             "project": "brazil-trip"}) == "Time on brazil-trip"


def test_a_stage_move_counts_as_progress():
    """Unlike a thread "looking closed", a stage move is a fact: he confirmed
    it by tapping, and project_stage_history says who changed it and when."""
    from iblu_keeper.pings.runner import _gain_evidence

    class _Conn(_EvidenceConn):
        def execute(self, sql, params=()):
            flat = " ".join(sql.split())
            self.sql.append(flat)
            if "project_stage_history" in flat:
                rows = [{"id": 5, "project": "machina", "to_stage": "implement",
                         "label": "Implementing fully"}]
            else:
                rows = []

            class _Cur:
                def fetchone(self_inner):
                    return rows[0] if rows else None

                def fetchall(self_inner):
                    return rows

            return _Cur()

    ev = _gain_evidence(_Conn(), date(2026, 9, 15))
    assert ev["progressed"][0]["label"] == "Machina reached 'Implementing fully'"


def test_a_missing_project_registry_does_not_break_the_card():
    from iblu_keeper.pings.runner import _gain_evidence

    class _Conn(_EvidenceConn):
        def execute(self, sql, params=()):
            if "project_stage_history" in " ".join(sql.split()):
                raise RuntimeError('relation "project_stage_history" does not exist')
            return super().execute(sql, params)

    assert _gain_evidence(_Conn(), date(2026, 9, 15))["progressed"] == []
