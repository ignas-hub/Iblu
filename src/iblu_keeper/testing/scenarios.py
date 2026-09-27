"""A golden-day scenario harness — the real pipeline, whole days as data.

The suite grew from 236 to 746 tests and stayed green while Ignas's shadow
calendar was roughly half wrong: those tests assert on dicts before
validation, on `inspect.getsource` text, or pin the bug itself. This harness
tests the two things that are actually the product — **the reconstructed
day** and **the questions** — by calling the real functions
(`analyst.blocks.cluster_signals`/`build`, `pings.compose.compose`) with the
LLM stubbed out, never a re-implementation of what they are supposed to do.

Two kinds of scenario:

  * `Scenario` — a day's signals and calendar intents, in local time, run
    through `cluster_signals`/`build`/`_clip_to_day` (exactly what
    `analyst.blocks.reconstruct` does, minus the database and the judge), then
    checked against `expect`, a list of small named assertions (see
    `ASSERTIONS`).
  * `QuestionScenario` — a window of signals (+ the day's blocks) run through
    the real `pings.compose.compose`, once with the model unavailable
    (deterministic fallback) and once with a stubbed LLM response, both
    checked against `expect` (see `QUESTION_ASSERTIONS`).

No network, no database writes: `DRY_RUN` is pinned before anything that
reads `config.py` is imported (same discipline as `tests/conftest.py`), and
every LLM call is stubbed rather than made.
"""

from __future__ import annotations

import contextlib
import os

# Must happen before `..config` (and anything importing it) is loaded — see
# `tests/conftest.py`'s own docstring for why this has to be the first thing.
os.environ.setdefault("DRY_RUN", "true")

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from ..analyst import blocks as B
from ..pings import compose as C
from ..pings import schedule as S

UTC = timezone.utc


# ---------------------------------------------------------------------------
# time helpers — every scenario speaks in local "HH:MM"; the pipeline speaks
# in UTC datetimes. This is the one place that translates.
# ---------------------------------------------------------------------------


def _to_utc(local_time: str, on: date, tz: ZoneInfo) -> datetime:
    hour, minute = (int(p) for p in local_time.split(":"))
    return datetime.combine(on, time(hour, minute), tzinfo=tz).astimezone(UTC)


