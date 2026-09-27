"""python -m iblu_keeper.jobs.audit [--days N] [--json] [--no-record]
                                   [--section blocks|questions|accuracy]

Ignas asked two questions directly, after finding six classes of wrong output
himself in two weeks (a shadow calendar he judged roughly half wrong, a
question about someone else's mail): *how accurate is the shadow calendar?*
and *how accurate are the questions you send me?* The 746-test suite stayed
green through all six, because it tests functions — `build()`, `compose()`,
`record_tap()` — in isolation, never the artifacts those functions leave
behind in a real database on a real day.

This job measures the artifacts. It is a deterministic sweep over already-
recorded `blocks`, `pings` and `context_entries` — no LLM, no Anthropic call,
no Google call, nothing sent to Chat — and it turns what it finds into
`observations` rows so the existing watchdog pipeline can surface them, the
same "check, record, announce" pipeline `analyst/sensecheck.py` and
`jobs/watchdog.py` already use. It deliberately does not re-implement THEIR
checks (blocks overlapping on a single day at reconstruct time, a collector
falling silent, a unit being down) — this is the retrospective, multi-day
view a human runs on demand, not the per-run pass that fires automatically.
Where the same invariant is worth checking both ways (overlap, the 15-minute
floor), this one aggregates across the whole `--days` window and reports by
date rather than duplicating sensecheck's single-day SQL, and records under
its own fingerprint namespace (`fingerprint("audit", ...)`) so the two passes
never fight over the same open row.

Three sections, each independent (`--section` may be repeated to run a
subset; the default is all three):

  1. **Structural invariants over stored blocks** — facts, `detected_by=
     'rule'`. Every violation below is a `source='analyst'` observation with
     a summary naming the date:
       * two live blocks overlapping the same minute
       * a block shorter than the 15-minute floor
       * a block whose starts_at/ends_at fall outside its own local_date,
         in local time
       * a day whose live blocks sum to more minutes than the day had
       * `attention='present'` with no evidence, unless it is the family
         inference (`venture='family'`, reasoning says "assumed" — or "you
         said you went" for a tap-confirmed instance) or a continuation
         slice (reasoning says "part of the surrounding stretch") — see
         `analyst/blocks.py`'s `_reasoning()`, which is what these two
         phrases are read back from
       * a block citing a signal that is `actor='other'` or has
         `excluded_reason` set — inbound mail or test data counted as his
         attention (HANDOFF §25)
       * a block with `work_type` or `project` but no evidence at all
       * `confidence='fact'` on an analyst block whose signals are all
         `venture_confidence='inferred'`
       * a mirrored calendar event id left on a superseded block (a stale
         Secretary-calendar entry)

  2. **Question audit** over `pings` rows (midday/evening; `questions` is
     the snapshot stored at send time, so this reads exactly what reached
     his phone):
       * a question whose cited signals are all `actor='other'`, or which
         cites no signal at all while naming something specific
       * a time-looking string (`HH:MM`) in the text that matches no cited
         signal's local time, no bound of the ping window, and no block it
         refers to — this is exactly the UTC-vs-local bug (HANDOFF §25)
       * an internal code (a qid, a `gain_kind:` prefix, an unambiguous
         venture code, a hyphenated project code) appearing bare
       * the answer rate, and of the answered ones, how many were later
         corrected (retapped or superseded) — a proxy for "wrong"

  3. **Shadow-calendar accuracy** — the number he actually asked for.
     Compares every span he confirmed (a `blocks` row with `source='ping'`
     from a split/gap tap, or an `attended` answer in `context_entries`)
     against what the analyst had produced for that same span BEFORE the
     confirmation, walking `superseded_by` backwards. Reports agreement on
     venture and on attention as `matched/total`, plus honest coverage:
     present/displaced/ambiguous/untracked minutes per day, what share of
     the workday has any evidence at all, and what share of `present`
     minutes rest on a single signal (thin evidence). **Never a percentage
     from an empty denominator** — below `MIN_CONFIRMED_SPANS` confirmed
     spans this prints "not enough confirmed spans yet (N)" instead of a
     number.

Hard constraints, worth restating here because they are enforced nowhere
else: this job is read-only against `blocks`, `signals`, `context_entries`
and `pings` — it may write ONLY `observations` rows, and only when
`--no-record` is absent. Exit code is always 0: a report is not a failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import db
from ..config import settings
from ..store import observations as obs

logger = logging.getLogger("iblu_keeper.jobs.audit")

DEFAULT_DAYS = 14
SECTIONS = ("blocks", "questions", "accuracy")

# Every finding this job writes carries this source — section 1 is required
# to by the spec ("source='analyst'"); sections 2/3 keep the same source so
# "what has IBLU been catching" (docs/OBSERVATIONS.md, store/observations.py)
# stays answerable from one enum rather than growing a new source per job.
SOURCE = "analyst"

# Below this many confirmed spans, a matched/total percentage is noise
# dressed as a number — "not enough confirmed spans yet (3)" is the honest
# output, never a 100% (or 0%) computed from a denominator of one or two.
MIN_CONFIRMED_SPANS = 5

QIDS = ("sink", "displaced", "split", "work_type", "gap", "gains", "body_mind", "attended")
GAIN_KINDS = ("learned", "progressed", "experienced")

# Deliberately narrow, the same restraint compose.py's own JARGON list uses
# (only "sink" / "attention sink", not all eight qids): "gap", "split",
# "attended", "displaced" and "gains" are ordinary English words a
# well-written question can use legitimately ("attended a meeting"), so
# flagging every bare qid would cry wolf on normal prose. Only the
# code-shaped ones (an underscore is never natural English) and the one
# documented historical leak ("sink" — compose.py JARGON, HANDOFF) are
# checked as bare qids.
BARE_CODE_QIDS = ("sink", "work_type", "body_mind")

# Only these qids ever claim to be about a specific thread/event — "gains"
# and "body_mind" are fixed, deliberately generic templates (compose.py's
# `compose_gains_question`/`compose_body_mind_question` never carry a
# container or signal_ids because they are not about any one thing), and
# "attended" points at a calendar event, not a signal. Checking "cites no
# evidence while naming something" against those three just trips on an
# incidental capitalised word ("Iblu", "Tap") in an otherwise-fine generic
# question — a false alarm this narrower list avoids.
EVIDENCE_CLAIMING_QIDS = frozenset({"sink", "displaced", "split", "work_type", "gap"})

# Venture codes are deliberately NOT checked for bare appearance. Unlike a
# qid or a project slug, a venture code IS how Ignas already refers to these
# things in ordinary prose — "BLT", "Choco", "Deadlift", "Jakusi" are his own
# short names (see HANDOFF.md's own usage), not internal jargon that needs
# translating. A first version of this check flagged "BLT/leads automation"
# as a leak; it reads fine to him and flagging it would just be noise. The
# historical "Experienced: time with personal" bug is still caught below, by
# the gain_kind ':' prefix, which is what actually made that one unmistakably
# a leak rather than the bare word "personal".

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.iblu_timezone)


def _local_today() -> date:
    return datetime.now(_tz()).date()


def _hhmm(dt: datetime) -> str:
    return dt.astimezone(_tz()).strftime("%H:%M")


def _names_something(text: str) -> bool:
    """Does this question point at something a human could look up later?

    Mirrors `pings.compose._names_something`'s heuristic exactly, kept as an
    independent copy rather than an import: `pings/` is owned by another
    worker in parallel, and this audit must keep working (and keep meaning
    the same thing) even if that private helper is renamed or removed there.
    """
    if '"' in text or "'" in text or "@" in text:
        return True
    words = text.split()
    return any(w[:1].isupper() for w in words[1:] if w[:1].isalpha())


def _finding(kind, summary, *, severity="warn", detail=None, evidence=None, fp_parts=()):
    return {
        "source": SOURCE, "kind": kind, "summary": summary, "severity": severity,
        "detail": detail, "evidence": evidence or {}, "detected_by": "rule",
        "fp": obs.fingerprint("audit", kind, *fp_parts),
    }


# ---------------------------------------------------------------------------
# 1. structural invariants over stored blocks
# ---------------------------------------------------------------------------


def _check_overlaps(conn, since: date) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.id AS a_id, b.id AS b_id, a.local_date
          FROM blocks a JOIN blocks b
            ON a.local_date = b.local_date AND a.id < b.id
           AND a.superseded_by IS NULL AND b.superseded_by IS NULL
           AND a.starts_at < b.ends_at AND b.starts_at < a.ends_at
         WHERE a.local_date >= %s
         ORDER BY a.local_date
        """,
        (since,),
    ).fetchall()
    by_date: dict = defaultdict(list)
    for r in rows:
        by_date[r["local_date"]].append([r["a_id"], r["b_id"]])
    return [
        _finding(
            "blocks_overlap",
            f"{len(pairs)} pair(s) of live blocks overlap on {on}",
            severity="error",
            detail="Two live blocks covering the same minute means every "
                   "minutes figure for that day is inflated.",
            evidence={"date": str(on), "pairs": pairs[:10]}, fp_parts=(on,),
        )
        for on, pairs in by_date.items()
    ]


