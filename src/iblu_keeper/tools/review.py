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


# A thread gone quiet for this long by the end of the window is read as
# "likely closed" for the Gains section (plan §1.7's "progressed" evidence) —
# not because two quiet days proves it, but because the alternative (treating
# every recurring thread as still open forever) would mean nothing ever
# counts as a completed step, which is exactly the Gap-style goalpost this
# system exists to avoid. Long enough to not catch an overnight pause,
# nowhere near long enough to be mistaken for a claim of certainty — it is
# reported as "likely", never as "closed".
LIKELY_CLOSED_QUIET = timedelta(days=2)


def _share(counts: dict[str, int]) -> list[dict]:
    """Counts to sorted share-of-total, so the caller never divides by zero."""
    total = sum(counts.values())
    if not total:
        return []
    return [
        {"key": key, "signals": n, "share_pct": round(100 * n / total)}
        for key, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    ]


def review(
    window: str = "7d",
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict:
    """Attention over `window`, as counts and shares. No LLM, no estimates.

    `since`/`until` let a caller hand in an explicit range (the weekly review
    uses Monday 00:00 -> Friday 18:00 local, which is not a fixed duration and
    so cannot be expressed as a "7d"-style `window` string) while keeping
    `window` as the label recorded in the output and read by `context_review`.
    """
    if settings.use_mock:
        return dict(_MOCK)

    if since is None or until is None:
        since, until = _window(window)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, source, occurred_at, counterpart, container, subject, "
            "       initiator, venture, work_type, project, actor "
            "FROM signals WHERE occurred_at >= %s AND actor = 'me' "
            "ORDER BY occurred_at",
            (since,),
        ).fetchall()

        # Inbound demand: things that landed on a group Ignas works (the Choco
        # payables queue, the BLT contracts group) which he did not write.
        # Counted separately and NEVER mixed into the attention split — other
        # people's mail inflating his numbers is precisely the flattery the
        # mission forbids.
        demand = conn.execute(
            "SELECT venture, counterpart, count(*) AS n FROM signals "
            "WHERE occurred_at >= %s AND actor = 'other' "
            "GROUP BY venture, counterpart ORDER BY n DESC LIMIT 10",
            (since,),
        ).fetchall()
        demand_total = conn.execute(
            "SELECT count(*) AS n FROM signals "
            "WHERE occurred_at >= %s AND actor = 'other'",
            (since,),
        ).fetchone()["n"]

        # work_type comes from what Ignas TAPPED, not from what was inferred —
        # signals.work_type is almost always null, and guessing here would
        # manufacture a number he never confirmed.
        # The same week in minutes, when the day has been reconstructed.
        # None until the analyst has run over this window — a caller must fall
        # back to counts rather than print a confident zero.
        minutes = minutes_from_blocks(conn, since, until)

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

    # Untracked share: the fraction of the window with no evidence at all.
    # Reported as its own number (never folded into `by_venture`, where a gap
    # would silently vanish into whichever venture happens to be largest) so
    # the Truth section can say plainly how much of the week is unknown
    # rather than implying full coverage.
    untracked_share_pct = round(100 * (window_days - observed_days) / window_days)

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
        "untracked": {
            "share_pct": untracked_share_pct,
            "basis": "days",
            "note": (
                "Share of the window's days with no signal at all — unknown, "
                "not idle. Superseded by the minutes figure once the day has "
                "been reconstructed; see `minutes`."
            ),
        },
        "minutes": minutes,
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
        "inbound_demand": {
            "signals": demand_total,
            "note": (
                "Landed on a group Ignas works but was written by someone else. "
                "Demand on his time, not evidence of his attention — never "
                "counted in the splits above."
            ),
            "top_senders": [
                {"who": d["counterpart"] or "unknown", "venture": d["venture"],
                 "signals": d["n"]}
                for d in demand
            ],
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

    demand = data.get("inbound_demand") or {}
    if demand.get("signals"):
        top = ", ".join(
            f"{d['who']} {d['signals']}" for d in demand["top_senders"][:3]
        )
        lines.append(
            f"\n**Landed on your groups:** {demand['signals']} items you did not "
            f"write{' — ' + top if top else ''}. (Demand, not your attention.)"
        )

    p = data["pings"]
    if p["sent"]:
        verdict = "on target" if (p["answer_rate_pct"] or 0) >= p["target_pct"] else "below target"
        lines.append(
            f"\n**Pings:** {p['answered']}/{p['sent']} answered "
            f"({p['answer_rate_pct']}%, {verdict} of {p['target_pct']}%)."
        )

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Gains — measured backward from the window's start (plan §1.7, context §7)
# --------------------------------------------------------------------------

# Session 8 (built separately, may or may not exist yet) taps the evening
# "what moved today?" card straight into context_entries tagged 'gain', with
# meta.kind in {'learned','progressed','experienced'}. Reading them here if
# present costs nothing and loses nothing if absent — the computed evidence
# below covers the same three kinds independently, from data that already
# exists today.
_GAIN_KINDS = ("learned", "progressed", "experienced")



def minutes_from_blocks(conn, since: datetime, until: datetime) -> dict | None:
    """The week in minutes, from the reconstructed day (plan §3.4).

    Signal counts answer "what got his attention"; blocks answer "for how
    long". Both are in the review because they fail differently: counts
    over-weight a chatty thread, minutes over-weight a long quiet one.

    Returns None when no day in the window has been reconstructed yet — a
    caller must then fall back to counts rather than print a confident zero.

    Every number here is split `fact` / `inferred`, because a minute Ignas
    confirmed and a minute IBLU guessed are not the same evidence and must
    never be added together silently.
    """
    rows = conn.execute(
        """
        SELECT venture, work_type, attention, confidence, intent_title,
               EXTRACT(EPOCH FROM (ends_at - starts_at)) / 60 AS minutes
          FROM blocks
         WHERE superseded_by IS NULL
           AND starts_at >= %s AND starts_at < %s
        """,
        (since, until),
    ).fetchall()
    if not rows:
        return None

    # EXTRACT() comes back as Decimal; mixing it with float minutes raises.
    total = float(sum(float(r["minutes"]) for r in rows))
    by_venture: dict[str, float] = {}
    by_work_type: dict[str, float] = {}
    by_attention: dict[str, float] = {}
    by_confidence: dict[str, float] = {}
    untracked = 0.0

    for r in rows:
        m = float(r["minutes"])
        by_attention[r["attention"]] = by_attention.get(r["attention"], 0) + m
        by_confidence[r["confidence"]] = by_confidence.get(r["confidence"], 0) + m
        if r["venture"]:
            by_venture[r["venture"]] = by_venture.get(r["venture"], 0) + m
        else:
            # No venture and no intent is genuinely unaccounted time. An
            # unaccounted minute is unknown, never idle, and never quietly
            # folded into whichever venture happens to be largest.
            untracked += m
        if r["work_type"]:
            by_work_type[r["work_type"]] = by_work_type.get(r["work_type"], 0) + m

    def _pct(x: float) -> int:
        return round(100 * x / total) if total else 0

    def _split(d: dict[str, float]) -> list[dict]:
        return [
            {"key": k, "minutes": round(v), "share_pct": _pct(v)}
            for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)
        ]

    return {
        "total_minutes": round(total),
        "by_venture": _split(by_venture),
        "by_work_type": _split(by_work_type),
        "by_attention": _split(by_attention),
        "by_confidence": _split(by_confidence),
        "untracked": {"minutes": round(untracked), "share_pct": _pct(untracked)},
        "displaced": {
            "minutes": round(by_attention.get("displaced", 0)),
            "share_pct": _pct(by_attention.get("displaced", 0)),
        },
        "note": (
            "Minutes come from the reconstructed day, not a clock. 'ambiguous' "
            "is unknown, not idle, and 'inferred' minutes were never confirmed "
            "by Ignas."
        ),
    }

