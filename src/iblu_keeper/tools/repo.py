"""Reading Ignas's own code — the thing IBLU could not do.

IBLU could read his mail, chat, calendar and Drive but not a line of his own
source, which made "what does the tick job actually do?" a question he had to
open a laptop for.

This is deliberately ONE tool with an `action` rather than four tools: every
new `@mcp.tool` costs a manual permission click in the connector UI, forever
(HANDOFF.md §12).

Security. `mcp.iblugames.com` is internet-facing, so a filesystem read here is
a remote file-read primitive pointed at a box holding a Google refresh token,
an Anthropic key and a database password. The OAuth gate in front of the
connector is the main protection; this module assumes it will one day fail and
is built so that even then it cannot hand out a credential:

  * only paths under an explicit allowlist of roots are readable;
  * the real path is resolved and re-checked, so `..` and symlinks cannot escape;
  * credential-shaped filenames are refused wherever they appear, including
    inside an allowed root;
  * binaries are refused, output is capped;
  * nothing here writes, moves, deletes or executes anything.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import subprocess
from pathlib import Path

from ..config import settings

logger = logging.getLogger("iblu_keeper.repo")

_MOCK = {"status": "mock"}

# Only these trees are readable. Adding one is a deliberate edit, never a
# parameter the caller can supply.
ROOTS: dict[str, Path] = {
    "iblu": Path("/home/ignas/iblu"),
    "automations": Path("/home/ignas/automations"),
}

# Refused wherever they appear, even inside an allowed root. Matched against
# the file NAME, so a rename cannot smuggle one past.
SECRET_PATTERNS = (
    ".env", ".env.*", "*.env",
    "*.key", "*.pem", "*.p12", "*.pfx", "*.crt",
    "id_rsa*", "id_ed25519*", "*.ppk",
    "token*.json", "*credential*", "*secret*", "service-account*",
    "client_secret*", ".netrc", ".pgpass", ".htpasswd",
)

# Never worth reading, and huge.
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", "dist", "build", ".next", "data", "backups", ".claude",
}

MAX_CHARS = 20_000
MAX_ENTRIES = 400
MAX_MATCHES = 60
BINARY_SNIFF = 8000


class RepoError(Exception):
    """Bad request. The message never leaks a path outside the allowlist."""


def _root(repo: str) -> Path:
    if repo not in ROOTS:
        raise RepoError(f"unknown repo {repo!r} — available: {', '.join(sorted(ROOTS))}")
    return ROOTS[repo]


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pat) for pat in SECRET_PATTERNS)


def _resolve(repo: str, rel: str | None) -> Path:
    """Resolve `rel` inside `repo`, refusing anything that escapes the root."""
    root = _root(repo).resolve()
    target = (root / (rel or "")).resolve()
    # The containment check happens AFTER resolution, so `..` and symlinks are
    # both caught — checking the unresolved string would miss a symlink.
    if target != root and root not in target.parents:
        raise RepoError(f"path escapes {repo!r}")
    if _is_secret(target.name):
        raise RepoError(f"refusing to read {target.name} — credential-shaped filename")
    return target


def _skip(path: Path, root: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.relative_to(root).parts)


def _looks_binary(data: bytes) -> bool:
    chunk = data[:BINARY_SNIFF]
    if b"\x00" in chunk:
        return True
    # Mostly-unprintable content is binary enough to refuse.
    printable = sum(1 for b in chunk if 9 <= b <= 13 or 32 <= b <= 126 or b >= 128)
    return bool(chunk) and printable / len(chunk) < 0.85


def list_dir(repo: str, path: str | None = None) -> dict:
    root = _root(repo).resolve()
    target = _resolve(repo, path)
    if not target.is_dir():
        raise RepoError(f"not a directory: {path or '.'}")

    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name in SKIP_DIRS or _is_secret(child.name):
            continue
        entries.append({
            "name": child.name + ("/" if child.is_dir() else ""),
            "type": "dir" if child.is_dir() else "file",
            "bytes": child.stat().st_size if child.is_file() else None,
        })
        if len(entries) >= MAX_ENTRIES:
            break
    return {
        "repo": repo,
        "path": str(target.relative_to(root)) or ".",
        "count": len(entries),
        "entries": entries,
    }


def read_file(repo: str, path: str, max_chars: int = MAX_CHARS) -> dict:
    root = _root(repo).resolve()
    target = _resolve(repo, path)
    if not target.is_file():
        raise RepoError(f"not a file: {path}")
    if _skip(target, root):
        raise RepoError(f"refusing to read inside an excluded directory: {path}")

    raw = target.read_bytes()
    if _looks_binary(raw):
        raise RepoError(f"{path} looks binary — this tool reads text only")

    text = raw.decode("utf-8", errors="replace")
    limit = max(200, min(int(max_chars), MAX_CHARS))
    truncated = len(text) > limit
    return {
        "repo": repo,
        "path": str(target.relative_to(root)),
        "bytes": len(raw),
        "lines": text.count("\n") + 1,
        "truncated": truncated,
        "text": text[:limit],
    }


def search(repo: str, query: str, path: str | None = None) -> dict:
    """Literal, case-insensitive search. Never a regex the caller supplies."""
    if not query or not query.strip():
        raise RepoError("query must not be empty")
    root = _root(repo).resolve()
    base = _resolve(repo, path)
    needle = query.lower()

    matches = []
    for candidate in sorted(base.rglob("*")):
        if len(matches) >= MAX_MATCHES:
            break
        if not candidate.is_file() or _is_secret(candidate.name):
            continue
        if _skip(candidate, root):
            continue
        try:
            raw = candidate.read_bytes()
        except OSError:
            continue
        if _looks_binary(raw):
            continue
        for number, line in enumerate(
            raw.decode("utf-8", errors="replace").splitlines(), start=1
        ):
            if needle in line.lower():
                matches.append({
                    "path": str(candidate.relative_to(root)),
                    "line": number,
                    "text": line.strip()[:200],
                })
                if len(matches) >= MAX_MATCHES:
                    break
    return {"repo": repo, "query": query, "count": len(matches), "matches": matches}


_SAFE_LIMIT = re.compile(r"^\d{1,3}$")


def log(repo: str, limit: int = 20) -> dict:
    root = _root(repo)
    count = str(int(limit))
    if not _SAFE_LIMIT.match(count):
        raise RepoError("limit must be 1-999")
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", f"-{count}",
             "--format=%h%x1f%an%x1f%ad%x1f%s", "--date=short"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise RepoError(f"git log failed: {exc.stderr[:200]}") from exc
    except FileNotFoundError as exc:
        raise RepoError("git is not available") from exc

    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append(dict(zip(("sha", "author", "date", "subject"), parts)))
    return {"repo": repo, "count": len(commits), "commits": commits}


def _use_github(repo_name: str, source: str) -> bool:
    """Where to read from.

    One namespace, so the caller never has to know which machine a project is
    on: a local alias reads from disk (it may have uncommitted work), anything
    else is a GitHub repository. `source` forces it either way.
    """
    if source == "local":
        return False
    if source == "github":
        return True
    return repo_name not in ROOTS


# Where list/read/log start when the caller names no repository. `search`
# deliberately does NOT default: with no repo it searches everything.
DEFAULT_REPO = "iblu"


def repo(
    action: str = "list",
    repo_name: str | None = None,
    path: str | None = None,
    query: str | None = None,
    limit: int = 20,
    max_chars: int = MAX_CHARS,
    source: str = "auto",
    ref: str | None = None,
) -> dict:
    """Dispatch across the local disk and GitHub. One tool — see the docstring."""
    if settings.use_mock:
        return dict(_MOCK)

    from . import github_repo as gh

    if action == "repos":
        local = [
            {"name": name, "source": "local", "path": str(p), "exists": p.is_dir()}
            for name, p in sorted(ROOTS.items())
        ]
        if not gh.configured():
            return {
                "repos": local,
                "github": "not configured — set GITHUB_TOKEN to reach "
                          "repositories that are not on this box",
            }
        try:
            remote = gh.list_repos(limit=100)
        except gh.GitHubError as exc:
            return {"repos": local, "github": f"unavailable: {exc}"}
        return {
            "local": local,
            "github": remote["repos"],
            "count": len(local) + remote["count"],
        }

    if action == "search" and not repo_name:
        # Search across every repository the token can see. This is the whole
        # point of asking "where did I write X?" without knowing which project.
        from . import github_repo as gh_mod

        if not gh_mod.configured():
            raise RepoError(
                "searching all repositories needs GITHUB_TOKEN; name a local "
                f"repo instead ({', '.join(sorted(ROOTS))})"
            )
        try:
            return gh_mod.search(None, query or "")
        except gh_mod.GitHubError as exc:
            raise RepoError(str(exc)) from exc

    repo_name = repo_name or DEFAULT_REPO
    use_gh = _use_github(repo_name, source)

    try:
        if use_gh:
            if action == "list":
                return gh.list_dir(repo_name, path, ref)
            if action == "read":
                if not path:
                    raise RepoError("read requires a path")
                return gh.read_file(repo_name, path, max_chars, ref)
            if action == "search":
                return gh.search(repo_name, query or "")
            if action == "log":
                return gh.log(repo_name, limit)
        else:
            if action == "list":
                return list_dir(repo_name, path)
            if action == "read":
                if not path:
                    raise RepoError("read requires a path")
                return read_file(repo_name, path, max_chars)
            if action == "search":
                return search(repo_name, query or "", path)
            if action == "log":
                return log(repo_name, limit)
    except gh.GitHubError as exc:
        # Surface GitHub problems as the tool's own error type, so the caller
        # gets one consistent shape whichever source answered.
        raise RepoError(str(exc)) from exc

    raise RepoError(
        f"unknown action {action!r} — expected repos, list, read, search or log"
    )