def _check_below_floor(conn, since: date) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, local_date FROM blocks
         WHERE local_date >= %s AND superseded_by IS NULL
           AND ends_at - starts_at < interval '15 minutes'
        """,
        (since,),
    ).fetchall()
    by_date: dict = defaultdict(list)
    for r in rows:
        by_date[r["local_date"]].append(r["id"])
    return [
        _finding(
            "block_below_floor",
            f"{len(ids)} block(s) on {on} are shorter than the 15-minute floor",
            evidence={"date": str(on), "block_ids": ids[:10]}, fp_parts=(on,),
        )
        for on, ids in by_date.items()
    ]


def _check_date_mismatch(conn, since: date) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, local_date, starts_at, ends_at FROM blocks
         WHERE local_date >= %s AND superseded_by IS NULL
        """,
        (since,),
    ).fetchall()
    tz = _tz()
    by_date: dict = defaultdict(list)
    for r in rows:
        local_start = r["starts_at"].astimezone(tz)
        local_end = r["ends_at"].astimezone(tz)
        start_ok = local_start.date() == r["local_date"]
        # A block ending exactly at local midnight belongs to the day it
        # covered, not to the day that midnight begins.
        end_ok = local_end.date() == r["local_date"] or (
            local_end.date() == r["local_date"] + timedelta(days=1)
            and local_end.time() == time(0, 0)
        )
        if not (start_ok and end_ok):
            by_date[r["local_date"]].append(r["id"])
    return [
        _finding(
            "block_date_mismatch",
            f"{len(ids)} block(s) filed under {on} start or end outside that "
            f"local day",
            severity="error",
            detail="`_clip_to_day` exists precisely to stop a 23:58 signal "
                   "producing a block that runs into tomorrow under "
                   "yesterday's local_date.",
            evidence={"date": str(on), "block_ids": ids[:10]}, fp_parts=(on,),
        )
        for on, ids in by_date.items()
    ]


