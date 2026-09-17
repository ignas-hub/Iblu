"""What IBLU noticed was wrong with itself.

Every other store module here records what happened to Ignas. This one records
what happened to IBLU: an LLM response rejected, an invariant violated, a
collector erroring for one account, a day that reconstructed into something
implausible.

It exists because the alternative was thirty `logger.warning` calls, each of
which noticed something real and then let it rotate out of the journal. A later
session — human or Claude Code — needs to be able to ask "what has this system
been quietly catching?" and get an answer.

Two rules shape everything here:

  * **Recording an observation must never break the thing being observed.**
    `record_safe` swallows every exception, including the database being down.
    A watchdog that can take down what it watches is worse than no watchdog.
  * **A rule and a model are not the same witness.** `detected_by='rule'` is a
    failed invariant — a fact. `detected_by='llm'` is the sense-check pass
    saying something looks wrong — a lead. Nothing downstream may merge them.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("iblu_keeper.observations")

SOURCES = (
    "judge", "composer", "weekly", "tick", "analyst", "collector", "sensecheck",
)
SEVERITIES = ("info", "warn", "error")


def fingerprint(*parts: Any) -> str:
    """A stable id for "this same problem", so repeats bump a count.

    Deliberately built from the CALLER's chosen parts rather than from the
    message text: an exception string usually carries a timestamp or an id, and
    fingerprinting on it would make every occurrence a new row.
    """
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def record(
    conn,
    *,
    source: str,
    kind: str,
    summary: str,
    detail: str | None = None,
    evidence: dict | None = None,
    severity: str = "warn",
    detected_by: str = "rule",
    fp: str | None = None,
) -> int:
    """Record one observation, or bump the open one that matches. Returns its id."""
    from psycopg.types.json import Jsonb

    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity {severity!r} — expected {SEVERITIES}")
    if detected_by not in ("rule", "llm"):
        raise ValueError("detected_by must be 'rule' or 'llm'")

    fp = fp or fingerprint(source, kind, summary)
    row = conn.execute(
        """
        INSERT INTO observations
            (source, kind, severity, detected_by, summary, detail, evidence, fingerprint)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (fingerprint) WHERE status <> 'resolved'
        DO UPDATE SET
            last_seen_at = now(),
            occurrences  = observations.occurrences + 1,
            -- The newest detail wins: the most recent instance of a recurring
            -- problem is the one worth reading.
            detail       = EXCLUDED.detail,
            evidence     = EXCLUDED.evidence,
            severity     = EXCLUDED.severity
        RETURNING id
        """,
        (source, kind, severity, detected_by, summary, detail,
         Jsonb(evidence or {}), fp),
    ).fetchone()
    return row["id"]


def record_safe(**kwargs) -> int | None:
    """`record` with its own connection, which never raises.

    This is the form every caller should use. An observation is a side note; a
    failure to write one must never surface as a failure of the collector, the
    ping, or the review that was trying to report it.
    """
    from ..config import settings

    if settings.use_mock:
        logger.info("observation (mock, not written): %s", kwargs.get("summary"))
        return None
    try:
        from .. import db

        if not db.is_configured():
            return None
        with db.get_conn() as conn:
            return record(conn, **kwargs)
    except Exception:  # noqa: BLE001 — see the docstring; this is the whole point
        logger.warning("could not record an observation", exc_info=True)
        return None


def open_observations(
    conn, *, limit: int = 50, source: str | None = None, since: datetime | None = None
) -> list[dict]:
    """Everything still open, worst and most recent first."""
    clauses = ["status = 'open'"]
    args: list[Any] = []
    if source:
        clauses.append("source = %s")
        args.append(source)
    if since:
        clauses.append("last_seen_at >= %s")
        args.append(since)
    args.append(limit)
    return conn.execute(
        f"""
        SELECT id, first_seen_at, last_seen_at, occurrences, source, kind,
               severity, detected_by, summary, detail, evidence
          FROM observations
         WHERE {' AND '.join(clauses)}
         ORDER BY array_position(ARRAY['error','warn','info'], severity),
                  last_seen_at DESC
         LIMIT %s
        """,
        tuple(args),
    ).fetchall()


def resolve(conn, observation_id: int, resolution: str) -> bool:
    """Close one out. Never deletes — the history is the point."""
    row = conn.execute(
        """
        UPDATE observations
           SET status = 'resolved', resolved_at = now(), resolution = %s
         WHERE id = %s AND status <> 'resolved'
        RETURNING id
        """,
        (resolution, observation_id),
    ).fetchone()
    return row is not None


# How long an unrepeated LLM lead stays open. A model's finding cannot retire
# itself the way a rule's can — absence on one run proves nothing, because the
# output is not reproducible. But keeping them forever is worse: the list silts
# up with leads about days that have since been rebuilt, and the real signals
# drown in them. A week without recurrence is enough to stop showing it.
LLM_LEAD_TTL = timedelta(days=7)


def age_out_llm_leads(conn, *, ttl: timedelta = LLM_LEAD_TTL) -> int:
    """Close LLM findings that have not recurred. Rule findings are untouched.

    Deliberately worded as "not reproduced" rather than "fixed": nobody checked.
    That is the honest claim, and it is why this only applies to leads.
    """
    rows = conn.execute(
        """
        UPDATE observations
           SET status = 'resolved', resolved_at = now(),
               resolution = %s
         WHERE status = 'open' AND detected_by = 'llm'
           AND severity <> 'error'
           AND last_seen_at < %s
        RETURNING id
        """,
        (
            f"aged out: not reproduced in {ttl.days} days. A lead, not a fact — "
            f"nobody confirmed or refuted it, and it stopped recurring.",
            datetime.now(timezone.utc) - ttl,
        ),
    ).fetchall()
    return len(rows)


def summary_counts(conn, days: int = 7) -> dict:
    """How much IBLU has been catching lately, by source."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = conn.execute(
        """
        SELECT source, severity, detected_by, count(*) AS n,
               sum(occurrences) AS total
          FROM observations
         WHERE last_seen_at >= %s AND status = 'open'
         GROUP BY source, severity, detected_by
         ORDER BY sum(occurrences) DESC
        """,
        (since,),
    ).fetchall()
    return {
        "days": days,
        "rows": [dict(r) for r in rows],
        "open_total": sum(int(r["total"]) for r in rows),
    }


