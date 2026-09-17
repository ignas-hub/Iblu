"""Collector tests: git commits Ignas authored — no network, no real git, no
database.

`FakeConn` mirrors `tests/test_slack_sent.py`'s: a dict of watermarks keyed by
state name, and `ON CONFLICT (source, source_ref) DO NOTHING` simulated via a
list of already-seen refs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from iblu_keeper.collectors import git_commits as G

ME = "ignas@blanklabel.team"
SOMEONE_ELSE = "not-ignas@example.com"


# --- fakes -------------------------------------------------------------


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    """Enough of psycopg.Connection to run the collector's state calls."""

    def __init__(self):
        self.watermarks: dict[str, datetime] = {}
        self.state_calls: list[tuple] = []
        self.insert_calls: list[dict] = []
        self.inserted_refs: list[str] = []

    def execute(self, sql, params=None):
        s = sql.strip()
        if s.startswith("SELECT watermark"):
            (name,) = params
            row = {"watermark": self.watermarks[name]} if name in self.watermarks else None
            return _Result(row)
        if s.startswith("INSERT INTO collector_state"):
            name, watermark, cursor, error = params
            self.state_calls.append((name, watermark, cursor, error))
            if watermark is not None:
                self.watermarks[name] = watermark
            return _Result(None)
        if s.startswith("INSERT INTO signals"):
            self.insert_calls.append(dict(params))
            ref = params["source_ref"]
            if ref in self.inserted_refs:
                return _Result(None)
            self.inserted_refs.append(ref)
            return _Result({"id": len(self.inserted_refs)})
        raise AssertionError(f"unexpected SQL: {sql[:60]!r}")


class _Proc:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _log_entry(
    sha, email, author_iso, committer_iso=None, parents="", branch="main",
    subject="did a thing", body="", stat=None, name="Ignas Gee",
):
    committer_iso = committer_iso or author_iso
    rest = body
    if stat:
        files, ins, dele = stat
        rest += f"\n\n {files} files changed, {ins} insertions(+), {dele} deletions(-)"
    fields = [sha, name, email, author_iso, committer_iso, parents, branch, subject, rest]
    return G._RS + G._US.join(fields)


def _fake_run_all_repos(entries_by_repo: dict[str, str], remote_ok=False):
    """A fake `subprocess.run` dispatching on the `-C <root>` argument."""

    def fake_run(cmd, capture_output, text, timeout):
        assert capture_output and text
        root = cmd[cmd.index("-C") + 1]
        repo_name = Path(root).name
        out = entries_by_repo.get(repo_name, "")
        return _Proc(stdout=out, returncode=0)

    return fake_run


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("GIT_AUTHOR_EMAILS", raising=False)


@pytest.fixture
def one_email(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_EMAILS", ME)


@pytest.fixture
def no_github(monkeypatch):
    monkeypatch.setattr(G.gh, "configured", lambda: False)


@pytest.fixture
def one_repo(monkeypatch):
    monkeypatch.setattr(G, "ROOTS", {"iblu": Path("/fake/iblu")})


SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)


# --- author filtering --------------------------------------------------


def test_only_his_commits_are_recorded(monkeypatch, one_email, no_github, one_repo):
    log = (
        _log_entry("a" * 40, ME, "2026-09-16T10:00:00+00:00")
        + _log_entry("b" * 40, SOMEONE_ELSE, "2026-09-16T11:00:00+00:00")
    )
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"iblu": log}))
    conn = FakeConn()

    inserted = G.collect(conn)

    assert inserted == 1
    assert conn.insert_calls[0]["account"] == ME
    assert conn.insert_calls[0]["source_ref"] == f"git:{'a' * 40}"


# --- Claude co-authorship -----------------------------------------------


def test_claude_coauthored_commit_is_kept_and_flagged(monkeypatch, one_email, no_github, one_repo):
    """A Claude Code co-authored commit is still his work — kept, not dropped,
    and flagged in `meta` rather than silently attributed to no one."""
    log = _log_entry(
        "c" * 40, ME, "2026-09-16T10:00:00+00:00",
        body="\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
    )
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"iblu": log}))
    conn = FakeConn()

    G.collect(conn)

    from psycopg.types.json import Jsonb

    meta = conn.insert_calls[0]["meta"]
    assert isinstance(meta, Jsonb)
    assert meta.obj["coauthored_by_claude"] is True


def test_ordinary_commit_is_not_flagged(monkeypatch, one_email, no_github, one_repo):
    log = _log_entry("d" * 40, ME, "2026-09-16T10:00:00+00:00", body="no trailer here")
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"iblu": log}))
    conn = FakeConn()

    G.collect(conn)

    from psycopg.types.json import Jsonb

    meta: Jsonb = conn.insert_calls[0]["meta"]
    assert meta.obj["coauthored_by_claude"] is False


# --- merge commits -------------------------------------------------------


def test_merge_commit_is_skipped(monkeypatch, one_email, no_github, one_repo):
    log = _log_entry("e" * 40, ME, "2026-09-16T10:00:00+00:00", parents="p1 p2")
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"iblu": log}))
    conn = FakeConn()

    inserted = G.collect(conn)

    assert inserted == 0
    assert conn.insert_calls == []


