"""Reconstructing the day from signals.

The algorithm is deliberately deterministic — no LLM. Given the same signals
and the same calendar it produces the same day, which is the only way a
reconstruction can be argued with. "Scripts fetch, the LLM judges": this module
fetches and assembles; judging what the day *means* is the reading Claude does
on top of it.

    signals ──gap-split──> evidence clusters ─┐
                                              ├─ cut at intent boundaries ─> blocks
    primary calendar ──> intent intervals ────┘

Anything left of an intent interval that no evidence covers becomes an
`ambiguous` block. Anything left of the day that neither touches is not a
block: an unobserved hour is unobserved, not idle.

**The one place silence becomes presence.** A calendar title is normally not
evidence (HANDOFF §23) — but a FAMILY commitment is different from a work
meeting in one specific way, in Ignas's own words: "What is written in the
family calendar are not facts that those events happened — they might be
just blockers. If over that time there was nothing else happening, or very
little, then assume the family event happened." A work meeting with no
evidence stays `ambiguous` exactly as before; a family commitment (venture
`family`, and — for a calendar that is not exclusively his own — confirmed by
the classifier as something he actually attends, not merely a whereabouts
note about him or someone else) gets its silent remainder turned into
`present`/`family`/`inferred`, never `fact`, and the reasoning always says
"assumed" so nobody mistakes an inference for a recording. Four guard-rails
gate it, each named below with the reasoning behind it
(`_apply_family_inference`): the span must not be mostly work already
(`FAMILY_INFERENCE_MAX_WORK_SHARE`), the remainder must be long enough to be
more than a gap between two chats (`FAMILY_INFERENCE_MIN_MINUTES`), the
recorder must be demonstrably running that day (a signal somewhere outside
the span), and the inference must not reach past the newest data IBLU has
actually collected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..collectors import venture_hints
from ..config import settings

logger = logging.getLogger("iblu_keeper.analyst.blocks")

# Everything is floored/ceiled to this, per the plan: a 40-second reply and the
# thinking around it are not distinguishable, and pretending otherwise would
# give the day a precision the evidence cannot support.
FLOOR = timedelta(minutes=15)

# Two signals closer than this belong to the same stretch of work. Wider than a
# typical reply-to-reply gap, narrower than a coffee break.
GAP = timedelta(minutes=25)

# A cluster's last signal is not the moment the work stopped — sending the mail
# is the end of the writing, and something usually follows it.
TAIL = timedelta(minutes=10)

# An ambiguous remainder shorter than this is scheduling noise, not a gap in
# the record.
MIN_AMBIGUOUS = timedelta(minutes=15)

# The stretch of the local day that IBLU claims to have an opinion about.
# Outside it, absence of evidence is not evidence of anything — he is asleep,
# or living, and a grey block there would be noise pretending to be a finding.
WORKDAY_START = time(7, 0)
WORKDAY_END = time(20, 0)

# An unobserved stretch inside the workday shorter than this is the gap between
# two tasks, not a gap in the record. Half an hour is the smallest span it is
# worth asking him about.
MIN_UNTRACKED = timedelta(minutes=30)

# A project or work_type needs a REAL majority of the evidence that had an
# opinion, not a plurality: a 135-minute block ("invoices, ITIN/company setup,
# a client thread") once became project 'email-writer' because that was the
# single most common Chat space among many unrelated ones — the winner of a
# three-way split, not agreement. 60% of the opinions, and those opinions must
# be at least half the slice, or the label is honestly None.
LABEL_AGREEMENT = 0.60
LABEL_COVERAGE = 0.50

# Venture is far less noisy than project/work_type (fewer, coarser categories,
# usually one obvious owner), so it keeps a plain majority — but it still must
# be a REAL majority: a venture with exactly half the votes is a tie, not a
# winner.
VENTURE_AGREEMENT = 0.50

# An event longer than this is a marker, not a claim on a specific stretch of
# attention — "Ignas LT Fri 12h - Sun 17h" is a three-day travel marker; taken
# as an intent it would claim every waking hour of three days as one meeting.
# Context only, on any calendar, before the window even clips it to a day.
CONTEXT_MAX_DURATION = timedelta(hours=12)

# Guard-rails for turning a family intent's silence into `present`/`family`.
# See the module docstring and `_apply_family_inference`.
#
# (a) Density: "If most of the time I was on Claude Code or email, maybe it
# didn't happen." A span that is mostly displaced work evidence probably
# means the family event was skipped, not silently attended.
FAMILY_INFERENCE_MAX_WORK_SHARE = 0.6
# (b) Minimum length: a gap between two bursts of chat is not family time.
FAMILY_INFERENCE_MIN_MINUTES = 20

# Advisory-lock namespace for per-day rebuilds. Any constant works; this one is
# arbitrary and only has to be unique within IBLU.
_LOCK_NAMESPACE = 8171


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.iblu_timezone)


def _floor(dt: datetime) -> datetime:
    seconds = (dt.minute * 60 + dt.second) % int(FLOOR.total_seconds())
    return (dt - timedelta(seconds=seconds)).replace(microsecond=0)


def _ceil(dt: datetime) -> datetime:
    floored = _floor(dt)
    return floored if floored == dt else floored + FLOOR


def day_bounds(on: date, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """The UTC instants that bracket a local calendar day."""
    tz = tz or _tz()
    start = datetime.combine(on, datetime.min.time(), tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(
        timezone.utc
    )


# --- the pieces the timeline is assembled from ------------------------------


@dataclass
class Interval:
    """A stretch of the day with an intent attached to it (a calendar event)."""

    start: datetime
    end: datetime
    event_id: str | None = None
    title: str = ""
    venture: str | None = None
    # Which calendar this intent came from — set by `load_intents`, used for
    # de-duplication, the intent classifier's cache key, and diagnostics.
    calendar_id: str | None = None
    account: str | None = None
    ical_uid: str | None = None
    attendee_count: int = 0
    # Domains only (never full addresses) — enough for the classifier to see
    # "this involves deadlift.io" without carrying anyone's email into a
    # prompt.
    attendee_domains: tuple[str, ...] = ()
    # Only meaningful when `needs_commitment_check` is True: who created and
    # organised the event, and whether Ignas himself is a named attendee. A
    # shared family calendar holds his wife's and children's plans just as
    # easily as his own, so these are what the commitment classifier reasons
    # from — never used for a workspace primary, which is trusted as-is.
    creator_email: str | None = None
    organizer_email: str | None = None
    is_self_attendee: bool = False
    # True for anything that is NOT a configured account's own primary
    # calendar — i.e. an `INTENT_CALENDARS` entry. Those calendars are not
    # exclusively his, so an event there defaults to `is_context=True` until
    # the classifier (or nothing, ever) confirms it is actually his.
    needs_commitment_check: bool = False
    # An intent `build()` must ignore for blocks and attention — informative
    # only (module docstring §B / the coordinator's whereabouts-marker note).
    # Defaults True for anything still needing a commitment check, and True
    # for anything longer than `CONTEXT_MAX_DURATION` — the latter can never
    # be un-set by a classification, which is what `is_context_locked` guards.
    is_context: bool = False
    is_context_locked: bool = False


@dataclass
class Cluster:
    """A stretch of the day with evidence attached to it (signals)."""

    start: datetime
    end: datetime
    signal_ids: list[int] = field(default_factory=list)
    times: list[datetime] = field(default_factory=list)
    ventures: list[tuple[str | None, str]] = field(default_factory=list)
    # Index-aligned with `times`/`signal_ids` (unlike a naive "only append
    # when truthy" list would be) — a signal with no work_type/project still
    # gets a `None` placeholder, because per-SLICE attribution (`build`) picks
    # a signal's labels out of these lists BY POSITION when it cuts a cluster
    # at an intent boundary, and a shifted index would attribute one signal's
    # label to a different signal's minute.
    work_types: list[str | None] = field(default_factory=list)
    projects: list[str | None] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def _majority(values: list) -> tuple[object | None, int, int]:
    """The most common value, how often it won, and how many votes there were."""
    votes = [v for v in values if v]
    if not votes:
        return None, 0, 0
    counts: dict = {}
    for v in votes:
        counts[v] = counts.get(v, 0) + 1
    winner = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))
    return winner[0], winner[1], len(votes)


def _majority_with_threshold(
    values: list, *, total: int, min_agreement: float, min_coverage: float | None = None,
) -> object | None:
    """`_majority`, but the winner must be a REAL majority to count.

    `min_agreement` is the share of the votes (not of `total`) the winner must
    hold — the "60% of the opinions" or "more than 50% of the votes" half of
    LABEL_AGREEMENT/VENTURE_AGREEMENT. `min_coverage`, when given, is the
    share of `total` (the whole slice) those votes must themselves cover —
    the "and those make up at least half of the block's signals" half: three
    people naming the same project out of three who bothered to is not the
    same as three out of thirty.
    """
    winner, agree, votes = _majority(values)
    if winner is None or votes == 0:
        return None
    if agree / votes < min_agreement:
        return None
    if min_coverage is not None and (total == 0 or votes / total < min_coverage):
        return None
    return winner


def cluster_signals(rows: list[dict]) -> list[Cluster]:
    """Gap-split signals into stretches of work, floored to `FLOOR`.

    `rows` must be ordered by `occurred_at`. Each row needs at least `id`,
    `occurred_at`, `venture`, `venture_confidence`, `work_type`, `project`,
    `source`.
    """
    clusters: list[Cluster] = []
    current: Cluster | None = None
    last_at: datetime | None = None

    for row in rows:
        at = row["occurred_at"]
        if current is None or last_at is None or (at - last_at) > GAP:
            current = Cluster(start=at, end=at)
            clusters.append(current)
        current.end = at
        current.signal_ids.append(row["id"])
        current.times.append(at)
        current.ventures.append(
            (row.get("venture"), row.get("venture_confidence") or "inferred")
        )
        # Unconditional appends, including `None` — see the `Cluster.work_types`
        # docstring on why this list must stay index-aligned with `times`.
        current.work_types.append(row.get("work_type"))
        current.projects.append(row.get("project"))
        current.sources.append(row.get("source") or "")
        last_at = at

    # Widen to the floor only once the whole cluster is known, so a single
    # signal becomes exactly one 15-minute block rather than two.
    for c in clusters:
        c.start, c.end = _floor(c.start), _ceil(c.end + TAIL)
        if c.end - c.start < FLOOR:
            c.end = c.start + FLOOR
    return _merge_overlaps(clusters)


def _merge_overlaps(clusters: list[Cluster]) -> list[Cluster]:
    """Flooring can make two neighbours touch; touching stretches are one stretch."""
    merged: list[Cluster] = []
    for c in clusters:
        if merged and c.start <= merged[-1].end:
            prev = merged[-1]
            prev.end = max(prev.end, c.end)
            prev.signal_ids += c.signal_ids
            prev.times += c.times
            prev.ventures += c.ventures
            prev.work_types += c.work_types
            prev.projects += c.projects
            prev.sources += c.sources
        else:
            merged.append(c)
    return merged


def _subtract(interval: tuple[datetime, datetime], covers: list[tuple[datetime, datetime]]):
    """What is left of `interval` once every `covers` span is removed."""
    remaining = [interval]
    for c_start, c_end in sorted(covers):
        nxt = []
        for r_start, r_end in remaining:
            if c_end <= r_start or c_start >= r_end:
                nxt.append((r_start, r_end))
                continue
            if c_start > r_start:
                nxt.append((r_start, c_start))
            if c_end < r_end:
                nxt.append((c_end, r_end))
        remaining = nxt
    return remaining


def _intent_at(intents: list[Interval], start: datetime, end: datetime) -> Interval | None:
    """The intent covering the midpoint of a span, if any — the longest wins."""
    mid = start + (end - start) / 2
    covering = [i for i in intents if i.start <= mid < i.end]
    if not covering:
        return None
    return max(covering, key=lambda i: i.end - i.start)


def _cut_points(cluster: Cluster, intents: list[Interval]) -> list[datetime]:
    """Boundaries inside a cluster where the intent changes."""
    points = {cluster.start, cluster.end}
    for i in intents:
        for edge in (i.start, i.end):
            if cluster.start < edge < cluster.end:
                points.add(edge)
    return sorted(points)


def build(
    clusters: list[Cluster],
    intents: list[Interval],
    workday: tuple[datetime, datetime] | None = None,
    horizon: datetime | None = None,
) -> list[dict]:
    """Assemble the day. Returns block dicts ready for `_insert`.

    `workday` is the span IBLU will account for — pass it to have unobserved
    stretches inside it come back as `untracked` blocks (what the evening ping's
    `gap` question asks about). Omit it and only evidence and intent produce
    blocks, which is what the unit tests want.

    `horizon` bounds how far into the future family-presence inference is
    allowed to reach — see `_apply_family_inference`. Omitting it (the
    default) means no restriction, which is what every test that does not
    care about it wants.
    """
    # A context intent (a whereabouts marker, someone else's plan on a shared
    # calendar, a multi-day travel marker) is informative but is never allowed
    # to create a block or cause `displaced` — see `Interval.is_context`.
    intents = [i for i in intents if not i.is_context]

    blocks: list[dict] = []

    for cluster in clusters:
        edges = _cut_points(cluster, intents)

        for start, end in zip(edges, edges[1:]):
            # A signal belongs to the slice it happened in. Giving every slice
            # the whole cluster's evidence would make a 15-minute stretch claim
            # the 32 messages of the three hours around it. Thresholds are
            # applied per SLICE, on the slice's own evidence, for the same
            # reason (LABEL_AGREEMENT's docstring).
            here = [
                i for i, t in enumerate(cluster.times) if start <= t < end
            ]
            slice_ventures = [cluster.ventures[i] for i in here]
            venture_winner, v_agree, v_votes = _majority(
                [v for v, _ in slice_ventures]
            )
            venture = (
                venture_winner
                if v_votes > 0 and v_agree / v_votes > VENTURE_AGREEMENT
                else None
            )
            work_type = _majority_with_threshold(
                [cluster.work_types[i] for i in here], total=len(here),
                min_agreement=LABEL_AGREEMENT, min_coverage=LABEL_COVERAGE,
            )
            project = _majority_with_threshold(
                [cluster.projects[i] for i in here], total=len(here),
                min_agreement=LABEL_AGREEMENT, min_coverage=LABEL_COVERAGE,
            )
            # 'fact' only when every vote agreed AND at least one of them was
            # a fact in the first place. Anything less is an inference.
            all_agree = v_votes > 0 and v_agree == v_votes
            any_fact = any(
                c == "fact" and v == venture for v, c in slice_ventures
            )
            confidence = "fact" if (all_agree and any_fact) else "inferred"

            intent = _intent_at(intents, start, end)
            if not here and intent is not None:
                # A slice cut at an intent boundary that has NO signal of its
                # own is not "part of the surrounding stretch" — that reading
                # is only honest when nothing claims the time. Here something
                # DOES claim it (the intent), and nothing was recorded, which
                # is the ambiguous case, not `present`.
                attention = "ambiguous"
                venture = intent.venture
                work_type = None
                project = None
                confidence = "inferred"
            elif intent is None:
                attention = "present"
            elif intent.venture and venture and intent.venture != venture:
                attention = "displaced"
            else:
                attention = "present"
            blocks.append(
                {
                    "starts_at": start,
                    "ends_at": end,
                    "venture": venture,
                    "work_type": work_type,
                    "project": project,
                    "attention": attention,
                    "confidence": confidence,
                    "evidence": [cluster.signal_ids[i] for i in here],
                    "intent_event_id": intent.event_id if intent else None,
                    "_sources": _count_sources([cluster.sources[i] for i in here]),
                    "_intent_title": intent.title if intent else None,
                }
            )

    # Intent that produced nothing at all is the honest 'ambiguous'.
    #
    # `covered` grows as each intent claims its span. The first version computed
    # it once from the evidence blocks and never updated it, so two OVERLAPPING
    # calendar events each emitted an ambiguous block for the same minutes —
    # "Emory hosting" 15:45-16:00 and "dinner with emory" 15:45-17:30 on
    # 2026-09-14 both claimed 15:45, and the day double-counted itself. Ignas
    # double-books constantly; this is the normal case, not an edge one.
    #
    # Longest-first within the same start, so the span goes to the event that
    # says more about the afternoon than a fifteen-minute fragment does.
    covered = [(b["starts_at"], b["ends_at"]) for b in blocks]
    for intent in sorted(intents, key=lambda i: (i.start, -(i.end - i.start))):
        for start, end in _subtract((intent.start, intent.end), covered):
            if end - start < MIN_AMBIGUOUS:
                continue
            covered.append((start, end))
            blocks.append(
                {
                    "starts_at": start,
                    "ends_at": end,
                    "venture": intent.venture,
                    "work_type": None,
                    "project": None,
                    "attention": "ambiguous",
                    "confidence": "inferred",
                    "evidence": [],
                    "intent_event_id": intent.event_id,
                    "_sources": {},
                    "_intent_title": intent.title,
                }
            )

    _apply_family_inference(blocks, intents, clusters, horizon)

    blocks += _untracked(blocks, workday)

    blocks.sort(key=lambda b: (b["starts_at"], b["ends_at"]))
    merged = _merge_adjacent(blocks)
    # The line is written last, so it describes the block that survived the
    # merge rather than one of the slices that went into it.
    for block in merged:
        block["reasoning"] = _reasoning(block)
        block["intent_title"] = block.pop("_intent_title", None)
        block["untracked"] = bool(block.pop("_untracked", False))
        block.pop("_sources", None)
        block.pop("_family_inferred", None)
    return merged


def _apply_family_inference(
    blocks: list[dict],
    intents: list[Interval],
    clusters: list[Cluster],
    horizon: datetime | None,
) -> None:
    """Turn a family intent's silent remainder into `present`/`family`/`inferred`.

    Mutates `blocks` in place. Only ever touches blocks that are already
    `ambiguous` with no evidence and whose `intent_event_id` names a `family`
    intent (which, by the time this runs, has already survived the
    `is_context` filter — either a trusted workspace primary or an
    `INTENT_CALENDARS` event the classifier confirmed, at high confidence, is
    something Ignas himself attends, never a whereabouts marker). See the
    module docstring for why this exists and the four guard-rails below.
    """
    family_intents = {
        i.event_id: i for i in intents if i.venture == "family" and i.event_id
    }
    if not family_intents:
        return

    by_intent: dict[str, list[dict]] = {}
    for b in blocks:
        eid = b.get("intent_event_id")
        if eid in family_intents:
            by_intent.setdefault(eid, []).append(b)

    for eid, intent_blocks in by_intent.items():
        intent = family_intents[eid]
        span_minutes = (intent.end - intent.start).total_seconds() / 60
        if span_minutes <= 0:
            continue

        # (a) Density: a span mostly covered by evidence of DISPLACED work
        # probably means the family event did not happen, not that it
        # happened silently alongside a busy inbox.
        #
        # Counts every slice with evidence that is not itself family, not only
        # `displaced` ones: a work slice is only marked displaced when its
        # venture is known, and the label thresholds now leave venture unset
        # on a mixed stretch. Counting only `displaced` let an afternoon of
        # unattributable work be claimed as family time.
        work_minutes = sum(
            (b["ends_at"] - b["starts_at"]).total_seconds() / 60
            for b in intent_blocks
            if b.get("evidence") and b.get("venture") != "family"
        )
        if work_minutes / span_minutes >= FAMILY_INFERENCE_MAX_WORK_SHARE:
            continue

        # (c) The recorder must have been demonstrably running that day —
        # otherwise a quiet afternoon and a dead collector look identical.
        watched_elsewhere = any(
            t < intent.start or t >= intent.end
            for c in clusters
            for t in c.times
        )
        if not watched_elsewhere:
            continue

        for b in intent_blocks:
            if b.get("evidence") or b["attention"] != "ambiguous":
                continue  # a slice with its own evidence, not a remainder

            # (b) Minimum length: a gap between two bursts of chat is not
            # family time.
            minutes = (b["ends_at"] - b["starts_at"]).total_seconds() / 60
            if minutes < FAMILY_INFERENCE_MIN_MINUTES:
                continue

            # (d) Data horizon: do not infer into a stretch that may simply
            # not have been collected yet. Deliberately not split at the
            # horizon — a remainder that only PARTLY fits would leave a
            # sliver behind, which is its own kind of misleading.
            if horizon is not None and b["ends_at"] > horizon:
                continue

            b["attention"] = "present"
            b["venture"] = "family"
            b["confidence"] = "inferred"
            b["_family_inferred"] = True


def _merge_adjacent(blocks: list[dict]) -> list[dict]:
    """Neighbouring blocks that say the same thing are one block."""
    out: list[dict] = []
    for b in blocks:
        prev = out[-1] if out else None
        same = (
            prev
            and prev["ends_at"] == b["starts_at"]
            and prev.get("_untracked") == b.get("_untracked")
            # Two back-to-back meetings that both produced nothing are two
            # unaccounted meetings, not one long one — merging them lost the
            # second one's name, which is the only thing that makes an
            # ambiguous block answerable.
            and prev.get("_intent_title") == b.get("_intent_title")
            # An inferred "assumed it happened" block reads nothing like a
            # real evidence-based one, even when every other field matches —
            # they must never silently merge into one reasoning line.
            and prev.get("_family_inferred") == b.get("_family_inferred")
            and all(
                prev[k] == b[k]
                for k in ("venture", "work_type", "project", "attention", "confidence")
            )
        )
        if same:
            prev["ends_at"] = b["ends_at"]
            prev["evidence"] = list(dict.fromkeys(prev["evidence"] + b["evidence"]))
            for src, n in b["_sources"].items():
                prev["_sources"][src] = prev["_sources"].get(src, 0) + n
            prev["_intent_title"] = prev["_intent_title"] or b["_intent_title"]
        else:
            out.append(dict(b))
    return out


def _count_sources(sources: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in sources:
        if s:
            counts[s] = counts.get(s, 0) + 1
    return counts


def _reasoning(block: dict) -> str:
    """The line the card shows — why this block says what it says.

    Written after merging, so the minutes it quotes are the block's own.
    """
    minutes = int((block["ends_at"] - block["starts_at"]).total_seconds() // 60)
    title = block.get("_intent_title")

    if block.get("_family_inferred"):
        # The one place IBLU turns silence into presence — the wording must
        # say "assumed" every time so it is never mistaken for a recording.
        return f'{minutes} min · nothing else recorded during "{title}" — assumed it happened'

    if block["attention"] == "ambiguous":
        if not title:
            return f"{minutes} min · nothing recorded, and nothing on the calendar either"
        return f'{minutes} min · "{title}" was on the calendar; nothing recorded during it'

    evidence = ", ".join(
        f"{n} {src}" for src, n in sorted(block["_sources"].items())
    )
    # A slice cut out of the middle of a stretch of work can legitimately
    # contain no signal of its own; it is still part of that stretch.
    line = f"{minutes} min · {evidence or 'part of the surrounding stretch'}"
    if title:
        line += (
            f' · calendar said "{title}"'
            if block["attention"] == "displaced"
            else f' · during "{title}"'
        )
    return line


# --- reading the two inputs -------------------------------------------------


def load_signals(conn, start: datetime, end: datetime) -> list[dict]:
    """Signals Ignas himself produced in the window, oldest first.

    `actor='other'` rows are inbound demand, not his attention — they belong to
    the review, not to the reconstruction of what he did.
    """
    return conn.execute(
        """
        SELECT id, source, occurred_at, venture, venture_confidence,
               work_type, project, subject, counterpart, collected_at
          FROM signals
         WHERE occurred_at >= %s AND occurred_at < %s
           AND actor = 'me'
           -- Excluded signals are real messages that were never work: test
           -- sends and fixtures. Kept for audit, never counted (migration 009).
           AND excluded_reason IS NULL
         ORDER BY occurred_at
        """,
        (start, end),
    ).fetchall()


# Account aliases whose PRIMARY calendar is a dedicated work calendar — no
# guessing needed, unlike BLT's primary, which also carries flights, school
# runs and family and so gets NO account-level default (HANDOFF §16: an
# intent with no venture of its own can never cause `displaced`). Only used
# as the fallback when the event's own title/attendees give `venture_hints`
# nothing to go on.
WORKSPACE_ACCOUNT_VENTURE: dict[str, str] = {"deadlift": "deadlift", "choco": "choco"}


def _known_ventures(conn) -> set[str] | None:
    """`ventures.code`, or `None` if the table cannot be read right now.

    `None` means "cannot validate", not "nothing is valid" — an `INTENT_
    CALENDARS` entry is accepted rather than silently dropped when the
    registry itself is unreachable, so a DB hiccup does not also cost the
    calendars that were fine.
    """
    try:
        return {r["code"] for r in conn.execute("SELECT code FROM ventures").fetchall()}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def _load_ventures(conn) -> list[dict]:
    """`ventures.code` and `.label`, or `[]` if the table cannot be read."""
    try:
        return conn.execute("SELECT code, label FROM ventures").fetchall()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def _compact_intent(
    event: dict,
    *,
    calendar_id: str,
    account: str,
    default_venture: str | None,
    needs_commitment_check: bool,
    tz: ZoneInfo,
    window: tuple[datetime, datetime],
) -> Interval | None:
    """One Google Calendar event -> an `Interval`, or `None` if it is not one.

    All-day events are not intents about a stretch of the day, and declined
    meetings were never commitments, so both are dropped.
    """
    from ..tools import calendar_manage

    if event.get("status") == "cancelled":
        return None
    if calendar_manage._is_declined(event):
        return None
    if "dateTime" not in (event.get("start") or {}):
        return None  # all-day

    raw_start, _ = calendar_manage._edge(event["start"], tz)
    raw_end, _ = calendar_manage._edge(event["end"], tz)
    # (see CONTEXT_MAX_DURATION) — computed from the RAW event span, before
    # the day-window clip below, so a multi-day marker is caught even though
    # clipping would otherwise make it look like an ordinary same-day event.
    is_context_locked = (raw_end - raw_start) > CONTEXT_MAX_DURATION

    window_start, window_end = window
    e_start = max(_floor(raw_start.astimezone(timezone.utc)), window_start)
    e_end = min(_ceil(raw_end.astimezone(timezone.utc)), window_end)
    if e_end <= e_start:
        return None

    title = event.get("summary", "") or "(untitled)"
    attendees_list = event.get("attendees") or []
    attendee_emails = [a.get("email", "") for a in attendees_list]
    attendees_str = " ".join(attendee_emails)
    # Deliberately no account passed to `infer`: it falls back to "whose
    # mailbox was this" when nothing else matches, and that fallback would
    # give every event on his calendar the venture 'blt' — which would make a
    # flight, a dentist appointment and a client call all claim BLT's time.
    # An intent only has a venture when the event itself says so, or the
    # calendar it came from has a configured default; an intent with no
    # venture can never make a block `displaced`. A flight is not a claim on
    # his attention, a client meeting is.
    venture, _ = venture_hints.infer("", counterpart=attendees_str, subject=title)
    if venture is None:
        venture = default_venture

    domains = tuple(sorted({
        e.split("@", 1)[1].lower() for e in attendee_emails if "@" in e
    }))

    return Interval(
        start=e_start,
        end=e_end,
        event_id=event.get("id"),
        title=title,
        venture=venture,
        calendar_id=calendar_id,
        account=account,
        ical_uid=event.get("iCalUID"),
        attendee_count=len(attendees_list),
        attendee_domains=domains,
        creator_email=(event.get("creator") or {}).get("email"),
        organizer_email=(event.get("organizer") or {}).get("email"),
        is_self_attendee=calendar_manage._self_response(event) is not None,
        needs_commitment_check=needs_commitment_check,
        # Long events are locked as context no matter what; a calendar not
        # exclusively his own defaults to context until the classifier — or
        # nothing, ever — confirms otherwise.
        is_context=is_context_locked or needs_commitment_check,
        is_context_locked=is_context_locked,
    )


def _dedupe_intents(intents: list[Interval]) -> list[Interval]:
    """The same real-world meeting often appears on two calendars (invited on
    both accounts). Keep one: same `iCalUID`, or identical title and identical
    start/end, in the order the calendars were read (primaries before
    `INTENT_CALENDARS` extras — see `load_intents`), so the more-trusted copy
    wins when both exist.
    """
    seen_uids: set[str] = set()
    seen_title_time: set[tuple[str, datetime, datetime]] = set()
    out: list[Interval] = []
    for iv in intents:
        if iv.ical_uid and iv.ical_uid in seen_uids:
            continue
        key = (iv.title, iv.start, iv.end)
        if key in seen_title_time:
            continue
        if iv.ical_uid:
            seen_uids.add(iv.ical_uid)
        seen_title_time.add(key)
        out.append(iv)
    return out


def load_intents(conn, start: datetime, end: datetime) -> list[Interval]:
    """The day Ignas planned — every configured account's primary calendar,
    plus every `INTENT_CALENDARS` extra. Never written to.

    A calendar that cannot be read (403/404/not shared/no credentials) is
    logged and skipped; the others are never lost over one bad calendar.
    """
    from ..google_auth import build_service
    from ..store import observations as obs
    from ..tools import calendar_manage

    tz = _tz()
    window = (start, end)
    known_ventures = _known_ventures(conn) if conn is not None else None

    # (calendar_id, account, default_venture, needs_commitment_check)
    sources: list[tuple[str, str, str | None, bool]] = []
    for acct in settings.configured_accounts():
        alias = acct["alias"]
        sources.append(("primary", alias, WORKSPACE_ACCOUNT_VENTURE.get(alias), False))

    for entry in settings.intent_calendars_parsed:
        if known_ventures is not None and entry["venture"] not in known_ventures:
            logger.warning(
                "analyst: INTENT_CALENDARS names unknown venture %r — skipping calendar %r",
                entry["venture"], entry["calendar_id"],
            )
            continue
        sources.append((entry["calendar_id"], entry["account"], entry["venture"], True))

    intents: list[Interval] = []
    for calendar_id, account, default_venture, needs_commitment_check in sources:
        try:
            service = build_service("calendar", "v3", account=account)
            items = calendar_manage._fetch_events(service, calendar_id, start, end)
        except Exception as exc:  # noqa: BLE001 — one bad calendar must not lose the rest
            logger.warning(
                "analyst: intent calendar %r on account %r unreadable (%s)",
                calendar_id, account, exc,
            )
            obs.record_safe(
                source="analyst", kind="intent_calendar_unreadable", severity="warn",
                summary=f"calendar {calendar_id!r} on account {account!r} could not be read",
                detail=str(exc)[:500],
                evidence={"calendar_id": calendar_id, "account": account},
                fp=obs.fingerprint("analyst", "intent_calendar_unreadable", calendar_id, account),
            )
            continue

        for event in items:
            iv = _compact_intent(
                event,
                calendar_id=calendar_id, account=account,
                default_venture=default_venture,
                needs_commitment_check=needs_commitment_check,
                tz=tz, window=window,
            )
            if iv is not None:
                intents.append(iv)

    return _dedupe_intents(intents)


# --- writing ----------------------------------------------------------------


def _insert(conn, on: date, rows: list[dict]) -> list[int]:
    from psycopg.types.json import Jsonb

    ids = []
    for row in rows:
        result = conn.execute(
            """
            INSERT INTO blocks
                (local_date, starts_at, ends_at, venture, work_type, project,
                 attention, confidence, evidence, reasoning, intent_event_id,
                 intent_title, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'analyst')
            RETURNING id
            """,
            (
                on,
                row["starts_at"],
                row["ends_at"],
                row["venture"],
                row["work_type"],
                row["project"],
                row["attention"],
                row["confidence"],
                Jsonb(row["evidence"]),
                row["reasoning"],
                row["intent_event_id"],
                row["intent_title"],
            ),
        ).fetchone()
        ids.append(result["id"])
    return ids


def _supersede(conn, on: date, previous_ids: list[int], new_ids: list[int]) -> None:
    """Point the old day at the new one. Nothing is deleted (D9).

    There is no per-block correspondence between the two days, so every old
    block points at the first new one — enough to find the replacement, honest
    about the fact that the day was rebuilt rather than edited.
    """
    if not previous_ids:
        return
    if new_ids:
        conn.execute(
            "UPDATE blocks SET superseded_by = %s WHERE id = ANY(%s)",
            (new_ids[0], previous_ids),
        )
        return
    # Nothing replaced them — every minute of the day is now covered by blocks
    # Ignas confirmed, so the rebuild produced no rows at all. The old guesses
    # must still retire: `superseded_by = NULL` is "still live", so the previous
    # version left them standing, overlapping the confirmed blocks and
    # double-counting the day. A row pointing at itself is retired without
    # claiming a replacement that does not exist.
    conn.execute(
        "UPDATE blocks SET superseded_by = id WHERE id = ANY(%s)", (previous_ids,)
    )


def live_blocks(conn, on: date) -> list[dict]:
    """The current reconstruction of a day."""
    return conn.execute(
        """
        SELECT id, starts_at, ends_at, venture, work_type, project, attention,
               confidence, evidence, reasoning, calendar_event_id,
               intent_event_id, intent_title
          FROM blocks
         WHERE local_date = %s AND superseded_by IS NULL
         ORDER BY starts_at
        """,
        (on,),
    ).fetchall()


def reconstruct(conn, on: date, *, dry: bool = False, mirror: bool = True) -> dict:
    """Rebuild one local day. Returns a summary of what it decided."""
    if settings.use_mock:
        raise RuntimeError(
            "refusing to reconstruct in mock mode — the calendar and signals "
            "would both be fabricated"
        )

    start, end = day_bounds(on)

    # One rebuild of a given day at a time. The timer fires at 17:00 and 20:15
    # and a manual run can overlap either; two concurrent rebuilds would each
    # read the same `previous`, each insert a full day, and each supersede only
    # the rows the other had already replaced — leaving TWO live generations,
    # double minutes, and duplicate mirror events. The lock is per-day and
    # released with the transaction.
    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                 (_LOCK_NAMESPACE, on.toordinal()))

    signals = load_signals(conn, start, end)
    try:
        intents = load_intents(conn, start, end)
    except Exception as exc:  # a calendar outage must not lose the evidence
        logger.warning("analyst: no intent calendar for %s (%s)", on, exc)
        intents = []
    else:
        try:
            from .intents import classify_missing

            intents = classify_missing(conn, intents, _load_ventures(conn))
        except Exception as exc:  # noqa: BLE001 — a bad classifier must not break the day
            logger.warning("analyst: intent classifier unavailable for %s (%s)", on, exc)

    # Anything Ignas confirmed by tapping is a fact, and a later reconstruct is
    # a guess. The guess never overwrites the fact: confirmed blocks are held
    # out of the rebuild entirely and the new timeline is cut around them.
    confirmed = [b for b in live_blocks(conn, on) if b["confidence"] == "fact"]
    workday = workday_bounds(on)
    # Family-presence inference (see the module docstring) must never reach
    # into a stretch that simply has not been collected yet — the newest
    # `collected_at` this day has, or `now` when the day has no signals at
    # all and so no later data could exist regardless.
    collected_ats = [s["collected_at"] for s in signals if s.get("collected_at")]
    horizon = max(collected_ats) if collected_ats else datetime.now(timezone.utc)
    rows = build(cluster_signals(signals), intents, workday=workday, horizon=horizon)
    rows = _clip_to_day(rows, (start, end))
    rows = _carve_out(
        rows, [(b["starts_at"], b["ends_at"]) for b in confirmed], signals
    )

    # The arithmetic is done; now the judgement. A failing judge is a no-op —
    # the computed day stands and `llm=False` says so.
    rows, llm_used = _judge(conn, rows, signals)

    summary = {
        "date": on.isoformat(),
        "signals": len(signals),
        "intents": len(intents),
        "blocks": len(rows),
        "confirmed_kept": len(confirmed),
        "by_attention": _counts(rows),
        "minutes": {
            k: sum(
                int((b["ends_at"] - b["starts_at"]).total_seconds() // 60)
                for b in rows
                if b["attention"] == k
            )
            for k in ("present", "displaced", "ambiguous")
        },
        "llm": llm_used,
        "dry": dry,
    }
    if dry:
        summary["preview"] = [
            {
                "start": b["starts_at"].astimezone(_tz()).strftime("%H:%M"),
                "end": b["ends_at"].astimezone(_tz()).strftime("%H:%M"),
                "venture": b["venture"],
                "attention": b["attention"],
                "reasoning": b["reasoning"],
            }
            for b in rows
        ]
        return summary

    # `confirmed` rows stay live — they are not superseded and not rewritten.
    previous = [
        b["id"] for b in live_blocks(conn, on) if b["confidence"] != "fact"
    ]
    new_ids = _insert(conn, on, rows)
    _supersede(conn, on, previous, new_ids)
    summary["superseded"] = len(previous)
    summary["ids"] = new_ids

    # The analyst has had a `collector_state` row since migration 005 and never
    # wrote to it, so "has the analyst run?" had no answer — which is exactly
    # the kind of silence the sense-check is for. It found this one itself.
    from ..collectors import set_state

    set_state(conn, "analyst_blocks", watermark=end, error=None)

    if mirror and settings.secretary_calendar_id:
        from .mirror import mirror_day

        try:
            summary["mirror"] = mirror_day(conn, on)
        except Exception as exc:  # the database is the record; the calendar is a view
            logger.exception("analyst: mirroring %s failed", on)
            summary["mirror"] = {"error": str(exc)}
    return summary


def _judge(conn, rows: list[dict], signals: list[dict]) -> tuple[list[dict], bool]:
    """Let the LLM relabel the day. Never fatal — see `analyst/judge.py`."""
    from .judge import judge

    try:
        ventures = [r["code"] for r in conn.execute("SELECT code FROM ventures").fetchall()]
        work_types = [r["code"] for r in conn.execute("SELECT code FROM work_types").fetchall()]
        try:  # the project registry may not exist yet on an older database
            projects = [
                r["code"] for r in conn.execute("SELECT code FROM projects").fetchall()
            ]
        except Exception:
            conn.rollback()
            projects = []
    except Exception as exc:  # noqa: BLE001
        logger.warning("analyst: could not read the taxonomy (%s) — skipping the judge", exc)
        return rows, False

    return judge(
        rows, signals,
        ventures=ventures, work_types=work_types, projects=projects, tz=_tz(),
    )


def _counts(rows: list[dict]) -> dict[str, int]:
    out = {"present": 0, "displaced": 0, "ambiguous": 0}
    for r in rows:
        out[r["attention"]] += 1
    return out


def _untracked(blocks: list[dict], workday: tuple[datetime, datetime] | None) -> list[dict]:
    """The stretches of the workday nothing accounts for.

    These are the only blocks IBLU writes with no evidence *and* no intent, and
    they exist for one reason: the evening ping's `gap` question needs something
    to supersede when Ignas says what an unaccounted hour was. They are
    `ambiguous` with `venture = NULL` — the schema's way of saying "unknown",
    which is not the same as "idle" and must never be rendered as idle.
    """
    if workday is None:
        return []
    covered = [(b["starts_at"], b["ends_at"]) for b in blocks]
    out = []
    for start, end in _subtract(workday, covered):
        if end - start < MIN_UNTRACKED:
            continue
        out.append(
            {
                "starts_at": start,
                "ends_at": end,
                "venture": None,
                "work_type": None,
                "project": None,
                "attention": "ambiguous",
                "confidence": "inferred",
                "evidence": [],
                "intent_event_id": None,
                "_sources": {},
                "_intent_title": None,
                "_untracked": True,
            }
        )
    return out


def workday_bounds(on: date, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """The part of a local day IBLU will account for, in UTC."""
    tz = tz or _tz()
    start = datetime.combine(on, WORKDAY_START, tzinfo=tz)
    end = datetime.combine(on, WORKDAY_END, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _carve_out(
    rows: list[dict],
    keep: list[tuple[datetime, datetime]],
    signals: list[dict] | None = None,
) -> list[dict]:
    """Remove the spans already answered for from a freshly built day.

    A block Ignas confirmed is the truth for its span; the reconstruction is
    only allowed to describe what is left. A remainder shorter than the floor
    is dropped rather than emitted as a sliver.

    Every surviving piece has its evidence and its reasoning **re-derived** for
    its own new span. The first version copied both onto each piece unchanged,
    which reintroduced one step later exactly the bug `build()` exists to avoid:
    a 20-minute remainder of a 60-minute block reading "60 min · 3 gmail" and
    claiming a signal that fell inside the confirmed span that was just cut out.
    """
    if not keep:
        return rows

    at_by_id = {s["id"]: s["occurred_at"] for s in (signals or [])}
    src_by_id = {s["id"]: s.get("source") or "" for s in (signals or [])}

    out: list[dict] = []
    for row in rows:
        for start, end in _subtract((row["starts_at"], row["ends_at"]), keep):
            if end - start < FLOOR:
                continue
            piece = dict(row)
            piece["starts_at"], piece["ends_at"] = start, end
            if piece.get("untracked") or not at_by_id:
                piece["evidence"] = []
            else:
                piece["evidence"] = [
                    sid for sid in (row["evidence"] or [])
                    if sid in at_by_id and start <= at_by_id[sid] < end
                ]
            piece["_sources"] = _count_sources(
                [src_by_id.get(sid, "") for sid in piece["evidence"]]
            )
            piece["_intent_title"] = row.get("intent_title")
            piece["_untracked"] = bool(row.get("untracked"))
            piece["reasoning"] = _reasoning(piece)
            piece.pop("_sources", None)
            piece.pop("_intent_title", None)
            piece.pop("_untracked", None)
            out.append(piece)
    return out


def _clip_to_day(rows: list[dict], bounds: tuple[datetime, datetime]) -> list[dict]:
    """Keep every block inside the local day it will be filed under.

    `cluster_signals` widens a cluster by `TAIL` with no idea where midnight is,
    so a signal at 23:58 produced a block running to 00:15 — stored under
    *yesterday*, invisible to tomorrow's rebuild, and free to overlap whatever
    tomorrow produces for the same minutes. `local_date` is a single column; a
    block must not straddle it.
    """
    day_start, day_end = bounds
    out = []
    for row in rows:
        start = max(row["starts_at"], day_start)
        end = min(row["ends_at"], day_end)
        if end - start < FLOOR:
            continue
        if (start, end) != (row["starts_at"], row["ends_at"]):
            row = dict(row)
            row["starts_at"], row["ends_at"] = start, end
        out.append(row)
    return out
