"""Changing the state of mail that already exists: labels, archive, star,
trash, drafts, filters — and the two explicit send-class verbs, forward and
send_draft.

`tools/gmail.py` can draft, send, reply, and mark read/unread. Everything a
person does to mail *between* those — file it under a label, get it out of
the inbox, star it, bin it, fix a draft, set up a filter so the next one
files itself — had no tool, and each one was a request in conversation.
Ignas, 2026-10-26: "It's crazy how many times I have to ask to add tools."
This module is the write half of the batch that ends that; `gmail_read.py`
is the read half.

It is ONE MCP tool with an `action` parameter (HANDOFF §12: one tool per
domain × side-effect class — every `@mcp.tool` is a permanent Allow click).
The read tool cannot write and this tool cannot read beyond what a write
needs, so the permission groups stay honest.

Send-class actions. Two actions here put a message in front of another
person: ``forward`` and ``send_draft``. Both are their own explicit action
names — nothing else in this module sends, and no action sends as a side
effect of another (CLAUDE.md: never send on his behalf unless he says "go";
the action name *is* the "go"). ``update_draft`` and ``discard_draft`` touch
a draft, which is not mail.

What is deliberately absent: **permanent deletion**. There is no
``messages.delete`` / ``threads.delete`` here and there will not be — Gmail's
``delete`` bypasses the bin and cannot be undone. ``trash`` is the strongest
verb this tool knows, and ``untrash`` reverses it.

Message vs thread. The label/archive/star/trash family accepts exactly one of
``message_id`` or ``thread_id`` and the result's ``target`` field says which
was touched, because labelling a thread touches every message in it and a
caller should see that in the answer, not infer it.

In dry-run mode every action returns a clearly marked mock with status
``not_<verb>_mock`` and never builds a Google service.
"""

from __future__ import annotations

import base64
import email
import email.policy
import logging
import re
from email.message import EmailMessage

from ..config import resolve_account, settings
from .gmail import _build_raw, _extract_body, _from_address

logger = logging.getLogger("iblu_keeper.tools.gmail_manage")

ACTIONS = (
    "label", "unlabel", "archive", "unarchive", "trash", "untrash",
    "star", "unstar", "mark_important", "unmark_important",
    "create_label", "forward", "send_draft", "update_draft", "discard_draft",
    "create_filter", "delete_filter",
)

# Which actions are "send-class": each puts mail in another person's inbox.
SEND_ACTIONS = ("forward", "send_draft")

# Actions that take exactly one of message_id / thread_id.
_TARGET_ACTIONS = (
    "label", "unlabel", "archive", "unarchive", "trash", "untrash",
    "star", "unstar", "mark_important", "unmark_important",
)

# What the mock reports for each verb — read as "this did NOT happen".
_MOCK_STATUS = {
    "label": "not_labeled_mock",
    "unlabel": "not_unlabeled_mock",
    "archive": "not_archived_mock",
    "unarchive": "not_unarchived_mock",
    "trash": "not_trashed_mock",
    "untrash": "not_untrashed_mock",
    "star": "not_starred_mock",
    "unstar": "not_unstarred_mock",
    "mark_important": "not_marked_mock",
    "unmark_important": "not_unmarked_mock",
    "create_label": "not_created_mock",
    "forward": "not_sent_mock",
    "send_draft": "not_sent_mock",
    "update_draft": "not_updated_mock",
    "discard_draft": "not_discarded_mock",
    "create_filter": "not_created_mock",
    "delete_filter": "not_deleted_mock",
}

# Simple label flips: action → (add, remove).
_FLIPS = {
    "archive": ([], ["INBOX"]),
    "unarchive": (["INBOX"], []),
    "star": (["STARRED"], []),
    "unstar": ([], ["STARRED"]),
    "mark_important": (["IMPORTANT"], []),
    "unmark_important": ([], ["IMPORTANT"]),
}
_FLIP_STATUS = {
    "archive": "archived", "unarchive": "unarchived",
    "star": "starred", "unstar": "unstarred",
    "mark_important": "marked_important", "unmark_important": "unmarked_important",
}


