"""Gmail tools via the delegated service account.

Gmail via service account is known-good (Ignas has used this pattern before).
In dry-run / no-credentials mode these return deterministic mock data.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage

from ..config import settings
from ..store import drafts


def _service():
    from ..google_auth import build_service

    return build_service("gmail", "v1")


def _build_raw(to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = settings.google_delegated_user
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def search(query: str, limit: int = 20) -> list[dict]:
    """Search Gmail. Returns lightweight message summaries (newest first)."""
    if settings.use_mock:
        return [
            {
                "id": "MOCK_MSG_1",
                "thread_id": "MOCK_THREAD_1",
                "from": "client@example.com",
                "subject": f"Re: {query or 'Proposal'}",
                "snippet": "Thanks, this looks good. One question about pricing...",
                "date": "2026-06-10T08:30:00Z",
            },
            {
                "id": "MOCK_MSG_2",
                "thread_id": "MOCK_THREAD_2",
                "from": "noreply@calendar.google.com",
                "subject": "Invitation: Sales sync @ Wed 2pm",
                "snippet": "You have been invited to the following event...",
                "date": "2026-06-09T17:05:00Z",
            },
        ]

    service = _service()
    resp = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=limit)
        .execute()
    )
    out: list[dict] = []
    for ref in resp.get("messages", []):
        full = (
            service.users()
            .messages()
            .get(userId="me", id=ref["id"], format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
        out.append(
            {
                "id": full.get("id"),
                "thread_id": full.get("threadId"),
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "snippet": full.get("snippet", ""),
                "date": headers.get("Date", ""),
            }
        )
    return out


def get_message(message_id: str) -> dict:
    """Fetch a single message with its plain-text body."""
    if settings.use_mock:
        return {
            "id": message_id,
            "from": "client@example.com",
            "to": settings.google_delegated_user,
            "subject": "Re: Proposal",
            "date": "2026-06-10T08:30:00Z",
            "body": "Thanks, this looks good. One question about pricing — "
            "can we do a 12-month term?",
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
    """Send an email immediately. In mock mode, returns a fake confirmation."""
    if settings.use_mock:
        return {
            "id": "MOCK_SENT_EMAIL",
            "to": to,
            "subject": subject,
            "status": "sent",
            "mock": True,
        }

    service = _service()
    sent = (
        service.users()
        .messages()
        .send(userId="me", body={"raw": _build_raw(to, subject, body)})
        .execute()
    )
    return {"id": sent.get("id"), "to": to, "subject": subject, "status": "sent"}
