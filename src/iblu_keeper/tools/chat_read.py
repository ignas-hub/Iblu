"""Reading Chat below the level of "the last N messages in a space".

`tools/chat.py` lists conversations, pages recent messages, lists unread,
marks read and sends. It cannot pull one thread out of a busy space, find
the message where someone said "invoice", name the members of a space with
their addresses, describe a space, or reply inside a thread rather than
starting a new one. Every one of those came up as "can you add a tool".
Ignas, 2026-10-26: "It's crazy how many times I have to ask to add tools."
This module is the Chat part of the batch that ends that.

Two functions, by side-effect class (HANDOFF §12 — one tool per domain ×
side-effect class, each a permanent Allow click):

* `chat_read`  — ONE read tool with an `action` parameter. Nothing here writes.
* `chat_reply` — ONE explicit send-class action: a reply in an existing
  thread. It is a separate tool precisely so it can sit in the "ask" group
  with `chat_send_message`, and so a read can never post as a side effect.
  It never defaults the space — `space` is required and there is no
  fallback to the Secretary space (CLAUDE.md: the Secretary space is the only
  space *IBLU* may post to on its timers; this is Ignas posting, through
  Claude, to a space he named).

Search is client-side. The Chat API has no server-side text search for a
user token — `spaces.messages.list` filters only on `createTime` and
`thread.name` — so "search" lists recent messages and matches the words
here. It is bounded by `days` and by a cap on the spaces it scans; say so to
the caller when a result might simply be older than the window.

Scopes, verified 2026-10-26: `chat.spaces.readonly`, `chat.memberships.readonly`,
`chat.messages`. Reactions and space creation are outside them and not here.

Everything live goes through the `GoogleChatBackend` in `tools/chat.py`
(service, self-id, People-API name cache), so there is one place that knows
how to act as which account.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from ..config import resolve_account, settings
from .chat import get_backend

logger = logging.getLogger("iblu_keeper.tools.chat_read")

ACTIONS = ("thread", "search", "members", "space", "my_spaces")

# `search` without a space scans this many most-recently-active spaces. More
# than this is a fan-out the Chat quota notices.
SEARCH_SPACE_CAP = 20
# Messages pulled per space per page in `search`; Chat's pageSize ceiling.
SEARCH_PAGE = 100
# Pages walked when `search` targets one space (one page when it fans out).
SEARCH_PAGES_SINGLE = 5


class ChatReadError(ValueError):
    """Bad input to `chat_read` / `chat_reply`. The message names what was expected."""


def _backend(account: str | None):
    """The live backend for one account. A module-level seam so tests can
    hand in a fake without touching `get_backend`'s cache."""
    return get_backend(resolve_account(account))


def _space_name(space: str | None) -> str:
    if not space or not str(space).strip():
        raise ChatReadError("space is required (e.g. 'spaces/AAAA…') — there is no default space")
    text = str(space).strip()
    return text if text.startswith("spaces/") else f"spaces/{text}"


def _thread_name(space: str, thread: str | None) -> str:
    if not thread or not str(thread).strip():
        raise ChatReadError("thread is required ('spaces/…/threads/…' or the thread id)")
    text = str(thread).strip()
    if text.startswith("spaces/"):
        return text
    return f"{space}/threads/{text}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --- shaping ----------------------------------------------------------------


def _sender_uid(msg: dict) -> str:
    name = (msg.get("sender") or {}).get("name", "") or ""
    return name.removeprefix("users/") if name.startswith("users/") else ""


def _message_view(backend, msg: dict, self_id: str) -> dict:
    uid = _sender_uid(msg)
    name = msg.get("name", "") or ""
    text = msg.get("text", "") or ""
    if not text and msg.get("attachment"):
        text = "[attachment]"
    return {
        "id": name,
        "space": name.split("/messages/")[0] if "/messages/" in name else "",
        "thread": (msg.get("thread") or {}).get("name", ""),
        "sender": backend._label(uid) if uid else (msg.get("sender") or {}).get("name", ""),
        "sender_id": uid,
        "is_me": bool(self_id and uid == self_id),
        "text": text,
        "create_time": msg.get("createTime", ""),
    }


