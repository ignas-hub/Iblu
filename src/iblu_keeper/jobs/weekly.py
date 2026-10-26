"""The weekly review — the uncomfortable numbers, delivered unasked.

    python -m iblu_keeper.jobs.weekly [--dry] [--window 7d] [--yes]

Mission stage 2: "a weekly review: attention per venture and work type, what he
touched three times that should be someone else's, what to automate.
Uncomfortable numbers are the product."

On-demand review is a review that gets read when Ignas remembers to ask. This
posts it into the Secretary space on Friday evening whether he asks or not.

It reuses the ping delivery path (same webhook, same space) but sends plain
text, not a card: there is nothing to tap. The point is to be read.

Restructured per plan §1.7 (2026-09-13): three sections, in this fixed order,
because gains come first, always —

    1. Gains  — what exists now that didn't at the start of the window.
    2. Truth  — the uncomfortable numbers `tools.review` already computes.
    3. One removal — exactly one project and one concrete step forward.

The reason the ordering and the language matter this much is in the plan's
§7: Ignas measures himself against ideals he hasn't reached and beats himself
up for the distance (the Gap). This review's entire purpose is the opposite —
evidence, measured backward from a baseline (the Gain) — so every section
below, and the validator in `review_language`, exists to protect that.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError, field_validator

from .. import db
from ..config import settings
from ..tools import review as review_tools
from .review_language import validate_language

logger = logging.getLogger("iblu_keeper.jobs.weekly")

TIMEOUT = 20
# Below this, a "review" is noise dressed as insight.
MIN_SIGNALS = 10


def _guard() -> str | None:
    if settings.use_mock:
        return "refusing to run in mock mode (DRY_RUN=true)"
    if not db.is_configured():
        return "refusing to run without DATABASE_URL"
    if not settings.secretary_webhook_url:
        return "refusing to run without SECRETARY_WEBHOOK_URL"
    return None


def _week_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Monday 00:00 -> now, in `settings.iblu_timezone`.

    Plan §1.7: the review window is Monday 00:00 -> Friday 18:00 local. The
    timer fires at Friday 18:00, but that is not a fixed 7-day duration — a
    "7d" lookback run on, say, Wednesday would bleed into last week. Running
    by hand mid-week (for testing, or because Ignas asks early) should show
    "this week so far" instead.
    """
    tz = ZoneInfo(settings.iblu_timezone)
    now_utc = now or datetime.now(timezone.utc)
    local_now = now_utc.astimezone(tz)
    monday_local = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday_local.astimezone(timezone.utc), now_utc


# --------------------------------------------------------------------------
# Gains — an LLM may phrase the fixed evidence list; it may never add to it.
# --------------------------------------------------------------------------

# Verbatim per plan §4.6 — carried by every LLM call in this system (composer,
# review, reconstruct, statement), not paraphrased, so the rule is auditable
# in the transcript rather than trusted to have been "roughly" followed.
GAIN_RULE = (
    "Measure backward from the baselines, never against an ideal, a goal, a "
    "competitor or another person. State gains as dated evidence. Never "
    "praise generically. Never present a plan as a gain. Gains first."
)

SYSTEM = f"""You write the opening "Gains" section of a weekly review for a \
founder who grades himself against ideals he hasn't reached and beats himself \
up for the distance he has not closed. This system exists to do the opposite.

{GAIN_RULE}

You are given a FIXED list of things that already happened this window, each \
already dated. Turn only that list into 3-6 short sentences that name what \
happened and when. Rules:
- Do not add anything not in the list. Do not invent totals, adjectives, or
  comparisons to anything not in the list.
- Never use future tense anywhere ("will", "going to", "plan to", "next week
  I..."). This section is about what already happened, not what happens next.
- No comparison to other people or companies.
- No generic praise ("great job", "amazing", "well done", "impressive",
  "crushing it").
- Plain, flat sentences. Output STRICT JSON of the shape {{"summary": "..."}}.
  No markdown, no commentary."""