def _check_day_exceeds_wallclock(conn, since: date) -> list[dict]:
    from ..analyst.blocks import day_bounds

    rows = conn.execute(
        """
        SELECT local_date,
               sum(EXTRACT(EPOCH FROM (ends_at - starts_at)) / 60) AS minutes
          FROM blocks
         WHERE local_date >= %s AND superseded_by IS NULL
         GROUP BY local_date
        """,
        (since,),
    ).fetchall()
    out = []
    for r in rows:
        on = r["local_date"]
        start, end = day_bounds(on)
        wall_minutes = (end - start).total_seconds() / 60
        minutes = float(r["minutes"] or 0)
        if minutes > wall_minutes + 1:  # a minute of float slop
            out.append(_finding(
                "day_minutes_exceed_wallclock",
                f"{on}: blocks sum to {round(minutes)} minutes, more than the "
                f"{round(wall_minutes)}-minute day",
                severity="error",
                detail="Even with no outright overlap, one day's own blocks "
                       "cannot add up to more time than the day had.",
                evidence={"date": str(on), "minutes": round(minutes),
                          "wall_clock_minutes": round(wall_minutes)},
                fp_parts=(on,),
            ))
    return out


def _check_present_without_evidence(conn, since: date) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, local_date, venture, reasoning FROM blocks
         WHERE local_date >= %s AND superseded_by IS NULL
           AND attention = 'present' AND jsonb_array_length(evidence) = 0
        """,
        (since,),
    ).fetchall()
    if not rows:
        return []

    # A day that has at least one confirmed (ping/human) block is a day
    # `_carve_out` may have run on — it can legitimately empty a present/
    # displaced block's evidence when a tap confirms an overlapping span,
    # re-deriving that remainder's reasoning for its own new span (see
    # `_carve_out`'s docstring).
    confirmed_dates = {
        r["local_date"] for r in conn.execute(
            "SELECT DISTINCT local_date FROM blocks WHERE local_date >= %s "
            "AND source IN ('ping', 'human')",
            (since,),
        ).fetchall()
    }

    by_date: dict = defaultdict(list)
    for r in rows:
        reasoning = (r.get("reasoning") or "").lower()
        # Legal in the shapes `build()`/`_carve_out` can actually produce —
        # checked two ways, ORed, because either can miss it alone:
        #  (a) `venture='family'` with no evidence is only ever written by
        #      `_apply_family_inference`/`_apply_maybe_family_inference` —
        #      nothing else in `build()` can produce that combination, so
        #      this alone is conclusive regardless of the reasoning text;
        #  (b) a continuation slice — either `_carve_out`'s remainder of a
        #      confirmed span (only possible on a day that HAS a confirmed
        #      block), or `build()`'s own TAIL/FLOOR padding leaving a short
        #      evidence-free gap between two calendar events inside one
        #      cluster's widened span (confirmed against the live database:
        #      2026-09-22 14:15-14:30, `venture=None`, sitting between a
        #      13:30 `displaced` block and a 14:30 `ambiguous` one — no
        #      confirmed block that day at all, purely `build()`'s own
        #      widening). The DETERMINISTIC text ("part of the surrounding
        #      stretch") only proves (b) when the judge has not since
        #      rewritten it — the judge may rewrite any block's reasoning
        #      (HANDOFF §18), which is why the family case in (a) is never
        #      decided by text: a live family-inference block was found
        #      with judge-rewritten reasoning matching neither deterministic
        #      phrase at all.
        legal = (
            r["venture"] == "family"
            or r["local_date"] in confirmed_dates
            or "part of the surrounding stretch" in reasoning
        )
        if not legal:
            by_date[r["local_date"]].append(r["id"])
    return [
        _finding(
            "present_without_evidence",
            f"{len(ids)} block(s) on {on} are 'present' with no evidence and "
            f"are neither a family inference nor a continuation slice",
            severity="error",
            detail="A present block needs something behind it: a signal, a "
                   "confirmed/assumed family inference, or inheritance from "
                   "the surrounding stretch. Anything else claiming his "
                   "attention with nothing recorded is a labelling bug — "
                   "'Go pickup Emory' becoming 420 minutes of blt/client, one "
                   "step removed.",
            evidence={"date": str(on), "block_ids": ids[:10]}, fp_parts=(on,),
        )
        for on, ids in by_date.items()
    ]


def _check_evidence_tainted(conn, since: date) -> list[dict]:
    rows = conn.execute(
        """
        SELECT b.id AS block_id, b.local_date, s.id AS signal_id
          FROM blocks b
          CROSS JOIN LATERAL jsonb_array_elements_text(b.evidence) AS e(sid)
          JOIN signals s ON s.id = e.sid::bigint
         WHERE b.local_date >= %s AND b.superseded_by IS NULL
           AND (s.actor = 'other' OR s.excluded_reason IS NOT NULL)
        """,
        (since,),
    ).fetchall()
    by_date: dict = defaultdict(set)
    for r in rows:
        by_date[r["local_date"]].add(r["block_id"])
    return [
        _finding(
            "evidence_tainted",
            f"{len(ids)} block(s) on {on} cite a signal that is not evidence "
            f"of HIS attention (actor='other' or excluded)",
            severity="error",
            detail="Inbound mail and test data must never count as his "
                   "attention — the PandaDoc bug (HANDOFF §25), checked "
                   "against what is actually stored in `blocks.evidence`.",
            evidence={"date": str(on), "block_ids": sorted(ids)[:10]}, fp_parts=(on,),
        )
        for on, ids in by_date.items()
    ]


def _check_labelled_without_evidence(conn, since: date) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, local_date FROM blocks
         WHERE local_date >= %s AND superseded_by IS NULL
           AND jsonb_array_length(evidence) = 0
           AND (work_type IS NOT NULL OR project IS NOT NULL)
        """,
        (since,),
    ).fetchall()
    by_date: dict = defaultdict(list)
    for r in rows:
        by_date[r["local_date"]].append(r["id"])
    return [
        _finding(
            "work_type_or_project_without_evidence",
            f"{len(ids)} block(s) on {on} carry a work type or project with "
            f"no evidence at all",
            severity="error",
            evidence={"date": str(on), "block_ids": ids[:10]}, fp_parts=(on,),
        )
        for on, ids in by_date.items()
    ]


