"""When to ask (plan §7.1).

Pure functions only — no database, no Google, no clock of their own. Everything
takes `now` and the day's events as arguments so the rules are unit-testable,
because "did it ask at a sensible moment?" is the part that decides whether the
whole thing is tolerable to live with.

The rule in one sentence: inside the window, ask at the first moment Ignas is
plainly not in a meeting; if the window is running out, ask anyway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger("iblu_keeper.pings.schedule")

KINDS = ("midday", "evening")

# Don't ask while a meeting is in progress, in the settling minutes after one,
# or just before the next starts.
AFTER_EVENT_GRACE = timedelta(minutes=5)
BEFORE_EVENT_GRACE = timedelta(minutes=10)

# Where the midday window's coverage starts, and the evening's fallback start
# if no midday ping was actually sent.
DAY_START = time(6, 0)
EVENING_FALLBACK_START = time(13, 0)

_DAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


@dataclass(frozen=True)
class Event:
    """A calendar event reduced to what scheduling cares about."""

    start: datetime
    end: datetime
    summary: str | None = None
    attendee_count: int = 0

    @property
    def is_self_block(self) -> bool:
        """Time I blocked for myself — the thing that gets displaced."""
        return self.attendee_count == 0


def parse_window(raw: str) -> tuple[time, time]:
    """'12:30-14:00' -> (time(12,30), time(14,0))."""
    try:
        start_raw, end_raw = raw.split("-", 1)
        start = time.fromisoformat(start_raw.strip())
        end = time.fromisoformat(end_raw.strip())
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            f"invalid ping window {raw!r} — expected 'HH:MM-HH:MM', e.g. '12:30-14:00'"
        ) from exc
    if end <= start:
        raise ValueError(f"ping window {raw!r} ends at or before it starts")
    return start, end


def is_ping_day(day: date, day_codes: frozenset[str]) -> bool:
    """True when this weekday is in PING_DAYS."""
    return _DAY_CODES[day.weekday()] in day_codes


def window_bounds(
    raw_window: str, day: date, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """The window as tz-aware datetimes on `day`."""
    start, end = parse_window(raw_window)
    return (
        datetime.combine(day, start, tzinfo=tz),
        datetime.combine(day, end, tzinfo=tz),
    )


def relevant_events(events: list[Event], window_end: datetime) -> list[Event]:
    """Events that could block a ping — ignores anything already long past."""
    return [e for e in events if e.end >= window_end - timedelta(hours=12)]


def free_now(now: datetime, events: list[Event]) -> tuple[bool, str]:
    """Is this a decent moment to interrupt? Returns `(ok, why_not)`.

    The reason string is for the log line — when a ping lands at the window's
    hard edge, it should be possible to see what kept blocking it.
    """
    for event in events:
        if event.start <= now < event.end:
            return False, f"in progress: {event.summary or 'untitled'}"
        if event.end <= now < event.end + AFTER_EVENT_GRACE:
            return False, f"just ended: {event.summary or 'untitled'}"
        if now < event.start < now + BEFORE_EVENT_GRACE:
            return False, f"starts soon: {event.summary or 'untitled'}"
    return True, ""


@dataclass(frozen=True)
class Decision:
    send: bool
    reason: str                # human sentence, for the log
    code: str = "ok"           # short token, for the one-line tick summary
    forced: bool = False       # sent at the window edge, not at a free moment


def decide(
    now: datetime,
    window_start: datetime,
    window_end: datetime,
    events: list[Event],
    already_handled: bool,
) -> Decision:
    """Whether to send this kind of ping, at this tick."""
    if already_handled:
        return Decision(False, "already sent or answered today", "done")
    if now < window_start:
        return Decision(False, f"before the window (opens {window_start:%H:%M})", "early")
    if now >= window_end:
        # Last chance: the window is closing, so interrupt regardless.
        return Decision(True, "window closing — sending regardless", "edge", forced=True)

    ok, why_not = free_now(now, relevant_events(events, window_end))
    if ok:
        return Decision(True, "free moment inside the window", "free")
    return Decision(False, why_not, "busy")


def coverage(
    kind: str,
    now: datetime,
    day: date,
    tz: ZoneInfo,
    midday_sent_at: datetime | None = None,
) -> tuple[datetime, datetime]:
    """The period the questions are about: `(covers_from, covers_to)`.

    Midday covers the morning; evening covers from the midday ping onward, so
    the two never ask about the same stretch twice.
    """
    if kind == "midday":
        return datetime.combine(day, DAY_START, tzinfo=tz), now
    if midday_sent_at is not None:
        return midday_sent_at, now
    return datetime.combine(day, EVENING_FALLBACK_START, tzinfo=tz), now
