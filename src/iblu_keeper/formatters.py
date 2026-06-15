"""Compact, voice-friendly markdown renderers for tool responses.

When a tool is called with ``response_format="markdown"``, the underlying
``items`` list is rendered to a short markdown string the model can read
aloud without parsing JSON. JSON remains the default for programmatic use.

Each renderer takes the items list and any optional context (e.g. envelope
``next_page_token``) and returns a plain-text markdown string.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _human_when(iso_or_rfc: str) -> str:
    """Render a timestamp as a compact relative-style string.

    Returns the original string if parsing fails; falls back to date-only if
    we can parse but can't compute relative time.
    """
    if not iso_or_rfc:
        return ""
    try:
        # Try ISO 8601 first (Google APIs / our envelope use this).
        s = iso_or_rfc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except Exception:
        try:
            # RFC 2822 (Gmail headers).
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(iso_or_rfc)
        except Exception:
            return iso_or_rfc
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 86400 * 7:
        return f"{secs // 86400}d ago"
    return dt.strftime("%Y-%m-%d")


def render_gmail_list(items: list[dict], next_page_token: str | None = None) -> str:
    """Render Gmail search / unread results."""
    if not items:
        return "_No emails matched._"
    lines = []
    for i, m in enumerate(items, 1):
        sender = m.get("from", "?")
        subj = m.get("subject", "(no subject)")
        when = _human_when(m.get("date", ""))
        snippet = (m.get("snippet") or "").strip().replace("\n", " ")[:140]
        lines.append(f"{i}. **{subj}** — {sender} · {when}")
        if snippet:
            lines.append(f"   > {snippet}")
        lines.append(f"   `id: {m.get('id', '')}`")
    out = "\n".join(lines)
    if next_page_token:
        out += "\n\n_More results available — pass `page_token` to fetch the next page._"
    return out


def render_chat_list(items: list[dict], next_page_token: str | None = None) -> str:
    """Render a list of Chat conversations (used by list_conversations + list_unread)."""
    if not items:
        return "_No conversations matched._"
    lines = []
    for i, c in enumerate(items, 1):
        name = c.get("name", "?")
        kind = c.get("type", "")
        when = _human_when(c.get("last_active_time", ""))
        preview = (c.get("last_message_preview") or "").strip().replace("\n", " ")[:160]
        lines.append(f"{i}. **{name}** ({kind}) · {when}")
        if preview:
            lines.append(f"   > {preview}")
        lines.append(f"   `id: {c.get('id', '')}`")
    out = "\n".join(lines)
    if next_page_token:
        out += "\n\n_More results available — pass `page_token` to fetch the next page._"
    return out


def render_chat_messages(
    items: list[dict], next_page_token: str | None = None
) -> str:
    """Render a chat message history (oldest → newest)."""
    if not items:
        return "_No messages._"
    lines = []
    for m in items:
        sender = m.get("sender", "?")
        when = _human_when(m.get("create_time", ""))
        text = (m.get("text") or "").strip().replace("\n", " ")[:280]
        lines.append(f"- **{sender}** · {when}: {text}")
    out = "\n".join(lines)
    if next_page_token:
        out += "\n\n_Older messages available — pass `page_token` for the next page._"
    return out


def to_markdown_envelope(
    kind: str, items: list[dict], next_page_token: str | None = None
) -> dict:
    """Wrap the rendered markdown in a small envelope the model can read.

    ``kind`` selects which renderer to apply: ``"gmail_list"``,
    ``"chat_list"``, or ``"chat_messages"``. Returns a dict with ``markdown``,
    ``count``, and ``next_page_token`` so the @stamped envelope can merge it.
    """
    renderers = {
        "gmail_list": render_gmail_list,
        "chat_list": render_chat_list,
        "chat_messages": render_chat_messages,
    }
    render = renderers[kind]
    return {
        "markdown": render(items, next_page_token),
        "count": len(items),
        "next_page_token": next_page_token,
    }