class GmailManageError(ValueError):
    """Bad input to `gmail_manage`. The message always names what was expected."""


def _service(account: str | None = None):
    from ..google_auth import build_service

    return build_service("gmail", "v1", account=account)


# --- targets and labels -------------------------------------------------------


def _target(message_id: str | None, thread_id: str | None) -> tuple[str, str]:
    """('message', id) or ('thread', id) — exactly one, never both, never neither."""
    if bool(message_id) == bool(thread_id):
        raise GmailManageError("expected exactly one of message_id or thread_id")
    return ("message", message_id) if message_id else ("thread", thread_id)


def _names(labels: list[str] | str | None) -> list[str]:
    if labels is None:
        return []
    if isinstance(labels, str):
        labels = labels.split(",")
    return [n.strip() for n in labels if n and n.strip()]


def _label_index(service) -> dict[str, dict]:
    """lower-cased label name → {id, name, type}. System labels are listed by
    their canonical names (INBOX, STARRED, …), so those resolve too."""
    listed = service.users().labels().list(userId="me").execute().get("labels", []) or []
    return {
        (lab.get("name") or "").lower(): {
            "id": lab.get("id"), "name": lab.get("name"), "type": lab.get("type"),
        }
        for lab in listed
    }


def _create_label(service, name: str, color: str | None = None) -> dict:
    body: dict = {
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    if color:
        body["color"] = {"backgroundColor": color, "textColor": _text_color_for(color)}
    created = service.users().labels().create(userId="me", body=body).execute()
    if not created.get("id"):
        raise RuntimeError(
            f"Gmail labels.create returned no id (response={created!r}); "
            "treating as failure rather than reporting a false success."
        )
    return created


def _text_color_for(background: str) -> str:
    """Black on a light background, white on a dark one. Gmail only accepts
    colours from its own palette, and both of these are in it."""
    hex_part = background.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", hex_part):
        raise GmailManageError(
            f"color must be a hex colour like '#fb4c2f' from Gmail's label palette, got {background!r}"
        )
    r, g, b = (int(hex_part[i:i + 2], 16) for i in (0, 2, 4))
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#000000" if luminance > 150 else "#ffffff"


def _resolve_labels(
    service, names: list[str], create: bool,
) -> tuple[list[str], list[str], list[str]]:
    """Names → ids. Returns (ids, resolved_names, created_names).

    A name that does not exist is an error unless `create=True`, in which case
    a user label is made for it — creating labels silently on a typo is how a
    mailbox ends up with "Clients", "clients" and "Cleints".
    """
    index = _label_index(service)
    ids: list[str] = []
    resolved: list[str] = []
    created: list[str] = []
    for name in names:
        hit = index.get(name.lower())
        if hit is None:
            if not create:
                raise GmailManageError(
                    f"no label named {name!r} — pass create=True to make it, "
                    "or list labels with gmail_read(action='labels')"
                )
            made = _create_label(service, name)
            hit = {"id": made["id"], "name": made.get("name", name)}
            index[name.lower()] = hit
            created.append(hit["name"])
        ids.append(hit["id"])
        resolved.append(hit["name"])
    return ids, resolved, created


def _modify(service, kind: str, target_id: str, add: list[str], remove: list[str]) -> dict:
    body: dict = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove
    coll = service.users().messages() if kind == "message" else service.users().threads()
    return coll.modify(userId="me", id=target_id, body=body).execute()


def _result(kind: str, target_id: str, status: str, res: dict, **extra) -> dict:
    out = {"id": res.get("id", target_id), "target": kind, "status": status, **extra}
    if kind == "message":
        out["label_ids"] = res.get("labelIds", []) or []
    else:
        out["message_count"] = len(res.get("messages", []) or [])
    return out


# --- label family --------------------------------------------------------------


def _label_action(service, action: str, kind: str, target_id: str, names: list[str], create: bool) -> dict:
    if not names:
        raise GmailManageError(f"{action} requires labels (a name or list of names)")
    ids, resolved, created = _resolve_labels(service, names, create=create and action == "label")
    if action == "label":
        res = _modify(service, kind, target_id, add=ids, remove=[])
        return _result(kind, target_id, "labeled", res, labels=resolved, created_labels=created)
    res = _modify(service, kind, target_id, add=[], remove=ids)
    return _result(kind, target_id, "unlabeled", res, labels=resolved)


def _trash_action(service, action: str, kind: str, target_id: str) -> dict:
    coll = service.users().messages() if kind == "message" else service.users().threads()
    if action == "trash":
        res = coll.trash(userId="me", id=target_id).execute()
        return _result(kind, target_id, "trashed", res, note="In the bin for 30 days; untrash reverses this.")
    res = coll.untrash(userId="me", id=target_id).execute()
    return _result(kind, target_id, "untrashed", res)


# --- send-class ----------------------------------------------------------------


def _plain_text_of(parsed) -> str:
    """Plain body of a parsed MIME message, falling back to de-tagged HTML."""
    part = parsed.get_body(preferencelist=("plain",))
    if part is not None:
        return part.get_content()
    part = parsed.get_body(preferencelist=("html",))
    if part is not None:
        html = part.get_content()
        text = re.sub(r"<(br|/p|/div)\s*/?>", "\n", html, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    return ""


def _forward(service, message_id: str, to: str, note: str | None, account: str | None) -> dict:
    raw_msg = service.users().messages().get(userId="me", id=message_id, format="raw").execute()
    raw = raw_msg.get("raw")
    if not raw:
        raise RuntimeError(f"Gmail returned no raw content for message {message_id!r}")
    original = email.message_from_bytes(
        base64.urlsafe_b64decode(raw), policy=email.policy.default,
    )

    subject = original.get("Subject", "") or ""
    if not re.match(r"^\s*fwd?:", subject, flags=re.I):
        subject = f"Fwd: {subject}".strip()

    quoted = "\n".join([
        "---------- Forwarded message ---------",
        f"From: {original.get('From', '')}",
        f"Date: {original.get('Date', '')}",
        f"Subject: {original.get('Subject', '')}",
        f"To: {original.get('To', '')}",
        *([f"Cc: {original.get('Cc')}"] if original.get("Cc") else []),
        "",
        _plain_text_of(original),
    ])
    text = f"{note.strip()}\n\n{quoted}" if note and note.strip() else quoted

    fwd = EmailMessage()
    fwd["To"] = to
    fwd["From"] = _from_address(account)
    fwd["Subject"] = subject
    fwd.set_content(text)

    attached: list[str] = []
    for part in original.iter_attachments():
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        maintype, _, subtype = (part.get_content_type() or "application/octet-stream").partition("/")
        filename = part.get_filename() or "attachment"
        fwd.add_attachment(payload, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
        attached.append(filename)

    encoded = base64.urlsafe_b64encode(fwd.as_bytes()).decode()
    sent = service.users().messages().send(userId="me", body={"raw": encoded}).execute()
    sent_id = sent.get("id")
    if not sent_id:
        raise RuntimeError(
            f"Gmail forward returned no message id (response={sent!r}); "
            "treating as failure rather than reporting a false success."
        )
    logger.info("gmail_manage forward of %s to %s (gmail id=%s)", message_id, to, sent_id)
    return {
        "id": sent_id,
        "forwarded_message_id": message_id,
        "to": to,
        "subject": subject,
        "attachments": attached,
        "status": "sent",
    }


def _send_draft(service, draft_id: str) -> dict:
    sent = service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    sent_id = sent.get("id")
    if not sent_id:
        raise RuntimeError(
            f"Gmail drafts.send returned no message id (response={sent!r}); "
            "treating as failure rather than reporting a false success."
        )
    logger.info("gmail_manage send_draft %s (gmail id=%s)", draft_id, sent_id)
    return {"id": sent_id, "draft_id": draft_id, "thread_id": sent.get("threadId"), "status": "sent"}


# --- drafts ----------------------------------------------------------------------


def _update_draft(
    service, draft_id: str, to: str | None, subject: str | None, body: str | None, account: str | None,
) -> dict:
    if to is None and subject is None and body is None:
        raise GmailManageError("update_draft requires at least one of to, subject, body")
    existing = service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
    msg = existing.get("message", {}) or {}
    payload = msg.get("payload", {}) or {}
    headers = {(h.get("name") or "").lower(): h.get("value") or "" for h in payload.get("headers") or []}
    merged_to = to if to is not None else headers.get("to", "")
    merged_subject = subject if subject is not None else headers.get("subject", "")
    merged_body = body if body is not None else _extract_body(payload)

    message: dict = {"raw": _build_raw(merged_to, merged_subject, merged_body, account=account)}
    if msg.get("threadId"):
        message["threadId"] = msg["threadId"]
    updated = service.users().drafts().update(userId="me", id=draft_id, body={"message": message}).execute()
    return {
        "draft_id": updated.get("id", draft_id),
        "to": merged_to,
        "subject": merged_subject,
        "thread_id": message.get("threadId"),
        "status": "updated",
    }


def _discard_draft(service, draft_id: str) -> dict:
    service.users().drafts().delete(userId="me", id=draft_id).execute()
    return {"draft_id": draft_id, "status": "discarded", "note": "A draft, not mail — nothing was sent or deleted from any thread."}


# --- filters ---------------------------------------------------------------------


def _create_filter(
    service,
    filter_from: str | None, filter_to: str | None, filter_subject: str | None, query: str | None,
    labels: list[str], create: bool, archive: bool, star: bool, mark_read: bool,
) -> dict:
    criteria: dict = {}
    if filter_from:
        criteria["from"] = filter_from
    if filter_to:
        criteria["to"] = filter_to
    if filter_subject:
        criteria["subject"] = filter_subject
    if query:
        criteria["query"] = query
    if not criteria:
        raise GmailManageError(
            "create_filter requires at least one criterion: filter_from, filter_to, filter_subject, query"
        )

    add: list[str] = []
    remove: list[str] = []
    label_names: list[str] = []
    created: list[str] = []
    if labels:
        ids, label_names, created = _resolve_labels(service, labels, create=create)
        add.extend(ids)
    if star:
        add.append("STARRED")
    if archive:
        remove.append("INBOX")
    if mark_read:
        remove.append("UNREAD")
    if not add and not remove:
        raise GmailManageError(
            "create_filter requires at least one action: labels, archive, star, mark_read"
        )

    action: dict = {}
    if add:
        action["addLabelIds"] = add
    if remove:
        action["removeLabelIds"] = remove
    made = (
        service.users().settings().filters()
        .create(userId="me", body={"criteria": criteria, "action": action})
        .execute()
    )
    filter_id = made.get("id")
    if not filter_id:
        raise RuntimeError(
            f"Gmail filters.create returned no id (response={made!r}); "
            "treating as failure rather than reporting a false success."
        )
    return {
        "filter_id": filter_id,
        "criteria": criteria,
        "actions": {
            "add_labels": label_names + (["STARRED"] if star else []),
            "remove_labels": remove,
        },
        "created_labels": created,
        "status": "created",
        "note": "Applies to mail arriving from now on; existing mail is untouched.",
    }


def _delete_filter(service, filter_id: str) -> dict:
    service.users().settings().filters().delete(userId="me", id=filter_id).execute()
    return {"filter_id": filter_id, "status": "deleted"}


# --- dispatch ----------------------------------------------------------------------


def gmail_manage(
    action: str,
    message_id: str | None = None,
    thread_id: str | None = None,
    labels: list[str] | str | None = None,
    create: bool = False,
    name: str | None = None,
    color: str | None = None,
    to: str | None = None,
    note: str | None = None,
    draft_id: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    filter_from: str | None = None,
    filter_to: str | None = None,
    filter_subject: str | None = None,
    query: str | None = None,
    archive: bool = False,
    star: bool = False,
    mark_read: bool = False,
    filter_id: str | None = None,
    account: str | None = None,
) -> dict:
    """Change the state of existing mail. One tool, one `action`.

    Message-or-thread actions (exactly one of message_id / thread_id; the
    result's ``target`` says which was touched):
      label             add `labels` (names; a missing user label is created
                        only when create=True).
      unlabel           remove `labels`.
      archive           remove INBOX.         unarchive   put INBOX back.
      trash             move to the bin.       untrash     take it out.
      star / unstar     STARRED on / off.
      mark_important / unmark_important   IMPORTANT on / off.

    Labels and filters:
      create_label      `name`, optional `color` (hex from Gmail's palette).
      create_filter     criteria from filter_from / filter_to / filter_subject /
                        query; actions from labels / archive / star / mark_read.
                        Returns the filter id. Future mail only.
      delete_filter     `filter_id`.

    Drafts (not mail):
      update_draft      `draft_id` + any of to / subject / body; unspecified
                        fields are kept from the existing draft.
      discard_draft     `draft_id` — deletes the draft. Nothing is sent.

    SEND-CLASS — each puts mail in someone's inbox and is its own action,
    never a side effect of another:
      forward           `message_id` + `to`, optional `note` above the quoted
                        original. Original attachments ride along.
      send_draft        `draft_id`.

    Permanent deletion (messages.delete) is deliberately not implemented and
    never will be here — `trash` is the strongest verb. ``account`` — which
    mailbox: ``blt`` (default), ``deadlift``, ``choco``.
    """
    if action not in ACTIONS:
        raise GmailManageError(
            f"unknown action {action!r} — expected one of {', '.join(ACTIONS)}"
        )

    # Validate before the mock return, so a refusal reads the same in both modes.
    kind = target_id = None
    if action in _TARGET_ACTIONS:
        kind, target_id = _target(message_id, thread_id)
    label_names = _names(labels)
    if action in ("label", "unlabel") and not label_names:
        raise GmailManageError(f"{action} requires labels (a name or list of names)")
    if action == "create_label" and not (name and name.strip()):
        raise GmailManageError("create_label requires name")
    if action == "forward":
        if not message_id:
            raise GmailManageError("forward requires message_id")
        if not (to and to.strip()):
            raise GmailManageError("forward requires to — refusing to send without a recipient")
    if action in ("send_draft", "update_draft", "discard_draft") and not draft_id:
        raise GmailManageError(f"{action} requires draft_id")
    if action == "delete_filter" and not filter_id:
        raise GmailManageError("delete_filter requires filter_id")

    account = resolve_account(account)
    if settings.use_mock:
        if action in SEND_ACTIONS:
            logger.warning("MOCK gmail_manage %s — NOT sent (DRY_RUN).", action)
        return {
            "_mock": True,
            "action": action,
            "status": _MOCK_STATUS[action],
            **({"target": kind, "id": target_id} if kind else {}),
            "note": "MOCK MODE — nothing was changed or sent. Set DRY_RUN=false.",
        }

    service = _service(account)

    if action in ("label", "unlabel"):
        return _label_action(service, action, kind, target_id, label_names, create)
    if action in _FLIPS:
        add, remove = _FLIPS[action]
        res = _modify(service, kind, target_id, add=add, remove=remove)
        return _result(kind, target_id, _FLIP_STATUS[action], res)
    if action in ("trash", "untrash"):
        return _trash_action(service, action, kind, target_id)
    if action == "create_label":
        made = _create_label(service, name.strip(), color)
        return {"id": made["id"], "name": made.get("name", name), "status": "created"}
    if action == "forward":
        return _forward(service, message_id, to.strip(), note, account)
    if action == "send_draft":
        return _send_draft(service, draft_id)
    if action == "update_draft":
        return _update_draft(service, draft_id, to, subject, body, account)
    if action == "discard_draft":
        return _discard_draft(service, draft_id)
    if action == "create_filter":
        return _create_filter(
            service, filter_from, filter_to, filter_subject, query,
            label_names, create, archive, star, mark_read,
        )
    return _delete_filter(service, filter_id)
