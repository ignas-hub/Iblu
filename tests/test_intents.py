"""The intent classifier (`analyst/intents.py`) and `INTENT_CALENDARS` parsing.

No network, no live database — the model call is monkeypatched and the cache
is a plain dict-backed fake connection. Fakes only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from iblu_keeper.analyst import blocks as B
from iblu_keeper.analyst import intents as I
from iblu_keeper.config import Settings

UTC = timezone.utc

VENTURES = [
    {"code": "blt", "label": "Blank Label Team"},
    {"code": "family", "label": "Family & personal life"},
    {"code": "deadlift", "label": "Deadlift.io"},
]


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 16, hour, minute, tzinfo=UTC)


def iv(**over) -> B.Interval:
    base = dict(
        start=at(10, 0), end=at(11, 0), event_id="e1", title="Futbolas",
        venture=None, calendar_id="cal1", account="blt",
    )
    base.update(over)
    return B.Interval(**base)


class _FakeCacheConn:
    """A stand-in for a psycopg connection that only ever sees `intent_labels`
    queries, backed by a plain dict — no real database anywhere."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.put_calls = 0

    def execute(self, sql, params=None):
        self._sql = sql
        self._params = params or ()
        return self

    def fetchone(self):
        if "SELECT" not in self._sql:
            return None
        key = self._params[0]
        return self.rows.get(key)

    def rollback(self):
        pass

    def _insert(self):
        (key, title, venture, is_work, confidence, is_commit, commit_conf, model) = self._params
        self.rows[key] = {
            "venture": venture, "is_work": is_work, "confidence": confidence,
            "is_ignas_commitment": is_commit, "commitment_confidence": commit_conf,
        }
        self.put_calls += 1


def _fake_conn_execute_dispatch(monkeypatch):
    """`_cache_put`'s INSERT and `_cache_get`'s SELECT share `.execute`; make
    the fake actually insert on an INSERT statement."""
    real_execute = _FakeCacheConn.execute

    def execute(self, sql, params=None):
        real_execute(self, sql, params)
        if "INSERT" in sql:
            self._insert()
        return self

    monkeypatch.setattr(_FakeCacheConn, "execute", execute)


# --- venture classification -------------------------------------------------


def test_low_confidence_classification_is_not_applied(monkeypatch):
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture=None, title="Random thing")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "low",
             "is_ignas_commitment": None, "commitment_confidence": None}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.venture is None


def test_an_invalid_venture_code_is_ignored(monkeypatch):
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture=None, title="Something")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "not-a-real-venture", "is_work": True, "confidence": "high",
             "is_ignas_commitment": None, "commitment_confidence": None}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.venture is None


def test_a_high_confidence_valid_venture_is_applied(monkeypatch):
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture=None, title="Emory dentist")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "is_ignas_commitment": None, "commitment_confidence": None}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.venture == "family"


def test_cache_hit_skips_the_model_call(monkeypatch):
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    key = I.event_key("cal1", "e1", "Futbolas")
    conn.rows[key] = {
        "venture": "family", "is_work": False, "confidence": "high",
        "is_ignas_commitment": None, "commitment_confidence": None,
    }

    def boom(*a, **k):
        raise AssertionError("the model must not be called on a cache hit")

    monkeypatch.setattr(I, "_call", boom)
    target = iv(venture=None, title="Futbolas", event_id="e1", calendar_id="cal1")
    I.classify_missing(conn, [target], VENTURES)
    assert target.venture == "family"


def test_a_classifier_failure_leaves_intents_unclassified(monkeypatch):
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture=None)

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(I, "_call", boom)
    result = I.classify_missing(conn, [target], VENTURES)
    assert result[0].venture is None  # never raised, left as-is


def test_no_api_key_is_a_silent_no_op(monkeypatch):
    class _NoKey:
        anthropic_api_key = ""

    monkeypatch.setattr(I, "settings", _NoKey())
    target = iv(venture=None)
    result = I.classify_missing(None, [target], VENTURES)
    assert result[0].venture is None


# --- is_ignas_commitment: whereabouts markers must stay context ------------


def test_low_confidence_commitment_true_does_not_flip_context(monkeypatch):
    """Even if the model SAYS is_ignas_commitment=true, low confidence must
    never be trusted — a whereabouts marker misread as a commitment would
    turn a quiet afternoon into invented family time."""
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Ignas Zagreb 10-14")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "is_ignas_commitment": True, "commitment_confidence": "low"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.is_context is True


def test_high_confidence_commitment_flips_context_to_false(monkeypatch):
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Futbolas")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "is_ignas_commitment": True, "commitment_confidence": "high"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.is_context is False


def test_locked_context_is_never_unlocked_by_the_classifier(monkeypatch):
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                is_context_locked=True, title="Ignas LT Fri-Sun")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "is_ignas_commitment": True, "commitment_confidence": "high"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.is_context is True


def test_a_whereabouts_marker_produces_no_block_even_at_low_confidence_true(monkeypatch):
    """End to end: a low-confidence 'yes' from the classifier must still
    leave the event as context, so `build()` creates no block for it."""
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Ignas Zagreb 10-14", event_id="wa1")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "is_ignas_commitment": True, "commitment_confidence": "low"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    blocks = B.build([], [target])
    assert blocks == []


# --- INTENT_CALENDARS parsing (item A) --------------------------------------


def test_both_real_calendars_parse_to_venture_family():
    s = Settings(
        google_accounts="blt",
        intent_calendars=(
            "family:family17712020332388721989@group.calendar.google.com|blt,"
            "family:ignas.ignas@gmail.com|blt"
        ),
    )
    parsed = s.intent_calendars_parsed
    assert len(parsed) == 2
    assert all(p["venture"] == "family" for p in parsed)
    assert all(p["account"] == "blt" for p in parsed)
    assert parsed[0]["calendar_id"] == "family17712020332388721989@group.calendar.google.com"
    assert parsed[1]["calendar_id"] == "ignas.ignas@gmail.com"


def test_intent_calendars_account_suffix_defaults_to_primary():
    s = Settings(google_accounts="blt,choco", intent_calendars="family:abc@group.calendar.google.com")
    [entry] = s.intent_calendars_parsed
    assert entry["account"] == "blt"