class GainsSummary(BaseModel):
    """The Gains section's prose. The model's only degree of freedom is
    phrasing — the evidence itself comes from `tools.review.gains`."""

    summary: str

    @field_validator("summary")
    @classmethod
    def _bounded(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("empty gains summary")
        return v[:1500]


def _gains_prompt(gains_data: dict) -> str:
    lines: list[str] = []
    for kind in ("learned", "progressed", "experienced"):
        for item in gains_data.get(kind, []):
            lines.append(f"[{kind}] {item['date']} — {item['text']}")
    body = "\n".join(lines) if lines else "(nothing computed this window)"
    return f"Evidence (already true, already dated):\n{body}"


def compose_gains_llm(gains_data: dict) -> GainsSummary:
    """Ask the model to phrase the fixed evidence list. Raises on any problem."""
    import anthropic

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    mission, mission_digest = db.load_mission()
    system = SYSTEM
    if mission.strip():
        logger.info("weekly: mission sha=%s loaded", (mission_digest or "")[:12])
        system = f"{mission}\n\n---\n\n{SYSTEM}"
    else:
        logger.warning("weekly: mission EMPTY — continuing")

    # Priorities and the gain-practice rules (plan §1.4/§4.6) live in
    # governance — another worker is building that module in parallel, so it
    # may not exist, or may not yet define these names. Importing the NAMES
    # directly means either case raises ImportError, and this degrades to
    # mission-only rather than failing the review. A DB error reading them
    # (e.g. DATABASE_URL unset) is left to propagate to this function's own
    # caller, `_gains_body`, which already falls back to the deterministic
    # template on ANY failure — there is no separate degrade-path needed here.
    try:
        from ..store import governance

        with db.get_conn() as conn:
            priorities = governance.current_priorities(conn)
            baselines = governance.current_baselines(conn)
            rules = governance.gain_rules(conn)
        system += "\n\n---\n\n" + governance.as_prompt_block(priorities, baselines, rules)
    except ImportError:
        logger.info("weekly: governance module unavailable — mission-only system prompt")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60.0)
    # NOTE: no `temperature` — sampling parameters were removed on Sonnet 5
    # and the call returns a 400. Determinism comes from low effort + a
    # strict schema instead (same convention as pings/compose.py).
    response = client.messages.create(
        model=settings.iblu_llm_model,
        max_tokens=800,
        system=system,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": _gains_prompt(gains_data)}],
    )

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    return GainsSummary.model_validate(json.loads(text))


def _gains_lines(gains_data: dict) -> str:
    """Deterministic rendering: one flat, dated line per item.

    Always available, and by construction contains no adjective, no praise
    word and no future tense — it is a plain enumeration of evidence, which
    is exactly what `validate_language` is built to accept.
    """
    labels = {"learned": "Learned", "progressed": "Progressed", "experienced": "Experienced"}
    lines: list[str] = []
    for kind, label in labels.items():
        for item in gains_data.get(kind, []):
            # "progressed" used to carry every thread that went quiet, as a
            # bare `users/<id> (55x) — likely closed`. Those now live under
            # Evidence with names; only what HE tapped as progress stays here.
            if kind == "progressed" and item.get("source") != "tapped":
                continue
            lines.append(f"- {item['date']} · {label} · {item['text']}")
    return "\n".join(lines) if lines else "Nothing computed as a dated gain this window."


def _gains_body(gains_data: dict) -> tuple[str, str]:
    """Return `(text, composer)` for the Gains section — composer is 'llm' or
    'fallback', logged by the caller so a fallback is never silent."""
    try:
        narrative = compose_gains_llm(gains_data)
        return narrative.summary, "llm"
    except (ValidationError, json.JSONDecodeError) as exc:
        logger.warning("weekly: gains llm output rejected (%s) — using deterministic lines", exc)
    except Exception as exc:  # noqa: BLE001 - the API being down is not fatal
        logger.warning("weekly: gains llm unavailable (%s) — using deterministic lines", exc)
    return _gains_lines(gains_data), "fallback"


# --------------------------------------------------------------------------
# Truth and One removal — always deterministic. Numbers are not the LLM's to
# paraphrase, and a single derived fact does not need prose.
# --------------------------------------------------------------------------


