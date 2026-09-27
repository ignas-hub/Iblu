"""The evening day card: "here is what I think you did today — fix it in one
tap or one sentence."

    python -m iblu_keeper.jobs.daycard [--dry] [--date YYYY-MM-DD]

Ignas has never once confirmed a block: every ping asks an abstract question
("was that yours to do?") and the Secretary calendar is something he has to
open and read himself to check. So nothing in the system is `fact`, the
shadow calendar cannot be measured for accuracy, and every analyst rebuild
re-guesses the day from scratch. This is the one card that lets him bless a
day whole, or fix exactly the part that's wrong, without opening anything.

Two ways to answer, same as a ping:

  * **A tap** ("All correct" / "Something's wrong") — `record_daycard_tap`,
    called from `pings.answers.record_tap` via the same `/q/<token>` route.
  * **A reply** in the card's Chat thread — read by
    `pings.answers.read_thread_replies`, parsed here into structured
    corrections and applied.

Run by `iblu-daycard.timer`, weekday evenings, after the analyst's own 20:15
run so the card describes the fullest reconstruction of the day available.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError

from .. import db
from ..config import settings

logger = logging.getLogger("iblu_keeper.jobs.daycard")

TIMEOUT = 20

# The evening card is a summary, not a ledger — ten lines is what fits a
# glance on a phone. See `_cap_lines` for how an unusually fragmented day gets
# folded down to this without inventing anything.
MAX_LINES = 10

# What a venture is CALLED on the card — the same words a ping card uses
# (`pings.runner.VENTURE_WORDS`), with one deliberate difference: that dict
# renders `family` as "the family" for gains phrasing ("time with the
# family"); a plain venture label on this card just says "family", matching
# how the other three are named ("Blank Label", "Deadlift", "the house").
VENTURE_WORDS: dict[str, str] = {
    "blt": "Blank Label",
    "choco": "Choco",
    "deadlift": "Deadlift",
    "gostellar": "GoStellar",
    "jakusi": "the house",
    "family": "family",
    "personal": "your own tools",
}


def _venture_word(code: str | None) -> str | None:
    if not code:
        return None
    return VENTURE_WORDS.get(code, code)


def _project_word(code: str | None) -> str | None:
    """A project's real name, reusing the same lookup the ping cards use."""
    if not code:
        return None
    from ..pings.runner import _project_name

    return _project_name(code)


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.iblu_timezone)


def _local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


# --------------------------------------------------------------------------
# rendering — blocks -> at most MAX_LINES lettered lines
# --------------------------------------------------------------------------


def _mark_for(block: dict) -> str | None:
    """`?` displaced-unknown / `~` family-assumed / `⟂` displaced / `None`.

    Checked in this order because a family-inferred span is stored with
    `attention='present'` (see `analyst.blocks._apply_family_inference`) — the
    only way to tell it apart from an ordinary present block is its
    reasoning line, which always says "assumed" (module docstring,
    `analyst/blocks.py`). `ambiguous` is checked first because it can never
    be family-inferred (that path converts a block OUT of `ambiguous`).
    """
    if block.get("attention") == "ambiguous":
        return "?"
    if "assumed" in (block.get("reasoning") or "").lower():
        return "~"
    if block.get("attention") == "displaced":
        return "⟂"
    return None


def _label_for(block: dict, mark: str | None) -> str:
    venture = _venture_word(block.get("venture"))
    project = _project_word(block.get("project"))
    title = block.get("intent_title")

    if mark == "?":
        return f'"{title}" — nothing recorded' if title else "nothing recorded"
    if mark == "~":
        who = venture or "family"
        return f'assumed you went to "{title}"' if title else f"assumed {who}"
    if mark == "⟂":
        did = f"{venture} · {project}" if (venture and project) else (venture or "unlabeled work")
        return f'worked on {did} — calendar said "{title}"' if title else f"worked on {did} instead"

    if venture and project:
        return f"{venture} · {project}"
    if venture:
        return venture
    return "unlabeled work"


