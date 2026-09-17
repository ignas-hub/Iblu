"""Google Drive + Google Docs edit tools.

Covers the workflows Ignas actually needs:

  - Create / append / find-replace / rename / move Google Docs.
  - Create folders, list folder contents.
  - Save Gmail attachments straight to a Drive folder (zero round-trip).
  - Upload from a URL to Drive.

Read operations on Google Docs live in ``tools.gmail`` (alongside Gmail
attachment reading — see ``gmail.read_gdoc``).
"""

from __future__ import annotations

import io
import logging
import re
from contextlib import contextmanager

from ..config import resolve_account, settings


logger = logging.getLogger("iblu_keeper.tools.drive")


_GDOC_URL_RE = re.compile(
    r"docs\.google\.com/(?:document|spreadsheets|presentation)/d/([a-zA-Z0-9_-]+)"
)
_DRIVE_FOLDER_URL_RE = re.compile(r"drive\.google\.com/drive/folders/([a-zA-Z0-9_-]+)")


def _file_id(url_or_id: str) -> str:
    """Extract a Drive file id from a URL, or pass through if already an id."""
    for pat in (_GDOC_URL_RE, _DRIVE_FOLDER_URL_RE):
        m = pat.search(url_or_id)
        if m:
            return m.group(1)
    return url_or_id.strip()


def _docs(account: str | None = None):
    from ..google_auth import build_service

    return build_service("docs", "v1", account=account)


def _drive(account: str | None = None):
    from ..google_auth import build_service

    return build_service("drive", "v3", account=account)


def _viewable_url(file_id: str, mime_type: str = "") -> str:
    """A canonical URL the user can click to open the file."""
    if mime_type == "application/vnd.google-apps.document":
        return f"https://docs.google.com/document/d/{file_id}/edit"
    if mime_type == "application/vnd.google-apps.spreadsheet":
        return f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
    if mime_type == "application/vnd.google-apps.presentation":
        return f"https://docs.google.com/presentation/d/{file_id}/edit"
    if mime_type == "application/vnd.google-apps.folder":
        return f"https://drive.google.com/drive/folders/{file_id}"
    return f"https://drive.google.com/file/d/{file_id}/view"


def _mock(payload: dict) -> dict:
    return {"_mock": True, "note": "MOCK MODE — Drive operation NOT performed.", **payload}


# --------------------------------------------------------------------------- #
# Shared friendly-error helpers for Docs/Sheets full-power tools
# --------------------------------------------------------------------------- #
def _http_status(exc) -> int:
    try:
        status = getattr(getattr(exc, "resp", None), "status", None) or getattr(
            exc, "status_code", None
        )
        return int(status) if status is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def _http_detail(exc) -> str:
    try:
        import json as _json

        content = getattr(exc, "content", None)
        body = _json.loads(content.decode("utf-8")) if content else {}
        return ((body.get("error") or {}).get("message")) or ""
    except Exception:  # noqa: BLE001
        return ""


def friendly_access_error(exc: Exception, account: str | None, what: str) -> RuntimeError:
    """Turn a Drive/Docs/Sheets HttpError into a message that names the account.

    ``what`` is a short noun phrase for the resource being accessed (e.g.
    "this Google Doc", "this spreadsheet"). Two cases get a specific,
    actionable message:

    - The API required for this call is not enabled in that account's Cloud
      project (Google's own 403 body says so) — tell the human which account
      and that the API needs enabling.
    - A plain 403/404 on the file — the account may simply not have access
      to it (it may live in a different Workspace) — name the alias and
      suggest the ``account`` parameter.

    Anything else falls back to a generic, still-actionable message. Never
    raises itself — always returns a RuntimeError for the caller to raise.
    """
    alias = account or settings.primary_alias
    status = _http_status(exc)
    detail = _http_detail(exc)
    low = detail.lower()
    if status == 403 and ("has not been used in project" in low or "it is disabled" in low):
        api_name = "The Google Sheets API" if "sheets" in low else "A required Google API"
        return RuntimeError(
            f"{api_name} needs to be enabled in the '{alias}' account's Google "
            f"Cloud project before this will work. Google said: {detail}"
        )
    if status in (403, 404):
        return RuntimeError(
            f"The '{alias}' account may not have access to {what} (HTTP {status}). "
            "The file may belong to a different Google Workspace — pass the "
            "`account` parameter (blt, deadlift, or choco) to try another account."
        )
    return RuntimeError(
        f"Google API error accessing {what} (HTTP {status or '?'}): {detail or exc}"
    )


