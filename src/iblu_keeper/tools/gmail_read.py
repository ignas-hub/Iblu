"""Reading the parts of Gmail that `tools/gmail.py` never reached.

`tools/gmail.py` can search, open one message, list its attachments and mark
it read. It cannot show a whole thread in one call, say how many messages a
label holds, list the drafts waiting to be sent, open one of them, or explain
what a filter does — and every one of those became "can you add a tool for…"
in conversation. Ignas, 2026-10-26: "It's crazy how many times I have to ask
to add tools." This module and its three siblings (`gmail_manage`,
`calendar_edit`, `chat_read`) are the batch that ends that.

It is ONE MCP tool with an `action` parameter, not seven, because every
`@mcp.tool` is a permanent Allow click in the connector UI (HANDOFF §12: one
tool per domain × side-effect class). Everything here is read-only; the
writes live in `gmail_manage.py`, so this tool can sit in the always-allow
group without ever being able to send or change anything.

Scopes, verified against the live token 2026-10-26: `gmail.modify` covers
threads, labels, drafts, profile and sent mail; `gmail.settings` covers
`filters.list`.

In dry-run mode every action returns a clearly marked mock and never builds a
Google service.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from ..config import resolve_account, settings
from .gmail import _extract_body, _walk_parts

logger = logging.getLogger("iblu_keeper.tools.gmail_read")

ACTIONS = ("thread", "labels", "drafts", "draft", "filters", "profile", "sent_recent")

# Per-message body cap inside a thread. A long thread with a dozen quoted
# replies would otherwise return the same paragraphs twelve times over.
BODY_CAP = 4_000

# System labels whose removal/addition a filter expresses as a verb a person
# would recognise. Used by `_filter_summary`.
_REMOVE_VERBS = {
    "INBOX": "skip inbox",
    "UNREAD": "mark read",
    "SPAM": "never spam",
    "IMPORTANT": "never important",
}
_ADD_VERBS = {
    "STARRED": "star",
    "IMPORTANT": "mark important",
    "TRASH": "trash",
}


class GmailReadError(ValueError):
    """Bad input to `gmail_read`. The message always names what was expected."""


def _service(account: str | None = None):
    from ..google_auth import build_service

    return build_service("gmail", "v1", account=account)


# --- shaping ----------------------------------------------------------------


def _headers(payload: dict) -> dict[str, str]:
    """Header name → value, lower-cased keys. Gmail preserves the sender's
    capitalisation ("Message-ID" vs "Message-Id"), which is not a thing a
    caller should have to know."""
    return {
        (h.get("name") or "").lower(): h.get("value") or ""
        for h in (payload.get("headers") or [])
    }


def _attachments(payload: dict) -> list[dict]:
    out: list[dict] = []
    for part in _walk_parts(payload):
        body = part.get("body", {}) or {}
        att_id = body.get("attachmentId")
        if not att_id:
            continue
        out.append({
            "attachment_id": att_id,
            "filename": part.get("filename") or "",
            "mime_type": part.get("mimeType") or "",
            "size_bytes": body.get("size", 0),
        })
    return out


def _message_view(msg: dict) -> dict:
    """One message as a caller wants to see it: envelope, capped body, attachments."""
    payload = msg.get("payload", {}) or {}
    h = _headers(payload)
    body = _extract_body(payload)
    return {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "from": h.get("from", ""),
        "to": h.get("to", ""),
        "cc": h.get("cc", ""),
        "date": h.get("date", ""),
        "subject": h.get("subject", ""),
        "snippet": msg.get("snippet", ""),
        "labels": msg.get("labelIds", []) or [],
        "body": body[:BODY_CAP],
        "body_truncated": len(body) > BODY_CAP,
        "attachments": _attachments(payload),
    }


def _label_names(service) -> dict[str, str]:
    """Label id → name, so filters can be described in words rather than ids."""
    listed = service.users().labels().list(userId="me").execute().get("labels", []) or []
    return {lab.get("id", ""): lab.get("name", "") for lab in listed}


def _filter_summary(criteria: dict, action: dict, names: dict[str, str]) -> str:
    """'from:x subject:"y" → add label Clients, skip inbox' — what the filter
    does, in the words the Gmail settings page would use."""
    conds: list[str] = []
    if criteria.get("from"):
        conds.append(f"from:{criteria['from']}")
    if criteria.get("to"):
        conds.append(f"to:{criteria['to']}")
    if criteria.get("subject"):
        conds.append(f"subject:\"{criteria['subject']}\"")
    if criteria.get("query"):
        conds.append(criteria["query"])
    if criteria.get("negatedQuery"):
        conds.append(f"-({criteria['negatedQuery']})")
    if criteria.get("hasAttachment"):
        conds.append("has:attachment")
    if criteria.get("size"):
        cmp = "larger" if criteria.get("sizeComparison") == "larger" else "smaller"
        conds.append(f"{cmp}:{criteria['size']}")

    acts: list[str] = []
    for lab in action.get("addLabelIds", []) or []:
        acts.append(_ADD_VERBS.get(lab) or f"add label {names.get(lab, lab)}")
    for lab in action.get("removeLabelIds", []) or []:
        acts.append(_REMOVE_VERBS.get(lab) or f"remove label {names.get(lab, lab)}")
    if action.get("forward"):
        acts.append(f"forward to {action['forward']}")

    return f"{' '.join(conds) or '(no criteria)'} → {', '.join(acts) or '(no action)'}"


# --- actions ----------------------------------------------------------------


def _thread(service, thread_id: str) -> dict:
    t = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = [_message_view(m) for m in t.get("messages", []) or []]
    return {
        "status": "ok",
        "thread_id": t.get("id", thread_id),
        "subject": messages[0]["subject"] if messages else "",
        "count": len(messages),
        "messages": messages,
        "body_cap_per_message": BODY_CAP,
    }


def _labels(account: str | None) -> dict:
    listed = _service(account).users().labels().list(userId="me").execute().get("labels", []) or []

    # labels.list carries no counts; labels.get does, one call per label.
    # They are independent, so they run in parallel — each worker builds its
    # own service because httplib2 is not thread-safe.
    def fetch(lab: dict) -> dict:
        full = _service(account).users().labels().get(userId="me", id=lab["id"]).execute()
        return {
            "id": full.get("id", lab.get("id")),
            "name": full.get("name", lab.get("name", "")),
            "type": full.get("type", lab.get("type", "")),
            "messages_total": full.get("messagesTotal"),
            "messages_unread": full.get("messagesUnread"),
            "threads_total": full.get("threadsTotal"),
            "threads_unread": full.get("threadsUnread"),
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        items = list(ex.map(fetch, listed))
    items.sort(key=lambda lab: (lab["type"] != "system", lab["name"].lower()))
    return {"status": "ok", "count": len(items), "labels": items}


def _drafts(service, limit: int) -> dict:
    resp = service.users().drafts().list(userId="me", maxResults=limit).execute()
    items: list[dict] = []
    for ref in resp.get("drafts", []) or []:
        d = service.users().drafts().get(userId="me", id=ref["id"], format="metadata").execute()
        msg = d.get("message", {}) or {}
        h = _headers(msg.get("payload", {}) or {})
        items.append({
            "id": d.get("id", ref["id"]),
            "message_id": msg.get("id"),
            "thread_id": msg.get("threadId"),
            "to": h.get("to", ""),
            "subject": h.get("subject", ""),
            "date": h.get("date", ""),
            "snippet": msg.get("snippet", ""),
        })
    return {
        "status": "ok",
        "count": len(items),
        "drafts": items,
        "next_page_token": resp.get("nextPageToken") or None,
    }


def _draft(service, draft_id: str) -> dict:
    d = service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
    view = _message_view(d.get("message", {}) or {})
    return {"status": "ok", "draft_id": d.get("id", draft_id), **view}


def _filters(service) -> dict:
    resp = service.users().settings().filters().list(userId="me").execute()
    names = _label_names(service)
    items: list[dict] = []
    for f in resp.get("filter", []) or []:
        criteria = f.get("criteria", {}) or {}
        action = f.get("action", {}) or {}
        items.append({
            "id": f.get("id"),
            "criteria": {
                "from": criteria.get("from", ""),
                "to": criteria.get("to", ""),
                "subject": criteria.get("subject", ""),
                "query": criteria.get("query", ""),
                "negated_query": criteria.get("negatedQuery", ""),
                "has_attachment": bool(criteria.get("hasAttachment", False)),
            },
            "actions": {
                "add_labels": [names.get(x, x) for x in action.get("addLabelIds", []) or []],
                "remove_labels": [names.get(x, x) for x in action.get("removeLabelIds", []) or []],
                "forward": action.get("forward", ""),
            },
            "summary": _filter_summary(criteria, action, names),
        })
    return {"status": "ok", "count": len(items), "filters": items}


def _profile(service) -> dict:
    p = service.users().getProfile(userId="me").execute()
    return {
        "status": "ok",
        "address": p.get("emailAddress", ""),
        "messages_total": p.get("messagesTotal"),
        "threads_total": p.get("threadsTotal"),
        "history_id": p.get("historyId"),
    }


def _sent_recent(service, limit: int) -> dict:
    resp = (
        service.users().messages()
        .list(userId="me", labelIds=["SENT"], maxResults=limit)
        .execute()
    )
    items: list[dict] = []
    for ref in resp.get("messages", []) or []:
        full = (
            service.users().messages()
            .get(userId="me", id=ref["id"], format="metadata",
                 metadataHeaders=["To", "Subject", "Date"])
            .execute()
        )
        h = _headers(full.get("payload", {}) or {})
        items.append({
            "id": full.get("id", ref["id"]),
            "thread_id": full.get("threadId"),
            "to": h.get("to", ""),
            "subject": h.get("subject", ""),
            "date": h.get("date", ""),
            "snippet": full.get("snippet", ""),
        })
    return {"status": "ok", "count": len(items), "items": items}


# --- dispatch ---------------------------------------------------------------


def gmail_read(
    action: str,
    thread_id: str | None = None,
    draft_id: str | None = None,
    limit: int = 10,
    account: str | None = None,
) -> dict:
    """Read-only Gmail views that the search/get tools cannot give.

    Actions:
      thread       thread_id → every message: from/to/cc/date/subject, snippet,
                   plain body (capped at 4,000 chars per message, flagged when
                   cut), labels, attachments.
      labels       every system + user label with messagesTotal/messagesUnread
                   and threadsTotal/threadsUnread.
      drafts       the last `limit` drafts: id, to, subject, date, snippet.
      draft        draft_id → the full draft (same shape as a thread message).
      filters      the mailbox's filters, criteria and actions in words.
      profile      address, messagesTotal, threadsTotal, historyId.
      sent_recent  the last `limit` sent messages: to, subject, date.

    Nothing here changes a message, a label or a draft — those are
    `gmail_manage`. ``account`` — which mailbox: ``blt`` (default),
    ``deadlift``, ``choco``.
    """
    if action not in ACTIONS:
        raise GmailReadError(
            f"unknown action {action!r} — expected one of {', '.join(ACTIONS)}"
        )
    if action == "thread" and not thread_id:
        raise GmailReadError("thread requires thread_id")
    if action == "draft" and not draft_id:
        raise GmailReadError("draft requires draft_id")
    if limit < 1:
        raise GmailReadError(f"limit must be >= 1, got {limit!r}")

    account = resolve_account(account)
    if settings.use_mock:
        return {
            "_mock": True,
            "action": action,
            "status": "not_read_mock",
            "count": 0,
            "items": [],
            "note": "MOCK MODE — nothing was read from Gmail. Set DRY_RUN=false.",
        }

    if action == "labels":
        return _labels(account)

    service = _service(account)
    if action == "thread":
        return _thread(service, thread_id)
    if action == "drafts":
        return _drafts(service, limit)
    if action == "draft":
        return _draft(service, draft_id)
    if action == "filters":
        return _filters(service)
    if action == "profile":
        return _profile(service)
    return _sent_recent(service, limit)