def _check_fact_without_fact_evidence(conn, since: date) -> list[dict]:
    rows = conn.execute(
        """
        SELECT b.id, b.local_date FROM blocks b
         WHERE b.local_date >= %s AND b.superseded_by IS NULL
           AND b.source = 'analyst' AND b.confidence = 'fact'
           AND jsonb_array_length(b.evidence) > 0
           AND NOT EXISTS (
               SELECT 1 FROM jsonb_array_elements_text(b.evidence) AS e(sid)
               JOIN signals s ON s.id = e.sid::bigint
              WHERE s.venture_confidence = 'fact'
           )
        """,
        (since,),
    ).fetchall()
    by_date: dict = defaultdict(list)
    for r in rows:
        by_date[r["local_date"]].append(r["id"])
    return [
        _finding(
            "fact_confidence_without_fact_evidence",
            f"{len(ids)} analyst block(s) on {on} are confidence='fact' "
            f"though every signal behind them is only 'inferred'",
            severity="error",
            detail="`build()` marks a slice 'fact' only when every vote "
                   "agreed AND at least one of them already was a fact — "
                   "this checks that invariant against the stored row "
                   "rather than the function that is supposed to enforce it.",
            evidence={"date": str(on), "block_ids": ids[:10]}, fp_parts=(on,),
        )
        for on, ids in by_date.items()
    ]


def _check_stale_mirror(conn, since: date) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, local_date FROM blocks
         WHERE local_date >= %s AND superseded_by IS NOT NULL
           AND calendar_event_id IS NOT NULL
        """,
        (since,),
    ).fetchall()
    by_date: dict = defaultdict(list)
    for r in rows:
        by_date[r["local_date"]].append(r["id"])
    return [
        _finding(
            "stale_mirror_event",
            f"{len(ids)} superseded block(s) on {on} still carry a mirrored "
            f"calendar event id",
            detail="A superseded block's own mirror event should have been "
                   "cleared when its replacement was written (see "
                   "`analyst/mirror.py`'s `_clear`); one still pointing at an "
                   "old event id means the Secretary calendar is out of sync "
                   "with what `blocks` now says.",
            evidence={"date": str(on), "block_ids": ids[:10]}, fp_parts=(on,),
        )
        for on, ids in by_date.items()
    ]


def audit_blocks(conn, since: date) -> dict:
    """Every structural invariant, over stored blocks since `since`."""
    findings: list[dict] = []
    for check in (
        _check_overlaps, _check_below_floor, _check_date_mismatch,
        _check_day_exceeds_wallclock, _check_present_without_evidence,
        _check_evidence_tainted, _check_labelled_without_evidence,
        _check_fact_without_fact_evidence, _check_stale_mirror,
    ):
        try:
            findings += check(conn, since)
        except Exception:  # noqa: BLE001 — one bad check must not lose the rest
            logger.warning("audit: %s failed", check.__name__, exc_info=True)
    return {"findings": findings}


# ---------------------------------------------------------------------------
# 2. question audit
# ---------------------------------------------------------------------------


def _find_code_leaks(text: str, project_codes: list[str]) -> list[str]:
    lowered = text.lower()
    hits: list[str] = []
    for qid in BARE_CODE_QIDS:
        if re.search(rf"\b{re.escape(qid)}\b", lowered):
            hits.append(qid)
    for kind in GAIN_KINDS:
        if f"{kind}:" in lowered:
            hits.append(f"{kind}:")
    for code in project_codes:
        # Hyphenated/underscored codes ("email-writer", "bd-global") are not
        # natural English in any form; a plain lowercase code with no
        # separator ("machina") is ambiguous with a proper noun a human
        # might reasonably type, so only the unambiguous shape is checked.
        if ("-" in code or "_" in code) and re.search(rf"\b{re.escape(code)}\b", lowered):
            hits.append(code)
    return hits


def _project_codes(conn) -> list[str]:
    try:
        return [r["code"] for r in conn.execute("SELECT code FROM projects").fetchall()]
    except Exception:  # noqa: BLE001 — the registry may not exist on an older DB
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


def _question_evidence(conn, options: list[dict]) -> list[dict]:
    """Resolve a question's `signal_ids`/`container` back to real signals."""
    signal_ids = sorted({
        sid for opt in options
        for sid in (opt.get("payload") or {}).get("signal_ids") or []
    })
    if signal_ids:
        return conn.execute(
            "SELECT id, occurred_at, actor FROM signals WHERE id = ANY(%s)",
            (signal_ids,),
        ).fetchall()
    containers = sorted({
        (opt.get("payload") or {}).get("container")
        for opt in options if (opt.get("payload") or {}).get("container")
    })
    if containers:
        return conn.execute(
            "SELECT id, occurred_at, actor FROM signals WHERE container = ANY(%s) "
            "ORDER BY occurred_at DESC LIMIT 200",
            (containers,),
        ).fetchall()
    return []


