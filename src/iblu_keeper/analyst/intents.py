"""Classifying the intents `load_intents` could not place on its own.

`load_intents` (in `blocks.py`) only gives an event a venture when its title
or attendees match a keyword or domain, or the calendar it came from has a
configured default. "Go school", "Tenis 18:00", "Roditelsku sastanak u školi"
match none of that — deterministic rules run out, and the mission still needs
an opinion about whether that stretch of the day was a claim on his attention.
One batched LLM call per reconstruct, over every intent still unresolved,
fills the gap the rules cannot.

Two independent judgements come out of the same call, because they are two
different questions about the same event:

  * **venture / is_work / confidence** — what is this event ABOUT?
  * **attendance / attendance_confidence** — asked ONLY for events from a
    calendar that is not exclusively his own (`Interval.
    needs_commitment_check`, set by `load_intents` for every `INTENT_
    CALENDARS` extra, never for a workspace primary). A shared family
    calendar holds his wife's plans and his children's just as easily as his
    own, and it holds plain WHEREABOUTS markers ("Ignas LT", "Ignas Zagreb
    10-14") that are not activities at all — a reminder of where someone is,
    not a thing Ignas attends. This used to be a boolean
    (`is_ignas_commitment`), and a boolean cannot hold "sometimes I go,
    sometimes I don't" — his own words about his son's football ("Futbolas")
    and school parents' meetings. `attendance` is one of three:

      - "his" — an activity Ignas himself attends. Only a HIGH confidence
        "his" turns an event from context into something that can create a
        block or cause `displaced` — see `blocks.build`'s `is_context`
        filter, unchanged from before.
      - "maybe" — a family activity he sometimes joins but is not implied to
        attend by default (a child's sport or lesson, a school event, a
        family outing). Never a commitment, at ANY confidence — "maybe" is
        already the cautious answer — but its silence is still read the same
        careful way a confirmed commitment's is
        (`blocks._apply_maybe_family_inference`), just without ever being
        able to cause `displaced` or claim minutes on its own.
      - "not_his" — someone else's appointment or whereabouts, a reminder, a
        delivery, a birthday, or a whereabouts marker, even when it names
        him. Stays context, same as before.

    A family commitment's silence can be read as presence
    (`blocks._apply_family_inference`), so a whereabouts marker wrongly
    trusted as "his" would turn a quiet afternoon into invented family time —
    which is exactly why "maybe" exists as a distinct, cautious middle
    ground instead of forcing every ambiguous family event into "his" or
    "not_his".

    A per-day tap on the evening `attended` question (plan item 3/4) is
    ground truth for ONE instance and overrides whatever this module decided
    for that day — see `blocks._apply_attendance_answers`.

Same discipline as `judge.py` throughout: never raise out of this module — a
bad or unavailable classifier leaves every intent exactly as `load_intents`
found it. `is_ignas_commitment` / `commitment_confidence` are still written
to the cache (`is_ignas_commitment = attendance == "his"`) for anything still
reading those columns directly, but the source of truth is `attendance`.

Results are cached in `intent_labels` (migration 012, `attendance` columns
added in 013) keyed by a hash of the calendar id, event id and title, so the
same event classifies the same way on every rebuild; the analyst runs twice a
day, and a label that flaps between runs is worse than no label. A cached row
with `attendance IS NULL` — made before migration 013, or under a database
that has not applied it yet — is treated as a cache MISS for the
commitment-check half of a classification (never for the venture half), so
every existing label upgrades to the three-way answer on its own, the next
time it is asked about.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

from ..config import settings

logger = logging.getLogger("iblu_keeper.analyst.intents")

ATTENDANCE_VALUES = ("his", "maybe", "not_his")

SYSTEM = """You classify calendar events the deterministic rules could not place.

You are shown a batch of calendar events for one day: title, attendee count,
attendee email DOMAINS only (never full addresses), duration, and which
calendar the event came from. For each one decide:

  - venture: which of IBLU's known ventures this event most likely belongs
    to, or null if you cannot tell or it belongs to none of them.
  - is_work: true if this looks like work of any kind, false if it looks
    like personal or family life.
  - confidence: "high" only when the title and context leave little room for
    doubt; "low" otherwise.

Only "high" confidence classifications are ever applied — a wrong guess made
confidently is worse than an admitted "I don't know", so do not inflate
confidence to look useful.

A calendar title says what was MEANT to happen, never what did. You are not
confirming anything happened; you are guessing what an event on the calendar
was ABOUT, for later matching against what actually happened.

