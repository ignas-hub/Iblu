"""Collector tests: Slack messages I sent — no network, no database.

`FakeConn` stands in for psycopg's connection just enough to exercise
`get_watermark` / `set_state` / `insert_signal` (see
`iblu_keeper.collectors.__init__`): a dict of watermarks keyed by state name,
and a list of signal rows keyed by `source_ref` so `ON CONFLICT DO NOTHING`
can be simulated faithfully.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from iblu_keeper.collectors import slack_sent as S

WORKSPACE = {"alias": "blt", "token": "xoxp-test-token", "label": "blt", "venture": "blt"}


@pytest.fixture(autouse=True)
def _reset_self_id_cache():
    """`_SELF_IDS` is a module-level cache — never let one test see another's."""
    S._SELF_IDS.clear()
    yield
    S._SELF_IDS.clear()


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    """Enough of psycopg.Connection to run the collector's three state calls."""

    def __init__(self):
        self.watermarks: dict[str, datetime] = {}
        self.state_calls: list[tuple] = []
        self.insert_calls: list[dict] = []
        self.inserted_refs: list[str] = []

    def execute(self, sql, params=None):
        s = sql.strip()
        if s.startswith("SELECT watermark"):
            (name,) = params
            row = {"watermark": self.watermarks[name]} if name in self.watermarks else None
            return _Result(row)
        if s.startswith("INSERT INTO collector_state"):
            name, watermark, cursor, error = params
            self.state_calls.append((name, watermark, cursor, error))
            if watermark is not None:
                self.watermarks[name] = watermark
            return _Result(None)
        if s.startswith("INSERT INTO signals"):
            self.insert_calls.append(dict(params))
            ref = params["source_ref"]
            if ref in self.inserted_refs:
                return _Result(None)  # ON CONFLICT (source, source_ref) DO NOTHING
            self.inserted_refs.append(ref)
            return _Result({"id": len(self.inserted_refs)})
        raise AssertionError(f"unexpected SQL: {sql[:60]!r}")


def _match(ts, text="hello there", channel=None, permalink="https://x/p"):
    return {
        "ts": ts,
        "text": text,
        "permalink": permalink,
        "channel": channel or {"id": "C1", "name": "general"},
    }


def _page(matches, pages=1, page=1):
    return {"ok": True, "messages": {"matches": matches, "paging": {"pages": pages, "page": page}}}


def _auth_or_page(user_id, matches):
    def fake_call(token, method, **params):
        if method == "auth.test":
            return {"ok": True, "user_id": user_id}
        assert method == "search.messages"
        assert params["query"].startswith(f"from:<@{user_id}>")
        return _page(matches)

    return fake_call


# --- query construction -----------------------------------------------------


def test_query_is_from_me_after_the_day_before_the_watermark():
    since = datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert S._search_query("U123", since) == "from:<@U123> after:2026-09-09"


def test_self_id_is_resolved_once_and_cached_across_runs(monkeypatch):
    calls = []

    def fake_call(token, method, **params):
        calls.append(method)
        if method == "auth.test":
            return {"ok": True, "user_id": "U999"}
        return _page([])

    monkeypatch.setattr(S, "_call", fake_call)
    conn = FakeConn()

    S.collect(conn, workspace=WORKSPACE)
    assert calls.count("auth.test") == 1

    S.collect(conn, workspace=WORKSPACE)
    assert calls.count("auth.test") == 1  # not called again on the second run


# --- watermark filtering -----------------------------------------------------


def test_messages_older_than_the_watermark_are_dropped(monkeypatch):
    monkeypatch.setattr(
        S,
        "_call",
        _auth_or_page("U1", [_match("1700000000.000100"), _match("1900000000.000200")]),
    )
    conn = FakeConn()
    conn.watermarks["slack_sent:blt"] = datetime.fromtimestamp(1800000000, tz=timezone.utc)

    inserted = S.collect(conn, workspace=WORKSPACE)

    assert inserted == 1
    assert conn.inserted_refs == ["blt:C1:1900000000.000200"]


# --- error propagation --------------------------------------------------------


def test_ok_false_raises_with_the_slack_error(monkeypatch):
    class _Resp:
        def json(self):
            return {"ok": False, "error": "invalid_auth"}

    monkeypatch.setattr(S.requests, "post", lambda *a, **k: _Resp())

    with pytest.raises(RuntimeError, match="invalid_auth"):
        S._call("bad-token", "auth.test")


def test_a_failing_workspace_does_not_write_a_watermark(monkeypatch):
    def fake_call(token, method, **params):
        raise RuntimeError("slack search.messages: ratelimited")

    monkeypatch.setattr(S, "_call", fake_call)
    conn = FakeConn()

    with pytest.raises(RuntimeError, match="ratelimited"):
        S.collect(conn, workspace=WORKSPACE)
    assert conn.state_calls == []


# --- dry run -------------------------------------------------------------


def test_dry_run_counts_but_writes_nothing(monkeypatch):
    monkeypatch.setattr(S, "_call", _auth_or_page("U1", [_match("1900000000.000200")]))
    conn = FakeConn()

    inserted = S.collect(conn, dry=True, workspace=WORKSPACE)

    assert inserted == 1
    assert conn.insert_calls == []
    assert conn.state_calls == []
    assert conn.inserted_refs == []


# --- source_ref stability ----------------------------------------------------


def test_source_ref_is_stable_and_idempotent_across_runs(monkeypatch):
    monkeypatch.setattr(S, "_call", _auth_or_page("U1", [_match("1900000000.000200")]))
    conn = FakeConn()

    first = S.collect(conn, workspace=WORKSPACE)
    assert first == 1
    assert conn.inserted_refs == ["blt:C1:1900000000.000200"]

    # Slack's date-granular `after:` re-returns the same message on the next
    # run; UNIQUE(source, source_ref) must make the re-insert a no-op.
    second = S.collect(conn, workspace=WORKSPACE)
    assert second == 0
    assert conn.inserted_refs == ["blt:C1:1900000000.000200"]


# --- field mapping -------------------------------------------------------


def test_dm_channel_fields():
    counterpart, subject, channel_type = S._channel_fields(
        {"id": "D1", "is_im": True, "user_id": "U42", "name": "ana"}
    )
    assert (counterpart, subject, channel_type) == ("U42", "DM", "im")


def test_public_channel_fields():
    counterpart, subject, channel_type = S._channel_fields({"id": "C1", "name": "general"})
    assert (counterpart, subject, channel_type) == ("general", "#general", "channel")


def test_venture_falls_back_to_the_workspace_default_as_fact(monkeypatch):
    monkeypatch.setattr(
        S, "_call", _auth_or_page("U1", [_match("1900000000.000200", text="totally unrelated")])
    )
    conn = FakeConn()

    S.collect(conn, workspace=WORKSPACE)

    row = conn.insert_calls[0]
    assert row["venture"] == "blt"
    assert row["venture_confidence"] == "fact"


def test_venture_hints_win_over_the_workspace_default_as_inferred(monkeypatch):
    monkeypatch.setattr(
        S,
        "_call",
        _auth_or_page(
            "U1",
            [_match("1900000000.000200", text="Opera weekly sync notes", channel={"id": "C2", "name": "opera"})],
        ),
    )
    conn = FakeConn()

    S.collect(conn, workspace=WORKSPACE)

    row = conn.insert_calls[0]
    assert row["venture"] == "choco"
    assert row["venture_confidence"] == "inferred"
