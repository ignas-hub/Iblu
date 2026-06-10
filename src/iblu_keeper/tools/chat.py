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
    def list_conversations(self, query: str | None = None) -> list[dict]:
        """Recent conversations/spaces with participant names.

        `query` filters by person/space name (case-insensitive substring).
        Each item: {id, name, type, participants: [..], last_message_preview}.
        """

    @abc.abstractmethod
    def get_messages(self, conversation: str, limit: int = 20) -> list[dict]:
        """Message history for a conversation, oldest→newest.

        Each item: {id, sender, text, create_time}.
        """

    @abc.abstractmethod
    def send_message(self, conversation: str, text: str) -> dict:
        """Send a message. Returns {id, conversation, text, create_time}."""


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

    def list_conversations(self, query: str | None = None) -> list[dict]:
        if not query:
            return list(self._CONVERSATIONS)
        q = query.lower()
        return [
            c
            for c in self._CONVERSATIONS
            if q in c["name"].lower()
            or any(q in p.lower() for p in c["participants"])
        ]

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


# --------------------------------------------------------------------------- #
# Google Chat backend (real API)
# --------------------------------------------------------------------------- #
class GoogleChatBackend(ChatBackend):
    """Real Google Chat API backend via the delegated service account.

    NOTE: pending validation that service-account + Chat API can access Ignas's
    personal DMs/spaces. If that path proves unworkable, implement an
    alternative ChatBackend and switch `get_backend()` to return it.
    """

    def _service(self):
        from ..google_auth import build_service

        return build_service("chat", "v1")

    def list_conversations(self, query: str | None = None) -> list[dict]:
        service = self._service()
        resp = service.spaces().list(pageSize=100).execute()
        out: list[dict] = []
        for space in resp.get("spaces", []):
            display = space.get("displayName") or space.get("name", "")
            members: list[str] = []
            try:
                m = (
                    service.spaces()
                    .members()
                    .list(parent=space["name"], pageSize=50)
                    .execute()
                )
                for member in m.get("memberships", []):
                    member_name = (
                        member.get("member", {}).get("displayName")
                        or member.get("member", {}).get("name", "")
                    )
                    if member_name:
                        members.append(member_name)
            except Exception:  # noqa: BLE001 - membership listing may be restricted
                pass
            out.append(
                {
                    "id": space.get("name", ""),
                    "name": display,
                    "type": space.get("spaceType") or space.get("type", "SPACE"),
                    "participants": members,
                    "last_message_preview": "",
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
        resp = (
            service.spaces()
            .messages()
            .list(parent=conversation, pageSize=limit)
            .execute()
        )
        out: list[dict] = []
        for msg in resp.get("messages", []):
            out.append(
                {
                    "id": msg.get("name", ""),
                    "sender": msg.get("sender", {}).get("displayName", ""),
                    "text": msg.get("text", ""),
                    "create_time": msg.get("createTime", ""),
                }
            )
        return out

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
def list_conversations(query: str | None = None) -> list[dict]:
    return get_backend().list_conversations(query)


def get_messages(conversation: str, limit: int = 20) -> list[dict]:
    return get_backend().get_messages(conversation, limit)


def send_message(conversation: str, text: str) -> dict:
    return get_backend().send_message(conversation, text)


def draft_message(conversation: str, text: str) -> dict:
    """Store a chat draft locally for review in the dashboard (does not send)."""
    return drafts.add_draft("chat", {"conversation": conversation, "text": text})