def _entry(block: dict) -> dict:
    mark = _mark_for(block)
    return {
        "starts_at": block["starts_at"],
        "ends_at": block["ends_at"],
        "mark": mark,
        "text": _label_for(block, mark),
        "venture": block.get("venture"),
        "project": block.get("project"),
        "title": block.get("intent_title"),
        "block_ids": [block["id"]],
    }


# Two stretches of the same work separated by less than this are one stretch
# on the card. Shorter than MIN_UNTRACKED (30 min), so nothing that would have
# been offered as an unknown gap is ever swallowed.
MERGE_ACROSS_GAP = timedelta(minutes=30)


def _merge_adjacent_entries(entries: list[dict]) -> list[dict]:
    """Neighbouring lines that say the exact same thing are one line."""
    out: list[dict] = []
    for e in entries:
        prev = out[-1] if out else None
        # Also merge across a SHORT gap when the label is identical. A real day
        # came out as ten lines: "09:15-10:00 Blank Label", "10:15-12:30 Blank
        # Label", "12:45-13:00 Blank Label"… — the 15-minute holes between
        # clusters are an artefact of how evidence lands, not something he can
        # act on, and they made the card unreadable. The gaps are shown, so
        # nothing is claimed that was not recorded.
        gap = (e["starts_at"] - prev["ends_at"]) if prev else None
        same = (
            prev
            and prev["text"] == e["text"]
            and prev["mark"] == e["mark"]
            and gap is not None
            and timedelta() <= gap < MERGE_ACROSS_GAP
        )
        if same:
            if gap:
                prev["gaps"] = prev.get("gaps", 0) + 1
            prev["ends_at"] = e["ends_at"]
            prev["block_ids"] += e["block_ids"]
        else:
            out.append(dict(e))
    return out


# Which mark matters most to keep visible when two lines are folded into one
# — an unresolved "?" is the most worth his attention, then a mismatch
# against the plan, then an assumption; an ordinary present line carries no
# mark at all and yields to any of the other three.
_MARK_PRIORITY = {"?": 3, "⟂": 2, "~": 1, None: 0}


def _cap_lines(entries: list[dict], limit: int = MAX_LINES) -> list[dict]:
    """Fold the day down to at most `limit` lines without inventing anything.

    Repeatedly merges the adjacent PAIR with the smallest combined duration —
    the least consequential join available — keeping whichever side's label
    matters more (see `_MARK_PRIORITY`), or whichever side is longer when
    that's a tie. Nothing here changes what a line *means*; it only changes
    how many lines two adjacent, less-important stretches take up.
    """
    entries = list(entries)
    while len(entries) > limit and len(entries) > 1:
        best_i = 0
        best_dur = None
        for i in range(len(entries) - 1):
            dur = (entries[i]["ends_at"] - entries[i]["starts_at"]) + (
                entries[i + 1]["ends_at"] - entries[i + 1]["starts_at"]
            )
            if best_dur is None or dur < best_dur:
                best_dur, best_i = dur, i

        a, b = entries[best_i], entries[best_i + 1]
        pa, pb = _MARK_PRIORITY.get(a["mark"], 0), _MARK_PRIORITY.get(b["mark"], 0)
        if pa != pb:
            winner = a if pa > pb else b
        else:
            winner = a if (a["ends_at"] - a["starts_at"]) >= (b["ends_at"] - b["starts_at"]) else b

        merged = dict(winner)
        merged["starts_at"] = min(a["starts_at"], b["starts_at"])
        merged["ends_at"] = max(a["ends_at"], b["ends_at"])
        merged["block_ids"] = a["block_ids"] + b["block_ids"]
        entries[best_i : best_i + 2] = [merged]
    return entries