# How many rows one dimension shows. Six is the whole venture list; beyond
# that the project split turns into the log again.
SPLIT_ROWS = 6


def _split_table(data: dict, stage_extra: dict | None) -> str:
    """The week as percentages, by dimension, in one monospace block.

    Asked for directly: "simple table of what I worked on over the week, in
    percentages, by couple different dimensions". A Chat text message cannot
    render a markdown table, so this is a ``` block with hand-aligned columns,
    which a phone shows exactly as written.

    Each dimension names its basis, because they differ and must not be read
    as one number: minutes come from the reconstructed day and count time;
    signals count things he did, so a chatty thread weighs more than a long
    quiet build. "(untagged)" is a row, never a renormalisation — 60% of a
    week with no project is a fact about the tagging and he should see it.
    """
    blocks: list[tuple[str, list[tuple[str, int]]]] = []
    mins = data.get("minutes") or {}

    def rows(items: list[dict]) -> list[tuple[str, int]]:
        shown = [(i["key"], int(i["share_pct"])) for i in items if int(i["share_pct"]) > 0]
        head, tail = shown[:SPLIT_ROWS], shown[SPLIT_ROWS:]
        if tail:
            head.append(("other", sum(pct for _, pct in tail)))
        return head

    if mins.get("by_venture"):
        venture = rows(mins["by_venture"])
        # Minutes with no venture are a row, for the same reason "(untagged)"
        # is: without it blt 57% + deadlift 2% is a column that sums to 59%
        # and reads as broken. Unaccounted is unknown, never idle.
        unaccounted = int((mins.get("untracked") or {}).get("share_pct") or 0)
        if unaccounted:
            venture.append(("(unaccounted)", unaccounted))
        blocks.append(("Venture (minutes)", venture))
    elif data.get("by_venture"):
        blocks.append(("Venture (signals)", rows(data["by_venture"])))
    if data.get("by_project"):
        blocks.append(("Project (signals)", rows(data["by_project"])))
    if data.get("by_source"):
        blocks.append(("Channel (signals)", rows(data["by_source"])))
    if mins.get("by_attention"):
        blocks.append(("Attention (minutes)", rows(mins["by_attention"])))
    if stage_extra and stage_extra.get("by_stage"):
        blocks.append(("Stage (signals)", rows(stage_extra["by_stage"])))

    blocks = [(t, items) for t, items in blocks if items]
    if not blocks:
        return ""

    width = min(max(max(len(n) for _, items in blocks for n, _ in items), 10), 22)
    out = ["```"]
    for i, (title, items) in enumerate(blocks):
        if i:
            out.append("")
        out.append(title)
        for name, pct in items:
            label = name if len(name) <= width else name[: width - 1] + "…"
            out.append(f"  {label:<{width}} {pct:>3}%")
    out.append("```")
    return "\n".join(out)


