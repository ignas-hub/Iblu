"""IBLU checking its own work.

The recorder runs unattended: a tick every ten minutes, an analyst pass twice a
day, a review once a week. Nothing in that loop has a human in it, so the
failure mode that matters is not a crash — a crash is loud — but a system that
keeps running while quietly producing nonsense. A collector whose watermark
stopped advancing, a day reconstructed as 100% unknown, an account whose Chat
messages all look like someone else's: each of those looks exactly like a quiet
week.

Two passes, kept apart on purpose:

  * `run_rules` — deterministic invariants. Blocks must not overlap, a
    confirmed block must never be superseded by a guess, a watermark must move.
    A finding here is a **fact**: something that cannot be true is true.
  * `run_llm` — the model reads a compact snapshot of what the scripts just
    produced and says what looks wrong. A finding here is a **lead**. It may be
    mistaken, it is recorded as `detected_by='llm'`, and nothing acts on it
    automatically.

Both write to `observations`, so "what has IBLU been catching?" has one answer
in one place. Neither is ever allowed to fail the job that called it.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from ..config import settings
from ..store import observations as obs

logger = logging.getLogger("iblu_keeper.analyst.sensecheck")

# A day in which almost nothing could be attributed is not a quiet day; it is a
# blind recorder. The threshold is high on purpose — early on, most of the day
# genuinely is unobserved, and crying wolf would train Ignas to ignore this.
UNTRACKED_ALARM_PCT = 95

# How long a collector may go without its watermark moving before that is worth
# saying out loud. Longer than a weekend, so Monday morning is not a false alarm.
STALE_WATERMARK_HOURS = 72


# --- deterministic invariants ----------------------------------------------


def run_rules(conn, on: date) -> list[dict]:
    """Check what must be true. Every finding here is a fact, not an opinion."""
    found: list[dict] = []

    def _flag(kind, summary, *, severity="warn", detail=None, evidence=None, fp_parts=()):
        found.append({
            "source": "sensecheck", "kind": kind, "summary": summary,
            "severity": severity, "detail": detail, "evidence": evidence or {},
            "detected_by": "rule",
            "fp": obs.fingerprint("sensecheck", kind, *fp_parts),
        })

    # 1. Two live blocks may never cover the same minute.
    overlaps = conn.execute(
        """
        SELECT a.id AS a, b.id AS b, a.starts_at, a.ends_at
          FROM blocks a JOIN blocks b
            ON a.local_date = b.local_date AND a.id < b.id
           AND a.superseded_by IS NULL AND b.superseded_by IS NULL
           AND a.starts_at < b.ends_at AND b.starts_at < a.ends_at
         WHERE a.local_date = %s
        """,
        (on,),
    ).fetchall()
    if overlaps:
        _flag(
            "blocks_overlap",
            f"{len(overlaps)} pair(s) of blocks overlap on {on} — the day double-counts itself",
            severity="error",
            detail="Two live blocks covering the same minute means every minutes "
                   "figure for that day is inflated. Likely a reconstruct that "
                   "did not supersede cleanly, or two runs racing.",
            evidence={"date": str(on), "pairs": [[r["a"], r["b"]] for r in overlaps[:10]]},
            fp_parts=(on,),
        )

    # 2. A confirmed block must never be replaced by a guess.
    clobbered = conn.execute(
        """
        SELECT id FROM blocks
         WHERE local_date = %s AND confidence = 'fact' AND superseded_by IS NOT NULL
           AND source <> 'ping'
        """,
        (on,),
    ).fetchall()
    if clobbered:
        _flag(
            "fact_block_superseded",
            f"{len(clobbered)} block(s) Ignas confirmed on {on} were superseded by a reconstruction",
            severity="error",
            detail="A tap is truth and a reconstruction is a guess. If a guess "
                   "superseded a fact, `_carve_out` or the supersede filter in "
                   "`analyst/blocks.reconstruct` has regressed.",
            evidence={"date": str(on), "block_ids": [r["id"] for r in clobbered[:10]]},
            fp_parts=(on,),
        )

    # 3. Blocks below the floor should not exist.
    slivers = conn.execute(
        """
        SELECT count(*) AS n FROM blocks
         WHERE local_date = %s AND superseded_by IS NULL
           AND ends_at - starts_at < interval '15 minutes'
        """,
        (on,),
    ).fetchone()
    if slivers and slivers["n"]:
        _flag(
            "block_below_floor",
            f"{slivers['n']} block(s) on {on} are shorter than the 15-minute floor",
            detail="The floor exists because the evidence cannot support finer "
                   "resolution. A sliver means an interval was cut without being "
                   "re-checked against FLOOR.",
            evidence={"date": str(on), "count": slivers["n"]},
            fp_parts=(on,),
        )

    # 4. A day that reconstructed into almost nothing.
    day = conn.execute(
        """
        SELECT
          sum(EXTRACT(EPOCH FROM (ends_at - starts_at))/60) AS total,
          sum(CASE WHEN venture IS NULL
                   THEN EXTRACT(EPOCH FROM (ends_at - starts_at))/60 ELSE 0 END) AS unknown
        FROM blocks WHERE local_date = %s AND superseded_by IS NULL
        """,
        (on,),
    ).fetchone()
    if day and day["total"]:
        pct = round(100 * float(day["unknown"]) / float(day["total"]))
        if pct >= UNTRACKED_ALARM_PCT:
            _flag(
                "day_almost_entirely_unknown",
                f"{pct}% of {on} could not be attributed to anything",
                detail="Not necessarily wrong — an unobserved day is a real "
                       "thing, and silence is never presence. But at this level "
                       "it is worth checking that the collectors actually ran, "
                       "rather than reading it as a quiet day.",
                evidence={"date": str(on), "untracked_pct": pct},
                fp_parts=(on,),
            )

    # 5. Collectors: errors, and watermarks that stopped moving.
    for row in conn.execute(
        "SELECT name, watermark, last_run_at, last_error FROM collector_state"
    ).fetchall():
        if row["last_error"]:
            _flag(
                "collector_error",
                f"collector {row['name']} last failed: {row['last_error'][:120]}",
                severity="error",
                detail="One collector failing never stops the others, which is "
                       "why this can go unnoticed for days.",
                evidence={"collector": row["name"], "error": row["last_error"][:500]},
                fp_parts=(row["name"], (row["last_error"] or "")[:60]),
            )
        stale = (
            row["watermark"]
            and row["last_run_at"]
            and row["last_run_at"] - row["watermark"] > timedelta(hours=STALE_WATERMARK_HOURS)
        )
        if stale:
            _flag(
                "watermark_not_advancing",
                f"collector {row['name']} has run recently but its watermark is "
                f"{(row['last_run_at'] - row['watermark']).days} days behind",
                detail="The collector is running and finding nothing new. Either "
                       "that is true, or its query stopped matching — the second "
                       "looks identical to the first from the outside.",
                evidence={
                    "collector": row["name"],
                    "watermark": str(row["watermark"]),
                    "last_run_at": str(row["last_run_at"]),
                },
                fp_parts=(row["name"],),
            )

    # 6. The mission on disk drifting from the seeded copy.
    try:
        from .. import db

        if db.mission_sha() and db.read_mission_file():
            import hashlib

            on_disk = hashlib.sha256(db.read_mission_file().encode()).hexdigest()
            if not db.mission_sha().startswith(on_disk[:12]) and db.mission_sha() != on_disk:
                _flag(
                    "mission_stale",
                    "docs/MISSION.md has changed but was not re-seeded",
                    detail="Every LLM call carries the SEEDED mission, not the "
                           "file. Until `db seed-mission` runs, the edit exists "
                           "in the repo and nowhere else.",
                    evidence={"seeded_sha": db.mission_sha()[:12], "file_sha": on_disk[:12]},
                    fp_parts=(on_disk[:12],),
                )
    except Exception:  # noqa: BLE001 — a check must never break the checker
        logger.debug("sensecheck: mission comparison unavailable", exc_info=True)

    return found


# --- the LLM pass -----------------------------------------------------------

SYSTEM = """You are checking another program's work, not doing it.

