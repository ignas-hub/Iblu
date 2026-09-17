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
import re
from collections import Counter
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ..config import settings

logger = logging.getLogger("iblu_keeper.pings.compose")

# Ceilings the Pydantic schema itself will not exceed. These are NOT the
# binding tap budget (plan §0) — that is `enforce_tap_budget`, below, which
# depends on the ping *kind*. These are just the shape any single question can
# ever take: the widest card evening ships is body/mind (4 options + escape).
MAX_QUESTIONS = 4
MAX_OPTIONS = 5
MAX_TEXT = 160
MAX_LABEL = 40

QIDS = ("sink", "displaced", "split", "work_type", "gap", "gains", "body_mind")
VERDICTS = (
    "planned_mine", "unplanned_mine", "someone_else", "one_off",
    "did_it", "other", "right", "more", "way_off",
    "classify",   # answer to the work_type question: the tapped option IS the answer
    "meeting", "deep_work", "personal_life",           # gap (§3.3)
    "learned", "progressed", "experienced",            # gains (§4.1)
    "strong_excited", "strong_tired", "weak_excited", "weak_exhausted",  # body_mind (§4.2)
)

# Plan §4.6 — carried verbatim on every LLM call this module makes.
GAIN_SYSTEM_RULE = (
    "Measure backward from the baselines, never against an ideal, a goal, a "
    "competitor or another person. State gains as dated evidence. Never "
    "praise generically. Never present a plan as a gain. Gains first."
)


class Payload(BaseModel):
    """The structured meaning behind an option — this is what gets analysed."""

    kind: Literal["sink", "displaced", "split", "work_type", "gap", "gains", "body_mind"]
    verdict: Literal[VERDICTS]  # type: ignore[valid-type]
    venture: str | None = None
    work_type: str | None = None
    project: str | None = None
    signal_ids: list[int] = Field(default_factory=list)
    container: str | None = None
    event_id: str | None = None

    # split / gap (§3.3): the analyst block a tap confirms or corrects, and
    # the attention verdict the correction should carry. Never written to
    # directly — a tap always writes a NEW blocks row and supersedes this one.
    block_id: int | None = None
    attention: Literal["present", "displaced", "ambiguous"] | None = None

    # gains (§4.1): which of the three kinds this tap is, and the evidence it
    # points back at (context_entries ids, block ids — kept as strings since
    # they come from more than one table).
    gain_kind: Literal["learned", "progressed", "experienced"] | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    # body_mind (§4.2): the 1-5 numbers IBLU stores and nothing else.
    body: int | None = None
    mind: int | None = None

    @field_validator("body", "mind")
    @classmethod
    def _on_the_1_to_5_scale(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 5):
            raise ValueError(f"body/mind is a 1-5 scale, got {v!r}")
        return v


class Option(BaseModel):
    key: str
    label: str
    payload: Payload

    @field_validator("payload")
    @classmethod
    def _classification_is_present(cls, v: "Payload") -> "Payload":
        # The whole point of the work_type question is that the answer is a
        # fact Ignas tapped, not a guess. An option that records nothing is
        # worse than no question at all.
        if v.kind == "work_type" and v.verdict == "classify" and not v.work_type:
            raise ValueError("a work_type option must carry a work_type code")
        return v

    @field_validator("label")
    @classmethod
    def _short(cls, v: str) -> str:
        """Trim to a word, not to a character.

        A hard slice produced buttons like "Learned: YEARLY TOP PRIORITY — Ja",
        which is unreadable on a phone and looks broken rather than shortened.
        """
        v = v.strip()
        if len(v) <= MAX_LABEL:
            return v
        cut = v[: MAX_LABEL - 1]
        space = cut.rfind(" ")
        if space > MAX_LABEL // 2:
            cut = cut[:space]
        return cut.rstrip(" ,.;:—-") + "…"

    @model_validator(mode="after")
    def _gains_option_is_a_gain(self) -> "Option":
        # Plan §4.1: "reject any option that is a plan, a percentage, future
        # tense, or a goal not yet reached." The escape hatch ("Add one →
        # reply") records no claim of its own, so it is exempt — the claim it
        # eventually carries is validated on the reply, not on the button.
        if self.payload.kind == "gains" and self.payload.verdict != "other":
            # A truncated gain cannot be checked. `_short` is a field validator
            # and runs BEFORE this one, so by the time we get here the label is
            # already cut to 40 characters — and the disqualifying word is very
            # often the one past the cut ("Signed three clients, WILL announce
            # the plan next month"). Rather than validate a sentence with its
            # ending removed, refuse it: a gain that does not fit on a phone
            # button was never a good option anyway, and refusing drops the set
            # to the deterministic fallback instead of sending a plan as a gain.
            lowered_label = self.label.lower()
            for word in GAINS_JARGON:
                if lowered_label.startswith(word):
                    raise ValueError(
                        f"gains option leads with the internal kind name "
                        f"{word!r}: {self.label!r}"
                    )
            if self.label.endswith("…"):
                raise ValueError(
                    "gains option was truncated, so it cannot be validated: "
                    f"{self.label!r}"
                )
            ok, reason = validate_gain_option(self.label)
            if not ok:
                raise ValueError(
                    f"gains option reads like a {reason}, not a gain: {self.label!r}"
                )
        return self


