"""`tools.drive_manage` — the write half of the Drive domain.

Fakes only. `settings` is swapped whole (frozen dataclass, DRY_RUN pinned by
conftest) and `_drive` returns a fake service that records every call's
kwargs. No network.
"""

from __future__ import annotations

import inspect

import pytest

from iblu_keeper.tools import drive_manage as DM


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
        monkeypatch.setattr(DM, "settings", _Live())
        monkeypatch.setattr(DM, "_drive", lambda account=None: fake)
        return fake
    return _install


def _boom(monkeypatch):
    def fail(account=None):
        raise AssertionError("_drive() must not be called")
    monkeypatch.setattr(DM, "_drive", fail)


# --- dispatch and mock ----------------------------------------------------------


def test_unknown_action_names_the_options():
    """'delete' is not an action; the message must say what is, not guess."""
    with pytest.raises(DM.DriveManageError, match="unknown action.*trash"):
        DM.drive_manage("delete", "F1")


def test_mock_mode_writes_nothing_for_every_action(monkeypatch):
    """The suite runs in DRY_RUN; a mock that reached Google would trash a real file."""
    _boom(monkeypatch)
    for action in DM.ACTIONS:
        out = DM.drive_manage(action, "F1", new_name="n", folder_id_or_url="D", email="a@x.com", description="d")
        assert out["_mock"] is True
        assert out["status"].startswith("not_") and out["status"].endswith("_mock"), action


# --- the file updates ---------------------------------------------------------------


def test_copy_sends_name_and_parent(live):
    """Without `parents` the copy lands beside the original; without `name` Drive says 'Copy of'."""
    fake = live({"files.copy": {"id": "F2", "name": "Template v2", "mimeType": "application/vnd.google-apps.document"}})
    out = DM.drive_manage("copy", "F1", new_name="Template v2", folder_id_or_url="https://drive.google.com/drive/folders/DIR")
    sent = fake.kwargs("files.copy")[0]
    assert sent["fileId"] == "F1"
    assert sent["body"] == {"name": "Template v2", "parents": ["DIR"]}
    assert sent["supportsAllDrives"] is True
    assert out["status"] == "copied" and out["id"] == "F2" and out["source_id"] == "F1"

    fake = live({"files.copy": {"id": "F3"}})
    DM.drive_manage("copy", "F1")
    assert fake.kwargs("files.copy")[0]["body"] == {}


def test_rename_updates_only_the_name(live):
    """An update body with anything else in it would clobber that field."""
    fake = live({"files.update": {"id": "F1", "name": "New"}})
    out = DM.drive_manage("rename", "F1", new_name="  New ")
    sent = fake.kwargs("files.update")[0]
    assert sent["body"] == {"name": "New"} and sent["fileId"] == "F1"
    assert out["status"] == "renamed"
    with pytest.raises(DM.DriveManageError, match="new_name"):
        DM.drive_manage("rename", "F1", new_name="")


def test_move_replaces_every_previous_parent(live):
    """addParents alone leaves the file in both folders — a move that is really a link."""
    fake = live({
        "files.get": {"parents": ["OLD1", "OLD2"]},
        "files.update": {"id": "F1", "parents": ["NEW"]},
    })
    out = DM.drive_manage("move", "F1", folder_id_or_url="NEW")
    sent = fake.kwargs("files.update")[0]
    assert sent["addParents"] == "NEW" and sent["removeParents"] == "OLD1,OLD2"
    assert out["previous_parents"] == ["OLD1", "OLD2"] and out["status"] == "moved"


def test_trash_and_untrash_flip_the_trashed_flag(live):
    """trashed=true is recoverable for 30 days; that is the only removal this tool knows."""
    fake = live({"files.update": [{"id": "F1", "trashed": True}, {"id": "F1", "trashed": False}]})
    out = DM.drive_manage("trash", "F1")
    assert fake.kwargs("files.update")[0]["body"] == {"trashed": True}
    assert out["status"] == "trashed" and "untrash" in out["undo"]
    DM.drive_manage("untrash", "F1")
    assert fake.kwargs("files.update")[1]["body"] == {"trashed": False}


def test_star_unstar_and_description_send_one_field_each(live):
    fake = live({"files.update": {"id": "F1"}})
    DM.drive_manage("star", "F1")
    DM.drive_manage("unstar", "F1")
    DM.drive_manage("set_description", "F1", description="Q4 budget, final")
    bodies = [kw["body"] for kw in fake.kwargs("files.update")]
    assert bodies == [{"starred": True}, {"starred": False}, {"description": "Q4 budget, final"}]


def test_set_description_needs_a_value():
    """None would send `description: null` — Google rejects it; '' is the way to clear."""
    with pytest.raises(DM.DriveManageError, match="description"):
        DM.drive_manage("set_description", "F1")


# --- permanent delete is absent -------------------------------------------------------


def test_permanent_delete_is_absent_and_documented():
    """files.delete skips the Trash and cannot be undone from anywhere."""
    source = inspect.getsource(DM)
    assert "delete" not in DM.ACTIONS
    assert "files().delete" not in source
    assert "No permanent delete" in DM.__doc__


