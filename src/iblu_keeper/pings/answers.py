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
from .tokens import InvalidToken, read_token

logger = logging.getLogger("iblu_keeper.pings.answers")

REPLY_LOOKBACK = timedelta(hours=48)


class UnknownPing(Exception):
    """Valid signature, but the ping or question no longer exists."""


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


def record_tap(conn: psycopg.Connection, token: str) -> dict:
    """Verify a tap token and write the answer. Returns what to show the user."""
    data = read_token(token, settings.ping_signing_secret)
    ping_id, qid, key = data["ping_id"], data["qid"], data["key"]

    ping = conn.execute(
        "SELECT id, kind, local_date, covers_from, covers_to, questions "
        "FROM pings WHERE id = %s",
        (ping_id,),
    ).fetchone()
    if ping is None:
        raise UnknownPing(f"ping {ping_id} no longer exists")

    question, option = _find_option(ping["questions"], qid, key)
    payload = option.get("payload") or {}
    source_ref = f"ping:{ping_id}:{qid}"

    # Show the date on both ends when the window crosses midnight, so a stored
    # line like "11:48–11:48" can never be mistaken for a zero-length window.
    if ping["covers_from"].date() == ping["covers_to"].date():
        span = f"{ping['covers_from']:%H:%M}–{ping['covers_to']:%H:%M}"
    else:
        span = f"{ping['covers_from']:%d %b %H:%M}–{ping['covers_to']:%d %b %H:%M}"
    content = f"{ping['local_date']} {span} · {question.get('text')} → {option.get('label')}"

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
            ["ping", ping["kind"]],
            payload.get("venture"),
            payload.get("work_type"),
            payload.get("project"),
            source_ref,
            ping["covers_to"],
            Jsonb({
                "ping_id": ping_id,
                "qid": qid,
                "choice_key": key,
                "payload": payload,
                "answered_via": "tap",
                "covers_from": ping["covers_from"].isoformat(),
                "covers_to": ping["covers_to"].isoformat(),
            }),
        ),
    ).fetchone()["id"]

    # Re-answering the same question supersedes the previous answer (D9).
    superseded = conn.execute(
        "UPDATE context_entries SET superseded_by = %s "
        "WHERE source_ref = %s AND id <> %s AND superseded_by IS NULL "
        "RETURNING id",
        (new_id, source_ref, new_id),
    ).fetchall()

    conn.execute(
        "UPDATE pings SET status = 'answered' WHERE id = %s AND status <> 'answered'",
        (ping_id,),
    )

    logger.info(
        "answers: ping %s question %s answered %s%s",
        ping_id, qid, key,
        f" (superseded {len(superseded)})" if superseded else "",
    )
    return {
        "entry_id": str(new_id),
        "key": key,
        "label": option.get("label", ""),
        "question": question.get("text", ""),
        "superseded": len(superseded),
    }


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
                ["ping", ping["kind"], "reply"],
                message["name"],
                created,
                Jsonb({"ping_id": ping["id"], "answered_via": "reply"}),
            ),
        ).fetchone()
        if written is not None:
            inserted += 1

    if not dry:
        set_state(conn, "secretary_replies", watermark=newest, error=None)
    if inserted:
        logger.info("answers: recorded %d free-text repl(y/ies)", inserted)
    return inserted