def render_lines(entries: list[dict], tz: ZoneInfo) -> list[dict]:
    """Attach a letter and the final display string to each entry, in order."""
    out = []
    for i, e in enumerate(entries):
        letter = chr(65 + i)
        span = f"{_local(e['starts_at'], tz)}–{_local(e['ends_at'], tz)}"
        mark_prefix = f"{e['mark']}  " if e["mark"] else ""
        line = f"{letter}  {span}  {mark_prefix}{e['text']}"
        out.append({**e, "letter": letter, "line": line})
    return out


def build_day_lines(conn, on: date) -> list[dict]:
    """The day's live blocks, rendered and lettered — at most `MAX_LINES`."""
    from ..analyst.blocks import live_blocks

    blocks = sorted(live_blocks(conn, on), key=lambda b: (b["starts_at"], b["ends_at"]))
    entries = [_entry(b) for b in blocks]
    entries = _merge_adjacent_entries(entries)
    entries = _cap_lines(entries)
    return render_lines(entries, _tz())


# --------------------------------------------------------------------------
# the reply-example line — drawn from HIS own day, never invented
# --------------------------------------------------------------------------


def _dominant_venture(lines: list[dict]) -> str | None:
    """The venture that most of the day's recorded minutes belong to."""
    from collections import Counter

    counts = Counter(
        l["venture"] for l in lines if l.get("venture") and l["venture"] != "family"
    )
    return counts.most_common(1)[0][0] if counts else None


def _correction_example(lines: list[dict], *, avoid: str | None = None) -> str | None:
    """`"C was Deadlift · Machina"`-style example, preferring an uncertain line."""
    for prefer_uncertain in (True, False):
        for l in lines:
            if prefer_uncertain and l["mark"] not in ("?", "⟂"):
                continue
            # An unknown line is the one most worth correcting, and it has no
            # venture of its own — so suggest the day's dominant one. The old
            # rule skipped unknown lines entirely and produced "B was Blank
            # Label" for a line already labelled Blank Label: an example that
            # teaches the grammar by asking him to change nothing.
            venture = l.get("venture") or _dominant_venture(lines)
            if not venture:
                continue
            label = _venture_word(venture)
            project = _project_word(l.get("project")) if l.get("venture") else None
            if project:
                label = f"{label} · {project}"
            text = f'"{l["letter"]} was {label}"'
            if text != avoid:
                return text
    return None


def _skip_example(lines: list[dict]) -> str | None:
    """`"I skipped Futbolas"`-style example, from an assumed family line."""
    for l in lines:
        if l["mark"] == "~" and l.get("title"):
            return f'"I skipped {l["title"]}"'
    return None


def _range_example(lines: list[dict], *, avoid: str | None = None) -> str | None:
    """`"13:15-16:00 was Deadlift"` — correcting by time rather than by letter."""
    venture = _dominant_venture(lines)
    for l in lines:
        if l["mark"] != "?":
            continue
        span = l["line"].split()[1].replace("–", "-")
        other = "Deadlift" if venture != "deadlift" else "Blank Label"
        text = f'"{span} was {other}"'
        if text != avoid:
            return text
    return None


def reply_example(lines: list[dict]) -> str:
    first = _correction_example(lines)
    # The second example teaches a DIFFERENT grammar — skipping a family event,
    # or naming a time range directly. Two examples of the same shape taught
    # him nothing he did not already have from the first.
    second = _skip_example(lines) or _range_example(lines, avoid=first)
    examples = [e for e in (first, second) if e]
    if not examples:
        return '"C was Deadlift · Machina"'
    return " or ".join(examples)


def render_card_text(lines: list[dict]) -> str:
    """What `--dry` prints, and the plain-text body of the Chat card."""
    body = "\n".join(l["line"] for l in lines)
    return f"{body}\n\nReply like: {reply_example(lines)}"


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------