def gains(truth: dict) -> dict:
    """What exists now that didn't at the start of `truth`'s window.

    Three kinds, all dated, all backward-looking:
      * **learned**    — a decision or correction logged in the window.
      * **progressed** — a `blocks` row confirmed `confidence='fact'`, or a
        thread touched >=3x that has since gone quiet (§ `LIKELY_CLOSED_QUIET`
        — reported as "likely closed", never as a certainty).
      * **experienced** — family/personal time the reconstructed day marked
        `attention='present'`: actually lived, not merely scheduled.

    Takes `truth` (the output of `review()`) rather than a window string so
    the two can never disagree about what "this week" means, and so the
    thread heuristic reuses `truth["recurring"]` instead of re-deriving
    thread clustering from signals a second time.
    """
    if settings.use_mock:
        return dict(_MOCK, learned=[], progressed=[], experienced=[])

    since = datetime.fromisoformat(truth["since"])
    until = datetime.fromisoformat(truth["until"])
    since_date, until_date = since.date(), until.date()

    learned: list[dict] = []
    progressed: list[dict] = []
    experienced: list[dict] = []
    buckets = {"learned": learned, "progressed": progressed, "experienced": experienced}

    with db.get_conn() as conn:
        # Session 8's tapped gains, if the table already has any.
        tapped = conn.execute(
            """
            SELECT content, meta, occurred_at, created_at, venture
              FROM context_entries
             WHERE 'gain' = ANY(tags) AND created_at >= %s AND created_at < %s
               AND superseded_by IS NULL
               AND NOT ('test' = ANY(tags))
             ORDER BY created_at
            """,
            (since, until),
        ).fetchall()
        for row in tapped:
            kind = (row["meta"] or {}).get("kind")
            if kind in _GAIN_KINDS:
                buckets[kind].append({
                    "date": (row["occurred_at"] or row["created_at"]).date().isoformat(),
                    "text": row["content"][:200],
                    "venture": row["venture"],
                    "source": "tapped",
                })

        # learned — decisions and corrections logged in the window.
        decisions = conn.execute(
            """
            SELECT content, occurred_at, created_at, venture
              FROM context_entries
             WHERE type IN ('decision', 'correction')
               AND created_at >= %s AND created_at < %s
               -- A priority, a baseline and the gain rules are the measuring
               -- stick. Counting them as progress would mean the week Ignas
               -- wrote down what he wants scores as his best week ever.
               AND (source_ref IS NULL OR source_ref !~ '^(priority|baseline|gain):')
               -- A superseded decision was replaced, not achieved. Without
               -- this a single revised priority appears three times.
               AND superseded_by IS NULL
               -- Acceptance probes and scratch entries are not his week.
               AND NOT ('test' = ANY(tags))
             ORDER BY created_at
            """,
            (since, until),
        ).fetchall()
        for row in decisions:
            learned.append({
                "date": (row["occurred_at"] or row["created_at"]).date().isoformat(),
                "text": row["content"][:200],
                "venture": row["venture"],
                "source": "decision",
            })

        # progressed — confirmed (confidence='fact') blocks written this window.
        fact_blocks = conn.execute(
            """
            SELECT local_date, venture, reasoning
              FROM blocks
             WHERE confidence = 'fact' AND superseded_by IS NULL
               AND local_date >= %s AND local_date < %s
             ORDER BY local_date
            """,
            (since_date, until_date),
        ).fetchall()
        for row in fact_blocks:
            progressed.append({
                "date": row["local_date"].isoformat(),
                "text": row["reasoning"] or "confirmed block",
                "venture": row["venture"],
                "source": "block:fact",
            })

        # experienced — family/personal time the reconstruction marked lived.
        lived = conn.execute(
            """
            SELECT local_date, venture, reasoning
              FROM blocks
             WHERE venture IN ('family', 'personal') AND attention = 'present'
               AND superseded_by IS NULL
               AND local_date >= %s AND local_date < %s
             ORDER BY local_date
            """,
            (since_date, until_date),
        ).fetchall()
        for row in lived:
            experienced.append({
                "date": row["local_date"].isoformat(),
                "text": row["reasoning"] or "present",
                "venture": row["venture"],
                "source": "block:present",
            })

    # progressed, continued — threads touched >=3x that went quiet before the
    # window closed. `truth["recurring"]` already carries `last` per thread,
    # so this reuses the clustering `review()` already did instead of hitting
    # `signals` again.
    for thread in truth.get("recurring", []):
        last = datetime.fromisoformat(thread["last"])
        if until - last >= LIKELY_CLOSED_QUIET:
            progressed.append({
                "date": last.date().isoformat(),
                "text": f"{thread['name']} ({thread['signals']}x) — likely closed, quiet since",
                "venture": thread.get("venture"),
                "source": "thread:likely_closed",
            })

    return {"learned": learned, "progressed": progressed, "experienced": experienced}


