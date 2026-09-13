"""Tests for the `account` parameter threaded through the read (+ send) tools.

Session 6 (docs/plans/2026-09-13-sessions5-8.md §2) added multi-account
support (`GOOGLE_ACCOUNTS='blt,deadlift,choco'`); this file covers the one
piece that wasn't done yet: giving the read tools (and the three send tools
that keep asking) an `account` parameter, validated against
`settings.account_aliases` via the single `config.resolve_account` helper,
plus a per-account section on `server_health`.

Tests substitute a stub `settings` object on the relevant module (per
conftest's rule: `Settings` is frozen, never patch it directly) and a fake
`build_service` / `get_backend`, so nothing here touches the network, the
database, or the live `.env`.
"""

from __future__ import annotations

import pytest


class _Live:
    """Minimal live-mode settings stand-in with three configured accounts."""

    use_mock = False
    dry_run = False
    google_user_email = "ignas@blanklabel.team"
    account_aliases = ("blt", "deadlift", "choco")
    primary_alias = "blt"

    _EMAILS = {
        "blt": "ignas@blanklabel.team",
        "deadlift": "admin@deadlift.io",
        "choco": "ignacio@chocoagency.com",
    }

    def account(self, alias: str) -> dict:
        alias = alias.lower()
        return {
            "alias": alias,
            "email": self._EMAILS.get(alias, ""),
            "client_id": "x",
            "client_secret": "y",
            "token_file": f"/nonexistent/token.{alias}.json",
        }


def _fake_gmail_service(captured):
    class FakeExec:
        def execute(self):
            return {"messages": []}

    class FakeMessages:
        def list(self, **kwargs):
            return FakeExec()

        def get(self, **kwargs):
            return FakeExec()

    class FakeUsers:
        def messages(self):
            return FakeMessages()

    class FakeGmailService:
        def users(self):
            return FakeUsers()

    def fake_build_service(api, version, scopes=None, account=None):
        captured["api"] = api
        captured["account"] = account
        return FakeGmailService()

    return fake_build_service


# --------------------------------------------------------------------------- #
# config.resolve_account — the one shared validation helper
# --------------------------------------------------------------------------- #
def test_resolve_account_default_means_primary(monkeypatch):
    import iblu_keeper.config as config

    monkeypatch.setattr(config, "settings", _Live())
    assert config.resolve_account(None) is None


def test_resolve_account_known_alias_is_normalized(monkeypatch):
    import iblu_keeper.config as config

    monkeypatch.setattr(config, "settings", _Live())
    assert config.resolve_account("DEADLIFT") == "deadlift"
    assert config.resolve_account("choco") == "choco"


def test_resolve_account_unknown_alias_lists_valid_ones(monkeypatch):
    import iblu_keeper.config as config

    monkeypatch.setattr(config, "settings", _Live())
    with pytest.raises(config.UnknownAccountError, match="blt, deadlift, choco"):
        config.resolve_account("acme")


# --------------------------------------------------------------------------- #
# Gmail — search() as the representative read tool
# --------------------------------------------------------------------------- #
def test_gmail_search_default_account_reaches_build_service_as_none(monkeypatch):
    import iblu_keeper.config as config
    import iblu_keeper.google_auth as google_auth
    import iblu_keeper.tools.gmail as gmail_real

    monkeypatch.setattr(config, "settings", _Live())
    monkeypatch.setattr(gmail_real, "settings", _Live())
    captured: dict = {}
    monkeypatch.setattr(google_auth, "build_service", _fake_gmail_service(captured))

    gmail_real.search("hello")
    assert captured["account"] is None
    assert captured["api"] == "gmail"


def test_gmail_search_explicit_account_reaches_build_service(monkeypatch):
    import iblu_keeper.config as config
    import iblu_keeper.google_auth as google_auth
    import iblu_keeper.tools.gmail as gmail_real

    monkeypatch.setattr(config, "settings", _Live())
    monkeypatch.setattr(gmail_real, "settings", _Live())
    captured: dict = {}
    monkeypatch.setattr(google_auth, "build_service", _fake_gmail_service(captured))

    gmail_real.search("hello", account="deadlift")
    assert captured["account"] == "deadlift"


def test_gmail_search_unknown_account_raises_before_touching_google(monkeypatch):
    import iblu_keeper.config as config
    import iblu_keeper.google_auth as google_auth
    import iblu_keeper.tools.gmail as gmail_real

    monkeypatch.setattr(config, "settings", _Live())
    monkeypatch.setattr(gmail_real, "settings", _Live())
    captured: dict = {}
    monkeypatch.setattr(google_auth, "build_service", _fake_gmail_service(captured))

    with pytest.raises(config.UnknownAccountError, match="blt, deadlift, choco"):
        gmail_real.search("hello", account="acme")
    assert "account" not in captured, "must fail before ever calling build_service"


def test_gmail_get_message_routes_account(monkeypatch):
    import iblu_keeper.config as config
    import iblu_keeper.google_auth as google_auth
    import iblu_keeper.tools.gmail as gmail_real

    monkeypatch.setattr(config, "settings", _Live())
    monkeypatch.setattr(gmail_real, "settings", _Live())
    captured: dict = {}
    monkeypatch.setattr(google_auth, "build_service", _fake_gmail_service(captured))

    gmail_real.get_message("MSG1", account="choco")
    assert captured["account"] == "choco"


def test_gmail_search_mock_mode_is_unaffected_by_the_new_parameter():
    """Default (mock) behaviour, exercised with no `account` argument at all,
    stays byte-for-byte identical to before this change."""
    import iblu_keeper.tools.gmail as gmail_real

    page = gmail_real.search("anything")
    assert page["items"] and page["items"][0]["_mock"] is True


