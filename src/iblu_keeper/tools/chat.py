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

        # Resolve display names for every unique participant in one batch.
        self._resolve_names({uid for ids in space_member_ids.values() for uid in ids})
        self_id = self._ensure_self_id()

        # Fetch the latest message per space for `last_message_preview`.
        previews: dict[str, str] = {}
        for space in spaces:
            sid = space["name"]
            try:
                msgs = (
                    service.spaces()
                    .messages()
                    .list(parent=sid, pageSize=1, orderBy="createTime desc")
                    .execute()
                    .get("messages", [])
                )
                text = (msgs[0].get("text", "") if msgs else "") or ""
                if not text and msgs and msgs[0].get("attachment"):
                    text = "[attachment]"
                previews[sid] = text[: self._PREVIEW_MAX]
            except Exception:  # noqa: BLE001
                previews[sid] = ""

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

            out.append(
                {
                    "id": sid,
                    "name": display,
                    "type": stype,
                    "participants": participants,
                    "last_message_preview": previews.get(sid, ""),
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

        # Filter to spaces with new activity since lastReadTime. Walk the
        # sorted list and stop as soon as we have `limit` unread ones.
        unread_spaces: list[dict] = []
        read_times: dict[str, str] = {}
        for space in spaces:
            if len(unread_spaces) >= max(1, min(limit, 50)):
                break
            last_active = space.get("lastActiveTime") or ""
            last_read = self._get_last_read_time(space["name"])
            read_times[space["name"]] = last_read
            if last_active and (not last_read or last_active > last_read):
                unread_spaces.append(space)

        # Reuse the same enrichment as list_conversations: members + names +
        # previews. Done here inline rather than calling list_conversations to
        # avoid re-fetching/re-sorting everything.
        space_member_ids: dict[str, list[str]] = {}
        for space in unread_spaces:
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

        self._resolve_names({uid for ids in space_member_ids.values() for uid in ids})
        self_id = self._ensure_self_id()

        previews: dict[str, str] = {}
        for space in unread_spaces:
            sid = space["name"]
            try:
                msgs = (
                    service.spaces().messages()
                    .list(parent=sid, pageSize=1, orderBy="createTime desc")
                    .execute().get("messages", [])
                )
                text = (msgs[0].get("text", "") if msgs else "") or ""
                if not text and msgs and msgs[0].get("attachment"):
                    text = "[attachment]"
                previews[sid] = text[: self._PREVIEW_MAX]
            except Exception:  # noqa: BLE001
                previews[sid] = ""

        out: list[dict] = []
        for space in unread_spaces:
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
            out.append({
                "id": sid, "name": display, "type": stype,
                "participants": participants,
                "last_message_preview": previews.get(sid, ""),
                "last_active_time": space.get("lastActiveTime", ""),
                "last_read_time": read_times.get(sid, ""),
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