def _truth_body(data: dict, stage_extra: dict | None) -> str:
    cov = data["coverage"]
    lines = [f"{cov['signals']} signals over {cov['days_with_signals']} of {cov['days_in_window']} days."]

    # The split table replaces the one-line "Where:" — venture is its first
    # block, basis named — and absorbs the "Attention by stage" line below.
    table = _split_table(data, stage_extra)
    if table:
        lines.append(table)
    elif data["by_venture"]:
        lines.append(
            "Where: " + " · ".join(f"{v['key']} {v['share_pct']}%" for v in data["by_venture"])
        )
    if data["by_work_type"]:
        lines.append(
            "What kind: " + " · ".join(f"{w['key']} {w['share_pct']}%" for w in data["by_work_type"])
        )
    else:
        lines.append("What kind: not yet — no pings answered in this window.")

    inbound = data["inbound"]
    lines.append(f"Inbound: {inbound['share_pct']}% of signals were in threads Ignas did not start.")

    minutes = data.get("minutes")
    if minutes:
        # Minutes supersede the day-level figure the moment the day has been
        # reconstructed: "no day was empty" is true and useless, where "41% of
        # the reconstructed week is unaccounted for" is the actual finding.
        lines.append(
            f"Minutes: {minutes['total_minutes']} reconstructed · "
            + " · ".join(
                f"{b['key']} {b['share_pct']}%" for b in minutes["by_attention"]
            )
        )
        confirmed = next(
            (b for b in minutes["by_confidence"] if b["key"] == "fact"), None
        )
        lines.append(
            "Confirmed by Ignas: "
            + (f"{confirmed['share_pct']}% of those minutes" if confirmed else "none yet")
            + " — the rest is inferred."
        )
        lines.append(
            f"Untracked: {minutes['untracked']['share_pct']}% of the "
            f"reconstructed week is unaccounted for — unknown, not idle."
        )
        if minutes["displaced"]["minutes"]:
            lines.append(
                f"Displaced: {minutes['displaced']['minutes']} min went to a "
                f"venture the calendar had claimed for another."
            )
    else:
        untracked = data.get("untracked")
        if untracked:
            lines.append(
                f"Untracked: {untracked['share_pct']}% of the window's days "
                f"had no signal at all."
            )

    # `review()` clusters by container, which puts a Chat partner with no
    # contact entry in as `users/<id>` and, for sources with no container, a
    # commit subject in as a "thread". Neither is something anyone can be
    # handed, and "delegate users/106454719402311288628" is the line that made
    # the whole section read as noise.
    import re as _re

    _commit_shaped = _re.compile(r"^[a-z]+(\([^)]*\))?[:!] ")
    recurring = [
        t for t in data["recurring"]
        if not (t["name"] or "").startswith("users/")
        and not _commit_shaped.match(t["name"] or "")
    ]
    if recurring:
        lines.append("Touched 3 or more times (delegation candidates):")
        for t in recurring[:5]:
            who = "he started it" if t["started_by"] == "me" else "someone else started it"
            lines.append(f"- {t['name']} — {t['signals']}x, {who}")

    p = data["pings"]
    if p["sent"]:
        lines.append(
            # No "target is 80%": measuring 0 against 80 is Gap framing in
            # substance even when every word passes the gate. The count is
            # Truth; the target was a grade.
            f"Pings: {p['answered']}/{p['sent']} answered."
        )

    if stage_extra:
        # "Attention by stage" now lives in the split table as its own block.
        stuck = stage_extra.get("stuck")
        if stuck:
            # At most three. A fourteen-project list is noise, and he asked
            # for the three that matter — the oldest, which `stage_truth`
            # already orders first — with the rest as a count.
            shown = stuck[:3]
            from .stage_check import short_name

            names = ", ".join(short_name(x.get("name"), x.get("code", "?")) for x in shown)
            more = len(stuck) - len(shown)
            lines.append(
                f"Unmoved 30+ days in stages 5-7: {names}"
                + (f" — and {more} more." if more > 0 else ".")
            )

    return "\n".join(lines)


def _removal_body(removal: dict) -> str:
    if not removal.get("available"):
        return removal.get("reason", "Insufficient evidence this week to name one.")
    step = removal["step"]
    return step[0].upper() + step[1:] + "."


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------


