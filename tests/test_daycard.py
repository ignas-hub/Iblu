"""`jobs.daycard` — the evening day card (HANDOFF, this session).

Fakes only for rendering/parsing logic; the rolled-back-transaction pattern
from `tests/test_tap_roundtrip.py` for anything that writes `blocks` or
`context_entries` — the behaviour that matters (supersede, never delete, a
confirmed block is never touched again) *is* the SQL, so it is proven against
the real statements, not a hand-rolled fake that could quietly diverge from
them (HANDOFF §22's rule about a fake that reimplements the logic under
test). Nothing here sends a Chat message or applies migration 014.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from iblu_keeper import db
from iblu_keeper.jobs import daycard as D

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URL not set — database tests skipped by design",
)

UTC = timezone.utc
TZ = ZoneInfo("Europe/Zagreb")


def _b(id: int, h1: int, m1: int, h2: int, m2: int, **over) -> dict:
    """A fake `live_blocks` row, on 2026-09-24 (CEST, UTC+2)."""
    base = dict(
        id=id,
        starts_at=datetime(2026, 9, 24, h1, m1, tzinfo=TZ).astimezone(UTC),
        ends_at=datetime(2026, 9, 24, h2, m2, tzinfo=TZ).astimezone(UTC),
        venture=None, work_type=None, project=None,
        attention="present", confidence="inferred", evidence=[],
        reasoning="", intent_event_id=None, intent_title=None, source="analyst",
    )
    base.update(over)
    return base


def _render(blocks: list[dict]) -> list[dict]:
    entries = [D._entry(b) for b in sorted(blocks, key=lambda b: b["starts_at"])]
    entries = D._merge_adjacent_entries(entries)
    entries = D._cap_lines(entries)
    return D.render_lines(entries, TZ)


# ---------------------------------------------------------------------------
# rendering: letters, local times, human labels, merged lines, the three marks
# ---------------------------------------------------------------------------


def test_present_line_shows_local_times_and_human_names_not_codes():
    lines = _render([_b(1, 9, 0, 11, 15, venture="deadlift", project="machina")])
    assert len(lines) == 1
    assert lines[0]["letter"] == "A"
    assert lines[0]["mark"] is None
    assert lines[0]["line"] == "A  09:00–11:15  Deadlift · Machina"


def test_unknown_line_gets_a_question_mark_and_says_nothing_recorded():
    lines = _render([_b(1, 11, 15, 13, 0, attention="ambiguous")])
    assert lines[0]["mark"] == "?"
    assert lines[0]["line"] == "A  11:15–13:00  ?  nothing recorded"


def test_unknown_line_with_a_calendar_title_names_it():
    lines = _render([_b(1, 11, 0, 12, 0, attention="ambiguous", intent_title="Womanizer alignment")])
    assert lines[0]["mark"] == "?"
    assert "Womanizer alignment" in lines[0]["text"]


def test_displaced_line_gets_the_displaced_mark():
    lines = _render([_b(
        1, 14, 0, 15, 30, attention="displaced", venture="blt", intent_title="Client sync",
    )])
    assert lines[0]["mark"] == "⟂"
    assert "Blank Label" in lines[0]["text"]
    assert "Client sync" in lines[0]["text"]


def test_family_inferred_line_gets_the_assumed_mark():
    lines = _render([_b(
        1, 18, 0, 19, 30, attention="present", venture="family", intent_title="Futbolas",
        reasoning='90 min · nothing else recorded during "Futbolas" — assumed you went',
    )])
    assert lines[0]["mark"] == "~"
    assert lines[0]["text"] == 'assumed you went to "Futbolas"'


def test_ventures_are_named_in_english_never_by_code():
    lines = _render([
        _b(1, 8, 0, 9, 0, venture="blt"),
        _b(2, 9, 0, 10, 0, venture="jakusi"),
    ])
    texts = [l["text"] for l in lines]
    assert "Blank Label" in texts[0]
    assert "the house" in texts[1]
    assert "blt" not in texts[0] and "jakusi" not in texts[1]


def test_adjacent_lines_saying_the_same_thing_are_merged():
    blocks = [
        _b(1, 11, 0, 11, 30, attention="ambiguous"),
        _b(2, 11, 30, 12, 0, attention="ambiguous"),
    ]
    lines = _render(blocks)
    assert len(lines) == 1
    assert lines[0]["block_ids"] == [1, 2]
    assert lines[0]["line"] == "A  11:00–12:00  ?  nothing recorded"


def test_adjacent_lines_saying_different_things_are_not_merged():
    blocks = [
        _b(1, 11, 0, 11, 30, attention="ambiguous", intent_title="Standup"),
        _b(2, 11, 30, 12, 0, attention="ambiguous", intent_title="1:1 with Ante"),
    ]
    lines = _render(blocks)
    assert len(lines) == 2


def test_lines_are_lettered_in_chronological_order():
    blocks = [
        _b(2, 10, 0, 11, 0, venture="deadlift"),
        _b(1, 9, 0, 10, 0, venture="blt"),
    ]
    lines = _render(blocks)
    assert [l["letter"] for l in lines] == ["A", "B"]
    assert "Blank Label" in lines[0]["text"]
    assert "Deadlift" in lines[1]["text"]


def test_more_than_ten_blocks_are_capped_to_ten_lines():
    base = datetime(2026, 9, 24, 7, 0, tzinfo=UTC)
    cycle = ["blt", "deadlift", "choco", "jakusi", "family", "personal", "gostellar"]
    blocks = []
    for i in range(16):
        start = base + timedelta(minutes=7 * i)
        end = start + timedelta(minutes=7)
        blocks.append(dict(
            id=i, starts_at=start, ends_at=end, venture=cycle[i % len(cycle)],
            work_type=None, project=None, attention="present", confidence="inferred",
            evidence=[], reasoning="", intent_event_id=None, intent_title=None,
            source="analyst",
        ))
    lines = D.render_lines(D._cap_lines(D._merge_adjacent_entries(
        [D._entry(b) for b in blocks]
    )), TZ)
    assert len(lines) <= D.MAX_LINES
    # capping folds spans together; it never drops a minute of the day.
    assert lines[0]["starts_at"] == blocks[0]["starts_at"]
    assert lines[-1]["ends_at"] == blocks[-1]["ends_at"]


def test_reply_example_is_drawn_from_this_specific_day():
    lines = _render([
        _b(1, 9, 0, 10, 0, attention="displaced", venture="deadlift", project="machina",
           intent_title="standup"),
        _b(2, 18, 0, 19, 0, attention="present", venture="family", intent_title="Futbolas",
           reasoning='60 min · nothing else recorded during "Futbolas" — assumed you went'),
    ])
    example = D.reply_example(lines)
    assert "was Deadlift · Machina" in example
    assert "I skipped Futbolas" in example


def test_render_card_text_lists_every_line_and_the_reply_hint():
    lines = _render([_b(1, 9, 0, 11, 15, venture="deadlift", project="machina")])
    text = D.render_card_text(lines)
    assert "A  09:00–11:15  Deadlift · Machina" in text
    assert "Reply like:" in text


# ---------------------------------------------------------------------------
# the job itself: an empty day sends nothing
# ---------------------------------------------------------------------------


def test_empty_day_sends_nothing(monkeypatch, capsys):
    monkeypatch.setattr(D, "settings", SimpleNamespace(
        use_mock=False, database_url="postgres://fake", iblu_timezone="Europe/Zagreb",
    ))
    monkeypatch.setattr(D.db, "is_configured", lambda: True)

    class _Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(D.db, "get_conn", lambda: _Ctx())
    monkeypatch.setattr(D, "build_day_lines", lambda conn, on: [])

    sent = {"called": False}
    monkeypatch.setattr(D, "send", lambda *a, **k: sent.__setitem__("called", True))
    monkeypatch.setattr(D, "_upsert_day_card", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not write a day_cards row for an empty day")
    ))

    rc = D.run(on=date(2026, 9, 24), dry=False)
    assert rc == 0
    assert sent["called"] is False
    assert capsys.readouterr().out == ""


def test_dry_run_prints_the_card_and_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(D, "settings", SimpleNamespace(
        use_mock=False, database_url="postgres://fake", iblu_timezone="Europe/Zagreb",
    ))
    monkeypatch.setattr(D.db, "is_configured", lambda: True)

    class _Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(D.db, "get_conn", lambda: _Ctx())
    lines = _render([_b(1, 9, 0, 11, 15, venture="deadlift", project="machina")])
    monkeypatch.setattr(D, "build_day_lines", lambda conn, on: lines)
    monkeypatch.setattr(D, "_upsert_day_card", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("--dry must write nothing")
    ))
    monkeypatch.setattr(D, "send", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("--dry must send nothing")
    ))

    rc = D.run(on=date(2026, 9, 24), dry=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Deadlift · Machina" in out
    assert "Reply like:" in out


# ---------------------------------------------------------------------------
# "All correct" — the important tap
# ---------------------------------------------------------------------------


def _insert_block(conn, on, start, end, **over):
    fields = dict(
        venture=None, work_type=None, project=None, attention="present",
        confidence="inferred", reasoning="test block", intent_title=None,
        source="analyst",
    )
    fields.update(over)
    return conn.execute(
        """
        INSERT INTO blocks
            (local_date, starts_at, ends_at, venture, work_type, project,
             attention, confidence, evidence, reasoning, intent_title, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '[]'::jsonb, %s, %s, %s)
        RETURNING id
        """,
        (
            on, start, end, fields["venture"], fields["work_type"], fields["project"],
            fields["attention"], fields["confidence"], fields["reasoning"],
            fields["intent_title"], fields["source"],
        ),
    ).fetchone()["id"]


@requires_db
def test_all_correct_supersedes_every_non_confirmed_block_and_writes_one_entry():
    import psycopg

    with db.get_conn() as conn:
        with conn.transaction() as tx:
            on = date(2031, 1, 15)
            guess = _insert_block(
                conn, on,
                datetime(2031, 1, 15, 9, 0, tzinfo=UTC), datetime(2031, 1, 15, 10, 0, tzinfo=UTC),
                venture="blt", source="analyst",
            )
            already_confirmed = _insert_block(
                conn, on,
                datetime(2031, 1, 15, 10, 0, tzinfo=UTC), datetime(2031, 1, 15, 11, 0, tzinfo=UTC),
                venture="deadlift", source="human",
            )

            confirmed = D.confirm_all(conn, on)
            assert confirmed == 1

            guess_row = conn.execute(
                "SELECT superseded_by FROM blocks WHERE id = %s", (guess,),
            ).fetchone()
            assert guess_row["superseded_by"] is not None

            new_row = conn.execute(
                "SELECT venture, source, confidence, starts_at, ends_at FROM blocks WHERE id = %s",
                (guess_row["superseded_by"],),
            ).fetchone()
            assert new_row["venture"] == "blt"
            assert new_row["source"] == "human"
            assert new_row["confidence"] == "fact"

            # never touches an already-confirmed block
            confirmed_row = conn.execute(
                "SELECT superseded_by FROM blocks WHERE id = %s", (already_confirmed,),
            ).fetchone()
            assert confirmed_row["superseded_by"] is None

            entries = conn.execute(
                "SELECT count(*) AS n FROM context_entries WHERE source_ref = %s",
                (f"daycard:{on.isoformat()}:confirm",),
            ).fetchone()
            assert entries["n"] == 1

            row = conn.execute(
                "SELECT tags FROM context_entries WHERE source_ref = %s",
                (f"daycard:{on.isoformat()}:confirm",),
            ).fetchone()
            assert row["tags"] == ["ping", "daycard", "confirmed"]

            raise psycopg.Rollback(tx)


@requires_db
def test_a_second_all_correct_tap_is_idempotent():
    import psycopg

    with db.get_conn() as conn:
        with conn.transaction() as tx:
            on = date(2031, 1, 16)
            _insert_block(
                conn, on,
                datetime(2031, 1, 16, 9, 0, tzinfo=UTC), datetime(2031, 1, 16, 10, 0, tzinfo=UTC),
                venture="blt",
            )

            first = D.confirm_all(conn, on)
            second = D.confirm_all(conn, on)
            assert first == 1
            assert second == 0

            entries = conn.execute(
                "SELECT count(*) AS n FROM context_entries WHERE source_ref = %s",
                (f"daycard:{on.isoformat()}:confirm",),
            ).fetchone()
            assert entries["n"] == 1

            blocks = conn.execute(
                "SELECT count(*) AS n FROM blocks WHERE local_date = %s", (on,),
            ).fetchone()
            # one original + one confirmation = two, never three
            assert blocks["n"] == 2

            raise psycopg.Rollback(tx)


class _FakeDayCardRow(dict):
    """`fetchone()` needs dict-style access; a plain dict already gives it."""


class _FakeConn:
    """Just enough of `psycopg.Connection` for `record_daycard_tap`'s own
    lookup — no supersede logic reimplemented here, so it cannot mask a real
    regression (HANDOFF §22)."""

    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        return self

    def fetchone(self):
        return self._row


def test_record_daycard_tap_raises_for_a_gone_card():
    from iblu_keeper.pings.answers import UnknownPing

    with pytest.raises(UnknownPing):
        D.record_daycard_tap(_FakeConn(None), 999, "A")


# ---------------------------------------------------------------------------
# reply parsing — line-letter, time-range, attendance, unparseable, unknown venture
# ---------------------------------------------------------------------------

VENTURES = ["blt", "choco", "deadlift", "jakusi", "family", "personal"]
WORK_TYPES = ["sales", "client", "delivery", "people", "finance", "build", "admin", "life"]


@requires_db
def test_line_letter_correction_writes_a_new_fact_block(monkeypatch):
    import psycopg

    with db.get_conn() as conn:
        with conn.transaction() as tx:
            on = date(2031, 1, 17)
            s = datetime(2031, 1, 17, 14, 0, tzinfo=UTC)
            e = datetime(2031, 1, 17, 15, 30, tzinfo=UTC)
            block_id = _insert_block(conn, on, s, e, attention="ambiguous")

            day_card = {
                "id": 1, "local_date": on,
                "lines": [{
                    "letter": "C", "block_ids": [block_id],
                    "starts_at": s.isoformat(), "ends_at": e.isoformat(),
                    "venture": None, "project": None, "mark": "?", "title": None,
                }],
            }
            monkeypatch.setattr(D, "parse_daycard_reply", lambda *a, **k: D.DayCardCorrections(
                corrections=[D.DayCardCorrection(line="C", venture="deadlift", project="machina")]
            ))

            result = D.apply_daycard_reply(conn, day_card, "C was Deadlift · Machina", VENTURES, WORK_TYPES)
            assert result == {"applied": 1, "rejected": 0, "parsed": True}

            row = conn.execute(
                "SELECT superseded_by FROM blocks WHERE id = %s", (block_id,),
            ).fetchone()
            assert row["superseded_by"] is not None
            new_row = conn.execute(
                "SELECT venture, project, source, confidence FROM blocks WHERE id = %s",
                (row["superseded_by"],),
            ).fetchone()
            assert new_row["venture"] == "deadlift"
            assert new_row["project"] == "machina"
            assert new_row["source"] == "human"
            assert new_row["confidence"] == "fact"

            raise psycopg.Rollback(tx)


@requires_db
def test_time_range_correction_resolves_without_a_letter(monkeypatch):
    import psycopg

    monkeypatch.setattr(D, "settings", SimpleNamespace(iblu_timezone="Europe/Zagreb"))

    with db.get_conn() as conn:
        with conn.transaction() as tx:
            on = date(2031, 1, 18)  # January: Zagreb is CET, UTC+1, no DST
            s = datetime(2031, 1, 18, 12, 0, tzinfo=UTC)   # 13:00 local
            e = datetime(2031, 1, 18, 13, 0, tzinfo=UTC)   # 14:00 local
            block_id = _insert_block(conn, on, s, e, attention="ambiguous")

            day_card = {
                "id": 2, "local_date": on,
                "lines": [{
                    "letter": "B", "block_ids": [block_id],
                    "starts_at": s.isoformat(), "ends_at": e.isoformat(),
                    "venture": None, "project": None, "mark": "?", "title": None,
                }],
            }
            monkeypatch.setattr(D, "parse_daycard_reply", lambda *a, **k: D.DayCardCorrections(
                corrections=[D.DayCardCorrection(
                    line=None, starts_at="13:00", ends_at="14:00", venture="blt",
                )]
            ))

            result = D.apply_daycard_reply(
                conn, day_card, "13:00-14:00 was Blank Label", VENTURES, WORK_TYPES,
            )
            assert result["applied"] == 1

            row = conn.execute(
                "SELECT superseded_by FROM blocks WHERE id = %s", (block_id,),
            ).fetchone()
            new_row = conn.execute(
                "SELECT venture FROM blocks WHERE id = %s", (row["superseded_by"],),
            ).fetchone()
            assert new_row["venture"] == "blt"

            raise psycopg.Rollback(tx)


@requires_db
def test_attended_false_leaves_the_family_span_unknown(monkeypatch):
    import psycopg

    with db.get_conn() as conn:
        with conn.transaction() as tx:
            on = date(2031, 1, 19)
            s = datetime(2031, 1, 19, 17, 0, tzinfo=UTC)
            e = datetime(2031, 1, 19, 18, 30, tzinfo=UTC)
            block_id = _insert_block(
                conn, on, s, e, venture="family", attention="present",
                reasoning='90 min · nothing else recorded during "Futbolas" — assumed you went',
                intent_title="Futbolas",
            )

            day_card = {
                "id": 3, "local_date": on,
                "lines": [{
                    "letter": "D", "block_ids": [block_id],
                    "starts_at": s.isoformat(), "ends_at": e.isoformat(),
                    "venture": "family", "project": None, "mark": "~", "title": "Futbolas",
                }],
            }
            monkeypatch.setattr(D, "parse_daycard_reply", lambda *a, **k: D.DayCardCorrections(
                corrections=[D.DayCardCorrection(line="D", attended=False)]
            ))

            result = D.apply_daycard_reply(conn, day_card, "I skipped Futbolas", VENTURES, WORK_TYPES)
            assert result["applied"] == 1

            row = conn.execute(
                "SELECT superseded_by FROM blocks WHERE id = %s", (block_id,),
            ).fetchone()
            new_row = conn.execute(
                "SELECT venture, work_type, project, attention, confidence FROM blocks WHERE id = %s",
                (row["superseded_by"],),
            ).fetchone()
            assert new_row["venture"] is None
            assert new_row["work_type"] is None
            assert new_row["project"] is None
            assert new_row["attention"] == "ambiguous"
            assert new_row["confidence"] == "fact"

            raise psycopg.Rollback(tx)


def test_unparseable_reply_applies_nothing_and_records_an_observation(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "iblu_keeper.store.observations.record_safe",
        lambda **kw: recorded.append(kw),
    )
    monkeypatch.setattr(
        D, "parse_daycard_reply",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("not valid json")),
    )

    day_card = {"id": 9, "local_date": date(2026, 9, 24), "lines": []}
    result = D.apply_daycard_reply(None, day_card, "uhh what do you mean?", VENTURES, WORK_TYPES)

    assert result == {"applied": 0, "rejected": 0, "parsed": False}
    assert recorded, "expected an observation to be recorded"
    assert recorded[0]["kind"] == "daycard_reply_unparsed"
    assert recorded[0]["severity"] == "info"


def test_correction_naming_an_unknown_venture_is_refused():
    on = date(2026, 9, 24)
    line = {
        "letter": "C", "block_ids": [123],
        "starts_at": datetime(2026, 9, 24, 9, 0, tzinfo=UTC).isoformat(),
        "ends_at": datetime(2026, 9, 24, 10, 0, tzinfo=UTC).isoformat(),
        "venture": None, "project": None, "mark": None, "title": None,
    }
    correction = D.DayCardCorrection(line="C", venture="not-a-real-venture")

    # conn is never touched: the venture check happens before any database
    # access, so `None` here proves it, rather than merely not exercising it.
    ok, reason = D._apply_one_correction(
        None, on, [line], correction, set(VENTURES), set(WORK_TYPES),
    )
    assert ok is False
    assert "unknown venture" in reason
