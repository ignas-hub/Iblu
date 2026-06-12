"""Google Chat tools behind a swappable backend interface.

IMPORTANT (from handover): it is NOT yet validated that the Google Chat API +
service account can read/send Ignas's personal DMs and spaces. So the Chat
integration sits behind a clean `ChatBackend` interface. If the Chat API path
fails, an alternative backend (e.g. a different transport) can be dropped in by
implementing `ChatBackend` and registering it in `get_backend()` — no tool or
server code changes required.

Backends:
  - MockChatBackend  : deterministic fake data; used in dry-run / no-creds mode.
  - GoogleChatBackend: real Google Chat API via the delegated service account.

Conversations are searched/identified primarily by the participant's name.
"""

from __future__ import annotations

import abc
from functools import lru_cache

from ..config import settings
from ..store import drafts


# --------------------------------------------------------------------------- #
# Backend interface
# --------------------------------------------------------------------------- #
class ChatBackend(abc.ABC):
    """Swappable Google Chat backend contract."""

    @abc.abstractmethod
    def list_conversations(
        self, query: str | None = None, limit: int = 20
    ) -> list[dict]:
        """Recent conversations/spaces with participant names.

        Returns up to `limit` items, sorted by most recent activity first.
        `query` filters by person/space name (case-insensitive substring).
        Each item: {id, name, type, participants: [..], last_message_preview,
        last_active_time}.
        """

    @abc.abstractmethod
    def get_messages(self, conversation: str, limit: int = 20) -> list[dict]:
        """Message history for a conversation, oldest→newest.

        Each item: {id, sender, text, create_time}.
        """

    @abc.abstractmethod
    def send_message(self, conversation: str, text: str) -> dict:
        """Send a message. Returns {id, conversation, text, create_time}."""

    @abc.abstractmethod
    def list_unread(self, limit: int = 10) -> list[dict]:
        """Conversations that have new messages since the user last read them.

        Each item is the same shape as `list_conversations` (id, name, type,
        participants, last_message_preview, last_active_time) with an added
        `last_read_time` field for context.
        """

    @abc.abstractmethod
    def mark_read(self, conversation: str) -> dict:
        """Mark a Chat conversation as read up to now."""


# --------------------------------------------------------------------------- #
# Mock backend (dry-run / no credentials)
# --------------------------------------------------------------------------- #
class MockChatBackend(ChatBackend):
    """Deterministic fake Chat data so the system runs without Google access."""

    _CONVERSATIONS = [
        {
            "id": "spaces/MOCK_AAAA",
            "name": "Jonas Petrauskas",
            "type": "DM",
            "participants": ["Ignas", "Jonas Petrauskas"],
            "last_message_preview": "Can we move the call to 3pm?",
        },
        {
            "id": "spaces/MOCK_BBBB",
            "name": "Sales Team",
            "type": "SPACE",
            "participants": ["Ignas", "Jonas", "Marta", "Lukas"],
            "last_message_preview": "Q2 pipeline looks strong.",
        },
        {
            "id": "spaces/MOCK_CCCC",
            "name": "Marta Kazlauskaite",
            "type": "DM",
            "participants": ["Ignas", "Marta Kazlauskaite"],
            "last_message_preview": "Sent over the proposal draft.",
        },
    ]

    def list_conversations(
        self, query: str | None = None, limit: int = 20
    ) -> list[dict]:
        items = list(self._CONVERSATIONS)
        if query:
            q = query.lower()
            items = [
                c
                for c in items
                if q in c["name"].lower()
                or any(q in p.lower() for p in c["participants"])
            ]
        return items[: max(1, limit)]

    def get_messages(self, conversation: str, limit: int = 20) -> list[dict]:
        messages = [
            {
                "id": f"{conversation}/messages/1",
                "sender": "Jonas Petrauskas",
                "text": "Hey, are we still on for tomorrow?",
                "create_time": "2026-06-09T09:15:00Z",
            },
            {
                "id": f"{conversation}/messages/2",
                "sender": "Ignas",
                "text": "Yes — 2pm works.",
                "create_time": "2026-06-09T09:17:00Z",
            },
            {
                "id": f"{conversation}/messages/3",
                "sender": "Jonas Petrauskas",
                "text": "Can we move the call to 3pm?",
                "create_time": "2026-06-09T16:40:00Z",
            },
        ]
        return messages[-limit:]

    def send_message(self, conversation: str, text: str) -> dict:
        return {
            "id": f"{conversation}/messages/MOCK_SENT",
            "conversation": conversation,
            "text": text,
            "create_time": "2026-06-10T12:00:00Z",
            "mock": True,
        }

    def list_unread(self, limit: int = 10) -> list[dict]:
        unread = [
            {**c, "last_read_time": "2026-06-08T00:00:00Z"}
            for c in self._CONVERSATIONS[:limit]
        ]
        return unread

    def mark_read(self, conversation: str) -> dict:
        return {
            "conversation": conversation, "status": "read",
            "last_read_time": "2026-06-10T12:00:00Z", "mock": True,
        }


