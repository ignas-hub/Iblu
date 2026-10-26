"""Changing Google Drive files — the write half of the Drive domain.

IBLU could create, rename and move a file and that was the whole of its
hands. Copying a template, starring something, trashing a duplicate, sharing
a Doc with a collaborator, or dropping a shortcut into a project folder each
meant asking for a new tool — "It's crazy how many times I have to ask to add
tools." This is the batch that ends that for Drive writes.

ONE MCP tool with an ``action`` parameter, per HANDOFF.md §12 (one tool per
domain × side-effect class): every ``@mcp.tool`` costs Ignas a permission
click forever, and a write must never hide inside a read tool to dodge one.
Reads live in ``drive_read``; this module only writes.

What each action writes:

  copy             files.copy  — a new file (optionally named, in a folder).
  rename           files.update name.
  move             files.update addParents/removeParents — replaces parents.
  trash            files.update trashed=true. Recoverable from Drive's Trash
                   for 30 days.
  untrash          files.update trashed=false.
  star / unstar    files.update starred.
  set_description  files.update description.
  share            permissions.create for one email, role viewer / commenter
                   / writer. ``notify=True`` makes Google email that person;
                   it defaults to off because IBLU never sends on Ignas's
                   behalf unless he says go.
  unshare          permissions.delete for that email's permission.
  create_shortcut  files.create of a Drive shortcut pointing at the file.

Two things are deliberately absent:

  * **No permanent delete.** ``files.delete`` skips the Trash and cannot be
    undone from anywhere. ``trash`` is the only removal here; if a file must
    really go, Ignas empties the Trash himself in the Drive UI.
  * **No ownership transfer.** ``share`` refuses ``role="owner"`` outright.
    Giving a file away is a one-way door that also changes whose quota it
    counts against; it stays a human click in the Drive UI.
"""

from __future__ import annotations

import logging

from ..config import resolve_account, settings
from .drive import _drive, _file_id, _mock, _viewable_url, friendly_google_errors

logger = logging.getLogger("iblu_keeper.tools.drive_manage")

ACTIONS = (
    "copy", "rename", "move", "trash", "untrash", "star", "unstar",
    "set_description", "share", "unshare", "create_shortcut",
)

_SHORTCUT = "application/vnd.google-apps.shortcut"
_FIELDS = "id,name,mimeType,parents,starred,trashed,description,webViewLink"

# What Ignas says -> what the Drive API calls it. "owner" is absent on purpose.
_ROLES = {
    "viewer": "reader", "reader": "reader",
    "commenter": "commenter",
    "writer": "writer", "editor": "writer",
}

_MOCK_STATUS = {
    "copy": "not_copied_mock", "rename": "not_renamed_mock",
    "move": "not_moved_mock", "trash": "not_trashed_mock",
    "untrash": "not_untrashed_mock", "star": "not_starred_mock",
    "unstar": "not_unstarred_mock", "set_description": "not_described_mock",
    "share": "not_shared_mock", "unshare": "not_unshared_mock",
    "create_shortcut": "not_created_mock",
}


class DriveManageError(ValueError):
    """Bad input to `drive_manage`. The message always names what was expected."""


def _result(res: dict, status: str, **extra) -> dict:
    file_id = res.get("id", "")
    return {
        "id": file_id,
        "name": res.get("name", ""),
        "mime_type": res.get("mimeType", ""),
        "url": res.get("webViewLink") or _viewable_url(file_id, res.get("mimeType", "")),
        **extra,
        "status": status,
    }


def _update(drive, file_id: str, body: dict, account: str | None, what: str, **params) -> dict:
    with friendly_google_errors(account, what):
        return drive.files().update(
            fileId=file_id, body=body, fields=_FIELDS, supportsAllDrives=True, **params,
        ).execute()


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def copy(
    file_id_or_url: str, new_name: str | None = None,
    folder_id_or_url: str | None = None, account: str | None = None,
) -> dict:
    """Copy a file. Without ``new_name`` Drive names it "Copy of ..."; without
    a folder the copy lands beside the original."""
    file_id = _file_id(file_id_or_url)
    body: dict = {}
    if new_name:
        body["name"] = new_name
    if folder_id_or_url:
        body["parents"] = [_file_id(folder_id_or_url)]
    drive = _drive(account)
    with friendly_google_errors(account, "this Drive file"):
        res = drive.files().copy(
            fileId=file_id, body=body, fields=_FIELDS, supportsAllDrives=True,
        ).execute()
    logger.info("drive_manage copy: %s -> %s", file_id, res.get("id"))
    return _result(res, "copied", source_id=file_id, parents=res.get("parents", []) or [])


