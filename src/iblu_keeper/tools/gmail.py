"""Gmail tools via the delegated service account.

Gmail via service account is known-good (Ignas has used this pattern before).
In dry-run / no-credentials mode these return deterministic mock data.
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage

from ..config import settings
from ..store import drafts

logger = logging.getLogger("iblu_keeper.tools.gmail")


def _service():
    from ..google_auth import build_service

    return build_service("gmail", "v1")


def _build_raw(to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = settings.google_user_email
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def search(
    query: str, limit: int = 20, page_token: str | None = None
) -> dict:
    """Search Gmail. Returns lightweight message summaries (newest first).

    Pass ``page_token`` to fetch the next page (the value returned in the
    previous response's ``next_page_token``). Returns
    ``{items, count, next_page_token}``; ``next_page_token`` is None when
    there are no more results.
    """
    if settings.use_mock:
        return {
            "items": [
                {
                    "_mock": True,
                    "id": "MOCK_MSG_1",
                    "thread_id": "MOCK_THREAD_1",
                    "from": "client@example.com",
                    "subject": f"[MOCK] Re: {query or 'Proposal'}",
                    "snippet": "MOCK DATA — server is in DRY_RUN mode, not live Gmail.",
                    "date": "2026-06-10T08:30:00Z",
                },
            ],
            "count": 1,
            "next_page_token": None,
        }

    service = _service()
    list_kwargs: dict = {"userId": "me", "q": query, "maxResults": limit}
    if page_token:
        list_kwargs["pageToken"] = page_token
    resp = service.users().messages().list(**list_kwargs).execute()

    items: list[dict] = []
    for ref in resp.get("messages", []):
        full = (
            service.users()
            .messages()
            .get(userId="me", id=ref["id"], format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
        items.append(
            {
                "id": full.get("id"),
                "thread_id": full.get("threadId"),
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "snippet": full.get("snippet", ""),
                "date": headers.get("Date", ""),
            }
        )
    return {
        "items": items,
        "count": len(items),
        "next_page_token": resp.get("nextPageToken") or None,
    }


def get_message(message_id: str) -> dict:
    """Fetch a single message with its plain-text body."""
    if settings.use_mock:
        return {
            "_mock": True,
            "id": message_id,
            "from": "client@example.com",
            "to": settings.google_user_email,
            "subject": "[MOCK] Re: Proposal",
            "date": "2026-06-10T08:30:00Z",
            "body": "MOCK DATA — server is in DRY_RUN mode, not live Gmail.",
        }

    service = _service()
    full = (
        service.users().messages().get(userId="me", id=message_id, format="full").execute()
    )
    payload = full.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    return {
        "id": full.get("id"),
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "body": _extract_body(payload),
    }


def _extract_body(payload: dict) -> str:
    """Best-effort plain-text body extraction."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text:
            return text
    data = payload.get("body", {}).get("data")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return ""


def draft_email(to: str, subject: str, body: str) -> dict:
    """Create a Gmail draft. In mock mode, stores it in the local draft store."""
    if settings.use_mock:
        return drafts.add_draft(
            "email", {"to": to, "subject": subject, "body": body}
        )

    service = _service()
    created = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": _build_raw(to, subject, body)}})
        .execute()
    )
    return {"id": created.get("id"), "to": to, "subject": subject, "status": "draft"}


def send_email(to: str, subject: str, body: str) -> dict:
    """Send an email immediately. In mock mode, returns a clearly fake result."""
    if settings.use_mock:
        logger.warning("MOCK send_email to %s — NOT actually sent (DRY_RUN).", to)
        return {
            "_mock": True,
            "id": "MOCK_SENT_EMAIL",
            "to": to,
            "subject": subject,
            "status": "not_sent_mock",
            "note": "MOCK MODE — email was NOT delivered. Set DRY_RUN=false.",
        }

    service = _service()
    sent = (
        service.users()
        .messages()
        .send(userId="me", body={"raw": _build_raw(to, subject, body)})
        .execute()
    )
    # The real Gmail message id is proof the send actually reached Google.
    # If it's missing for any reason, raise — never return a success-shaped
    # payload Claude could mistake for confirmation.
    msg_id = sent.get("id")
    if not msg_id:
        raise RuntimeError(
            f"Gmail send returned no message id (response={sent!r}); "
            "treating as failure rather than reporting a false success."
        )
    logger.info("send_email delivered to %s (gmail id=%s)", to, msg_id)
    return {"id": msg_id, "to": to, "subject": subject, "status": "sent"}


# --------------------------------------------------------------------------- #
# Read / unread + reply tools (added for voice-style workflows)
# --------------------------------------------------------------------------- #
def list_unread(
    limit: int = 10, query: str | None = None, page_token: str | None = None
) -> dict:
    """List unread emails, newest first. Returns paginated envelope."""
    q = "is:unread" + (f" {query}" if query else "")
    return search(q, limit=limit, page_token=page_token)


