"""The LLM's part of the reconstruction: labelling, not arithmetic.

"Scripts fetch; the LLM judges." `blocks.build()` decides *where* the day's
boundaries are — that is arithmetic over timestamps, and arithmetic should be
reproducible. What it cannot do is read "Re: Noshinku 3PL training" and know
that is client work for BLT rather than delivery for Choco. That is the judge's
job: it takes the assembled day and may refine `venture`, `work_type`,
`project` and the one-line reasoning.

Three hard limits, all enforced after the call rather than asked for politely:

  * **It may not move a boundary.** Start and end times come back unchanged or
    the whole response is rejected. A model that can reshape the timeline can
    invent an hour of work.
  * **It may not upgrade confidence.** Only a tap makes a block a `fact`.
  * **It may not invent a code.** Every venture, work type and project it
    returns must already exist.

Any violation, any timeout, any malformed JSON — the deterministic day stands
and `llm=False` is recorded on every block, so a later reader can tell which
days were judged and which were merely computed.
"""

from __future__ import annotations

import json
import logging

from ..config import settings

logger = logging.getLogger("iblu_keeper.analyst.judge")

MAX_REASONING = 120

SYSTEM = """You label a reconstructed day. You do not build it.

You are given blocks already cut from recorded evidence. For each one you may
correct the venture, the work type, the project, and the one-line reasoning
shown to Ignas. You may not change any time, you may not add or remove a block,
and you may not claim more certainty than the evidence carries.

Measure backward from the baselines, never against an ideal, a goal, a
competitor or another person. State gains as dated evidence. Never praise
generically. Never present a plan as a gain. Gains first.

A block marked untracked has no evidence at all. Leave it untracked: silence is
never presence, and guessing what an unobserved hour was is the one thing that
would make this record worthless.

Return only JSON: {"blocks": [{"i": <index>, "venture": <code|null>,
"work_type": <code|null>, "project": <code|null>, "reasoning": "<=120 chars"}]}
Omit any block you would not change."""


def _lines(rows: list[dict], tz) -> str:
    out = []
    for i, b in enumerate(rows):
        start = b["starts_at"].astimezone(tz).strftime("%H:%M")
        end = b["ends_at"].astimezone(tz).strftime("%H:%M")
        mark = " UNTRACKED" if b.get("untracked") else ""
        out.append(
            f"{i}. {start}-{end} venture={b['venture'] or '-'} "
            f"work_type={b['work_type'] or '-'} project={b['project'] or '-'} "
            f"attention={b['attention']}{mark} :: {b['reasoning']}"
        )
    return "\n".join(out)


def _evidence(rows: list[dict], signals: list[dict]) -> str:
    """The subjects behind each block, so the judge reads text and not ids."""
    by_id = {s["id"]: s for s in signals}
    out = []
    for i, b in enumerate(rows):
        subjects = []
        for sid in (b.get("evidence") or [])[:6]:
            s = by_id.get(sid)
            if not s:
                continue
            subjects.append(
                f"{s['source']}:{(s.get('subject') or s.get('counterpart') or '')[:60]}"
            )
        if subjects:
            out.append(f"{i}. " + " | ".join(subjects))
    return "\n".join(out)


def judge(
    rows: list[dict],
    signals: list[dict],
    *,
    ventures: list[str],
    work_types: list[str],
    projects: list[str],
    tz,
) -> tuple[list[dict], bool]:
    """Return `(rows, llm_used)`. Never raises — a bad judge is a silent no-op."""
    if not rows or not settings.anthropic_api_key:
        return rows, False
    try:
        patched = _call(rows, signals, ventures, work_types, projects, tz)
    except Exception as exc:  # noqa: BLE001 — the day stands without the judge
        logger.warning("judge: unavailable or rejected (%s) — keeping the computed day", exc)
        # A rejection means either the model tried something it is not allowed
        # to do, or the guard rails are too tight. Both are worth reading later;
        # neither is visible from the reconstructed day itself.
        from ..store import observations as obs

        obs.record_safe(
            source="judge", kind="llm_output_rejected", severity="warn",
            summary="the analyst's judge was rejected; the day was kept as computed",
            detail=str(exc)[:1000],
            evidence={"blocks": len(rows)},
            fp=obs.fingerprint("judge", "llm_output_rejected", str(exc)[:60]),
        )
        return rows, False
    return patched, True


