"""`record_tap` against a real Postgres, inside a transaction that never commits.

Found in review 2026-09-13: every existing test of the tap path used a
`FakeConn` that matched on the first few words of each statement and then
applied its OWN hand-written supersede logic. If the real SQL lost
`AND superseded_by IS NULL` — silently re-superseding a dead row and corrupting
a chain — or lost `id <> %s`, every one of those tests would still pass,
because the fake substitutes its own correctness for the code's.

So these run the actual statements against the actual database and then roll
everything back: nothing here survives the test, and `signals` and `blocks` are
untouched afterwards.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from iblu_keeper import db

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URL not set — database tests skipped by design",
)

UTC = timezone.utc


@pytest.fixture
def live_answers(monkeypatch):
    """The tap writer with mock mode off and a known signing secret."""
    from iblu_keeper.pings import answers, tokens

    class _Live:
        use_mock = False
        dry_run = False
        ping_signing_secret = "roundtrip-test-secret"
        iblu_timezone = "Europe/Zagreb"

    monkeypatch.setattr(answers, "settings", _Live())
    return answers, tokens, _Live.ping_signing_secret


def _questions(qid: str, keys: list[str], payload_extra: dict | None = None) -> list[dict]:
    return [{
        "qid": qid,
        "text": "Was that yours to do?",
        "options": [
            {
            "key": k,
            "label": f"option {k}",
            "payload": {
                "kind": qid, "verdict": "planned_mine" if qid == "sink" else "other",
                **(payload_extra or {}),
            },
            }
            for k in keys
        ],
    }]


def _make_ping(conn, questions: list[dict]) -> int:
    from psycopg.types.json import Jsonb

    now = datetime.now(UTC)
    return conn.execute(
        """
        INSERT INTO pings (kind, local_date, window_start, window_end,
                       covers_from, covers_to, questions, composer, status)
        VALUES ('test', %s, %s, %s, %s, %s, %s, 'fallback', 'sent')
        RETURNING id
        """,
        (now.date(), now - timedelta(hours=1), now, now - timedelta(hours=6), now,
         Jsonb(questions)),
    ).fetchone()["id"]


def _live_answers(conn, ping_id: int, qid: str) -> list[dict]:
    return conn.execute(
        """
        SELECT id, meta, superseded_by FROM context_entries
         WHERE source = 'ping' AND source_ref LIKE %s AND superseded_by IS NULL
         ORDER BY created_at
        """,
        (f"ping:{ping_id}:{qid}%",),
    ).fetchall()


@requires_db
def test_retapping_a_question_leaves_exactly_one_live_answer(live_answers):
    """The invariant the whole supersede chain exists for — proven against the
    real UPDATE, not against a fake that reimplements it."""
    import psycopg

    answers, tokens, secret = live_answers
    with db.get_conn() as conn:
        # `conn.transaction()` swallows Rollback itself — that is how it is
        # meant to be used, and it means nothing here reaches the database.
        with conn.transaction() as tx:
            ping_id = _make_ping(conn, _questions("sink", ["A", "B", "C"]))

            for key in ("A", "B", "C"):
                answers.record_tap(
                    conn,
                    tokens.make_token(ping_id=ping_id, qid="sink", key=key, secret=secret),
                )

            live = _live_answers(conn, ping_id, "sink")
            assert len(live) == 1, f"{len(live)} live answers after three taps"
            assert live[0]["meta"]["choice_key"] == "C", "the last tap should win"

            superseded = conn.execute(
                "SELECT count(*) AS n FROM context_entries "
                " WHERE source_ref LIKE %s AND superseded_by IS NOT NULL",
                (f"ping:{ping_id}:sink%",),
            ).fetchone()["n"]
            assert superseded == 2, "the earlier answers must be kept, not deleted"
            raise psycopg.Rollback(tx)


@requires_db
def test_the_same_option_tapped_twice_still_leaves_one_live_answer(live_answers):
    """A double-tap is the ordinary case — the page itself says 'try again'."""
    import psycopg

    answers, tokens, secret = live_answers
    with db.get_conn() as conn:
        # `conn.transaction()` swallows Rollback itself — that is how it is
        # meant to be used, and it means nothing here reaches the database.
        with conn.transaction() as tx:
            ping_id = _make_ping(conn, _questions("sink", ["A"]))
            token = tokens.make_token(ping_id=ping_id, qid="sink", key="A", secret=secret)
            answers.record_tap(conn, token)
            answers.record_tap(conn, token)
            assert len(_live_answers(conn, ping_id, "sink")) == 1
            raise psycopg.Rollback(tx)


@requires_db
def test_gains_taps_do_not_supersede_each_other_against_real_sql(live_answers):
    """Three things happening in one day are three gains, not one answer
    revised twice — the one question kind that is NOT a chain."""
    import psycopg

    answers, tokens, secret = live_answers
    questions = [{
        "qid": "gains",
        "text": "What moved today?",
        "options": [
            {"key": "A", "label": "Closed the Opera thread",
             "payload": {"kind": "gains", "verdict": "learned", "gain_kind": "learned"}},
            {"key": "B", "label": "Machina approvals shipped",
             "payload": {"kind": "gains", "verdict": "progressed", "gain_kind": "progressed"}},
            {"key": "C", "label": "A full day with Leo",
             "payload": {"kind": "gains", "verdict": "experienced", "gain_kind": "experienced"}},
        ],
        "multi": True,
    }]
    with db.get_conn() as conn:
        # `conn.transaction()` swallows Rollback itself — that is how it is
        # meant to be used, and it means nothing here reaches the database.
        with conn.transaction() as tx:
            ping_id = _make_ping(conn, questions)
            for key in ("A", "B", "C"):
                answers.record_tap(
                    conn,
                    tokens.make_token(ping_id=ping_id, qid="gains", key=key, secret=secret),
                )
            live = _live_answers(conn, ping_id, "gains")
            assert len(live) == 3, f"{len(live)} live gains — they superseded each other"
            # `meta.kind` per plan §4.1 — the three kinds a gain can be.
            kinds = {r["meta"].get("kind") for r in live}
            assert kinds == {"learned", "progressed", "experienced"}
            raise psycopg.Rollback(tx)


@requires_db
def test_a_forged_token_writes_nothing(live_answers):
    import psycopg

    answers, tokens, secret = live_answers
    with db.get_conn() as conn:
        # `conn.transaction()` swallows Rollback itself — that is how it is
        # meant to be used, and it means nothing here reaches the database.
        with conn.transaction() as tx:
            ping_id = _make_ping(conn, _questions("sink", ["A"]))
            token = tokens.make_token(ping_id=ping_id, qid="sink", key="A", secret=secret)
            with pytest.raises(tokens.InvalidToken):
                answers.record_tap(conn, token[:-3] + "aaa")
            assert _live_answers(conn, ping_id, "sink") == []
            raise psycopg.Rollback(tx)