@contextmanager
def friendly_google_errors(account: str | None, what: str):
    """Context manager: turn any HttpError raised inside into `friendly_access_error`.

    Use around individual Docs/Sheets/Drive API calls in the full-power
    tools, so a disabled-API or access-denied 403 gets a message naming the
    account instead of a generic stack trace.
    """
    try:
        from googleapiclient.errors import HttpError  # type: ignore
    except Exception:  # pragma: no cover
        HttpError = Exception  # type: ignore[assignment]  # noqa: N806

    try:
        yield
    except HttpError as exc:  # type: ignore[misc]
        raise friendly_access_error(exc, account, what) from exc


# --------------------------------------------------------------------------- #
# Google Docs editing
# --------------------------------------------------------------------------- #
def gdoc_create(title: str, content: str = "", folder_id: str | None = None) -> dict:
    """Create a new Google Doc with optional initial body text."""
    if settings.use_mock:
        return _mock({"id": "MOCK_DOC", "name": title, "status": "not_created_mock"})

    docs = _docs()
    drive = _drive()
    doc = docs.documents().create(body={"title": title}).execute()
    doc_id = doc.get("documentId")
    if not doc_id:
        raise RuntimeError(f"Docs create returned no documentId (response={doc!r}).")

    if content:
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
        ).execute()

    if folder_id:
        folder_id = _file_id(folder_id)
        # New docs are created in the user's My Drive root; move into the
        # requested folder by adding it as a parent and removing the existing
        # one.
        meta = drive.files().get(
            fileId=doc_id, fields="parents", supportsAllDrives=True,
        ).execute()
        prev = ",".join(meta.get("parents", []) or [])
        drive.files().update(
            fileId=doc_id,
            addParents=folder_id,
            removeParents=prev,
            fields="id,parents",
            supportsAllDrives=True,
        ).execute()

    logger.info("gdoc_create: id=%s title=%r folder=%s", doc_id, title, folder_id)
    return {
        "id": doc_id,
        "name": title,
        "url": _viewable_url(doc_id, "application/vnd.google-apps.document"),
        "status": "created",
    }


def gdoc_append(doc_id_or_url: str, text: str) -> dict:
    """Append text to the end of an existing Google Doc.

    Preserves all prior content. Adds a leading newline if the doc already
    has content so the new text starts on a fresh line.
    """
    if settings.use_mock:
        return _mock({"id": doc_id_or_url, "status": "not_modified_mock"})

    doc_id = _file_id(doc_id_or_url)
    docs = _docs()
    # endIndex of the last segment minus 1 = insertion point right before
    # the doc's trailing empty paragraph.
    doc = docs.documents().get(documentId=doc_id, fields="body.content/endIndex,title").execute()
    body = doc.get("body", {}).get("content", [])
    end = max((seg.get("endIndex", 1) for seg in body), default=1) - 1
    payload = ("\n" + text) if end > 1 else text
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": end}, "text": payload}}]},
    ).execute()
    return {
        "id": doc_id, "name": doc.get("title", ""),
        "appended_chars": len(payload),
        "url": _viewable_url(doc_id, "application/vnd.google-apps.document"),
        "status": "appended",
    }


def gdoc_replace_text(
    doc_id_or_url: str, find: str, replace_with: str, match_case: bool = False
) -> dict:
    """Find-and-replace text in a Google Doc.

    Replaces ALL occurrences of ``find`` with ``replace_with``. Set
    ``match_case=True`` for case-sensitive matching.
    """
    if settings.use_mock:
        return _mock({"id": doc_id_or_url, "status": "not_modified_mock"})

    doc_id = _file_id(doc_id_or_url)
    docs = _docs()
    result = docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"replaceAllText": {
            "containsText": {"text": find, "matchCase": match_case},
            "replaceText": replace_with,
        }}]},
    ).execute()
    occurrences = 0
    for rep in result.get("replies", []):
        occurrences += rep.get("replaceAllText", {}).get("occurrencesChanged", 0)
    return {
        "id": doc_id,
        "find": find,
        "replace_with": replace_with,
        "occurrences": occurrences,
        "url": _viewable_url(doc_id, "application/vnd.google-apps.document"),
        "status": "replaced",
    }


