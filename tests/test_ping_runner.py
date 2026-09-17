"""`pings.runner`'s gathering of today's `maybe`-attendance family intents for
the evening `attended` card (item 4).

Fakes only — no database, no network. `_maybe_events_today` and
`_answered_instance_keys` are exercised directly; `load_intents` and
`classify_missing` are monkeypatched so nothing here touches Google or
Anthropic.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from iblu_keeper.analyst import blocks as B
from iblu_keeper.analyst import intents as I
from iblu_keeper.pings import runner

UTC = timezone.utc
DAY = date(2026, 9, 15)

VENTURES = [{"code": "family", "label": "Family & personal life"}]


def _iv(**over) -> B.Interval:
    base = dict(
        start=datetime(2026, 9, 15, 17, 0, tzinfo=UTC),
        end=datetime(2026, 9, 15, 18, 30, tzinfo=UTC),
        event_id="fut1", title="Futbolas", venture="family",
        calendar_id="fam-cal", account="blt",
        needs_commitment_check=True, is_context=True, attendance="maybe",
    )
    base.update(over)
    return B.Interval(**base)


def test_maybe_events_today_keeps_only_maybe_family_intents(monkeypatch):
    keep = _iv()
    not_maybe = _iv(event_id="fut2", attendance="his", is_context=False)
    not_family = _iv(event_id="fut3", venture="blt")
    no_event_id = _iv(event_id=None)

    monkeypatch.setattr(B, "load_intents", lambda conn, s, e: [keep, not_maybe, not_family, no_event_id])
    monkeypatch.setattr(I, "classify_missing", lambda conn, intents, ventures: intents)

    events = runner._maybe_events_today(object(), DAY, VENTURES)
    assert len(events) == 1
    assert events[0]["title"] == "Futbolas"
    assert events[0]["instance_key"] == I.instance_key("fam-cal", "fut1", DAY)
    assert events[0]["start"] == keep.start and events[0]["end"] == keep.end


def test_maybe_events_today_never_raises_when_loading_fails(monkeypatch):
    def boom(conn, s, e):
        raise RuntimeError("calendar unreadable")

    monkeypatch.setattr(B, "load_intents", boom)
    assert runner._maybe_events_today(object(), DAY, VENTURES) == []


def test_maybe_events_today_never_raises_when_the_classifier_fails(monkeypatch):
    monkeypatch.setattr(B, "load_intents", lambda conn, s, e: [_iv()])

    def boom(conn, intents, ventures):
        raise RuntimeError("classifier unavailable")

    monkeypatch.setattr(I, "classify_missing", boom)
    assert runner._maybe_events_today(object(), DAY, VENTURES) == []


class _FakeAnsweredConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return self._rows

    def rollback(self):
        pass


def test_answered_instance_keys_reads_back_what_the_query_returns():
    conn = _FakeAnsweredConn([{"instance_key": "k1"}, {"instance_key": "k2"}])
    assert runner._answered_instance_keys(conn, ["k1", "k2", "k3"]) == {"k1", "k2"}


def test_answered_instance_keys_is_empty_for_no_candidates():
    conn = _FakeAnsweredConn([{"instance_key": "k1"}])
    assert runner._answered_instance_keys(conn, []) == set()


def test_answered_instance_keys_never_raises_on_a_bad_query():
    class _Boom:
        def execute(self, sql, params=None):
            raise RuntimeError("db hiccup")

    assert runner._answered_instance_keys(_Boom(), ["k1"]) == set()