When is_work is false and nothing else fits, only set venture to "family" if
the title clearly concerns family, children or home ("Go school", "Emory
dentist", "pickup kids") — an ordinary personal appointment with no such
signal gets venture: null, not "family".

Some events are marked [COMMITMENT CHECK] — they come from a calendar that is
not exclusively Ignas's own (a shared family calendar, a personal calendar),
so the event might belong to someone else entirely, or might not be an
activity at all. For those events ONLY, you are also given who created it,
who organised it, whether Ignas is a named attendee, and also decide:

  - attendance: "his", "maybe", or "not_his" — never a plain yes/no. "I
    sometimes go, sometimes I don't" (his own words, about his son's
    football and school parents' meetings) is a real answer for some of
    these events, and a boolean cannot hold it.

      * "his" — an activity Ignas HIMSELF attends: a client dinner, his own
        appointment, a meeting he is down to run. His own presence is the
        point of the event, not merely mentioned by it.
      * "maybe" — a FAMILY activity he sometimes joins but is not implied to
        attend by default: a child's sport practice or lesson, a school
        event, a family outing. He might be there, he might not, and the
        calendar entry alone does not say which.
          examples: "Futbolas" (his son's football practice) -> maybe.
          "Roditeljsku sastanak u školi" (a parents' meeting at school —
          sometimes him, sometimes his wife) -> maybe.
      * "not_his" — someone else's appointment or whereabouts, a reminder, a
        delivery, a birthday, or a WHEREABOUTS marker, even when it names
        him: "Ignas LT", "Ignas Zagreb 10-14" say where he is, not that this
        is a scheduled activity.
          examples: "Greta nicoj" (his wife's night out) -> not_his.
          "Ignas LT" (a location note, not an activity) -> not_his.
          "Mamos gdienis" (his mother's name day — not his activity) ->
          not_his.

    When genuinely unsure whether an event is "his" or "maybe", prefer
    "maybe" — the default must be conservative, because treating a
    whereabouts note or someone else's plan as "his" can make a quiet
    afternoon look like a confirmed commitment he skipped.
  - attendance_confidence: "high" only when you are quite sure. "maybe" does
    not need high confidence to be trusted — it is already the cautious
    answer — but "his" only ever turns an event into a real commitment (able
    to cause a work block to read as `displaced`) at HIGH confidence; at low
    confidence it is treated exactly like "not_his".

For events NOT marked [COMMITMENT CHECK], omit attendance and
attendance_confidence entirely — they do not apply to those events.

Measure nothing here against an ideal or a goal; you are labelling one batch
of calendar events, not judging performance.

Return only JSON: {"intents": [{"i": <index>, "venture": <code|null>,
"is_work": <bool>, "confidence": "high"|"low", "attendance":
"his"|"maybe"|"not_his", "attendance_confidence": "high"|"low"}]}
Only include attendance/attendance_confidence for a [COMMITMENT CHECK]
event. Include every index you were given."""


def event_key(calendar_id: str, event_id: str, title: str) -> str:
    """Stable cache key. A renamed event is a DIFFERENT key on purpose — a
    retitled event should be re-classified, not silently keep a stale label
    that no longer matches what it now says.
    """
    raw = f"{calendar_id or ''}|{event_id or ''}|{title or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


def instance_key(calendar_id: str, event_id: str, local_date) -> str:
    """Stable key for ONE occurrence of an event on ONE local day.

    What the evening `attended` question answers (`pings.compose`), and what
    `analyst.blocks._apply_attendance_answers` looks the answer up by before
    `build()` runs. `load_intents` reads calendars with `singleEvents=True`,
    so a recurring event already gets a distinct `event_id` per occurrence —
    the local date is included anyway, for defence in depth rather than
    necessity: it keeps the key legible as "this calendar, this event, this
    day" without ever having to trust that Google never reuses an id.
    """
    day = local_date.isoformat() if hasattr(local_date, "isoformat") else str(local_date)
    raw = f"{calendar_id or ''}|{event_id or ''}|{day}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(conn, key: str) -> dict | None:
    """Never raises — a missing `intent_labels` table, or a missing
    `attendance` column (migration 013 not yet applied), degrades to "no
    cache", never a broken reconstruct.
    """
    if conn is None:
        return None
    try:
        row = conn.execute(
            """
            SELECT venture, is_work, confidence, is_ignas_commitment,
                   commitment_confidence, attendance, attendance_confidence
              FROM intent_labels WHERE event_key = %s
            """,
            (key,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    if row is None:
        return None
    return dict(row)


def _cache_put(
    conn, key: str, title: str, result: dict, model: str,
) -> None:
    """Best-effort write. Same degrade-gracefully rule as `_cache_get` — but
    unlike a read, a write that fails because `attendance`/
    `attendance_confidence` do not exist yet (migration 013 not applied)
    retries with the old column set, so the classification is not lost
    entirely over two missing columns.
    """
    if conn is None:
        return

    is_commit = result.get("is_ignas_commitment")
    commit_conf = result.get("commitment_confidence")
    attendance = result.get("attendance")
    attendance_confidence = result.get("attendance_confidence")

    try:
        conn.execute(
            """
            INSERT INTO intent_labels
                (event_key, title, venture, is_work, confidence,
                 is_ignas_commitment, commitment_confidence,
                 attendance, attendance_confidence, model)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_key) DO UPDATE SET
                title = EXCLUDED.title,
                venture = EXCLUDED.venture,
                is_work = EXCLUDED.is_work,
                confidence = EXCLUDED.confidence,
                is_ignas_commitment = EXCLUDED.is_ignas_commitment,
                commitment_confidence = EXCLUDED.commitment_confidence,
                attendance = EXCLUDED.attendance,
                attendance_confidence = EXCLUDED.attendance_confidence,
                model = EXCLUDED.model,
                created_at = now()
            """,
            (
                key, title, result.get("venture"), result.get("is_work"),
                result.get("confidence"), is_commit, commit_conf,
                attendance, attendance_confidence, model,
            ),
        )
        return
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass

    # Column not there yet — degrade to the pre-013 shape rather than losing
    # the classification (venture/is_work/commitment) entirely.
    try:
        conn.execute(
            """
            INSERT INTO intent_labels
                (event_key, title, venture, is_work, confidence,
                 is_ignas_commitment, commitment_confidence, model)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_key) DO UPDATE SET
                title = EXCLUDED.title,
                venture = EXCLUDED.venture,
                is_work = EXCLUDED.is_work,
                confidence = EXCLUDED.confidence,
                is_ignas_commitment = EXCLUDED.is_ignas_commitment,
                commitment_confidence = EXCLUDED.commitment_confidence,
                model = EXCLUDED.model,
                created_at = now()
            """,
            (
                key, title, result.get("venture"), result.get("is_work"),
                result.get("confidence"), is_commit, commit_conf, model,
            ),
        )
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass


def _attendee_line(iv) -> str:
    domains = ",".join(getattr(iv, "attendee_domains", None) or ()) or "-"
    minutes = int((iv.end - iv.start).total_seconds() // 60)
    source = f"{getattr(iv, 'calendar_id', None) or 'primary'}@{getattr(iv, 'account', None) or '-'}"
    line = (
        f'"{iv.title}" attendees={getattr(iv, "attendee_count", 0)} '
        f"domains={domains} minutes={minutes} source={source}"
    )
    if getattr(iv, "needs_commitment_check", False):
        line += (
            f" creator={iv.creator_email or '-'} organizer={iv.organizer_email or '-'} "
            f"self_attendee={iv.is_self_attendee} [COMMITMENT CHECK]"
        )
    return line


def _call(targets: list, ventures: list[dict]) -> list[dict | None]:
    """One Anthropic call, classifying every target by its position. Never
    caught here — `classify_missing` is the boundary that must never raise.
    """
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
        logger.info("intents: continuing without mission/priorities (%s)", exc)

    venture_lines = "\n".join(f"  {v['code']}: {v['label']}" for v in ventures) or "  (none registered)"
    lines = [f"{i}. {_attendee_line(iv)}" for i, iv in enumerate(targets)]
    prompt = f"Ventures:\n{venture_lines}\n\nEvents:\n" + "\n".join(lines)

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
    return _parse_response(json.loads(text), len(targets))


def _parse_response(payload: dict, n: int) -> list[dict | None]:
    items = payload.get("intents")
    if not isinstance(items, list):
        raise ValueError("classifier returned no 'intents' list")

    out: list[dict | None] = [None] * n
    for item in items:
        if not isinstance(item, dict):
            continue
        i = item.get("i")
        if not isinstance(i, int) or not 0 <= i < n:
            continue
        confidence = item.get("confidence")
        if confidence not in ("high", "low"):
            continue
        venture = item.get("venture")
        if venture is not None and not isinstance(venture, str):
            continue
        is_work = item.get("is_work")
        attendance = item.get("attendance")
        if attendance not in ATTENDANCE_VALUES:
            attendance = None
        attendance_confidence = item.get("attendance_confidence")
        if attendance_confidence not in ("high", "low"):
            attendance_confidence = None
        out[i] = {
            "venture": venture,
            "is_work": bool(is_work) if isinstance(is_work, bool) else None,
            "confidence": confidence,
            "attendance": attendance,
            "attendance_confidence": attendance_confidence,
            # Compatibility columns for anything still reading the old
            # boolean directly (see the module docstring) — derived, never
            # asked of the model.
            "is_ignas_commitment": (attendance == "his") if attendance else None,
            "commitment_confidence": attendance_confidence,
        }
    return out


def _maybe_override_needles() -> list[str]:
    """`INTENT_MAYBE_TITLES` — comma-separated, case-insensitive substrings.

    Read with `os.getenv` directly, not through the frozen `Settings`
    dataclass: this is a runtime knob Ignas can change without a deploy, and
    `config.py` is not the place for it. Empty/unset means "no override" —
    the classifier decides alone, as before.
    """
    raw = os.getenv("INTENT_MAYBE_TITLES", "")
    return [n.strip().lower() for n in raw.split(",") if n.strip()]


def _apply_maybe_title_override(intents: list) -> None:
    """Force `attendance='maybe'` for any title matching `INTENT_MAYBE_TITLES`,
    regardless of what the classifier says — or whether it runs at all.

    Deliberately the FIRST thing `classify_missing` does, before the cache or
    the model are even consulted: Ignas's own override must hold even when
    there is no API key or the classifier is down, and it must never be
    second-guessed by a cached or freshly-returned model opinion (see the
    `attendance == "maybe"` skip later in `classify_missing`).
    """
    needles = _maybe_override_needles()
    if not needles:
        return
    for iv in intents:
        if not getattr(iv, "needs_commitment_check", False):
            continue
        # A locked context (> CONTEXT_MAX_DURATION) is never undone by
        # anything — see `Interval.is_context_locked`.
        if getattr(iv, "is_context_locked", False):
            continue
        title = (iv.title or "").lower()
        if any(needle in title for needle in needles):
            iv.attendance = "maybe"
            iv.is_context = True


def classify_missing(conn, intents: list, ventures: list[dict]) -> list:
    """Classify every intent that still needs a venture or a commitment check.

    Mutates and returns `intents`. Never raises: any failure — no API key, a
    network error, a malformed response — leaves every intent exactly as
    `load_intents` (and `_apply_maybe_title_override`) produced it.
    """
    _apply_maybe_title_override(intents)

    targets = [
        iv for iv in intents
        if iv.venture is None or getattr(iv, "needs_commitment_check", False)
    ]
    if not targets or not settings.anthropic_api_key:
        return intents

    valid_codes = {v["code"] for v in ventures}

    resolved: list[tuple[object, dict]] = []
    to_call: list[tuple[object, str]] = []
    for iv in targets:
        key = event_key(getattr(iv, "calendar_id", None) or "", iv.event_id or "", iv.title)
        cached = _cache_get(conn, key)
        # A cached row with `attendance IS NULL` predates migration 013 (or
        # the migration is not applied yet) — it is a cache MISS for the
        # commitment-check half of the classification, so an existing label
        # upgrades to the three-way answer instead of staying stuck on the
        # old boolean forever. Only matters for events that NEED a
        # commitment check; a plain venture-only intent's cache is still a
        # hit even with `attendance IS NULL`, since attendance never applied
        # to it in the first place.
        stale_attendance = (
            getattr(iv, "needs_commitment_check", False)
            and cached is not None
            and cached.get("attendance") is None
        )
        if cached is not None and not stale_attendance:
            resolved.append((iv, cached))
        else:
            to_call.append((iv, key))

    if to_call:
        try:
            results = _call([iv for iv, _ in to_call], ventures)
        except Exception as exc:  # noqa: BLE001 — the day stands without the classifier
            logger.warning(
                "intents: classifier unavailable (%s) — leaving %d intent(s) unclassified",
                exc, len(to_call),
            )
            from ..store import observations as obs

            obs.record_llm_failure("analyst", exc, context="intent classification")
            results = [None] * len(to_call)
        model = settings.iblu_check_model
        for (iv, key), result in zip(to_call, results):
            if result is None:
                continue
            _cache_put(conn, key, iv.title, result, model)
            resolved.append((iv, result))

    for iv, result in resolved:
        if iv.venture is None and result.get("confidence") == "high":
            venture = result.get("venture")
            if venture is not None and venture in valid_codes:
                iv.venture = venture

        if not getattr(iv, "needs_commitment_check", False):
            continue
        # A locked context (e.g. > CONTEXT_MAX_DURATION) is never undone by a
        # classification — see `Interval.is_context_locked`.
        if getattr(iv, "is_context_locked", False):
            continue
        # `INTENT_MAYBE_TITLES` already decided this one — never let a cached
        # or freshly-returned model opinion second-guess the override.
        if getattr(iv, "attendance", None) == "maybe":
            continue

        attendance = result.get("attendance")
        attendance_confidence = result.get("attendance_confidence")
        if attendance == "maybe":
            # Already the cautious answer — applied at any confidence, see
            # the module docstring.
            iv.attendance = "maybe"
            iv.is_context = True
        elif attendance_confidence == "high" and attendance == "his":
            iv.attendance = "his"
            iv.is_context = False
        elif attendance_confidence == "high" and attendance == "not_his":
            iv.attendance = "not_his"
            iv.is_context = True
        # else: low-confidence "his"/"not_his" — leave the interval exactly
        # as `load_intents` made it (context, `attendance` unset). "When in
        # doubt, false" (module docstring / SYSTEM prompt).

    return intents
