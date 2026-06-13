"""Smoke tests — verify the system imports and runs in mock mode.

These run without any Google credentials (DRY_RUN defaults to true), so CI and
local dev can validate wiring before the service-account access test concludes.
"""

import os

os.environ.setdefault("DRY_RUN", "true")

from iblu_keeper.config import settings  # noqa: E402
from iblu_keeper.tools import calendar as calendar_tools  # noqa: E402
from iblu_keeper.tools import chat as chat_tools  # noqa: E402
from iblu_keeper.tools import context as context_tools  # noqa: E402
from iblu_keeper.tools import gmail as gmail_tools  # noqa: E402


def test_mock_mode_only_when_dry_run():
    # Mock mode is driven SOLELY by DRY_RUN (set true at module top), never a
    # silent fallback when credentials are missing.
    assert settings.use_mock is True
    assert settings.dry_run is True


def test_no_silent_mock_when_misconfigured(monkeypatch):
    """If DRY_RUN=false but no token, the server must NOT silently mock."""
    from iblu_keeper.config import Settings

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_FILE", "/nonexistent/iblu/token.json")
    s = Settings()
    assert s.use_mock is False          # not mocking
    assert s.misconfigured_live is True  # flagged as broken, will fail loudly


def test_chat_list_and_messages():
    convs = chat_tools.list_conversations()
    assert convs and "id" in convs[0]
    msgs = chat_tools.get_messages(convs[0]["id"], limit=2)
    assert len(msgs) <= 2
    assert all({"sender", "text"} <= m.keys() for m in msgs)


def test_chat_search_by_name():
    # Search matches by conversation name OR participant name.
    results = chat_tools.list_conversations("marta")
    assert results
    assert all(
        "marta" in r["name"].lower()
        or any("marta" in p.lower() for p in r["participants"])
        for r in results
    )
    assert any("marta" in r["name"].lower() for r in results)


def test_chat_send_is_mocked():
    out = chat_tools.send_message("spaces/MOCK_AAAA", "hi")
    assert out.get("_mock") is True
    assert out["status"] == "not_sent_mock"  # no false-positive "sent"


def test_chat_draft_persisted():
    draft = chat_tools.draft_message("spaces/MOCK_AAAA", "draft text")
    assert draft["kind"] == "chat" and draft["status"] == "pending"


def test_gmail_search_and_get():
    results = gmail_tools.search("test")
    assert results and "subject" in results[0]
    msg = gmail_tools.get_message(results[0]["id"])
    assert "body" in msg


def test_gmail_send_is_mocked():
    out = gmail_tools.send_email("a@b.com", "subj", "body")
    # Mock send must NOT claim it was sent — that was the false-positive bug.
    assert out.get("_mock") is True and out["status"] == "not_sent_mock"


def test_gmail_search_mock_is_flagged():
    # Mock search results must be clearly tagged so they can't be mistaken for live.
    for m in gmail_tools.search("anything"):
        assert m.get("_mock") is True


def test_calendar_create_is_mocked():
    out = calendar_tools.create_event("t", "2026-06-11T14:00:00+03:00", "2026-06-11T14:30:00+03:00")
    assert out.get("_mock") is True and out["title"] == "t"
    assert out["status"] == "not_created_mock"


def test_context_stubs():
    assert context_tools.log_conversation("c", "user", "hi")["status"] == "stub"
    assert context_tools.get_summary("1d")["status"] == "stub"


def test_server_imports_and_registers_tools():
    # Importing the server builds the FastMCP app and ASGI app at module load.
    from iblu_keeper import server

    assert server.app is not None
    assert server.mcp is not None
