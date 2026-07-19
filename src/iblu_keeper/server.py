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
from .formatters import to_markdown_envelope
from .google_errors import with_google_errors, with_retry


def _maybe_markdown(result, kind: str, response_format: str):
    """If ``response_format == 'markdown'``, render the items as compact text;
    otherwise return the original shape unchanged.

    Handles both shapes a tool may return:
    - a list (un-paginated tools like chat_list_conversations)
    - a dict with ``items`` (paginated tools like gmail_search)
    """
    if response_format != "markdown":
        return result
    if isinstance(result, list):
        items = result
        token = None
    else:
        items = result.get("items", [])
        token = result.get("next_page_token")
    return to_markdown_envelope(kind, items, token)
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
        "AVAILABILITY PROTOCOL — CRITICAL, read this first. If you can read "
        "these instructions, the IBLU connector IS connected and its tools "
        "ARE callable in this exact conversation. Ignas may call it 'IBLU', "
        "'Iblu', 'iblu', or voice-transcribed variants like 'Ibloo', "
        "'I blue', 'e-blue', 'ee-blue'. If he asks whether you can connect "
        "to / see / access IBLU, or claims you're missing it, do NOT answer "
        "from reasoning or prior context — call `server_health` immediately "
        "and let the result answer: on success say IBLU is connected and "
        "working, then carry on with his actual request. NEVER tell him you "
        "don't have IBLU tools, that IBLU 'isn't wired up to you', or that "
        "the connector still needs to be built — that is always wrong in "
        "any conversation where these instructions are visible. If a tool "
        "call genuinely fails, report that specific error instead of "
        "denying the tools exist."
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
        "\n\n"
        "VOICE-MODE PROTOCOL — Ignas often uses Claude in voice / read-aloud "
        "mode (driving, walking, hands busy). Detect it from voice-style "
        "transcribed input (filler words like 'um', 'uh', dictation cadence, "
        "or any explicit mention of speaking / voice / reading aloud). When "
        "in voice mode, follow ALL of these rules:\n"
        "1. NEVER speak any of the following — they're noise to a human ear "
        "and break the conversation flow: tool names (`gmail_list_unread`, "
        "`chat_send_message`, etc.); parameters or argument values you "
        "passed (`limit=3`, `query='from:bob'`, `conversation='spaces/...'`); "
        "raw JSON, dict keys, or field names from responses; envelope "
        "metadata (`fetched_at`, `request_id`, `next_page_token`, `count`); "
        "any ID, hex string, UUID, or `spaces/AAAxxx` / `users/123…` resource "
        "name; words like 'tool', 'function', 'called', 'invoked', "
        "'returned', 'fetched', 'parameters', 'arguments', 'API'.\n"
        "2. Speak ONLY the natural-language summary of what you found, as if "
        "you were a human assistant answering. Example for 'list 3 unread "
        "emails': WRONG — 'I called gmail_list_unread with limit equals 3 "
        "and got back item one id 19eca, from Tamara Lovric, subject Hello, "
        "fetched at 10:30 UTC...'. RIGHT — 'You have three unread. Tamara "
        "asked 25 minutes ago if you can check the invoice. Alexan from "
        "Sparklead wants a 15-minute call. And there's a Bolt ride receipt "
        "from this morning.' (Convert timestamps to relative time, refer to "
        "people by first name, summarize the snippet.)\n"
        "3. When calling any read tool that supports `response_format` in "
        "voice mode, pass `response_format='markdown'` so you receive a "
        "compact, human-shaped string instead of JSON. There's less noise "
        "for you to filter out before speaking.\n"
        "4. If Ignas asks a follow-up like 'reply to that one' or 'mark it "
        "read', just confirm what you're doing in human terms ('Replying to "
        "Tamara now…' or 'Marked as read.'). Do not name the tool, the id, "
        "or speak the JSON confirmation."
    ),
    auth=_build_auth(),
)