# --------------------------------------------------------------------------- #
# Chat — list_conversations / get_messages / list_unread / send_message /
# mark_read all route through get_backend(account)
# --------------------------------------------------------------------------- #
class _FakeChatBackend:
    def list_conversations(self, query=None, limit=20):
        return [{"id": "spaces/FAKE"}]

    def get_messages(self, conversation, limit=20, page_token=None):
        return {"items": [], "count": 0, "next_page_token": None}

    def list_unread(self, limit=10):
        return []

    def mark_read(self, conversation):
        return {"conversation": conversation, "status": "read"}

    def send_message(self, conversation, text):
        return {"id": "m1", "conversation": conversation, "text": text, "status": "sent"}


@pytest.mark.parametrize(
    "call, expect_account",
    [
        (lambda m: m.list_conversations(), None),
        (lambda m: m.list_conversations(account="deadlift"), "deadlift"),
        (lambda m: m.get_messages("spaces/X", account="choco"), "choco"),
        (lambda m: m.list_unread(account="deadlift"), "deadlift"),
        (lambda m: m.mark_read("spaces/X", account="choco"), "choco"),
        (lambda m: m.send_message("spaces/X", "hi", account="deadlift"), "deadlift"),
    ],
)
def test_chat_tools_route_account_to_get_backend(monkeypatch, call, expect_account):
    import iblu_keeper.config as config
    import iblu_keeper.tools.chat as chat_real

    monkeypatch.setattr(config, "settings", _Live())
    captured: dict = {}

    def fake_get_backend(account=None):
        captured["account"] = account
        return _FakeChatBackend()

    monkeypatch.setattr(chat_real, "get_backend", fake_get_backend)
    call(chat_real)
    assert captured["account"] == expect_account


def test_chat_list_conversations_unknown_account_raises(monkeypatch):
    import iblu_keeper.config as config
    import iblu_keeper.tools.chat as chat_real

    monkeypatch.setattr(config, "settings", _Live())
    captured: dict = {}
    monkeypatch.setattr(
        chat_real, "get_backend",
        lambda account=None: captured.setdefault("account", account) or _FakeChatBackend(),
    )

    with pytest.raises(config.UnknownAccountError, match="blt, deadlift, choco"):
        chat_real.list_conversations(account="acme")
    assert "account" not in captured


# --------------------------------------------------------------------------- #
# Drive — drive_list_folder()
# --------------------------------------------------------------------------- #
def test_drive_list_folder_routes_account(monkeypatch):
    import iblu_keeper.config as config
    import iblu_keeper.google_auth as google_auth
    import iblu_keeper.tools.drive as drive_real

    monkeypatch.setattr(config, "settings", _Live())
    monkeypatch.setattr(drive_real, "settings", _Live())
    captured: dict = {}

    class FakeExec:
        def execute(self):
            return {"files": []}

    class FakeFiles:
        def list(self, **kwargs):
            return FakeExec()

    class FakeDriveService:
        def files(self):
            return FakeFiles()

    def fake_build_service(api, version, scopes=None, account=None):
        captured["account"] = account
        return FakeDriveService()

    monkeypatch.setattr(google_auth, "build_service", fake_build_service)

    drive_real.drive_list_folder()
    assert captured["account"] is None

    drive_real.drive_list_folder(account="choco")
    assert captured["account"] == "choco"

    with pytest.raises(config.UnknownAccountError, match="blt, deadlift, choco"):
        drive_real.drive_list_folder(account="acme")


# --------------------------------------------------------------------------- #
# server_health / google_auth — per-account auth status
# --------------------------------------------------------------------------- #
def test_auth_status_by_account_covers_every_configured_alias():
    from iblu_keeper.config import settings
    from iblu_keeper.google_auth import auth_status_by_account

    result = auth_status_by_account()
    assert set(result.keys()) == set(settings.account_aliases)
    for status in result.values():
        assert {"ok", "account", "error", "token_file_exists"} <= status.keys()
        # The whole suite runs with DRY_RUN=true (conftest) — never "live" here.
        assert status["ok"] is False


def test_auth_status_by_account_never_raises_and_never_prints_secrets(monkeypatch):
    """A broken account is reported as ITS OWN status; it never takes down
    the whole probe, and no secret value ever appears in the result."""
    import iblu_keeper.google_auth as google_auth

    class Stub:
        dry_run = False
        account_aliases = ("blt", "broken")
        primary_alias = "blt"

        def account(self, alias):
            if alias == "blt":
                return {
                    "alias": "blt", "email": "ignas@blanklabel.team",
                    "client_id": "id-secret", "client_secret": "shh-secret",
                    "token_file": "/nonexistent/blt.json",
                }
            return {
                "alias": "broken", "email": "", "client_id": "", "client_secret": "",
                "token_file": "/nonexistent/broken.json",
            }

    monkeypatch.setattr(google_auth, "settings", Stub())
    result = google_auth.auth_status_by_account()

    assert result["blt"]["ok"] is False
    assert result["broken"]["ok"] is False
    assert "No OAuth client configured" in result["broken"]["error"]
    dumped = repr(result)
    assert "id-secret" not in dumped and "shh-secret" not in dumped


def test_server_health_reports_a_section_per_account():
    from iblu_keeper.config import settings
    from iblu_keeper.server import server_health

    result = server_health()
    assert "accounts" in result
    assert set(result["accounts"].keys()) == set(settings.account_aliases)
    # The primary account's own probe is unaffected by adding the section.
    assert "auth" in result and {"ok", "account", "error"} <= result["auth"].keys()