def build_card(day_card_id: int, lines: list[dict], self_id: str, base_url: str, secret: str) -> dict:
    from ..pings.tokens import make_token, tap_url

    token_all = make_token(day_card_id, "daycard", "A", secret)
    token_wrong = make_token(day_card_id, "daycard", "B", secret)
    mention = f"<users/{self_id}> " if self_id else ""
    body = "\n".join(l["line"] for l in lines)

    return {
        "text": f"{mention}Evening day card — here's what I think you did today.",
        "cardsV2": [{
            "cardId": f"daycard-{day_card_id}",
            "card": {
                "sections": [
                    {"widgets": [{"textParagraph": {"text": body}}]},
                    {"widgets": [{"buttonList": {"buttons": [
                        {"text": "All correct",
                         "onClick": {"openLink": {"url": tap_url(base_url, token_all)}}},
                        {"text": "Something's wrong → reply",
                         "onClick": {"openLink": {"url": tap_url(base_url, token_wrong)}}},
                    ]}}]},
                    {"widgets": [{"textParagraph": {
                        "text": f"Reply like: {reply_example(lines)}"
                    }}]},
                ]
            },
        }],
    }


def send(day_card_id: int, lines: list[dict], self_id: str) -> dict:
    """POST the card. Returns `{message_ref, thread_ref}`; raises on failure."""
    import requests

    from ..pings.deliver import DeliveryError, _scrub, preflight_tap_route

    if not settings.secretary_webhook_url:
        raise DeliveryError("SECRETARY_WEBHOOK_URL is not set")
    if not settings.ping_signing_secret:
        raise DeliveryError("PING_SIGNING_SECRET is not set — tap links would be unsignable")
    if not settings.mcp_public_base_url:
        raise DeliveryError("MCP_PUBLIC_BASE_URL is not set — tap links would be relative")

    preflight_tap_route()

    body = build_card(day_card_id, lines, self_id, settings.mcp_public_base_url, settings.ping_signing_secret)

    url = settings.secretary_webhook_url
    separator = "&" if "?" in url else "?"
    url = (
        f"{url}{separator}threadKey=daycard-{day_card_id}"
        "&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    )

    try:
        response = requests.post(url, json=body, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise DeliveryError(f"webhook unreachable: {_scrub(exc)}") from exc
    if response.status_code >= 400:
        raise DeliveryError(f"webhook returned {response.status_code}: {response.text[:300]}")

    data = response.json()
    refs = {
        "message_ref": data.get("name"),
        "thread_ref": (data.get("thread") or {}).get("name"),
    }
    logger.info("daycard: %s sent as %s", day_card_id, refs["message_ref"])
    return refs


def _snapshot(lines: list[dict]) -> list[dict]:
    return [
        {
            "letter": l["letter"],
            "block_ids": l["block_ids"],
            "starts_at": l["starts_at"].isoformat(),
            "ends_at": l["ends_at"].isoformat(),
            "venture": l.get("venture"),
            "project": l.get("project"),
            "mark": l.get("mark"),
            "title": l.get("title"),
        }
        for l in lines
    ]


def _upsert_day_card(conn, on: date, lines: list[dict]) -> int:
    """One row per day (`day_cards_one_per_day`), same discipline as `pings`:
    the snapshot is only replaced if the previous attempt never actually
    reached Chat — otherwise a retry would repoint the tap tokens already on
    a card sitting on his phone."""
    return conn.execute(
        """
        INSERT INTO day_cards (local_date, lines, status)
        VALUES (%s, %s, 'sent')
        ON CONFLICT (local_date) DO UPDATE SET
            lines  = CASE WHEN day_cards.chat_message_ref IS NULL
                          THEN EXCLUDED.lines ELSE day_cards.lines END,
            status = CASE WHEN day_cards.chat_message_ref IS NULL
                          THEN 'sent' ELSE day_cards.status END
        RETURNING id
        """,
        (on, Jsonb(_snapshot(lines))),
    ).fetchone()["id"]


def run(on: date | None = None, dry: bool = False) -> int:
    if settings.use_mock:
        logger.error("daycard: refusing to run in mock mode (DRY_RUN=true)")
        return 1
    if not db.is_configured():
        logger.error("daycard: no DATABASE_URL")
        return 1

    day = on or datetime.now(_tz()).date()

    with db.get_conn() as conn:
        lines = build_day_lines(conn, day)
        if not lines:
            logger.info("daycard %s: no blocks recorded — sending nothing", day)
            return 0

        if dry:
            print(render_card_text(lines))
            return 0

        day_card_id = _upsert_day_card(conn, day, lines)

        from ..tools.chat import get_backend

        try:
            self_id = get_backend()._ensure_self_id()
        except Exception:  # noqa: BLE001 - the mention is nice-to-have
            self_id = ""

        try:
            refs = send(day_card_id, lines, self_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("daycard: delivery failed: %s", exc)
            conn.execute(
                "UPDATE day_cards SET meta = meta || %s WHERE id = %s",
                (Jsonb({"error": str(exc)[:500]}), day_card_id),
            )
            return 1

        conn.execute(
            "UPDATE day_cards SET chat_message_ref = %s, chat_thread_ref = %s WHERE id = %s",
            (refs["message_ref"], refs["thread_ref"], day_card_id),
        )

    logger.info("daycard %s: sent (%d lines)", day, len(lines))
    return 0


# --------------------------------------------------------------------------
# taps — "All correct" / "Something's wrong", called from
# `pings.answers.record_tap` (same `/q/<token>` route as every ping)
# --------------------------------------------------------------------------

# Advisory-lock namespace for day-card taps. Arbitrary; only has to be unique
# within IBLU (the ping tap lock and the analyst's per-day lock each use their
# own).
_LOCK_NAMESPACE = 8173


def confirm_all(conn, on: date) -> int:
    """"All correct": every non-confirmed live block of `on` becomes a new
    `source='human'`, `confidence='fact'` row with the same span and labels,
    superseding the analyst's guess (never UPDATE, never DELETE).

    Idempotent by construction: a block already confirmed (`source in
    ('ping','human')`) is filtered out by `_is_confirmed` before this ever
    sees it, so a second "All correct" tap finds nothing left to confirm and
    writes nothing — including no second confirmation entry (the caller only
    writes one when this returns non-zero).
    """
    from ..analyst.blocks import _is_confirmed, live_blocks

    blocks = [b for b in live_blocks(conn, on) if not _is_confirmed(b)]
    if not blocks:
        return 0

    for b in blocks:
        new_id = conn.execute(
            """
            INSERT INTO blocks
                (local_date, starts_at, ends_at, venture, work_type, project,
                 attention, confidence, evidence, reasoning, intent_event_id,
                 intent_title, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'fact', %s, %s, %s, %s, 'human')
            RETURNING id
            """,
            (
                on, b["starts_at"], b["ends_at"], b["venture"], b["work_type"],
                b["project"], b["attention"], Jsonb(b["evidence"] or []),
                b["reasoning"], b["intent_event_id"], b["intent_title"],
            ),
        ).fetchone()["id"]
        conn.execute("UPDATE blocks SET superseded_by = %s WHERE id = %s", (new_id, b["id"]))

    conn.execute(
        """
        INSERT INTO context_entries
            (type, content, importance, tags, source, source_ref, occurred_at, meta)
        VALUES ('work_log', %s, 3, %s, 'ping', %s, now(), %s)
        """,
        (
            f"{on} — confirmed the whole day correct ({len(blocks)} block(s))",
            ["ping", "daycard", "confirmed"],
            f"daycard:{on.isoformat()}:confirm",
            Jsonb({"date": on.isoformat(), "confirmed_blocks": len(blocks)}),
        ),
    )
    return len(blocks)


def _flag_wrong(conn, day_card_id: int, on: date) -> None:
    conn.execute(
        """
        INSERT INTO context_entries
            (type, content, importance, tags, source, source_ref, occurred_at, meta)
        VALUES ('work_log', %s, 3, %s, 'ping', %s, now(), %s)
        """,
        (
            f"{on} — flagged the day card as wrong, reply pending",
            ["ping", "daycard", "flagged"],
            f"daycard:{day_card_id}:flagged",
            Jsonb({"date": on.isoformat()}),
        ),
    )


def record_daycard_tap(conn, day_card_id: int, key: str) -> dict:
    """Handle a tap on the day card's own two options.

    Called from `pings.answers.record_tap` when the decoded token's qid is
    `daycard` — the day card reuses the exact same signed-token machinery and
    `/q/<token>` route as a ping, just against `day_cards` instead of
    `pings`. Raises `pings.answers.UnknownPing` for a day card that no longer
    exists or a key that isn't one of its two options, so `server.py`'s tap
    endpoint handles it exactly like a dead ping link (never a 500).
    """
    from ..pings.answers import UnknownPing

    # Same discipline as the ping tap lock (`answers._tap_lock_key`): one
    # writer at a time per day card, so two nearly-simultaneous taps can
    # never both see "nothing confirmed yet" and both write a confirmation.
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (_LOCK_NAMESPACE, int(day_card_id) % 2_147_483_647),
    )

    card = conn.execute(
        "SELECT id, local_date, status FROM day_cards WHERE id = %s", (day_card_id,),
    ).fetchone()
    if card is None:
        raise UnknownPing(f"day card {day_card_id} no longer exists")

    already_answered = card["status"] == "answered"

    if key == "A":
        confirmed = confirm_all(conn, card["local_date"])
        label = "All correct"
    elif key == "B":
        confirmed = 0
        _flag_wrong(conn, day_card_id, card["local_date"])
        label = "Something's wrong"
    else:
        raise UnknownPing(f"day card option {key!r} is not on this card")

    conn.execute(
        "UPDATE day_cards SET status = 'answered' WHERE id = %s AND status <> 'answered'",
        (day_card_id,),
    )

    return {
        "qid": "daycard", "key": key, "label": label,
        "superseded": already_answered, "confirmed": confirmed,
    }


# --------------------------------------------------------------------------
# replies — parsed by the model into structured corrections, applied here
# --------------------------------------------------------------------------


class DayCardCorrection(BaseModel):
    """One thing his reply said, resolved back to a card line if possible."""

    line: str | None = None
    starts_at: str | None = None   # "HH:MM", local time — only when no line letter fit
    ends_at: str | None = None
    venture: str | None = None
    work_type: str | None = None
    project: str | None = None
    attended: bool | None = None
    drop: bool = False


class DayCardCorrections(BaseModel):
    corrections: list[DayCardCorrection] = Field(default_factory=list)


DAYCARD_SYSTEM = """You read one reply Ignas sent under his evening day card \
— a lettered list of what IBLU thinks he did that day — and turn it into zero \
or more structured corrections.

A correction is one of:
- a line-letter correction: he named a letter from the card ("C was Deadlift \
Machina").
- a time-range correction: he named a span of time instead of a letter \
("12:00-13:00 was Blank Label").
- an attendance answer: he said whether he actually attended something the \
card marked as assumed ("I skipped Futbolas", "I did go to the school thing").
- a drop: he said a line is simply wrong, with no replacement ("ignore C", \
"B is not right").

Output STRICT JSON of this shape, nothing else:
{"corrections": [{"line": "C" or null, "starts_at": "HH:MM" or null, \
"ends_at": "HH:MM" or null, "venture": a code from the list below or null, \
"work_type": a code from the list below or null, "project": free text or \
null, "attended": true or false or null, "drop": true or false}]}

Only ever use a venture CODE from this exact list, never invent or guess one \
— leave venture null if you are not sure: {ventures}
Only ever use a work_type CODE from this exact list, never invent one: \
{work_types}
If his reply does not clearly match anything on the card, or you are not \
confident, return {{"corrections": []}} rather than guessing.
No markdown, no commentary — JSON only."""


def _card_text_for_prompt(lines: list[dict]) -> str:
    out = []
    for l in lines:
        bits = [l["letter"], f"{l['starts_at']}–{l['ends_at']}"]
        if l.get("mark"):
            bits.append(l["mark"])
        if l.get("venture"):
            bits.append(_venture_word(l["venture"]))
        if l.get("project"):
            bits.append(_project_word(l["project"]) or l["project"])
        if l.get("title"):
            bits.append(f'"{l["title"]}"')
        out.append(" · ".join(bits))
    return "\n".join(out)


def parse_daycard_reply(
    text: str, lines: list[dict], ventures: list[str], work_types: list[str],
) -> DayCardCorrections:
    """Ask the model to structure one reply. Raises on any problem — the
    caller (`_apply_reply`) treats every exception as "could not be applied"
    and records an observation rather than guessing.

    `settings.iblu_check_model` (not the cheaper ping composer's model): a
    mistake here silently becomes the history everything is measured
    against, the same reasoning HANDOFF §21 gives for the judge and the
    sense-check running on the better model.
    """
    import anthropic

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    system = DAYCARD_SYSTEM.format(
        ventures=", ".join(ventures), work_types=", ".join(work_types),
    )
    prompt = f"The card:\n{_card_text_for_prompt(lines)}\n\nHis reply:\n{text}"

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60.0)
    # NOTE: no `temperature` — sampling parameters were removed on Sonnet 5
    # and the call 400s. Determinism comes from low effort + a strict schema.
    response = client.messages.create(
        model=settings.iblu_check_model,
        max_tokens=800,
        system=system,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )

    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].removeprefix("json").strip()
    return DayCardCorrections.model_validate(json.loads(raw))