# Internal schema words that must never reach the person answering. "sink" is
# the qid for the attention question; asked as "biggest sink?" it is meaningless
# to a human at a glance. A prompt rule alone is not enough — a model that
# regresses should fail validation and fall through to the fallback templates.
JARGON = ("sink", "attention sink")

# The three gain kinds are payload values, not English. "Experienced: time with
# personal" reached Ignas's phone on 2026-09-15 — the kind name prefixed to a
# venture PRIMARY KEY. Both halves were internal; neither meant anything to a
# human reading a button.
GAINS_JARGON = ("learned:", "progressed:", "experienced:")

# An unnamed reference is unauditable: "a gmail thread" cannot be resolved back
# to one of forty threads six weeks later. The composer has the subject lines,
# so a vague reference is laziness, not missing data — reject it and let the
# fallback (which always names the container) answer instead.
# Always vague: the article or quantifier IS the vagueness. "a gmail thread"
# points at nothing no matter what else the sentence names.
VAGUE = (
    "a gmail thread", "an email thread", "a chat thread", "an email exchange",
    "some emails", "some messages", "a few messages", "a few emails",
    "a thread", "the thread", "that thread", "various threads", "other threads",
    "some work", "several messages",
)

# Vague only when nothing beside them is named. A bare noun is how you refer to
# a thread you have just named — "Binance/Defixolt email thread with Jurgita"
# is precise, and rejecting it for containing "email thread" threw away a good
# question for a whole day on 2026-09-14.
VAGUE_UNLESS_NAMED = ("email thread", "chat thread", "gmail thread")


def _names_something(text: str) -> bool:
    """Does this question point at something a human could look up later?

    A proper noun, a quoted string, or an address is enough. Deliberately a
    heuristic and deliberately generous: the cost of being too strict is a good
    question silently replaced by a generic one, which is the failure this
    check just caused. The cost of being too loose is a vague question Ignas
    can answer anyway.
    """
    if '"' in text or "'" in text or "@" in text:
        return True
    words = text.split()
    # Skip the first word: every sentence starts with a capital.
    return any(w[:1].isupper() for w in words[1:] if w[:1].isalpha())


