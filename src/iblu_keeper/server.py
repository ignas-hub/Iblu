"""FastMCP server exposing Chat / Gmail / Calendar (+ Phase 2 context stubs).

Run locally:        python -m iblu_keeper.server
Or via console:     iblu-mcp

The server speaks streamable-HTTP so Claude (claude.ai custom connector /
Claude apps) can connect over HTTPS. Authentication is Google OAuth via
FastMCP's GoogleProvider — Claude.ai performs Dynamic Client Registration,
then the human signs into Google. The OAuth consent screen is configured
"Internal" in Google Cloud, so only blanklabel.team Workspace users can
complete the flow.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider

from .config import settings
from .tools import calendar as calendar_tools
from .tools import chat as chat_tools
from .tools import context as context_tools
from .tools import gmail as gmail_tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("iblu_keeper.server")


def _build_auth() -> GoogleProvider | None:
    """Build the Google OAuth provider, or None if credentials/base URL are missing."""
    if not (
        settings.google_oauth_client_id
        and settings.google_oauth_client_secret
        and settings.mcp_public_base_url
    ):
        logger.warning(
            "Google OAuth client_id/secret or MCP_PUBLIC_BASE_URL missing — "
            "server will run UNAUTHENTICATED. Set them in .env."
        )
        return None
    return GoogleProvider(
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        base_url=settings.mcp_public_base_url,
        required_scopes=["openid", "email"],
    )


mcp = FastMCP(
    name="iblu-keeper",
    instructions=(
        "Personal assistant tools for Ignas: read/send Google Chat, "
        "read/draft/send Gmail, and create Calendar events. Identify Chat "
        "conversations primarily by the person's name. Use draft_* tools when a "
        "human should review before anything is sent."
    ),
    auth=_build_auth(),
)


# --------------------------------------------------------------------------- #
# Google Chat
# --------------------------------------------------------------------------- #
@mcp.tool(name="chat.list_conversations")
def chat_list_conversations(query: str | None = None) -> list[dict]:
    """List recent Chat conversations/spaces. Filter by a person's name via `query`."""
    return chat_tools.list_conversations(query)


@mcp.tool(name="chat.get_messages")
def chat_get_messages(conversation: str, limit: int = 20) -> list[dict]:
    """Get message history for a conversation (use an id from chat.list_conversations)."""
    return chat_tools.get_messages(conversation, limit)


@mcp.tool(name="chat.send_message")
def chat_send_message(conversation: str, text: str) -> dict:
    """Send a Chat message to a conversation."""
    return chat_tools.send_message(conversation, text)


@mcp.tool(name="chat.draft_message")
def chat_draft_message(conversation: str, text: str) -> dict:
    """Store a Chat draft for human review (does NOT send)."""
    return chat_tools.draft_message(conversation, text)


# --------------------------------------------------------------------------- #
# Gmail
# --------------------------------------------------------------------------- #
@mcp.tool(name="gmail.search")
def gmail_search(query: str, limit: int = 20) -> list[dict]:
    """Search Gmail using standard Gmail query syntax (e.g. 'from:bob is:unread')."""
    return gmail_tools.search(query, limit)


@mcp.tool(name="gmail.get_message")
def gmail_get_message(id: str) -> dict:
    """Fetch a single email (headers + plain-text body) by message id."""
    return gmail_tools.get_message(id)


@mcp.tool(name="gmail.draft_email")
def gmail_draft_email(to: str, subject: str, body: str) -> dict:
    """Create a Gmail draft for human review (does NOT send)."""
    return gmail_tools.draft_email(to, subject, body)


@mcp.tool(name="gmail.send_email")
def gmail_send_email(to: str, subject: str, body: str) -> dict:
    """Send an email immediately."""
    return gmail_tools.send_email(to, subject, body)


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #
@mcp.tool(name="calendar.create_event")
def calendar_create_event(
    title: str, start: str, end: str, description: str | None = None
) -> dict:
    """Create a calendar event. start/end are RFC 3339 timestamps with offset."""
    return calendar_tools.create_event(title, start, end, description)


# --------------------------------------------------------------------------- #
# Context / memory — Phase 2 stubs (interfaces stable now)
# --------------------------------------------------------------------------- #
@mcp.tool(name="context.log_conversation")
def context_log_conversation(
    conversation: str, role: str, text: str, source: str = "chat"
) -> dict:
    """[Phase 2 stub] Record a conversation turn for long-term memory."""
    return context_tools.log_conversation(conversation, role, text, source)


@mcp.tool(name="context.get_summary")
def context_get_summary(window: str = "1d") -> dict:
    """[Phase 2 stub] Summarize recent activity over a time window (e.g. '1d')."""
    return context_tools.get_summary(window)


# --------------------------------------------------------------------------- #
# Health endpoint (unauthenticated) — used by the dashboard status page
# --------------------------------------------------------------------------- #
@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "iblu-keeper",
            "mode": "mock" if settings.use_mock else "live",
            "dry_run": settings.dry_run,
            "has_google_credentials": settings.has_google_credentials,
        }
    )


@mcp.custom_route("/", methods=["GET"])
async def root(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("iblu-keeper MCP server. See /health.")


def build_app():
    """Build the Starlette ASGI app (MCP over streamable-HTTP + OAuth)."""
    return mcp.http_app()


# ASGI entrypoint for `uvicorn iblu_keeper.server:app`
app = build_app()


def main() -> None:
    import uvicorn

    if settings.use_mock:
        logger.warning(
            "Running in MOCK mode (dry_run=%s, has_creds=%s). Tools return fake data.",
            settings.dry_run,
            settings.has_google_credentials,
        )

    uvicorn.run(
        "iblu_keeper.server:app",
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
