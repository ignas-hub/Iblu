"""Reading Google Drive — everything that looks, nothing that touches.

Until this existed IBLU could list a folder and read a Doc, and that was the
whole of its Drive eyesight. "Which file did she share with me yesterday?",
"who can see this?", "what did this look like before Tuesday?", "how full is
my Drive?" each needed a new tool, and Ignas said it out loud: "It's crazy how
many times I have to ask to add tools." This module is the batch that ends
that for Drive reads.

It is ONE MCP tool with an ``action`` parameter, not nine, because of the rule
in HANDOFF.md §12: one tool per domain × side-effect class. Every new
``@mcp.tool`` costs Ignas a permission click, forever, and a changed schema
costs it again. Reads for Drive live here; writes live in ``drive_manage``.
Nothing in this module writes, moves, shares or deletes — it only calls
``get``/``list``/``export``/``about``, so it belongs in the read-only group
he can allow once.

Actions (all read-only):

  search          Drive ``q`` query string, or plain words (-> fullText contains).
  get             One file's metadata, incl. parents, sharing and permissions.
  recent          Most recently modified files.
  shared_with_me  Files other people shared with this account.
  permissions     Who has which role on a file.
  revisions       Version history: when, by whom.
  export_text     Plain text of a Doc/Sheet/Slides, 20,000 chars per call,
                  paged with ``offset`` exactly like the ``repo`` tool.
  comments        Every comment on a file, resolved ones included, with ids.
  storage         The account's quota, in GB.
"""

from __future__ import annotations

import logging
import re

from ..config import resolve_account, settings
from .drive import (
    _docs,  # noqa: F401 — kept importable so callers/tests see one surface
    _drive,
    _file_id,
    _mock,
    _viewable_url,
    friendly_google_errors,
)
from .repo import MAX_CHARS, window

logger = logging.getLogger("iblu_keeper.tools.drive_read")

ACTIONS = (
    "search", "get", "recent", "shared_with_me", "permissions", "revisions",
    "export_text", "comments", "storage",
)

_GOOGLE_NATIVE = "application/vnd.google-apps."
_SHEET = "application/vnd.google-apps.spreadsheet"

# A query that already speaks Drive's search grammar is passed through as is;
# anything else is treated as words to look for in the file's full text.
_DRIVE_Q_RE = re.compile(
    r"(?:\b(?:name|fullText|mimeType|modifiedTime|createdTime|viewedByMeTime|"
    r"trashed|starred|sharedWithMe|visibility|properties|appProperties)\b"
    r"\s*(?:contains|=|!=|<=|>=|<|>|has))"
    r"|(?:\bin\s+(?:parents|owners|writers|readers)\b)",
    re.I,
)

_LIST_FIELDS = (
    "files(id,name,mimeType,modifiedTime,owners(displayName,emailAddress),"
    "webViewLink,size,sharingUser(displayName,emailAddress),sharedWithMeTime),"
    "nextPageToken"
)
_GET_FIELDS = (
    "id,name,mimeType,modifiedTime,createdTime,size,parents,shared,description,"
    "starred,trashed,owners(displayName,emailAddress),"
    "lastModifyingUser(displayName,emailAddress),webViewLink,driveId,"
    "shortcutDetails,permissions(id,type,role,emailAddress,displayName,domain)"
)
_PERMISSION_FIELDS = (
    "permissions(id,type,role,emailAddress,displayName,domain,deleted,"
    "expirationTime,pendingOwner),nextPageToken"
)
_REVISION_FIELDS = (
    "revisions(id,modifiedTime,lastModifyingUser(displayName,emailAddress),"
    "size,keepForever,originalFilename),nextPageToken"
)
_COMMENT_FIELDS = (
    "comments(id,content,resolved,deleted,quotedFileContent,"
    "author(displayName,emailAddress),createdTime,modifiedTime,htmlLink,"
    "replies(id,content,action,author(displayName),createdTime)),nextPageToken"
)


class DriveReadError(ValueError):
    """Bad input to `drive_read`. The message always names what was expected."""