def _acceptable_local_times(conn, ping: dict, options: list[dict], signals: list[dict]) -> set[str]:
    acceptable = {_hhmm(s["occurred_at"]) for s in signals}
    for key in ("covers_from", "covers_to", "window_start", "window_end"):
        value = ping.get(key)
        if value:
            acceptable.add(_hhmm(value))
    for opt in options:
        payload = opt.get("payload") or {}
        for key in ("starts_at", "ends_at"):
            raw = payload.get(key)
            if raw:
                try:
                    acceptable.add(_hhmm(datetime.fromisoformat(raw)))
                except ValueError:
                    pass
        block_id = payload.get("block_id")
        if block_id is not None:
            brow = conn.execute(
                "SELECT starts_at, ends_at FROM blocks WHERE id = %s", (block_id,)
            ).fetchone()
            if brow:
                acceptable.add(_hhmm(brow["starts_at"]))
                acceptable.add(_hhmm(brow["ends_at"]))
    return acceptable


def _questions_list(raw) -> list[dict]:
    """`pings.questions` is stored as a bare JSON array; accept the wrapped
    `{"questions": [...]}` shape too, the same defensive read `pings.answers.
    _find_option` uses for a snapshot that predates a schema change."""
    if isinstance(raw, dict):
        return raw.get("questions", []) or []
    return raw or []


def audit_questions(conn, since: date) -> dict:
    pings = conn.execute(
        """
        SELECT id, kind, local_date, covers_from, covers_to, window_start,
               window_end, questions
          FROM pings
         WHERE local_date >= %s AND kind IN ('midday', 'evening')
         ORDER BY local_date
        """,
        (since,),
    ).fetchall()

    project_codes = _project_codes(conn)

    ping_ids = [p["id"] for p in pings]
    answer_rows = []
    if ping_ids:
        answer_rows = conn.execute(
            """
            SELECT meta ->> 'ping_id' AS ping_id, meta ->> 'qid' AS qid,
                   count(*) AS n,
                   bool_or(superseded_by IS NOT NULL) AS any_superseded
              FROM context_entries
             WHERE source = 'ping' AND meta ->> 'ping_id' = ANY(%s)
             GROUP BY 1, 2
            """,
            ([str(i) for i in ping_ids],),
        ).fetchall()
    answers_by_pair = {
        (r["ping_id"], r["qid"]): r for r in answer_rows if r.get("qid")
    }

    findings: list[dict] = []
    total_questions = 0
    answered_pairs: set = set()
    corrected_pairs: set = set()

    for ping in pings:
        for q in _questions_list(ping["questions"]):
            total_questions += 1
            qid = q.get("qid")
            text = q.get("text") or ""
            options = q.get("options") or []
            pair = (str(ping["id"]), qid)

            row = answers_by_pair.get(pair)
            if row:
                answered_pairs.add(pair)
                if int(row["n"]) > 1 or row.get("any_superseded"):
                    corrected_pairs.add(pair)

            try:
                signals = _question_evidence(conn, options)
            except Exception:  # noqa: BLE001 — one bad question must not lose the rest
                logger.warning("audit: could not resolve evidence for ping %s [%s]",
                               ping["id"], qid, exc_info=True)
                signals = []

            if signals and all(s["actor"] == "other" for s in signals):
                findings.append(_finding(
                    "question_evidence_all_other",
                    f"ping {ping['id']} [{qid}] on {ping['local_date']} cites only "
                    f"actor='other' signals: {text[:100]!r}",
                    severity="error",
                    detail="Inbound mail is demand, not attention — asking "
                           "whether it was 'his to do' when nothing HE wrote "
                           "is behind it is the PandaDoc bug (HANDOFF §25).",
                    evidence={"ping_id": ping["id"], "qid": qid,
                              "local_date": str(ping["local_date"])},
                    fp_parts=(ping["id"], qid),
                ))
            elif not signals and qid in EVIDENCE_CLAIMING_QIDS and _names_something(text):
                findings.append(_finding(
                    "question_no_evidence_named_thread",
                    f"ping {ping['id']} [{qid}] on {ping['local_date']} names "
                    f"something but cites no signal at all: {text[:100]!r}",
                    evidence={"ping_id": ping["id"], "qid": qid,
                              "local_date": str(ping["local_date"])},
                    fp_parts=(ping["id"], qid),
                ))

            try:
                acceptable = _acceptable_local_times(conn, ping, options, signals)
            except Exception:  # noqa: BLE001
                logger.warning("audit: could not compute acceptable times for ping %s [%s]",
                               ping["id"], qid, exc_info=True)
                acceptable = set()
            found_times = {f"{h}:{m}" for h, m in TIME_RE.findall(text)}
            bad_times = found_times - acceptable
            if bad_times:
                findings.append(_finding(
                    "question_time_mismatch",
                    f"ping {ping['id']} [{qid}] on {ping['local_date']} shows a "
                    f"time not in local time for any cited signal or window: "
                    f"{sorted(bad_times)}",
                    severity="error",
                    detail=f"text: {text!r}\nacceptable local times: {sorted(acceptable)}",
                    evidence={"ping_id": ping["id"], "qid": qid,
                              "bad_times": sorted(bad_times)},
                    fp_parts=(ping["id"], qid),
                ))

            leaked = _find_code_leaks(text, project_codes)
            if leaked:
                findings.append(_finding(
                    "question_internal_code_leak",
                    f"ping {ping['id']} [{qid}] on {ping['local_date']} shows an "
                    f"internal code bare ({', '.join(leaked)}): {text[:100]!r}",
                    evidence={"ping_id": ping["id"], "qid": qid, "codes": leaked},
                    fp_parts=(ping["id"], qid),
                ))

    answer_rate = len(answered_pairs) / total_questions if total_questions else None
    corrected_rate = len(corrected_pairs) / len(answered_pairs) if answered_pairs else None

    return {
        "findings": findings,
        "pings_checked": len(pings),
        "total_questions": total_questions,
        "answered": len(answered_pairs),
        "answer_rate": answer_rate,
        "corrected": len(corrected_pairs),
        "corrected_rate": corrected_rate,
    }