# --- rebase / re-dated commits --------------------------------------------


def test_rebase_redated_commit_is_skipped(monkeypatch, one_email, no_github, one_repo):
    # Author date is 10 days before the committer date — a rebase, not new work.
    log = _log_entry(
        "f" * 40, ME,
        author_iso="2026-09-06T10:00:00+00:00",
        committer_iso="2026-09-16T10:00:00+00:00",
    )
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"iblu": log}))
    conn = FakeConn()

    inserted = G.collect(conn)

    assert inserted == 0


def test_commit_within_the_rebase_threshold_is_kept(monkeypatch, one_email, no_github, one_repo):
    log = _log_entry(
        "1" * 40, ME,
        author_iso="2026-09-16T10:00:00+00:00",
        committer_iso="2026-09-16T12:00:00+00:00",
    )
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"iblu": log}))
    conn = FakeConn()

    inserted = G.collect(conn)

    assert inserted == 1


# --- local + GitHub dedup -------------------------------------------------


def test_duplicate_sha_local_and_github_recorded_once(monkeypatch, one_email, one_repo):
    sha = "2" * 40
    log = _log_entry(sha, ME, "2026-09-16T10:00:00+00:00", stat=(2, 10, 5))
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"iblu": log}))

    monkeypatch.setattr(G.gh, "configured", lambda: True)
    monkeypatch.setattr(
        G, "_list_github_repos",
        lambda: [{"full_name": "someorg/iblu", "archived": False,
                   "pushed_at": "2026-09-16T00:00:00Z"}],
    )

    def fake_get(path, **params):
        assert path == "/repos/someorg/iblu/commits"
        return [{
            "sha": sha,
            "commit": {
                "author": {"name": "Ignas Gee", "email": ME, "date": "2026-09-16T10:00:00Z"},
                "committer": {"date": "2026-09-16T10:00:00Z"},
                "message": "did a thing",
            },
            "parents": [{"sha": "0" * 40}],
        }]

    monkeypatch.setattr(G.gh, "_get", fake_get)
    conn = FakeConn()

    inserted = G.collect(conn)

    assert inserted == 1
    assert conn.inserted_refs == [f"git:{sha}"]
    # The local copy wins — it carries real line counts, GitHub's does not.
    assert conn.insert_calls[0]["length_chars"] == 15


# --- venture / project mapping -------------------------------------------


def test_unmapped_repo_has_no_venture(monkeypatch, one_email, no_github):
    monkeypatch.setattr(G, "ROOTS", {"some-random-repo": Path("/fake/some-random-repo")})
    log = _log_entry("3" * 40, ME, "2026-09-16T10:00:00+00:00")
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"some-random-repo": log}))
    conn = FakeConn()

    G.collect(conn)

    row = conn.insert_calls[0]
    assert row["venture"] is None
    assert row["venture_confidence"] == "inferred"
    assert row["project"] is None


def test_mapped_repo_gets_venture_and_project_as_fact(monkeypatch, one_email, no_github):
    monkeypatch.setattr(G, "ROOTS", {"machina": Path("/fake/machina")})
    log = _log_entry("4" * 40, ME, "2026-09-16T10:00:00+00:00")
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"machina": log}))
    conn = FakeConn()

    G.collect(conn)

    row = conn.insert_calls[0]
    assert row["venture"] == "deadlift"
    assert row["project"] == "machina"
    assert row["venture_confidence"] == "fact"
    assert row["work_type"] == "build"


# --- snippet never exceeds the first line ---------------------------------


def test_snippet_is_only_the_first_line(monkeypatch, one_email, no_github, one_repo):
    log = _log_entry(
        "5" * 40, ME, "2026-09-16T10:00:00+00:00",
        subject="Fix the thing",
        body="\n\nThis body has a lot of extra detail about the diff and files.\nSecond line.",
    )
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"iblu": log}))
    conn = FakeConn()

    G.collect(conn)

    row = conn.insert_calls[0]
    assert row["snippet"] == "Fix the thing"
    assert "\n" not in row["snippet"]
    assert "extra detail" not in row["snippet"]


def test_subject_is_truncated_to_200_chars(monkeypatch, one_email, no_github, one_repo):
    long_subject = "x" * 250
    log = _log_entry("6" * 40, ME, "2026-09-16T10:00:00+00:00", subject=long_subject)
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"iblu": log}))
    conn = FakeConn()

    G.collect(conn)

    row = conn.insert_calls[0]
    assert len(row["subject"]) == 200


# --- watermark: read_through taken before the read ------------------------


def test_watermark_is_read_through_taken_before_the_read():
    import inspect

    source = inspect.getsource(G.collect)
    assert "max(newest, read_through)" in source
    assert "read_through = datetime.now(timezone.utc)" in source
    assert source.index("read_through =") < source.index("max(newest, read_through)")