_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def _parse_local_hhmm(text: str | None, on: date, tz: ZoneInfo) -> datetime | None:
    m = _HHMM_RE.match(text or "")
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return datetime.combine(on, time(hour, minute), tzinfo=tz).astimezone(timezone.utc)


def _find_line_by_letter(lines: list[dict], letter: str | None) -> dict | None:
    if not letter:
        return None
    letter = letter.strip().upper()
    return next((l for l in lines if l.get("letter") == letter), None)


def _find_line_by_time(
    lines: list[dict], on: date, tz: ZoneInfo, starts_at: str | None,
    tolerance: timedelta = timedelta(minutes=10),
) -> dict | None:
    target = _parse_local_hhmm(starts_at, on, tz)
    if target is None:
        return None
    best, best_gap = None, None
    for l in lines:
        entry_start = datetime.fromisoformat(l["starts_at"])
        gap = abs(entry_start - target)
        if gap <= tolerance and (best_gap is None or gap < best_gap):
            best, best_gap = l, gap
    return best


def _resolve_correction_line(
    lines: list[dict], on: date, tz: ZoneInfo, correction: DayCardCorrection,
) -> dict | None:
    return (
        _find_line_by_letter(lines, correction.line)
        or _find_line_by_time(lines, on, tz, correction.starts_at)
    )