class Question(BaseModel):
    qid: Literal["sink", "displaced", "split", "work_type", "gap", "gains", "body_mind"]
    text: str
    options: list[Option] = Field(min_length=2, max_length=MAX_OPTIONS)
    # gains (§4.1) is not a supersede chain: Ignas can tap more than one of
    # its options over the evening, each an independent gain. Every other
    # kind is a single answer, corrected by re-tapping.
    multi: bool = False

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
        # The rule is "an unnamed reference is unauditable", not "this phrase is
        # banned". Matching the bare phrase threw away a perfectly good
        # question on 2026-09-14: "Binance/Defixolt email thread with Jurgita —
        # you authorized power of attorney at 15:26" was rejected for
        # containing "email thread", although it names the thread twice. The
        # composer fell back to a generic question for the rest of the day.
        for phrase in VAGUE:
            if phrase in lowered:
                raise ValueError(
                    f"question text is vague ({phrase!r}) — name the thread: {text!r}"
                )
        if not _names_something(text):
            for phrase in VAGUE_UNLESS_NAMED:
                if phrase in lowered:
                    raise ValueError(
                        f"question text is vague ({phrase!r}) — name the thread: {text!r}"
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
# tap budget (plan §0, binding) — the single place that enforces it
# --------------------------------------------------------------------------

# midday: a flat cap, any kind. evening (and 'test', which exercises the same
# card shapes for hand-inspection): at most 2 attention questions, 1 gains
# card, 1 body/mind card — 4 total, never more.
TAP_BUDGET: dict[str, dict[str, int]] = {
    "midday": {"total": 2, "attention": 2},
    "evening": {"total": 4, "attention": 2, "gains": 1, "body_mind": 1},
}
TAP_BUDGET["test"] = TAP_BUDGET["evening"]

_ATTENTION_QIDS = {"sink", "displaced", "split", "work_type", "gap"}


def _bucket(qid: str) -> str:
    if qid == "gains":
        return "gains"
    if qid == "body_mind":
        return "body_mind"
    return "attention"


def enforce_tap_budget(kind: str, questions: list["Question"]) -> list["Question"]:
    """Trim to the binding tap budget for this ping kind.

    This is the one place the cap is enforced, so a composer that drifts
    (an LLM that returns 3 attention questions, a fallback that appends both
    gains and a stray sixth card) cannot silently exceed it — every path into
    a `pings` row passes through here. Order is preserved: earlier questions
    in the list win their bucket's slots.
    """
    budget = TAP_BUDGET.get(kind, TAP_BUDGET["midday"])
    counts = {"attention": 0, "gains": 0, "body_mind": 0}
    kept: list[Question] = []
    for q in questions:
        bucket = _bucket(q.qid)
        if counts[bucket] >= budget.get(bucket, 0):
            continue
        if len(kept) >= budget["total"]:
            break
        kept.append(q)
        counts[bucket] += 1
    return kept


# --------------------------------------------------------------------------
# split & gap — confirming and correcting the reconstructed day (plan §3.3)
# --------------------------------------------------------------------------
#
# Both read `blocks` (the analyst's reconstruction, `analyst.blocks.live_blocks`)
# rather than clustering signals themselves: the day has already been judged
# once, and these two cards exist to confirm or correct that judgement, not to
# re-derive it. A tap writes a NEW `blocks` row — never an UPDATE of the one it
# corrects — via `pings.answers._write_block_correction`.


def _day_ventures(blocks: list[dict]) -> list[str]:
    """Tracked ventures seen in the day's blocks, busiest first."""
    counts = Counter(b["venture"] for b in blocks if b.get("venture"))
    return [v for v, _ in counts.most_common()]


def _default_top_venture(blocks: list[dict], signals: list[dict]) -> str | None:
    """The venture most of the day (or, failing that, the window) belongs to."""
    day_ventures = _day_ventures(blocks)
    if day_ventures:
        return day_ventures[0]
    split = [v for v, _ in _venture_split(signals) if v != "unknown"]
    return split[0] if split else None


def _local(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(settings.iblu_timezone)).strftime("%H:%M")


def compose_split_question(blocks: list[dict]) -> dict | None:
    """Confirm one still-inferred block: `"14:00–16:00 looks like Deadlift · Machina — right?"`.

    Only `inferred` blocks are worth asking about — a `fact` block is already
    confirmed. The busiest one is picked so a single tap corrects the most
    consequential guess of the day.
    """
    candidates = [
        b for b in blocks
        if b.get("venture") and b.get("confidence") == "inferred"
    ]
    if not candidates:
        return None
    block = max(candidates, key=lambda b: b["ends_at"] - b["starts_at"])

    name = block["venture"]
    if block.get("project"):
        name = f"{name} · {block['project']}"
    span = f"{_local(block['starts_at'])}–{_local(block['ends_at'])}"

    options = [{
        "key": "A", "label": "Right",
        "payload": {
            "kind": "split", "verdict": "right", "block_id": block["id"],
            "venture": block["venture"], "work_type": block.get("work_type"),
            "project": block.get("project"), "attention": block.get("attention") or "present",
        },
    }]
    alternates = [v for v in _day_ventures(blocks) if v != block["venture"]][:2]
    for venture in alternates:
        options.append({
            "key": chr(65 + len(options)), "label": f"Actually {venture}"[:MAX_LABEL],
            "payload": {
                "kind": "split", "verdict": "more", "block_id": block["id"],
                "venture": venture, "attention": block.get("attention") or "present",
            },
        })
    options.append({
        "key": chr(65 + len(options)), "label": "Way off → reply",
        "payload": {"kind": "split", "verdict": "way_off", "block_id": block["id"]},
    })
    return {
        "qid": "split",
        "text": f"{span} looks like {name} — right?",
        "options": options[:MAX_OPTIONS],
    }


def compose_gap_question(blocks: list[dict], top_venture: str | None) -> dict | None:
    """Ask about one untracked stretch: `"11:00–12:30 shows nothing. What was it?"`.

    An untracked block is `venture IS NULL` with no `intent_title` either —
    the analyst has neither evidence nor a calendar event for that stretch
    (`analyst.blocks._untracked`). The first one in the day is asked about;
    the rest wait for another ping.
    """
    untracked = [
        b for b in blocks
        if b.get("venture") is None and not b.get("intent_title")
    ]
    if not untracked:
        return None
    block = untracked[0]
    span = f"{_local(block['starts_at'])}–{_local(block['ends_at'])}"
    deep_work_label = f"Deep work — {top_venture}" if top_venture else "Deep work"

    options = [
        {"key": "A", "label": "Meeting / call not in my mail",
         "payload": {"kind": "gap", "verdict": "meeting", "block_id": block["id"],
                     "work_type": "client", "attention": "present"}},
        {"key": "B", "label": deep_work_label[:MAX_LABEL],
         "payload": {"kind": "gap", "verdict": "deep_work", "block_id": block["id"],
                     "venture": top_venture, "work_type": "build", "attention": "present"}},
        {"key": "C", "label": "Personal / life",
         "payload": {"kind": "gap", "verdict": "personal_life", "block_id": block["id"],
                     "venture": "family", "work_type": "life", "attention": "present"}},
        {"key": "D", "label": "Other → reply",
         "payload": {"kind": "gap", "verdict": "other", "block_id": block["id"]}},
    ]
    return {"qid": "gap", "text": f"{span} shows nothing. What was it?", "options": options}


# --------------------------------------------------------------------------
# gains — "what moved today?" (plan §4.1)
# --------------------------------------------------------------------------
#
# Iblu measures backward; it never grades. This card only ever shows things
# that have already happened — the validator below is what keeps it that way
# even if a future composer tries to phrase one as a plan.

GAIN_REJECT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("a future tense ('will')", re.compile(r"\bwill\b", re.I)),
    ("a future tense ('going to')", re.compile(r"\bgoing to\b", re.I)),
    ("a future tense ('gonna')", re.compile(r"\bgonna\b", re.I)),
    ("a future tense ('shall')", re.compile(r"\bshall\b", re.I)),
    ("a future tense (contraction)", re.compile(r"\b(?:i|we|you|he|she|they)['’]ll\b", re.I)),
    ("a future time reference", re.compile(r"\b(tomorrow|next week|next month|next quarter|next year|soon)\b", re.I)),
    ("a plan", re.compile(r"\bplan(?:s|ned|ning)?\b", re.I)),
    ("a plan (to-do)", re.compile(r"\bto-?do\b", re.I)),
    ("a percentage", re.compile(r"\d+\s*%|\bpercent\b", re.I)),
    ("a goal not yet reached", re.compile(
        r"\b(in progress|still working|working on|trying to|aiming to|hoping to|hope to)\b", re.I)),
    ("a goal not yet reached (goal/target)", re.compile(r"\b(goal|target)\b", re.I)),
]


