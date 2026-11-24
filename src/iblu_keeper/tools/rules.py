"""`rules_read` / `rules_write` — Ignas's rules for Claude, from any chat.

Ignas, 2026-11-24: "when I'm in some chat and type 'update my rules at iblu',
this would trigger IBLU tool that would register my frustration /
instructions." Two tools, by side-effect class (HANDOFF §12):

* `rules_read`  — list the active rules, render the paste-ready block for one
  destination, or show the history. Never writes.
* `rules_write` — add, reword or retire a rule. It writes a row and mirrors it
  to the Google Doc; it never sends anything to a person, so it is not in the
  asking class. Corrections supersede, never delete (D9).

The store (`store/rules.py`) does the work and the rendering; this module is
the validation and the shape of the answer.
"""

from __future__ import annotations

import logging

from ..store import rules as store

logger = logging.getLogger("iblu_keeper.tools.rules")

READ_ACTIONS = ("list", "render", "history")
WRITE_ACTIONS = ("add", "edit", "retire")


def _conn():
    from .. import db

    return db.get_conn()


def _public(row: dict) -> dict:
    out = dict(row)
    for k in ("created_at", "retired_at", "doc_synced_at"):
        if out.get(k) is not None and hasattr(out[k], "isoformat"):
            out[k] = out[k].isoformat()
    return out


def rules_read(action: str = "list", scope: str = "all", limit: int = 100) -> dict:
    if action not in READ_ACTIONS:
        raise store.RuleError(f"action must be one of {READ_ACTIONS}, got {action!r}")
    with _conn() as conn:
        if action == "history":
            rows = store.history(conn, limit=limit)
            return {"action": action, "count": len(rows), "rules": [_public(r) for r in rows]}
        rows = store.active(conn, scope)
    if action == "render":
        return {
            "action": action, "scope": scope, "count": len(rows),
            "paste_into": store.SCOPE_TITLES.get(scope, scope),
            "block": store.render(rows, scope),
            "note": ("Paste the block into the destination named in paste_into. "
                     "Claude Code sessions on the IBLU box already get these rules "
                     "at start; claude.ai preference and Project fields have no API, "
                     "so they are pasted by hand."),
        }
    return {"action": action, "scope": scope, "count": len(rows),
            "rules": [_public(r) for r in rows]}


def rules_write(
    action: str,
    text: str | None = None,
    kind: str = "rule",
    scope: str = "all",
    context: str | None = None,
    rule_id: int | None = None,
    note: str | None = None,
) -> dict:
    if action not in WRITE_ACTIONS:
        raise store.RuleError(f"action must be one of {WRITE_ACTIONS}, got {action!r}")

    with _conn() as conn:
        if action == "add":
            row = store.add(conn, text=text or "", kind=kind, scope=scope,
                            context=context, source="chat")
            conn.commit()
            mirror = store.sync_to_doc(conn, row)
            conn.commit()
            return {
                "action": "add", "rule": _public(row), "line": store.line(row),
                "doc": mirror,
                "reaches": _reaches(scope),
            }
        if action == "edit":
            if rule_id is None:
                raise store.RuleError("edit needs rule_id (the rule being reworded)")
            row = store.supersede(conn, rule_id, text=text or "", context=context)
            conn.commit()
            mirror = store.sync_to_doc(conn, row)
            conn.commit()
            return {"action": "edit", "superseded": rule_id, "rule": _public(row),
                    "line": store.line(row), "doc": mirror, "reaches": _reaches(row["scope"])}
        if rule_id is None:
            raise store.RuleError("retire needs rule_id")
        ok = store.retire(conn, rule_id, note)
        conn.commit()
        return {"action": "retire", "rule_id": rule_id,
                "status": "retired" if ok else "was_not_active"}
    raise AssertionError("unreachable")


def _reaches(scope: str) -> dict:
    """Where this rule now lands, truthfully: what is automatic and what is not."""
    return {
        "automatic": [
            "every Claude Code session on the IBLU box, from its next start (SessionStart hook)",
            "the Google Doc named by RULES_DOC_ID (appended under 'NEW RULES')",
        ],
        "by_hand": [
            "claude.ai account preferences: paste rules_read(action='render', scope='chat')",
            "a Project's instructions: paste rules_read(action='render', scope='project')",
            "other machines' ~/.claude/CLAUDE.md: paste rules_read(action='render', scope='code')",
        ] if scope in ("all", "chat", "project", "code") else [],
    }
