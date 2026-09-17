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
  * **is_ignas_commitment / commitment_confidence** — asked ONLY for events
    from a calendar that is not exclusively his own (`Interval.
    needs_commitment_check`, set by `load_intents` for every `INTENT_
    CALENDARS` extra, never for a workspace primary). A shared family
    calendar holds his wife's plans and his children's just as easily as his
    own, and it holds plain WHEREABOUTS markers ("Ignas LT", "Ignas Zagreb
    10-14") that are not activities at all — a reminder of where someone is,
    not a thing Ignas attends. `is_ignas_commitment` means "an activity Ignas
    takes part in", never "an event that mentions Ignas". Only a HIGH
    confidence "yes" turns an event from context into something that can
    create a block or cause `displaced` — see `blocks.build`'s `is_context`
    filter. This distinction matters more now that a family commitment's
    silence can be read as presence (`blocks._apply_family_inference`): a
    whereabouts marker wrongly trusted as a commitment would turn a quiet
    afternoon into invented family time.

Same discipline as `judge.py` throughout: never raise out of this module — a
bad or unavailable classifier leaves every intent exactly as `load_intents`
found it — and only a `high` confidence classification is ever applied.
Results are cached in `intent_labels` (migration 012) keyed by a hash of the
calendar id, event id and title, so the same event classifies the same way on
every rebuild; the analyst runs twice a day, and a label that flaps between
runs is worse than no label.
"""

from __future__ import annotations

import hashlib
import json
import logging

from ..config import settings

logger = logging.getLogger("iblu_keeper.analyst.intents")

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

  - is_ignas_commitment: true ONLY when the event is clearly an ACTIVITY
    Ignas himself takes part in — a football match he attends, a parents'
    meeting, a dinner. It means "an activity Ignas takes part in", never "an
    event that mentions Ignas". A WHEREABOUTS marker is context, not a
    commitment, even when it names him: "Ignas LT", "Ignas Zagreb 10-14",
    "Ignas in Warsaw" say where he is, not that this is a scheduled
    activity. Anything about where his wife or children are, or an event
    that is clearly theirs ("Greta nicoj", "Futbolas" for a child's football
    practice), is also not his commitment even if he created the calendar
    entry. When in doubt, false — the default must be conservative, because
    treating a whereabouts note as a commitment can make a quiet afternoon
    look like confirmed family time.
  - commitment_confidence: "high" only when you are quite sure either way.

For events NOT marked [COMMITMENT CHECK], omit is_ignas_commitment and
commitment_confidence entirely — they do not apply to those events.

Measure nothing here against an ideal or a goal; you are labelling one batch
of calendar events, not judging performance.

Return only JSON: {"intents": [{"i": <index>, "venture": <code|null>,
"is_work": <bool>, "confidence": "high"|"low", "is_ignas_commitment": <bool>,
"commitment_confidence": "high"|"low"}]}
Only include is_ignas_commitment/commitment_confidence for a [COMMITMENT
CHECK] event. Include every index you were given."""


def event_key(calendar_id: str, event_id: str, title: str) -> str:
    """Stable cache key. A renamed event is a DIFFERENT key on purpose — a
    retitled event should be re-classified, not silently keep a stale label
    that no longer matches what it now says.
    """
    raw = f"{calendar_id or ''}|{event_id or ''}|{title or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(conn, key: str) -> dict | None:
    """Never raises — a missing `intent_labels` table (migration not yet
    applied) degrades to "no cache", never a broken reconstruct.
    """
    if conn is None:
        return None
    try:
        row = conn.execute(
            """
            SELECT venture, is_work, confidence, is_ignas_commitment, commitment_confidence
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
    """Best-effort write. Same degrade-gracefully rule as `_cache_get`."""
    if conn is None:
        return
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
                result.get("confidence"), result.get("is_ignas_commitment"),
                result.get("commitment_confidence"), model,
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
        commitment_confidence = item.get("commitment_confidence")
        is_ignas_commitment = item.get("is_ignas_commitment")
        out[i] = {
            "venture": venture,
            "is_work": bool(is_work) if isinstance(is_work, bool) else None,
            "confidence": confidence,
            "is_ignas_commitment": (
                bool(is_ignas_commitment) if isinstance(is_ignas_commitment, bool) else None
            ),
            "commitment_confidence": (
                commitment_confidence if commitment_confidence in ("high", "low") else None
            ),
        }
    return out


def classify_missing(conn, intents: list, ventures: list[dict]) -> list:
    """Classify every intent that still needs a venture or a commitment check.

    Mutates and returns `intents`. Never raises: any failure — no API key, a
    network error, a malformed response — leaves every intent exactly as
    `load_intents` produced it.
    """
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
        if cached is not None:
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
        if result.get("commitment_confidence") == "high":
            iv.is_context = not bool(result.get("is_ignas_commitment"))

    return intents