def mark_read(message_id: str) -> dict:
    """Remove the UNREAD label from a message (i.e. mark it as read)."""
    if settings.use_mock:
        return {"id": message_id, "status": "read", "mock": True}
    service = _service()
    res = (
        service.users()
        .messages()
        .modify(
            userId="me", id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        )
        .execute()
    )
    return {"id": res.get("id", message_id), "status": "read", "labels": res.get("labelIds", [])}


def mark_unread(message_id: str) -> dict:
    """Add the UNREAD label to a message (i.e. mark it as unread)."""
    if settings.use_mock:
        return {"id": message_id, "status": "unread", "mock": True}
    service = _service()
    res = (
        service.users()
        .messages()
        .modify(
            userId="me", id=message_id,
            body={"addLabelIds": ["UNREAD"]},
        )
        .execute()
    )
    return {"id": res.get("id", message_id), "status": "unread", "labels": res.get("labelIds", [])}


def _walk_parts(payload: dict):
    """Yield every part in a Gmail payload (depth-first)."""
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _walk_parts(part)


def list_attachments(message_id: str) -> list[dict]:
    """List the attachments of a Gmail message (filename, size, MIME type, id).

    Returns lightweight metadata only — use `read_attachment` to fetch and
    extract the content of one.
    """
    if settings.use_mock:
        return [{
            "attachment_id": "MOCK_ATT_1", "filename": "mock-contract.pdf",
            "mime_type": "application/pdf", "size_bytes": 12345,
        }]
    service = _service()
    full = (
        service.users().messages()
        .get(userId="me", id=message_id, format="full").execute()
    )
    out: list[dict] = []
    for part in _walk_parts(full.get("payload", {})):
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


def _extract_pdf_text(data: bytes, max_chars: int) -> str:
    """Extract plain text from a PDF byte string (up to `max_chars`)."""
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:  # noqa: BLE001
        return "(pypdf not installed — install pypdf to read PDF attachments)"
    import io
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        return f"(PDF parse error: {exc})"
    out = []
    total = 0
    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            txt = ""
        if not txt:
            continue
        out.append(txt)
        total += len(txt)
        if total >= max_chars:
            break
    return ("\n\n".join(out))[:max_chars]


def _extract_docx_text(data: bytes, max_chars: int) -> str:
    """Best-effort DOCX text extraction without a heavy dependency."""
    import io
    import re
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            with z.open("word/document.xml") as f:
                xml = f.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return f"(DOCX parse error: {exc})"
    text = re.sub(r"<[^>]+>", " ", xml)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _sniff_mime(data: bytes) -> str:
    """Best-effort MIME detection from leading bytes."""
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:2] == b"PK":  # ZIP container — DOCX/XLSX/PPTX/ODT
        # Peek for the DOCX-specific path inside the zip
        if b"word/document.xml" in data[:4096]:
            return (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            )
        return "application/zip"
    try:
        data[:512].decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def read_attachment(
    message_id: str, attachment_id: str, max_chars: int = 12000
) -> dict:
    """Fetch a Gmail attachment and extract its content as text.

    Supports PDF (via pypdf), DOCX (lightweight XML extract), and any text/*
    MIME type. Returns {filename, mime_type, size_bytes, text, truncated}.
    For binary attachments we can't parse, `text` is empty and `note` explains.
    """
    if settings.use_mock:
        return {
            "filename": "mock.pdf", "mime_type": "application/pdf",
            "size_bytes": 1234, "text": "Mock PDF content...",
            "truncated": False, "mock": True,
        }

    service = _service()

    # Download the attachment bytes first. The attachmentId field is the
    # primary key for this operation; the metadata-lookup below is for
    # filename + mime_type display only (Gmail can regenerate attachmentIds
    # between message.get calls, so we sniff bytes as a fallback).
    att = (
        service.users().messages().attachments()
        .get(userId="me", messageId=message_id, id=attachment_id).execute()
    )
    data = base64.urlsafe_b64decode(att.get("data", ""))

    # Try to find the part for filename + mime_type — may miss if Gmail
    # rotated the attachment IDs between calls.
    filename = ""
    mime_type = ""
    full = (
        service.users().messages()
        .get(userId="me", id=message_id, format="full").execute()
    )
    target_part = None
    for part in _walk_parts(full.get("payload", {})):
        body = part.get("body", {}) or {}
        if body.get("attachmentId") == attachment_id:
            target_part = part
            break
        # Fallback: match by size (Gmail attachment size doesn't change).
        if body.get("size") == len(data) and part.get("filename"):
            target_part = part
            # Don't break — a direct attachmentId match would override
    if target_part is not None:
        filename = target_part.get("filename") or ""
        mime_type = target_part.get("mimeType") or ""
    if not mime_type:
        mime_type = _sniff_mime(data)

    text = ""
    note = ""
    if mime_type == "application/pdf" or filename.lower().endswith(".pdf"):
        text = _extract_pdf_text(data, max_chars)
    elif mime_type.startswith("text/"):
        text = data.decode("utf-8", errors="replace")[:max_chars]
    elif (
        mime_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or filename.lower().endswith(".docx")
    ):
        text = _extract_docx_text(data, max_chars)
    else:
        note = f"Unsupported MIME type for inline text: {mime_type}"

    return {
        "filename": filename,
        "mime_type": mime_type,
        "size_bytes": len(data),
        "text": text,
        "truncated": len(text) >= max_chars,
        **({"note": note} if note else {}),
    }


