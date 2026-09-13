"""Governance layer tests (plan §1.4) — priorities, baselines, gain rules.

Everything here runs against a `FakeConn` double, never a real database: the
three store functions are plain `SELECT`s, and their SQL shape (which tags,
which DISTINCT ON key) matters more than round-tripping through Postgres.
`requires_db` (see test_migrations.py) is not used at all in this file —
there is nothing here that needs it.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from iblu_keeper.store import governance


# --------------------------------------------------------------------------
# FakeConn — a minimal double that dispatches on distinguishing SQL substrings
# --------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """Records every query and returns canned rows keyed by SQL shape.

    Dispatch is by substring rather than by call order, so a test can hand
    the same conn to `current_priorities`, `current_baselines` and
    `gain_rules` in any order without the fake caring which ran first.
    """

    def __init__(
        self,
        *,
        brief: dict | None = None,
        priorities: list[dict] | None = None,
        baselines: list[dict] | None = None,
        gain: dict | None = None,
    ):
        self.calls: list[str] = []
        self.used = False
        self._brief = brief
        self._priorities = priorities or []
        self._baselines = baselines or []
        self._gain = [gain] if gain else []

    def execute(self, sql: str, params: tuple = ()):
        self.used = True
        self.calls.append(sql)
        if "context_brief" in sql:
            return _Result([self._brief] if self._brief else [])
        if "'priority' = ANY(tags)" in sql:
            return _Result(self._priorities)
        if "'baseline' = ANY(tags)" in sql:
            return _Result(self._baselines)
        if "gain:practice-rules" in sql:
            return _Result(self._gain)
        raise AssertionError(f"FakeConn got an unexpected query:\n{sql}")


def _row(venture, content, source_ref=None, **extra):
    return {
        "id": uuid.uuid4(),
        "venture": venture,
        "content": content,
        "created_at": datetime(2026, 9, 13, 10, 40, tzinfo=timezone.utc),
        **({"source_ref": source_ref} if source_ref is not None else {}),
        **extra,
    }


# --------------------------------------------------------------------------
# current_priorities / current_baselines / gain_rules
# --------------------------------------------------------------------------


def test_current_priorities_shape_and_query():
    rows = [_row("blt", "blt priority text"), _row("gostellar", "gostellar priority")]
    conn = FakeConn(priorities=rows)

    out = governance.current_priorities(conn)

    assert len(out) == 2
    for item in out:
        assert set(item) == {"id", "venture", "content", "created_at"}
        assert isinstance(item["id"], str)  # normalised, not a raw UUID
        assert isinstance(item["created_at"], str)  # ISO-8601, not a datetime

    sql = conn.calls[0]
    assert "DISTINCT ON (venture)" in sql
    assert "type = 'decision'" in sql
    assert "'priority' = ANY(tags)" in sql
    assert "superseded_by IS NULL" in sql


def test_current_baselines_shape_includes_source_ref():
    rows = [
        _row(None, "gostellar baseline, no venture yet", source_ref="baseline:gostellar"),
        _row("personal", "hated tasks A-H", source_ref="baseline:personal:hated-tasks"),
        _row("personal", "health + gain", source_ref="baseline:personal:health-gain"),
    ]
    conn = FakeConn(baselines=rows)

    out = governance.current_baselines(conn)

    assert len(out) == 3
    for item in out:
        assert set(item) == {"id", "venture", "content", "created_at", "source_ref"}

    # Both personal baselines survive — this is exactly what DISTINCT ON
    # (venture, source_ref) buys over DISTINCT ON (venture) alone, and the
    # venture=None GoStellar row is not dropped either.
    personal = [r for r in out if r["venture"] == "personal"]
    assert {r["source_ref"] for r in personal} == {
        "baseline:personal:hated-tasks",
        "baseline:personal:health-gain",
    }
    assert any(r["venture"] is None for r in out)

    sql = conn.calls[0]
    assert "DISTINCT ON (venture, source_ref)" in sql
    assert "type = 'fact'" in sql
    assert "'baseline' = ANY(tags)" in sql


def test_gain_rules_returns_the_row_when_present():
    gain = _row(None, "Log every gain within an hour.", source_ref="gain:practice-rules")
    conn = FakeConn(gain=gain)

    out = governance.gain_rules(conn)

    assert out is not None
    assert out["content"] == "Log every gain within an hour."
    assert out["source_ref"] == "gain:practice-rules"
    assert isinstance(out["id"], str)
    sql = conn.calls[0]
    assert "gain:practice-rules" in sql
    assert "superseded_by IS NULL" in sql


def test_gain_rules_returns_none_when_absent():
    conn = FakeConn(gain=None)
    assert governance.gain_rules(conn) is None


# --------------------------------------------------------------------------
# as_prompt_block
# --------------------------------------------------------------------------


def test_as_prompt_block_includes_every_ventures_priority_and_stays_short():
    ventures = ["blt", "choco", "deadlift", "jakusi", "family", "personal", "gostellar"]
    priorities = [
        {"venture": v, "content": f"{v} yearly priority " + "x" * 250} for v in ventures
    ]
    baselines = [
        {"venture": v, "content": f"{v} baseline " + "y" * 250} for v in ventures
    ]
    rules = {"content": "Log gains within the hour. No backdating."}

    block = governance.as_prompt_block(priorities, baselines, rules)

    for v in ventures:
        assert v in block
    assert "Log gains within the hour. No backdating." in block
    # 7 ventures x ~200-char excerpts, twice (priorities + baselines), plus
    # headers and the rules line — comfortably under a few KB, nowhere near
    # the length the unbounded 250-char filler content would have produced.
    assert len(block) < 4000


def test_as_prompt_block_handles_nothing_set():
    block = governance.as_prompt_block([], [], None)
    assert "(none set)" in block
    assert "PRIORITIES:" in block
    assert "BASELINES:" in block
    assert "GAIN RULES:" in block


# --------------------------------------------------------------------------
# get_context wiring
# --------------------------------------------------------------------------


class _LiveSettings:
    use_mock = False
    dry_run = False


class _MockSettings:
    use_mock = True
    dry_run = True


def test_get_context_key_order_and_governance_payload(monkeypatch):
    from iblu_keeper.tools import context as ctx

    monkeypatch.setattr(ctx, "settings", _LiveSettings())
    monkeypatch.setattr(ctx.db, "load_mission", lambda: ("mission text", "sha-abc"))
    monkeypatch.setattr(ctx.db, "read_mission_file", lambda: "mission text")
    monkeypatch.setattr(ctx.db, "mission_sha", lambda text: "sha-abc")
    # get_summary does its own separate `with db.get_conn()` block with a
    # different set of queries (signals/pings/work_log) — stubbing it keeps
    # this test about get_context's own wiring, not a second FakeConn dialect.
    monkeypatch.setattr(ctx, "get_summary", lambda window: {"stub": True, "window": window})

    priorities_rows = [_row("blt", "blt priority")]
    baselines_rows = [_row("blt", "blt baseline", source_ref="baseline:blt")]
    gain_row = _row(None, "gain rules text", source_ref="gain:practice-rules")

    fake_conn = FakeConn(
        brief={"content": "the brief"},
        priorities=priorities_rows,
        baselines=baselines_rows,
        gain=gain_row,
    )

    @contextmanager
    def fake_get_conn():
        yield fake_conn

    monkeypatch.setattr(ctx.db, "get_conn", fake_get_conn)

    out = ctx.get_context("1d")

    assert list(out.keys()) == [
        "mission",
        "mission_sha",
        "mission_stale",
        "priorities",
        "baselines",
        "gain_rules",
        "brief",
        "summary",
    ]
    assert out["mission"] == "mission text"
    assert out["priorities"][0]["venture"] == "blt"
    assert out["baselines"][0]["source_ref"] == "baseline:blt"
    assert out["gain_rules"]["source_ref"] == "gain:practice-rules"
    assert out["brief"] == "the brief"
    assert out["summary"] == {"stub": True, "window": "1d"}


def test_get_context_mock_mode_opens_no_connection(monkeypatch):
    from iblu_keeper.tools import context as ctx

    monkeypatch.setattr(ctx, "settings", _MockSettings())

    fake_conn = FakeConn()

    @contextmanager
    def fake_get_conn():
        yield fake_conn

    monkeypatch.setattr(ctx.db, "get_conn", fake_get_conn)

    out = ctx.get_context("1d")

    assert out == {"status": "mock"}
    assert fake_conn.used is False, "mock mode must never touch the connection"
