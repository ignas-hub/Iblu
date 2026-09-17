"""Collector: git commits Ignas authored, across local checkouts and GitHub.

Writing code is a large share of Ignas's real work (IBLU itself, Radovi,
Machina, BT automations) and it leaves no email or chat trace — a tick that
only reads Gmail/Chat/Calendar/Slack structurally cannot see it. A commit he
authored is exactly as much evidence of attention spent as a message he sent,
so it becomes a `source='git'` signal the same way.

Two sources, both read-only:
  * local checkouts under `tools.repo.ROOTS` — `git log`, nothing else, no
    shell, with a timeout;
  * GitHub, for repositories that are not checked out on this box — the REST
    API, reusing `tools.github_repo`'s token-trying helpers rather than
    duplicating auth.
A commit seen in both (pushed local work) is deduplicated by its full sha,
local winning because it carries real `--shortstat` line counts GitHub's
commit-listing endpoint does not.

Only Ignas's own commits count — see `_author_emails`. A commit authored by
him but built by Claude Code still counts as his work (he directs it); it is
kept and flagged via `meta.coauthored_by_claude` rather than being treated as
someone/something else's contribution.

Merge commits are skipped (merging is not authoring), and so is any commit
whose author date is more than `REBASE_THRESHOLD` older than its committer
date — a rebase or cherry-pick re-dating old work onto today would otherwise
double-count a day that already has its own commits.

KNOWN LIMITATION — a commit is a point in time, not a duration. It marks the
END of a stretch of coding, not its start: an hour of debugging before a
5-minute commit looks, to a timestamp, like five minutes of work. The
analyst's block-building (clustering signals into blocks, then a TAIL after
the last one and a GAP where evidence stops) was tuned against gmail/chat/
calendar/slack signals that are themselves spread across a conversation, so a
lone commit signal likely under-counts the coding time around it. This
collector deliberately does not try to fix that here — it only turns commits
into timestamped facts, the same shape every other collector already
produces — but the assumption is wrong for this source and worth revisiting
when the analyst's clustering is next touched.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from ..config import settings
from ..tools import github_repo as gh
from ..tools.repo import ROOTS
from . import default_since, get_watermark, insert_signal, set_state

logger = logging.getLogger("iblu_keeper.collectors.git_commits")

NAME = "git_commits"

# How far a first run looks back — a week, same reasoning as gmail_sent /
# slack_sent's FIRST_RUN_HOURS: a source joining mid-stream should not
# under-report its first day against every other collector.
FIRST_RUN_HOURS = 168

GIT_TIMEOUT = 30  # seconds; a hung `git log` must not hang the whole tick

# A commit whose author date is older than its committer date by more than
# this is a rebase / cherry-pick re-dating old work onto today, not new work.
REBASE_THRESHOLD = timedelta(days=7)

# GitHub call budget per run — this runs every tick, so it must stay cheap
# even with a large org behind the token.
MAX_GH_REPOS = 40
MAX_GH_COMMITS_PER_REPO = 100

_CLAUDE_COAUTHOR_RE = re.compile(r"co-authored-by:\s*claude", re.IGNORECASE)

# `git log` record/field separators — ASCII RS/US, never present in a commit
# message, so a multi-line body cannot be mistaken for a field boundary.
_RS = "\x1e"
_US = "\x1f"
_GIT_LOG_FORMAT = (
    f"{_RS}%H{_US}%an{_US}%ae{_US}%aI{_US}%cI{_US}%P{_US}%S{_US}%s{_US}%b"
)
_STAT_RE = re.compile(
    r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?"
)

# --------------------------------------------------------------------------
# venture / project mapping — from the REPO, never the commit message. A
# commit message says what changed; the repository is what genuinely
# identifies whose venture the work belongs to (plan-equivalent reasoning to
# slack_sent's workspace-default: "Deadlift's Slack IS Deadlift"). This is
# the authoritative mapping — extend it as new repos appear. Keyed by the
# bare repo name (case-insensitive), not the owner/org, since the same
# project can live under different owners locally vs. on GitHub.
#
# A tuple's second element may be None: the venture is still a *fact* (the
# repo genuinely belongs to that org/venture) even when no SEED_PROJECTS
# entry (store/projects.py) matches the repo name closely enough to claim.
# --------------------------------------------------------------------------
REPO_VENTURES: dict[str, tuple[str | None, str | None]] = {
    # personal / IBLU itself
    "iblu": ("personal", "iblu"),
    # Personal accounting automator. The local checkout (tools.repo.ROOTS
    # alias) is named "automations"; the GitHub repo behind it is named
    # "accounting" — same project, two names picked at different times.
    "automations": ("personal", "accounting-app"),
    "accounting": ("personal", "accounting-app"),
    # Jakusi (house/property) — Radovi is also referred to as "Posla".
    "radovi": ("jakusi", "radovi"),
    "posla": ("jakusi", "radovi"),
    # Deadlift's Machina.
    "machina": ("deadlift", "machina"),
    # BlankTracker org repos with a clear match in store/projects.SEED_PROJECTS.
    "email-writer": ("blt", "email-writer"),
    "scout": ("blt", "scout"),
    # SEED_PROJECTS literally names this project "Signals / AI outreach
    # system" — this is that project's repo.
    "signals": ("blt", "signals-outreach"),
    # Other BlankTracker org repos: venture is a fact (the org IS blt), but
    # nothing in SEED_PROJECTS names them closely enough to claim a project.
    "email-tracker": ("blt", None),
    "reports": ("blt", None),
    "activities-tracker": ("blt", None),
    "insights": ("blt", None),
    # Server ops for this box (disk cleanup, the infra collector). That is
    # precisely the `personal` venture: "Own tooling & infra (IBLU, accounting
    # bot, servers)".
    "mission-control": ("personal", None),
}


def _venture_and_project(repo_name: str) -> tuple[str | None, str | None, str]:
    """`(venture, project, venture_confidence)` for a bare repo name."""
    key = repo_name.rsplit("/", 1)[-1].lower()
    venture, project = REPO_VENTURES.get(key, (None, None))
    return venture, project, ("fact" if venture else "inferred")


# --------------------------------------------------------------------------
# author matching
# --------------------------------------------------------------------------


def _default_author_emails() -> set[str]:
    """The Google account emails IBLU already knows, lower-cased."""
    emails: set[str] = set()
    if settings.google_user_email:
        emails.add(settings.google_user_email.strip().lower())
    for account in settings.configured_accounts():
        email = (account.get("email") or "").strip().lower()
        if email:
            emails.add(email)
    return emails


def _author_emails() -> set[str]:
    """`GIT_AUTHOR_EMAILS` (comma-separated), or the known Google accounts.

    Read directly with `os.getenv` — this collector owns no `Settings` field
    (config.py is another worker's file this run).
    """
    raw = os.getenv("GIT_AUTHOR_EMAILS")
    if raw:
        return {e.strip().lower() for e in raw.split(",") if e.strip()}
    return _default_author_emails()


def _first_line(text: str) -> str:
    stripped = (text or "").strip()
    return stripped.splitlines()[0].strip() if stripped else ""


def _parse_gh_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# local source
# --------------------------------------------------------------------------


def _run_git_log(root: Path, since: datetime) -> str:
    """`git log --all` since `since`, one commit per record, stats inline.

    Read-only, no shell, bounded by `GIT_TIMEOUT`. `--shortstat` piggy-backs
    the added/deleted line count onto the same call rather than a second
    `git show` per commit.
    """
    cmd = [
        "git", "-C", str(root), "log", "--all", "--source",
        f"--since={since.isoformat()}",
        "--shortstat",
        f"--pretty=format:{_GIT_LOG_FORMAT}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"git log failed ({result.returncode}): {result.stderr[:200]}")
    return result.stdout


def _parse_local_log(output: str, repo_name: str) -> list[dict]:
    """Turn `_run_git_log`'s output into normalized commit dicts."""
    commits: list[dict] = []
    for entry in output.split(_RS)[1:]:
        parts = entry.split(_US, 8)
        if len(parts) < 9:
            continue
        sha, author_name, author_email, a_iso, c_iso, parents, branch, subject, rest = parts
        try:
            author_date = datetime.fromisoformat(a_iso.strip())
            committer_date = datetime.fromisoformat(c_iso.strip())
        except ValueError:
            continue

        stat = _STAT_RE.search(rest)
        files_changed = int(stat.group(1)) if stat else None
        lines_changed = None
        if stat:
            added = int(stat.group(2)) if stat.group(2) else 0
            deleted = int(stat.group(3)) if stat.group(3) else 0
            lines_changed = added + deleted

        commits.append({
            "sha": sha.strip(),
            "author_email": author_email.strip().lower(),
            "author_date": author_date,
            "committer_date": committer_date,
            "parent_count": len(parents.split()),
            "branch": branch.strip() or None,
            "subject": subject.strip(),
            "message_full": rest,
            "files_changed_count": files_changed,
            "files_changed_lines": lines_changed,
            "repo_name": repo_name,
            "container": repo_name,
        })
    return commits


def _collect_local(since: datetime) -> tuple[list[dict], bool, list[str]]:
    """`(commits, source_failed, errors)` from every repo under `ROOTS`.

    A single missing/broken checkout is logged and skipped — it never costs
    the others. The whole source only counts as failed when every configured
    root errored (or there were roots to try and none of them worked).
    """
    commits: list[dict] = []
    errors: list[str] = []
    ok = 0
    for repo_name, root in sorted(ROOTS.items()):
        try:
            # No upfront `is_dir()` check: `git -C <missing>` itself fails
            # with a clean non-zero exit that `_run_git_log` already turns
            # into a RuntimeError, so there is nothing this would catch that
            # the git invocation does not already catch.
            output = _run_git_log(root, since)
        except Exception as exc:  # one bad checkout must not lose the rest
            errors.append(f"{repo_name}: {exc}")
            logger.warning("%s: local repo %s unreadable: %s", NAME, repo_name, exc)
            continue
        ok += 1
        commits.extend(_parse_local_log(output, repo_name))
    failed = bool(ROOTS) and ok == 0
    return commits, failed, errors


# --------------------------------------------------------------------------
# GitHub source
# --------------------------------------------------------------------------


def _list_github_repos() -> list[dict]:
    """Every repository any configured token can see, with `archived` and
    `pushed_at` kept (github_repo.list_repos() strips them for its own
    callers). Reuses `gh._request` — the same per-token try/merge primitive
    `list_repos()` itself is built on — rather than re-implementing auth.
    """
    seen: dict[str, dict] = {}
    ok = False
    for token in settings.github_tokens:
        response = gh._request(
            token, "/user/repos", per_page=100, sort="pushed",
            affiliation="owner,collaborator,organization_member",
        )
        if response.status_code >= 400:
            continue
        ok = True
        for r in response.json():
            seen.setdefault(r["full_name"], r)
    if not ok and settings.github_tokens:
        raise gh.GitHubError("no repositories readable with any configured GitHub token")
    return sorted(seen.values(), key=lambda r: r.get("pushed_at") or "", reverse=True)


def _parse_github_commit(item: dict, bare: str, full: str) -> dict | None:
    sha = item.get("sha")
    commit = item.get("commit") or {}
    author = commit.get("author") or {}
    committer = commit.get("committer") or {}
    author_date = _parse_gh_ts(author.get("date"))
    committer_date = _parse_gh_ts(committer.get("date"))
    if not sha or author_date is None or committer_date is None:
        return None
    message = commit.get("message") or ""
    return {
        "sha": sha,
        "author_email": (author.get("email") or "").strip().lower(),
        "author_date": author_date,
        "committer_date": committer_date,
        "parent_count": len(item.get("parents") or []),
        "branch": None,  # not carried by the commit-listing endpoint
        "subject": _first_line(message),
        "message_full": message,
        "files_changed_count": None,  # would cost one extra call per commit
        "files_changed_lines": None,  # — not "cheaply available" here
        "repo_name": bare,
        "container": full,
    }


def _collect_github(since: datetime) -> tuple[list[dict], bool, list[str]]:
    """`(commits, source_failed, errors)` from GitHub.

    Not being configured at all is not a failure (a box with no GITHUB_TOKEN
    is a valid, if reduced, configuration) — only an actual outage while
    configured counts. Archived repos and repos not pushed to since the
    watermark are never queried for commits at all, so an idle org costs one
    listing call and nothing more.
    """
    if not gh.configured():
        return [], False, []

    try:
        repos = _list_github_repos()
    except Exception as exc:
        logger.warning("%s: GitHub repository listing failed: %s", NAME, exc)
        return [], True, [str(exc)]

    commits: list[dict] = []
    errors: list[str] = []
    considered = 0
    for repo in repos:
        if considered >= MAX_GH_REPOS:
            break
        full = repo.get("full_name") or ""
        if not full or repo.get("archived"):
            continue
        pushed_at = _parse_gh_ts(repo.get("pushed_at"))
        if pushed_at is not None and pushed_at < since:
            continue  # idle since the watermark — nothing to fetch

        considered += 1
        try:
            items = gh._get(
                f"/repos/{full}/commits",
                since=since.isoformat(),
                per_page=MAX_GH_COMMITS_PER_REPO,
            )
        except Exception as exc:  # one repo's outage must not lose the rest
            errors.append(f"{full}: {exc}")
            logger.warning("%s: GitHub commits for %s unreadable: %s", NAME, full, exc)
            continue

        bare = full.rsplit("/", 1)[-1]
        for item in items or []:
            parsed = _parse_github_commit(item, bare, full)
            if parsed is not None:
                commits.append(parsed)

    return commits, False, errors


# --------------------------------------------------------------------------
# row building + collect()
# --------------------------------------------------------------------------


def _build_row(commit: dict, author_emails: set[str], since: datetime) -> dict | None:
    """A `signals` row for `commit`, or None when it should not be recorded."""
    if commit["author_date"] < since:
        return None  # coarse over-fetch, filtered precisely — see collect()
    if commit["parent_count"] > 1:
        return None  # a merge is not authored work
    if commit["author_email"] in AGENT_AUTHOR_EMAILS:
        return None  # an agent's commit — output, not his attention (see above)
    if commit["author_email"] not in author_emails:
        return None  # not his commit
    if commit["committer_date"] - commit["author_date"] > REBASE_THRESHOLD:
        return None  # a rebase/cherry-pick re-dating old work onto today

    repo_name = commit["repo_name"]
    venture, project, venture_confidence = _venture_and_project(repo_name)
    subject = commit["subject"][:200]
    coauthored = bool(_CLAUDE_COAUTHOR_RE.search(commit.get("message_full") or ""))

    return {
        "source": "git",
        "kind": "commit",
        "account": commit["author_email"],
        "occurred_at": commit["author_date"],
        "actor": "me",
        "counterpart": repo_name,
        "container": commit.get("container") or repo_name,
        "subject": subject,
        # Never more than the first line — never the diff, never file
        # contents (plan D8-equivalent rule for every other collector).
        "snippet": subject,
        "length_chars": commit.get("files_changed_lines"),
        "venture": venture,
        "venture_confidence": venture_confidence,
        "project": project,
        "work_type": "build",  # writing/shipping code IS this kind of work
        "source_ref": f"git:{commit['sha']}",
        "meta": {
            "repo": repo_name,
            "branch": commit.get("branch"),
            "files_changed": commit.get("files_changed_count"),
            "coauthored_by_claude": coauthored,
        },
    }


# Commits whose AUTHOR is an AI agent. These are skipped even when the agent was
# working on his repos: an agent running unattended — a cloud session at 03:00 —
# produces commits at times Ignas was asleep, and IBLU records attention, not
# output. Commits HE authored with a `Co-Authored-By: Claude` trailer are kept,
# flagged `coauthored_by_claude`, because he was directing that session.
AGENT_AUTHOR_EMAILS = frozenset({"noreply@anthropic.com"})


def collect(conn: psycopg.Connection, *, dry: bool = False) -> int:
    """Insert a signal for every commit Ignas authored since the watermark.

    Runs once per tick (scope 'primary') — commits are not per-Google-account.
    """
    since = default_since(get_watermark(conn, NAME), fallback_hours=FIRST_RUN_HOURS)
    author_emails = _author_emails()
    # Taken BEFORE the read, so a commit made while this run is mid-flight is
    # re-read next run rather than skipped — same rule as gmail_sent/chat_sent.
    read_through = datetime.now(timezone.utc)

    local_commits, local_failed, local_errors = _collect_local(since)
    github_commits, github_failed, github_errors = _collect_github(since)

    if local_failed and github_failed:
        raise RuntimeError(
            "git_commits: both sources failed — local: "
            f"{'; '.join(local_errors) or 'unknown'}; github: "
            f"{'; '.join(github_errors) or 'unknown'}"
        )

    seen_shas: set[str] = set()
    rows: list[dict] = []
    newest = since
    # Local first: a commit seen both locally and on GitHub is recorded once,
    # keeping the local copy — it carries real --shortstat line counts the
    # GitHub commit-listing endpoint does not.
    for commit in local_commits + github_commits:
        if commit["sha"] in seen_shas:
            continue
        seen_shas.add(commit["sha"])
        row = _build_row(commit, author_emails, since)
        if row is None:
            continue
        newest = max(newest, row["occurred_at"])
        rows.append(row)

    inserted = 0
    for row in rows:
        if dry:
            logger.info("%s [dry]: would insert %s — %s", NAME, row["source_ref"], row["subject"])
            inserted += 1
            continue
        if insert_signal(conn, row):
            inserted += 1

    if not dry:
        set_state(conn, NAME, watermark=max(newest, read_through), error=None)
    logger.info(
        "%s: %d new signal(s) from %d local + %d github candidate(s)",
        NAME, inserted, len(local_commits), len(github_commits),
    )
    return inserted
