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


# --- GitHub source --------------------------------------------------------


class _Settings:
    """Stand-in for the frozen Settings dataclass."""

    github_owner = "ignas-hub"

    def __init__(self, *tokens):
        self.github_token = tokens[0] if tokens else ""
        self.github_tokens = tuple(t for t in tokens if t)


def _no_token(monkeypatch):
    from iblu_keeper.tools import github_repo as gh

    monkeypatch.setattr(gh, "settings", _Settings())
    return gh


def _with_token(monkeypatch, *tokens):
    from iblu_keeper.tools import github_repo as gh

    monkeypatch.setattr(gh, "settings", _Settings(*(tokens or ("ghp_test",))))
    return gh


def test_github_is_optional_and_says_so(monkeypatch):
    """No token must degrade to local-only, never break the tool."""
    gh = _no_token(monkeypatch)
    assert gh.configured() is False

    class _Live:
        use_mock = False

    monkeypatch.setattr(R, "settings", _Live())
    out = R.repo(action="repos")
    assert "not configured" in out["github"]
    assert {r["name"] for r in out["repos"]} == {"iblu", "automations"}


def test_bare_repo_names_get_the_owner(monkeypatch):
    gh = _with_token(monkeypatch)
    assert gh._full_name("machina") == "ignas-hub/machina"
    assert gh._full_name("someorg/thing") == "someorg/thing"


def test_routing_between_local_and_github():
    # local aliases read from disk (they may hold uncommitted work)
    assert R._use_github("iblu", "auto") is False
    assert R._use_github("automations", "auto") is False
    # anything else is GitHub
    assert R._use_github("machina", "auto") is True
    assert R._use_github("ignas-hub/insights", "auto") is True
    # and the caller can force either
    assert R._use_github("iblu", "github") is True
    assert R._use_github("machina", "local") is False


def test_github_refuses_credential_files_too(monkeypatch):
    """A repo can contain a committed secret by accident."""
    gh = _with_token(monkeypatch)
    with pytest.raises(gh.GitHubError, match="credential-shaped"):
        gh.read_file("machina", "config/.env")


class _Resp:
    def __init__(self, code, remaining="42", payload=None):
        self.status_code = code
        self.headers = {"x-ratelimit-remaining": remaining}
        self.text = "nope"
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def test_github_errors_are_actionable(monkeypatch):
    gh = _with_token(monkeypatch)
    for code, expect in [
        (401, "expired or revoked"),
        (403, "Contents: Read"),
        (404, "not found"),
    ]:
        monkeypatch.setattr(gh, "_request", lambda *a, **k: _Resp(code))
        with pytest.raises(gh.GitHubError, match=expect):
            gh._get("/anything")


def test_github_rate_limit_is_distinguished_from_permission(monkeypatch):
    gh = _with_token(monkeypatch)
    monkeypatch.setattr(gh, "_request", lambda *a, **k: _Resp(403, remaining="0"))
    with pytest.raises(gh.GitHubError, match="rate limit"):
        gh._get("/anything")


def test_a_missing_token_is_explained_not_crashed(monkeypatch):
    gh = _no_token(monkeypatch)
    with pytest.raises(gh.GitHubError, match="no GitHub token configured"):
        gh._get("/anything")


def test_a_second_token_is_tried_when_the_first_cannot_see_the_repo(monkeypatch):
    """Org repos need their own fine-grained token; personal ones can't see them."""
    gh = _with_token(monkeypatch, "personal", "org")
    tried = []

    def _req(token, path, **params):
        tried.append(token)
        return _Resp(404) if token == "personal" else _Resp(200, payload={"ok": True})

    monkeypatch.setattr(gh, "_request", _req)
    assert gh._get("/repos/someorg/machina") == {"ok": True}
    assert tried == ["personal", "org"]


def test_the_404_message_points_at_organisation_tokens(monkeypatch):
    gh = _with_token(monkeypatch, "only-personal")
    monkeypatch.setattr(gh, "_request", lambda *a, **k: _Resp(404))
    with pytest.raises(gh.GitHubError, match="GITHUB_TOKEN_2"):
        gh._get("/repos/someorg/machina")


# --- long files must be reachable, not merely reported as cut (2026-10-25) --
#
# `MAX_CHARS` is a ceiling per call and `min(max_chars, MAX_CHARS)` means asking
# for more never grants more. With no offset there was no way to ask for a LATER
# part either, so everything past the first 20,000 characters of a file was
# unreachable. A session reading HANDOFF.md (59k) got sections 1-10 of 29 and
# reported the record as ending in September; §§28-29 carry the rules in force.


def test_a_long_file_can_be_read_to_the_end():
    from iblu_keeper.tools import repo as R

    pieces, offset, calls = [], 0, 0
    while True:
        result = R.read_file("iblu", "HANDOFF.md", offset=offset)
        pieces.append(result["text"])
        calls += 1
        if not result["truncated"]:
            break
        offset = result["next_offset"]
        assert calls < 50, "paging is not converging"

    assert calls > 1, "the fixture must be longer than one window, or it proves nothing"
    assert "".join(pieces) == (
        __import__("pathlib").Path(R.ROOTS["iblu"] / "HANDOFF.md").read_text()
    ), "the reassembled pieces must be the file, byte for byte"


def test_a_truncated_result_carries_the_argument_that_continues_it():
    """A reader must not need to know the tool's internals to get the rest."""
    from iblu_keeper.tools.repo import window

    r = window("x" * 50_000, max_chars=20_000)
    assert r["truncated"] is True
    assert r["next_offset"] == 20_000
    assert "offset=20000" in r["read_the_rest"]
    assert "Do NOT summarise" in r["read_the_rest"]


def test_the_last_window_says_it_is_the_last():
    from iblu_keeper.tools.repo import window

    r = window("x" * 1000, max_chars=20_000)
    assert r["truncated"] is False
    assert "next_offset" not in r
    assert "read_the_rest" not in r


def test_max_chars_cannot_be_raised_above_the_ceiling():
    """It is a ceiling, not a default — the cap exists to protect the context."""
    from iblu_keeper.tools.repo import MAX_CHARS, window

    r = window("x" * 100_000, max_chars=90_000)
    assert len(r["text"]) == MAX_CHARS
    assert r["truncated"] is True


def test_a_window_past_the_end_is_empty_not_an_error():
    from iblu_keeper.tools.repo import window

    r = window("x" * 100, max_chars=20_000, offset=500)
    assert r["text"] == ""
    assert r["truncated"] is False


def test_a_negative_offset_reads_from_the_start():
    from iblu_keeper.tools.repo import window

    assert window("abcdef", max_chars=20_000, offset=-5)["offset"] == 0


def test_both_readers_window_the_same_way():
    """The local and GitHub readers drifted once; only one reported truncation."""
    import inspect

    from iblu_keeper.tools import github_repo

    source = inspect.getsource(github_repo.read_file)
    assert "window(text, max_chars, offset)" in source
    assert "text[:limit]" not in source, "github_repo must not re-implement the cap"


def test_the_mcp_tool_tells_a_session_how_to_page():
    """The instruction has to live where a reader of the tool will meet it."""
    import inspect

    from iblu_keeper import server

    doc = inspect.getdoc(server.repo.fn if hasattr(server.repo, "fn") else server.repo)
    assert "next_offset" in doc
    assert "offset=" in doc
    assert "HANDOFF.md" in doc, "name the file this actually bit"