def _ladder_body(ladder: dict | None, ach: dict | None) -> str:
    """The Gain as the mission defines it: rungs toward autonomous, measured
    back from 13 September. Deterministic — these are counts, and the model
    never gets to paraphrase a count.

    This replaces a list of tasks as the opening of the review. He read three
    task-shaped drafts of the same week and said: "they do not show how I'm
    getting closer to my goals". A rung is the unit the mission names, so a
    rung is what opens the review; the week's work appears beneath it as the
    EVIDENCE for the climb, never as the headline.

    Wording is checked by `review_language`: no "only", no "still ... away",
    no "will" in this section. "one rung from autonomous" is a position, not
    a distance to a target, and it passes.
    """
    if not ladder:
        return "Stage ladder unavailable this week."
    from .stage_check import short_name

    lines = [f"Measured back from {ladder['baseline']}."]

    if ladder["this_week"]:
        lines.append("")
        lines.append("Climbed this week:")
        for m in ladder["this_week"]:
            rungs = m["to_ord"] - m["from_ord"]
            lines.append(
                f"- {short_name(m['name'], m.get('project', ''))} — {m['from_stage']} → {m['to_stage']}"
                + (f" ({rungs} rungs)" if rungs > 1 else "")
            )
    sb = ladder["since_baseline"]
    if sb["moves"]:
        lines.append(
            f"Since {ladder['baseline']}: {sb['rungs']} rung(s) climbed across "
            f"{sb['projects']} project(s), confirmed by Ignas."
        )
    else:
        lines.append(
            f"Since {ladder['baseline']}: no rung confirmed yet. The stage card "
            "that follows this review is where the count starts."
        )
    if ladder["at_top"]:
        lines.append(f"Autonomous: {ladder['at_top']} of {ladder['total_active']} projects.")
    if ladder["one_from_top"]:
        names = ", ".join(short_name(x["name"], x["code"]) for x in ladder["one_from_top"][:6])
        lines.append(f"One rung from autonomous: {names}.")

    if ach:
        ev = []
        if ach.get("shipped"):
            top = ach["shipped"][:3]
            total = sum(x["commits"] for x in ach["shipped"])
            ev.append(f"- Built: {total} commits — " + ", ".join(
                f"{x['repo']} ({x['commits']})" for x in top))
        if ach.get("closed"):
            theirs = sum(1 for c in ach["closed"] if c["started_by"] == "other")
            ev.append(
                f"- Closed: {len(ach['closed'])} threads, {theirs} opened by someone else — "
                + ", ".join(c["name"] for c in ach["closed"][:3]))
        if ach.get("time_back"):
            ev.append("- Quieter than last week: " + ", ".join(
                f"{t['name']} {t['was']}→{t['now']}" for t in ach["time_back"][:3]))
        if ev:
            lines.append("")
            lines.append("Evidence this week:")
            lines.extend(ev)
    return "\n".join(lines)


# NOTE: these two live ABOVE the `__main__` entry point on purpose.
# They were appended to the end of the file on 2026-09-13 (1dd5a80, the Gain
# layer), below `raise SystemExit(main())` — so running this as a module
# called main() before Python had reached the definitions, and every Friday
# review since died on `NameError: name '_gap_lines' is not defined`. Six
# weeks of weekly reviews, and the only thing that told anyone was
# `systemctl is-failed`, which nothing was asking.
def _gap_lines(audit: dict) -> str:
    """His own words about the week, quoted back beside the week's evidence.

    Never a judgement, never advice — a count and at most three short excerpts,
    all of them his. Seeing "we are behind" next to what actually shipped is
    the whole intervention; saying anything about it would be grading.
    """
    phrases = ", ".join(f"{label} x{n}" for label, n in audit["by_phrase"])
    lines = [
        f"How the week was described: {audit['findings']} of "
        f"{audit['messages_scanned']} messages he sent used Gap phrasing"
        + (f" ({phrases})." if phrases else ".")
    ]
    for q in audit["quotes"]:
        lines.append(f'  {q["date"]} · "{q["quote"]}"')
    return "\n".join(lines)


def _monthly_body(statement: dict) -> str:
    """Per venture: the baseline, then what is dated since. No adjectives."""
    lines = [f"Measured back to {statement['since']}."]
    for item in statement["ventures"]:
        venture = item["venture"] or "unassigned"
        lines.append(f"*{venture}* — baseline {item['baseline_set']}:")
        if not item["since_then"]:
            lines.append("  nothing recorded since — which is not nothing happening.")
            continue
        for entry in item["since_then"][:4]:
            lines.append(f"  {entry['date']} · {entry['text']}")
    return "\n".join(lines)


