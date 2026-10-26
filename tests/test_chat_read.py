"""chat_read / chat_reply — request shapes, client-side search, and the rule
that nothing posts without an explicit space.

The live backend is replaced by `_FakeBackend`, which exposes the same private
seams `GoogleChatBackend` does (`_service`, `_people`, `_resolve_names`,
`_label`, `_ensure_self_id`, `_get_last_read_time`) over a `Recorder`.
"""

from __future__ import annotations

import pytest
from _fake_google import Recorder

from iblu_keeper.tools import chat_read as CR

SPACE = "spaces/AAAA"


def _m(mid: str, text: str, sender: str, when: str, thread: str = "spaces/AAAA/threads/T1") -> dict:
    return {
        "name": f"{SPACE}/messages/{mid}", "text": text,
        "sender": {"name": f"users/{sender}", "type": "HUMAN"},
        "createTime": when, "thread": {"name": thread},
    }


class _FakeBackend:
    def __init__(self, responses=None, people=None):
        self.service = Recorder(responses)
        self.people = Recorder(people)
        self.resolved: set[str] = set()
        self.names = {"u1": "Jonas", "me": "Ignas"}
        self.read_times: dict[str, str] = {}

    def _service(self):
        return self.service

    def _people(self):
        return self.people

    def _resolve_names(self, uids):
        self.resolved |= set(uids)

    def _label(self, uid):
        return self.names.get(uid, f"users/{uid}")

    def _ensure_self_id(self):
        return "me"

    def _get_last_read_time(self, sid):
        return self.read_times.get(sid, "")


class _Live:
    use_mock = False
    dry_run = False


@pytest.fixture
def live(monkeypatch):
    fake = _FakeBackend()
    monkeypatch.setattr(CR, "settings", _Live())
    monkeypatch.setattr(CR, "_backend", lambda account: fake)
    return fake


# --- refusals --------------------------------------------------------------


def test_unknown_action_is_refused():
    """A typo must not fall through to a default read of some space."""
    with pytest.raises(CR.ChatReadError, match="unknown action"):
        CR.chat_read("threads", space=SPACE)


def test_space_actions_refuse_without_a_space():
    """There is no default space — least of all the Secretary space."""
    for action in ("thread", "members", "space"):
        with pytest.raises(CR.ChatReadError, match="no default space"):
            CR.chat_read(action, thread="T1")


def test_thread_requires_a_thread():
    """A thread read without a thread would silently become 'recent messages'."""
    with pytest.raises(CR.ChatReadError, match="thread is required"):
        CR.chat_read("thread", space=SPACE)


def test_search_requires_a_query():
    """An empty query matches everything and returns the newest 20 of a space, which is not a search."""
    with pytest.raises(CR.ChatReadError, match="query"):
        CR.chat_read("search", space=SPACE, query="  ")


# --- mock mode -------------------------------------------------------------


def test_mock_mode_never_touches_the_backend(monkeypatch):
    """DRY_RUN must not build a backend; the status must say nothing was read."""
    def boom(account):
        raise AssertionError("backend built in mock mode")

    monkeypatch.setattr(CR, "_backend", boom)
    out = CR.chat_read("members", space="AAAA")
    assert out["_mock"] is True
    assert out["status"] == "not_read_mock"
    assert out["space"] == SPACE


def test_mock_reply_sends_nothing(monkeypatch):
    """A mock reply that reached the backend would be a real post in DRY_RUN."""
    def boom(account):
        raise AssertionError("backend built in mock mode")

    monkeypatch.setattr(CR, "_backend", boom)
    out = CR.chat_reply(SPACE, "T1", "hello")
    assert out["status"] == "not_sent_mock"
    assert out["thread"] == f"{SPACE}/threads/T1"


# --- thread ----------------------------------------------------------------


def test_thread_filters_by_thread_name_and_returns_oldest_first(live):
    """Without the thread filter this is the whole space; without the sort it is newest-first."""
    live.service.responses["spaces.messages.list"] = {"messages": [
        _m("2", "second", "me", "2026-06-09T09:17:00Z"),
        _m("1", "first", "u1", "2026-06-09T09:15:00Z"),
    ]}
    out = CR.chat_read("thread", space="AAAA", thread="T1", limit=10)
    kw = live.service.calls_to("spaces.messages.list")[0]
    assert kw["parent"] == SPACE
    assert kw["filter"] == f"thread.name = {SPACE}/threads/T1"
    assert kw["pageSize"] == 10
    assert [m["text"] for m in out["messages"]] == ["first", "second"]
    assert out["messages"][0]["sender"] == "Jonas"
    assert out["messages"][1]["is_me"] is True
    assert live.resolved == {"u1", "me"}