def gdoc_rename(doc_id_or_url: str, new_name: str) -> dict:
    """Rename a Drive file (works for Docs / Sheets / Slides / any file)."""
    if settings.use_mock:
        return _mock({"id": doc_id_or_url, "name": new_name, "status": "not_renamed_mock"})

    file_id = _file_id(doc_id_or_url)
    drive = _drive()
    res = drive.files().update(
        fileId=file_id, body={"name": new_name}, fields="id,name,mimeType",
        supportsAllDrives=True,
    ).execute()
    return {
        "id": res["id"], "name": res["name"],
        "url": _viewable_url(res["id"], res.get("mimeType", "")),
        "status": "renamed",
    }


def gdoc_move(file_id_or_url: str, folder_id_or_url: str) -> dict:
    """Move a Drive file into the given folder.

    Replaces the file's existing parent(s) with the target folder.
    """
    if settings.use_mock:
        return _mock({"id": file_id_or_url, "status": "not_moved_mock"})

    file_id = _file_id(file_id_or_url)
    folder_id = _file_id(folder_id_or_url)
    drive = _drive()
    meta = drive.files().get(
        fileId=file_id, fields="parents,name,mimeType", supportsAllDrives=True,
    ).execute()
    prev = ",".join(meta.get("parents", []) or [])
    res = drive.files().update(
        fileId=file_id,
        addParents=folder_id,
        removeParents=prev,
        fields="id,parents,name,mimeType",
        supportsAllDrives=True,
    ).execute()
    return {
        "id": res["id"], "name": res.get("name", ""),
        "parents": res.get("parents", []),
        "url": _viewable_url(res["id"], res.get("mimeType", "")),
        "status": "moved",
    }


# --------------------------------------------------------------------------- #
# Drive folders + uploads
# --------------------------------------------------------------------------- #
def drive_create_folder(name: str, parent_id: str | None = None) -> dict:
    """Create a folder in Drive (optionally under a parent folder)."""
    if settings.use_mock:
        return _mock({"id": "MOCK_FOLDER", "name": name, "status": "not_created_mock"})

    drive = _drive()
    body: dict = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [_file_id(parent_id)]
    res = drive.files().create(
        body=body, fields="id,name,mimeType", supportsAllDrives=True,
    ).execute()
    return {
        "id": res["id"], "name": res["name"],
        "url": _viewable_url(res["id"], res.get("mimeType", "")),
        "status": "created",
    }


def drive_list_folder(
    folder_id_or_url: str | None = None,
    query: str | None = None,
    limit: int = 20,
    page_token: str | None = None,
    account: str | None = None,
) -> dict:
    """List files/folders inside a Drive folder (or matching a query).

    Without ``folder_id``, searches the user's whole Drive by name via
    ``query`` (e.g. ``"Contracts"`` to find a folder by name). With
    ``folder_id``, lists immediate children of that folder. ``account`` —
    which Drive to search: ``blt`` (default), ``deadlift``, ``choco``.
    """
    account = resolve_account(account)
    if settings.use_mock:
        return {"items": [], "count": 0, "next_page_token": None}

    drive = _drive(account)
    filters = ["trashed = false"]
    if folder_id_or_url:
        fid = _file_id(folder_id_or_url)
        filters.append(f"'{fid}' in parents")
    if query:
        # Escape single quotes by doubling them per Drive query syntax.
        safe = query.replace("'", "\\'")
        filters.append(f"name contains '{safe}'")
    q = " and ".join(filters)
    kwargs: dict = {
        "q": q,
        "pageSize": max(1, min(limit, 100)),
        "fields": "files(id,name,mimeType,modifiedTime,parents,driveId),nextPageToken",
        "orderBy": "modifiedTime desc",
        # Include Shared Drives in the result set; without these flags the
        # API only sees My Drive items.
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "corpora": "allDrives",
    }
    if page_token:
        kwargs["pageToken"] = page_token
    resp = drive.files().list(**kwargs).execute()
    items = []
    for f in resp.get("files", []):
        items.append({
            "id": f["id"],
            "name": f.get("name", ""),
            "mime_type": f.get("mimeType", ""),
            "is_folder": f.get("mimeType") == "application/vnd.google-apps.folder",
            "modified_time": f.get("modifiedTime", ""),
            "url": _viewable_url(f["id"], f.get("mimeType", "")),
        })
    return {
        "items": items,
        "count": len(items),
        "next_page_token": resp.get("nextPageToken") or None,
    }