# --------------------------------------------------------------------------- #
# Google Chat
# --------------------------------------------------------------------------- #
@mcp.tool(name="chat_list_conversations", annotations={"title": "List Chat Conversations", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("chat_list_conversations")
@with_retry("chat_list_conversations")
def chat_list_conversations(
    query: Annotated[str | None, Field(default=None, description="Case-insensitive substring filter on space name or participants")] = None,
    limit: Annotated[int, Field(default=20, ge=1, le=100, description="Max items to return")] = 20,
    response_format: Annotated[str, Field(default="json", pattern="^(json|markdown)$", description="json (default) or markdown for voice-friendly output")] = "json",
):
    """List recent Chat conversations/spaces, most recently active first.

    Returns up to `limit` items (default 20, max 100). Filter by a person's
    name via `query`. Each item includes a `last_message_preview` snippet.
    Set `response_format='markdown'` for compact, voice-friendly output.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    raw = chat_tools.list_conversations(query, limit)
    return _maybe_markdown(raw, "chat_list", response_format)


@mcp.tool(name="chat_get_messages", annotations={"title": "Get Chat Messages", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("chat_get_messages")
@with_retry("chat_get_messages")
def chat_get_messages(
    conversation: Annotated[str, Field(min_length=1, pattern=r"^spaces/.+", description="Space resource name, e.g. spaces/AAAAxxxxx")],
    limit: Annotated[int, Field(default=20, ge=1, le=100)] = 20,
    page_token: Annotated[str | None, Field(default=None, description="Cursor from a previous response's next_page_token; omit for the first page")] = None,
    response_format: Annotated[str, Field(default="json", pattern="^(json|markdown)$", description="json (default) or markdown for voice-friendly output")] = "json",
):
    """Get message history for a conversation (use an id from chat_list_conversations).

    Returns ``{items, count, next_page_token}``; pass ``next_page_token`` from
    the response to ``page_token`` on the next call to fetch older messages.
    Set ``response_format='markdown'`` for compact, voice-friendly output.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    raw = chat_tools.get_messages(conversation, limit, page_token)
    return _maybe_markdown(raw, "chat_messages", response_format)


@mcp.tool(name="chat_send_message", annotations={"title": "Send Chat Message", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
@stamped
@with_google_errors("chat_send_message")
@with_retry("chat_send_message")
def chat_send_message(
    conversation: Annotated[str, Field(min_length=1, pattern=r"^spaces/.+", description="Target space, e.g. spaces/AAAAxxxxx")],
    text: Annotated[str, Field(min_length=1, max_length=4096, description="Message body")],
) -> dict:
    """Send a Chat message to a conversation."""
    return chat_tools.send_message(conversation, text)


@mcp.tool(name="chat_draft_message", annotations={"title": "Draft Chat Message (local)", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("chat_draft_message")
@with_retry("chat_draft_message")
def chat_draft_message(
    conversation: Annotated[str, Field(min_length=1, pattern=r"^spaces/.+")],
    text: Annotated[str, Field(min_length=1, max_length=4096)],
) -> dict:
    """Store a Chat draft for human review (does NOT send)."""
    return chat_tools.draft_message(conversation, text)


@mcp.tool(name="chat_list_unread", annotations={"title": "List Unread Chat Conversations", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("chat_list_unread")
@with_retry("chat_list_unread")
def chat_list_unread(
    limit: Annotated[int, Field(default=10, ge=1, le=50, description="Max unread spaces to return")] = 10,
    response_format: Annotated[str, Field(default="json", pattern="^(json|markdown)$", description="json (default) or markdown for voice-friendly output")] = "json",
):
    """List Chat conversations with new (unread) messages, most recent first.

    Returns up to `limit` items with the latest message preview, suitable for
    reading aloud. Each item includes `last_active_time` and `last_read_time`
    so the caller can tell what's actually new.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    raw = chat_tools.list_unread(limit)
    return _maybe_markdown(raw, "chat_list", response_format)


@mcp.tool(name="chat_mark_read", annotations={"title": "Mark Chat Conversation as Read", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("chat_mark_read")
@with_retry("chat_mark_read")
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
@with_retry("gmail_search")
def gmail_search(
    query: Annotated[str, Field(min_length=1, max_length=500, description="Gmail search syntax, e.g. \"from:bob is:unread\"")],
    limit: Annotated[int, Field(default=20, ge=1, le=100)] = 20,
    page_token: Annotated[str | None, Field(default=None, description="Cursor from a previous response's next_page_token; omit for the first page")] = None,
    response_format: Annotated[str, Field(default="json", pattern="^(json|markdown)$", description="json (default) or markdown for voice-friendly output")] = "json",
):
    """Search Gmail using standard Gmail query syntax (e.g. 'from:bob is:unread').

    Returns ``{items, count, next_page_token}``. Pass ``next_page_token`` back
    in ``page_token`` to fetch the next page of older results. Set
    ``response_format='markdown'`` for compact, voice-friendly output.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    raw = gmail_tools.search(query, limit, page_token)
    return _maybe_markdown(raw, "gmail_list", response_format)


@mcp.tool(name="gmail_get_message", annotations={"title": "Get Email Message", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("gmail_get_message")
@with_retry("gmail_get_message")
def gmail_get_message(
    id: Annotated[str, Field(min_length=1, description="Gmail message id (hex string)")],
) -> dict:
    """Fetch a single email (headers + plain-text body) by message id.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    return gmail_tools.get_message(id)


@mcp.tool(name="gmail_draft_email", annotations={"title": "Draft Email", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("gmail_draft_email")
@with_retry("gmail_draft_email")
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
@with_retry("gmail_send_email")
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
@with_retry("gmail_list_unread")
def gmail_list_unread(
    limit: Annotated[int, Field(default=10, ge=1, le=100)] = 10,
    query: Annotated[str | None, Field(default=None, max_length=500, description="Extra Gmail search filter appended to is:unread")] = None,
    page_token: Annotated[str | None, Field(default=None, description="Cursor from a previous response's next_page_token; omit for the first page")] = None,
    response_format: Annotated[str, Field(default="json", pattern="^(json|markdown)$", description="json (default) or markdown for voice-friendly output")] = "json",
):
    """List unread emails, newest first.

    Optional `query` is appended to `is:unread` (Gmail search syntax — e.g.
    'in:inbox', 'from:boss@example.com'). Returns ``{items, count,
    next_page_token}``; pass the token back as ``page_token`` for older
    unread. Set ``response_format='markdown'`` for compact, voice-friendly
    output.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    raw = gmail_tools.list_unread(limit, query, page_token)
    return _maybe_markdown(raw, "gmail_list", response_format)


@mcp.tool(name="gmail_mark_read", annotations={"title": "Mark Email as Read", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("gmail_mark_read")
@with_retry("gmail_mark_read")
def gmail_mark_read(
    message_id: Annotated[str, Field(min_length=1)],
) -> dict:
    """Mark a Gmail message as read (removes the UNREAD label)."""
    return gmail_tools.mark_read(message_id)


@mcp.tool(name="gmail_mark_unread", annotations={"title": "Mark Email as Unread", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("gmail_mark_unread")
@with_retry("gmail_mark_unread")
def gmail_mark_unread(
    message_id: Annotated[str, Field(min_length=1)],
) -> dict:
    """Mark a Gmail message as unread (adds the UNREAD label)."""
    return gmail_tools.mark_unread(message_id)


@mcp.tool(name="gmail_reply", annotations={"title": "Reply to Email", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
@stamped
@with_google_errors("gmail_reply")
@with_retry("gmail_reply")
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
@with_retry("gmail_list_attachments")
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
@with_retry("gmail_read_attachment")
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
@with_retry("gdoc_read")
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
# Google Drive + Docs edits
# --------------------------------------------------------------------------- #
@mcp.tool(name="gdoc_create", annotations={"title": "Create Google Doc", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("gdoc_create")
@with_retry("gdoc_create")
def gdoc_create(
    title: Annotated[str, Field(min_length=1, max_length=500, description="Title of the new Doc")],
    content: Annotated[str, Field(default="", max_length=200_000, description="Optional initial body text")] = "",
    folder_id: Annotated[str | None, Field(default=None, description="Drive folder ID or sharing URL to create the Doc inside")] = None,
) -> dict:
    """Create a new Google Doc, optionally with initial body text and inside a folder."""
    from .tools import drive as drive_tools
    return drive_tools.gdoc_create(title, content, folder_id)


@mcp.tool(name="gdoc_append", annotations={"title": "Append to Google Doc", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("gdoc_append")
@with_retry("gdoc_append")
def gdoc_append(
    doc_id_or_url: Annotated[str, Field(min_length=1, description="Doc ID or sharing URL")],
    text: Annotated[str, Field(min_length=1, max_length=200_000, description="Text to append at the end of the doc")],
) -> dict:
    """Append text to the end of an existing Google Doc. Preserves prior content."""
    from .tools import drive as drive_tools
    return drive_tools.gdoc_append(doc_id_or_url, text)


@mcp.tool(name="gdoc_replace_text", annotations={"title": "Find-and-Replace in Google Doc", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("gdoc_replace_text")
@with_retry("gdoc_replace_text")
def gdoc_replace_text(
    doc_id_or_url: Annotated[str, Field(min_length=1, description="Doc ID or sharing URL")],
    find: Annotated[str, Field(min_length=1, description="Text to search for")],
    replace_with: Annotated[str, Field(description="Text to replace with (can be empty to delete the matches)")],
    match_case: Annotated[bool, Field(default=False, description="Case-sensitive matching")] = False,
) -> dict:
    """Find-and-replace text in a Google Doc. Replaces ALL occurrences."""
    from .tools import drive as drive_tools
    return drive_tools.gdoc_replace_text(doc_id_or_url, find, replace_with, match_case)


@mcp.tool(name="gdoc_rename", annotations={"title": "Rename Drive File", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("gdoc_rename")
@with_retry("gdoc_rename")
def gdoc_rename(
    doc_id_or_url: Annotated[str, Field(min_length=1, description="File ID or sharing URL")],
    new_name: Annotated[str, Field(min_length=1, max_length=500, description="New file name")],
) -> dict:
    """Rename a Drive file (works for Docs, Sheets, Slides, any file)."""
    from .tools import drive as drive_tools
    return drive_tools.gdoc_rename(doc_id_or_url, new_name)


@mcp.tool(name="gdoc_move", annotations={"title": "Move Drive File to Folder", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("gdoc_move")
@with_retry("gdoc_move")
def gdoc_move(
    file_id_or_url: Annotated[str, Field(min_length=1, description="File ID or sharing URL")],
    folder_id_or_url: Annotated[str, Field(min_length=1, description="Destination folder ID or URL")],
) -> dict:
    """Move a Drive file into a folder (replaces existing parents)."""
    from .tools import drive as drive_tools
    return drive_tools.gdoc_move(file_id_or_url, folder_id_or_url)


@mcp.tool(name="drive_create_folder", annotations={"title": "Create Drive Folder", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("drive_create_folder")
@with_retry("drive_create_folder")
def drive_create_folder(
    name: Annotated[str, Field(min_length=1, max_length=500, description="Folder name")],
    parent_id: Annotated[str | None, Field(default=None, description="Optional parent folder ID or URL (creates in My Drive root if omitted)")] = None,
) -> dict:
    """Create a new folder in Google Drive."""
    from .tools import drive as drive_tools
    return drive_tools.drive_create_folder(name, parent_id)


@mcp.tool(name="drive_list_folder", annotations={"title": "List Drive Folder Contents", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("drive_list_folder")
@with_retry("drive_list_folder")
def drive_list_folder(
    folder_id_or_url: Annotated[str | None, Field(default=None, description="Folder ID/URL to list. Omit to search by name across all your Drive.")] = None,
    query: Annotated[str | None, Field(default=None, max_length=200, description="Filter by name (substring match). Useful for finding a folder by name when you don't have its id.")] = None,
    limit: Annotated[int, Field(default=20, ge=1, le=100)] = 20,
    page_token: Annotated[str | None, Field(default=None, description="Cursor from a previous response's next_page_token; omit for the first page")] = None,
) -> dict:
    """List files/folders in a Drive folder, or search by name across Drive.

    Returns ``{items, count, next_page_token}``; each item includes id, name,
    mime_type, is_folder, modified_time, url. Sorted by most-recently modified
    first.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    from .tools import drive as drive_tools
    return drive_tools.drive_list_folder(folder_id_or_url, query, limit, page_token)


@mcp.tool(name="drive_save_gmail_attachment", annotations={"title": "Save Email Attachment to Drive", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("drive_save_gmail_attachment")
@with_retry("drive_save_gmail_attachment")
def drive_save_gmail_attachment(
    message_id: Annotated[str, Field(min_length=1, description="Gmail message id holding the attachment")],
    attachment_id: Annotated[str, Field(min_length=1, description="Attachment id from gmail_list_attachments")],
    folder_id_or_url: Annotated[str | None, Field(default=None, description="Destination Drive folder (My Drive root if omitted)")] = None,
    filename: Annotated[str | None, Field(default=None, max_length=500, description="Override filename (defaults to original attachment name)")] = None,
) -> dict:
    """Save a Gmail attachment directly to Drive without a client round-trip."""
    from .tools import drive as drive_tools
    return drive_tools.drive_save_gmail_attachment(message_id, attachment_id, folder_id_or_url, filename)


@mcp.tool(name="drive_upload_from_url", annotations={"title": "Upload URL Content to Drive", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("drive_upload_from_url")
@with_retry("drive_upload_from_url")
def drive_upload_from_url(
    url: Annotated[str, Field(min_length=8, max_length=2048, pattern=r"^https?://", description="HTTP(S) URL to download")],
    filename: Annotated[str, Field(min_length=1, max_length=500)],
    folder_id_or_url: Annotated[str | None, Field(default=None, description="Destination Drive folder (My Drive root if omitted)")] = None,
    mime_type: Annotated[str | None, Field(default=None, max_length=200, description="MIME type override. Auto-detected from response Content-Type when omitted.")] = None,
) -> dict:
    """Download a URL and save its body to Drive as a new file.

    The server fetches the URL — Claude only needs to provide the link. Use
    for "save this PDF / image / file from the web to my Drive" workflows.
    Marked openWorldHint=true because this tool reaches an arbitrary URL.
    """
    from .tools import drive as drive_tools
    return drive_tools.drive_upload_from_url(url, filename, folder_id_or_url, mime_type)


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #
@mcp.tool(name="calendar_create_event", annotations={"title": "Create Calendar Event", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("calendar_create_event")
@with_retry("calendar_create_event")
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
@mcp.tool(name="context_log_conversation", annotations={"title": "Log Conversation (Phase 2 stub)", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
@stamped
@with_google_errors("context_log_conversation")
@with_retry("context_log_conversation")
def context_log_conversation(
    conversation: str, role: str, text: str, source: str = "chat"
) -> dict:
    """[Phase 2 stub] Record a conversation turn for long-term memory."""
    return context_tools.log_conversation(conversation, role, text, source)


@mcp.tool(name="context_get_summary", annotations={"title": "Get Activity Summary (Phase 2 stub)", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("context_get_summary")
@with_retry("context_get_summary")
def context_get_summary(window: str = "1d") -> dict:
    """[Phase 2 stub] Summarize recent activity over a time window (e.g. '1d').

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    return context_tools.get_summary(window)


# --------------------------------------------------------------------------- #
# Infrastructure status (reads collector output from Drive)
# --------------------------------------------------------------------------- #
@mcp.tool(name="get_infra_status", annotations={"title": "Get Infrastructure Status", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
def get_infra_status() -> dict:
    """Fetch the latest infrastructure health from the Drive collector folder.

    Returns a compact summary (``status``, ``summary``, ``generated``) plus
    the raw ``latest.md`` (human-readable) and ``latest.json`` (structured)
    files so callers can either speak the summary or drill into the data.

    ``status`` is derived from host ``ts`` freshness:
    - ``healthy`` — every host reported a fresh timestamp
    - ``degraded`` — at least one host is stale (>24h)
    - ``error`` — no hosts, all stale, or the JSON couldn't be parsed

    Results are cached in-process for 5 minutes. On any failure (folder not
    configured, missing files, Drive API error, JSON parse error) the tool
    returns ``{"error": "..."}`` — no exceptions bubble up to Claude.

    Returns live data fetched at call time. Always call again for current state; never reuse a previous result. Response includes fetched_at and request_id — report fetched_at to the user.
    """
    from .tools import infra as infra_tools

    return infra_tools.get_infra_status()


# --------------------------------------------------------------------------- #
# Server health (callable by Claude when responses look stale/mocked)
# --------------------------------------------------------------------------- #
@mcp.tool(name="server_health", annotations={"title": "MCP Server Health Check", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
@stamped
@with_google_errors("server_health")
@with_retry("server_health")
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
