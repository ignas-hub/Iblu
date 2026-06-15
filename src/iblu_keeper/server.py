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
from typing import Annotated

from pydantic import Field
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider

from .config import settings
from .envelope import now_iso, stamped
from .google_errors import with_google_errors
from .tools import calendar as calendar_tools
from .tools import chat as chat_tools
from .tools import context as context_tools
from .tools import gmail as gmail_tools


# Standardised line appended to every read tool's docstring so the model is
# reminded — every turn, in its prompt — to call fresh and quote fetched_at.
_FRESHNESS_LINE = (
    " Returns live data fetched at call time. Always call again for current "
    "state; never reuse a previous result. Response includes fetched_at and "
    "request_id — report fetched_at to the user."
)


class _NoStoreMiddleware(BaseHTTPMiddleware):
    """Force ``Cache-Control: no-store`` on every response so neither browsers
    nor intermediaries (Cloudflare etc.) can cache tool output."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

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
        "read/draft/send Gmail, read attachments + Google Docs, and create "
        "Calendar events. Identify Chat conversations primarily by the "
        "person's name. Use draft_* tools when a human should review before "
        "anything is sent. "
        "\n\n"
        "FRESHNESS PROTOCOL — read this carefully, it is the most common "
        "source of complaints. Ignas's inbox, chat, and calendar change "
        "constantly (every few minutes). When he asks any question about "
        "current state — 'what's unread', 'latest emails', 'recent messages', "
        "'today's calendar', 'has X replied', 'show me my chats with Tamara' "
        "— you MUST call the relevant tool FRESH at the moment of the "
        "question. **NEVER quote a previous tool result from earlier in this "
        "same conversation as if it were still current**, even if you ran "
        "the same tool five minutes ago — the data is already stale. If "
        "Ignas asks a similar question twice, call the tool both times. "
        "Briefly acknowledge the refresh ('let me check now…') so he sees "
        "it's not cached. The only exception is acting on a specific item "
        "he just referenced ('reply to that one', 'mark it read') — there "
        "the message_id from the most recent fetch is fine to reuse. When "
        "in doubt, re-fetch."
        "\n\n"
        "HEALTH-CHECK PROTOCOL — Ignas is non-developer and should not have "
        "to diagnose this himself. If he says results look stale, mocked, "
        "fake, days-old, surprising, or 'wrong' — OR if you notice a result "
        "that contains `_mock: true` or `status: not_sent_mock` / "
        "`not_created_mock` — call `server_health` FIRST, before anything "
        "else. If it returns `mode: mock` or `auth.ok: false`, tell Ignas in "
        "plain language that the server has lost authentication or is in "
        "mock mode, name the specific error, and STOP — do not try other "
        "tools to work around it. If `server_health` returns `mode: live` "
        "and `auth.ok: true`, the server is genuinely live; if results "
        "still look off after that, re-run the read tool fresh (see "
        "FRESHNESS PROTOCOL) before concluding anything is wrong."
    ),
    auth=_build_auth(),
)


# --------------------------------------------------------------------------- #
# Google Chat
# --------------------------------------------------------------------------- #
@mcp.tool(name="chat_list_conversations", annotations={"title": "List Chat Conversations", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("chat_list_conversations")
def chat_list_conversations(
    query: Annotated[str | None, Field(default=None, description="Case-insensitive substring filter on space name or participants")] = None,
    limit: Annotated[int, Field(default=20, ge=1, le=100, description="Max items to return")] = 20
) -> list[dict]:
    """List recent Chat conversations/spaces, most recently active first.

    Returns up to `limit` items (default 20, max 100). Filter by a person's
    name via `query` (case-insensitive substring match on conversation name or
    participants). Each item includes a `last_message_preview` snippet.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    return chat_tools.list_conversations(query, limit)