# Mission stage 2 / plan §1.7: "attention per stage (share of the week in
# stages 1-3 vs 6-8)". 4-5 (trial, implement) are deliberately left out of
# both buckets — they are the middle of the ladder, neither "just looked at
# it" nor "running without him" — and reported as 'other' rather than folded
# into whichever bucket happens to be more flattering.
_EARLY_STAGES = {"research", "initiate", "build"}
_LATE_STAGES = {"deliver", "maintain", "autonomous"}


def stage_truth(since: datetime, until: datetime) -> dict | None:
    """Attention per stage and projects stuck >=30 days — only if the stage
    registry already exists (plan §1.5/§1.6; `iblu_keeper.store.projects` is
    built by another worker in parallel and may not exist yet, or may not yet
    define these names).

    Importing the NAMES directly (not just the module) means either "the
    module doesn't exist" or "the module exists but doesn't have this name
    yet" raises the same `ImportError` — one guard covers both, and the Truth
    section simply omits these lines rather than failing.
    """
    if settings.use_mock:
        return None
    try:
        from ..store.projects import list_projects, resolve, stuck
    except ImportError:
        return None
    try:
        with db.get_conn() as conn:
            # Attention per stage needs each `me` signal's free-text project
            # resolved to a registered project's CURRENT stage — signals
            # never store a stage themselves, only projects do.
            rows = conn.execute(
                """
                SELECT project FROM signals
                 WHERE occurred_at >= %s AND occurred_at < %s
                   AND actor = 'me' AND project IS NOT NULL
                """,
                (since, until),
            ).fetchall()
            stage_by_code = {p["code"]: p["stage"] for p in list_projects(conn, active=None)}

            counts = {"early (stages 1-3)": 0, "late (stages 6-8)": 0, "other": 0}
            for row in rows:
                code = resolve(conn, row["project"])
                stage = stage_by_code.get(code) if code else None
                if stage in _EARLY_STAGES:
                    counts["early (stages 1-3)"] += 1
                elif stage in _LATE_STAGES:
                    counts["late (stages 6-8)"] += 1
                else:
                    counts["other"] += 1

            stuck_rows = stuck(conn, today=until.date())

        total = sum(counts.values())
        by_stage = (
            [{"key": k, "share_pct": round(100 * n / total)} for k, n in counts.items() if n]
            if total else []
        )
        return {
            "by_stage": by_stage,
            "stuck": [{"name": r["name"], "code": r["code"]} for r in stuck_rows],
        }
    except Exception:
        # The module exists but something in it failed (e.g. its own table
        # not migrated on this box yet) — the rest of the review must not go
        # down with it.
        logger.warning("review: stage_truth unavailable", exc_info=True)
        return None