def _escape(value: str) -> str:
    """Escape a literal for inside single quotes in a Drive ``q`` string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def looks_like_drive_query(query: str) -> bool:
    return bool(_DRIVE_Q_RE.search(query))


def _page_size(limit: int) -> int:
    return max(1, min(int(limit), 100))


def _file_row(f: dict) -> dict:
    """One file, the way every listing action reports it."""
    row = {
        "id": f.get("id", ""),
        "name": f.get("name", ""),
        "mime_type": f.get("mimeType", ""),
        "modified_time": f.get("modifiedTime", ""),
        "owners": [
            o.get("emailAddress") or o.get("displayName") or ""
            for o in f.get("owners", []) or []
        ],
        "url": f.get("webViewLink") or _viewable_url(f.get("id", ""), f.get("mimeType", "")),
        "size_bytes": int(f["size"]) if f.get("size") else None,
    }
    if f.get("sharingUser"):
        su = f["sharingUser"]
        row["shared_by"] = su.get("emailAddress") or su.get("displayName") or ""
    if f.get("sharedWithMeTime"):
        row["shared_time"] = f["sharedWithMeTime"]
    return row


def _list(drive, q: str, order_by: str, limit: int, page_token: str | None) -> dict:
    kwargs: dict = {
        "q": q,
        "pageSize": _page_size(limit),
        "fields": _LIST_FIELDS,
        "orderBy": order_by,
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "corpora": "allDrives",
    }
    if page_token:
        kwargs["pageToken"] = page_token
    resp = drive.files().list(**kwargs).execute()
    items = [_file_row(f) for f in resp.get("files", []) or []]
    return {
        "items": items,
        "count": len(items),
        "next_page_token": resp.get("nextPageToken") or None,
    }


def _gb(value) -> float | None:
    """Bytes (Drive sends them as strings) to GB, or None when not reported."""
    if value in (None, ""):
        return None
    return round(int(value) / 1_000_000_000, 2)


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def search(
    query: str, limit: int = 20, page_token: str | None = None, account: str | None = None,
) -> dict:
    """Find files. ``query`` is a Drive ``q`` string if it reads like one
    (``name contains 'x'``, ``'<id>' in parents``, ``mimeType = ...``);
    otherwise it is words, searched for in the file's full text and title.
    Trashed files are excluded unless the query mentions ``trashed`` itself."""
    if not query or not query.strip():
        raise DriveReadError("search needs a query")
    query = query.strip()
    if looks_like_drive_query(query):
        q = query
        if "trashed" not in query.lower():
            q = f"({query}) and trashed = false"
        kind = "drive_q"
    else:
        q = f"fullText contains '{_escape(query)}' and trashed = false"
        kind = "words"
    drive = _drive(account)
    with friendly_google_errors(account, "Drive search"):
        out = _list(drive, q, "modifiedTime desc", limit, page_token)
    return {"query": query, "query_kind": kind, "q": q, **out, "status": "searched"}


def get(file_id_or_url: str, account: str | None = None) -> dict:
    """Everything Drive knows about one file, including who it is shared with."""
    file_id = _file_id(file_id_or_url)
    drive = _drive(account)
    with friendly_google_errors(account, "this Drive file"):
        f = drive.files().get(
            fileId=file_id, fields=_GET_FIELDS, supportsAllDrives=True,
        ).execute()
    perms = f.get("permissions", []) or []
    summary = []
    for p in perms:
        who = p.get("emailAddress") or p.get("domain") or p.get("type", "")
        summary.append(f"{who}: {p.get('role', '')}")
    return {
        **_file_row(f),
        "created_time": f.get("createdTime", ""),
        "description": f.get("description", "") or "",
        "parents": f.get("parents", []) or [],
        "drive_id": f.get("driveId"),
        "shared": bool(f.get("shared", False)),
        "starred": bool(f.get("starred", False)),
        "trashed": bool(f.get("trashed", False)),
        "last_modified_by": (f.get("lastModifyingUser") or {}).get("emailAddress")
        or (f.get("lastModifyingUser") or {}).get("displayName") or "",
        "shortcut_target": (f.get("shortcutDetails") or {}).get("targetId"),
        "permissions_summary": summary,
        "permission_count": len(perms),
        "status": "read",
    }


def recent(limit: int = 20, page_token: str | None = None, account: str | None = None) -> dict:
    """Files most recently modified by anyone, newest first."""
    drive = _drive(account)
    with friendly_google_errors(account, "Drive"):
        out = _list(drive, "trashed = false", "modifiedTime desc", limit, page_token)
    return {**out, "status": "read"}


def shared_with_me(
    limit: int = 20, page_token: str | None = None, account: str | None = None,
) -> dict:
    """Files other people have shared with this account, newest share first."""
    drive = _drive(account)
    with friendly_google_errors(account, "Drive"):
        out = _list(
            drive, "sharedWithMe = true and trashed = false",
            "sharedWithMeTime desc", limit, page_token,
        )
    return {**out, "status": "read"}


def permissions(file_id_or_url: str, account: str | None = None) -> dict:
    """Who holds which role on a file (owner / writer / commenter / reader)."""
    file_id = _file_id(file_id_or_url)
    drive = _drive(account)
    with friendly_google_errors(account, "this file's permissions"):
        resp = drive.permissions().list(
            fileId=file_id, fields=_PERMISSION_FIELDS, pageSize=100,
            supportsAllDrives=True,
        ).execute()
    items = [
        {
            "permission_id": p.get("id", ""),
            "type": p.get("type", ""),
            "role": p.get("role", ""),
            "email": p.get("emailAddress"),
            "name": p.get("displayName"),
            "domain": p.get("domain"),
            "expires": p.get("expirationTime"),
            "pending_owner": bool(p.get("pendingOwner", False)),
        }
        for p in resp.get("permissions", []) or []
        if not p.get("deleted")
    ]
    return {"id": file_id, "items": items, "count": len(items), "status": "read"}


def revisions(
    file_id_or_url: str, limit: int = 20, page_token: str | None = None,
    account: str | None = None,
) -> dict:
    """Version history: each revision's time and who made it."""
    file_id = _file_id(file_id_or_url)
    drive = _drive(account)
    kwargs: dict = {"fileId": file_id, "fields": _REVISION_FIELDS, "pageSize": _page_size(limit)}
    if page_token:
        kwargs["pageToken"] = page_token
    with friendly_google_errors(account, "this file's revisions"):
        resp = drive.revisions().list(**kwargs).execute()
    items = [
        {
            "revision_id": r.get("id", ""),
            "modified_time": r.get("modifiedTime", ""),
            "modified_by": (r.get("lastModifyingUser") or {}).get("emailAddress")
            or (r.get("lastModifyingUser") or {}).get("displayName") or "",
            "size_bytes": int(r["size"]) if r.get("size") else None,
            "keep_forever": bool(r.get("keepForever", False)),
        }
        for r in resp.get("revisions", []) or []
    ]
    return {
        "id": file_id, "items": items, "count": len(items),
        "next_page_token": resp.get("nextPageToken") or None, "status": "read",
    }


