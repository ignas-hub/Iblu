"""gmail_read — request shapes and result shapes, no Google.

`settings` is a frozen dataclass, so each live test swaps the module's whole
`settings` object (conftest rule) and replaces `_service` with a `Recorder`.
"""

from __future__ import annotations

import base64

import pytest
from _fake_google import Recorder

from iblu_keeper.tools import gmail_read as GR


class _Live:
    use_mock = False
    dry_run = False


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _msg(mid: str, body: str = "hello", **headers) -> dict:
    hdrs = {"From": "a@x.com", "To": "b@x.com", "Subject": "Subj", "Date": "Mon", **headers}
    return {
        "id": mid, "threadId": "t1", "snippet": body[:20], "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": k, "value": v} for k, v in hdrs.items()],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(body)}},
                {"mimeType": "application/pdf", "filename": "deck.pdf",
                 "body": {"attachmentId": "att1", "size": 99}},
            ],
        },
    }


@pytest.fixture
def live(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(GR, "settings", _Live())
    monkeypatch.setattr(GR, "_service", lambda account=None: rec)
    return rec


# --- refusals --------------------------------------------------------------


def test_unknown_action_is_refused():
    """A typo would otherwise fall through to some default action and read the wrong thing."""
    with pytest.raises(GR.GmailReadError, match="unknown action"):
        GR.gmail_read("threads")


def test_thread_requires_thread_id():
    """threads.get(id=None) would be a confusing 400 from Google instead of a named refusal."""
    with pytest.raises(GR.GmailReadError, match="thread_id"):
        GR.gmail_read("thread")


def test_draft_requires_draft_id():
    """Same as above for drafts.get."""
    with pytest.raises(GR.GmailReadError, match="draft_id"):
        GR.gmail_read("draft")


def test_limit_below_one_is_refused():
    """maxResults=0 silently returns nothing and the caller would read that as 'no drafts'."""
    with pytest.raises(GR.GmailReadError, match="limit"):
        GR.gmail_read("drafts", limit=0)


# --- mock mode -------------------------------------------------------------


def test_mock_mode_never_builds_a_service(monkeypatch):
    """DRY_RUN must not touch Gmail, and the result must say it did not."""
    def boom(account=None):
        raise AssertionError("service built in mock mode")

    monkeypatch.setattr(GR, "_service", boom)
    out = GR.gmail_read("thread", thread_id="t1")
    assert out["_mock"] is True
    assert out["status"] == "not_read_mock"


# --- thread ----------------------------------------------------------------


def test_thread_fetches_full_and_shapes_every_message(live):
    """Without format=full there is no body; without attachments the caller cannot ask for one."""
    live.responses["users.threads.get"] = {
        "id": "t1", "messages": [_msg("m1", "first"), _msg("m2", "second")],
    }
    out = GR.gmail_read("thread", thread_id="t1")
    assert live.calls_to("users.threads.get") == [{"userId": "me", "id": "t1", "format": "full"}]
    assert out["count"] == 2
    assert out["subject"] == "Subj"
    first = out["messages"][0]
    assert first["from"] == "a@x.com"
    assert first["body"] == "first"
    assert first["body_truncated"] is False
    assert first["attachments"] == [
        {"attachment_id": "att1", "filename": "deck.pdf", "mime_type": "application/pdf", "size_bytes": 99},
    ]


def test_thread_caps_each_body_at_4000_and_says_so(live):
    """A long thread would return the same quoted text a dozen times and blow the context."""
    live.responses["users.threads.get"] = {"id": "t1", "messages": [_msg("m1", "x" * 5000)]}
    out = GR.gmail_read("thread", thread_id="t1")
    m = out["messages"][0]
    assert len(m["body"]) == GR.BODY_CAP == 4000
    assert m["body_truncated"] is True


def test_thread_headers_are_matched_case_insensitively(live):
    """Senders write 'FROM' or 'from'; a case-sensitive lookup leaves the field blank."""
    shouted = _msg("m1", "hi")
    shouted["payload"]["headers"] = [
        {"name": "FROM", "value": "shout@x.com"}, {"name": "subject", "value": "lower"},
    ]
    live.responses["users.threads.get"] = {"id": "t1", "messages": [shouted]}
    out = GR.gmail_read("thread", thread_id="t1")
    assert out["messages"][0]["from"] == "shout@x.com"
    assert out["messages"][0]["subject"] == "lower"


# --- labels ----------------------------------------------------------------


def test_labels_fetches_counts_per_label_and_lists_system_first(live):
    """labels.list has no counts; without labels.get the numbers would be invented or absent."""
    live.responses["users.labels.list"] = {"labels": [
        {"id": "Label_1", "name": "Clients", "type": "user"},
        {"id": "INBOX", "name": "INBOX", "type": "system"},
    ]}
    live.responses["users.labels.get"] = lambda kw: {
        "id": kw["id"], "name": "Clients" if kw["id"] == "Label_1" else "INBOX",
        "type": "user" if kw["id"] == "Label_1" else "system",
        "messagesTotal": 10, "messagesUnread": 2, "threadsTotal": 5, "threadsUnread": 1,
    }
    out = GR.gmail_read("labels")
    assert sorted(kw["id"] for kw in live.calls_to("users.labels.get")) == ["INBOX", "Label_1"]
    assert [lab["name"] for lab in out["labels"]] == ["INBOX", "Clients"]
    assert out["labels"][1] == {
        "id": "Label_1", "name": "Clients", "type": "user",
        "messages_total": 10, "messages_unread": 2, "threads_total": 5, "threads_unread": 1,
    }


# --- drafts ----------------------------------------------------------------


def test_drafts_lists_then_reads_metadata_for_each(live):
    """drafts.list returns only ids — to/subject need a per-draft get."""
    live.responses["users.drafts.list"] = {"drafts": [{"id": "d1"}, {"id": "d2"}]}
    live.responses["users.drafts.get"] = lambda kw: {
        "id": kw["id"], "message": {
            "id": f"m-{kw['id']}", "threadId": "t9", "snippet": "snip",
            "payload": {"headers": [{"name": "To", "value": "c@x.com"}, {"name": "Subject", "value": "S"}]},
        },
    }
    out = GR.gmail_read("drafts", limit=2)
    assert live.calls_to("users.drafts.list") == [{"userId": "me", "maxResults": 2}]
    assert all(kw["format"] == "metadata" for kw in live.calls_to("users.drafts.get"))
    assert out["count"] == 2
    assert out["drafts"][0] == {
        "id": "d1", "message_id": "m-d1", "thread_id": "t9",
        "to": "c@x.com", "subject": "S", "date": "", "snippet": "snip",
    }


def test_draft_returns_the_full_message_view(live):
    """A draft the caller is about to send must show its body, not just a snippet."""
    live.responses["users.drafts.get"] = {"id": "d1", "message": _msg("m1", "draft body")}
    out = GR.gmail_read("draft", draft_id="d1")
    assert live.calls_to("users.drafts.get") == [{"userId": "me", "id": "d1", "format": "full"}]
    assert out["draft_id"] == "d1"
    assert out["body"] == "draft body"
    assert out["attachments"][0]["filename"] == "deck.pdf"


# --- filters / profile / sent -----------------------------------------------


def test_filters_are_described_in_words_with_label_names(live):
    """Raw filter JSON is ids; 'add label Clients, skip inbox' is what a person can check."""
    live.responses["users.settings.filters.list"] = {"filter": [{
        "id": "f1",
        "criteria": {"from": "billing@acme.com", "subject": "Invoice"},
        "action": {"addLabelIds": ["Label_1", "STARRED"], "removeLabelIds": ["INBOX", "UNREAD"]},
    }]}
    live.responses["users.labels.list"] = {"labels": [{"id": "Label_1", "name": "Clients", "type": "user"}]}
    out = GR.gmail_read("filters")
    f = out["filters"][0]
    assert f["id"] == "f1"
    assert f["actions"]["add_labels"] == ["Clients", "STARRED"]
    assert f["summary"] == 'from:billing@acme.com subject:"Invoice" → add label Clients, star, skip inbox, mark read'


def test_profile_maps_the_four_fields(live):
    """historyId is what an incremental sync keys on; dropping it makes the action pointless."""
    live.responses["users.getProfile"] = {
        "emailAddress": "me@x.com", "messagesTotal": 3, "threadsTotal": 2, "historyId": "77",
    }
    out = GR.gmail_read("profile")
    assert out == {"status": "ok", "address": "me@x.com", "messages_total": 3, "threads_total": 2, "history_id": "77"}


def test_sent_recent_lists_the_sent_label_with_limit(live):
    """Without labelIds=SENT this would be the inbox; without maxResults it would be everything."""
    live.responses["users.messages.list"] = {"messages": [{"id": "s1"}]}
    live.responses["users.messages.get"] = {
        "id": "s1", "threadId": "t1", "snippet": "sent",
        "payload": {"headers": [{"name": "To", "value": "z@x.com"}, {"name": "Subject", "value": "Hi"}, {"name": "Date", "value": "Tue"}]},
    }
    out = GR.gmail_read("sent_recent", limit=5)
    assert live.calls_to("users.messages.list") == [{"userId": "me", "labelIds": ["SENT"], "maxResults": 5}]
    assert out["items"] == [{"id": "s1", "thread_id": "t1", "to": "z@x.com", "subject": "Hi", "date": "Tue", "snippet": "sent"}]
