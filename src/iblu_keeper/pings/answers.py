"""Recording answers (plan §7.5–7.7).

Two ways in, one destination. A tap hits `/q/<token>` and writes a `work_log`
entry; a free-text reply in the ping's Chat thread writes the same kind of
entry with `answered_via='reply'`. Both carry the ping id and question id, and
the question text plus every option label is snapshotted on the `pings` row —
so "B" stays decodable long after the wording has been forgotten.

Corrections supersede, never delete (D9): tapping a different option on the
same question writes a new entry and marks the old one superseded.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from ..config import settings
from .compose import parse_body_mind_reply
from .tokens import InvalidToken, read_token

logger = logging.getLogger("iblu_keeper.pings.answers")

REPLY_LOOKBACK = timedelta(hours=48)

# Verdicts that mean "I don't know what this was" — the honest answer is a
# free-text reply, not a guess, so no block correction is written for them.
_NO_CORRECTION_VERDICTS = {"other", "way_off"}

_BLOCK_FIELDS = (
    "id, local_date, starts_at, ends_at, venture, work_type, project, "
    "attention, confidence, evidence, superseded_by"
)


class UnknownPing(Exception):
    """Valid signature, but the ping or question no longer exists."""


# --------------------------------------------------------------------------
# block corrections — split & gap (plan §3.3)
# --------------------------------------------------------------------------
#
# A tap never rewrites the block it is about. It writes a NEW `blocks` row
# with confidence='fact', source='ping', and points the block being corrected
# at it via `superseded_by` — the same supersede-not-overwrite pattern
# `analyst.blocks` itself uses for a rebuild, and the one this module already
# uses for `context_entries` below. A second tap on the same question walks
# to whichever block is live now (the first tap's correction) and supersedes
# THAT one, so the chain is never broken and nothing is ever double-corrected.


def _resolve_live_block(conn: psycopg.Connection, block_id: int) -> dict | None:
    """Follow `superseded_by` to the block that is live now."""
    row = conn.execute(
        f"SELECT {_BLOCK_FIELDS} FROM blocks WHERE id = %s", (block_id,)
    ).fetchone()
    seen: set[int] = set()
    while row is not None and row["superseded_by"] is not None and row["id"] not in seen:
        seen.add(row["id"])
        row = conn.execute(
            f"SELECT {_BLOCK_FIELDS} FROM blocks WHERE id = %s", (row["superseded_by"],)
        ).fetchone()
    return row


def _write_block_correction(
    conn: psycopg.Connection,
    block_id: int,
    *,
    venture: str | None,
    work_type: str | None,
    project: str | None,
    attention: str,
) -> int | None:
    """Write the fact block a tap confirms/corrects. Returns its id, or None
    if the block it was meant to correct no longer exists at all."""
    current = _resolve_live_block(conn, block_id)
    if current is None:
        logger.warning("answers: block %s not found — no correction written", block_id)
        return None

    new_id = conn.execute(
        """
        INSERT INTO blocks
            (local_date, starts_at, ends_at, venture, work_type, project,
             attention, confidence, evidence, reasoning, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'fact', %s, %s, 'ping')
        RETURNING id
        """,
        (
            current["local_date"], current["starts_at"], current["ends_at"],
            venture, work_type, project, attention,
            Jsonb(current["evidence"]), "confirmed by ping tap",
        ),
    ).fetchone()["id"]

    conn.execute(
        "UPDATE blocks SET superseded_by = %s WHERE id = %s",
        (new_id, current["id"]),
    )
    return new_id


def _backfill_from_signals(conn, payload: dict) -> tuple[str | None, str | None, str]:
    """Fill in venture / project from the signals the question was about.

    The composer often names a thread without classifying its venture. Rather
    than storing NULL — which makes the row useless for "where did sales time
    go?" — take the majority venture from the signals the option refers to.

    Returns `(venture, project, source)` where source is 'tapped' when the
    option itself carried the value and 'inferred' when it came from the
    signals. That distinction is recorded in meta: an inferred venture must
    never be mistaken later for something Ignas confirmed.
    """
    venture, project = payload.get("venture"), payload.get("project")
    if venture:
        return venture, project, "tapped"

    ids = payload.get("signal_ids") or []
    container = payload.get("container")
    if ids:
        rows = conn.execute(
            "SELECT venture, count(*) AS n FROM signals "
            "WHERE id = ANY(%s) AND venture IS NOT NULL "
            "GROUP BY venture ORDER BY n DESC LIMIT 1",
            (ids,),
        ).fetchone()
    elif container:
        rows = conn.execute(
            "SELECT venture, count(*) AS n FROM signals "
            "WHERE container = %s AND venture IS NOT NULL "
            "GROUP BY venture ORDER BY n DESC LIMIT 1",
            (container,),
        ).fetchone()
    else:
        rows = None

    if rows is None:
        return None, project, "unknown"
    return rows["venture"], project, "inferred"


def _find_option(questions, qid: str, key: str) -> tuple[dict, dict]:
    """Look the tapped option up in the snapshot stored on the ping row.

    Accepts both the documented list shape and a `{"questions": [...]}`
    wrapper: a tap link is already on a phone by the time anyone notices the
    shape is wrong, and it must still resolve.
    """
    if isinstance(questions, dict):
        questions = questions.get("questions", [])
    for question in questions:
        if not isinstance(question, dict):
            continue
        if question.get("qid") != qid:
            continue
        for option in question.get("options", []):
            if option.get("key") == key:
                return question, option
    raise UnknownPing(f"question {qid!r} option {key!r} is not on this ping")


# Advisory-lock namespace for tap writes. Arbitrary; only has to be unique
# within IBLU (the analyst's per-day rebuild lock uses a different one).
_TAP_LOCK_NAMESPACE = 8172


def _tap_lock_key(ping_id: int, qid: str) -> int:
    """A stable 32-bit key for one question of one ping.

    `hash()` is salted per process, so two concurrent /q requests in different
    workers would take different locks and serialise nothing.
    """
    import hashlib

    digest = hashlib.sha256(f"{ping_id}:{qid}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 2_147_483_647


def record_tap(conn: psycopg.Connection, token: str) -> dict:
    """Verify a tap token and write the answer. Returns what to show the user."""
    data = read_token(token, settings.ping_signing_secret)
    ping_id, qid, key = data["ping_id"], data["qid"], data["key"]

    # One writer at a time per (ping, question). `/q` opens its own connection
    # per request and the tap page's own error tells him to "try again", so a
    # genuine double-tap is ordinary. Without this, two requests can each run
    # their supersede-UPDATE before the other's INSERT is visible under READ
    # COMMITTED, leaving two rows with `superseded_by IS NULL` answering the
    # same question — the one thing the supersede chain exists to prevent.
    # Released with the transaction.
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (_TAP_LOCK_NAMESPACE, _tap_lock_key(ping_id, qid)),
    )

    ping = conn.execute(
        "SELECT id, kind, local_date, covers_from, covers_to, questions "
        "FROM pings WHERE id = %s",
        (ping_id,),
    ).fetchone()
    if ping is None:
        raise UnknownPing(f"ping {ping_id} no longer exists")

    question, option = _find_option(ping["questions"], qid, key)
    payload = option.get("payload") or {}
    kind = payload.get("kind")

    # gains (§4.1) is not a supersede chain: each option is its own gain, so
    # each gets its own source_ref — tapping "progressed" must never
    # supersede an earlier "learned" tap on the same card. Everything else
    # (including a retap of the SAME gain option, which is a correction of
    # that one gain) keeps the one-source_ref-per-question shape below.
    if kind == "gains":
        source_ref = f"ping:{ping_id}:gains:{payload.get('gain_kind') or key}"
    else:
        source_ref = f"ping:{ping_id}:{qid}"

    venture, project, venture_source = _backfill_from_signals(conn, payload)

    # Show the date on both ends when the window crosses midnight, so a stored
    # line like "11:48–11:48" can never be mistaken for a zero-length window.
    if ping["covers_from"].date() == ping["covers_to"].date():
        span = f"{ping['covers_from']:%H:%M}–{ping['covers_to']:%H:%M}"
    else:
        span = f"{ping['covers_from']:%d %b %H:%M}–{ping['covers_to']:%d %b %H:%M}"
    content = f"{ping['local_date']} {span} · {question.get('text')} → {option.get('label')}"

    tags = ["ping", ping["kind"]]
    meta_extra: dict = {}
    if kind == "gains":
        tags.append("gain")
        meta_extra["kind"] = payload.get("gain_kind")
        meta_extra["evidence_ids"] = payload.get("evidence_ids") or []
    elif kind == "body_mind":
        # Plan §4.2, hard rule: the numbers and nothing else. No inferred
        # mood word goes anywhere near this row.
        tags.append("health")
        meta_extra["body"] = payload.get("body")
        meta_extra["mind"] = payload.get("mind")
    elif kind in ("split", "gap"):
        tags.append(kind)

    new_id = conn.execute(
        """
        INSERT INTO context_entries
            (type, content, importance, tags, venture, work_type, project,
             source, source_ref, occurred_at, meta)
        VALUES ('work_log', %s, 3, %s, %s, %s, %s, 'ping', %s, %s, %s)
        RETURNING id
        """,
        (
            content,
            tags,
            venture,
            payload.get("work_type"),
            project,
            source_ref,
            ping["covers_to"],
            Jsonb({
                "ping_id": ping_id,
                "qid": qid,
                "choice_key": key,
                "payload": payload,
                "answered_via": "tap",
                "venture_source": venture_source,
                "covers_from": ping["covers_from"].isoformat(),
                "covers_to": ping["covers_to"].isoformat(),
                **meta_extra,
            }),
        ),
    ).fetchone()["id"]

    # Re-answering the same question supersedes the previous answer (D9).
    # For gains this only ever matches a retap of the SAME option, because
    # source_ref is keyed by gain_kind above — different gains never collide.
    superseded = conn.execute(
        "UPDATE context_entries SET superseded_by = %s "
        "WHERE source_ref = %s AND id <> %s AND superseded_by IS NULL "
        "RETURNING id",
        (new_id, source_ref, new_id),
    ).fetchall()

    # split / gap (§3.3): the tap also confirms or corrects a `blocks` row —
    # a NEW 'fact' row, never an UPDATE of the one it corrects. "Other" and
    # "way off" mean he doesn't know either, so nothing is written; that is
    # what the free-text reply is for.
    block_id = None
    if kind in ("split", "gap") and payload.get("block_id") is not None \
            and payload.get("verdict") not in _NO_CORRECTION_VERDICTS:
        block_id = _write_block_correction(
            conn, int(payload["block_id"]),
            venture=payload.get("venture"),
            work_type=payload.get("work_type"),
            project=payload.get("project"),
            attention=payload.get("attention") or "present",
        )

    conn.execute(
        "UPDATE pings SET status = 'answered' WHERE id = %s AND status <> 'answered'",
        (ping_id,),
    )

    logger.info(
        "answers: ping %s question %s answered %s%s%s",
        ping_id, qid, key,
        f" (superseded {len(superseded)})" if superseded else "",
        f" (block {block_id})" if block_id else "",
    )
    return {
        "entry_id": str(new_id),
        "key": key,
        "label": option.get("label", ""),
        "question": question.get("text", ""),
        "superseded": len(superseded),
        "block_id": block_id,
    }


# Which card a "→ reply" escape belongs to, expressed as the tag its answer
# should carry.
REPLY_TAG_BY_QID = {
    "body_mind": "health",
    "gains": "gain",
    "sink": "attention",
    "displaced": "attention",
    "split": "attention",
    "work_type": "attention",
    "gap": "attention",
}


def _awaiting_reply_qid(conn, ping_id: int) -> str | None:
    """Which question he most recently tapped "Other → reply" on.

    This is the whole answer to "what is this reply about". Every card's escape
    hatch records a tap with `verdict='other'` before he starts typing, so the
    newest one names the question he is answering.
    """
    row = conn.execute(
        """
        SELECT meta ->> 'qid' AS qid
          FROM context_entries
         WHERE source = 'ping' AND meta ->> 'ping_id' = %s
           AND meta -> 'payload' ->> 'verdict' = 'other'
         ORDER BY created_at DESC
         LIMIT 1
        """,
        (str(ping_id),),
    ).fetchone()
    return row["qid"] if row else None


def _classify_reply(conn, ping: dict, text: str) -> tuple[list[str], dict, bool]:
    """What a free-text reply in a ping's thread means.

    Returns `(extra_tags, meta_extra, overrides_body_mind)`.

    The first version assumed every evening reply that was not literally
    "body 4 mind 2" was a gain. On 2026-09-15 Ignas tapped "Other → reply" on
    the body/mind card and wrote "ok body since I've slept for 8 hours. But
    mentally I feel I'm just grinding through" — an answer about his health,
    filed as something that moved today. Answering a question in words is the
    obvious thing to do; the system has to know which question.

    So the reply is attributed to whichever card he last tapped the escape on.
    An explicit "body N mind N" still wins outright, whatever he tapped: it is
    unambiguous, and it is the only path that sets the numbers. Prose is kept
    verbatim and left unscored — reading a mood out of his sentences is exactly
    what §4.2 forbids, and the card promises Iblu records the numbers rather
    than interpreting them.
    """
    parsed = parse_body_mind_reply(text)
    if parsed is not None:
        body, mind = parsed
        return ["health", "reply"], {"body": body, "mind": mind}, True

    qid = _awaiting_reply_qid(conn, ping["id"])
    if qid:
        tag = REPLY_TAG_BY_QID.get(qid, "reply")
        meta = {"answers_qid": qid}
        if qid == "body_mind":
            # His own words, no numbers derived from them.
            meta |= {"body": None, "mind": None, "scored": False}
        return [tag, "reply"], meta, False

    if ping["kind"] in ("evening", "test"):
        # No escape tapped: an unprompted note in the evening thread, which
        # plan §4.1 reads as a gain ("replies in the card's thread are gain
        # entries too").
        return ["gain", "reply"], {"answers_qid": None}, False
    return ["reply"], {}, False


def read_thread_replies(conn: psycopg.Connection, *, dry: bool = False) -> int:
    """Ingest free-text replies posted in a ping's Chat thread (plan §7.7)."""
    if not settings.secretary_space:
        logger.debug("answers: SECRETARY_SPACE not set, skipping reply read")
        return 0

    from ..tools.chat import get_backend
    from ..collectors import get_watermark, set_state

    recent = conn.execute(
        "SELECT id, kind, chat_thread_ref FROM pings "
        "WHERE status IN ('sent','answered') AND sent_at >= %s "
        "AND chat_thread_ref IS NOT NULL",
        (datetime.now(timezone.utc) - REPLY_LOOKBACK,),
    ).fetchall()
    if not recent:
        return 0
    by_thread = {row["chat_thread_ref"]: row for row in recent}

    watermark = get_watermark(conn, "secretary_replies") or (
        datetime.now(timezone.utc) - REPLY_LOOKBACK
    )
    backend = get_backend()
    svc = backend._service()
    self_name = f"users/{backend._ensure_self_id()}"

    stamp = watermark.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    listing = (
        svc.spaces()
        .messages()
        .list(
            parent=settings.secretary_space,
            filter=f'createTime > "{stamp}"',
            pageSize=100,
        )
        .execute()
    )

    inserted = 0
    newest = watermark
    for message in listing.get("messages", []) or []:
        created = datetime.fromisoformat(
            message["createTime"].replace("Z", "+00:00")
        )
        newest = max(newest, created)

        thread_ref = (message.get("thread") or {}).get("name")
        ping = by_thread.get(thread_ref)
        if ping is None:
            continue
        # Only Ignas's own replies are answers; the app's own card is not.
        if (message.get("sender") or {}).get("name") != self_name:
            continue
        text = (message.get("text") or "").strip()
        if not text:
            continue

        if dry:
            logger.info("answers [dry]: would record reply to ping %s", ping["id"])
            inserted += 1
            continue

        extra_tags, meta_extra, overrides_body_mind = _classify_reply(conn, ping, text)

        # RETURNING so the count reflects rows actually written, not messages
        # seen: the watermark overlap re-reads the same message every tick.
        written = conn.execute(
            """
            INSERT INTO context_entries
                (type, content, importance, tags, source, source_ref, occurred_at, meta)
            VALUES ('work_log', %s, 3, %s, 'chat_reply', %s, %s, %s)
            -- ce_chat_reply_unique (migration 002)
            ON CONFLICT (source_ref) WHERE source = 'chat_reply' DO NOTHING
            RETURNING id
            """,
            (
                text,
                ["ping", ping["kind"], *extra_tags],
                message["name"],
                created,
                Jsonb({"ping_id": ping["id"], "answered_via": "reply", **meta_extra}),
            ),
        ).fetchone()
        if written is not None:
            inserted += 1
            if overrides_body_mind:
                # The card is "1 card, 1 tap" (plan §4.2) — a reply overriding
                # it supersedes whatever answered body_mind before, tap or
                # reply, the same supersede-on-retap pattern as everywhere
                # else (D9), scoped by the qid's own source_ref rather than
                # this row's (which is the message id, for de-duplication).
                conn.execute(
                    "UPDATE context_entries SET superseded_by = %s "
                    "WHERE source_ref = %s AND id <> %s AND superseded_by IS NULL",
                    (written["id"], f"ping:{ping['id']}:body_mind", written["id"]),
                )

    if not dry:
        set_state(conn, "secretary_replies", watermark=newest, error=None)
    if inserted:
        logger.info("answers: recorded %d free-text repl(y/ies)", inserted)
    return inserted
