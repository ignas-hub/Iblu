"""The intent classifier (`analyst/intents.py`) and `INTENT_CALENDARS` parsing.

No network, no live database — the model call is monkeypatched and the cache
is a plain dict-backed fake connection. Fakes only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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
        (key, title, venture, is_work, confidence, is_commit, commit_conf,
         attendance, attendance_confidence, model) = self._params
        self.rows[key] = {
            "venture": venture, "is_work": is_work, "confidence": confidence,
            "is_ignas_commitment": is_commit, "commitment_confidence": commit_conf,
            "attendance": attendance, "attendance_confidence": attendance_confidence,
        }
        self.put_calls += 1


class _FakeCacheConnPre013:
    """Simulates a database that has not applied migration 013 yet — any
    statement mentioning the new `attendance` columns fails, the same way a
    real "column does not exist" error would. Used to prove `_cache_get`/
    `_cache_put` degrade gracefully rather than losing the classification.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.insert_attempts = 0

    def execute(self, sql, params=None):
        if "attendance" in sql:
            raise Exception(
                'column "attendance" of relation "intent_labels" does not exist'
            )
        self._sql, self._params = sql, params or ()
        if "INSERT" in sql:
            self.insert_attempts += 1
            (key, title, venture, is_work, confidence, is_commit, commit_conf,
             model) = self._params
            self.rows[key] = {
                "venture": venture, "is_work": is_work, "confidence": confidence,
                "is_ignas_commitment": is_commit, "commitment_confidence": commit_conf,
            }
        return self

    def fetchone(self):
        if "SELECT" not in getattr(self, "_sql", ""):
            return None
        key = self._params[0]
        return self.rows.get(key)

    def rollback(self):
        pass


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


# --- attendance: his / maybe / not_his (a boolean cannot hold "sometimes I
#     go, sometimes I don't") ---------------------------------------------


def test_high_confidence_his_flips_context_to_false(monkeypatch):
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Client dinner")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "attendance": "his", "attendance_confidence": "high"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.is_context is False
    assert target.attendance == "his"


def test_low_confidence_his_stays_context_and_attendance_is_left_unset(monkeypatch):
    """Low confidence 'his' is treated exactly like 'not_his' — the default
    stays conservative, same doctrine as before."""
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Ignas Zagreb 10-14")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "attendance": "his", "attendance_confidence": "low"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.is_context is True
    assert target.attendance is None


def test_not_his_high_confidence_stays_context_and_is_flagged(monkeypatch):
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Greta nicoj")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "attendance": "not_his", "attendance_confidence": "high"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.is_context is True
    assert target.attendance == "not_his"


@pytest.mark.parametrize("confidence", ["high", "low"])
def test_maybe_is_applied_at_any_confidence_and_never_a_commitment(monkeypatch, confidence):
    """'maybe' is already the cautious answer — high or low confidence, it
    never turns into a commitment (is_context stays True)."""
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Futbolas")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "attendance": "maybe", "attendance_confidence": confidence}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.is_context is True
    assert target.attendance == "maybe"


def test_locked_context_is_never_unlocked_by_the_classifier(monkeypatch):
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                is_context_locked=True, title="Ignas LT Fri-Sun")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "attendance": "his", "attendance_confidence": "high"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.is_context is True
    assert target.attendance is None


def test_a_whereabouts_marker_produces_no_block_even_at_low_confidence_his(monkeypatch):
    """End to end: a low-confidence 'his' from the classifier must still
    leave the event as context, so `build()` creates no block for it."""
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Ignas Zagreb 10-14", event_id="wa1")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "attendance": "his", "attendance_confidence": "low"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    blocks = B.build([], [target])
    assert blocks == []


def test_a_maybe_event_produces_no_block_of_its_own(monkeypatch):
    """End to end: 'maybe' never creates a block on its own (no evidence, no
    watched-elsewhere signal to pass the family-inference guard-rail)."""
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Futbolas", event_id="fut1")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "attendance": "maybe", "attendance_confidence": "high"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    blocks = B.build([], [target])
    assert blocks == []


# --- INTENT_MAYBE_TITLES: a deterministic override -------------------------


def test_intent_maybe_titles_overrides_the_model(monkeypatch):
    """The env override wins even when the classifier confidently says 'his'
    — Ignas's own knowledge of "sometimes I go, sometimes I don't" outranks
    a model guess."""
    monkeypatch.setenv("INTENT_MAYBE_TITLES", "futbolas,roditeljsk,roditelsk")
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Futbolas")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "attendance": "his", "attendance_confidence": "high"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.attendance == "maybe"
    assert target.is_context is True