def as_markdown(rows: list[dict], *, title: str = "IBLU — open observations") -> str:
    """The document a later session reads.

    Written for a Claude Code session opening the repo cold: what IBLU caught,
    when, how often, and — the part that matters — whether a rule proved it or
    a model merely suspects it.
    """
    now = datetime.now(timezone.utc)
    out = [
        f"# {title}",
        "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC · {len(rows)} open.",
        "",
        "Regenerate with `python -m iblu_keeper.store.observations --write-doc`.",
        "The database is the source of truth; this file is a copy for reading.",
        "",
        "`rule` means an invariant failed and the finding is a fact. `llm` means",
        "the sense-check pass thought something looked wrong — a lead to check,",
        "never a conclusion to act on blindly.",
        "",
    ]
    if not rows:
        out += ["Nothing open. Either it is all working, or nothing has run yet —",
                "check `collector_state.last_run_at` before concluding the first."]
        return "\n".join(out)

    for r in rows:
        seen = (
            f"seen {r['occurrences']}x, last {r['last_seen_at']:%Y-%m-%d %H:%M} UTC"
            if r["occurrences"] > 1
            else f"{r['last_seen_at']:%Y-%m-%d %H:%M} UTC"
        )
        out.append(f"## [{r['severity']}] {r['summary']}")
        out.append("")
        out.append(f"`#{r['id']}` · {r['source']} · `{r['kind']}` · {r['detected_by']} · {seen}")
        out.append("")
        if r.get("detail"):
            out.append(r["detail"])
            out.append("")
        if r.get("evidence"):
            out.append("```json")
            import json

            out.append(json.dumps(r["evidence"], indent=2, default=str))
            out.append("```")
            out.append("")
    out.append("---")
    out.append("")
    out.append("Close one with `python -m iblu_keeper.store.observations "
               "--resolve <id> --note \"what was done\"`.")
    return "\n".join(out)