# ---------------------------------------------------------------------------
# 3. shadow-calendar accuracy
# ---------------------------------------------------------------------------


def _predecessor(conn, block_id: int, *, _seen: set | None = None) -> dict | None:
    """Walk `superseded_by` backwards to the nearest `source='analyst'` row.

    A ping confirmation supersedes exactly one row (`_write_block_correction`
    in `pings/answers.py`); if that row was itself an earlier ping/human
    correction, keep walking until an analyst-produced guess is found, or the
    chain runs out.
    """
    seen = _seen if _seen is not None else set()
    rows = conn.execute(
        "SELECT id, source, venture, attention FROM blocks WHERE superseded_by = %s",
        (block_id,),
    ).fetchall()
    if not rows:
        return None
    row = rows[0]
    if row["id"] in seen:
        return None
    if row["source"] == "analyst":
        return row
    seen.add(row["id"])
    return _predecessor(conn, row["id"], _seen=seen)


def _ping_confirmed_cases(conn, since: date) -> list[dict]:
    confirmed = conn.execute(
        """
        SELECT id, local_date, starts_at, ends_at, venture, attention
          FROM blocks
         WHERE local_date >= %s AND source = 'ping'
         ORDER BY local_date, starts_at
        """,
        (since,),
    ).fetchall()
    cases = []
    for row in confirmed:
        before = _predecessor(conn, row["id"])
        if before is None:
            continue
        cases.append({
            "date": str(row["local_date"]),
            "span": f"{_hhmm(row['starts_at'])}-{_hhmm(row['ends_at'])}",
            "kind": "split_gap_tap",
            "confirmed": {"venture": row["venture"], "attention": row["attention"]},
            "analyst_before": {"venture": before["venture"], "attention": before["attention"]},
            "venture_match": before["venture"] == row["venture"],
            "attention_match": before["attention"] == row["attention"],
        })
    return cases


def _attendance_cases(conn, since: date) -> list[dict]:
    """`attended` taps (item 4) as confirmed spans.

    An attendance answer never touches `blocks` at tap time — it is read back
    on the NEXT `reconstruct()` of that day (`analyst.blocks.
    _apply_attendance_answers`) — so there is no `superseded_by` chain to
    walk here. "Before the confirmation" is approximated as the most
    recently created block covering the span that already existed at the
    moment he answered (`created_at <= the tap's occurred_at`), which is the
    closest available reading of "what the analyst had said up to then".
    """
    rows = conn.execute(
        """
        SELECT id, meta, occurred_at FROM context_entries
         WHERE type = 'work_log' AND 'attendance' = ANY(tags)
           AND superseded_by IS NULL AND occurred_at >= %s
        """,
        (datetime.combine(since, datetime.min.time(), tzinfo=_tz()).astimezone(timezone.utc),),
    ).fetchall()

    cases = []
    for row in rows:
        meta = row.get("meta") or {}
        answer = meta.get("attended")
        starts_raw, ends_raw = meta.get("starts_at"), meta.get("ends_at")
        if not (answer and starts_raw and ends_raw):
            continue
        try:
            span_start = datetime.fromisoformat(starts_raw)
            span_end = datetime.fromisoformat(ends_raw)
        except ValueError:
            continue
        local_date = span_start.astimezone(_tz()).date()

        candidate = conn.execute(
            """
            SELECT venture, attention FROM blocks
             WHERE local_date = %s AND starts_at < %s AND ends_at > %s
               AND created_at <= %s
             ORDER BY created_at DESC LIMIT 1
            """,
            (local_date, span_end, span_start, row["occurred_at"]),
        ).fetchone()
        if candidate is None:
            continue

        confirmed_present = answer in ("yes", "part")
        analyst_said_present = (
            candidate["venture"] == "family" and candidate["attention"] == "present"
        )
        agree = analyst_said_present == confirmed_present
        cases.append({
            "date": str(local_date),
            "span": f"{_hhmm(span_start)}-{_hhmm(span_end)}",
            "kind": "attendance_tap",
            "confirmed": {"attended": answer},
            "analyst_before": {"venture": candidate["venture"], "attention": candidate["attention"]},
            "venture_match": agree,
            "attention_match": agree,
        })
    return cases