def _apply_one_correction(
    conn, on: date, lines: list[dict], correction: DayCardCorrection,
    known_ventures: set[str], known_work_types: set[str],
) -> tuple[bool, str | None]:
    """Apply one parsed correction. Returns `(applied, reason_if_not)`."""
    tz = _tz()
    line = _resolve_correction_line(lines, on, tz, correction)
    if line is None:
        return False, "could not resolve which line or span this reply refers to"

    if correction.venture and correction.venture not in known_ventures:
        # Never guess a venture that is not in `ventures` — refuse the whole
        # correction rather than write one with a code nothing else knows.
        return False, f"named an unknown venture {correction.venture!r}"

    work_type = correction.work_type if correction.work_type in known_work_types else None

    unknown = bool(correction.drop) or correction.attended is False

    from ..pings.answers import _resolve_live_block

    single = len(line.get("block_ids", [])) == 1
    override_start = _parse_local_hhmm(correction.starts_at, on, tz) if single else None
    override_end = _parse_local_hhmm(correction.ends_at, on, tz) if single else None
    use_override = bool(override_start and override_end and override_end > override_start)

    written = 0
    for block_id in line.get("block_ids", []):
        current = _resolve_live_block(conn, block_id)
        if current is None:
            continue

        if unknown:
            new_venture = new_work_type = new_project = None
            new_attention = "ambiguous"
            title = line.get("title") or current.get("intent_title")
            reasoning = (
                f'Ignas said he did not attend "{title}"' if title and correction.attended is False
                else "Ignas said he did not attend — left unknown" if correction.attended is False
                else "Ignas said this line was wrong — left unknown"
            )
        else:
            new_venture = correction.venture or current["venture"]
            new_work_type = work_type or current["work_type"]
            new_project = correction.project or current["project"]
            new_attention = "present"
            reasoning = "confirmed by reply to the day card"

        starts_at, ends_at = current["starts_at"], current["ends_at"]
        if use_override:
            starts_at, ends_at = override_start, override_end

        new_id = conn.execute(
            """
            INSERT INTO blocks
                (local_date, starts_at, ends_at, venture, work_type, project,
                 attention, confidence, evidence, reasoning, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'fact', %s, %s, 'human')
            RETURNING id
            """,
            (
                on, starts_at, ends_at, new_venture, new_work_type, new_project,
                new_attention, Jsonb(current["evidence"] or []), reasoning,
            ),
        ).fetchone()["id"]
        conn.execute("UPDATE blocks SET superseded_by = %s WHERE id = %s", (new_id, current["id"]))
        written += 1

    if written == 0:
        return False, "the block(s) this line pointed to no longer exist"
    return True, None


