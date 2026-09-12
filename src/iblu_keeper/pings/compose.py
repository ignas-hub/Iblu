"""What to ask (plan §7.2–7.3).

Two composers behind one interface. The LLM one reads the window's signals and
names the specific thread that ate the time; the fallback fills the same three
templates deterministically. Any failure — no key, timeout, bad JSON, schema
violation — falls back silently, because Monday must not depend on the API
being up.

Both produce the same validated shape, and that shape is snapshotted onto the
`pings` row, so an answer of "B" stays decodable years later.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..config import settings

logger = logging.getLogger("iblu_keeper.pings.compose")

MAX_QUESTIONS = 3
MAX_OPTIONS = 4
MAX_TEXT = 160
MAX_LABEL = 40

QIDS = ("sink", "displaced", "split")
VERDICTS = (
    "planned_mine", "unplanned_mine", "someone_else", "one_off",
    "did_it", "other", "right", "more", "way_off",
)


class Payload(BaseModel):
    """The structured meaning behind an option — this is what gets analysed."""

    kind: Literal["sink", "displaced", "split"]
    verdict: Literal[VERDICTS]  # type: ignore[valid-type]
    venture: str | None = None
    work_type: str | None = None
    project: str | None = None
    signal_ids: list[int] = Field(default_factory=list)
    container: str | None = None
    event_id: str | None = None


class Option(BaseModel):
    key: str
    label: str
    payload: Payload

    @field_validator("label")
    @classmethod
    def _short(cls, v: str) -> str:
        return v.strip()[:MAX_LABEL]


# Internal schema words that must never reach the person answering. "sink" is
# the qid for the attention question; asked as "biggest sink?" it is meaningless
# to a human at a glance. A prompt rule alone is not enough — a model that
# regresses should fail validation and fall through to the fallback templates.
JARGON = ("sink", "attention sink")


class Question(BaseModel):
    qid: Literal["sink", "displaced", "split"]
    text: str
    options: list[Option] = Field(min_length=2, max_length=MAX_OPTIONS)

    @field_validator("text")
    @classmethod
    def _short(cls, v: str) -> str:
        text = v.strip()[:MAX_TEXT]
        lowered = text.lower()
        for word in JARGON:
            if word in lowered:
                raise ValueError(
                    f"question text leaks the internal key {word!r}: {text!r}"
                )
        return text


class QuestionSet(BaseModel):
    questions: list[Question] = Field(min_length=1, max_length=MAX_QUESTIONS)


# --------------------------------------------------------------------------
# signal summarisation (shared by both composers)
# --------------------------------------------------------------------------


def _clusters(signals: list[dict]) -> list[tuple[str, list[dict]]]:
    """Signals grouped by container (thread / space), busiest first."""
    buckets: dict[str, list[dict]] = {}
    for sig in signals:
        buckets.setdefault(sig.get("container") or "?", []).append(sig)
    return sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)


def _cluster_name(rows: list[dict]) -> str:
    first = rows[0]
    who = first.get("counterpart") or "someone"
    what = first.get("subject") or ""
    if what and what != who:
        return f"{what} with {who}"
    return str(who)


def _venture_split(signals: list[dict]) -> list[tuple[str, int]]:
    counts = Counter((s.get("venture") or "unknown") for s in signals)
    return counts.most_common()


def signal_lines(signals: list[dict], limit: int = 120) -> str:
    """Compact one-line-per-signal rendering for the model (plan §7.3)."""
    lines = []
    for sig in signals[:limit]:
        when = sig["occurred_at"].strftime("%H:%M")
        who = sig.get("counterpart") or "?"
        subject = (sig.get("subject") or "")[:60]
        snippet = (sig.get("snippet") or "")[:120]
        lines.append(
            f"{when} {sig['source']} {who} — {subject} — "
            f"initiator={sig.get('initiator') or '?'} — {snippet}"
        )
    return "\n".join(lines)


def calendar_lines(events: list) -> str:
    out = []
    for e in events:
        kind = "self-block" if e.is_self_block else f"{e.attendee_count} people"
        out.append(
            f"{e.start:%H:%M}-{e.end:%H:%M} {e.summary or 'untitled'} ({kind})"
        )
    return "\n".join(out)


# --------------------------------------------------------------------------
# fallback composer — deterministic, always available
# --------------------------------------------------------------------------


def compose_fallback(
    signals: list[dict],
    events: list,
    covers_from: datetime,
    covers_to: datetime,
) -> QuestionSet:
    """The same three templates, filled from counts rather than judgement."""
    questions: list[dict] = []
    clusters = _clusters(signals)

    # sink — the busiest thread in the window.
    if clusters:
        container, rows = clusters[0]
        name = _cluster_name(rows)
        ids = [r["id"] for r in rows][:50]
        questions.append({
            "qid": "sink",
            "text": (
                f"{name} — {len(rows)} msgs since {rows[0]['occurred_at']:%H:%M}. "
                "Was that yours to do?"
            ),
            "options": [
                {"key": "A", "label": "Planned & mine",
                 "payload": {"kind": "sink", "verdict": "planned_mine",
                             "venture": rows[0].get("venture"), "container": container,
                             "signal_ids": ids}},
                {"key": "B", "label": "Unplanned, still mine",
                 "payload": {"kind": "sink", "verdict": "unplanned_mine",
                             "venture": rows[0].get("venture"), "container": container,
                             "signal_ids": ids}},
                {"key": "C", "label": "Should be someone else's",
                 "payload": {"kind": "sink", "verdict": "someone_else",
                             "venture": rows[0].get("venture"), "container": container,
                             "signal_ids": ids}},
                {"key": "D", "label": "One-off, ignore",
                 "payload": {"kind": "sink", "verdict": "one_off",
                             "container": container, "signal_ids": ids}},
            ],
        })

    # displaced — a self-block in the window with nothing recorded during it.
    for event in events:
        if not event.is_self_block:
            continue
        if not (covers_from <= event.start <= covers_to):
            continue
        during = [s for s in signals if event.start <= s["occurred_at"] <= event.end]
        if during:
            continue
        options = [
            {"key": chr(65 + i), "label": _cluster_name(rows)[:MAX_LABEL],
             "payload": {"kind": "displaced", "verdict": "more",
                         "venture": rows[0].get("venture"), "container": container,
                         "event_id": event.summary}}
            for i, (container, rows) in enumerate(clusters[:2])
        ]
        options.append({"key": chr(65 + len(options)), "label": "Nothing — I did it",
                        "payload": {"kind": "displaced", "verdict": "did_it",
                                    "event_id": event.summary}})
        options.append({"key": chr(65 + len(options)), "label": "Other → reply in thread",
                        "payload": {"kind": "displaced", "verdict": "other",
                                    "event_id": event.summary}})
        questions.append({
            "qid": "displaced",
            "text": f"You had '{event.summary or 'a block'}' planned at {event.start:%H:%M}. What took it?",
            "options": options[:MAX_OPTIONS],
        })
        break

    # split — how the window divided across ventures.
    split = _venture_split(signals)
    if len(split) >= 1:
        total = sum(n for _, n in split) or 1
        top = split[:2]
        summary = " / ".join(f"{v} {round(100 * n / total)}%" for v, n in top)
        options = [{"key": "A", "label": "Right",
                    "payload": {"kind": "split", "verdict": "right",
                                "venture": top[0][0] if top else None}}]
        # "More blt" is a useless option when the window is already 100% blt:
        # offer the runner-up, and when there is no runner-up, offer the honest
        # escape hatch instead of a tautology.
        alternatives = [v for v, _ in split[1:3]]
        for venture in alternatives:
            options.append({"key": chr(65 + len(options)),
                            "label": f"More {venture}"[:MAX_LABEL],
                            "payload": {"kind": "split", "verdict": "more", "venture": venture}})
        if not alternatives:
            options.append({"key": chr(65 + len(options)),
                            "label": "Some was another venture → reply",
                            "payload": {"kind": "split", "verdict": "more"}})
        options.append({"key": chr(65 + len(options)), "label": "Way off → reply",
                        "payload": {"kind": "split", "verdict": "way_off"}})
        questions.append({
            "qid": "split",
            "text": f"Looks like {summary}. Right?",
            "options": options[:MAX_OPTIONS],
        })

    if not questions:
        # A genuinely empty window still deserves one honest question.
        questions.append({
            "qid": "sink",
            "text": f"Nothing recorded between {covers_from:%H:%M} and {covers_to:%H:%M}. What were you doing?",
            "options": [
                {"key": "A", "label": "Deep work, off the tools",
                 "payload": {"kind": "sink", "verdict": "planned_mine"}},
                {"key": "B", "label": "Meetings / calls",
                 "payload": {"kind": "sink", "verdict": "unplanned_mine"}},
                {"key": "C", "label": "Not working",
                 "payload": {"kind": "sink", "verdict": "did_it", "work_type": "life"}},
                {"key": "D", "label": "Other → reply in thread",
                 "payload": {"kind": "sink", "verdict": "other"}},
            ],
        })

    return QuestionSet(questions=questions[:MAX_QUESTIONS])


# --------------------------------------------------------------------------
# LLM composer
# --------------------------------------------------------------------------

SYSTEM = """You write a 3-question tap-quiz that a busy founder answers in \
under ten seconds on his phone, to reconstruct where his attention actually \
went.