_GDOC_URL_RE = None


def _gdoc_id(url_or_id: str) -> str:
    """Extract a Google Docs file ID from a URL, or return as-is if already an ID."""
    import re
    global _GDOC_URL_RE
    if _GDOC_URL_RE is None:
        _GDOC_URL_RE = re.compile(
            r"docs\.google\.com/(?:document|spreadsheets|presentation)/d/([a-zA-Z0-9_-]+)"
        )
    m = _GDOC_URL_RE.search(url_or_id)
    if m:
        return m.group(1)
    # Already a file ID? Accept any URL-safe-looking string.
    return url_or_id.strip()


def read_gdoc(url_or_id: str, max_chars: int = 20000) -> dict:
    """Fetch a Google Doc (or Sheet/Slides) as plain text via Drive Export.

    Accepts either a full sharing URL (e.g. https://docs.google.com/document/d/<ID>/edit)
    or a raw file ID. For Docs we export as text/plain; for Sheets we use CSV;
    for Slides we fall back to text/plain.
    """
    file_id = _gdoc_id(url_or_id)
    if settings.use_mock:
        return {
            "file_id": file_id, "name": "Mock Doc", "text": "Mock document content.",
            "truncated": False, "mock": True,
        }

    from ..google_auth import build_service

    drive = build_service("drive", "v3")
    meta = drive.files().get(
        fileId=file_id, fields="id,name,mimeType", supportsAllDrives=True,
    ).execute()
    mime = meta.get("mimeType", "")
    if mime == "application/vnd.google-apps.spreadsheet":
        export_mime = "text/csv"
    else:
        export_mime = "text/plain"
    # files.export doesn't accept supportsAllDrives (it operates on file
    # content, not metadata), but the prior .get() validates access to
    # the Shared Drive file — if that succeeded, export will too.
    raw = drive.files().export(fileId=file_id, mimeType=export_mime).execute()
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig", errors="replace")  # strip UTF-8 BOM if present
    else:
        text = str(raw)
    return {
        "file_id": file_id,
        "name": meta.get("name", ""),
        "mime_type": mime,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


def reply(message_id: str, body: str, send: bool = True) -> dict:
    """Reply to a Gmail message, properly threaded.

    Looks up the original message's From/Subject/Message-Id/References headers,
    composes a threaded reply, and either sends it (`send=True`) or saves it
    as a draft (`send=False`). The recipient is taken from the original
    message's `Reply-To` if present, else `From`.
    """
    if settings.use_mock:
        return {
            "id": "MOCK_REPLY", "in_reply_to": message_id,
            "status": "sent" if send else "draft", "mock": True,
        }

    service = _service()
    original = (
        service.users()
        .messages()
        .get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["From", "Reply-To", "To", "Subject", "Message-ID", "References"],
        )
        .execute()
    )
    thread_id = original.get("threadId")
    headers = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}

    reply_to = headers.get("Reply-To") or headers.get("From") or ""
    subject = headers.get("Subject", "")
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    msg = EmailMessage()
    msg["To"] = reply_to
    msg["From"] = settings.google_user_email
    msg["Subject"] = subject
    original_mid = headers.get("Message-ID") or headers.get("Message-Id")
    if original_mid:
        msg["In-Reply-To"] = original_mid
        # References should chain: existing References + original Message-ID.
        existing_refs = headers.get("References", "").strip()
        msg["References"] = (existing_refs + " " + original_mid).strip()
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    body_payload = {"raw": raw, "threadId": thread_id}
    if send:
        result = service.users().messages().send(userId="me", body=body_payload).execute()
        msg_id = result.get("id")
        if not msg_id:
            raise RuntimeError(
                f"Gmail reply.send returned no message id (response={result!r}); "
                "treating as failure rather than reporting a false success."
            )
        return {
            "id": msg_id, "thread_id": thread_id,
            "to": reply_to, "subject": subject, "status": "sent",
        }
    drafted = (
        service.users().drafts().create(userId="me", body={"message": body_payload}).execute()
    )
    draft_id = drafted.get("id")
    if not draft_id:
        raise RuntimeError(
            f"Gmail draft create returned no id (response={drafted!r})."
        )
    return {
        "id": draft_id, "thread_id": thread_id,
        "to": reply_to, "subject": subject, "status": "draft",
    }
