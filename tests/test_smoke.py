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


# --------------------------------------------------------------------------- #
# Freshness / anti-replay acceptance tests (matches the spec dated 2026-06-15)
# --------------------------------------------------------------------------- #
def test_A_envelope_stamps_every_response():
    """A. Every tool response carries fetched_at + a unique request_id, and
    consecutive calls produce different request_ids with advancing timestamps.
    """
    from iblu_keeper.envelope import stamped

    @stamped
    def fake_list(limit=2):
        return [{"id": "x"}, {"id": "y"}]

    @stamped
    def fake_dict(to):
        return {"id": "abc", "status": "sent"}

    r1 = fake_list(limit=2)
    r2 = fake_list(limit=2)
    assert r1["request_id"] != r2["request_id"], "Each call must get a fresh request_id"
    assert r1["fetched_at"] <= r2["fetched_at"], "fetched_at must advance monotonically"
    assert r1["count"] == 2 and r1["items"] == [{"id": "x"}, {"id": "y"}]
    assert r1["query"] == {"limit": 2}, "Query echo must reflect actual kwargs"

    d = fake_dict(to="a@b.com")
    # Dict-returning tool: envelope merged at top level
    assert {"fetched_at", "request_id", "query", "id", "status"} <= d.keys()
    assert d["query"] == {"to": "a@b.com"}


def test_D_query_echo_in_envelope():
    """D. The server echoes the parameters it actually used."""
    from iblu_keeper.envelope import stamped

    @stamped
    def my_search(query: str, limit: int = 5):
        return [{"hit": query}]

    out = my_search(query="alexan@sparkleadgo.com", limit=3)
    assert out["query"] == {"query": "alexan@sparkleadgo.com", "limit": 3}
    assert out["items"] == [{"hit": "alexan@sparkleadgo.com"}]


def test_F_no_false_success_on_write_failure(monkeypatch):
    """F. When the Google API returns no resource id, the tool MUST raise
    rather than return a success-shaped payload."""
    import iblu_keeper.tools.gmail as gmail_real
    import iblu_keeper.tools.calendar as cal_real
    import iblu_keeper.tools.chat as chat_real

    # Force live path (skip the mock branch) so the strict id-check kicks in.
    # Settings is a frozen dataclass — substitute a stub with use_mock=False.
    class _Live:
        use_mock = False
        dry_run = False
        google_user_email = "ignas@blanklabel.team"

    monkeypatch.setattr(gmail_real, "settings", _Live())
    monkeypatch.setattr(cal_real, "settings", _Live())
    monkeypatch.setattr(chat_real, "settings", _Live())

    class FakeExec:
        def execute(self):  # Google returns no id — simulates a failure mode.
            return {}

    class FakeMessages:
        def send(self, **_): return FakeExec()
        def create(self, **_): return FakeExec()

    class FakeUsers:
        def messages(self): return FakeMessages()

    class FakeGmailService:
        def users(self): return FakeUsers()

    class FakeEvents:
        def insert(self, **_): return FakeExec()

    class FakeCalService:
        def events(self): return FakeEvents()

    class FakeSpacesMessages:
        def create(self, **_): return FakeExec()

    class FakeSpaces:
        def messages(self): return FakeSpacesMessages()

    class FakeChatService:
        def spaces(self): return FakeSpaces()

    monkeypatch.setattr(gmail_real, "_service", lambda: FakeGmailService())
    monkeypatch.setattr(cal_real, "_service", lambda: FakeCalService())

    # gmail.send_email
    import pytest
    with pytest.raises(RuntimeError, match="no message id"):
        gmail_real.send_email("a@b.com", "s", "b")
    # calendar.create_event
    with pytest.raises(RuntimeError, match="no event id"):
        cal_real.create_event("t", "2026-06-15T10:00:00Z", "2026-06-15T11:00:00Z")
    # chat.send_message — patch the backend's _service directly
    backend = chat_real.GoogleChatBackend()
    backend._service = lambda: FakeChatService()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="no message name"):
        backend.send_message("spaces/X", "hi")


def test_C_cache_control_no_store_header():
    """C. Every HTTP response sets Cache-Control: no-store."""
    from starlette.testclient import TestClient
    from iblu_keeper.server import app

    with TestClient(app) as client:
        for path in ["/health", "/health?probe=1", "/"]:
            r = client.get(path)
            assert r.headers.get("cache-control") == "no-store", (
                f"{path} missing Cache-Control: no-store "
                f"(got {r.headers.get('cache-control')!r})"
            )


def test_server_health_contains_server_time_and_auth():
    """The server_health tool returns server_time (the spec's clock endpoint)."""
    from iblu_keeper.server import server_health

    # The tool is wrapped by @stamped → @mcp.tool. Call the underlying
    # function via __wrapped__ if available, else the FastMCP tool wraps
    # `.fn`. Either way we just want the dict it produces.
    result = server_health() if callable(server_health) else None
    if result is None:
        # If FastMCP made it non-callable, exercise through list_tools.
        import asyncio
        from iblu_keeper.server import mcp
        async def _go():
            tools = await mcp.list_tools()
            t = next(t for t in tools if t.name == "server_health")
            return t
        t = asyncio.run(_go())
        assert t is not None
        return
    assert "server_time" in result
    assert "auth" in result
    assert result["mode"] in ("live", "mock")