Rules:
- Name the SPECIFIC thread, person or event. "Womanizer permissions thread with \
Bella — 7 messages since 09:10" is useful; "your messages" is worthless.
- Write plain English a tired person understands at a glance. The qid values \
("sink", "displaced", "split") are INTERNAL KEYS — never put them, or words \
like "attention sink", in the text he reads. Ask the thing itself:
    good: "Ante Cetinic contract thread — 3 msgs, 09:07-09:22. Took the most of \
your morning. Was that yours to do?"
    bad:  "Ante Cetinic contract thread — 3 msgs. Biggest sink?"
    good: "You blocked 14:00-16:00 for Machina but nothing shows. What took it?"
    good: "Morning looks like BLT 70% / Deadlift 30%. Right?"
- Every question must be answerable by the options you give it. If the options \
are about whether the work was his, the question has to ask that.
- Questions <=160 chars, option labels <=40 chars. No preamble, no pleasantries.
- Ask only what the signals support. Never invent a meeting or a person.
- Output STRICT JSON matching the schema. No markdown, no commentary."""

SCHEMA_HINT = """{"questions":[{"qid":"sink|displaced|split","text":"...", \
"options":[{"key":"A","label":"...","payload":{"kind":"sink|displaced|split", \
"verdict":"planned_mine|unplanned_mine|someone_else|one_off|did_it|other|right|more|way_off", \
"venture":null,"work_type":null,"project":null,"signal_ids":[],"container":null,"event_id":null}}]}]}"""


def compose_llm(
    signals: list[dict],
    events: list,
    covers_from: datetime,
    covers_to: datetime,
    ventures: list[dict],
    work_types: list[dict],
) -> QuestionSet:
    """Ask the model for the question set. Raises on any problem."""
    import anthropic

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    prompt = f"""Window: {covers_from:%a %H:%M} to {covers_to:%H:%M}.

