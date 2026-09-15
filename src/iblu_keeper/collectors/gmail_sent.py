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
# How far a brand-new account looks back on its very first run.
FIRST_RUN_HOURS = 168


def _service(account: str | None = None):
    from ..google_auth import build_service

    return build_service("gmail", "v1", account=account)


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


def _send_as_addresses(svc, me: str) -> set[str]:
    """Every address this mailbox can legitimately send as.

    A mailbox often sends under an alias: the Choco account sends invoices as
    `ap@chocoagency.com`, and those are still Ignas's work. Without this, every
    aliased message is discarded as "not written by me".
    """
    addresses = {me.lower()}
    try:
        for entry in svc.users().settings().sendAs().list(userId="me").execute().get("sendAs", []):
            address = (entry.get("sendAsEmail") or "").lower()
            if address:
                addresses.add(address)
    except Exception as exc:  # noqa: BLE001 - fall back to the primary address
        logger.warning("%s: could not read send-as aliases: %s", NAME, exc)
    return addresses


def _group_author(headers: dict[str, str]) -> str | None:
    """The real author behind a Google Group rewrite, or None.

    `"'Diana Saavedra' via ap" <ap@chocoagency.com>` -> "Diana Saavedra".
    Keeping the author means inbound group traffic is attributable later,
    rather than 31 identical-looking rows from the group address.
    """
    raw = headers.get("from") or ""
    if " via " not in raw.lower():
        return None
    display = raw.split("<")[0].strip().strip('"').strip()
    author = display.split(" via ")[0].strip().strip("'").strip()
    return author or None


def _is_group_delivery(headers: dict[str, str]) -> bool:
    """True when this is Google Group traffic, not something the user sent.

    Group aliases appear in the mailbox's own send-as list, so the alias check
    alone would re-admit them. The reliable signal is Google's own rewrite of
    the From display name: mail delivered through a Group to a member arrives
    as `'Original Author' via GroupName`, and only a Group does that.

    Deliberately NOT keyed on list-unsubscribe / precedence headers, although
    group mail carries them: forwarding a newsletter preserves the original's
    list headers, so that rule discarded genuine forwards — one was found in
    the Choco mailbox the moment aliases were switched on.
    """
    return " via " in (headers.get("from") or "").lower()


def _is_mine(headers: dict[str, str], me: str, aliases: set[str] | None = None) -> bool:
    """True only when *I* am the author.

    Compares the parsed address against every address this mailbox may send as,
    then excludes Google Group deliveries — which carry a group alias in From
    and are filed under `in:sent` for members even though someone else wrote
    them. Six of the first seven messages collected were other people's.
    """
    address = _address_of(headers.get("from"))
    if not address:
        return False
    if address not in (aliases or {me.lower()}):
        return False
    return not _is_group_delivery(headers)


def _thread_context(
    svc, thread_id: str, me: str, my_ts: datetime, aliases: set[str] | None = None
) -> tuple[str | None, str, int]:
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
    initiator = "me" if _is_mine(first_headers, me, aliases) else "other"

    ask = None
    for msg in messages:
        ts = _occurred_at(msg)
        if ts >= my_ts:
            continue
        headers = _headers(msg)
        if _is_mine(headers, me, my_addresses):
            continue
        ask = snippets.snippet(_extract_body(msg.get("payload", {})))
    return ask, initiator, len(messages)


def collect(
    conn: psycopg.Connection, *, dry: bool = False, account: dict | None = None
) -> int:
    """Insert a signal for every message I sent since the watermark.

    `account` selects the Google account; omitting it uses the primary one.
    The watermark is per account, so one mailbox falling behind cannot cause
    another's messages to be skipped.
    """
    alias = (account or {}).get("alias") or settings.primary_alias
    me = (account or {}).get("email") or settings.google_user_email
    state_key = NAME if alias == settings.primary_alias else f"{NAME}:{alias}"
    # An account joining mid-stream would otherwise start with 24h of history
    # while the others have weeks, and the venture split would under-report it
    # for the first day. A first run reaches back a week; subsequent runs use
    # the watermark as normal.
    since = default_since(get_watermark(conn, state_key), fallback_hours=FIRST_RUN_HOURS)
    svc = _service(None if alias == settings.primary_alias else alias)
    my_addresses = _send_as_addresses(svc, me)

    # Gmail's `after:` takes whole seconds since the epoch.
    query = f"in:sent after:{int(since.timestamp())}"
    logger.info("%s[%s]: querying %r", NAME, alias, query)

    listing = (
        svc.users()
        .messages()
        .list(userId="me", q=query, maxResults=MAX_PER_RUN)
        .execute()
    )
    ids = [m["id"] for m in listing.get("messages", []) or []]
    if not ids:
        if not dry:
            set_state(conn, state_key, watermark=datetime.now(timezone.utc), error=None)
        logger.info("%s: nothing new", NAME)
        return 0

    from ..tools.gmail import _extract_body

    inserted = 0
    skipped_not_mine = 0
    newest = since
    # Taken BEFORE the read, so anything that arrives during it is re-read next
    # run rather than skipped.
    read_through = datetime.now(timezone.utc)
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
        mine = _is_mine(headers, me, my_addresses)
        group_author = _group_author(headers)
        from_address = _address_of(headers.get("from"))

        if not mine:
            # Mail delivered through a Google Group Ignas belongs to. He did not
            # write it, so it is never his attention — but on a group he works
            # (ap@chocoagency.com is the Choco payables queue) the inbound
            # volume is real demand, and invisible demand cannot be reasoned
            # about. Recorded with actor='other' so the attention split stays
            # honest while the load becomes visible.
            if not (group_author and from_address in my_addresses):
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
                thread_cache[thread_id] = _thread_context(svc, thread_id, me, occurred, my_addresses)
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
            "kind": "sent" if mine else "received",
            "account": me,
            "occurred_at": occurred,
            "actor": "me" if mine else "other",
            "counterpart": group_author or counterpart,
            "initiator": initiator if mine else "other",
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
                **({"group": from_address} if not mine else {}),
            },
        }

        if dry:
            logger.info("%s [dry]: would insert %s — %s", NAME, message_id, subject)
            inserted += 1
            continue
        if insert_signal(conn, row):
            inserted += 1

    # The watermark means "I have read up to here", not "the newest thing I
    # found". Leaving it at the newest signal meant a quiet mailbox looked
    # identical to a broken one: gmail_sent:deadlift sat five days behind its
    # own last run, the collector re-read the same window on every tick, and
    # the staleness check could never tell a silent week from a dead token.
    #
    # `get_watermark` subtracts OVERLAP when reading it back, so a message that
    # arrived while this run was mid-flight is still picked up next time, and
    # UNIQUE(source, source_ref) makes the re-read free.
    if not dry:
        set_state(conn, state_key, watermark=max(newest, read_through), error=None)
    logger.info(
        "%s[%s]: %d new signal(s) from %d message(s) (%d not written by me)",
        NAME, alias, inserted, len(ids), skipped_not_mine,
    )
    return inserted