def test_thread_accepts_a_full_thread_resource_name(live):
    """Chat hands back 'spaces/X/threads/Y'; forcing the caller to strip it is a trap."""
    live.service.responses["spaces.messages.list"] = {"messages": []}
    CR.chat_read("thread", space=SPACE, thread=f"{SPACE}/threads/T9")
    assert live.service.calls_to("spaces.messages.list")[0]["filter"] == f"thread.name = {SPACE}/threads/T9"


# --- search ----------------------------------------------------------------


def test_search_matches_all_words_client_side_newest_first(live):
    """Chat has no server-side text search for a user token; the match has to happen here."""
    live.service.responses["spaces.messages.list"] = {"messages": [
        _m("1", "Sent the INVOICE for Acme", "u1", "2026-06-01T09:00:00Z"),
        _m("2", "invoice paid, acme happy", "me", "2026-06-03T09:00:00Z"),
        _m("3", "lunch?", "u1", "2026-06-02T09:00:00Z"),
        _m("4", "acme call", "u1", "2026-06-04T09:00:00Z"),
    ]}
    out = CR.chat_read("search", space=SPACE, query="Acme invoice", limit=5, days=7)
    kw = live.service.calls_to("spaces.messages.list")[0]
    assert kw["filter"].startswith('createTime > "') and kw["filter"].endswith('Z"')
    assert kw["orderBy"] == "createTime desc"
    assert [m["id"].rsplit("/", 1)[1] for m in out["items"]] == ["2", "1"]
    assert out["spaces_scanned"] == 1
    assert "no server-side text search" in out["note"]


def test_search_without_space_scans_the_most_active_spaces(live):
    """Fanning out to every space ever joined is a quota problem; the cap and the order matter."""
    spaces = [{"name": f"spaces/S{i}", "lastActiveTime": f"2026-06-{i:02d}T00:00:00Z"} for i in range(1, 26)]
    live.service.responses["spaces.list"] = {"spaces": spaces}
    live.service.responses["spaces.messages.list"] = lambda kw: {"messages": [
        _m("x", "needle", "u1", "2026-06-10T00:00:00Z", thread=f"{kw['parent']}/threads/t"),
    ]} if kw["parent"] == "spaces/S25" else {"messages": []}
    out = CR.chat_read("search", query="needle", limit=10)
    scanned = {kw["parent"] for kw in live.service.calls_to("spaces.messages.list")}
    assert len(scanned) == CR.SEARCH_SPACE_CAP == 20
    assert "spaces/S25" in scanned and "spaces/S1" not in scanned
    assert out["count"] == 1


def test_search_respects_limit(live):
    """Twenty hits in a chatty space must not become twenty screens."""
    live.service.responses["spaces.messages.list"] = {"messages": [
        _m(str(i), "needle", "u1", f"2026-06-{i:02d}T00:00:00Z") for i in range(1, 10)
    ]}
    out = CR.chat_read("search", space=SPACE, query="needle", limit=3)
    assert out["count"] == 3


# --- members / space / my_spaces --------------------------------------------


def test_members_resolves_names_and_emails_via_people(live):
    """Chat returns users/<id>; a member list of opaque ids answers nothing."""
    live.service.responses["spaces.members.list"] = {"memberships": [
        {"member": {"name": "users/u1", "type": "HUMAN"}, "role": "ROLE_MEMBER", "state": "JOINED"},
        {"member": {"name": "users/ext", "type": "HUMAN"}, "role": "ROLE_MANAGER", "state": "JOINED"},
    ]}
    live.people.responses["people.getBatchGet"] = {"responses": [
        {"requestedResourceName": "people/u1", "person": {
            "names": [{"displayName": "Jonas P"}], "emailAddresses": [{"value": "jonas@x.com"}],
        }},
        {"requestedResourceName": "people/ext", "person": {}},
    ]}
    out = CR.chat_read("members", space=SPACE)
    assert live.people.calls_to("people.getBatchGet")[0]["resourceNames"] == ["people/u1", "people/ext"]
    assert out["members"][0] == {
        "user_id": "u1", "name": "Jonas P", "email": "jonas@x.com",
        "type": "HUMAN", "role": "ROLE_MEMBER", "state": "JOINED", "joined": "",
    }
    assert out["members"][1]["email"] == ""  # directory did not share it; never invented