# --- CLI: what a later session actually runs --------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from pathlib import Path

    from .. import db

    parser = argparse.ArgumentParser(
        prog="python -m iblu_keeper.store.observations",
        description="What IBLU noticed was wrong with itself.",
    )
    parser.add_argument("--source", help="only this source")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--write-doc", action="store_true",
                        help="regenerate docs/OBSERVATIONS.md")
    parser.add_argument("--resolve", type=int, metavar="ID")
    parser.add_argument("--note", default="", help="why it is resolved")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    if not db.is_configured():
        print("no DATABASE_URL — nothing to read")
        return 1

    try:
        with db.get_conn() as conn:
            if args.resolve:
                if not args.note:
                    print("--resolve needs --note: say what was done about it")
                    return 2
                ok = resolve(conn, args.resolve, args.note)
                print(f"#{args.resolve} " + ("resolved" if ok else "not open — nothing changed"))
                return 0 if ok else 1

            rows = open_observations(conn, limit=args.limit, source=args.source)
            text = as_markdown(rows)
            if args.write_doc:
                path = Path(__file__).resolve().parents[3] / "docs" / "OBSERVATIONS.md"
                path.write_text(text + "\n")
                print(f"wrote {path} ({len(rows)} open)")
            else:
                print(text)
        return 0
    finally:
        db.close_pool()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


# --- the LLM layer being down is an outage, not a footnote ------------------
#
# Found 2026-09-17: the Anthropic credit balance ran out, and every LLM step —
# the ping composer, the analyst's judge, the intent classifier, the
# sense-check — fell back quietly, exactly as designed. The sense-check recorded
# its own failure at severity `info`, so the watchdog (errors only) never said a
# word. Graceful degradation with no alarm is indistinguishable from working.

_OUTAGE_MARKERS = (
    "credit balance", "billing", "authentication_error", "permission_error",
    "invalid x-api-key", "invalid api key", "api key",
)


def is_llm_outage(exc: BaseException) -> bool:
    """True when the API refused us for a reason that will not fix itself."""
    text = str(exc).lower()
    return any(marker in text for marker in _OUTAGE_MARKERS)


def record_llm_failure(source: str, exc: BaseException, *, context: str = "") -> None:
    """Record an LLM failure at the severity it deserves. Never raises.

    A billing or authentication refusal is ONE error for the whole system —
    one fingerprint, whichever step hit it first — so the watchdog announces it
    once instead of once per component. Anything else (a timeout, a malformed
    response) stays a per-source warning, because those are transient.
    """
    try:
        from ..pings.deliver import _scrub

        detail = _scrub(exc)[:800]
    except Exception:  # noqa: BLE001
        detail = str(exc)[:800]

    if is_llm_outage(exc):
        record_safe(
            source=source, kind="llm_unavailable", severity="error",
            summary="the Anthropic API is refusing requests — every LLM step "
                    "(pings, judge, calendar classifier, sense-check) is running "
                    "on fallbacks",
            detail=f"{detail}\n\nIf this is the credit balance, top it up at "
                   "console.anthropic.com → Plans & Billing. Nothing is lost "
                   "meanwhile; the reconstruction is simply unjudged.",
            evidence={"first_seen_in": source, "context": context},
            fp=fingerprint("llm", "unavailable"),
        )
        return
    record_safe(
        source=source, kind="llm_call_failed", severity="warn",
        summary=f"an LLM call failed in {source}",
        detail=detail,
        evidence={"context": context},
        fp=fingerprint(source, "llm_call_failed"),
    )