def validate_gain_option(text: str) -> tuple[bool, str | None]:
    """True when `text` describes something that has already happened.

    Rejects a plan, a percentage, future tense, or a goal not yet reached
    (plan §4.1) — the historical gains register shows exactly this drift, so
    this is checked in its own function, independent of the LLM prompt rule
    that asks for the same thing.
    """
    for reason, pattern in GAIN_REJECT_PATTERNS:
        if pattern.search(text):
            return False, reason
    return True, None


def compose_gains_question(evidence: dict[str, list[dict]] | None) -> dict | None:
    """One option per kind of evidence that exists, plus the escape hatch.

    `evidence` is `{"learned": [...], "progressed": [...], "experienced": [...]}`,
    each item `{"label": str, "evidence_ids": list[str]}` — already-happened,
    already-dated things, gathered by the caller (see `pings.runner`).

    A day IBLU could see nothing in still gets the card, with the reply escape
    as its only option. Iblu never invents a gain — but "what moved today?"
    with no options of its own is a real question, and dropping it on quiet
    days would mean the practice fires least on exactly the days it is for.
    What IBLU can observe is mail, chat and calendar; most of what actually
    moves a day is none of those.
    """
    evidence = evidence or {}
    options: list[dict] = []
    for gain_kind in ("learned", "progressed", "experienced"):
        items = evidence.get(gain_kind) or []
        if not items:
            continue
        item = items[0]
        # Validate the FULL text, then truncate. The other way round, the
        # 40-character label cut the disqualifying word off before the
        # validator ever saw it: "Learned: Signed three clients, will announce
        # the plan next month" became "Learned: Signed three clients, will a…"
        # in one case and, for a longer prefix, lost "will" entirely — so a
        # plan passed the gate and reached his phone as a gain.
        # Validate what he actually did, not the 38 characters that fit on the
        # button. `source_text` is the full sentence; the label is a deliberate
        # shortening of it made by the caller.
        ok, why = validate_gain_option(item.get("source_text") or item["label"])
        if not ok:
            logger.info("compose: gains option rejected (%s)", why)
            continue
        options.append({
            "key": chr(65 + len(options)), "label": item["label"][:MAX_LABEL],
            "payload": {
                "kind": "gains", "verdict": gain_kind, "gain_kind": gain_kind,
                "evidence_ids": [str(i) for i in item.get("evidence_ids", [])],
            },
        })
    options.append({
        "key": chr(65 + len(options)), "label": "Add one → reply",
        "payload": {"kind": "gains", "verdict": "other"},
    })
    return {
        "qid": "gains",
        # Says what a tap MEANS and that more than one is allowed. The header
        # used to be three words, and with a button reading "Experienced: time
        # with personal" beside it, Ignas could not tell what was being asked.
        "text": "What actually moved today? Tap any that happened — more than one is fine.",
        "options": options[:MAX_OPTIONS], "multi": True,
    }