# --- share / unshare ----------------------------------------------------------------


def test_share_creates_a_user_permission_without_emailing(live):
    """viewer must become Drive's 'reader', and no notification goes out unless asked."""
    fake = live({"permissions.create": {"id": "p1", "role": "reader", "emailAddress": "a@x.com"}})
    out = DM.drive_manage("share", "F1", email="a@x.com", role="viewer", message="hi")
    sent = fake.kwargs("permissions.create")[0]
    assert sent["body"] == {"type": "user", "role": "reader", "emailAddress": "a@x.com"}
    assert sent["sendNotificationEmail"] is False
    assert "emailMessage" not in sent, "a message with no notification is a message nobody gets"
    assert sent["supportsAllDrives"] is True
    assert out["status"] == "shared" and out["permission_id"] == "p1" and out["notified"] is False


def test_share_with_notify_passes_the_message(live):
    fake = live({"permissions.create": {"id": "p1"}})
    DM.drive_manage("share", "F1", email="a@x.com", role="writer", notify=True, message="please review")
    sent = fake.kwargs("permissions.create")[0]
    assert sent["body"]["role"] == "writer"
    assert sent["sendNotificationEmail"] is True and sent["emailMessage"] == "please review"


def test_share_refuses_owner_before_touching_google(monkeypatch):
    """Ownership transfer is a one-way door; the refusal must not depend on mock or live mode."""
    _boom(monkeypatch)
    with pytest.raises(DM.DriveManageError, match="owner"):
        DM.drive_manage("share", "F1", email="a@x.com", role="owner")
    monkeypatch.setattr(DM, "settings", _Live())
    with pytest.raises(DM.DriveManageError, match="owner"):
        DM.drive_manage("share", "F1", email="a@x.com", role="Owner")


def test_share_refuses_an_unknown_role_and_a_bad_email(live):
    """Google's error for a bad role names the enum, not the word the caller used."""
    live()
    with pytest.raises(DM.DriveManageError, match="unknown role 'admin'"):
        DM.drive_manage("share", "F1", email="a@x.com", role="admin")
    with pytest.raises(DM.DriveManageError, match="email"):
        DM.drive_manage("share", "F1", email="not-an-address")


def test_unshare_finds_the_permission_by_email_and_deletes_it(live):
    """permissions.delete needs a permission id; nobody has one to hand."""
    fake = live({"permissions.list": {"permissions": [
        {"id": "p0", "role": "owner", "emailAddress": "ignas@blanklabel.team"},
        {"id": "p1", "role": "writer", "emailAddress": "Alice@X.com"},
    ]}})
    out = DM.drive_manage("unshare", "F1", email="alice@x.com")
    sent = fake.kwargs("permissions.delete")[0]
    assert sent == {"fileId": "F1", "permissionId": "p1", "supportsAllDrives": True}
    assert out["status"] == "unshared" and out["removed_role"] == "writer"


def test_unshare_refuses_the_owner_and_an_unknown_email(live):
    """Removing the owner's permission is a 403 at best and an orphaned file at worst."""
    fake = live({"permissions.list": {"permissions": [
        {"id": "p0", "role": "owner", "emailAddress": "ignas@blanklabel.team"},
    ]}})
    with pytest.raises(DM.DriveManageError, match="owns this file"):
        DM.drive_manage("unshare", "F1", email="ignas@blanklabel.team")
    with pytest.raises(DM.DriveManageError, match="no permission"):
        DM.drive_manage("unshare", "F1", email="nobody@x.com")
    assert fake.kwargs("permissions.delete") == []


# --- shortcuts ------------------------------------------------------------------------


def test_create_shortcut_points_at_the_target_inside_the_folder(live):
    """A shortcut is a files.create with a special mime type; a plain copy would duplicate the file."""
    fake = live({
        "files.get": {"name": "Roadmap"},
        "files.create": {"id": "S1", "name": "Roadmap", "mimeType": DM._SHORTCUT, "parents": ["DIR"]},
    })
    out = DM.drive_manage("create_shortcut", "https://docs.google.com/document/d/TARGET/edit", folder_id_or_url="DIR")
    sent = fake.kwargs("files.create")[0]
    assert sent["body"] == {
        "name": "Roadmap",
        "mimeType": "application/vnd.google-apps.shortcut",
        "shortcutDetails": {"targetId": "TARGET"},
        "parents": ["DIR"],
    }
    assert out["status"] == "created" and out["target_id"] == "TARGET"


def test_create_shortcut_needs_a_folder_and_honours_new_name(live):
    fake = live({"files.create": {"id": "S1"}})
    with pytest.raises(DM.DriveManageError, match="folder"):
        DM.drive_manage("create_shortcut", "TARGET")
    DM.drive_manage("create_shortcut", "TARGET", folder_id_or_url="DIR", new_name="Alias")
    assert fake.kwargs("files.create")[0]["body"]["name"] == "Alias"
    assert fake.kwargs("files.get") == [], "a given name needs no lookup of the target's"