def _coverage_report(conn, since: date) -> dict:
    from ..analyst.blocks import workday_bounds

    rows = conn.execute(
        """
        SELECT local_date, starts_at, ends_at, attention, venture,
               intent_title, jsonb_array_length(evidence) AS n_evidence
          FROM blocks
         WHERE local_date >= %s AND superseded_by IS NULL
         ORDER BY local_date, starts_at
        """,
        (since,),
    ).fetchall()

    by_day: dict = defaultdict(list)
    for r in rows:
        by_day[r["local_date"]].append(r)

    per_day: dict = {}
    total_present = total_present_thin = total_any_evidence = total_workday = 0.0

    for on, day_rows in by_day.items():
        minutes = {"present": 0.0, "displaced": 0.0, "ambiguous": 0.0, "untracked": 0.0}
        any_evidence = present = present_thin = 0.0
        for r in day_rows:
            m = (r["ends_at"] - r["starts_at"]).total_seconds() / 60
            n_evidence = r.get("n_evidence") or 0
            # `_untracked()` never persisted a boolean column for this — it
            # is reconstructed here from the exact shape that function
            # writes: no venture, no intent, no evidence at all.
            is_untracked = (
                r["attention"] == "ambiguous" and r["venture"] is None
                and r["intent_title"] is None and n_evidence == 0
            )
            minutes["untracked" if is_untracked else r["attention"]] += m
            if n_evidence > 0:
                any_evidence += m
            if r["attention"] == "present":
                present += m
                if n_evidence == 1:
                    present_thin += m

        w_start, w_end = workday_bounds(on)
        workday_minutes = (w_end - w_start).total_seconds() / 60

        per_day[str(on)] = {
            "minutes": {k: round(v) for k, v in minutes.items()},
            "workday_minutes": round(workday_minutes),
            "any_evidence_minutes": round(any_evidence),
            "any_evidence_share": round(any_evidence / workday_minutes, 3) if workday_minutes else None,
            "present_minutes": round(present),
            "present_thin_evidence_minutes": round(present_thin),
            "present_thin_evidence_share": round(present_thin / present, 3) if present else None,
        }
        total_present += present
        total_present_thin += present_thin
        total_any_evidence += any_evidence
        total_workday += workday_minutes

    return {
        "per_day": per_day,
        "overall": {
            "any_evidence_share": (
                round(total_any_evidence / total_workday, 3) if total_workday else None
            ),
            "present_thin_evidence_share": (
                round(total_present_thin / total_present, 3) if total_present else None
            ),
        },
    }


def audit_accuracy(conn, since: date) -> dict:
    cases = _ping_confirmed_cases(conn, since) + _attendance_cases(conn, since)
    total = len(cases)
    venture_matched = sum(1 for c in cases if c["venture_match"])
    attention_matched = sum(1 for c in cases if c["attention_match"])
    return {
        "confirmed_spans": total,
        "venture_matched": venture_matched,
        "attention_matched": attention_matched,
        "cases": cases,
        "coverage": _coverage_report(conn, since),
    }


# ---------------------------------------------------------------------------
# putting it together
# ---------------------------------------------------------------------------