def test_intent_maybe_titles_matches_case_insensitively_and_by_substring(monkeypatch):
    """`roditeljsk` (a substring, lower case) must still match "RODITELJSKU
    sastanak u školi" (upper case, a longer word) — comma-separated
    SUBSTRINGS, case-insensitive, per the env var's documented format."""
    monkeypatch.setenv("INTENT_MAYBE_TITLES", "roditeljsk")
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="RODITELJSKU sastanak u školi", event_id="school1")
    I._apply_maybe_title_override([target])
    assert target.attendance == "maybe"
    assert target.is_context is True


def test_intent_maybe_titles_works_even_without_an_api_key(monkeypatch):
    """The override must hold even when the classifier cannot run at all —
    it is a deterministic fact about the title, not a model opinion."""
    monkeypatch.setenv("INTENT_MAYBE_TITLES", "futbolas")

    class _NoKey:
        anthropic_api_key = ""

    monkeypatch.setattr(I, "settings", _NoKey())
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Futbolas")
    I.classify_missing(None, [target], VENTURES)
    assert target.attendance == "maybe"
    assert target.is_context is True


def test_intent_maybe_titles_empty_is_a_no_op(monkeypatch):
    monkeypatch.delenv("INTENT_MAYBE_TITLES", raising=False)
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Futbolas")
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: [
            {"venture": "family", "is_work": False, "confidence": "high",
             "attendance": "his", "attendance_confidence": "high"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert target.attendance == "his"


def test_intent_maybe_titles_never_unlocks_a_locked_context(monkeypatch):
    monkeypatch.setenv("INTENT_MAYBE_TITLES", "ignas lt")
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                is_context_locked=True, title="Ignas LT Fri-Sun")
    I._apply_maybe_title_override([target])
    assert target.is_context is True
    assert target.attendance is None


# --- cache: a NULL attendance is a cache miss for anything that needed the
#     commitment check, but not for a plain venture-only intent -------------


def test_a_null_attendance_cached_row_is_reclassified(monkeypatch):
    """A label cached under the old boolean (no `attendance` column, or a row
    written before migration 013) must be re-asked, not trusted forever."""
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    key = I.event_key("cal1", "e1", "Futbolas")
    conn.rows[key] = {
        "venture": "family", "is_work": False, "confidence": "high",
        "is_ignas_commitment": True, "commitment_confidence": "high",
        "attendance": None, "attendance_confidence": None,
    }
    target = iv(venture="family", needs_commitment_check=True, is_context=True,
                title="Futbolas", event_id="e1", calendar_id="cal1")
    called = []
    monkeypatch.setattr(
        I, "_call",
        lambda targets, ventures: called.append(1) or [
            {"venture": "family", "is_work": False, "confidence": "high",
             "attendance": "maybe", "attendance_confidence": "high"}
        ],
    )
    I.classify_missing(conn, [target], VENTURES)
    assert called, "a NULL-attendance cache row must trigger a fresh classification"
    assert target.attendance == "maybe"


def test_a_null_attendance_cached_row_is_still_a_hit_for_a_plain_venture_intent(monkeypatch):
    """Attendance never applied to a plain (non-commitment-check) intent in
    the first place — a NULL there is not a miss."""
    conn = _FakeCacheConn()
    _fake_conn_execute_dispatch(monkeypatch)
    key = I.event_key("cal1", "e1", "Something")
    conn.rows[key] = {
        "venture": "deadlift", "is_work": True, "confidence": "high",
        "is_ignas_commitment": None, "commitment_confidence": None,
        "attendance": None, "attendance_confidence": None,
    }

    def boom(*a, **k):
        raise AssertionError("a plain venture intent's cache hit must not call the model")

    monkeypatch.setattr(I, "_call", boom)
    target = iv(venture=None, needs_commitment_check=False, title="Something",
                event_id="e1", calendar_id="cal1")
    I.classify_missing(conn, [target], VENTURES)
    assert target.venture == "deadlift"


# --- degrading gracefully when migration 013 has not been applied yet ------


def test_cache_put_degrades_when_attendance_columns_are_missing():
    conn = _FakeCacheConnPre013()
    result = {
        "venture": "family", "is_work": False, "confidence": "high",
        "is_ignas_commitment": True, "commitment_confidence": "high",
        "attendance": "his", "attendance_confidence": "high",
    }
    I._cache_put(conn, "k1", "Futbolas", result, "model-x")
    assert conn.insert_attempts == 1, "only the fallback (old-shape) insert should succeed"
    assert conn.rows["k1"]["venture"] == "family"
    assert "attendance" not in conn.rows["k1"]


def test_cache_get_degrades_to_a_miss_when_attendance_column_is_missing():
    conn = _FakeCacheConnPre013()
    assert I._cache_get(conn, "nonexistent") is None


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
