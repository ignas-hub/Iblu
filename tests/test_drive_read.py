"""`tools.drive_read` — the read half of the Drive domain.

Fakes only. `settings` is a frozen dataclass and the suite pins DRY_RUN=true,
so the module's `settings` object is swapped whole and `_drive` is replaced
with a fake service that records every call's kwargs. No network.
"""

from __future__ import annotations

import pytest

from iblu_keeper.tools import drive_read as DR


class _Live:
    use_mock = False


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Resource:
    def __init__(self, name, log, scripted):
        self._name, self._log, self._scripted = name, log, scripted

    def __getattr__(self, method):
        if method.startswith("_"):
            raise AttributeError(method)
        key = f"{self._name}.{method}"

        def call(**kwargs):
            self._log.append((key, kwargs))
            result = self._scripted.get(key, {})
            if isinstance(result, list):
                result = result.pop(0) if result else {}
            return _Call(result)

        return call


class _FakeService:
    """`drive.files().list(**kw)` etc. — records (resource.method, kwargs)."""

    def __init__(self, scripted=None):
        self.calls: list[tuple[str, dict]] = []
        self._scripted = scripted or {}

    def __getattr__(self, resource):
        if resource.startswith("_"):
            raise AttributeError(resource)
        return lambda: _Resource(resource, self.calls, self._scripted)

    def kwargs(self, key):
        return [kw for k, kw in self.calls if k == key]

    def methods(self):
        return [k for k, _ in self.calls]


@pytest.fixture
def live(monkeypatch):
    def _install(scripted=None):
        fake = _FakeService(scripted)
        monkeypatch.setattr(DR, "settings", _Live())
        monkeypatch.setattr(DR, "_drive", lambda account=None: fake)
        return fake
    return _install


# --- dispatch ---------------------------------------------------------------


def test_unknown_action_names_the_options():
    """A typo would otherwise fall through to some default view and look like an answer."""
    with pytest.raises(DR.DriveReadError, match="unknown action.*search"):
        DR.drive_read("serach", query="x")


def test_mock_mode_reads_nothing(monkeypatch):
    """DRY_RUN is the suite's mode; a mock that reached Google would be a live call in tests."""
    monkeypatch.setattr(DR, "_drive", lambda account=None: (_ for _ in ()).throw(AssertionError("called _drive")))
    out = DR.drive_read("recent")
    assert out["_mock"] is True
    assert out["status"] == "not_read_mock"


def test_read_only_actions_never_call_a_writing_method(live):
    """The whole point of a read tool is that it can sit in the always-allow group."""
    fake = live({
        "files.get": {"id": "F", "mimeType": "application/vnd.google-apps.document"},
        "files.export": b"hi",
        "about.get": {"storageQuota": {}},
    })
    for action in DR.ACTIONS:
        DR.drive_read(action, file_id_or_url="F", query="plan", limit=5)
    verbs = {m.split(".")[1] for m in fake.methods()}
    assert verbs <= {"get", "list", "export"}, verbs


# --- search -----------------------------------------------------------------


def test_plain_words_become_a_fulltext_query_excluding_trash(live):
    """Words sent raw as `q` are a syntax error at Google, not a search."""
    fake = live({"files.list": {"files": [], "nextPageToken": None}})
    out = DR.drive_read("search", query="quarterly plan", limit=10)
    sent = fake.kwargs("files.list")[0]
    assert sent["q"] == "fullText contains 'quarterly plan' and trashed = false"
    assert sent["pageSize"] == 10
    assert sent["orderBy"] == "modifiedTime desc"
    assert sent["includeItemsFromAllDrives"] is True
    for field in ("owners", "webViewLink", "size", "modifiedTime", "mimeType"):
        assert field in sent["fields"]
    assert out["query_kind"] == "words"


def test_a_drive_q_string_passes_through(live):
    """`name contains 'x'` wrapped in fullText-contains would search for the operator text."""
    fake = live({"files.list": {"files": []}})
    DR.drive_read("search", query="name contains 'Budget' and mimeType = 'application/pdf'")
    assert fake.kwargs("files.list")[0]["q"] == (
        "(name contains 'Budget' and mimeType = 'application/pdf') and trashed = false"
    )
    assert DR.looks_like_drive_query("'abc123' in parents")
    assert not DR.looks_like_drive_query("notes from the board meeting")