@mcp.tool(name="chat_get_messages", annotations={"title": "Get Chat Messages", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("chat_get_messages")
def chat_get_messages(
    conversation: Annotated[str, Field(min_length=1, pattern=r"^spaces/.+", description="Space resource name, e.g. spaces/AAAAxxxxx")],
    limit: Annotated[int, Field(default=20, ge=1, le=100)] = 20,
) -> list[dict]:
    """Get message history for a conversation (use an id from chat_list_conversations).

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    return chat_tools.get_messages(conversation, limit)


@mcp.tool(name="chat_send_message", annotations={"title": "Send Chat Message", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
@stamped
@with_google_errors("chat_send_message")
def chat_send_message(
    conversation: Annotated[str, Field(min_length=1, pattern=r"^spaces/.+", description="Target space, e.g. spaces/AAAAxxxxx")],
    text: Annotated[str, Field(min_length=1, max_length=4096, description="Message body")],
) -> dict:
    """Send a Chat message to a conversation."""
    return chat_tools.send_message(conversation, text)


@mcp.tool(name="chat_draft_message", annotations={"title": "Draft Chat Message (local)", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@stamped
@with_google_errors("chat_draft_message")
def chat_draft_message(
    conversation: Annotated[str, Field(min_length=1, pattern=r"^spaces/.+")],
    text: Annotated[str, Field(min_length=1, max_length=4096)],
) -> dict:
    """Store a Chat draft for human review (does NOT send)."""
    return chat_tools.draft_message(conversation, text)


@mcp.tool(name="chat_list_unread", annotations={"title": "List Unread Chat Conversations", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("chat_list_unread")
def chat_list_unread(
    limit: Annotated[int, Field(default=10, ge=1, le=50, description="Max unread spaces to return")] = 10,
) -> list[dict]:
    """List Chat conversations with new (unread) messages, most recent first.

    Returns up to `limit` items with the latest message preview, suitable for
    reading aloud. Each item includes `last_active_time` and `last_read_time`
    so the caller can tell what's actually new.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    return chat_tools.list_unread(limit)


@mcp.tool(name="chat_mark_read", annotations={"title": "Mark Chat Conversation as Read", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@stamped
@with_google_errors("chat_mark_read")
def chat_mark_read(
    conversation: Annotated[str, Field(min_length=1, pattern=r"^spaces/.+")],
) -> dict:
    """Mark a Chat conversation as read up to now (sets lastReadTime=now)."""
    return chat_tools.mark_read(conversation)


# --------------------------------------------------------------------------- #
# Gmail
# --------------------------------------------------------------------------- #
@mcp.tool(name="gmail_search", annotations={"title": "Search Gmail", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("gmail_search")
def gmail_search(
    query: Annotated[str, Field(min_length=1, max_length=500, description="Gmail search syntax, e.g. \"from:bob is:unread\"")],
    limit: Annotated[int, Field(default=20, ge=1, le=100)] = 20,
) -> list[dict]:
    """Search Gmail using standard Gmail query syntax (e.g. 'from:bob is:unread').

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    return gmail_tools.search(query, limit)


@mcp.tool(name="gmail_get_message", annotations={"title": "Get Email Message", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("gmail_get_message")
def gmail_get_message(
    id: Annotated[str, Field(min_length=1, description="Gmail message id (hex string)")],
) -> dict:
    """Fetch a single email (headers + plain-text body) by message id.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    return gmail_tools.get_message(id)


@mcp.tool(name="gmail_draft_email", annotations={"title": "Draft Email", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@stamped
@with_google_errors("gmail_draft_email")
def gmail_draft_email(
    to: Annotated[str, Field(min_length=3, max_length=320, pattern=r".+@.+\..+", description="Recipient email")],
    subject: Annotated[str, Field(min_length=1, max_length=998)],
    body: Annotated[str, Field(min_length=1)],
) -> dict:
    """Create a Gmail draft for human review (does NOT send)."""
    return gmail_tools.draft_email(to, subject, body)


@mcp.tool(name="gmail_send_email", annotations={"title": "Send Email", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
@stamped
@with_google_errors("gmail_send_email")
def gmail_send_email(
    to: Annotated[str, Field(min_length=3, max_length=320, pattern=r".+@.+\..+", description="Recipient email")],
    subject: Annotated[str, Field(min_length=1, max_length=998)],
    body: Annotated[str, Field(min_length=1)],
) -> dict:
    """Send an email immediately."""
    return gmail_tools.send_email(to, subject, body)


@mcp.tool(name="gmail_list_unread", annotations={"title": "List Unread Emails", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("gmail_list_unread")
def gmail_list_unread(
    limit: Annotated[int, Field(default=10, ge=1, le=100)] = 10,
    query: Annotated[str | None, Field(default=None, max_length=500, description="Extra Gmail search filter appended to is:unread")] = None,
) -> list[dict]:
    """List unread emails, newest first.

    Optional `query` is appended to `is:unread` (Gmail search syntax — e.g.
    'in:inbox', 'from:boss@example.com'). Returns lightweight summaries (id,
    thread_id, from, subject, snippet, date) suitable for reading aloud.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    return gmail_tools.list_unread(limit, query)


@mcp.tool(name="gmail_mark_read", annotations={"title": "Mark Email as Read", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@stamped
@with_google_errors("gmail_mark_read")
def gmail_mark_read(
    message_id: Annotated[str, Field(min_length=1)],
) -> dict:
    """Mark a Gmail message as read (removes the UNREAD label)."""
    return gmail_tools.mark_read(message_id)


@mcp.tool(name="gmail_mark_unread", annotations={"title": "Mark Email as Unread", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@stamped
@with_google_errors("gmail_mark_unread")
def gmail_mark_unread(
    message_id: Annotated[str, Field(min_length=1)],
) -> dict:
    """Mark a Gmail message as unread (adds the UNREAD label)."""
    return gmail_tools.mark_unread(message_id)


@mcp.tool(name="gmail_reply", annotations={"title": "Reply to Email", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@stamped
@with_google_errors("gmail_reply")
def gmail_reply(
    message_id: Annotated[str, Field(min_length=1, description="Gmail message id you are replying to")],
    body: Annotated[str, Field(min_length=1, description="Reply body text")],
    send: Annotated[bool, Field(default=True, description="True=send immediately, False=save as draft")] = True,
) -> dict:
    """Reply to a Gmail message, properly threaded.

    Looks up the original message's headers and composes a threaded reply
    addressed to the original sender (Reply-To if present, else From). If
    `send=True` (default) the reply is sent; otherwise saved as a draft.
    Use this when the user wants to "reply to" or "respond to" an email.
    """
    return gmail_tools.reply(message_id, body, send)


@mcp.tool(name="gmail_list_attachments", annotations={"title": "List Email Attachments", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("gmail_list_attachments")
def gmail_list_attachments(
    message_id: Annotated[str, Field(min_length=1)],
) -> list[dict]:
    """List the attachments of a Gmail message (id, filename, mime_type, size).

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    return gmail_tools.list_attachments(message_id)


@mcp.tool(name="gmail_read_attachment", annotations={"title": "Read Email Attachment", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("gmail_read_attachment")
def gmail_read_attachment(
    message_id: Annotated[str, Field(min_length=1)],
    attachment_id: Annotated[str, Field(min_length=1)],
    max_chars: Annotated[int, Field(default=12000, ge=100, le=200_000)] = 12000
) -> dict:
    """Download a Gmail attachment and return its text content.

    Supports PDF, DOCX, and plain-text MIME types. Use this when the user
    asks to "read", "open", "summarize", or "check" an email attachment.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    return gmail_tools.read_attachment(message_id, attachment_id, max_chars)


@mcp.tool(name="gdoc_read", annotations={"title": "Read Google Doc / Sheet / Slides", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("gdoc_read")
def gdoc_read(
    url_or_id: Annotated[str, Field(min_length=1, description="Sharing URL or raw Drive file id")],
    max_chars: Annotated[int, Field(default=20000, ge=100, le=200_000)] = 20000,
) -> dict:
    """Fetch a Google Doc, Sheet, or Slides file as plain text.

    Accepts either a sharing URL (https://docs.google.com/document/d/<ID>/...)
    or a raw file ID. Use this when an email contains a Google Docs/Sheets
    link the user wants to read or summarize.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    return gmail_tools.read_gdoc(url_or_id, max_chars)


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #
@mcp.tool(name="calendar_create_event", annotations={"title": "Create Calendar Event", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@stamped
@with_google_errors("calendar_create_event")
def calendar_create_event(
    title: Annotated[str, Field(min_length=1, max_length=500, description="Event title")],
    start: Annotated[str, Field(min_length=10, description="RFC 3339 timestamp with offset, e.g. 2026-06-16T14:00:00+03:00")],
    end: Annotated[str, Field(min_length=10, description="RFC 3339 timestamp; must be after `start`")],
    description: Annotated[str | None, Field(default=None, max_length=8000)] = None
) -> dict:
    """Create a calendar event. start/end are RFC 3339 timestamps with offset."""
    return calendar_tools.create_event(title, start, end, description)


# --------------------------------------------------------------------------- #
# Context / memory — Phase 2 stubs (interfaces stable now)
# --------------------------------------------------------------------------- #
@mcp.tool(name="context_log_conversation", annotations={"title": "Log Conversation (Phase 2 stub)", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
@stamped
@with_google_errors("context_log_conversation")
def context_log_conversation(
    conversation: str, role: str, text: str, source: str = "chat"
) -> dict:
    """[Phase 2 stub] Record a conversation turn for long-term memory."""
    return context_tools.log_conversation(conversation, role, text, source)


@mcp.tool(name="context_get_summary", annotations={"title": "Get Activity Summary (Phase 2 stub)", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("context_get_summary")
def context_get_summary(window: str = "1d") -> dict:
    """[Phase 2 stub] Summarize recent activity over a time window (e.g. '1d').

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    return context_tools.get_summary(window)


# --------------------------------------------------------------------------- #
# Server health (callable by Claude when responses look stale/mocked)
# --------------------------------------------------------------------------- #
@mcp.tool(name="server_health", annotations={"title": "MCP Server Health Check", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("server_health")
def server_health() -> dict:
    """Verify the MCP server is live and authenticated to Google as Ignas.

    Returns `mode` ("live" or "mock"), `dry_run`, `misconfigured_live`, and an
    `auth` block ({ok, account, error}). Call this FIRST whenever a result
    looks stale, mocked, wrong, or contains `_mock: true` — if `mode` is
    "mock" or `auth.ok` is false, the server has lost authentication; tell
    Ignas in plain language and stop. If `mode` is "live" and `auth.ok` is
    true, the data you just received is genuinely from the live account.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    from .google_auth import auth_status

    return {
        "mode": "mock" if settings.use_mock else "live",
        "dry_run": settings.dry_run,
        "misconfigured_live": settings.misconfigured_live,
        "server_time": now_iso(),
        "auth": auth_status(),
    }


# --------------------------------------------------------------------------- #
# Health endpoint (unauthenticated HTTP) — used by the dashboard status page
# --------------------------------------------------------------------------- #
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Health + mode. Add ?probe=1 to actively verify live Google credentials.

    Without probe, this is cheap (safe for frequent polling). With probe, it
    refreshes the token so you can see immediately whether the server is truly
    able to reach Google as the right account (catches expired/rotated creds).
    """
    body = {
        "status": "ok",
        "service": "iblu-keeper",
        "mode": "mock" if settings.use_mock else "live",
        "dry_run": settings.dry_run,
        "has_google_credentials": settings.has_google_credentials,
        "misconfigured_live": settings.misconfigured_live,
        "server_time": now_iso(),
    }
    if settings.misconfigured_live:
        body["warning"] = (
            "DRY_RUN is false but no usable Google token is present — live tool "
            "calls will FAIL (this is intentional: no silent fake data)."
        )
    if request.query_params.get("probe"):
        from .google_auth import auth_status

        body["auth"] = auth_status()
    return JSONResponse(body)


@mcp.custom_route("/", methods=["GET"])
async def root(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("iblu-keeper MCP server. See /health.")


def build_app():
    """Build the Starlette ASGI app (MCP over streamable-HTTP + OAuth +
    Cache-Control: no-store on every response).
    """
    return mcp.http_app(middleware=[Middleware(_NoStoreMiddleware)])


# ASGI entrypoint for `uvicorn iblu_keeper.server:app`
app = build_app()


def main() -> None:
    import uvicorn

    if settings.use_mock:
        logger.warning(
            "Running in MOCK mode (DRY_RUN=true). Tools return FAKE data — "
            "set DRY_RUN=false for live Google access."
        )
    elif settings.misconfigured_live:
        logger.error(
            "DRY_RUN=false but no usable Google token "
            "(client_id set=%s, token file present=%s). Live tool calls will "
            "FAIL until a valid token exists. Run scripts/connect_google.py.",
            bool(settings.google_oauth_client_id),
            settings.has_google_credentials,
        )
    else:
        # Proactively verify credentials at startup so problems are visible in
        # logs immediately, not on the first user tool call.
        from .google_auth import auth_status

        status = auth_status()
        if status["ok"]:
            logger.info("LIVE mode — Google credentials OK (account=%s).", status["account"])
        else:
            logger.error("LIVE mode but credentials NOT working: %s", status["error"])

    uvicorn.run(
        "iblu_keeper.server:app",
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