# --------------------------------------------------------------------------- #
# Google Chat backend (real API)
# --------------------------------------------------------------------------- #
class GoogleChatBackend(ChatBackend):
    """Real Google Chat API backend via single-user OAuth.

    Display names for users are NOT returned by Chat API under user OAuth (by
    design — see Google's docs). We resolve `users/<id>` → "Display Name" via
    the People API (`people:batchGet`) using the directory.readonly scope, and
    cache the mapping in-memory for the life of the process. Names may still
    be empty for external users or when Workspace directory contact-sharing
    is disabled — we fall back to the raw user ID in that case.
    """

    _PREVIEW_MAX = 140

    def __init__(self) -> None:
        self._name_cache: dict[str, str] = {}  # "users/<id>" stripped → display
        self._self_user_id: str | None = None

    def _service(self):
        from ..google_auth import build_service

        return build_service("chat", "v1")

    def _people(self):
        from ..google_auth import build_service

        return build_service("people", "v1")

    def _ensure_self_id(self) -> str:
        if self._self_user_id is not None:
            return self._self_user_id
        from ..google_auth import get_credentials

        creds = get_credentials()
        if not creds.valid:
            from google.auth.transport.requests import Request  # type: ignore

            creds.refresh(Request())
        import requests

        try:
            r = requests.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": creds.token},
                timeout=10,
            ).json()
            self._self_user_id = r.get("sub", "") or ""
        except Exception:  # noqa: BLE001
            self._self_user_id = ""
        return self._self_user_id

    def _resolve_names(self, user_ids: set[str]) -> None:
        """Batch-resolve `users/<id>` IDs to display names via People API.

        Caches results (including empty strings to avoid retrying misses).
        """
        missing = [uid for uid in user_ids if uid not in self._name_cache]
        if not missing:
            return
        people = self._people()
        # People API allows up to 200 IDs per batch.
        for i in range(0, len(missing), 200):
            chunk = missing[i : i + 200]
            try:
                resp = (
                    people.people()
                    .getBatchGet(
                        resourceNames=[f"people/{uid}" for uid in chunk],
                        personFields="names,emailAddresses",
                    )
                    .execute()
                )
            except Exception:  # noqa: BLE001
                # Cache empty so we don't keep hammering on errors.
                for uid in chunk:
                    self._name_cache.setdefault(uid, "")
                continue
            for r in resp.get("responses", []):
                req = r.get("requestedResourceName", "")
                uid = req.removeprefix("people/")
                person = r.get("person", {}) or {}
                display = ""
                names = person.get("names") or []
                if names:
                    display = names[0].get("displayName", "") or ""
                if not display:
                    emails = person.get("emailAddresses") or []
                    if emails:
                        display = emails[0].get("value", "") or ""
                self._name_cache[uid] = display
        # Make sure every requested ID has an entry (handles partial responses).
        for uid in missing:
            self._name_cache.setdefault(uid, "")

    def _label(self, uid: str) -> str:
        """Cached display name, or `users/<id>` fallback."""
        name = self._name_cache.get(uid, "")
        return name if name else f"users/{uid}"

    def _format_preview(self, text: str, sender_uid: str, self_id: str) -> str:
        """Format a message as 'Sender: text' (or 'You: text' for own messages)."""
        text = (text or "")[: self._PREVIEW_MAX]
        if sender_uid and self_id and sender_uid == self_id:
            return f"You: {text}"
        if sender_uid:
            return f"{self._label(sender_uid)}: {text}"
        return text

    def _latest_message(self, conversation: str, look_back: int = 1) -> list[dict]:
        """Fetch up to `look_back` most recent messages (newest first). [] on error."""
        service = self._service()
        try:
            return (
                service.spaces().messages()
                .list(parent=conversation, pageSize=look_back, orderBy="createTime desc")
                .execute().get("messages", [])
            )
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _sender_uid(msg: dict) -> str:
        name = msg.get("sender", {}).get("name", "")
        return name.removeprefix("users/") if name.startswith("users/") else ""

    def _msg_text(self, msg: dict) -> str:
        text = msg.get("text", "") or ""
        if not text and msg.get("attachment"):
            text = "[attachment]"
        return text

    def _pick_unread_message(
        self, conversation: str, self_id: str, since: str
    ) -> dict | None:
        """Pick the latest message in `conversation` that's genuinely new for the user.

        Walks the 5 most recent messages and returns the latest one that BOTH
        (a) was sent by someone other than self, AND (b) is newer than `since`
        (the user's lastReadTime). Returns None if no such message exists —
        meaning the space's recent activity is entirely the user's own.
        """
        for msg in self._latest_message(conversation, look_back=5):
            uid = self._sender_uid(msg)
            ct = msg.get("createTime", "")
            if self_id and uid == self_id:
                continue
            if since and ct and ct <= since:
                continue
            return msg
        return None

    def list_conversations(
        self, query: str | None = None, limit: int = 20
    ) -> list[dict]:
        service = self._service()
        spaces = service.spaces().list(pageSize=100).execute().get("spaces", [])

        # Sort by lastActiveTime descending (None → end). Chat API does not
        # support `orderBy` on spaces.list, so we sort client-side.
        spaces.sort(key=lambda s: s.get("lastActiveTime") or "", reverse=True)
        cap = max(1, min(limit, 100))
        spaces = spaces[:cap]

        # Fetch members for each space (one call each). Stripped to raw IDs.
        space_member_ids: dict[str, list[str]] = {}
        for space in spaces:
            sid = space["name"]
            try:
                m = (
                    service.spaces()
                    .members()
                    .list(parent=sid, pageSize=50)
                    .execute()
                )
                ids = []
                for mem in m.get("memberships", []):
                    member_name = mem.get("member", {}).get("name", "")
                    if member_name.startswith("users/"):
                        ids.append(member_name.removeprefix("users/"))
                space_member_ids[sid] = ids
            except Exception:  # noqa: BLE001 - membership listing may be restricted
                space_member_ids[sid] = []

        # Fetch the most recent message per space (just the latest one — the
        # preview honestly reflects "what's the last thing said here", even if
        # it's the user's own message; the sender is attributed below).
        latest_msgs: dict[str, dict] = {}
        for space in spaces:
            sid = space["name"]
            msgs = self._latest_message(sid, look_back=1)
            if msgs:
                latest_msgs[sid] = msgs[0]

        # Resolve names for participants AND the senders of the latest messages,
        # in one People API batch.
        all_uids: set[str] = {uid for ids in space_member_ids.values() for uid in ids}
        for msg in latest_msgs.values():
            uid = self._sender_uid(msg)
            if uid:
                all_uids.add(uid)
        self._resolve_names(all_uids)
        self_id = self._ensure_self_id()

        out: list[dict] = []
        for space in spaces:
            sid = space["name"]
            stype = space.get("spaceType") or space.get("type", "SPACE")
            member_ids = space_member_ids.get(sid, [])
            participants = [self._label(uid) for uid in member_ids]

            display = space.get("displayName", "") or ""
            if not display:
                # For DMs and unnamed group chats, build a label from the "other"
                # participants (filter out self when known).
                others = [
                    self._label(uid)
                    for uid in member_ids
                    if not self_id or uid != self_id
                ]
                if stype == "DIRECT_MESSAGE" and others:
                    display = others[0]
                elif others:
                    extra = "" if len(others) <= 3 else f" +{len(others)-3} more"
                    display = ", ".join(others[:3]) + extra
                else:
                    display = sid

            msg = latest_msgs.get(sid, {})
            preview = self._format_preview(
                self._msg_text(msg), self._sender_uid(msg), self_id
            ) if msg else ""

            out.append(
                {
                    "id": sid,
                    "name": display,
                    "type": stype,
                    "participants": participants,
                    "last_message_preview": preview,
                    "last_active_time": space.get("lastActiveTime", ""),
                }
            )

        if query:
            q = query.lower()
            out = [
                c
                for c in out
                if q in c["name"].lower()
                or any(q in p.lower() for p in c["participants"])
            ]
        return out

    def get_messages(self, conversation: str, limit: int = 20) -> list[dict]:
        service = self._service()
        msgs = (
            service.spaces()
            .messages()
            .list(parent=conversation, pageSize=limit, orderBy="createTime desc")
            .execute()
            .get("messages", [])
        )
        # Collect sender IDs and resolve to names in one batch.
        sender_ids = set()
        for msg in msgs:
            sname = msg.get("sender", {}).get("name", "")
            if sname.startswith("users/"):
                sender_ids.add(sname.removeprefix("users/"))
        self._resolve_names(sender_ids)

        out: list[dict] = []
        # Reverse to oldest → newest (consistent with old contract).
        for msg in reversed(msgs):
            sname = msg.get("sender", {}).get("name", "")
            uid = sname.removeprefix("users/") if sname.startswith("users/") else ""
            sender_display = self._label(uid) if uid else sname
            out.append(
                {
                    "id": msg.get("name", ""),
                    "sender": sender_display,
                    "text": msg.get("text", ""),
                    "create_time": msg.get("createTime", ""),
                }
            )
        return out

    def _get_last_read_time(self, conversation: str) -> str:
        """Fetch the user's lastReadTime for a space (empty string on error)."""
        from googleapiclient.errors import HttpError  # type: ignore

        service = self._service()
        try:
            state = (
                service.users()
                .spaces()
                .getSpaceReadState(name=f"users/me/{conversation}/spaceReadState")
                .execute()
            )
            return state.get("lastReadTime", "") or ""
        except HttpError:
            return ""

    def list_unread(self, limit: int = 10) -> list[dict]:
        service = self._service()
        spaces = service.spaces().list(pageSize=100).execute().get("spaces", [])
        spaces.sort(key=lambda s: s.get("lastActiveTime") or "", reverse=True)
        self_id = self._ensure_self_id()
        cap = max(1, min(limit, 50))

        # Walk sorted spaces; for each, find the latest message from someone
        # OTHER than self that's newer than the user's lastReadTime. A space
        # only counts as unread if such a message exists — i.e. there's
        # something from someone else for the user to actually read.
        kept: list[tuple[dict, str, dict]] = []  # (space, last_read, msg)
        for space in spaces:
            if len(kept) >= cap:
                break
            last_active = space.get("lastActiveTime") or ""
            last_read = self._get_last_read_time(space["name"])
            if not last_active or (last_read and last_active <= last_read):
                continue  # no activity at all since last read
            msg = self._pick_unread_message(space["name"], self_id, last_read)
            if msg is None:
                continue  # only self-sent or already-read activity
            kept.append((space, last_read, msg))

        # Fetch members for each surviving space.
        space_member_ids: dict[str, list[str]] = {}
        for space, _, _ in kept:
            sid = space["name"]
            try:
                m = (
                    service.spaces().members()
                    .list(parent=sid, pageSize=50).execute()
                )
                ids = []
                for mem in m.get("memberships", []):
                    member_name = mem.get("member", {}).get("name", "")
                    if member_name.startswith("users/"):
                        ids.append(member_name.removeprefix("users/"))
                space_member_ids[sid] = ids
            except Exception:  # noqa: BLE001
                space_member_ids[sid] = []

        # Batch-resolve names for participants AND each message's sender.
        all_uids: set[str] = {uid for ids in space_member_ids.values() for uid in ids}
        for _, _, msg in kept:
            uid = self._sender_uid(msg)
            if uid:
                all_uids.add(uid)
        self._resolve_names(all_uids)

        out: list[dict] = []
        for space, last_read, msg in kept:
            sid = space["name"]
            stype = space.get("spaceType") or space.get("type", "SPACE")
            member_ids = space_member_ids.get(sid, [])
            participants = [self._label(uid) for uid in member_ids]
            display = space.get("displayName", "") or ""
            if not display:
                others = [
                    self._label(uid) for uid in member_ids
                    if not self_id or uid != self_id
                ]
                if stype == "DIRECT_MESSAGE" and others:
                    display = others[0]
                elif others:
                    extra = "" if len(others) <= 3 else f" +{len(others)-3} more"
                    display = ", ".join(others[:3]) + extra
                else:
                    display = sid
            preview = self._format_preview(
                self._msg_text(msg), self._sender_uid(msg), self_id
            )
            out.append({
                "id": sid, "name": display, "type": stype,
                "participants": participants,
                "last_message_preview": preview,
                "last_active_time": space.get("lastActiveTime", ""),
                "last_read_time": last_read,
                "latest_unread_at": msg.get("createTime", ""),
            })
        return out

    def mark_read(self, conversation: str) -> dict:
        """Update spaceReadState.lastReadTime to NOW.

        Google clamps lastReadTime to the latest message createTime, so this
        effectively marks "everything visible right now" as read.
        """
        import datetime

        from googleapiclient.errors import HttpError  # type: ignore

        service = self._service()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        try:
            updated = (
                service.users()
                .spaces()
                .updateSpaceReadState(
                    name=f"users/me/{conversation}/spaceReadState",
                    updateMask="lastReadTime",
                    body={"lastReadTime": now},
                )
                .execute()
            )
            return {
                "conversation": conversation, "status": "read",
                "last_read_time": updated.get("lastReadTime", now),
            }
        except HttpError as exc:
            return {
                "conversation": conversation, "status": "error",
                "error": str(exc),
            }

    def send_message(self, conversation: str, text: str) -> dict:
        service = self._service()
        msg = (
            service.spaces()
            .messages()
            .create(parent=conversation, body={"text": text})
            .execute()
        )
        return {
            "id": msg.get("name", ""),
            "conversation": conversation,
            "text": msg.get("text", text),
            "create_time": msg.get("createTime", ""),
        }


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def get_backend() -> ChatBackend:
    """Return the active Chat backend.

    Mock when dry-run / no credentials, otherwise the real Google Chat backend.
    This single function is the only place that decides which backend is live —
    swap the alternative implementation in here if the Chat API path fails.
    """
    if settings.use_mock:
        return MockChatBackend()
    return GoogleChatBackend()


# --------------------------------------------------------------------------- #
# Tool functions (wrapped by server.py)
# --------------------------------------------------------------------------- #
def list_conversations(query: str | None = None, limit: int = 20) -> list[dict]:
    return get_backend().list_conversations(query, limit)


def get_messages(conversation: str, limit: int = 20) -> list[dict]:
    return get_backend().get_messages(conversation, limit)


def send_message(conversation: str, text: str) -> dict:
    return get_backend().send_message(conversation, text)


def draft_message(conversation: str, text: str) -> dict:
    """Store a chat draft locally for review in the dashboard (does not send)."""
    return drafts.add_draft("chat", {"conversation": conversation, "text": text})


def list_unread(limit: int = 10) -> list[dict]:
    return get_backend().list_unread(limit)


def mark_read(conversation: str) -> dict:
    return get_backend().mark_read(conversation)