def _call(rows, signals, ventures, work_types, projects, tz) -> list[dict]:
    import anthropic

    system = SYSTEM
    try:  # governance is optional: a fresh database has no priorities yet
        from .. import db
        from ..store import governance

        mission, _ = db.load_mission()
        with db.get_conn() as conn:
            block = governance.as_prompt_block(
                governance.current_priorities(conn),
                governance.current_baselines(conn),
                governance.gain_rules(conn),
            )
        system = "\n\n---\n\n".join(p for p in (mission.strip(), block, SYSTEM) if p)
    except Exception as exc:  # noqa: BLE001
        logger.info("judge: continuing without mission/priorities (%s)", exc)

    prompt = f"""Ventures: {', '.join(ventures)}
Work types: {', '.join(work_types)}
Known projects: {', '.join(projects) or '(none registered)'}

Blocks:
{_lines(rows, tz)}

Evidence behind them:
{_evidence(rows, signals) or '(none)'}"""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60.0)
    # NOTE: no `temperature` — sampling params return 400 on Sonnet 5.
    response = client.messages.create(
        model=settings.iblu_check_model,
        max_tokens=2000,
        system=system,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    return apply_patch(rows, json.loads(text), ventures, work_types, projects)


def _rejected_language(text: str) -> bool:
    """True when the judge's line breaks the rules every IBLU output follows."""
    try:
        from ..jobs.review_language import validate_language

        violations = validate_language(text)
    except Exception:  # noqa: BLE001 — never let the gate break the judge
        return False
    if violations:
        logger.info("judge: reasoning rejected by the language gate: %s", violations)
    return bool(violations)


def apply_patch(
    rows: list[dict],
    payload: dict,
    ventures: list[str],
    work_types: list[str],
    projects: list[str],
) -> list[dict]:
    """Merge the judge's corrections, dropping anything it was not allowed to say.

    Separated from the API call so the rules can be tested without a network.
    """
    patches = payload.get("blocks")
    if not isinstance(patches, list):
        raise ValueError("judge returned no 'blocks' list")

    out = [dict(r) for r in rows]
    for patch in patches:
        i = patch.get("i")
        if not isinstance(i, int) or not 0 <= i < len(out):
            raise ValueError(f"judge referred to block {i!r}, which does not exist")
        block = out[i]

        # An untracked stretch stays untracked. This is the rule that keeps the
        # record honest, so it is checked before anything else is applied.
        if block.get("untracked"):
            continue

        if "venture" in patch and patch["venture"] is not None:
            if patch["venture"] not in ventures:
                raise ValueError(f"judge invented venture {patch['venture']!r}")
            block["venture"] = patch["venture"]
        if "work_type" in patch and patch["work_type"] is not None:
            if patch["work_type"] not in work_types:
                raise ValueError(f"judge invented work type {patch['work_type']!r}")
            block["work_type"] = patch["work_type"]
        if "project" in patch and patch["project"] is not None:
            # An unregistered project name is kept as text elsewhere in IBLU,
            # but the judge may not be the one to coin it.
            if projects and patch["project"] not in projects:
                raise ValueError(f"judge invented project {patch['project']!r}")
            block["project"] = patch["project"]
        if patch.get("reasoning"):
            # `reasoning` is the one free-text field the judge controls, and it
            # is rendered onto the Secretary calendar. Two reasons it goes
            # through the same gate as everything else IBLU writes back:
            # praise or a comparison here would violate "a tracker that
            # flatters is worse than none", and the prompt interpolates raw
            # email subjects, so an inbound subject line is an untrusted input
            # with a path to this string. A rejected line simply keeps the
            # computed one.
            if _rejected_language(str(patch["reasoning"])):
                continue
            # The duration is arithmetic and stays IBLU's; the judge supplies
            # only the "why". Letting it rewrite the whole line lost the
            # minutes, which is the one number a glance at the card needs.
            minutes = int(
                (block["ends_at"] - block["starts_at"]).total_seconds() // 60
            )
            block["reasoning"] = (
                f"{minutes} min · {str(patch['reasoning'])[:MAX_REASONING]}"
            )

        # Confidence is not the judge's to give. Only a tap makes a fact.
        block["confidence"] = rows[i]["confidence"]
        block["starts_at"] = rows[i]["starts_at"]
        block["ends_at"] = rows[i]["ends_at"]
        block["attention"] = rows[i]["attention"]
    return out