def _upload_bytes(
    data: bytes,
    filename: str,
    mime_type: str,
    folder_id: str | None,
) -> dict:
    """Internal: upload raw bytes to Drive as a new file."""
    from googleapiclient.http import MediaInMemoryUpload  # type: ignore

    drive = _drive()
    body: dict = {"name": filename, "mimeType": mime_type}
    if folder_id:
        body["parents"] = [_file_id(folder_id)]
    media = MediaInMemoryUpload(data, mimetype=mime_type, resumable=False)
    res = drive.files().create(
        body=body, media_body=media,
        fields="id,name,mimeType,size,webViewLink",
        supportsAllDrives=True,
    ).execute()
    file_id = res.get("id")
    if not file_id:
        raise RuntimeError(f"Drive upload returned no id (response={res!r}).")
    logger.info(
        "drive upload: id=%s name=%r mime=%s bytes=%d", file_id,
        res.get("name", ""), mime_type, len(data),
    )
    return {
        "id": file_id,
        "name": res.get("name", filename),
        "mime_type": res.get("mimeType", mime_type),
        "size_bytes": int(res.get("size") or len(data)),
        "url": res.get("webViewLink") or _viewable_url(file_id, mime_type),
        "status": "uploaded",
    }


def drive_save_gmail_attachment(
    message_id: str,
    attachment_id: str,
    folder_id_or_url: str | None = None,
    filename: str | None = None,
) -> dict:
    """Save a Gmail attachment directly to Drive (no client round-trip).

    Looks the attachment up on the original message for filename + MIME type
    when ``filename`` isn't provided. ``folder_id_or_url`` is the Drive folder
    to save into (omit to save to My Drive root).
    """
    if settings.use_mock:
        return _mock({"id": "MOCK_FILE", "name": filename or "mock.bin",
                      "status": "not_uploaded_mock"})

    # Reuse the Gmail attachment read path; it already handles mime sniffing
    # and the Gmail attachmentId quirks.
    from . import gmail as gmail_tools

    fetched = gmail_tools.read_attachment(message_id, attachment_id, max_chars=0)
    # read_attachment returns text + metadata. We also need the raw bytes.
    # Refetch directly so we get bytes without text-extraction overhead.
    import base64
    service = gmail_tools._service()
    att = (
        service.users().messages().attachments()
        .get(userId="me", messageId=message_id, id=attachment_id).execute()
    )
    data = base64.urlsafe_b64decode(att.get("data", ""))
    name = filename or fetched.get("filename") or "attachment"
    mime = fetched.get("mime_type") or "application/octet-stream"
    return _upload_bytes(data, name, mime, folder_id_or_url)


def drive_upload_from_url(
    url: str,
    filename: str,
    folder_id_or_url: str | None = None,
    mime_type: str | None = None,
) -> dict:
    """Fetch a URL and save its body to Drive as a new file.

    ``mime_type`` is inferred from the response's Content-Type header when
    omitted. For binary downloads (PDFs, images, zips) pass an explicit
    ``mime_type`` to avoid wrong defaults.
    """
    if settings.use_mock:
        return _mock({"id": "MOCK_FILE", "name": filename, "status": "not_uploaded_mock"})

    import requests as _requests

    r = _requests.get(url, timeout=30, stream=True)
    r.raise_for_status()
    data = r.content
    if not mime_type:
        mime_type = (
            r.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
        )
    return _upload_bytes(data, filename, mime_type, folder_id_or_url)


def drive_create_file(
    filename: str,
    content: str,
    folder_id_or_url: str | None = None,
    mime_type: str = "text/plain",
) -> dict:
    """Create a new file in Drive with the given text content.

    Use for "save these notes as a .md file", "drop this JSON into my
    Reports folder", etc. Content is UTF-8-encoded before upload; for
    binary data use ``drive_save_gmail_attachment`` or
    ``drive_upload_from_url`` instead. Set ``mime_type`` to control how
    Drive treats the file (e.g. ``text/markdown``, ``application/json``,
    ``text/csv``).
    """
    if settings.use_mock:
        return _mock({"id": "MOCK_FILE", "name": filename, "status": "not_uploaded_mock"})

    return _upload_bytes(
        content.encode("utf-8"), filename, mime_type, folder_id_or_url,
    )