def _local_str(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def _describe(blocks: list[dict], tz: ZoneInfo) -> str:
    if not blocks:
        return "(none)"
    return "; ".join(
        f"{_local_str(b['starts_at'], tz)}-{_local_str(b['ends_at'], tz)} "
        f"{b.get('attention')}/{b.get('venture')} evidence={b.get('evidence')}"
        for b in sorted(blocks, key=lambda b: b["starts_at"])
    )


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


@contextlib.contextmanager
def _patched(module, **attrs):
    """Temporarily set attributes on `module`, restored on exit.

    A hand-rolled `monkeypatch` for use both under pytest and from the CLI
    runner (`__main__`), where pytest's fixture is not available.
    """
    originals = {name: getattr(module, name) for name in attrs}
    for name, value in attrs.items():
        setattr(module, name, value)
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(module, name, value)


# ---------------------------------------------------------------------------
# Scenario — a whole day, run through cluster_signals/build/_clip_to_day
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    """A day described as data. See the module docstring and `run`."""

    name: str
    description: str  # one line: what truth this protects
    signals: list[dict] = field(default_factory=list)
    intents: list[dict] = field(default_factory=list)
    expect: list[dict] = field(default_factory=list)
    timezone: str = "Europe/Zagreb"
    on: date = field(default_factory=lambda: date(2026, 9, 16))
    # Pass a workday span to `build()` so unaccounted stretches inside it come
    # back as `untracked` blocks. Off by default — most scenarios only care
    # about evidence- and intent-derived blocks (module docstring of
    # `analyst.blocks.build`).
    workday: bool = False
    # Local "HH:MM", or None for "no restriction" (build()'s own default).
    horizon: str | None = None


def _signal_row(spec: dict, idx: int, tz: ZoneInfo, on: date) -> dict:
    """One scenario signal -> the row shape `cluster_signals`/`compose` read.

    `actor`/`excluded_reason` are not columns `cluster_signals` looks at —
    they are what `analyst.blocks.load_signals` and `pings.runner._window_
    signals` filter ON before either function ever sees a row (HANDOFF §25:
    "anything that claims to describe HIS attention filters actor='me'").
    Carrying them here lets `run`/`run_questions` reproduce that filter
    instead of trusting every scenario author to pre-filter by hand.
    """
    return {
        "id": spec.get("id", idx),
        "source": spec.get("source", "gmail"),
        "occurred_at": _to_utc(spec["time"], on, tz),
        "venture": spec.get("venture"),
        "venture_confidence": spec.get("venture_confidence", "inferred"),
        "work_type": spec.get("work_type"),
        "project": spec.get("project"),
        "subject": spec.get("subject", ""),
        "snippet": spec.get("snippet", ""),
        "counterpart": spec.get("counterpart"),
        "container": spec.get("container"),
        "initiator": spec.get("initiator", "them"),
        "actor": spec.get("actor", "me"),
        "excluded_reason": spec.get("excluded_reason"),
    }


def _intent_interval(spec: dict, idx: int, tz: ZoneInfo, on: date) -> B.Interval:
    """One scenario intent -> the `Interval` `build()` consumes.

    `calendar_kind` stands in for "which calendar did this come from" the way
    `load_intents` would resolve it (HANDOFF §24): `workspace_primary` is
    trusted as-is; `family_calendar` needs a commitment check, and defaults to
    context (`is_context=True`) until `attendance` says "his" — exactly what
    `analyst.intents.classify_missing` would leave behind. A scenario
    pre-sets `attendance` (and, for a tapped answer, `attendance_answer`)
    to stand in for that classification without calling an LLM.
    """
    from ..collectors import venture_hints

    raw_start = _to_utc(spec["start"], on, tz)
    raw_end = _to_utc(spec["end"], on, tz)
    # `_compact_intent` floors/ceils every real calendar event to the same
    # 15-minute grid `cluster_signals` uses, BEFORE `build()` ever sees it —
    # so a cut point from an intent boundary always lands on that grid too.
    # `is_context_locked` is measured from the RAW span, same as
    # `_compact_intent` (a marker's true length, not its rounded one).
    is_context_locked = (raw_end - raw_start) > B.CONTEXT_MAX_DURATION
    start = B._floor(raw_start)
    end = B._ceil(raw_end)
    kind = spec.get("calendar_kind", "workspace_primary")
    needs_commitment_check = kind == "family_calendar"
    attendees = spec.get("attendees") or []
    domains = tuple(sorted({e.split("@", 1)[1].lower() for e in attendees if "@" in e}))

    venture = spec.get("venture")
    if venture is None:
        # Same fallback order as `_compact_intent`: infer from the title/
        # attendees first — never from the account (HANDOFF §16).
        venture, _ = venture_hints.infer(
            "", counterpart=" ".join(attendees), subject=spec.get("title", "")
        )
    if venture is None and kind == "family_calendar":
        venture = "family"

    attendance = spec.get("attendance")
    if needs_commitment_check:
        # Mirrors `analyst.intents.classify_missing`: only a HIGH-confidence
        # "his" ever clears `is_context`; "maybe"/"not_his"/unclassified stay
        # context (never able to create a block or cause `displaced`).
        is_context = is_context_locked or attendance != "his"
    else:
        is_context = is_context_locked

    return B.Interval(
        start=start,
        end=end,
        event_id=spec.get("event_id") or f"scenario-intent-{idx}",
        title=spec.get("title", ""),
        venture=venture,
        calendar_id=spec.get("calendar_id", "family-shared" if needs_commitment_check else "primary"),
        account=spec.get("account", "blt"),
        attendee_count=len(attendees),
        attendee_domains=domains,
        creator_email=spec.get("creator"),
        organizer_email=spec.get("organizer", spec.get("creator")),
        is_self_attendee=spec.get("is_self_attendee", False),
        needs_commitment_check=needs_commitment_check,
        is_context=is_context,
        is_context_locked=is_context_locked,
        attendance=attendance,
        attendance_answer=spec.get("attendance_answer"),
    )


def run(scenario: Scenario) -> list[dict]:
    """Convert `scenario` and run it through the real pipeline.

    `cluster_signals` -> `build` -> `_clip_to_day`, in that order — the same
    assembly `analyst.blocks.reconstruct` does, minus the database, the
    calendar API and the LLM judge (which only relabels; see HANDOFF §18 and
    `judge.py`'s own docstring — it cannot change what block exists where,
    which is the only thing this harness checks).
    """
    tz = ZoneInfo(scenario.timezone)
    rows = [_signal_row(s, i, tz, scenario.on) for i, s in enumerate(scenario.signals)]
    rows.sort(key=lambda r: r["occurred_at"])

    # analyst.blocks.load_signals: "actor='me' AND excluded_reason IS NULL" —
    # inbound demand is not his attention (HANDOFF §25).
    attention_rows = [r for r in rows if r["actor"] == "me" and r["excluded_reason"] is None]

    intents = [_intent_interval(iv, i, tz, scenario.on) for i, iv in enumerate(scenario.intents)]

    workday = B.workday_bounds(scenario.on, tz) if scenario.workday else None
    horizon = _to_utc(scenario.horizon, scenario.on, tz) if scenario.horizon else None

    built = B.build(B.cluster_signals(attention_rows), intents, workday=workday, horizon=horizon)
    clipped = B._clip_to_day(built, B.day_bounds(scenario.on, tz))
    clipped.sort(key=lambda b: (b["starts_at"], b["ends_at"]))
    return clipped


# --- the assertions vocabulary ----------------------------------------------
#
# Each is `(blocks, spec, scenario, tz) -> str | None` — `None` means it
# passed. `spec` is the assertion's dict with `kind` removed. Deliberately a
# flat lookup table, not a DSL: every scenario's `expect` is a list of these,
# so more than one `span`/`total_minutes`/... can appear without colliding.


def _assert_no_overlaps(blocks, spec, scenario, tz) -> str | None:
    ordered = sorted(blocks, key=lambda b: b["starts_at"])
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev["ends_at"] > nxt["starts_at"]:
            return (
                f"blocks overlap: {_local_str(prev['starts_at'], tz)}-"
                f"{_local_str(prev['ends_at'], tz)} and "
                f"{_local_str(nxt['starts_at'], tz)}-{_local_str(nxt['ends_at'], tz)}"
            )
    return None


def _assert_covers(blocks, spec, scenario, tz) -> str | None:
    """Every minute of `[start, end)` is claimed by exactly one block, with no
    gap and nothing double-counted — the "whole intent is accounted for"
    property `test_an_intent_is_split_into_the_part_that_happened_and_the_
    part_that_did_not` pins for a single case."""
    start = _to_utc(spec["start"], scenario.on, tz)
    end = _to_utc(spec["end"], scenario.on, tz)
    inside = sorted(
        (b for b in blocks if _overlaps(b["starts_at"], b["ends_at"], start, end)),
        key=lambda b: b["starts_at"],
    )
    if not inside:
        return f"covers {spec['start']}-{spec['end']}: no blocks in this span at all"
    if inside[0]["starts_at"] != start:
        return (
            f"covers {spec['start']}-{spec['end']}: first block starts at "
            f"{_local_str(inside[0]['starts_at'], tz)}, not {spec['start']}"
        )
    if inside[-1]["ends_at"] != end:
        return (
            f"covers {spec['start']}-{spec['end']}: last block ends at "
            f"{_local_str(inside[-1]['ends_at'], tz)}, not {spec['end']}"
        )
    for prev, nxt in zip(inside, inside[1:]):
        if prev["ends_at"] != nxt["starts_at"]:
            return (
                f"covers {spec['start']}-{spec['end']}: gap or overlap between "
                f"{_local_str(prev['ends_at'], tz)} and {_local_str(nxt['starts_at'], tz)}"
            )
    return None


_SPAN_FIELDS = ("attention", "venture", "work_type", "project", "confidence", "intent_event_id")


def _assert_span(blocks, spec, scenario, tz) -> str | None:
    """Exactly one block spans `[start, end)` and its fields match."""
    start = _to_utc(spec["start"], scenario.on, tz)
    end = _to_utc(spec["end"], scenario.on, tz)
    matches = [b for b in blocks if b["starts_at"] == start and b["ends_at"] == end]
    if len(matches) != 1:
        overlapping = [b for b in blocks if _overlaps(b["starts_at"], b["ends_at"], start, end)]
        return (
            f"span {spec['start']}-{spec['end']}: expected exactly one block with these "
            f"exact bounds, found {len(matches)}. Blocks overlapping this span: "
            f"{_describe(overlapping, tz)}"
        )
    b = matches[0]
    for f in _SPAN_FIELDS:
        if f in spec and b.get(f) != spec[f]:
            return (
                f"span {spec['start']}-{spec['end']}: expected {f}={spec[f]!r}, got "
                f"{f}={b.get(f)!r} (reasoning: {b.get('reasoning')!r})"
            )
    if "evidence" in spec and sorted(b.get("evidence") or []) != sorted(spec["evidence"]):
        return (
            f"span {spec['start']}-{spec['end']}: expected evidence {spec['evidence']}, "
            f"got {b.get('evidence')}"
        )
    if "untracked" in spec and bool(b.get("untracked")) != spec["untracked"]:
        return (
            f"span {spec['start']}-{spec['end']}: expected untracked={spec['untracked']}, "
            f"got {b.get('untracked')}"
        )
    if "reasoning_contains" in spec and spec["reasoning_contains"] not in (b.get("reasoning") or ""):
        return (
            f"span {spec['start']}-{spec['end']}: expected reasoning to contain "
            f"{spec['reasoning_contains']!r}, got {b.get('reasoning')!r}"
        )
    return None


def _assert_no_block_between(blocks, spec, scenario, tz) -> str | None:
    """No block overlapping `[start, end)` at all — or, with extra filter
    keys, none matching those fields (e.g. `venture='family'`: no FAMILY
    block in this span, without claiming nothing happened there at all)."""
    start = _to_utc(spec["start"], scenario.on, tz)
    end = _to_utc(spec["end"], scenario.on, tz)
    filters = {k: v for k, v in spec.items() if k not in ("start", "end")}
    hits = [
        b for b in blocks
        if _overlaps(b["starts_at"], b["ends_at"], start, end)
        and all(b.get(k) == v for k, v in filters.items())
    ]
    if hits:
        which = f" matching {filters}" if filters else ""
        return (
            f"expected no block{which} between {spec['start']}-{spec['end']}, found "
            f"{_describe(hits, tz)}"
        )
    return None


def _assert_total_minutes(blocks, spec, scenario, tz) -> str | None:
    bound_keys = ("equals", "at_most", "at_least", "start", "end")
    filters = {k: v for k, v in spec.items() if k not in bound_keys}
    start = _to_utc(spec["start"], scenario.on, tz) if "start" in spec else None
    end = _to_utc(spec["end"], scenario.on, tz) if "end" in spec else None
    total = sum(
        int((b["ends_at"] - b["starts_at"]).total_seconds() // 60)
        for b in blocks
        if all(b.get(k) == v for k, v in filters.items())
        and (start is None or _overlaps(b["starts_at"], b["ends_at"], start, end))
    )
    if "equals" in spec and total != spec["equals"]:
        return f"total_minutes{filters or ''}: expected {spec['equals']}, got {total}"
    if "at_most" in spec and total > spec["at_most"]:
        return f"total_minutes{filters or ''}: expected at most {spec['at_most']}, got {total}"
    if "at_least" in spec and total < spec["at_least"]:
        return f"total_minutes{filters or ''}: expected at least {spec['at_least']}, got {total}"
    return None


def _assert_every_block_with_evidence_has(blocks, spec, scenario, tz) -> str | None:
    field_name = spec["field"]
    start = _to_utc(spec["start"], scenario.on, tz) if "start" in spec else None
    end = _to_utc(spec["end"], scenario.on, tz) if "end" in spec else None
    for b in blocks:
        if not b.get("evidence"):
            continue
        if start is not None and not _overlaps(b["starts_at"], b["ends_at"], start, end):
            continue
        value = b.get(field_name)
        where = f"{_local_str(b['starts_at'], tz)}-{_local_str(b['ends_at'], tz)}"
        if "one_of" in spec and value not in spec["one_of"]:
            return f"block {where} has evidence but {field_name}={value!r}, expected one of {spec['one_of']}"
        if "equals" in spec and value != spec["equals"]:
            return f"block {where} has evidence but {field_name}={value!r}, expected {spec['equals']!r}"
        if "not_equals" in spec and value == spec["not_equals"]:
            return f"block {where} has evidence but {field_name}={value!r}, expected anything else"
    return None


def _assert_no_block_cites_signal(blocks, spec, scenario, tz) -> str | None:
    sid = spec["signal_id"]
    hits = [b for b in blocks if sid in (b.get("evidence") or [])]
    if hits:
        return f"signal {sid} is cited as evidence by {_describe(hits, tz)}"
    return None


ASSERTIONS = {
    "no_overlaps": _assert_no_overlaps,
    "covers": _assert_covers,
    "span": _assert_span,
    "no_block_between": _assert_no_block_between,
    "total_minutes": _assert_total_minutes,
    "every_block_with_evidence_has": _assert_every_block_with_evidence_has,
    "no_block_cites_signal": _assert_no_block_cites_signal,
}


def _baseline_invariants(blocks: list[dict], scenario: Scenario, tz: ZoneInfo) -> list[str]:
    """Checked for every scenario, whether or not `expect` asks for it —
    these are properties the product must never violate, not opt-in
    behaviour (no_overlaps: HANDOFF's overlapping-calendar-events bug; the
    day boundary: HANDOFF §25's midnight-crossing bug)."""
    failures = []
    overlap = _assert_no_overlaps(blocks, {}, scenario, tz)
    if overlap:
        failures.append(overlap)
    day_start, day_end = B.day_bounds(scenario.on, tz)
    for b in blocks:
        if not (day_start <= b["starts_at"] < day_end and day_start < b["ends_at"] <= day_end):
            failures.append(
                f"block {_local_str(b['starts_at'], tz)}-{_local_str(b['ends_at'], tz)} is not "
                f"inside its own local day {scenario.on} — a block must never cross local "
                "midnight (HANDOFF §25)"
            )
    return failures


def check(scenario: Scenario) -> list[str]:
    """Run `scenario` and evaluate its `expect`. Returns readable failures,
    empty when everything holds."""
    tz = ZoneInfo(scenario.timezone)
    try:
        blocks = run(scenario)
    except Exception as exc:  # noqa: BLE001 - a scenario that raises is a failure, not a crash
        return [f"raised {type(exc).__name__}: {exc}"]

    failures = _baseline_invariants(blocks, scenario, tz)
    for item in scenario.expect:
        kind = item["kind"]
        fn = ASSERTIONS.get(kind)
        if fn is None:
            failures.append(f"unknown assertion kind {kind!r}")
            continue
        spec = {k: v for k, v in item.items() if k != "kind"}
        result = fn(blocks, spec, scenario, tz)
        if result:
            failures.append(f"[{kind}] {result}")
    return failures


# ---------------------------------------------------------------------------
# QuestionScenario — a window of signals (+ blocks), run through compose()
# ---------------------------------------------------------------------------


@dataclass
class QuestionScenario:
    """A ping window described as data. See `run_questions`."""

    name: str
    description: str
    signals: list[dict] = field(default_factory=list)
    blocks: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)  # self-blocks, for the fallback 'displaced' card
    maybe_events: list[dict] = field(default_factory=list)
    answered_instance_keys: set[str] = field(default_factory=set)
    gain_evidence: dict | None = None
    top_venture: str | None = None
    llm_response: dict | None = None  # a raw {"questions": [...]} payload; None = don't test the llm path
    expect: list[dict] = field(default_factory=list)
    kind: str = "midday"  # midday | evening | test
    timezone: str = "Europe/Zagreb"
    on: date = field(default_factory=lambda: date(2026, 9, 16))
    covers_from: str = "07:00"
    covers_to: str = "20:00"


def _block_row(spec: dict, idx: int, tz: ZoneInfo, on: date) -> dict:
    return {
        "id": spec.get("id", idx + 1),
        "starts_at": _to_utc(spec["start"], on, tz),
        "ends_at": _to_utc(spec["end"], on, tz),
        "venture": spec.get("venture"),
        "work_type": spec.get("work_type"),
        "project": spec.get("project"),
        "attention": spec.get("attention", "present"),
        "confidence": spec.get("confidence", "inferred"),
        "intent_title": spec.get("intent_title"),
    }


def _all_other_containers(raw_signals: list[dict]) -> set[str]:
    """Containers whose signals are ALL `actor='other'` — inbound demand with
    nothing of his in it (the PandaDoc thread, HANDOFF §25)."""
    by_container: dict[str, list[dict]] = {}
    for s in raw_signals:
        by_container.setdefault(s.get("container") or "?", []).append(s)
    return {c for c, rows in by_container.items() if all(r["actor"] == "other" for r in rows)}


def run_questions(scenario: QuestionScenario) -> dict:
    """Run `scenario` through the real `pings.compose.compose`.

    Always runs the deterministic-fallback path (`anthropic_api_key` forced
    empty, so `compose_llm` raises before touching the network — see
    `compose.compose_llm`'s first line). Also runs the LLM path, with
    `compose_llm` itself stubbed to return `scenario.llm_response`, when that
    is set. Returns `{"fallback": QuestionSet, "llm": QuestionSet | None,
    "_raw_signals": [...], "_tz": ZoneInfo}`.
    """
    tz = ZoneInfo(scenario.timezone)
    on = scenario.on

    raw_signals = [_signal_row(s, i, tz, on) for i, s in enumerate(scenario.signals)]
    raw_signals.sort(key=lambda s: s["occurred_at"])
    # pings.runner._window_signals: "actor='me' AND excluded_reason IS NULL"
    # (HANDOFF §25) — the shape `compose()` actually receives carries neither
    # column, same as the real SQL SELECT list.
    window_signals = [
        {k: v for k, v in s.items() if k not in ("actor", "excluded_reason")}
        for s in raw_signals
        if s["actor"] == "me" and s["excluded_reason"] is None
    ]

    events = [
        S.Event(
            start=_to_utc(e["start"], on, tz).astimezone(tz),
            end=_to_utc(e["end"], on, tz).astimezone(tz),
            summary=e.get("summary"),
            attendee_count=e.get("attendee_count", 0),
        )
        for e in scenario.events
    ]
    blocks = [_block_row(b, i, tz, on) for i, b in enumerate(scenario.blocks)]
    maybe_events = [
        {
            "instance_key": e.get("instance_key", f"scenario-maybe-{i}"),
            "title": e.get("title"),
            "start": _to_utc(e["start"], on, tz),
            "end": _to_utc(e["end"], on, tz),
        }
        for i, e in enumerate(scenario.maybe_events)
    ]
    covers_from = _to_utc(scenario.covers_from, on, tz)
    covers_to = _to_utc(scenario.covers_to, on, tz)

    ventures = [
        {"code": "blt", "label": "Blank Label"},
        {"code": "deadlift", "label": "Deadlift"},
        {"code": "choco", "label": "Choco"},
        {"code": "family", "label": "Family"},
    ]
    work_types = [
        {"code": "client", "label": "Client comms & management"},
        {"code": "build", "label": "Software / automation"},
        {"code": "sales", "label": "Sales / BD / pitch"},
        {"code": "delivery", "label": "Doing the work"},
    ]

    # Forced empty regardless of the host's real .env — HARD CONSTRAINT: no
    # network. `iblu_timezone` is scenario-local so the local-time assertions
    # do not depend on the host's configured timezone either.
    stub_settings = type("_StubSettings", (), {
        "anthropic_api_key": "", "iblu_timezone": scenario.timezone,
    })()

    def _compose(**extra):
        return C.compose(
            window_signals, events, covers_from, covers_to, ventures, work_types,
            kind=scenario.kind, blocks=blocks, gain_evidence=scenario.gain_evidence,
            top_venture=scenario.top_venture, maybe_events=maybe_events,
            answered_instance_keys=scenario.answered_instance_keys,
            **extra,
        )

    with _patched(C, settings=stub_settings):
        fallback_qs, composer = _compose()
        if composer != "fallback":
            raise AssertionError(
                f"expected the fallback composer with no api key, got {composer!r}"
            )

    llm_qs = None
    if scenario.llm_response is not None:
        # Validated OUTSIDE the patched compose() call: a malformed fixture
        # should fail loudly as a scenario-authoring mistake, not be quietly
        # swallowed by compose()'s own ValidationError -> fallback path.
        stub_result = C.QuestionSet.model_validate(scenario.llm_response)

        def _stub_llm(*_a, **_kw):
            return stub_result

        with _patched(C, settings=stub_settings, compose_llm=_stub_llm):
            llm_qs, composer = _compose()
            if composer != "llm":
                raise AssertionError(f"expected the llm composer, got {composer!r}")

    return {
        "fallback": fallback_qs, "llm": llm_qs,
        "_raw_signals": raw_signals, "_tz": tz,
    }


# --- the question-assertions vocabulary -------------------------------------


def _assert_min_options(qs, spec, scenario, results, variant) -> str | None:
    n = spec["n"]
    for q in qs.questions:
        if len(q.options) < n:
            return f"{variant}: question {q.qid!r} has only {len(q.options)} option(s), need >= {n}"
    return None


def _assert_min_questions(qs, spec, scenario, results, variant) -> str | None:
    n = spec["n"]
    if len(qs.questions) < n:
        return f"{variant}: got only {len(qs.questions)} question(s), need >= {n}"
    return None


def _assert_text_contains(qs, spec, scenario, results, variant) -> str | None:
    sub = spec["substring"]
    if not any(sub in q.text for q in qs.questions):
        return f"{variant}: no question text contains {sub!r} — got {[q.text for q in qs.questions]}"
    return None


def _assert_no_text_contains(qs, spec, scenario, results, variant) -> str | None:
    sub = spec["substring"]
    hits = [q.text for q in qs.questions if sub in q.text]
    if hits:
        return f"{variant}: question text unexpectedly contains {sub!r}: {hits}"
    return None


def _assert_tap_budget_ok(qs, spec, scenario, results, variant) -> str | None:
    budget = C.TAP_BUDGET.get(scenario.kind, C.TAP_BUDGET["midday"])
    if len(qs.questions) > budget["total"]:
        return f"{variant}: {len(qs.questions)} questions exceeds the total budget of {budget['total']}"
    counts = {"attention": 0, "gains": 0, "body_mind": 0}
    for q in qs.questions:
        counts[C._bucket(q.qid)] += 1
    for bucket, n in counts.items():
        if n > budget.get(bucket, 0):
            return f"{variant}: {n} {bucket!r} question(s) exceeds its budget of {budget.get(bucket, 0)}"
    return None


def _assert_no_container_cites_other_only(qs, spec, scenario, results, variant) -> str | None:
    """No option (and no question text) refers to a container whose signals
    were ALL inbound — the PandaDoc bug (HANDOFF §25)."""
    other_only = _all_other_containers(results["_raw_signals"])
    if not other_only:
        return None
    for q in qs.questions:
        for opt in q.options:
            if opt.payload.container in other_only:
                return (
                    f"{variant}: question {q.qid!r} option {opt.key!r} cites container "
                    f"{opt.payload.container!r}, which is inbound-only"
                )
    return None


QUESTION_ASSERTIONS = {
    "min_options": _assert_min_options,
    "min_questions": _assert_min_questions,
    "text_contains": _assert_text_contains,
    "no_text_contains": _assert_no_text_contains,
    "tap_budget_ok": _assert_tap_budget_ok,
    "no_container_cites_other_only": _assert_no_container_cites_other_only,
}


def check_questions(scenario: QuestionScenario) -> list[str]:
    try:
        results = run_questions(scenario)
    except Exception as exc:  # noqa: BLE001 - a scenario that raises is a failure, not a crash
        return [f"raised {type(exc).__name__}: {exc}"]

    failures: list[str] = []
    for variant in ("fallback", "llm"):
        qs = results.get(variant)
        if qs is None:
            continue
        for item in scenario.expect:
            kind = item["kind"]
            fn = QUESTION_ASSERTIONS.get(kind)
            if fn is None:
                failures.append(f"unknown question assertion {kind!r}")
                continue
            spec = {k: v for k, v in item.items() if k != "kind"}
            result = fn(qs, spec, scenario, results, variant)
            if result:
                failures.append(f"[{variant}/{kind}] {result}")
    return failures


# ---------------------------------------------------------------------------
# a runner for humans
# ---------------------------------------------------------------------------


def _all_scenarios():
    from . import scenario_data

    return scenario_data.SCENARIOS, scenario_data.QUESTION_SCENARIOS


def main(argv: list[str] | None = None) -> int:
    import argparse

    day_scenarios, question_scenarios = _all_scenarios()
    by_name = {s.name: ("day", s) for s in day_scenarios}
    by_name.update({s.name: ("question", s) for s in question_scenarios})

    parser = argparse.ArgumentParser(prog="python -m iblu_keeper.testing.scenarios")
    parser.add_argument("--list", action="store_true", help="list every scenario and exit")
    parser.add_argument("--name", help="run only the named scenario")
    args = parser.parse_args(argv)

    if args.list:
        for s in day_scenarios:
            print(f"[day]      {s.name} — {s.description}")
        for s in question_scenarios:
            print(f"[question] {s.name} — {s.description}")
        return 0

    if args.name:
        if args.name not in by_name:
            print(f"unknown scenario {args.name!r}")
            return 2
        names = [args.name]
    else:
        names = list(by_name)

    failed = 0
    for name in names:
        kind, scenario = by_name[name]
        failures = check(scenario) if kind == "day" else check_questions(scenario)
        status = "PASS" if not failures else "FAIL"
        print(f"[{kind}] {status} {scenario.name} — {scenario.description}")
        for f in failures:
            print(f"    - {f}")
        if failures:
            failed += 1

    print(f"\n{len(names) - failed}/{len(names)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