def run(
    conn, *, days: int = DEFAULT_DAYS, record: bool = True,
    sections: tuple[str, ...] = SECTIONS,
) -> dict:
    since = _local_today() - timedelta(days=days)
    result: dict = {"days": days, "since": str(since), "sections": {}}
    all_findings: list[dict] = []

    if "blocks" in sections:
        b = audit_blocks(conn, since)
        all_findings += b["findings"]
        result["sections"]["blocks"] = {"violations": len(b["findings"]), "findings": b["findings"]}

    if "questions" in sections:
        q = audit_questions(conn, since)
        all_findings += q["findings"]
        result["sections"]["questions"] = q

    if "accuracy" in sections:
        result["sections"]["accuracy"] = audit_accuracy(conn, since)

    recorded = 0
    if record:
        for f in all_findings:
            try:
                obs.record(conn, **f)
                recorded += 1
            except Exception:  # noqa: BLE001 — recording a finding must not break the audit
                logger.warning("audit: could not record %s", f.get("kind"), exc_info=True)

    result["findings_total"] = len(all_findings)
    result["recorded"] = recorded
    return result


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def format_report(result: dict) -> str:
    lines = [f"IBLU audit — last {result['days']} day(s), since {result['since']}", ""]

    blocks = result["sections"].get("blocks")
    if blocks is not None:
        lines.append(f"## Shadow calendar — structural invariants ({blocks['violations']} violation(s))")
        if not blocks["findings"]:
            lines.append("  clean — no structural invariant was violated.")
        for f in blocks["findings"]:
            lines.append(f"  [{f['severity']}] {f['summary']}")
        lines.append("")

    q = result["sections"].get("questions")
    if q is not None:
        lines.append(f"## Questions — {q['pings_checked']} ping(s), {q['total_questions']} question(s)")
        if q["answer_rate"] is None:
            lines.append("  answer rate: not enough questions yet (0)")
        else:
            lines.append(f"  answer rate: {q['answered']}/{q['total_questions']} ({q['answer_rate']:.0%})")
        if q["corrected_rate"] is None:
            lines.append("  later corrected: not enough answered questions yet (0)")
        else:
            lines.append(
                f"  later corrected: {q['corrected']}/{q['answered']} "
                f"({q['corrected_rate']:.0%}) — a proxy for 'wrong'"
            )
        if not q["findings"]:
            lines.append("  no per-question defects found (evidence, time strings, jargon leaks).")
        for f in q["findings"]:
            lines.append(f"  [{f['severity']}] {f['summary']}")
        lines.append("")

    a = result["sections"].get("accuracy")
    if a is not None:
        lines.append("## Shadow-calendar accuracy")
        n = a["confirmed_spans"]
        if n < MIN_CONFIRMED_SPANS:
            lines.append(f"  not enough confirmed spans yet ({n})")
        else:
            lines.append(f"  venture agreement: {a['venture_matched']}/{n} ({a['venture_matched'] / n:.0%})")
            lines.append(f"  attention agreement: {a['attention_matched']}/{n} ({a['attention_matched'] / n:.0%})")
        for c in a["cases"]:
            mark = "match" if c["venture_match"] and c["attention_match"] else "MISMATCH"
            lines.append(
                f"    {c['date']} {c['span']} [{c['kind']}] {mark} — "
                f"analyst said {c['analyst_before']} vs confirmed {c['confirmed']}"
            )
        lines.append("")
        lines.append("  coverage, per day (present/displaced/ambiguous/untracked minutes):")
        for on, d in sorted(a["coverage"]["per_day"].items()):
            share = f"{d['any_evidence_share']:.0%}" if d["any_evidence_share"] is not None else "n/a"
            lines.append(
                f"    {on}: present={d['minutes']['present']}m "
                f"displaced={d['minutes']['displaced']}m "
                f"ambiguous={d['minutes']['ambiguous']}m "
                f"untracked={d['minutes']['untracked']}m "
                f"| any-evidence={share} of the workday"
            )
        overall = a["coverage"]["overall"]
        if overall["any_evidence_share"] is not None:
            lines.append(f"  overall any-evidence share of the workday: {overall['any_evidence_share']:.0%}")
        else:
            lines.append("  overall any-evidence share: not enough workday minutes to compute")
        if overall["present_thin_evidence_share"] is not None:
            lines.append(
                f"  share of 'present' minutes resting on a single signal (thin "
                f"evidence): {overall['present_thin_evidence_share']:.0%}"
            )
        else:
            lines.append("  thin-evidence share: no 'present' minutes to measure")

    return "\n".join(lines)


def one_line_summary(result: dict) -> str:
    parts = []
    blocks = result["sections"].get("blocks")
    if blocks is not None:
        parts.append(f"{blocks['violations']} block violation(s)")
    q = result["sections"].get("questions")
    if q is not None:
        parts.append(f"{len(q['findings'])} question defect(s)")
        if q["answer_rate"] is not None:
            parts.append(f"answer rate {q['answer_rate']:.0%}")
    a = result["sections"].get("accuracy")
    if a is not None:
        if a["confirmed_spans"] >= MIN_CONFIRMED_SPANS:
            parts.append(f"venture accuracy {a['venture_matched']}/{a['confirmed_spans']}")
        else:
            parts.append(f"only {a['confirmed_spans']} confirmed span(s) so far")
    note = f"{result['recorded']} observation(s) recorded" if result.get("recorded") else "no observations recorded"
    return "audit: " + ", ".join(parts) + f" — {note}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m iblu_keeper.jobs.audit",
        description="A deterministic accuracy audit of the shadow calendar and "
                     "the questions IBLU sends, over real recorded data.",
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-record", action="store_true", help="print only; write no observations")
    parser.add_argument(
        "--section", choices=SECTIONS, action="append", dest="sections",
        help="repeatable; default runs all three",
    )
    args = parser.parse_args(argv)

    # stderr, not stdout: `--json` promises machine-readable output, and a
    # log line ahead of the JSON (even just "db: connection pool opened")
    # would break every consumer that pipes this straight into a parser.
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    sections = tuple(args.sections) if args.sections else SECTIONS

    result: dict | None = None
    try:
        if settings.use_mock:
            print("audit: refusing to run in mock mode (DRY_RUN=true) — nothing real to audit")
        elif not db.is_configured():
            print("audit: no DATABASE_URL — nothing to audit")
        else:
            with db.get_conn() as conn:
                result = run(conn, days=args.days, record=not args.no_record, sections=sections)
    except Exception:  # noqa: BLE001 — a report is not a failure; never raise out of main
        logger.exception("audit: failed")
        print("audit: failed — see the log above")
    finally:
        db.close_pool()

    if result is not None:
        summary = one_line_summary(result)
        if args.json:
            # Machine-readable means exactly one JSON value on stdout — the
            # summary is embedded as a field rather than printed as a
            # trailing line, so `json.loads(stdout)` never has to guess
            # where the JSON ends.
            result["summary"] = summary
            print(json.dumps(result, indent=2, default=str))
        else:
            print(format_report(result))
            print(summary)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
