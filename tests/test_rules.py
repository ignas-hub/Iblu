"""Rules: what Ignas tells Claude, kept in IBLU and handed to every session.

Ignas, 2026-11-24: "I want to have a system where I report what's new and I
want IBLU to be updating things, my instructions." These tests pin the shape:
a row is the truth, corrections supersede and never delete (D9), the Google
Doc is a mirror whose failure never fails the write, and the SessionStart
hook prints nothing rather than anything that could stop a session.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from iblu_keeper.store import rules as store
from iblu_keeper.tools import rules as tools


class _FakeConn:
    """Records the SQL it was given and returns canned rows, one per execute."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.sql: list[str] = []
        self.args: list[tuple] = []
        self.commits = 0

    def execute(self, sql, args=()):
        self.sql.append(" ".join(sql.split()))
        self.args.append(args)
        rows = self.rows.pop(0) if self.rows else []

        class _Cur:
            def fetchone(self_inner):
                return rows[0] if rows else None

            def fetchall(self_inner):
                return rows

        return _Cur()

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_NOW = datetime(2026, 11, 24, 19, 0, tzinfo=timezone.utc)


def _row(i=1, **kw):
    base = {"id": i, "created_at": _NOW, "text": "never ask which remote", "kind": "rule",
            "scope": "all", "context": None, "source": "chat", "status": "active",
            "doc_synced_at": None}
    base.update(kw)
    return base


# --- writes ---------------------------------------------------------------


def test_add_inserts_his_words_and_returns_the_row():
    conn = _FakeConn([[_row()]])
    row = store.add(conn, text="  never ask which remote  ", context=" asked twice today ")
    assert row["id"] == 1
    assert "INSERT INTO rules" in conn.sql[0]
    assert conn.args[0][0] == "never ask which remote"   # trimmed, otherwise verbatim
    assert conn.args[0][3] == "asked twice today"


def test_add_refuses_an_empty_rule_and_unknown_kinds_or_scopes():
    with pytest.raises(store.RuleError):
        store.add(_FakeConn(), text="   ")
    with pytest.raises(store.RuleError):
        store.add(_FakeConn(), text="x", kind="wish")
    with pytest.raises(store.RuleError):
        store.add(_FakeConn(), text="x", scope="everywhere")


def test_retire_marks_and_never_deletes():
    conn = _FakeConn([[{"id": 4}]])
    assert store.retire(conn, 4, "no longer true") is True
    assert "UPDATE rules" in conn.sql[0] and "DELETE" not in conn.sql[0]
    assert "status = 'retired'" in conn.sql[0]


def test_retire_of_an_inactive_rule_changes_nothing():
    assert store.retire(_FakeConn([[]]), 4) is False


def test_edit_supersedes_with_a_forward_pointer():
    """D9: the old wording stays, retired, pointing at the new row."""
    conn = _FakeConn([
        [{"id": 4, "kind": "rule", "scope": "code", "context": "why", "source": "chat"}],  # old
        [_row(9, scope="code", text="push only on the word push")],                      # insert
        [],                                                                              # update old
    ])
    new = store.supersede(conn, 4, text="push only on the word push")
    assert new["id"] == 9
    assert "INSERT INTO rules" in conn.sql[1]
    assert conn.args[1][2] == "code"           # scope inherited from the old row
    assert "superseded_by = %s" in conn.sql[2]
    assert conn.args[2] == ("reworded as #9", 9, 4)
    assert not any("DELETE" in q for q in conn.sql)


def test_edit_of_an_inactive_rule_is_refused():
    with pytest.raises(store.RuleError):
        store.supersede(_FakeConn([[]]), 4, text="x")


# --- reads ----------------------------------------------------------------


def test_active_for_a_scope_includes_the_all_rules():
    conn = _FakeConn([[_row(1), _row(2, scope="code")]])
    rows = store.active(conn, "code")
    assert len(rows) == 2
    assert "scope IN ('all', %s)" in conn.sql[0]
    assert conn.args[0] == ("code",)


def test_active_for_all_has_no_scope_filter():
    conn = _FakeConn([[_row(1)]])
    store.active(conn, "all")
    assert "scope IN" not in conn.sql[0]


# --- rendering (pure) -----------------------------------------------------


def test_a_line_carries_date_text_and_why():
    assert store.line(_row(context="he asked twice")) == \
        "- 2026-11-24: never ask which remote (why: he asked twice)"
    assert store.line(_row(kind="frustration", scope="chat")) == \
        "- 2026-11-24 (frustration, chat): never ask which remote"


def test_render_is_empty_for_nothing_and_a_block_otherwise():
    assert store.render([], "code") == ""
    block = store.render([_row()], "code")
    assert block.startswith("## Rules added through IBLU — for ~/.claude/CLAUDE.md")
    assert "- 2026-11-24: never ask which remote" in block


def test_hook_text_is_empty_when_there_is_nothing_to_say():
    assert store.hook_text([]) == ""