# --------------------------------------------------------------------------
# body / mind — numbers only, never an interpretation (plan §4.2)
# --------------------------------------------------------------------------

# label -> (body, mind) on the 1-5 scale. Deliberately just two points per
# axis (2 and 4): a tired thumb can pick one of four buttons; anything finer
# belongs in the reply override, not the card.
#
# The labels name BOTH axes on every button. "strong · excited" left the reader
# to work out which word was the body and which was the mind, on a phone, at
# the end of a day.
BODY_MIND_OPTIONS: tuple[tuple[str, str, int, int], ...] = (
    ("strong_excited", "Body strong · mind sharp", 4, 4),
    ("strong_tired", "Body strong · mind tired", 4, 2),
    ("weak_excited", "Body tired · mind sharp", 2, 4),
    ("weak_exhausted", "Body tired · mind drained", 2, 2),
)

# "body 1 mind 3" (either order, optional punctuation) overrides with exact
# values — this is the only free-text path into body/mind, so it is parsed
# once, here, rather than re-derived by whoever reads the reply.
BODY_MIND_REPLY_RE = re.compile(
    r"body\D{0,4}([1-5]).{0,20}?mind\D{0,4}([1-5])"
    r"|mind\D{0,4}([1-5]).{0,20}?body\D{0,4}([1-5])",
    re.I | re.S,
)