def test_first_run_looks_back_seven_days():
    from iblu_keeper.collectors import default_since

    since = default_since(None, fallback_hours=G.FIRST_RUN_HOURS)
    age = datetime.now(timezone.utc) - since
    assert timedelta(days=6, hours=23) < age < timedelta(days=7, hours=1)


# --- one source failing does not lose the other ---------------------------


def test_github_failure_does_not_lose_local_commits(monkeypatch, one_email, one_repo):
    log = _log_entry("7" * 40, ME, "2026-09-16T10:00:00+00:00")
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"iblu": log}))

    monkeypatch.setattr(G.gh, "configured", lambda: True)

    def broken_listing():
        raise G.gh.GitHubError("rate limited")

    monkeypatch.setattr(G, "_list_github_repos", broken_listing)
    conn = FakeConn()

    inserted = G.collect(conn)

    assert inserted == 1


def test_local_failure_does_not_lose_github_commits(monkeypatch, one_email):
    monkeypatch.setattr(G, "ROOTS", {"iblu": Path("/fake/iblu")})

    def broken_run(cmd, capture_output, text, timeout):
        raise OSError("no such file or directory")

    monkeypatch.setattr(G.subprocess, "run", broken_run)

    monkeypatch.setattr(G.gh, "configured", lambda: True)
    monkeypatch.setattr(
        G, "_list_github_repos",
        lambda: [{"full_name": "someorg/iblu", "archived": False,
                   "pushed_at": "2026-09-16T00:00:00Z"}],
    )

    def fake_get(path, **params):
        return [{
            "sha": "8" * 40,
            "commit": {
                "author": {"name": "Ignas Gee", "email": ME, "date": "2026-09-16T10:00:00Z"},
                "committer": {"date": "2026-09-16T10:00:00Z"},
                "message": "did a thing",
            },
            "parents": [],
        }]

    monkeypatch.setattr(G.gh, "_get", fake_get)
    conn = FakeConn()

    inserted = G.collect(conn)

    assert inserted == 1


def test_all_sources_failing_raises(monkeypatch, one_email):
    monkeypatch.setattr(G, "ROOTS", {"iblu": Path("/fake/iblu")})

    def broken_run(cmd, capture_output, text, timeout):
        raise OSError("boom")

    monkeypatch.setattr(G.subprocess, "run", broken_run)

    monkeypatch.setattr(G.gh, "configured", lambda: True)

    def broken_listing():
        raise G.gh.GitHubError("outage")

    monkeypatch.setattr(G, "_list_github_repos", broken_listing)
    conn = FakeConn()

    with pytest.raises(RuntimeError, match="both sources failed"):
        G.collect(conn)
    # A failed run must never write a watermark — retried next tick.
    assert conn.state_calls == []


# --- dry run ---------------------------------------------------------------


def test_dry_run_counts_but_writes_nothing(monkeypatch, one_email, no_github, one_repo):
    log = _log_entry("9" * 40, ME, "2026-09-16T10:00:00+00:00")
    monkeypatch.setattr(G.subprocess, "run", _fake_run_all_repos({"iblu": log}))
    conn = FakeConn()

    inserted = G.collect(conn, dry=True)

    assert inserted == 1
    assert conn.insert_calls == []
    assert conn.state_calls == []


# --- idle / archived repos are not queried for commits ---------------------


def test_idle_and_archived_repos_are_not_queried(monkeypatch, one_email, no_github):
    monkeypatch.setattr(G, "ROOTS", {})  # local not under test here
    monkeypatch.setattr(G.gh, "configured", lambda: True)
    monkeypatch.setattr(
        G, "_list_github_repos",
        lambda: [
            {"full_name": "org/idle-repo", "archived": False, "pushed_at": "2026-08-01T00:00:00Z"},
            {"full_name": "org/archived-repo", "archived": True, "pushed_at": "2026-09-15T00:00:00Z"},
            {"full_name": "org/active-repo", "archived": False, "pushed_at": "2026-09-15T00:00:00Z"},
        ],
    )

    queried: list[str] = []

    def fake_get(path, **params):
        queried.append(path)
        return []

    monkeypatch.setattr(G.gh, "_get", fake_get)
    conn = FakeConn()
    conn.watermarks[G.NAME] = datetime(2026, 9, 10, tzinfo=timezone.utc)

    G.collect(conn)

    assert queried == ["/repos/org/active-repo/commits"]


def test_a_commit_authored_by_claude_itself_is_not_his_attention():
    """A cloud session committing at 03:00 is output, not attention. Even if
    someone listed the agent's address as one of his, it is still skipped."""
    from datetime import datetime, timedelta, timezone

    from iblu_keeper.collectors import git_commits as G

    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    commit = {
        "sha": "abc", "repo_name": "iblu", "subject": "fix thing", "body": "",
        "author_email": "noreply@anthropic.com", "author_name": "Claude",
        "author_date": now, "committer_date": now, "parent_count": 1,
    }
    assert G._build_row(commit, {"noreply@anthropic.com"}, now - timedelta(days=1)) is None


def test_mission_control_is_own_infra():
    from iblu_keeper.collectors import git_commits as G

    assert G.REPO_VENTURES["mission-control"][0] == "personal"
