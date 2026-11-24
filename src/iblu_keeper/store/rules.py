"""The rules Ignas gives Claude, and the three places they have to reach.

    python -m iblu_keeper.store.rules                 # the active rules, readable
    python -m iblu_keeper.store.rules --render code   # paste-ready block for ~/.claude/CLAUDE.md
    python -m iblu_keeper.store.rules --hook          # SessionStart hook JSON (never fails)

Ignas, 2026-11-24: "I want to have a system where I report what's new, and I
want IBLU to be updating things, my instructions." The failure it replaces: a
rule stated in one chat stayed in that chat. Getting it to the next session
meant a Google Doc, then `~/.claude/CLAUDE.md` on every machine, then the
claude.ai preference and Project fields, by hand, and so it rarely happened.

Now the `rules` table is the truth, and it is reached three ways:

  * **In.** `rules_write` (tools/rules.py) from any Claude chat or session:
    "update my rules at iblu: never ask me which remote" becomes a row.
  * **Out, automatically.** The SessionStart hook on this box prints the
    active rules into every Claude Code session (`--hook`), so no file under
    `~/.claude` needs editing here. Each `add` is also appended to the
    Google Doc named by `RULES_DOC_ID`, so the document he shares stays
    current without anyone retyping.
  * **Out, on demand.** `rules_read render` returns the block for a
    destination that has no API: claude.ai account preferences and Project
    instructions are pasted by hand, so the render is what he pastes.

D9: a correction supersedes and never deletes. `edit` inserts the new text
and retires the old row with `superseded_by`; `retire` marks and keeps.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

logger = logging.getLogger("iblu_keeper.store.rules")

KINDS = ("rule", "frustration", "fact")
SCOPES = ("all", "code", "chat", "project")

# What a scope's render is for, in the words the destination uses.
SCOPE_TITLES = {
    "all": "every place Claude reads rules from",
    "code": "~/.claude/CLAUDE.md (Claude Code, every repo on the machine)",
    "chat": "claude.ai Settings > Profile > personal preferences",
    "project": "a claude.ai Project's Instructions",
}

# The heading the Google Doc gets the first time a rule is appended. Everything
# under it is machine-appended; the blocks above it are his to revise.
DOC_SECTION_HEADER = (
    "\n\n═══════════════════════════════════════════════════════════════════\n"
    "NEW RULES - added through IBLU (\"update my rules at iblu\")\n"
    "═══════════════════════════════════════════════════════════════════\n"
    "Each line below was added from a Claude chat or session. Fold them into the "
    "blocks above when you next revise them; until then they also apply as written.\n"
)


class RuleError(ValueError):
    """Bad input to a rules action. The message says what was expected."""


def _check(kind: str, scope: str) -> None:
    if kind not in KINDS:
        raise RuleError(f"kind must be one of {KINDS}, got {kind!r}")
    if scope not in SCOPES:
        raise RuleError(f"scope must be one of {SCOPES}, got {scope!r}")


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
def add(
    conn,
    *,
    text: str,
    kind: str = "rule",
    scope: str = "all",
    context: str | None = None,
    source: str = "chat",
) -> dict:
    """Insert one rule and return its row."""
    text = (text or "").strip()
    if not text:
        raise RuleError("text is empty — the rule has to say something")
    _check(kind, scope)
    row = conn.execute(
        """
        INSERT INTO rules (text, kind, scope, context, source)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, created_at, text, kind, scope, context, source, status
        """,
        (text, kind, scope, (context or "").strip() or None, source),
    ).fetchone()
    return dict(row)


def retire(conn, rule_id: int, note: str | None = None) -> bool:
    """Mark a rule retired. Returns False if it was not active. Never deletes."""
    row = conn.execute(
        """
        UPDATE rules
           SET status = 'retired', retired_at = now(), retire_note = %s
         WHERE id = %s AND status = 'active'
        RETURNING id
        """,
        (note, rule_id),
    ).fetchone()
    return row is not None


def supersede(conn, rule_id: int, *, text: str, context: str | None = None) -> dict:
    """Replace a rule's wording: insert the new text, retire the old row with a
    forward pointer. The history stays readable in `history`."""
    old = conn.execute(
        "SELECT id, kind, scope, context, source FROM rules WHERE id = %s AND status = 'active'",
        (rule_id,),
    ).fetchone()
    if old is None:
        raise RuleError(f"rule {rule_id} is not an active rule")
    new = add(conn, text=text, kind=old["kind"], scope=old["scope"],
              context=context if context is not None else old["context"],
              source=old["source"])
    conn.execute(
        """
        UPDATE rules
           SET status = 'retired', retired_at = now(),
               retire_note = %s, superseded_by = %s
         WHERE id = %s
        """,
        (f"reworded as #{new['id']}", new["id"], rule_id),
    )
    return new


def mark_doc_synced(conn, rule_id: int) -> None:
    conn.execute("UPDATE rules SET doc_synced_at = now() WHERE id = %s", (rule_id,))


def any_doc_synced(conn) -> bool:
    """Has the Google Doc already received its machine-appended section?"""
    row = conn.execute(
        "SELECT 1 AS yes FROM rules WHERE doc_synced_at IS NOT NULL LIMIT 1"
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def active(conn, scope: str = "all") -> list[dict]:
    """Active rules that apply to `scope`: its own plus the `all` ones."""
    if scope not in SCOPES:
        raise RuleError(f"scope must be one of {SCOPES}, got {scope!r}")
    if scope == "all":
        rows = conn.execute(
            """
            SELECT id, created_at, text, kind, scope, context, source, doc_synced_at
              FROM rules WHERE status = 'active' ORDER BY created_at, id
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, created_at, text, kind, scope, context, source, doc_synced_at
              FROM rules WHERE status = 'active' AND scope IN ('all', %s)
             ORDER BY created_at, id
            """,
            (scope,),
        ).fetchall()
    return [dict(r) for r in rows]


def history(conn, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, created_at, text, kind, scope, context, source, status,
               retired_at, retire_note, superseded_by, doc_synced_at
          FROM rules ORDER BY created_at DESC, id DESC LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# rendering — pure functions, no database
# --------------------------------------------------------------------------- #
def _day(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)[:10] if value else ""


def _label(row: dict) -> str:
    kind = row.get("kind", "rule")
    scope = row.get("scope", "all")
    bits = []
    if kind != "rule":
        bits.append(kind)
    if scope != "all":
        bits.append(scope)
    return f" ({', '.join(bits)})" if bits else ""


def line(row: dict) -> str:
    """One rule as one line: date, text, and the why if there is one."""
    s = f"- {_day(row.get('created_at'))}{_label(row)}: {row['text']}"
    if row.get("context"):
        s += f" (why: {row['context']})"
    return s


def doc_line(row: dict) -> str:
    """The same rule as it is appended to the Google Doc (no markdown)."""
    s = f"[{_day(row.get('created_at'))}] {row.get('kind', 'rule').upper()}"
    if row.get("scope", "all") != "all":
        s += f" for {row['scope']}"
    s += f" #{row['id']}: {row['text']}"
    if row.get("context"):
        s += f"\n    why: {row['context']}"
    return s


def render(rows: list[dict], scope: str = "all") -> str:
    """A paste-ready block for one destination. Empty string when there is
    nothing, so a caller can tell 'no rules' from 'a rendering problem'."""
    if not rows:
        return ""
    head = f"## Rules added through IBLU — for {SCOPE_TITLES.get(scope, scope)}"
    body = "\n".join(line(r) for r in rows)
    return f"{head}\n{body}\n"


def hook_text(rows: list[dict]) -> str:
    """What a Claude Code session is told at start. Short on purpose: it is
    spent from the session's context before the first prompt."""
    if not rows:
        return ""
    lines = [
        "# Rules Ignas added through IBLU (apply them; they supersede older habits)",
        "",
    ]
    lines += [line(r) for r in rows]
    lines += [
        "",
        f"{len(rows)} active. All of them: `python -m iblu_keeper.store.rules`. "
        "When he states a new rule in this session, record it with "
        "`python -m iblu_keeper.store.rules --add \"...\" --context \"...\"` "
        "(or the rules_write tool) so every other session gets it too.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# the Google Doc
# --------------------------------------------------------------------------- #
def sync_to_doc(conn, row: dict) -> dict:
    """Append one rule to the doc named by RULES_DOC_ID. Never raises: the row
    is the truth and the doc is a mirror. Returns what happened."""
    from ..config import settings

    doc_id = settings.rules_doc_id
    if not doc_id:
        return {"status": "skipped", "reason": "RULES_DOC_ID is not set in .env"}
    try:
        from ..tools.drive import gdoc_append

        text = doc_line(row)
        if not any_doc_synced(conn):
            text = DOC_SECTION_HEADER + "\n" + text
        res = gdoc_append(doc_id, text)
        mark_doc_synced(conn, row["id"])
        return {"status": "appended", "url": res.get("url", "")}
    except Exception as exc:  # noqa: BLE001 — a mirror must not fail the write
        logger.warning("rule %s not mirrored to the doc: %s", row.get("id"), exc)
        return {"status": "failed", "reason": str(exc)[:200]}


# --------------------------------------------------------------------------- #
# CLI — the hook, and a way to add from a Claude Code session
# --------------------------------------------------------------------------- #
def _load_active(scope: str) -> list[dict]:
    """Active rules, or nothing. Never raises: this feeds a SessionStart hook,
    and a hook that can stop a session is worse than none."""
    try:
        from .. import db
        from ..config import settings

        if settings.use_mock or not db.is_configured():
            return []
        try:
            with db.get_conn() as conn:
                return active(conn, scope)
        finally:
            db.close_pool()
    except Exception:  # noqa: BLE001 — silence is the contract here
        return []


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m iblu_keeper.store.rules")
    p.add_argument("--hook", action="store_true", help="SessionStart hook JSON; prints nothing on any failure")
    p.add_argument("--render", choices=SCOPES, help="paste-ready block for one destination")
    p.add_argument("--scope", choices=SCOPES, default="all")
    p.add_argument("--add", metavar="TEXT", help="record a new rule from this session")
    p.add_argument("--kind", choices=KINDS, default="rule")
    p.add_argument("--context", default=None, help="what happened, in his words")
    p.add_argument("--retire", type=int, metavar="ID")
    p.add_argument("--note", default=None)
    args = p.parse_args(argv)

    if args.hook:
        text = hook_text(_load_active("code"))
        if text:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": text}}))
        return 0

    if args.add or args.retire:
        from .. import db

        with db.get_conn() as conn:
            if args.add:
                row = add(conn, text=args.add, kind=args.kind, scope=args.scope,
                          context=args.context, source="code")
                conn.commit()
                mirror = sync_to_doc(conn, row)
                conn.commit()
                print(f"recorded #{row['id']}: {line(row)}")
                print(f"doc: {mirror['status']}" + (f" ({mirror.get('reason') or mirror.get('url')})"))
            else:
                ok = retire(conn, args.retire, args.note)
                conn.commit()
                print(f"retired #{args.retire}" if ok else f"#{args.retire} was not active")
        return 0

    rows = _load_active(args.render or args.scope)
    if args.render:
        print(render(rows, args.render) or f"no active rules for {args.render}")
        return 0
    if not rows:
        print("no active rules (or no database)")
        return 0
    for r in rows:
        print(f"#{r['id']} {line(r)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
