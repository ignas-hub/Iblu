"""What IBLU noticed, handed to the next Claude Code session on arrival.

    python -m iblu_keeper.jobs.session_brief           # hook JSON, for SessionStart
    python -m iblu_keeper.jobs.session_brief --text     # the same thing, readable

`store/observations.py` records what IBLU caught. `jobs/watchdog.py` announces
the part a human can act on. This is the third consumer, and the one that
closes the loop: it puts the open findings in front of a Claude Code session at
the moment the session starts, so the errors get *fixed* rather than re-read.

It exists because of the shape of the failure it replaces. A monitoring alert
went to Ignas's phone at 04:45 saying a block rested on automated mail rather
than human work. There was nothing he could do with that. The finding needed a
code change, so it needed to reach whoever writes the code — and it only did
because he happened to screenshot it. Meanwhile a genuine defect in the mail
collector (`name 'my_addresses' is not defined`, firing on every reply thread)
sat in the journal for days, because a `logger.warning` is not addressed to
anybody.

The division of labour this enforces:

  * **A rule finding is a fact, and some facts a human must act on.** A dead
    unit, an expired token, a full disk: those go to Chat, because only Ignas
    can restart a service or sign in again.
  * **An LLM finding is a lead, and a lead is a coding task.** "This block
    looks like it rests on machine output" is not something to wake him for.
    It is something to reproduce, turn into a scenario, and fix for good.

So this module never sends anything anywhere. It prints, and something reads
it: `.claude/settings.json` wires it to `SessionStart`, and `CLAUDE.md` says
what the session is expected to do about it.

Two constraints, both load-bearing:

  * **It must never break a session.** No database, no `.env`, a migration
    pending, Postgres down — every one of those prints nothing and exits 0. A
    session-start hook that can refuse to let you start working is worse than
    no hook at all.
  * **It must stay small.** Everything it prints is spent from the session's
    context before the first prompt is read. It shows the worst handful and
    says where the rest is.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

# How many of each kind to show. The rest are one CLI call away; the point of
# the brief is to be read, and a 26-item list at session start is skipped.
MAX_FACTS = 6
MAX_LEADS = 8

# A finding nobody has seen for this long is history, not a to-do. It stays
# open in the log — closing it would be a claim nobody checked — but it does
# not get spent from a new session's context.
STALE_AFTER = timedelta(days=14)

# Per-finding detail budget. Enough to know what it is; not the whole rationale.
DETAIL_CHARS = 240


def _age(then: datetime, now: datetime) -> str:
    delta = now - then
    if delta.days >= 1:
        return f"{delta.days}d"
    if delta.seconds >= 3600:
        return f"{delta.seconds // 3600}h"
    return f"{max(delta.seconds // 60, 1)}m"


def _rank(row: dict) -> tuple:
    """Worst first, then most persistent, then most recent.

    Occurrences before recency deliberately: something caught fifteen times is
    a defect, and something caught once may be a day that has since been
    rebuilt.
    """
    severity_order = {"error": 0, "warn": 1, "info": 2}
    return (
        severity_order.get(row["severity"], 3),
        -int(row.get("occurrences") or 1),
        -row["last_seen_at"].timestamp(),
    )


def _one(row: dict, now: datetime) -> list[str]:
    seen = f"{row['occurrences']}x" if (row.get("occurrences") or 1) > 1 else "once"
    out = [
        f"- `#{row['id']}` [{row['severity']}] {row['summary']}",
        f"  {row['source']}/{row['kind']} · {seen} · last {_age(row['last_seen_at'], now)} ago",
    ]
    detail = (row.get("detail") or "").strip()
    if detail:
        first = " ".join(detail.split())[:DETAIL_CHARS]
        out.append(f"  → {first}")
    return out


def compose(rows: list[dict], *, now: datetime | None = None) -> str:
    """The brief. Empty string when there is nothing worth a session's attention.

    Takes rows rather than a connection so the shape of the output is testable
    without a database — which matters here, because the interesting cases are
    all about what happens when there is no database.
    """
    now = now or datetime.now(timezone.utc)
    fresh = [r for r in rows if now - r["last_seen_at"] <= STALE_AFTER]
    if not fresh:
        return ""

    facts = sorted([r for r in fresh if r["detected_by"] == "rule"], key=_rank)
    leads = sorted([r for r in fresh if r["detected_by"] == "llm"], key=_rank)
    stale = len(rows) - len(fresh)

    out = [
        "# IBLU — what it caught while nobody was looking",
        "",
        f"{len(facts)} fact(s) · {len(leads)} lead(s) open"
        + (f" · {stale} older than {STALE_AFTER.days}d not shown" if stale else ""),
        "",
        "The `observations` table. Ignas has not read these — the watchdog only "
        "messages him about faults he can fix himself, so everything here is "
        "waiting on a code change. Protocol: `CLAUDE.md` § *The observation log*.",
        "",
    ]

    if facts:
        out += [
            "## Facts — an invariant failed, so this reproduces",
            "",
        ]
        for row in facts[:MAX_FACTS]:
            out += _one(row, now)
        if len(facts) > MAX_FACTS:
            out.append(f"- …and {len(facts) - MAX_FACTS} more")
        out.append("")

    if leads:
        out += [
            "## Leads — an LLM read a reconstructed day and suspects this",
            "",
            "Each may be a real defect, a day since rebuilt, or the model being "
            "wrong. Reproduce before believing.",
            "",
        ]
        for row in leads[:MAX_LEADS]:
            out += _one(row, now)
        if len(leads) > MAX_LEADS:
            out.append(f"- …and {len(leads) - MAX_LEADS} more")
        out.append("")

    out += [
        f"All {len(rows)} open: `python -m iblu_keeper.store.observations`. "
        "Close one with `--resolve <id> --note \"what was done\"`.",
    ]
    return "\n".join(out)


def _load() -> list[dict]:
    """Open observations, or nothing at all. Never raises.

    Every failure mode here — no `.env`, no database, a pending migration, a
    connection refused — resolves to "there is nothing to say", because the
    alternative is a session that cannot start.
    """
    try:
        from .. import db
        from ..config import settings
        from ..store import observations as obs

        if settings.use_mock or not db.is_configured():
            return []
        try:
            with db.get_conn() as conn:
                return [dict(r) for r in obs.open_observations(conn, limit=60)]
        finally:
            db.close_pool()
    except Exception:  # noqa: BLE001 — see the docstring; silence is the contract
        return []


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m iblu_keeper.jobs.session_brief",
        description="Hand the observation log to a Claude Code session.",
    )
    parser.add_argument("--text", action="store_true",
                        help="print the brief itself instead of SessionStart hook JSON")
    args = parser.parse_args(argv)

    try:
        text = compose(_load())
    except Exception:  # noqa: BLE001 — a formatting bug must not block a session
        text = ""

    if args.text:
        print(text or "nothing open, or nothing readable")
        return 0

    if not text:
        return 0          # a hook that prints nothing adds nothing
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        },
    }))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
