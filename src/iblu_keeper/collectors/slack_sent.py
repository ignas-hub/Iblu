"""Collector: Slack messages I sent, across every configured workspace.

Slack only exposes `search.messages` to a USER token (xoxp-) — a bot token
cannot search at all — so this collector authenticates as Ignas himself in
each workspace, not as an installed app. The search result already carries
the channel's id/name/type, so a message never triggers an extra lookup call
(no `conversations.info` per row) — see `_channel_fields`.

Like `chat_sent`/`gmail_sent`, this only records what I actually sent: the
query is scoped to `from:<@my_id>` so someone else's message in the same
channel is never mistaken for mine.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

import psycopg

from . import default_since, get_watermark, insert_signal, set_state, snippets
from .venture_hints import infer

logger = logging.getLogger("iblu_keeper.collectors.slack_sent")

NAME = "slack_sent"
API = "https://slack.com/api"
TIMEOUT = 20
# search.messages pages 100 at a time; five pages covers 500 sent messages in
# one run, which even a very chatty day never reaches.
PAGE_SIZE = 100
MAX_PAGES = 5
# How far a brand-new workspace looks back on its very first run (plan
# gmail_sent's FIRST_RUN_HOURS: a week, so a workspace joining mid-stream
# does not under-report its first day against the others).
FIRST_RUN_HOURS = 168

# my Slack user id per workspace alias, resolved once via auth.test and never
# re-fetched — it cannot change mid-run, and re-resolving it would cost one
# API call per tick for no benefit.
_SELF_IDS: dict[str, str] = {}


def _call(token: str, method: str, **params) -> dict:
    """POST one Slack Web API method. Raises on transport or `ok:false`.

    Every Slack response carries `{"ok": bool}` regardless of HTTP status, so
    that is the one thing every caller must check — raising here means a
    failure surfaces as this collector's `last_error` (via run_all) without
    killing any other collector.
    """
    try:
        resp = requests.post(
            f"{API}/{method}",
            headers={"Authorization": f"Bearer {token}"},
            data=params,
            timeout=TIMEOUT,
        )
        data = resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"slack {method}: unreachable ({exc})") from exc
    if not data.get("ok"):
        raise RuntimeError(f"slack {method}: {data.get('error')}")
    return data


def _self_id(workspace: dict) -> str:
    alias = workspace["alias"]
    if alias not in _SELF_IDS:
        data = _call(workspace["token"], "auth.test")
        _SELF_IDS[alias] = data["user_id"]
    return _SELF_IDS[alias]


def _search_query(user_id: str, since: datetime) -> str:
    """`from:<@U...> after:YYYY-MM-DD`.

    Slack's `after:` is date-granular only, so a message on the same calendar
    day as the watermark could fall on either side of it — searching from the
    day *before* guarantees it is never missed. The resulting over-fetch is
    filtered precisely in Python against the real watermark, and
    `UNIQUE(source, source_ref)` makes any re-fetched row free.
    """
    day = (since - timedelta(days=1)).strftime("%Y-%m-%d")
    return f"from:<@{user_id}> after:{day}"


def _channel_fields(channel: dict) -> tuple[str, str, str]:
    """`(counterpart, subject, channel_type)` from a search result's channel.

    Deliberately reads only what the search result already gives us — no
    `conversations.info` call per message.
    """
    channel_id = channel.get("id") or ""
    name = channel.get("name") or ""
    if channel.get("is_im"):
        counterpart = channel.get("user_id") or name or channel_id
        return counterpart, "DM", "im"
    counterpart = name or channel_id
    subject = f"#{name}" if name else f"#{channel_id}"
    channel_type = (
        "mpim" if channel.get("is_mpim") else "group" if channel.get("is_group") else "channel"
    )
    return counterpart, subject, channel_type


def collect(
    conn: psycopg.Connection, *, dry: bool = False, workspace: dict
) -> int:
    """Insert a signal for every Slack message I sent in `workspace` since the watermark."""
    alias = workspace["alias"]
    token = workspace["token"]
    state_key = f"{NAME}:{alias}"
    since = default_since(get_watermark(conn, state_key), fallback_hours=FIRST_RUN_HOURS)

    user_id = _self_id(workspace)
    query = _search_query(user_id, since)

    inserted = 0
    newest = since
    seen = 0
    page = 1

    while page <= MAX_PAGES:
        data = _call(
            token,
            "search.messages",
            query=query,
            sort="timestamp",
            sort_dir="asc",
            count=PAGE_SIZE,
            page=page,
        )
        block = data.get("messages") or {}
        matches = block.get("matches") or []

        for match in matches:
            ts_raw = match.get("ts")
            if not ts_raw:
                continue
            try:
                occurred = datetime.fromtimestamp(float(ts_raw), timezone.utc)
            except (TypeError, ValueError):
                continue
            seen += 1
            if occurred < since:
                continue  # date-granular over-fetch — see _search_query
            newest = max(newest, occurred)

            channel = match.get("channel") or {}
            channel_id = channel.get("id") or ""
            counterpart, subject, channel_type = _channel_fields(channel)
            text = match.get("text") or ""

            venture, project = infer(
                account=f"slack:{alias}", counterpart=counterpart, subject=subject, text=text[:400]
            )
            if venture is not None:
                # A keyword/domain hit in venture_hints is still a guess.
                venture_confidence = "inferred"
            else:
                # The workspace itself genuinely identifies the venture —
                # Deadlift's Slack IS Deadlift — so this is not a guess.
                venture = workspace.get("venture") or None
                venture_confidence = "fact" if venture else "inferred"

            row = {
                "source": "slack",
                "kind": "sent",
                "account": f"slack:{alias}",
                "occurred_at": occurred,
                "actor": "me",
                "counterpart": counterpart,
                "container": channel_id,
                "subject": subject,
                "snippet": snippets.snippet(text),
                "length_chars": len(text),
                "venture": venture,
                "venture_confidence": venture_confidence,
                "project": project,
                "source_ref": f"{alias}:{channel_id}:{ts_raw}",
                "meta": {
                    "permalink": match.get("permalink"),
                    "workspace": alias,
                    "channel_type": channel_type,
                },
            }

            if dry:
                logger.info("%s [dry]: would insert %s in %s", NAME, row["source_ref"], subject)
                inserted += 1
                continue
            if insert_signal(conn, row):
                inserted += 1

        pages = (block.get("paging") or {}).get("pages") or 1
        if page >= pages:
            break
        page += 1

    if not dry:
        set_state(conn, state_key, watermark=newest, error=None)
    logger.info("%s[%s]: %d new signal(s) across %d matched message(s)", NAME, alias, inserted, seen)
    return inserted