def export_text(
    file_id_or_url: str, max_chars: int = MAX_CHARS, offset: int = 0,
    account: str | None = None,
) -> dict:
    """Plain text of a Google Doc, Sheet (CSV) or Slides deck.

    Capped at 20,000 characters per call. A truncated result carries
    ``next_offset``; call again with ``offset`` until ``truncated`` is false,
    exactly as the ``repo`` tool pages a long file.
    """
    file_id = _file_id(file_id_or_url)
    drive = _drive(account)
    with friendly_google_errors(account, "this file"):
        meta = drive.files().get(
            fileId=file_id, fields="id,name,mimeType", supportsAllDrives=True,
        ).execute()
    mime = meta.get("mimeType", "")
    if not mime.startswith(_GOOGLE_NATIVE):
        raise DriveReadError(
            f"export_text only works for Google Docs, Sheets and Slides; "
            f"{meta.get('name', file_id)!r} is {mime or 'of unknown type'}."
        )
    export_mime = "text/csv" if mime == _SHEET else "text/plain"
    # files.export does not accept supportsAllDrives; the get() above already
    # validated access to the file.
    with friendly_google_errors(account, "this file"):
        raw = drive.files().export(fileId=file_id, mimeType=export_mime).execute()
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else str(raw)
    return {
        "id": file_id,
        "name": meta.get("name", ""),
        "mime_type": mime,
        "export_mime": export_mime,
        **window(text, max_chars, offset),
        "url": _viewable_url(file_id, mime),
        "status": "read",
    }