def compose_review(window: str | None = None) -> tuple[str, dict]:
    """Return `(text, data)` — the three-section review and its raw numbers.

    `window` is a "7d"-style override for manual / ad-hoc runs. The default
    (`None`) uses the plan's actual window: Monday 00:00 -> now, local.
    """
    if window:
        data = review_tools.review(window)
        window_label = window
    else:
        since, until = _week_window()
        data = review_tools.review("week", since=since, until=until)
        window_label = "week"

    if data.get("status") == "mock":
        text = "*Weekly review*\n\n_mock mode — nothing recorded_"
        data["gains"] = {"learned": [], "progressed": [], "experienced": []}
        data["removal"] = {"available": False, "reason": "mock mode"}
        return text, data

    gains_data = review_tools.gains(data)
    removal = review_tools.one_removal(data)

    stage_extra = None
    try:
        since_dt = datetime.fromisoformat(data["since"])
        until_dt = datetime.fromisoformat(data["until"])
        stage_extra = review_tools.stage_truth(since_dt, until_dt)
    except Exception:  # the stage/stuck extras are strictly optional
        logger.warning("weekly: could not compute stage_truth", exc_info=True)

    # §4.3 — how he talked about the week, next to what the week produced.
    gap_audit = None
    monthly = None
    try:
        since_dt = datetime.fromisoformat(data["since"])
        until_dt = datetime.fromisoformat(data["until"])
        gap_audit = review_tools.gap_language_for(since_dt, until_dt)
        # §4.5 — on the last Friday of the month the review carries a fourth
        # section rather than getting a timer of its own.
        if review_tools.is_last_friday(until_dt.astimezone(ZoneInfo(settings.iblu_timezone)).date()):
            monthly = review_tools.backward_statement_for(
                until_dt - timedelta(days=30), until_dt
            )
    except Exception:  # neither is worth losing the review over
        logger.warning("weekly: gap audit / monthly statement unavailable", exc_info=True)

    truth_body = _truth_body(data, stage_extra)
    if gap_audit and gap_audit["findings"]:
        truth_body += "\n" + _gap_lines(gap_audit)
    removal_body = _removal_body(removal)
    gains_body, composer = _gains_body(gains_data)

    # The ladder opens Gains. Both halves are optional — a review with no
    # ladder is still a review — but the ladder is the part he asked for.
    ladder = None
    ach = None
    try:
        from . import stage_check

        since_dt = datetime.fromisoformat(data["since"])
        until_dt = datetime.fromisoformat(data["until"])
        with db.get_conn() as conn:
            ladder = stage_check.ladder_gains(conn, since_dt, until_dt)
        ach = review_tools.achievements(data)
    except Exception:  # noqa: BLE001 — never lose the review over its opening
        logger.warning("weekly: ladder / achievements unavailable", exc_info=True)
    data["ladder"] = ladder
    data["achievements"] = ach
    gains_body = _ladder_body(ladder, ach) + "\n\n" + gains_body

    tz = ZoneInfo(settings.iblu_timezone)

    def _section(gains_text: str) -> str:
        try:
            since_local = datetime.fromisoformat(data["since"]).astimezone(tz)
            until_local = datetime.fromisoformat(data["until"]).astimezone(tz)
            header = f"*Weekly review — {since_local:%b %d} to {until_local:%b %d}*\n\n"
        except Exception:
            header = f"*Weekly review — last {window_label}*\n\n"
        return (
            header
            + "## Gains\n" + gains_text
            + "\n\n## Truth\n" + truth_body
            + "\n\n## One removal\n" + removal_body
            + (("\n\n## 30 / 90 days\n" + _monthly_body(monthly)) if monthly else "")
        )

    text = _section(gains_body)

    if composer == "llm":
        violations = validate_language(text)
        if violations:
            logger.warning(
                "weekly: composed review failed language validation (%s) — "
                "falling back to the deterministic gains template",
                violations,
            )
            # The whole point of the validator is that Ignas never sees what it
            # caught. So the only way to know whether it is working — or
            # over-firing — is to write down what it rejected.
            from ..store import observations as obs

            obs.record_safe(
                source="weekly", kind="gap_language_rejected", severity="warn",
                summary=f"the composed review tripped {len(violations)} language rule(s)",
                detail="; ".join(str(v) for v in violations)[:1000],
                evidence={"violations": [str(v) for v in violations][:10]},
                fp=obs.fingerprint("weekly", "gap_language_rejected"),
            )
            gains_body = _gains_lines(gains_data)
            text = _section(gains_body)

    # The deterministic template is built from fixed, tested-clean phrasings
    # and should never fail this check — but "should never" is not "may
    # never send unchecked", so it is verified every time, not assumed. If it
    # somehow still fails (a venture/thread/counterpart name colliding with a
    # rule, say), the hard rule is "never send text that failed" — so this
    # withholds the composed text entirely rather than sending it anyway.
    # Appended BEFORE the gate. It used to be added after, which meant this
    # one fragment was structurally exempt from the check that exists to
    # guarantee "the caller never sends text this rejects". Its current wording
    # happens to pass; a future rewording would not have been caught.
    signals = data.get("coverage", {}).get("signals", 0)
    if signals < MIN_SIGNALS:
        text += (
            f"\n\n_Only {signals} signals this week — too little to draw a "
            "conclusion from. Reported so the gap is visible, not hidden._"
        )

    final_violations = validate_language(text)
    if final_violations:
        logger.error(
            "weekly: deterministic template failed language validation (%s) — "
            "this indicates a bug in the template, not the evidence; "
            "withholding the composed text",
            final_violations,
        )
        text = (
            "*Weekly review*\n\n"
            "Review withheld: the composed text failed the language check "
            f"({len(final_violations)} rule(s)). Nothing below was invented — "
            "the evidence computed this window is intact in the job log; only "
            "its wording tripped the gate."
        )

    data["gains"] = gains_data
    data["removal"] = removal
    return text, data