def test_a_quote_in_plain_words_is_escaped(live):
    """O'Brien would otherwise close the string literal and break the query."""
    fake = live({"files.list": {"files": []}})
    DR.drive_read("search", query="O'Brien")
    assert "O\\'Brien" in fake.kwargs("files.list")[0]["q"]


def test_search_pages_and_reports_each_file(live):
    """Without the token round-tripped, page two is unreachable."""
    fake = live({"files.list": {
        "files": [{
            "id": "F1", "name": "Plan", "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-10-20T10:00:00Z", "size": "1234",
            "owners": [{"emailAddress": "ignas@blanklabel.team"}],
            "webViewLink": "https://docs.google.com/document/d/F1/edit",
        }],
        "nextPageToken": "TOK2",
    }})
    out = DR.drive_read("search", query="plan", page_token="TOK1")
    assert fake.kwargs("files.list")[0]["pageToken"] == "TOK1"
    assert out["next_page_token"] == "TOK2"
    row = out["items"][0]
    assert row["owners"] == ["ignas@blanklabel.team"]
    assert row["size_bytes"] == 1234
    assert row["url"].endswith("/F1/edit")


# --- the other listings --------------------------------------------------------


def test_get_reports_sharing_and_location(live):
    """Parents, sharing and trash state are what 'where is this and who sees it' needs."""
    fake = live({"files.get": {
        "id": "F1", "name": "Plan", "mimeType": "application/pdf", "parents": ["P1"],
        "shared": True, "starred": False, "trashed": False, "description": "Q4",
        "permissions": [
            {"type": "user", "role": "owner", "emailAddress": "ignas@blanklabel.team"},
            {"type": "anyone", "role": "reader"},
        ],
    }})
    out = DR.drive_read("get", file_id_or_url="https://drive.google.com/file/d/F1/view")
    sent = fake.kwargs("files.get")[0]
    for field in ("parents", "shared", "permissions", "description", "starred", "trashed"):
        assert field in sent["fields"]
    assert sent["supportsAllDrives"] is True
    assert out["parents"] == ["P1"]
    assert out["permissions_summary"] == ["ignas@blanklabel.team: owner", "anyone: reader"]
    assert out["shared"] is True


def test_recent_and_shared_with_me_use_the_right_filters(live):
    """Both are `files.list` underneath; only the filter and sort tell them apart."""
    fake = live({"files.list": {"files": []}})
    DR.drive_read("recent", limit=7)
    DR.drive_read("shared_with_me", limit=7)
    recent, shared = fake.kwargs("files.list")
    assert recent["q"] == "trashed = false" and recent["orderBy"] == "modifiedTime desc"
    assert shared["q"] == "sharedWithMe = true and trashed = false"
    assert shared["orderBy"] == "sharedWithMeTime desc"


def test_permissions_lists_roles_and_drops_deleted_ones(live):
    """A deleted permission still comes back from the API; reporting it as access would be wrong."""
    fake = live({"permissions.list": {"permissions": [
        {"id": "p1", "type": "user", "role": "writer", "emailAddress": "a@x.com"},
        {"id": "p2", "type": "user", "role": "reader", "emailAddress": "gone@x.com", "deleted": True},
    ]}})
    out = DR.drive_read("permissions", file_id_or_url="F1")
    assert fake.kwargs("permissions.list")[0]["supportsAllDrives"] is True
    assert [i["email"] for i in out["items"]] == ["a@x.com"]
    assert out["items"][0]["role"] == "writer"


def test_revisions_report_when_and_who(live):
    """'What did this look like before Tuesday' needs the time and the author of each version."""
    fake = live({"revisions.list": {"revisions": [
        {"id": "r1", "modifiedTime": "2026-10-20T10:00:00Z",
         "lastModifyingUser": {"emailAddress": "a@x.com"}, "size": "10"},
    ], "nextPageToken": "N"}})
    out = DR.drive_read("revisions", file_id_or_url="F1", limit=3)
    sent = fake.kwargs("revisions.list")[0]
    assert sent["pageSize"] == 3 and "lastModifyingUser" in sent["fields"]
    assert out["items"][0]["modified_by"] == "a@x.com"
    assert out["next_page_token"] == "N"


# --- export_text ------------------------------------------------------------------