def comments(
    file_id_or_url: str, limit: int = 50, page_token: str | None = None,
    account: str | None = None,
) -> dict:
    """Every comment on a file — resolved ones too — with the ids that
    ``gdoc_resolve_comment`` needs, and each thread's replies."""
    file_id = _file_id(file_id_or_url)
    drive = _drive(account)
    kwargs: dict = {
        "fileId": file_id, "fields": _COMMENT_FIELDS,
        "pageSize": _page_size(limit), "includeDeleted": False,
    }
    if page_token:
        kwargs["pageToken"] = page_token
    with friendly_google_errors(account, "this file's comments"):
        resp = drive.comments().list(**kwargs).execute()
    items = []
    for c in resp.get("comments", []) or []:
        items.append({
            "comment_id": c.get("id", ""),
            "comment": c.get("content", "") or "",
            "resolved": bool(c.get("resolved", False)),
            "anchor_text": (c.get("quotedFileContent") or {}).get("value", ""),
            "author": (c.get("author") or {}).get("emailAddress")
            or (c.get("author") or {}).get("displayName") or "",
            "created_time": c.get("createdTime", ""),
            "modified_time": c.get("modifiedTime", ""),
            "replies": [
                {
                    "reply_id": r.get("id", ""),
                    "reply": r.get("content", "") or "",
                    "action": r.get("action"),
                    "author": (r.get("author") or {}).get("displayName", ""),
                    "created_time": r.get("createdTime", ""),
                }
                for r in c.get("replies", []) or []
            ],
        })
    return {
        "id": file_id,
        "items": items,
        "count": len(items),
        "open_count": sum(1 for i in items if not i["resolved"]),
        "next_page_token": resp.get("nextPageToken") or None,
        "status": "read",
    }


def storage(account: str | None = None) -> dict:
    """The account's Drive quota in GB. A Workspace account with no limit
    reports ``limit_gb: None`` and no percentage — a number that cannot be
    measured is not printed."""
    drive = _drive(account)
    with friendly_google_errors(account, "Drive storage"):
        about = drive.about().get(
            fields="storageQuota,user(emailAddress,displayName)",
        ).execute()
    quota = about.get("storageQuota", {}) or {}
    limit_gb = _gb(quota.get("limit"))
    used_gb = _gb(quota.get("usage"))
    out = {
        "account_email": (about.get("user") or {}).get("emailAddress", ""),
        "limit_gb": limit_gb,
        "used_gb": used_gb,
        "used_in_drive_gb": _gb(quota.get("usageInDrive")),
        "used_in_trash_gb": _gb(quota.get("usageInDriveTrash")),
        "status": "read",
    }
    if limit_gb and used_gb is not None:
        out["percent_used"] = round(100 * used_gb / limit_gb, 1)
    else:
        out["percent_used"] = None
        out["note"] = "no storage limit reported for this account, so no percentage"
    return out


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
def drive_read(
    action: str,
    file_id_or_url: str | None = None,
    query: str | None = None,
    limit: int = 20,
    page_token: str | None = None,
    account: str | None = None,
    **kw,
) -> dict:
    """Read Google Drive. One tool, nine views — see the module docstring.

    ``file_id_or_url`` is needed by get, permissions, revisions, export_text
    and comments; ``query`` by search. ``export_text`` also takes
    ``max_chars`` and ``offset`` (keyword) for paging. ``account`` — which
    Drive: ``blt`` (default), ``deadlift``, ``choco``.
    """
    if action not in ACTIONS:
        raise DriveReadError(
            f"unknown action {action!r} — expected one of: {', '.join(ACTIONS)}"
        )
    unknown = set(kw) - {"max_chars", "offset"}
    if unknown:
        raise DriveReadError(f"unexpected parameter(s): {', '.join(sorted(unknown))}")
    needs_file = {"get", "permissions", "revisions", "export_text", "comments"}
    if action in needs_file and not file_id_or_url:
        raise DriveReadError(f"{action} needs file_id_or_url")
    if action == "search" and not (query and query.strip()):
        raise DriveReadError("search needs a query")

    account = resolve_account(account)
    if settings.use_mock:
        return _mock({
            "action": action, "id": file_id_or_url, "items": [], "count": 0,
            "status": "not_read_mock",
        })

    if action == "search":
        return search(query or "", limit, page_token, account)
    if action == "get":
        return get(file_id_or_url or "", account)
    if action == "recent":
        return recent(limit, page_token, account)
    if action == "shared_with_me":
        return shared_with_me(limit, page_token, account)
    if action == "permissions":
        return permissions(file_id_or_url or "", account)
    if action == "revisions":
        return revisions(file_id_or_url or "", limit, page_token, account)
    if action == "export_text":
        return export_text(
            file_id_or_url or "",
            max_chars=int(kw.get("max_chars", MAX_CHARS)),
            offset=int(kw.get("offset", 0)),
            account=account,
        )
    if action == "comments":
        return comments(file_id_or_url or "", limit, page_token, account)
    return storage(account)