def _nameable_person(thread: dict) -> str | None:
    """A counterpart only counts as a person if it names one.

    A Chat space's counterpart is the space itself, so the naive read produced
    "hand the Email Writer thread to Email Writer". An external DM partner who
    is not in Google Contacts stays `users/<id>`, which is no better. In both
    cases there is no one to hand it to, and the honest step is to automate it.
    """
    person = (thread.get("counterpart") or "").strip()
    if not person:
        return None
    if person.startswith("users/"):
        return None
    if person.casefold() == (thread.get("name") or "").strip().casefold():
        return None
    return person


def one_removal(truth: dict) -> dict:
    """Exactly one project and one concrete step toward `autonomous` (plan
    §1.6/§1.7): the person to hand it to, or the automation to build.

    Drawn from the delegation candidates the review already computes —
    threads touched >=3x (mission stage 2: "what he touched three times that
    should be someone else's"), preferring ones he did not start. Never a
    list: the first candidate wins, or none is named at all when the evidence
    doesn't support one.
    """
    if truth.get("status") == "mock":
        return {"available": False, "reason": "mock mode"}

    recurring = truth.get("recurring") or []
    candidates = [t for t in recurring if t.get("started_by") != "me"] or recurring
    if not candidates:
        return {
            "available": False,
            # "insufficient", never "not enough" — the latter is Gap-grading
            # language (see `review_language.validate_language`) even though
            # this sentence is only about the evidence, not about Ignas.
            "reason": "insufficient evidence this week to name one",
        }

    thread = candidates[0]
    person = _nameable_person(thread)
    if person:
        return {
            "available": True,
            "kind": "person",
            "project": thread["name"],
            "step": f"hand the {thread['name']} thread to {person}",
        }
    return {
        "available": True,
        "kind": "automation",
        "project": thread["name"],
        "step": (
            f"automate the {thread['name']} thread — touched "
            f"{thread['signals']}x this week with no single owner"
        ),
    }
