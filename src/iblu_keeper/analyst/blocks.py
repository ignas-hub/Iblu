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


@dataclass
class Cluster:
    """A stretch of the day with evidence attached to it (signals)."""

    start: datetime
    end: datetime
    signal_ids: list[int] = field(default_factory=list)
    times: list[datetime] = field(default_factory=list)
    ventures: list[tuple[str | None, str]] = field(default_factory=list)
    work_types: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
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
        if row.get("work_type"):
            current.work_types.append(row["work_type"])
        if row.get("project"):
            current.projects.append(row["project"])
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
) -> list[dict]:
    """Assemble the day. Returns block dicts ready for `_insert`.

    `workday` is the span IBLU will account for — pass it to have unobserved
    stretches inside it come back as `untracked` blocks (what the evening ping's
    `gap` question asks about). Omit it and only evidence and intent produce
    blocks, which is what the unit tests want.
    """
    blocks: list[dict] = []

    for cluster in clusters:
        edges = _cut_points(cluster, intents)
        venture, agree, votes = _majority([v for v, _ in cluster.ventures])
        work_type, _, _ = _majority(cluster.work_types)
        project, _, _ = _majority(cluster.projects)
        # 'fact' only when every vote agreed AND at least one of them was a
        # fact in the first place. Anything less is an inference and says so.
        all_agree = votes > 0 and agree == votes
        any_fact = any(c == "fact" and v == venture for v, c in cluster.ventures)
        confidence = "fact" if (all_agree and any_fact) else "inferred"

        for start, end in zip(edges, edges[1:]):
            # A signal belongs to the slice it happened in. Giving every slice
            # the whole cluster's evidence would make a 15-minute stretch claim
            # the 32 messages of the three hours around it.
            here = [
                i for i, t in enumerate(cluster.times) if start <= t < end
            ]
            intent = _intent_at(intents, start, end)
            if intent is None:
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
    covered = [(b["starts_at"], b["ends_at"]) for b in blocks]
    for intent in intents:
        for start, end in _subtract((intent.start, intent.end), covered):
            if end - start < MIN_AMBIGUOUS:
                continue
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
    return merged


def _merge_adjacent(blocks: list[dict]) -> list[dict]:
    """Neighbouring blocks that say the same thing are one block."""
    out: list[dict] = []
    for b in blocks:
        prev = out[-1] if out else None
        same = (
            prev
            and prev["ends_at"] == b["starts_at"]
            and prev.get("_untracked") == b.get("_untracked")
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
               work_type, project, subject, counterpart
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


def load_intents(start: datetime, end: datetime) -> list[Interval]:
    """The day Ignas planned — his primary calendar. Never written to.

    All-day events are not intents about a stretch of the day, and declined
    meetings were never commitments, so both are dropped.
    """
    from ..tools import calendar_manage

    service = calendar_manage._service()
    items = calendar_manage._fetch_events(service, "primary", start, end)
    tz = _tz()

    intents: list[Interval] = []
    for event in items:
        if event.get("status") == "cancelled":
            continue
        if calendar_manage._is_declined(event):
            continue
        if "dateTime" not in (event.get("start") or {}):
            continue  # all-day
        e_start, _ = calendar_manage._edge(event["start"], tz)
        e_end, _ = calendar_manage._edge(event["end"], tz)
        e_start = max(_floor(e_start.astimezone(timezone.utc)), start)
        e_end = min(_ceil(e_end.astimezone(timezone.utc)), end)
        if e_end <= e_start:
            continue
        title = event.get("summary", "") or "(untitled)"
        attendees = " ".join(
            a.get("email", "") for a in (event.get("attendees") or [])
        )
        # Deliberately no account: `venture_hints` falls back to "whose mailbox
        # was this" when nothing else matches, and that fallback would give
        # every event on his calendar the venture 'blt' — which would make a
        # flight, a dentist appointment and a client call all claim BLT's time.
        # An intent only has a venture when the event itself says so, and an
        # intent with no venture can never make a block `displaced`: a flight
        # is not a claim on his attention, a client meeting is.
        venture, _ = venture_hints.infer(
            "", counterpart=attendees, subject=title
        )
        intents.append(
            Interval(
                start=e_start,
                end=e_end,
                event_id=event.get("id"),
                title=title,
                venture=venture,
            )
        )
    return intents


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
        intents = load_intents(start, end)
    except Exception as exc:  # a calendar outage must not lose the evidence
        logger.warning("analyst: no intent calendar for %s (%s)", on, exc)
        intents = []

    # Anything Ignas confirmed by tapping is a fact, and a later reconstruct is
    # a guess. The guess never overwrites the fact: confirmed blocks are held
    # out of the rebuild entirely and the new timeline is cut around them.
    confirmed = [b for b in live_blocks(conn, on) if b["confidence"] == "fact"]
    workday = workday_bounds(on)
    rows = build(cluster_signals(signals), intents, workday=workday)
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