def _record_unparsed(on: date, text: str, reason: str) -> None:
    from ..store import observations as obs

    obs.record_safe(
        source="composer", kind="daycard_reply_unparsed", severity="info",
        summary="a day-card reply could not be applied with confidence",
        detail=f"{reason}\n\nreply: {text[:500]}",
        evidence={"date": on.isoformat()},
        fp=obs.fingerprint("daycard", "reply_unparsed", on.isoformat(), text[:80]),
    )


def apply_daycard_reply(
    conn, day_card: dict, text: str, ventures: list[str], work_types: list[str],
) -> dict:
    """Parse one free-text reply in a day-card thread and apply what it
    confidently means. Never raises: an LLM outage, a malformed response, or
    a reply that names nothing on the card all degrade to "apply nothing,
    record why" rather than guessing or crashing the reply reader.
    """
    on = day_card["local_date"]
    if isinstance(on, str):
        on = date.fromisoformat(on)
    lines = day_card["lines"]

    try:
        parsed = parse_daycard_reply(text, lines, ventures, work_types)
    except (ValidationError, json.JSONDecodeError, ValueError) as exc:
        _record_unparsed(on, text, f"the model's output did not parse: {exc}")
        return {"applied": 0, "rejected": 0, "parsed": False}
    except Exception as exc:  # noqa: BLE001 - an API outage is not fatal
        from ..store import observations as obs

        obs.record_llm_failure("daycard", exc, context="day card reply parsing")
        return {"applied": 0, "rejected": 0, "parsed": False}

    if not parsed.corrections:
        _record_unparsed(on, text, "the model matched nothing on the card")
        return {"applied": 0, "rejected": 0, "parsed": True}

    known_ventures = set(ventures)
    known_work_types = set(work_types)
    applied = rejected = 0
    for correction in parsed.corrections:
        ok, reason = _apply_one_correction(
            conn, on, lines, correction, known_ventures, known_work_types,
        )
        if ok:
            applied += 1
        else:
            rejected += 1
            _record_unparsed(on, text, reason or "could not be applied")

    return {"applied": applied, "rejected": rejected, "parsed": True}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m iblu_keeper.jobs.daycard",
        description="Send the evening day card: here's what I think you did today.",
    )
    parser.add_argument("--date", help="local date to build (default: today)")
    parser.add_argument("--dry", action="store_true", help="print the card, write and send nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    on = date.fromisoformat(args.date) if args.date else None
    try:
        return run(on=on, dry=args.dry)
    finally:
        db.close_pool()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
