"""Collector: Chat messages I sent (plan §8.2).

Reuses `GoogleChatBackend` for auth, self-id resolution and name lookup — this
module must not duplicate that logic (plan §8). Only my own messages become
signals; other people's messages exist here only as `ask_snippet`, the thing I
was replying to.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import psycopg

from ..config import settings
from . import default_since, get_watermark, insert_signal, set_state, snippets
from .venture_hints import infer

logger = logging.getLogger("iblu_keeper.collectors.chat_sent")

NAME = "chat_sent"
MAX_SPACES = 100
MAX_MESSAGES_PER_SPACE = 50
# How far back to look for the message I was responding to, when it is not in
# an explicit thread.
ASK_LOOKBACK = timedelta(hours=4)


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _space_label(backend, space: dict, self_id: str) -> str:
    """Display name, or for a DM the other person's resolved name."""
    display = (space.get("displayName") or "").strip()
    if display:
        return display
    if space.get("spaceType") == "DIRECT_MESSAGE" or space.get("type") == "DM":
        try:
            members = backend._fetch_members(space["name"])
            others = [uid for uid in members if uid and uid != self_id]
            if others:
                backend._resolve_names(set(others))
                return backend._label(others[0])
        except Exception as exc:  # naming is cosmetic; never lose the signal
            logger.debug("%s: could not resolve DM name for %s: %s", NAME, space.get("name"), exc)
    return space.get("name", "unknown space")


def collect(
    conn: psycopg.Connection, *, dry: bool = False, account: dict | None = None
) -> int:
    """Insert a signal for every Chat message I sent since the watermark."""
    from ..tools.chat import get_backend

    alias = (account or {}).get("alias") or settings.primary_alias
    backend = get_backend(None if alias == settings.primary_alias else alias)
    if not hasattr(backend, "_ensure_self_id"):
        raise RuntimeError(
            "chat backend is not the Google backend — refusing to collect "
            "(mock data must never reach the database)"
        )

    me = (account or {}).get("email") or settings.google_user_email
    state_key = NAME if alias == settings.primary_alias else f"{NAME}:{alias}"
    self_id = backend._ensure_self_id()
    self_name = f"users/{self_id}"
    since = default_since(get_watermark(conn, state_key))
    svc = backend._service()

    inserted = 0
    newest = since
    seen_spaces = 0
    page_token = None

    while True:
        spaces_page = svc.spaces().list(pageSize=MAX_SPACES, pageToken=page_token).execute()
        spaces = spaces_page.get("spaces", []) or []

        for space in spaces:
            # Never record the Secretary space. Answering a ping is not work,
            # and left in, the recorder would eventually report talking to
            # itself as Ignas's biggest attention sink.
            if settings.secretary_space and space["name"] == settings.secretary_space:
                continue
            seen_spaces += 1
            # Cheap skip: nothing has happened here since we last looked.
            last_active = _parse_ts(space.get("lastActiveTime"))
            if last_active is not None and last_active < since:
                continue

            space_name = space["name"]
            try:
                listing = (
                    svc.spaces()
                    .messages()
                    .list(
                        parent=space_name,
                        filter=f'createTime > "{_rfc3339(since)}"',
                        orderBy="createTime desc",
                        pageSize=MAX_MESSAGES_PER_SPACE,
                    )
                    .execute()
                )
            except Exception as exc:
                logger.warning("%s: cannot read %s: %s", NAME, space_name, exc)
                continue

            messages = listing.get("messages", []) or []
            if not messages:
                continue

            label = _space_label(backend, space, self_id)
            # Oldest first, so `ask_snippet` lookups see earlier messages.
            messages.sort(key=lambda m: m.get("createTime") or "")

            for msg in messages:
                created = _parse_ts(msg.get("createTime"))
                if created is None:
                    continue
                newest = max(newest, created)

                if (msg.get("sender", {}) or {}).get("name") != self_name:
                    continue  # someone else's message: context, not signal

                text = msg.get("text") or ""
                thread_name = (msg.get("thread", {}) or {}).get("name")

                ask_snippet = None
                initiator = "me"
                for earlier in messages:
                    e_ts = _parse_ts(earlier.get("createTime"))
                    if e_ts is None or e_ts >= created:
                        continue
                    same_thread = (earlier.get("thread", {}) or {}).get("name") == thread_name
                    within_window = created - e_ts <= ASK_LOOKBACK
                    if not (same_thread or within_window):
                        continue
                    if (earlier.get("sender", {}) or {}).get("name") == self_name:
                        continue
                    ask_snippet = snippets.snippet(earlier.get("text") or "")
                    initiator = "other"

                venture, project = infer(
                    account=me, counterpart=label, subject=label, text=text[:400]
                )

                row = {
                    "source": "chat",
                    "kind": "sent",
                    "account": me,
                    "occurred_at": created,
                    "actor": "me",
                    "initiator": initiator,
                    "counterpart": label,
                    "container": space_name,
                    "subject": label,
                    "snippet": snippets.snippet(text),
                    "ask_snippet": ask_snippet,
                    "length_chars": snippets.unquoted_length(text),
                    "venture": venture,
                    "project": project,
                    "source_ref": msg["name"],
                    "meta": {"thread": thread_name, "space_type": space.get("spaceType")},
                }

                if dry:
                    logger.info("%s [dry]: would insert %s in %s", NAME, msg["name"], label)
                    inserted += 1
                    continue
                if insert_signal(conn, row):
                    inserted += 1

        page_token = spaces_page.get("nextPageToken")
        if not page_token:
            break

    if not dry:
        set_state(conn, state_key, watermark=newest, error=None)
    logger.info("%s[%s]: %d new signal(s) across %d space(s)", NAME, alias, inserted, seen_spaces)
    return inserted