def send(text: str) -> str:
    """Post the review into the Secretary space. Returns the message name."""
    url = settings.secretary_webhook_url
    separator = "&" if "?" in url else "?"
    url = f"{url}{separator}threadKey=weekly-review"

    try:
        response = requests.post(url, json={"text": text}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        # Same trap, same webhook, so the same scrubber — not a second regex
        # that can drift out of step with it.
        from ..pings.deliver import _scrub

        raise RuntimeError(f"webhook unreachable: {_scrub(exc)}") from exc
    if response.status_code >= 400:
        # Never log the URL — it carries the webhook key and token.
        raise RuntimeError(f"webhook returned {response.status_code}: {response.text[:200]}")
    return (response.json() or {}).get("name", "")


def run(window: str | None = None, dry: bool = False, assume_yes: bool = False) -> int:
    refusal = _guard()
    if refusal:
        logger.error("weekly: %s", refusal)
        return 1

    text, data = compose_review(window)

    if dry:
        print(text)
        logger.info(
            "weekly [dry]: %d signals, %d recurring threads — nothing sent",
            data["coverage"]["signals"], len(data["recurring"]),
        )
        return 0

    name = send(text)

    # The review is a durable conclusion about the week, so it belongs in
    # memory — not in `signals`, which is observation only (plan D1).
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO context_entries
                (type, content, importance, tags, source, source_ref, meta)
            VALUES ('decision', %s, 4, %s, 'analyst', %s, %s)
            """,
            (
                text,
                ["weekly_review", window or "week"],
                name or f"weekly:{data['since'][:10]}",
                Jsonb({
                    "window": window or "week",
                    "coverage": data["coverage"],
                    "by_venture": data["by_venture"],
                    "pings": data["pings"],
                    "gains_counts": {k: len(v) for k, v in data.get("gains", {}).items()},
                    "removal": data.get("removal"),
                }),
            ),
        )

    logger.info("weekly: review sent (%s signals) as %s", data["coverage"]["signals"], name)

    # The question that makes next week's Gains non-empty. Its failure must
    # never read as the review failing — the review is already sent.
    try:
        from . import stage_check

        stage_check.run(dry=False)
    except Exception:  # noqa: BLE001
        logger.warning("weekly: stage card not sent", exc_info=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m iblu_keeper.jobs.weekly",
        description="Post the weekly attention review into the Secretary space.",
    )
    parser.add_argument(
        "--window", default=None,
        help="override the window (e.g. '7d'); default is Monday 00:00 -> now, local",
    )
    parser.add_argument("--dry", action="store_true", help="print it, send nothing")
    parser.add_argument("--yes", action="store_true", help="skip the send confirmation")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if not args.dry and not args.yes and sys.stdin.isatty():
        if input("post the weekly review to Secretary now? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("cancelled")
            return 0

    try:
        return run(window=args.window, dry=args.dry, assume_yes=args.yes)
    finally:
        db.close_pool()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