def parse_body_mind_reply(text: str) -> tuple[int, int] | None:
    """`"body 1 mind 3"` -> `(1, 3)`. `None` when the text doesn't match."""
    m = BODY_MIND_REPLY_RE.search(text or "")
    if not m:
        return None
    if m.group(1) is not None:
        return int(m.group(1)), int(m.group(2))
    return int(m.group(4)), int(m.group(3))


def compose_body_mind_question() -> dict:
    options = [
        {"key": chr(65 + i), "label": label,
         "payload": {"kind": "body_mind", "verdict": verdict, "body": body, "mind": mind}}
        for i, (verdict, label, body, mind) in enumerate(BODY_MIND_OPTIONS)
    ]
    options.append({
        # The exact numbers are only reachable through this reply, and nothing
        # on the card said so. Ignas tapped it and answered in sentences, which
        # is the obvious thing to do — his words are kept either way, but if he
        # wants the scale he now knows how to give it.
        "key": chr(65 + len(options)), "label": "Other → reply 'body 4 mind 2'",
        "payload": {"kind": "body_mind", "verdict": "other"},
    })
    return {
        "qid": "body_mind",
        "text": "How were your body and mind today? (numbers only — Iblu records them, it does not read them)",
        "options": options,
    }


# --------------------------------------------------------------------------
# fallback composer — deterministic, always available
# --------------------------------------------------------------------------


def compose_fallback(
    signals: list[dict],
    events: list,
    covers_from: datetime,
    covers_to: datetime,
    *,
    kind: str = "midday",
    blocks: list[dict] | None = None,
    top_venture: str | None = None,
) -> QuestionSet:
    """The same templates, filled from counts and blocks rather than judgement."""
    blocks = blocks or []
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

    # work_type — what KIND of work the busiest thread was. Asked outright,
    # because nothing else in the system can recover it: venture is inferable
    # from the counterpart's domain, work_type never is.
    if clusters:
        container, rows = clusters[0]
        name = _cluster_name(rows)
        ids = [r["id"] for r in rows][:50]
        questions.append({
            "qid": "work_type",
            "text": f"{name} — what kind of work was that?",
            "options": [
                {"key": key, "label": label,
                 "payload": {"kind": "work_type", "verdict": "classify",
                             "work_type": code, "venture": rows[0].get("venture"),
                             "container": container, "signal_ids": ids}}
                for key, code, label in (
                    ("A", "sales", "Sales / BD / pitch"),
                    ("B", "client", "Client comms & management"),
                    ("C", "delivery", "Doing the work"),
                    ("D", "build", "Software / automation"),
                )
            ],
        })

    # split & gap — block-based confirmation/correction (plan §3.3). Both read
    # the analyst's reconstruction rather than re-deriving a split from raw
    # signals; empty `blocks` (e.g. the analyst has not run yet, or a caller
    # that only wants the old signal-based questions) means neither fires.
    split_q = compose_split_question(blocks)
    if split_q:
        questions.append(split_q)

    effective_top_venture = top_venture or _default_top_venture(blocks, signals)
    gap_q = compose_gap_question(blocks, effective_top_venture)
    if gap_q:
        questions.append(gap_q)

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

    # enforce_tap_budget bounds this well inside MAX_QUESTIONS, so there is no
    # need to pre-slice — doing so before budgeting could drop a `gap` or
    # `split` in favour of an earlier `sink`/`displaced` the budget would not
    # have kept anyway.
    parsed = [Question.model_validate(q) for q in questions]
    return QuestionSet(questions=enforce_tap_budget(kind, parsed))


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
("sink", "displaced", "work_type") are INTERNAL KEYS — never put them, or words \
like "attention sink", in the text he reads. Ask the thing itself:
    good: "Ante Cetinic contract thread — 3 msgs, 09:07-09:22. Took the most of \
your morning. Was that yours to do?"
    bad:  "Ante Cetinic contract thread — 3 msgs. Biggest sink?"
    good: "You blocked 14:00-16:00 for Machina but nothing shows. What took it?"