def rename(file_id_or_url: str, new_name: str, account: str | None = None) -> dict:
    if not new_name or not new_name.strip():
        raise DriveManageError("rename needs new_name")
    file_id = _file_id(file_id_or_url)
    res = _update(_drive(account), file_id, {"name": new_name.strip()}, account, "this Drive file")
    return _result(res, "renamed")


def move(file_id_or_url: str, folder_id_or_url: str, account: str | None = None) -> dict:
    """Move into a folder, replacing every existing parent."""
    if not folder_id_or_url:
        raise DriveManageError("move needs folder_id_or_url")
    file_id = _file_id(file_id_or_url)
    folder_id = _file_id(folder_id_or_url)
    drive = _drive(account)
    with friendly_google_errors(account, "this Drive file"):
        meta = drive.files().get(
            fileId=file_id, fields="parents", supportsAllDrives=True,
        ).execute()
    prev = ",".join(meta.get("parents", []) or [])
    res = _update(
        drive, file_id, {}, account, "this Drive file",
        addParents=folder_id, removeParents=prev,
    )
    return _result(res, "moved", parents=res.get("parents", []) or [], previous_parents=prev.split(",") if prev else [])


def trash(file_id_or_url: str, account: str | None = None) -> dict:
    """Move to Drive's Trash. Reversible with ``untrash`` or in the Drive UI
    for 30 days. There is no permanent delete in this module."""
    file_id = _file_id(file_id_or_url)
    res = _update(_drive(account), file_id, {"trashed": True}, account, "this Drive file")
    return _result(res, "trashed", trashed=True, undo="drive_manage(action='untrash', ...)")


def untrash(file_id_or_url: str, account: str | None = None) -> dict:
    file_id = _file_id(file_id_or_url)
    res = _update(_drive(account), file_id, {"trashed": False}, account, "this Drive file")
    return _result(res, "untrashed", trashed=False)


def star(file_id_or_url: str, account: str | None = None) -> dict:
    file_id = _file_id(file_id_or_url)
    res = _update(_drive(account), file_id, {"starred": True}, account, "this Drive file")
    return _result(res, "starred", starred=True)


def unstar(file_id_or_url: str, account: str | None = None) -> dict:
    file_id = _file_id(file_id_or_url)
    res = _update(_drive(account), file_id, {"starred": False}, account, "this Drive file")
    return _result(res, "unstarred", starred=False)


def set_description(file_id_or_url: str, description: str, account: str | None = None) -> dict:
    """Set the file's description (shown in Drive's details pane). An empty
    string clears it."""
    if description is None:
        raise DriveManageError("set_description needs description (use '' to clear)")
    file_id = _file_id(file_id_or_url)
    res = _update(_drive(account), file_id, {"description": description}, account, "this Drive file")
    return _result(res, "described", description=res.get("description", ""))


def share(
    file_id_or_url: str, email: str, role: str = "viewer", notify: bool = False,
    message: str | None = None, account: str | None = None,
) -> dict:
    """Give one person a role on the file.

    ``role``: viewer, commenter or writer. ``owner`` is refused — see the
    module docstring. ``notify=True`` sends Google's share email with
    ``message`` as the note; off by default.
    """
    if not email or "@" not in email:
        raise DriveManageError("share needs an email address")
    key = (role or "").strip().lower()
    if key == "owner":
        raise DriveManageError(
            "share refuses role='owner': transferring ownership is irreversible "
            "from this side and changes whose storage the file counts against. "
            "Do it by hand in Drive (Share > the person's role > Transfer ownership)."
        )
    if key not in _ROLES:
        raise DriveManageError(
            f"unknown role {role!r} — expected viewer, commenter or writer"
        )
    file_id = _file_id(file_id_or_url)
    params: dict = {
        "fileId": file_id,
        "body": {"type": "user", "role": _ROLES[key], "emailAddress": email.strip()},
        "sendNotificationEmail": bool(notify),
        "fields": "id,type,role,emailAddress,displayName",
        "supportsAllDrives": True,
    }
    if notify and message:
        params["emailMessage"] = message
    drive = _drive(account)
    with friendly_google_errors(account, "this Drive file's sharing"):
        res = drive.permissions().create(**params).execute()
    logger.info("drive_manage share: %s -> %s as %s", file_id, email, _ROLES[key])
    return {
        "id": file_id,
        "permission_id": res.get("id", ""),
        "email": res.get("emailAddress", email),
        "role": res.get("role", _ROLES[key]),
        "notified": bool(notify),
        "url": _viewable_url(file_id),
        "status": "shared",
    }


