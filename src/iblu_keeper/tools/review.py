"""The analyst read: where Ignas's attention actually went.

Stage 2 of the mission. Deliberately deterministic SQL, no LLM — these are
counts, and counts should be reproducible. The LLM's job is to judge them, not
to compute them ("scripts fetch; the LLM judges").

Two rules from the mission shape every query here:

  * **Silence is never presence.** A stretch with no signals is reported as
    unknown, never as work. `coverage` says how much of the window has any
    evidence at all, so a flattering-looking split can be read against how
    much was actually observed.
  * **Attention, not location.** Volume of signals is evidence of attention,
    not a clock. Numbers are counts and shares, never "hours worked" — this
    module must never imply a precision it does not have.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from ..config import settings

logger = logging.getLogger("iblu_keeper.review")

_MOCK = {"status": "mock"}

# A thread touched this many times is a delegation candidate (mission stage 2:
# "what he touched three times that should be someone else's").
RECURRING_THRESHOLD = 3


def _window(window: str) -> tuple[datetime, datetime]:
    from .context import _parse_window

    now = datetime.now(timezone.utc)
    return now - _parse_window(window), now


def _share(counts: dict[str, int]) -> list[dict]:
    """Counts to sorted share-of-total, so the caller never divides by zero."""
    total = sum(counts.values())
    if not total:
        return []
    return [
        {"key": key, "signals": n, "share_pct": round(100 * n / total)}
        for key, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    ]


def review(window: str = "7d") -> dict:
    """Attention over `window`, as counts and shares. No LLM, no estimates."""
    if settings.use_mock:
        return dict(_MOCK)

    since, until = _window(window)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, source, occurred_at, counterpart, container, subject, "
            "       initiator, venture, work_type, project "
            "FROM signals WHERE occurred_at >= %s ORDER BY occurred_at",
            (since,),
        ).fetchall()

        # work_type comes from what Ignas TAPPED, not from what was inferred —
        # signals.work_type is almost always null, and guessing here would
        # manufacture a number he never confirmed.
        tapped = conn.execute(
            """
            SELECT work_type, venture, project, count(*) AS n
            FROM context_entries
            WHERE source IN ('ping', 'chat_reply')
              AND superseded_by IS NULL
              AND COALESCE(occurred_at, created_at) >= %s
              AND work_type IS NOT NULL
            GROUP BY work_type, venture, project
            """,
            (since,),
        ).fetchall()

        pings = conn.execute(
            """
            SELECT local_date, kind, status
            FROM pings
            WHERE window_start >= %s AND kind IN ('midday', 'evening')
            ORDER BY local_date, kind
            """,
            (since,),
        ).fetchall()

    by_venture: dict[str, int] = {}
    by_source: dict[str, int] = {}
    threads: dict[str, dict[str, Any]] = {}
    inbound = 0

    for row in rows:
        # Calendar signals all share container='primary', so clustering them as
        # threads would merge every unrelated event into one bogus "thread"
        # named after whichever happened first. They are changes to the diary,
        # not conversations — counted, but never reported as something touched
        # repeatedly.
        if row["source"] == "calendar":
            by_venture[row["venture"] or "unclassified"] = (
                by_venture.get(row["venture"] or "unclassified", 0) + 1
            )
            by_source["calendar"] = by_source.get("calendar", 0) + 1
            continue

        by_venture[row["venture"] or "unclassified"] = (
            by_venture.get(row["venture"] or "unclassified", 0) + 1
        )
        by_source[row["source"]] = by_source.get(row["source"], 0) + 1

        key = row["container"] or "?"
        thread = threads.setdefault(
            key,
            {
                "container": key,
                "name": row["subject"] or row["counterpart"] or key,
                "counterpart": row["counterpart"],
                "signals": 0,
                "venture": row["venture"],
                "initiator": row["initiator"],
                "first": row["occurred_at"],
                "last": row["occurred_at"],
            },
        )
        thread["signals"] += 1
        thread["last"] = row["occurred_at"]
        if row["initiator"] == "other":
            thread["initiator"] = "other"
            inbound += 1

    ordered = sorted(threads.values(), key=lambda t: t["signals"], reverse=True)

    def _thread_out(t: dict) -> dict:
        return {
            "name": t["name"],
            "counterpart": t["counterpart"],
            "signals": t["signals"],
            "venture": t["venture"],
            "started_by": t["initiator"] or "unknown",
            "first": t["first"].isoformat(),
            "last": t["last"].isoformat(),
        }

    # Ping answer rate — the mission's own success metric (>=80% of weekdays).
    expected = {(p["local_date"], p["kind"]) for p in pings}
    answered = {
        (p["local_date"], p["kind"]) for p in pings if p["status"] == "answered"
    }
    answer_rate = round(100 * len(answered) / len(expected)) if expected else None

    # How much of the window has any evidence at all. Silence is not presence:
    # a 100%-one-venture split over 3 observed hours means far less than the
    # same split over 30, and the reader must be able to tell.
    observed_days = len({r["occurred_at"].date() for r in rows})
    window_days = max(1, round((until - since).total_seconds() / 86400))

    return {
        "window": window,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "coverage": {
            "signals": len(rows),
            "days_with_signals": observed_days,
            "days_in_window": window_days,
            "note": (
                "Counts are evidence of attention, not hours. Days with no "
                "signals are unknown, not idle."
            ),
        },
        "by_venture": _share(by_venture),
        "by_source": _share(by_source),
        "by_work_type": _share({r["work_type"]: r["n"] for r in tapped}),
        "work_type_note": (
            "Only from answers Ignas tapped — never inferred. Empty means the "
            "pings have not been answered yet, not that no work happened."
        ),
        "top_threads": [_thread_out(t) for t in ordered[:10]],
        "recurring": [
            _thread_out(t) for t in ordered if t["signals"] >= RECURRING_THRESHOLD
        ][:10],
        "inbound": {
            "signals_in_threads_i_did_not_start": inbound,
            "share_pct": round(100 * inbound / len(rows)) if rows else 0,
        },
        "pings": {
            "sent": len(expected),
            "answered": len(answered),
            "answer_rate_pct": answer_rate,
            "target_pct": 80,
        },
    }


def as_markdown(data: dict) -> str:
    """Compact, speakable rendering — Ignas reads these by voice while driving.

    Leads with the caveat when coverage is thin: a confident-sounding split over
    two observed days would be exactly the flattery the mission forbids.
    """
    if data.get("status") == "mock":
        return "_mock mode — nothing recorded_"

    cov = data["coverage"]
    lines: list[str] = []

    thin = cov["days_with_signals"] < max(2, cov["days_in_window"] // 2)
    lines.append(
        f"**{cov['signals']} signals** over {cov['days_with_signals']} of "
        f"{cov['days_in_window']} days."
        + ("  ⚠️ Thin coverage — treat the split as indicative." if thin else "")
    )

    if data["by_venture"]:
        lines.append(
            "\n**Where:** "
            + " · ".join(f"{v['key']} {v['share_pct']}%" for v in data["by_venture"])
        )

    if data["by_work_type"]:
        lines.append(
            "**What kind:** "
            + " · ".join(f"{w['key']} {w['share_pct']}%" for w in data["by_work_type"])
        )
    else:
        lines.append("**What kind:** not yet — no pings answered in this window.")

    inbound = data["inbound"]
    lines.append(
        f"**Inbound:** {inbound['share_pct']}% of it was in threads you did not start."
    )

    if data["recurring"]:
        lines.append("\n**Touched repeatedly** — candidates to delegate or automate:")
        for t in data["recurring"][:5]:
            who = "you started it" if t["started_by"] == "me" else "someone else started it"
            lines.append(f"- {t['name']} — {t['signals']}x, {who}")

    p = data["pings"]
    if p["sent"]:
        verdict = "on target" if (p["answer_rate_pct"] or 0) >= p["target_pct"] else "below target"
        lines.append(
            f"\n**Pings:** {p['answered']}/{p['sent']} answered "
            f"({p['answer_rate_pct']}%, {verdict} of {p['target_pct']}%)."
        )

    return "\n".join(lines)