- Every question must be answerable by the options you give it. If the options \
are about whether the work was his, the question has to ask that.
- NAME things. Never write "a gmail thread", "some messages" or "a few emails" \
— use the actual subject line or person, e.g. "the Temu contract thread with \
Giedre". An unnamed reference cannot be resolved six weeks later, so a vague \
question is worse than no question.
- Questions <=160 chars, option labels <=40 chars. No preamble, no pleasantries.
- Ask only what the signals support. Never invent a meeting or a person.
- """ + GAIN_SYSTEM_RULE + """
- Output STRICT JSON matching the schema. No markdown, no commentary."""

SCHEMA_HINT = """{"questions":[{"qid":"sink|displaced|work_type","text":"...", \
"options":[{"key":"A","label":"...","payload":{"kind":"sink|displaced|work_type", \
"verdict":"planned_mine|unplanned_mine|someone_else|one_off|did_it|other|classify", \
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
- [work_type] Name the thread or project that dominated the window and ask what
  KIND of work it was. Offer the 4 most plausible work_type codes from the list
  above as options; put the CODE in payload.work_type and a human label in the
  option label ("Sales / BD / pitch", not "sales"). verdict is "classify".
  This one matters most: venture can be inferred from an email domain later,
  work_type can never be recovered if it is not captured now.

Prefer [sink] + [work_type] about the SAME named thread, then [displaced] if
the window supports one. Always set payload.venture and payload.project where
the signals make them clear. Do not confirm or correct a reconstructed block
or a venture split — that is done separately, from the analyst's own record
of the day, not from your reading of these signals.

Set payload.signal_ids to the ids you are referring to where you can.
Signal ids, in order: {[s['id'] for s in signals][:80]}

Ask at most 3. Return only JSON of this shape:
{SCHEMA_HINT}"""

    # M3: the mission comes from the database — the same copy the tools serve —
    # so the questions are judged against what IBLU is for, not just the
    # signals. An empty mission warns once and continues: Monday must never
    # depend on this having been seeded.
    from .. import db

    mission, mission_digest = db.load_mission()
    if mission.strip():
        logger.info("composer: mission sha=%s loaded", (mission_digest or "")[:12])
    else:
        logger.warning("composer: mission EMPTY — continuing")

    # Plan §1.4/§4.6: priorities, baselines and gain rules, read through the
    # governance store so this composer can never disagree with get_context
    # or the weekly review about which priority is live. `store.governance`
    # may not exist yet (another session builds it) or the DB may not be
    # configured (tests, a laptop) — either way this degrades to mission-only.
    governance_block = _governance_block()

    parts = [p for p in (mission if mission.strip() else None, governance_block or None) if p]
    system = "\n\n---\n\n".join(parts + [SYSTEM]) if parts else SYSTEM

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60.0)
    # NOTE: no `temperature` — sampling parameters were removed on Sonnet 5 and
    # return a 400. Determinism comes from low effort + a strict schema instead.
    response = client.messages.create(
        model=settings.iblu_llm_model,
        max_tokens=2000,
        system=system,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    parsed = QuestionSet.model_validate(json.loads(text))

    # split/gap/gains/body_mind are never the LLM's to write (see the prompt
    # above) — a model that drifts into one anyway is dropped here rather
    # than trusted, the same way JARGON/VAGUE text is rejected by the schema.
    allowed = {"sink", "displaced", "work_type"}
    questions = [q for q in parsed.questions if q.qid in allowed]
    if not questions:
        raise ValueError("llm produced no sink/displaced/work_type question")
    return QuestionSet(questions=questions)


def _governance_block() -> str:
    """Priorities + baselines + gain rules, or "" if unavailable.

    `iblu_keeper.store.governance` is being built by another worker in
    parallel (plan §1.4) — imported lazily so this composer keeps working
    from disk states where it does not exist yet. A missing/unconfigured
    database degrades the same way: mission-only, never a hard failure.
    """
    try:
        from ..store import governance
    except ImportError:
        return ""

    from .. import db

    # Checked against THIS module's `settings` (substitutable per-test — see
    # `test_composer_continues_without_a_mission`), not `db.is_configured()`:
    # the latter reads the process-wide settings singleton directly and would
    # ignore a test's stand-in, quietly hitting a real database from a unit
    # test that never asked for one.
    if not getattr(settings, "database_url", ""):
        return ""
    try:
        with db.get_conn() as conn:
            priorities = governance.current_priorities(conn)
            baselines = governance.current_baselines(conn)
            rules = governance.gain_rules(conn)
        return governance.as_prompt_block(priorities, baselines, rules)
    except Exception as exc:  # noqa: BLE001 - a governance outage is not fatal
        logger.warning("compose: governance block unavailable (%s)", exc)
        return ""


def compose(
    signals: list[dict],
    events: list,
    covers_from: datetime,
    covers_to: datetime,
    ventures: list[dict],
    work_types: list[dict],
    *,
    kind: str = "midday",
    blocks: list[dict] | None = None,
    gain_evidence: dict[str, list[dict]] | None = None,
    top_venture: str | None = None,
) -> tuple[QuestionSet, str]:
    """Return `(questions, composer)` where composer is 'llm' or 'fallback'.

    `kind` decides the binding tap budget (plan §0): midday gets attention
    questions only; evening (and 'test') additionally gets the deterministic
    gains and body/mind cards. `blocks` is the day's current reconstruction
    (`analyst.blocks.live_blocks`) for the split/gap questions; `gain_evidence`
    is what `pings.runner` gathered for the gains card. Both default to
    "nothing available" so a caller that only wants the old signal-based
    questions (or a unit test) does not need to supply them.
    """
    blocks = blocks or []

    try:
        attention = compose_llm(
            signals, events, covers_from, covers_to, ventures, work_types
        )
        composer = "llm"
        logger.info("compose: llm produced %d question(s)", len(attention.questions))
    except (ValidationError, json.JSONDecodeError) as exc:
        logger.warning("compose: llm output rejected (%s) — using fallback", exc)
        # The fallback questions are fine, so this is invisible to Ignas — which
        # is why it needs recording. A composer that silently never works would
        # look exactly like one that always works.
        from ..store import observations as obs

        obs.record_safe(
            source="composer", kind="llm_output_rejected", severity="warn",
            summary="the ping composer's output was rejected; deterministic questions were sent",
            detail=str(exc)[:1000],
            fp=obs.fingerprint("composer", "llm_output_rejected"),
        )
        attention = None
    except Exception as exc:  # noqa: BLE001 - the API being down is not fatal
        logger.warning("compose: llm unavailable (%s) — using fallback", exc)
        from ..store import observations as obs

        obs.record_llm_failure("composer", exc, context="ping composition")
        attention = None

    if attention is None:
        composer = "fallback"
        attention = compose_fallback(
            signals, events, covers_from, covers_to,
            kind=kind, blocks=blocks, top_venture=top_venture,
        )
        logger.info("compose: fallback produced %d question(s)", len(attention.questions))

    # split & gap are always deterministic — confirming a structured record
    # (a block) is not a judgement call the way naming a busy thread is.
    # compose_fallback already adds them when it runs; the LLM never does
    # (see its prompt), so they are added here only for the LLM path.
    questions: list[Question] = list(attention.questions)
    effective_top_venture = top_venture or _default_top_venture(blocks, signals)
    if composer == "llm":
        split_q = compose_split_question(blocks)
        if split_q:
            questions.append(Question.model_validate(split_q))
        gap_q = compose_gap_question(blocks, effective_top_venture)
        if gap_q:
            questions.append(Question.model_validate(gap_q))

    # gains and body/mind are evening-only cards (plan §4.1/§4.2) — never
    # part of the midday budget, never LLM-composed (see compose_llm's
    # prompt): they are either grounded in today's evidence or they are the
    # fixed body/mind options, and neither benefits from an LLM's phrasing.
    if kind in ("evening", "test"):
        gains_q = compose_gains_question(gain_evidence)
        if gains_q:
            questions.append(Question.model_validate(gains_q))
        questions.append(Question.model_validate(compose_body_mind_question()))

    return QuestionSet(questions=enforce_tap_budget(kind, questions)), composer