def test_export_picks_csv_for_sheets_and_text_for_docs(live):
    """A Sheet exported as text/plain is a 400 from Google."""
    fake = live({
        "files.get": [
            {"id": "S", "mimeType": "application/vnd.google-apps.spreadsheet", "name": "Sheet"},
            {"id": "D", "mimeType": "application/vnd.google-apps.document", "name": "Doc"},
        ],
        "files.export": b"\xef\xbb\xbfa,b\n",
    })
    sheet = DR.drive_read("export_text", file_id_or_url="S")
    DR.drive_read("export_text", file_id_or_url="D")
    exports = fake.kwargs("files.export")
    assert exports[0]["mimeType"] == "text/csv"
    assert exports[1]["mimeType"] == "text/plain"
    assert sheet["text"] == "a,b\n", "the UTF-8 BOM Google prepends must not leak into the text"


def test_export_pages_at_20000_chars_with_next_offset(live):
    """Without paging, everything past 20,000 characters was unreachable — the end of a doc is where conclusions live."""
    long = "x" * 25_000
    fake = live({
        "files.get": {"id": "D", "mimeType": "application/vnd.google-apps.document"},
        "files.export": long,
    })
    first = DR.drive_read("export_text", file_id_or_url="D", max_chars=99_999)
    assert len(first["text"]) == 20_000, "asking for more must never grant more"
    assert first["truncated"] is True and first["next_offset"] == 20_000
    second = DR.drive_read("export_text", file_id_or_url="D", offset=first["next_offset"])
    assert second["text"] == "x" * 5_000
    assert second["truncated"] is False and second["continues_from"] == 20_000
    assert len(fake.kwargs("files.export")) == 2


def test_export_refuses_a_non_google_file_before_exporting(live):
    """`files.export` on a PDF is an error at Google; naming the type is more useful than relaying it."""
    fake = live({"files.get": {"id": "P", "mimeType": "application/pdf", "name": "deck.pdf"}})
    with pytest.raises(DR.DriveReadError, match="application/pdf"):
        DR.drive_read("export_text", file_id_or_url="P")
    assert fake.kwargs("files.export") == []


# --- comments and storage -------------------------------------------------------


def test_comments_include_resolved_threads_with_ids(live):
    """`gdoc_resolve_comment` lists only open ones; the history of a doc is in the resolved ones too."""
    fake = live({"comments.list": {"comments": [
        {"id": "c1", "content": "fix", "resolved": False, "author": {"displayName": "A"},
         "quotedFileContent": {"value": "Intro"},
         "replies": [{"id": "r1", "content": "ok", "action": None, "author": {"displayName": "B"}}]},
        {"id": "c2", "content": "done", "resolved": True, "author": {"displayName": "A"}},
    ]}})
    out = DR.drive_read("comments", file_id_or_url="F1")
    sent = fake.kwargs("comments.list")[0]
    assert sent["includeDeleted"] is False and "resolved" in sent["fields"]
    assert [c["comment_id"] for c in out["items"]] == ["c1", "c2"]
    assert out["open_count"] == 1
    assert out["items"][0]["replies"][0]["reply"] == "ok"


def test_storage_converts_to_gb_and_never_fabricates_a_percentage(live):
    """A Workspace account reports no limit; a percentage over an empty denominator is a lie."""
    live({"about.get": {
        "storageQuota": {"limit": "16106127360", "usage": "8053063680", "usageInDrive": "1000000000"},
        "user": {"emailAddress": "ignas@blanklabel.team"},
    }})
    out = DR.drive_read("storage")
    assert out["limit_gb"] == 16.11 and out["used_gb"] == 8.05
    assert out["percent_used"] == 50.0

    live({"about.get": {"storageQuota": {"usage": "8053063680"}}})
    unlimited = DR.drive_read("storage")
    assert unlimited["limit_gb"] is None
    assert unlimited["percent_used"] is None
    assert "note" in unlimited


def test_actions_that_need_a_file_say_so():
    """Google's error for a missing fileId is a 404 that names nothing."""
    with pytest.raises(DR.DriveReadError, match="get needs file_id_or_url"):
        DR.drive_read("get")
    with pytest.raises(DR.DriveReadError, match="search needs a query"):
        DR.drive_read("search", query="  ")
