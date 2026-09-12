"""Collector: mail I sent (plan §8.1).

What I *send* is the signal — it is evidence of attention spent. What lands in
my inbox is not. Each sent message becomes one `signals` row carrying my
snippet, the message I was replying to (`ask_snippet`), and who started the
thread (`initiator`), which is what makes "unplanned, someone else's agenda"
answerable later.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr

import psycopg

from ..config import settings
from . import default_since, get_watermark, insert_signal, set_state, snippets
from .venture_hints import infer

logger = logging.getLogger("iblu_keeper.collectors.gmail_sent")

NAME = "gmail_sent"
MAX_PER_RUN = 100


def _service():
    from ..google_auth import build_service

    return build_service("gmail", "v1")


def _headers(message: dict) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }


def _address_of(raw: str | None) -> str:
    """The bare email address from a From/To header value, lower-cased."""
    return parseaddr(raw or "")[1].strip().lower()


def _first_address(raw: str | None) -> str | None:
    """'Ana <a@x.com>, Bo <b@x.com>' -> 'Ana <a@x.com>'."""
    if not raw:
        return None
    return raw.split(",")[0].strip() or None


def _addresses(raw: str | None) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _occurred_at(message: dict) -> datetime:
    """Gmail's internalDate is epoch milliseconds."""
    return datetime.fromtimestamp(
        int(message.get("internalDate", "0")) / 1000, tz=timezone.utc
    )


def _is_mine(headers: dict[str, str], me: str) -> bool:
    """True only when *I* am the author.

    Must compare the parsed address, not a substring: Google Group traffic
    arrives as `"'Someone' via Contracts" <contracts@blanklabel.team>` and is
    filed under `in:sent` for group members even though someone else wrote it.
    Recording those as mine would attribute other people's work to Ignas.
    """
    return _address_of(headers.get("from")) == me.lower()


def _thread_context(svc, thread_id: str, me: str, my_ts: datetime) -> tuple[str | None, str, int]:
    """Return `(ask_snippet, initiator, thread_len)` for the thread I replied in.

    `ask_snippet` is the newest message before mine that is not from me — the
    thing I was actually responding to.
    """
    from .. import tools  # noqa: F401  (keeps the import graph explicit)
    from ..tools.gmail import _extract_body

    thread = (
        svc.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    messages = thread.get("messages", []) or []
    if not messages:
        return None, "me", 0

    first_headers = _headers(messages[0])
    initiator = "me" if _is_mine(first_headers, me) else "other"

    ask = None
    for msg in messages:
        ts = _occurred_at(msg)
        if ts >= my_ts:
            continue
        headers = _headers(msg)
        if _is_mine(headers, me):
            continue
        ask = snippets.snippet(_extract_body(msg.get("payload", {})))
    return ask, initiator, len(messages)


def collect(conn: psycopg.Connection, *, dry: bool = False) -> int:
    """Insert a signal for every message I sent since the watermark."""
    me = settings.google_user_email
    since = default_since(get_watermark(conn, NAME))
    svc = _service()

    # Gmail's `after:` takes whole seconds since the epoch.
    query = f"in:sent after:{int(since.timestamp())}"
    logger.info("%s: querying %r", NAME, query)

    listing = (
        svc.users()
        .messages()
        .list(userId="me", q=query, maxResults=MAX_PER_RUN)
        .execute()
    )
    ids = [m["id"] for m in listing.get("messages", []) or []]
    if not ids:
        if not dry:
            set_state(conn, NAME, watermark=datetime.now(timezone.utc), error=None)
        logger.info("%s: nothing new", NAME)
        return 0

    from ..tools.gmail import _extract_body

    inserted = 0
    skipped_not_mine = 0
    newest = since
    thread_cache: dict[str, tuple[str | None, str, int]] = {}

    for message_id in ids:
        full = (
            svc.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        headers = _headers(full)
        occurred = _occurred_at(full)
        newest = max(newest, occurred)

        # `in:sent` is not the same as "I wrote it" — see _is_mine.
        if not _is_mine(headers, me):
            skipped_not_mine += 1
            logger.debug(
                "%s: skipping %s, From=%r is not me",
                NAME, message_id, headers.get("from"),
            )
            continue

        body = _extract_body(full.get("payload", {}))
        thread_id = full.get("threadId")
        to_addresses = _addresses(headers.get("to"))
        counterpart = _first_address(headers.get("to"))
        subject = headers.get("subject")

        if thread_id not in thread_cache:
            try:
                thread_cache[thread_id] = _thread_context(svc, thread_id, me, occurred)
            except Exception as exc:  # a thread read failing must not lose the signal
                logger.warning("%s: thread %s unreadable: %s", NAME, thread_id, exc)
                thread_cache[thread_id] = (None, "me", 1)
        ask_snippet, initiator, thread_len = thread_cache[thread_id]

        venture, project = infer(
            account=me,
            counterpart=" ".join(to_addresses),
            subject=subject,
            text=body[:400],
        )

        row = {
            "source": "gmail",
            "kind": "sent",
            "account": me,
            "occurred_at": occurred,
            "actor": "me",
            "initiator": initiator,
            "counterpart": counterpart,
            "container": thread_id,
            "subject": subject,
            "snippet": snippets.snippet(body),
            "ask_snippet": ask_snippet,
            "length_chars": snippets.unquoted_length(body),
            "venture": venture,
            "project": project,
            "source_ref": message_id,
            "meta": {
                "to": to_addresses,
                "cc_count": len(_addresses(headers.get("cc"))),
                "thread_len": thread_len,
            },
        }

        if dry:
            logger.info("%s [dry]: would insert %s — %s", NAME, message_id, subject)
            inserted += 1
            continue
        if insert_signal(conn, row):
            inserted += 1

    if not dry:
        set_state(conn, NAME, watermark=newest, error=None)
    logger.info(
        "%s: %d new signal(s) from %d message(s) (%d not written by me)",
        NAME, inserted, len(ids), skipped_not_mine,
    )
    return inserted
