"""gmail_manage — every request shape, every refusal, nothing sent in mock.

`settings` is swapped per the conftest rule; `_service` is a `Recorder` so the
exact kwargs Google would have received are asserted.
"""

from __future__ import annotations

import base64
import email
import email.policy
import inspect
from email.message import EmailMessage

import pytest
from _fake_google import Recorder

from iblu_keeper.tools import gmail_manage as GM

LABELS = {"labels": [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "STARRED", "name": "STARRED", "type": "system"},
    {"id": "Label_7", "name": "Clients", "type": "user"},
]}


class _Live:
    use_mock = False
    dry_run = False


@pytest.fixture
def live(monkeypatch):
    rec = Recorder({"users.labels.list": LABELS})
    monkeypatch.setattr(GM, "settings", _Live())
    monkeypatch.setattr(GM, "_service", lambda account=None: rec)
    monkeypatch.setattr(GM, "_from_address", lambda account=None: "me@x.com")
    return rec


# --- refusals --------------------------------------------------------------


def test_unknown_action_is_refused():
    """A misspelled verb must not fall through to any default, least of all a send."""
    with pytest.raises(GM.GmailManageError, match="unknown action"):
        GM.gmail_manage("delete", message_id="m1")


def test_exactly_one_of_message_or_thread():
    """Both given is ambiguous; neither given has nothing to modify."""
    with pytest.raises(GM.GmailManageError, match="exactly one"):
        GM.gmail_manage("archive", message_id="m1", thread_id="t1")
    with pytest.raises(GM.GmailManageError, match="exactly one"):
        GM.gmail_manage("archive")


def test_forward_without_recipient_is_refused_before_any_api_call(live):
    """A forward with no `to` must never reach Google, where it would 400 or worse."""
    with pytest.raises(GM.GmailManageError, match="requires to"):
        GM.gmail_manage("forward", message_id="m1")
    assert live.calls == []


def test_permanent_delete_is_absent_from_the_module():
    """messages.delete bypasses the bin and cannot be undone; the module must not contain it."""
    src = inspect.getsource(GM)
    assert "messages().delete" not in src
    assert "threads().delete" not in src
    assert "delete" not in GM.ACTIONS  # only discard_draft / delete_filter, named for what they are


# --- mock mode -------------------------------------------------------------


@pytest.mark.parametrize("action,kwargs,status", [
    ("forward", {"message_id": "m1", "to": "a@x.com"}, "not_sent_mock"),
    ("send_draft", {"draft_id": "d1"}, "not_sent_mock"),
    ("trash", {"message_id": "m1"}, "not_trashed_mock"),
])
def test_mock_mode_sends_and_changes_nothing(monkeypatch, action, kwargs, status):
    """DRY_RUN must not build a service, and the status must say the verb did not happen."""
    def boom(account=None):
        raise AssertionError("service built in mock mode")

    monkeypatch.setattr(GM, "_service", boom)
    out = GM.gmail_manage(action, **kwargs)
    assert out["_mock"] is True
    assert out["status"] == status


# --- labels ----------------------------------------------------------------


def test_label_resolves_names_to_ids_on_a_message(live):
    """Gmail wants label ids; sending the name 'Clients' as an id is a 400."""
    live.responses["users.messages.modify"] = {"id": "m1", "labelIds": ["INBOX", "Label_7"]}
    out = GM.gmail_manage("label", message_id="m1", labels=["Clients", "starred"])
    assert live.calls_to("users.messages.modify") == [
        {"userId": "me", "id": "m1", "body": {"addLabelIds": ["Label_7", "STARRED"]}},
    ]
    assert out["target"] == "message"
    assert out["status"] == "labeled"
    assert out["labels"] == ["Clients", "STARRED"]
    assert out["label_ids"] == ["INBOX", "Label_7"]


def test_label_on_a_thread_uses_threads_modify_and_says_so(live):
    """Labelling a thread touches every message in it; the result must name the target."""
    live.responses["users.threads.modify"] = {"id": "t1", "messages": [{"id": "a"}, {"id": "b"}]}
    out = GM.gmail_manage("unlabel", thread_id="t1", labels="Clients")
    assert live.calls_to("users.threads.modify") == [
        {"userId": "me", "id": "t1", "body": {"removeLabelIds": ["Label_7"]}},
    ]
    assert live.calls_to("users.messages.modify") == []
    assert out["target"] == "thread"
    assert out["message_count"] == 2


def test_unknown_label_is_refused_unless_create(live):
    """Silently creating on a typo is how a mailbox grows 'Clients', 'clients' and 'Cleints'."""
    with pytest.raises(GM.GmailManageError, match="no label named 'Prospects'"):
        GM.gmail_manage("label", message_id="m1", labels=["Prospects"])
    assert live.calls_to("users.labels.create") == []
    assert live.calls_to("users.messages.modify") == []