def test_space_reports_threading_state_and_member_count(live):
    """Threading state decides whether chat_reply can thread at all."""
    live.service.responses["spaces.get"] = {
        "name": SPACE, "displayName": "Sales", "spaceType": "SPACE",
        "spaceThreadingState": "THREADED_MESSAGES", "createTime": "2025-01-01T00:00:00Z",
        "membershipCount": {"joinedDirectHumanUserCount": 4, "joinedGroupCount": 1},
    }
    out = CR.chat_read("space", space=SPACE)
    assert live.service.calls_to("spaces.get") == [{"name": SPACE}]
    assert out["display_name"] == "Sales"
    assert out["threading_state"] == "THREADED_MESSAGES"
    assert out["member_count"] == 5


def test_my_spaces_pairs_each_space_with_its_last_read_time(live):
    """has_unread is the one bit that tells him whether to open the space."""
    live.service.responses["spaces.list"] = {"spaces": [
        {"name": "spaces/A", "displayName": "A", "spaceType": "SPACE", "lastActiveTime": "2026-06-10T00:00:00Z"},
        {"name": "spaces/B", "spaceType": "DIRECT_MESSAGE", "lastActiveTime": "2026-06-11T00:00:00Z"},
    ]}
    live.read_times = {"spaces/A": "2026-06-12T00:00:00Z", "spaces/B": "2026-06-01T00:00:00Z"}
    out = CR.chat_read("my_spaces", limit=5)
    by_id = {s["id"]: s for s in out["spaces"]}
    assert [s["id"] for s in out["spaces"]] == ["spaces/B", "spaces/A"]  # most active first
    assert by_id["spaces/A"]["has_unread"] is False
    assert by_id["spaces/B"]["has_unread"] is True
    assert by_id["spaces/B"]["last_read_time"] == "2026-06-01T00:00:00Z"


# --- chat_reply ------------------------------------------------------------


def test_reply_posts_into_the_thread_with_fallback_option(live):
    """Without messageReplyOption the post lands as a new top-level message, not a reply."""
    live.service.responses["spaces.messages.create"] = {
        "name": f"{SPACE}/messages/new1", "text": "on it", "createTime": "2026-06-10T12:00:00Z",
        "thread": {"name": f"{SPACE}/threads/T1"},
    }
    out = CR.chat_reply("AAAA", "T1", "on it")
    assert live.service.calls_to("spaces.messages.create") == [{
        "parent": SPACE,
        "messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
        "body": {"text": "on it", "thread": {"name": f"{SPACE}/threads/T1"}},
    }]
    assert out["status"] == "sent"
    assert out["fell_back_to_new_thread"] is False


def test_reply_reports_when_chat_started_a_new_thread(live):
    """If the thread was gone the message still posted — somewhere else — and he should know."""
    live.service.responses["spaces.messages.create"] = {
        "name": f"{SPACE}/messages/new2", "thread": {"name": f"{SPACE}/threads/FRESH"},
    }
    out = CR.chat_reply(SPACE, "T1", "late reply")
    assert out["thread"] == f"{SPACE}/threads/FRESH"
    assert out["fell_back_to_new_thread"] is True


def test_reply_refuses_empty_space_thread_or_text(live):
    """Each missing piece would otherwise post the wrong thing to the wrong place, or nothing at all."""
    with pytest.raises(CR.ChatReadError, match="no default space"):
        CR.chat_reply("", "T1", "hi")
    with pytest.raises(CR.ChatReadError, match="thread is required"):
        CR.chat_reply(SPACE, "", "hi")
    with pytest.raises(CR.ChatReadError, match="non-empty text"):
        CR.chat_reply(SPACE, "T1", "   ")
    assert live.service.calls_to("spaces.messages.create") == []


def test_reply_without_a_message_name_is_a_failure_not_a_success(live):
    """A 'sent' with no resource name would be a false confirmation."""
    live.service.responses["spaces.messages.create"] = {}
    with pytest.raises(RuntimeError, match="no message name"):
        CR.chat_reply(SPACE, "T1", "hi")