You are shown what IBLU's scripts produced for one day: the reconstructed
blocks, a sample of the signals behind them, and the state of each collector.
Your job is to say what looks WRONG — misattributed work, a venture that makes
no sense for that counterpart, a stretch labelled confidently from thin
evidence, a collector that looks broken rather than idle, a day whose shape
contradicts its own evidence.

What is NOT wrong, and must never be reported:
- Unobserved time. Silence is never presence; a gap is unknown, not an error.
- A day with little activity. Quiet days exist.
- Anything you would only flag to seem useful. An empty list is the correct
  answer most days, and a false alarm costs more than a miss here: it trains
  the reader to ignore you.

`kind` MUST be one of these exactly — they are how repeats of the same problem
are recognised, so inventing a new slug for a problem already listed here
creates a duplicate rather than a new finding:
  misattributed_venture   — a block's venture contradicts its own evidence
  thin_evidence           — a confident label resting on very little
  test_data_as_work       — a placeholder or test message counted as real work
  collector_stalled       — a collector looks broken rather than idle
  timeline_implausible    — the day's shape contradicts its evidence
  calendar_mismatch       — blocks and calendar events disagree
  other                   — none of the above; say why in detail

Return only JSON: {"findings": [{"kind": "<one of the above>", "severity":
"info"|"warn"|"error", "summary": "<one line>", "detail": "<why you think so,
and what would confirm or refute it>"}]}
At most 5 findings. Return {"findings": []} when nothing looks wrong."""

# The closed set above. A slug outside it becomes 'other' rather than its own
# bucket — free-text kinds meant the same problem, reworded, opened a new row
# every run.
LLM_KINDS = (
    "misattributed_venture", "thin_evidence", "test_data_as_work",
    "collector_stalled", "timeline_implausible", "calendar_mismatch", "other",
)


def _snapshot(conn, on: date) -> str:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.iblu_timezone)
    blocks = conn.execute(
        """
        SELECT starts_at, ends_at, venture, work_type, project, attention,
               confidence, reasoning, intent_title
          FROM blocks WHERE local_date = %s AND superseded_by IS NULL
         ORDER BY starts_at
        """,
        (on,),
    ).fetchall()
    signals = conn.execute(
        """
        SELECT source, account, occurred_at, counterpart, subject, venture,
               venture_confidence, work_type
          FROM signals
         WHERE occurred_at::date = %s AND actor = 'me'
         ORDER BY occurred_at LIMIT 60
        """,
        (on,),
    ).fetchall()
    collectors = conn.execute(
        "SELECT name, watermark, last_run_at, last_error FROM collector_state"
    ).fetchall()

    lines = [f"Day: {on} (local time below, {settings.iblu_timezone})", "", "BLOCKS:"]
    for b in blocks:
        lines.append(
            f"  {b['starts_at'].astimezone(tz):%H:%M}-{b['ends_at'].astimezone(tz):%H:%M} "
            f"{b['venture'] or '-'}/{b['work_type'] or '-'}/{b['project'] or '-'} "
            f"{b['attention']} {b['confidence']} :: {b['reasoning']}"
        )
    lines += ["", "SIGNALS HE SENT:"]
    for s in signals:
        lines.append(
            f"  {s['occurred_at'].astimezone(tz):%H:%M} {s['source']} "
            f"[{s['account']}] -> {(s['counterpart'] or '')[:40]} "
            f"\"{(s['subject'] or '')[:60]}\" venture={s['venture'] or '-'}"
            f"({s['venture_confidence']})"
        )
    lines += ["", "COLLECTORS:"]
    for c in collectors:
        lines.append(
            f"  {c['name']}: watermark={c['watermark']} last_run={c['last_run_at']} "
            f"error={(c['last_error'] or 'none')[:80]}"
        )
    return "\n".join(lines)


def run_llm(conn, on: date) -> list[dict]:
    """Ask the model what looks wrong. Returns leads, never facts."""
    if not settings.anthropic_api_key:
        return []
    import anthropic

    snapshot = _snapshot(conn, on)
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60.0)
    # NOTE: no `temperature` — sampling params return 400 on Sonnet 5.
    response = client.messages.create(
        model=settings.iblu_check_model,
        max_tokens=1500,
        system=SYSTEM,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": snapshot}],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    payload = json.loads(text)

    out = []
    for f in (payload.get("findings") or [])[:5]:
        summary = str(f.get("summary") or "").strip()
        if not summary:
            continue
        severity = f.get("severity")
        kind = str(f.get("kind") or "other").strip().lower()
        if kind not in LLM_KINDS:
            kind = "other"
        out.append({
            "source": "sensecheck",
            "kind": kind,
            "severity": severity if severity in obs.SEVERITIES else "info",
            "summary": summary[:300],
            "detail": (str(f.get("detail") or "")[:2000]) or None,
            "evidence": {"date": str(on)},
            "detected_by": "llm",
            # Deliberately NOT the summary: the same problem described in
            # slightly different words is the same problem, and fingerprinting
            # on the wording opened a fresh row on every run.
            "fp": obs.fingerprint("sensecheck", "llm", kind, on),
        })
    return out


# --- the pass the jobs call -------------------------------------------------


def run(conn, on: date, *, use_llm: bool = True) -> dict:
    """Both passes, recorded. Never raises — a checker must not break its job."""
    findings: list[dict] = []
    try:
        findings += run_rules(conn, on)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sensecheck: rules pass failed (%s)", exc, exc_info=True)

    llm_ok = False
    if use_llm:
        try:
            findings += run_llm(conn, on)
            llm_ok = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("sensecheck: llm pass unavailable (%s)", exc)
            # The check failing is itself worth knowing about — quietly losing
            # the second opinion is exactly the silence this module exists for.
            obs.record_safe(
                source="sensecheck", kind="llm_sensecheck_unavailable",
                severity="info",
                summary="the LLM sense-check could not run",
                detail=str(exc)[:500],
                evidence={"date": str(on)},
                fp=obs.fingerprint("sensecheck", "llm_unavailable"),
            )

    recorded = 0
    for f in findings:
        try:
            obs.record(conn, **f)
            recorded += 1
        except Exception:  # noqa: BLE001
            logger.warning("sensecheck: could not record %s", f.get("kind"), exc_info=True)

    logger.info(
        "sensecheck %s: %d finding(s) recorded (llm=%s)", on, recorded, llm_ok
    )
    return {
        "date": str(on),
        "findings": recorded,
        "rules": sum(1 for f in findings if f["detected_by"] == "rule"),
        "llm": sum(1 for f in findings if f["detected_by"] == "llm"),
        "llm_ran": llm_ok,
    }