def _people_lookup(backend, uids: list[str]) -> dict[str, dict]:
    """users/<id> → {name, email} via People API. Empty strings where the
    directory does not share them (external users, sharing off)."""
    out = {uid: {"name": "", "email": ""} for uid in uids}
    if not uids:
        return out
    people = backend._people()
    for i in range(0, len(uids), 200):
        chunk = uids[i:i + 200]
        try:
            resp = (
                people.people()
                .getBatchGet(
                    resourceNames=[f"people/{uid}" for uid in chunk],
                    personFields="names,emailAddresses",
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("chat_read people lookup failed for %d ids: %s", len(chunk), exc)
            continue
        for r in resp.get("responses", []) or []:
            uid = (r.get("requestedResourceName", "") or "").removeprefix("people/")
            person = r.get("person", {}) or {}
            names = person.get("names") or []
            emails = person.get("emailAddresses") or []
            out[uid] = {
                "name": (names[0].get("displayName", "") if names else "") or "",
                "email": (emails[0].get("value", "") if emails else "") or "",
            }
    return out


def _list_messages(service, space: str, filter_: str | None, page_size: int, order: str, pages: int) -> list[dict]:
    """Walk up to `pages` pages of spaces.messages.list."""
    out: list[dict] = []
    token = None
    for _ in range(pages):
        kwargs: dict = {"parent": space, "pageSize": page_size, "orderBy": order}
        if filter_:
            kwargs["filter"] = filter_
        if token:
            kwargs["pageToken"] = token
        resp = service.spaces().messages().list(**kwargs).execute()
        out.extend(resp.get("messages", []) or [])
        token = resp.get("nextPageToken")
        if not token:
            break
    return out


# --- actions ----------------------------------------------------------------


def _thread(backend, space: str, thread: str, limit: int) -> dict:
    service = backend._service()
    msgs = _list_messages(
        service, space, f"thread.name = {thread}", min(limit, SEARCH_PAGE), "createTime asc",
        pages=max(1, -(-limit // SEARCH_PAGE)),
    )
    msgs.sort(key=lambda m: m.get("createTime", ""))
    msgs = msgs[:limit]
    backend._resolve_names({_sender_uid(m) for m in msgs if _sender_uid(m)})
    self_id = backend._ensure_self_id()
    items = [_message_view(backend, m, self_id) for m in msgs]
    return {"status": "ok", "space": space, "thread": thread, "count": len(items), "messages": items}


def _search(backend, query: str, space: str | None, limit: int, days: int) -> dict:
    words = [w for w in query.lower().split() if w]
    if not words:
        raise ChatReadError("search requires query (one or more words)")
    if days < 1:
        raise ChatReadError(f"days must be >= 1, got {days!r}")
    since = (_utc_now() - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    filter_ = f'createTime > "{since}"'

    if space:
        targets = [space]
        pages = SEARCH_PAGES_SINGLE
    else:
        spaces = backend._service().spaces().list(pageSize=100).execute().get("spaces", []) or []
        spaces.sort(key=lambda s: s.get("lastActiveTime") or "", reverse=True)
        targets = [s["name"] for s in spaces[:SEARCH_SPACE_CAP]]
        pages = 1

    def scan(target: str) -> list[dict]:
        # One service per worker — httplib2 is not thread-safe.
        return _list_messages(backend._service(), target, filter_, SEARCH_PAGE, "createTime desc", pages)

    hits: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(targets)))) as ex:
        futures = {ex.submit(scan, t): t for t in targets}
        for fut in as_completed(futures):
            try:
                msgs = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.info("chat_read search skipped %s: %s", futures[fut], exc)
                continue
            for m in msgs:
                text = (m.get("text", "") or "").lower()
                if all(w in text for w in words):
                    hits.append(m)

    hits.sort(key=lambda m: m.get("createTime", ""), reverse=True)
    hits = hits[:limit]
    backend._resolve_names({_sender_uid(m) for m in hits if _sender_uid(m)})
    self_id = backend._ensure_self_id()
    items = [_message_view(backend, m, self_id) for m in hits]
    return {
        "status": "ok",
        "query": query,
        "since": since,
        "spaces_scanned": len(targets),
        "count": len(items),
        "items": items,
        "note": (
            "Client-side match over recent messages — Chat has no server-side text "
            f"search for a user token. Window: last {days} days"
            + ("" if space else f", {len(targets)} most active spaces") + "."
        ),
    }


def _members(backend, space: str, limit: int) -> dict:
    service = backend._service()
    memberships: list[dict] = []
    token = None
    while len(memberships) < limit:
        kwargs: dict = {"parent": space, "pageSize": min(100, limit - len(memberships))}
        if token:
            kwargs["pageToken"] = token
        resp = service.spaces().members().list(**kwargs).execute()
        memberships.extend(resp.get("memberships", []) or [])
        token = resp.get("nextPageToken")
        if not token:
            break

    uids = []
    for m in memberships:
        member = m.get("member", {}) or {}
        name = member.get("name", "") or ""
        if name.startswith("users/"):
            uids.append(name.removeprefix("users/"))
    people = _people_lookup(backend, uids)

    items: list[dict] = []
    for m in memberships:
        member = m.get("member", {}) or {}
        name = member.get("name", "") or ""
        uid = name.removeprefix("users/") if name.startswith("users/") else ""
        looked = people.get(uid, {"name": "", "email": ""})
        items.append({
            "user_id": uid or name,
            "name": looked["name"] or member.get("displayName", "") or "",
            "email": looked["email"],
            "type": member.get("type", ""),
            "role": m.get("role", ""),
            "state": m.get("state", ""),
            "joined": m.get("createTime", ""),
        })
    return {"status": "ok", "space": space, "count": len(items), "members": items}


def _member_count(service, space: dict) -> int | None:
    counts = space.get("membershipCount") or {}
    if counts:
        return int(counts.get("joinedDirectHumanUserCount", 0) or 0) + int(counts.get("joinedGroupCount", 0) or 0)
    try:
        resp = service.spaces().members().list(parent=space["name"], pageSize=100).execute()
        n = len(resp.get("memberships", []) or [])
        return n if not resp.get("nextPageToken") else None
    except Exception:  # noqa: BLE001
        return None


def _space(backend, space: str) -> dict:
    service = backend._service()
    s = service.spaces().get(name=space).execute()
    return {
        "status": "ok",
        "id": s.get("name", space),
        "display_name": s.get("displayName", "") or "",
        "type": s.get("spaceType") or s.get("type", ""),
        "threading_state": s.get("spaceThreadingState", ""),
        "history_state": s.get("spaceHistoryState", ""),
        "external_user_allowed": bool(s.get("externalUserAllowed", False)),
        "create_time": s.get("createTime", ""),
        "last_active_time": s.get("lastActiveTime", ""),
        "member_count": _member_count(service, s),
    }


def _my_spaces(backend, limit: int) -> dict:
    spaces = backend._service().spaces().list(pageSize=100).execute().get("spaces", []) or []
    spaces.sort(key=lambda s: s.get("lastActiveTime") or "", reverse=True)
    spaces = spaces[:limit]

    # lastReadTime is served from the readstate cache when it has it, one
    # API call per space otherwise; they are independent, so in parallel.
    read_times: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(backend._get_last_read_time, s["name"]): s["name"] for s in spaces}
        for fut in as_completed(futures):
            try:
                read_times[futures[fut]] = fut.result() or ""
            except Exception:  # noqa: BLE001
                read_times[futures[fut]] = ""

    items = []
    for s in spaces:
        sid = s["name"]
        last_active = s.get("lastActiveTime", "") or ""
        last_read = read_times.get(sid, "")
        items.append({
            "id": sid,
            "display_name": s.get("displayName", "") or "",
            "type": s.get("spaceType") or s.get("type", ""),
            "last_active_time": last_active,
            "last_read_time": last_read,
            "has_unread": bool(last_active and last_read and last_active > last_read),
        })
    return {
        "status": "ok",
        "count": len(items),
        "spaces": items,
        "note": "DMs and unnamed group chats have an empty display_name here; chat_list_conversations names them.",
    }


# --- read dispatch ------------------------------------------------------------


def chat_read(
    action: str,
    space: str | None = None,
    thread: str | None = None,
    query: str | None = None,
    limit: int = 20,
    days: int = 30,
    account: str | None = None,
) -> dict:
    """Read Chat below the "recent messages" level. One tool, one `action`.

    Actions:
      thread     `space` + `thread` (full name or id) → that thread's
                 messages, oldest first, up to `limit`.
      search     `query` words, optional `space`, `limit`, `days` → messages
                 whose text contains every word. Client-side: Chat has no
                 server-side text search for a user token, so this lists the
                 last `days` days (one space, or the 20 most active) and
                 matches here.
      members    `space` → members with display name and email where the
                 directory shares them, role and state.
      space      `space` → displayName, type, threading state, history
                 state, createTime, lastActiveTime, member count.
      my_spaces  the `limit` most recently active spaces with the user's
                 lastReadTime and a has_unread flag.

    Nothing here posts. ``account`` — ``blt`` (default), ``deadlift``, ``choco``.
    """
    if action not in ACTIONS:
        raise ChatReadError(
            f"unknown action {action!r} — expected one of {', '.join(ACTIONS)}"
        )
    if limit < 1:
        raise ChatReadError(f"limit must be >= 1, got {limit!r}")

    space_name = _space_name(space) if action in ("thread", "members", "space") else (
        _space_name(space) if space else None
    )
    thread_name = _thread_name(space_name, thread) if action == "thread" else None
    if action == "search" and not (query and query.strip()):
        raise ChatReadError("search requires query (one or more words)")

    account = resolve_account(account)
    if settings.use_mock:
        return {
            "_mock": True,
            "action": action,
            "space": space_name,
            "status": "not_read_mock",
            "count": 0,
            "items": [],
            "note": "MOCK MODE — nothing was read from Chat. Set DRY_RUN=false.",
        }

    backend = _backend(account)
    if action == "thread":
        return _thread(backend, space_name, thread_name, limit)
    if action == "search":
        return _search(backend, query.strip(), space_name, limit, days)
    if action == "members":
        return _members(backend, space_name, limit)
    if action == "space":
        return _space(backend, space_name)
    return _my_spaces(backend, limit)


# --- the one send-class action ---------------------------------------------------


def chat_reply(space: str, thread: str, text: str, account: str | None = None) -> dict:
    """Reply inside an existing Chat thread. SEND-CLASS: this puts a message in
    front of other people, and is its own tool for that reason.

    `space` and `thread` are both required — there is no default space and
    this never falls back to the Secretary space. Posts with
    messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD, so if the thread
    no longer exists the message starts a new one in the same space rather
    than failing silently; the result's `thread` says where it landed.
    ``account`` — ``blt`` (default), ``deadlift``, ``choco``.
    """
    space_name = _space_name(space)
    thread_name = _thread_name(space_name, thread)
    if not text or not text.strip():
        raise ChatReadError("chat_reply requires non-empty text")

    account = resolve_account(account)
    if settings.use_mock:
        logger.warning("MOCK chat_reply to %s — NOT delivered (DRY_RUN).", thread_name)
        return {
            "_mock": True,
            "id": f"{space_name}/messages/MOCK_REPLY",
            "space": space_name,
            "thread": thread_name,
            "text": text,
            "status": "not_sent_mock",
            "note": "MOCK MODE — reply was NOT delivered. Set DRY_RUN=false.",
        }

    backend = _backend(account)
    msg = (
        backend._service().spaces().messages()
        .create(
            parent=space_name,
            messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
            body={"text": text, "thread": {"name": thread_name}},
        )
        .execute()
    )
    msg_name = msg.get("name", "")
    if not msg_name:
        raise RuntimeError(
            f"Chat reply returned no message name (response={msg!r}); "
            "treating as failure rather than reporting a false success."
        )
    landed = (msg.get("thread") or {}).get("name", "") or thread_name
    logger.info("chat_reply delivered to %s (chat name=%s)", landed, msg_name)
    return {
        "id": msg_name,
        "space": space_name,
        "thread": landed,
        "fell_back_to_new_thread": landed != thread_name,
        "text": msg.get("text", text),
        "create_time": msg.get("createTime", ""),
        "status": "sent",
    }