def test_label_with_create_makes_the_missing_label_then_applies_it(live):
    """The created id, not the name, must be what reaches modify."""
    live.responses["users.labels.create"] = {"id": "Label_new", "name": "Prospects"}
    live.responses["users.messages.modify"] = {"id": "m1", "labelIds": ["Label_new"]}
    out = GM.gmail_manage("label", message_id="m1", labels=["Prospects"], create=True)
    assert live.calls_to("users.labels.create")[0]["body"]["name"] == "Prospects"
    assert live.calls_to("users.messages.modify")[0]["body"] == {"addLabelIds": ["Label_new"]}
    assert out["created_labels"] == ["Prospects"]


@pytest.mark.parametrize("action,body,status", [
    ("archive", {"removeLabelIds": ["INBOX"]}, "archived"),
    ("unarchive", {"addLabelIds": ["INBOX"]}, "unarchived"),
    ("star", {"addLabelIds": ["STARRED"]}, "starred"),
    ("unstar", {"removeLabelIds": ["STARRED"]}, "unstarred"),
    ("mark_important", {"addLabelIds": ["IMPORTANT"]}, "marked_important"),
    ("unmark_important", {"removeLabelIds": ["IMPORTANT"]}, "unmarked_important"),
])
def test_label_flips_send_the_right_system_label(live, action, body, status):
    """Archive is 'remove INBOX' and nothing else; a wrong label here silently files mail wrong."""
    live.responses["users.messages.modify"] = {"id": "m1", "labelIds": []}
    out = GM.gmail_manage(action, message_id="m1")
    assert live.calls_to("users.messages.modify") == [{"userId": "me", "id": "m1", "body": body}]
    assert out["status"] == status


def test_trash_uses_the_trash_endpoint_not_delete(live):
    """trash is reversible; the call must be messages.trash and untrash must reverse it."""
    live.responses["users.messages.trash"] = {"id": "m1", "labelIds": ["TRASH"]}
    live.responses["users.threads.untrash"] = {"id": "t1", "messages": [{"id": "m1"}]}
    out = GM.gmail_manage("trash", message_id="m1")
    assert live.calls_to("users.messages.trash") == [{"userId": "me", "id": "m1"}]
    assert out["status"] == "trashed"
    back = GM.gmail_manage("untrash", thread_id="t1")
    assert live.calls_to("users.threads.untrash") == [{"userId": "me", "id": "t1"}]
    assert back["target"] == "thread"
    assert "delete" not in " ".join(live.paths())


def test_create_label_picks_a_readable_text_colour(live):
    """Gmail rejects a colour without textColor; a dark background needs white text."""
    live.responses["users.labels.create"] = {"id": "Label_9", "name": "Urgent"}
    out = GM.gmail_manage("create_label", name="Urgent", color="#000000")
    body = live.calls_to("users.labels.create")[0]["body"]
    assert body["color"] == {"backgroundColor": "#000000", "textColor": "#ffffff"}
    assert out == {"id": "Label_9", "name": "Urgent", "status": "created"}


# --- send-class ------------------------------------------------------------


def _raw_original() -> str:
    original = EmailMessage()
    original["From"] = "sender@acme.com"
    original["To"] = "me@x.com"
    original["Subject"] = "Invoice 42"
    original["Date"] = "Mon, 1 Jun 2026 10:00:00 +0200"
    original.set_content("Please find the invoice attached.")
    original.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="invoice.pdf")
    return base64.urlsafe_b64encode(original.as_bytes()).decode()


def test_forward_quotes_the_original_and_carries_its_attachment(live):
    """A 'forward' that drops the original text or its PDF is not a forward."""
    live.responses["users.messages.get"] = {"id": "m1", "raw": _raw_original()}
    live.responses["users.messages.send"] = {"id": "sent1"}

    out = GM.gmail_manage("forward", message_id="m1", to="acct@x.com", note="FYI — pay this one.")

    get_kw = live.calls_to("users.messages.get")[0]
    assert get_kw["format"] == "raw"
    send_kw = live.calls_to("users.messages.send")[0]
    assert "threadId" not in send_kw["body"]  # a forward starts its own thread
    sent = email.message_from_bytes(base64.urlsafe_b64decode(send_kw["body"]["raw"]), policy=email.policy.default)
    assert sent["To"] == "acct@x.com"
    assert sent["Subject"] == "Fwd: Invoice 42"
    text = sent.get_body(preferencelist=("plain",)).get_content()
    assert text.startswith("FYI — pay this one.")
    assert "---------- Forwarded message ---------" in text
    assert "From: sender@acme.com" in text
    assert "Please find the invoice attached." in text
    assert [a.get_filename() for a in sent.iter_attachments()] == ["invoice.pdf"]
    assert out == {
        "id": "sent1", "forwarded_message_id": "m1", "to": "acct@x.com",
        "subject": "Fwd: Invoice 42", "attachments": ["invoice.pdf"], "status": "sent",
    }