Ventures: {', '.join(f"{v['code']}={v['label']}" for v in ventures)}
Work types: {', '.join(f"{w['code']}={w['label']}" for w in work_types)}

Calendar today:
{calendar_lines(events) or '(nothing)'}

Signals in the window ({len(signals)}):
{signal_lines(signals) or '(none)'}

Write at most 3 questions, each with the qid given in brackets:
- [sink] Name the thread or person that took the most of his attention in this
  window, then ask whether that was his work to do. Options: Planned & mine /
  Unplanned, still mine / Should be someone else's / One-off, ignore.
- [displaced] ONLY if a self-block (no attendees) in the window has no signals
  during it: name the block and ask what took its place. Options: the top two
  clusters / Nothing — I did it / Other → reply.
- [split] State the venture split you infer as a claim, and ask if it is right.
  Options: Right / More <v1> / More <v2> / Way off → reply.

Set payload.signal_ids to the ids you are referring to where you can.
Signal ids, in order: {[s['id'] for s in signals][:80]}

Return only JSON of this shape:
{SCHEMA_HINT}"""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60.0)
    # NOTE: no `temperature` — sampling parameters were removed on Sonnet 5 and
    # return a 400. Determinism comes from low effort + a strict schema instead.
    response = client.messages.create(
        model=settings.iblu_llm_model,
        max_tokens=2000,
        system=SYSTEM,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    return QuestionSet.model_validate(json.loads(text))


def compose(
    signals: list[dict],
    events: list,
    covers_from: datetime,
    covers_to: datetime,
    ventures: list[dict],
    work_types: list[dict],
) -> tuple[QuestionSet, str]:
    """Return `(questions, composer)` where composer is 'llm' or 'fallback'."""
    try:
        questions = compose_llm(
            signals, events, covers_from, covers_to, ventures, work_types
        )
        logger.info("compose: llm produced %d question(s)", len(questions.questions))
        return questions, "llm"
    except (ValidationError, json.JSONDecodeError) as exc:
        logger.warning("compose: llm output rejected (%s) — using fallback", exc)
    except Exception as exc:  # noqa: BLE001 - the API being down is not fatal
        logger.warning("compose: llm unavailable (%s) — using fallback", exc)

    questions = compose_fallback(signals, events, covers_from, covers_to)
    logger.info("compose: fallback produced %d question(s)", len(questions.questions))
    return questions, "fallback"
