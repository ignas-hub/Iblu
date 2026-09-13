"""The repo reader — mostly a security test.

mcp.iblugames.com is internet-facing, so this tool is a remote file-read
primitive pointed at a box holding a Google refresh token, an Anthropic key and
a database password. These tests assume the OAuth gate in front of it will one
day fail and check that even then it cannot hand out a credential.
"""

from __future__ import annotations

import pytest

from iblu_keeper.tools import repo as R


def test_mock_mode_is_inert(monkeypatch):
    class _Mock:
        use_mock = True

    monkeypatch.setattr(R, "settings", _Mock())
    assert R.repo(action="list") == {"status": "mock"}


@pytest.mark.parametrize(
    "path",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "../automations/credentials",
        "src/../../../root/.ssh/id_rsa",
        "./../../etc/shadow",
    ],
)
def test_paths_cannot_escape_the_repo(path):
    with pytest.raises(R.RepoError, match="escapes"):
        R._resolve("iblu", path)


@pytest.mark.parametrize(
    "name",
    [
        ".env", ".env.prod", ".env.example", "prod.env",
        "token.json", "token.alias.json",
        "service-account.json", "client_secret_x.json",
        "id_rsa", "id_ed25519.pub", "server.key", "cert.pem",
        "my-credentials", "SECRETS.txt", ".netrc", ".pgpass",
    ],
)
def test_credential_shaped_filenames_are_refused(name):
    assert R._is_secret(name), f"{name} would have been readable"


@pytest.mark.parametrize(
    "name", ["server.py", "README.md", "MISSION.md", "schema.sql", "notes.txt"]
)
def test_ordinary_files_are_not_refused(name):
    assert not R._is_secret(name)


def test_unknown_repo_is_refused_and_lists_the_real_ones():
    with pytest.raises(R.RepoError) as exc:
        R._root("etc")
    assert "iblu" in str(exc.value)


def test_excluded_directories_are_not_readable():
    with pytest.raises(R.RepoError):
        R.read_file("iblu", ".git/config")


def test_binary_sniffing():
    assert R._looks_binary(b"\x00\x01\x02binary")
    assert not R._looks_binary(b"plain ascii source code\n")
    assert not R._looks_binary("# comment with unicode — em dash\n".encode())


def test_search_requires_a_query():
    with pytest.raises(R.RepoError):
        R.search("iblu", "   ")


def test_unknown_action_names_the_valid_ones(monkeypatch):
    class _Live:
        use_mock = False

    monkeypatch.setattr(R, "settings", _Live())
    with pytest.raises(R.RepoError) as exc:
        R.repo(action="delete")
    assert "search" in str(exc.value)


def test_it_can_actually_read_this_repo(monkeypatch):
    class _Live:
        use_mock = False

    monkeypatch.setattr(R, "settings", _Live())
    out = R.repo(action="read", path="docs/MISSION.md", max_chars=500)
    assert out["text"].startswith("# IBLU — Mission")
    assert out["truncated"] is True
    listing = R.repo(action="list")
    names = {e["name"] for e in listing["entries"]}
    assert "src/" in names and ".env" not in names