def test_forward_with_no_message_id_from_google_is_a_failure_not_a_success(live):
    """A success-shaped result with no Gmail id would be a false 'sent'."""
    live.responses["users.messages.get"] = {"id": "m1", "raw": _raw_original()}
    live.responses["users.messages.send"] = {}
    with pytest.raises(RuntimeError, match="no message id"):
        GM.gmail_manage("forward", message_id="m1", to="acct@x.com")


def test_send_draft_sends_by_id_and_reports_the_thread(live):
    """drafts.send takes the draft id in the body; anything else sends nothing and 400s."""
    live.responses["users.drafts.send"] = {"id": "sent2", "threadId": "t5"}
    out = GM.gmail_manage("send_draft", draft_id="d1")
    assert live.calls_to("users.drafts.send") == [{"userId": "me", "body": {"id": "d1"}}]
    assert out == {"id": "sent2", "draft_id": "d1", "thread_id": "t5", "status": "sent"}


# --- drafts ----------------------------------------------------------------


def test_update_draft_keeps_unspecified_fields_and_the_thread(live):
    """Changing only the body must not blank the recipient or detach the draft from its thread."""
    live.responses["users.drafts.get"] = {"id": "d1", "message": {
        "id": "m9", "threadId": "t3",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "To", "value": "keep@x.com"}, {"name": "Subject", "value": "Keep me"}],
            "body": {"data": base64.urlsafe_b64encode(b"old body").decode()},
        },
    }}
    live.responses["users.drafts.update"] = {"id": "d1"}
    out = GM.gmail_manage("update_draft", draft_id="d1", body="new body")
    upd = live.calls_to("users.drafts.update")[0]
    assert upd["id"] == "d1"
    assert upd["body"]["message"]["threadId"] == "t3"
    sent = email.message_from_bytes(base64.urlsafe_b64decode(upd["body"]["message"]["raw"]), policy=email.policy.default)
    assert sent["To"] == "keep@x.com"
    assert sent["Subject"] == "Keep me"
    assert sent.get_content().strip() == "new body"
    assert out["status"] == "updated"
    assert live.calls_to("users.drafts.send") == []


def test_update_draft_with_nothing_to_change_is_refused(live):
    """Rewriting a draft with its own content would still churn the draft id for no reason."""
    with pytest.raises(GM.GmailManageError, match="at least one"):
        GM.gmail_manage("update_draft", draft_id="d1")


def test_discard_draft_deletes_the_draft_only(live):
    """A draft delete is not mail deletion; the result must say so and nothing must be sent."""
    out = GM.gmail_manage("discard_draft", draft_id="d1")
    assert live.calls_to("users.drafts.delete") == [{"userId": "me", "id": "d1"}]
    assert out["status"] == "discarded"
    assert "draft" in out["note"].lower()
    assert live.calls_to("users.drafts.send") == []


# --- filters ---------------------------------------------------------------


def test_create_filter_builds_criteria_and_actions_from_names(live):
    """The filter must carry label ids and the INBOX/UNREAD removals, and return its id."""
    live.responses["users.settings.filters.create"] = {"id": "flt1"}
    out = GM.gmail_manage(
        "create_filter", filter_from="billing@acme.com", filter_subject="Invoice",
        labels=["Clients"], archive=True, mark_read=True, star=True,
    )
    body = live.calls_to("users.settings.filters.create")[0]["body"]
    assert body["criteria"] == {"from": "billing@acme.com", "subject": "Invoice"}
    assert body["action"] == {"addLabelIds": ["Label_7", "STARRED"], "removeLabelIds": ["INBOX", "UNREAD"]}
    assert out["filter_id"] == "flt1"
    assert out["status"] == "created"


def test_create_filter_refuses_without_criteria_or_without_actions(live):
    """A filter with no criteria matches everything; one with no action does nothing. Both are mistakes."""
    with pytest.raises(GM.GmailManageError, match="criterion"):
        GM.gmail_manage("create_filter", archive=True)
    with pytest.raises(GM.GmailManageError, match="action"):
        GM.gmail_manage("create_filter", filter_from="a@x.com")
    assert live.calls_to("users.settings.filters.create") == []


def test_delete_filter_targets_the_id(live):
    """Deleting the wrong filter is silent; the id must pass through untouched."""
    out = GM.gmail_manage("delete_filter", filter_id="flt1")
    assert live.calls_to("users.settings.filters.delete") == [{"userId": "me", "id": "flt1"}]
    assert out == {"filter_id": "flt1", "status": "deleted"}