def unshare(file_id_or_url: str, email: str, account: str | None = None) -> dict:
    """Remove one person's access. Refuses to remove the owner."""
    if not email or "@" not in email:
        raise DriveManageError("unshare needs an email address")
    file_id = _file_id(file_id_or_url)
    drive = _drive(account)
    with friendly_google_errors(account, "this Drive file's sharing"):
        listing = drive.permissions().list(
            fileId=file_id, fields="permissions(id,role,emailAddress,type)",
            pageSize=100, supportsAllDrives=True,
        ).execute()
    wanted = email.strip().lower()
    match = next(
        (p for p in listing.get("permissions", []) or []
         if (p.get("emailAddress") or "").lower() == wanted),
        None,
    )
    if match is None:
        raise DriveManageError(f"{email} has no permission on this file — nothing to remove")
    if match.get("role") == "owner":
        raise DriveManageError(f"{email} owns this file; an owner cannot be unshared")
    with friendly_google_errors(account, "this Drive file's sharing"):
        drive.permissions().delete(
            fileId=file_id, permissionId=match["id"], supportsAllDrives=True,
        ).execute()
    return {
        "id": file_id,
        "email": email.strip(),
        "removed_role": match.get("role", ""),
        "permission_id": match["id"],
        "url": _viewable_url(file_id),
        "status": "unshared",
    }


def create_shortcut(
    file_id_or_url: str, folder_id_or_url: str, new_name: str | None = None,
    account: str | None = None,
) -> dict:
    """Put a Drive shortcut to the file inside ``folder_id_or_url``. The
    shortcut takes the target's name unless ``new_name`` is given."""
    if not folder_id_or_url:
        raise DriveManageError("create_shortcut needs folder_id_or_url")
    file_id = _file_id(file_id_or_url)
    folder_id = _file_id(folder_id_or_url)
    drive = _drive(account)
    name = (new_name or "").strip()
    if not name:
        with friendly_google_errors(account, "this Drive file"):
            meta = drive.files().get(
                fileId=file_id, fields="name", supportsAllDrives=True,
            ).execute()
        name = meta.get("name", "") or "Shortcut"
    body = {
        "name": name,
        "mimeType": _SHORTCUT,
        "shortcutDetails": {"targetId": file_id},
        "parents": [folder_id],
    }
    with friendly_google_errors(account, "this Drive folder"):
        res = drive.files().create(
            body=body, fields=_FIELDS + ",shortcutDetails", supportsAllDrives=True,
        ).execute()
    return _result(
        res, "created",
        target_id=file_id, parents=res.get("parents", []) or [folder_id],
    )


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
def drive_manage(
    action: str,
    file_id_or_url: str,
    new_name: str | None = None,
    folder_id_or_url: str | None = None,
    email: str | None = None,
    role: str = "viewer",
    notify: bool = False,
    message: str | None = None,
    description: str | None = None,
    account: str | None = None,
) -> dict:
    """Change a Drive file. One tool, eleven verbs — see the module docstring.

    Trash only, never a permanent delete; ``share`` never grants ownership.
    ``account`` — which Drive: ``blt`` (default), ``deadlift``, ``choco``.
    """
    if action not in ACTIONS:
        raise DriveManageError(
            f"unknown action {action!r} — expected one of: {', '.join(ACTIONS)}"
        )
    if not file_id_or_url or not str(file_id_or_url).strip():
        raise DriveManageError(f"{action} needs file_id_or_url")
    # Refusals that do not need Google are checked before mock mode, so a
    # refusal never depends on which mode the server is in.
    if action == "share" and (role or "").strip().lower() == "owner":
        raise DriveManageError(
            "share refuses role='owner': transferring ownership is irreversible "
            "from this side and changes whose storage the file counts against. "
            "Do it by hand in Drive (Share > the person's role > Transfer ownership)."
        )
    if action == "set_description" and description is None:
        raise DriveManageError("set_description needs description (use '' to clear)")

    account = resolve_account(account)
    if settings.use_mock:
        return _mock({
            "action": action, "id": file_id_or_url, "status": _MOCK_STATUS[action],
        })

    if action == "copy":
        return copy(file_id_or_url, new_name, folder_id_or_url, account)
    if action == "rename":
        return rename(file_id_or_url, new_name or "", account)
    if action == "move":
        return move(file_id_or_url, folder_id_or_url or "", account)
    if action == "trash":
        return trash(file_id_or_url, account)
    if action == "untrash":
        return untrash(file_id_or_url, account)
    if action == "star":
        return star(file_id_or_url, account)
    if action == "unstar":
        return unstar(file_id_or_url, account)
    if action == "set_description":
        return set_description(file_id_or_url, description, account)  # type: ignore[arg-type]
    if action == "share":
        return share(file_id_or_url, email or "", role, notify, message, account)
    if action == "unshare":
        return unshare(file_id_or_url, email or "", account)
    return create_shortcut(file_id_or_url, folder_id_or_url or "", new_name, account)