def test_hook_text_names_how_to_record_the_next_one():
    text = store.hook_text([_row()])
    assert "never ask which remote" in text
    assert "rules_write" in text and "--add" in text


def test_doc_line_has_no_markdown_and_names_the_row():
    assert store.doc_line(_row(7, context="twice")) == \
        "[2026-11-24] RULE #7: never ask which remote\n    why: twice"


# --- the Google Doc mirror ------------------------------------------------


def test_doc_mirror_is_skipped_without_a_doc_id(monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(store, "settings", None, raising=False)
    import iblu_keeper.config as cfg

    monkeypatch.setattr(cfg, "settings", replace(cfg.settings, rules_doc_id=""))
    res = store.sync_to_doc(_FakeConn(), _row())
    assert res["status"] == "skipped"


def test_doc_mirror_failure_never_raises(monkeypatch):
    from dataclasses import replace

    import iblu_keeper.config as cfg
    import iblu_keeper.tools.drive as drive

    monkeypatch.setattr(cfg, "settings", replace(cfg.settings, rules_doc_id="doc123"))

    def boom(*a, **k):
        raise RuntimeError("Docs API down")

    monkeypatch.setattr(drive, "gdoc_append", boom)
    conn = _FakeConn([[]])  # any_doc_synced -> nothing yet
    res = store.sync_to_doc(conn, _row())
    assert res["status"] == "failed" and "Docs API down" in res["reason"]


def test_first_mirror_writes_the_section_header_then_never_again(monkeypatch):
    from dataclasses import replace

    import iblu_keeper.config as cfg
    import iblu_keeper.tools.drive as drive

    monkeypatch.setattr(cfg, "settings", replace(cfg.settings, rules_doc_id="doc123"))
    sent = []
    monkeypatch.setattr(drive, "gdoc_append", lambda doc, text: sent.append(text) or {"url": "u"})

    first = _FakeConn([[], []])           # not synced yet, then the mark
    assert store.sync_to_doc(first, _row(1))["status"] == "appended"
    assert sent[0].lstrip().startswith("═══") and "NEW RULES" in sent[0]
    assert "UPDATE rules SET doc_synced_at" in first.sql[1]

    later = _FakeConn([[{"yes": 1}], []])  # already synced once
    store.sync_to_doc(later, _row(2))
    assert "NEW RULES" not in sent[1]


# --- the tools --------------------------------------------------------------


def test_rules_write_add_records_mirrors_and_says_where_it_reaches(monkeypatch):
    conn = _FakeConn([[_row(3, context="asked twice")]])   # what RETURNING gives back
    monkeypatch.setattr(tools, "_conn", lambda: conn)
    monkeypatch.setattr(store, "sync_to_doc", lambda c, r: {"status": "appended", "url": "u"})
    out = tools.rules_write("add", text="never ask which remote", context="asked twice")
    assert out["rule"]["id"] == 3
    assert out["line"] == "- 2026-11-24: never ask which remote (why: asked twice)"
    assert out["doc"]["status"] == "appended"
    assert any("SessionStart" in s for s in out["reaches"]["automatic"])
    assert conn.commits == 2


def test_rules_write_edit_needs_the_rule_being_reworded(monkeypatch):
    monkeypatch.setattr(tools, "_conn", lambda: _FakeConn())
    with pytest.raises(store.RuleError):
        tools.rules_write("edit", text="x")


def test_rules_write_refuses_unknown_actions(monkeypatch):
    monkeypatch.setattr(tools, "_conn", lambda: _FakeConn())
    with pytest.raises(store.RuleError):
        tools.rules_write("delete", rule_id=1)


def test_rules_read_render_names_the_destination(monkeypatch):
    monkeypatch.setattr(tools, "_conn", lambda: _FakeConn([[_row()]]))
    out = tools.rules_read("render", scope="chat")
    assert out["paste_into"].startswith("claude.ai Settings")
    assert out["block"].startswith("## Rules added through IBLU")
    assert out["count"] == 1


def test_rules_read_dates_are_strings(monkeypatch):
    monkeypatch.setattr(tools, "_conn", lambda: _FakeConn([[_row()]]))
    out = tools.rules_read("list")
    assert out["rules"][0]["created_at"] == _NOW.isoformat()


# --- the hook ---------------------------------------------------------------


def test_hook_prints_nothing_without_a_database(monkeypatch, capsys):
    monkeypatch.setattr(store, "_load_active", lambda scope: [])
    assert store.main(["--hook"]) == 0
    assert capsys.readouterr().out == ""


def test_hook_prints_session_start_json_with_the_rules(monkeypatch, capsys):
    monkeypatch.setattr(store, "_load_active", lambda scope: [_row()])
    assert store.main(["--hook"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "never ask which remote" in payload["hookSpecificOutput"]["additionalContext"]


def test_render_cli_prints_the_block(monkeypatch, capsys):
    monkeypatch.setattr(store, "_load_active", lambda scope: [_row()])
    assert store.main(["--render", "code"]) == 0
    assert "~/.claude/CLAUDE.md" in capsys.readouterr().out
