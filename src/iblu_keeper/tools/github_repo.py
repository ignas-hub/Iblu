"""GitHub as a source for the `repo` tool.

The point of the repo reader is that any Claude session with IBLU can read any
of Ignas's projects — and most of them do not live on this box. Reading them
over the API rather than the filesystem covers every machine at once and
touches no local disk, so the credential-exposure question does not arise.

The token is a fine-grained PAT with Contents+Metadata read only. It cannot
write, cannot see Actions secrets, and cannot reach anything outside the
repositories it was scoped to.

Even so, a repository can contain a committed secret by accident, so the same
credential-shaped-filename refusal as the local reader applies here: IBLU
declining to read a stray `.env` out of a repo is cheap insurance.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import requests

from ..config import settings

logger = logging.getLogger("iblu_keeper.repo.github")

API = "https://api.github.com"
TIMEOUT = 20
MAX_CHARS = 20_000
MAX_ENTRIES = 400
MAX_MATCHES = 60
# GitHub returns base64 for files up to 1 MB on the contents endpoint.
MAX_FILE_BYTES = 1_000_000


class GitHubError(Exception):
    """A GitHub request failed, or the token is missing/insufficient."""


def configured() -> bool:
    return bool(settings.github_token)


def _headers() -> dict[str, str]:
    if not settings.github_token:
        raise GitHubError(
            "GITHUB_TOKEN is not set — IBLU can only read repositories on this "
            "box. Add a fine-grained read-only token to .env to reach the rest."
        )
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(path: str, **params: Any) -> Any:
    url = path if path.startswith("http") else f"{API}{path}"
    try:
        response = requests.get(url, headers=_headers(), params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise GitHubError(f"GitHub unreachable: {exc}") from exc

    if response.status_code == 401:
        raise GitHubError("GitHub rejected the token (401) — it may be expired or revoked")
    if response.status_code == 403:
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining == "0":
            raise GitHubError("GitHub rate limit reached — try again shortly")
        raise GitHubError(
            "GitHub returned 403 — the token likely lacks Contents: Read on that "
            "repository, or the repository is outside its scope"
        )
    if response.status_code == 404:
        raise GitHubError(
            "not found — either the path does not exist, or the token's "
            "repository scope does not include it"
        )
    if response.status_code >= 400:
        raise GitHubError(f"GitHub returned {response.status_code}: {response.text[:200]}")
    return response.json()


def _full_name(repo_name: str) -> str:
    """'machina' -> 'ignas-hub/machina'; 'org/thing' passes through."""
    return repo_name if "/" in repo_name else f"{settings.github_owner}/{repo_name}"


def list_repos(limit: int = 100) -> dict:
    """Every repository the token can see, most recently pushed first."""
    data = _get("/user/repos", per_page=min(limit, 100), sort="pushed", affiliation="owner,collaborator,organization_member")
    return {
        "source": "github",
        "count": len(data),
        "repos": [
            {
                "name": r["full_name"],
                "private": r["private"],
                "pushed_at": r.get("pushed_at"),
                "language": r.get("language"),
                "description": (r.get("description") or "")[:120],
            }
            for r in data
        ],
    }


def list_dir(repo_name: str, path: str | None = None, ref: str | None = None) -> dict:
    from .repo import _is_secret, SKIP_DIRS

    full = _full_name(repo_name)
    params = {"ref": ref} if ref else {}
    data = _get(f"/repos/{full}/contents/{(path or '').strip('/')}", **params)
    if isinstance(data, dict):
        raise GitHubError(f"{path} is a file, not a directory — use action='read'")

    entries = []
    for item in data:
        if item["name"] in SKIP_DIRS or _is_secret(item["name"]):
            continue
        entries.append({
            "name": item["name"] + ("/" if item["type"] == "dir" else ""),
            "type": "dir" if item["type"] == "dir" else "file",
            "bytes": item.get("size") if item["type"] == "file" else None,
        })
        if len(entries) >= MAX_ENTRIES:
            break
    return {
        "source": "github",
        "repo": full,
        "path": path or ".",
        "count": len(entries),
        "entries": sorted(entries, key=lambda e: (e["type"] == "file", e["name"].lower())),
    }


def read_file(
    repo_name: str, path: str, max_chars: int = MAX_CHARS, ref: str | None = None
) -> dict:
    from .repo import _is_secret, _looks_binary

    full = _full_name(repo_name)
    name = path.rsplit("/", 1)[-1]
    if _is_secret(name):
        raise GitHubError(f"refusing to read {name} — credential-shaped filename")

    params = {"ref": ref} if ref else {}
    data = _get(f"/repos/{full}/contents/{path.strip('/')}", **params)
    if isinstance(data, list):
        raise GitHubError(f"{path} is a directory — use action='list'")
    if data.get("size", 0) > MAX_FILE_BYTES:
        raise GitHubError(f"{path} is {data['size']} bytes — too large to read")

    raw = base64.b64decode(data.get("content", ""))
    if _looks_binary(raw):
        raise GitHubError(f"{path} looks binary — this tool reads text only")

    text = raw.decode("utf-8", errors="replace")
    limit = max(200, min(int(max_chars), MAX_CHARS))
    return {
        "source": "github",
        "repo": full,
        "path": path,
        "bytes": len(raw),
        "lines": text.count("\n") + 1,
        "truncated": len(text) > limit,
        "text": text[:limit],
        "url": data.get("html_url"),
    }


def search(repo_name: str | None, query: str) -> dict:
    """GitHub code search, scoped to one repo or to everything the token sees."""
    if not query or not query.strip():
        raise GitHubError("query must not be empty")
    scope = f" repo:{_full_name(repo_name)}" if repo_name else f" user:{settings.github_owner}"
    data = _get("/search/code", q=f"{query}{scope}", per_page=min(MAX_MATCHES, 100))

    from .repo import _is_secret

    matches = []
    for item in data.get("items", []):
        if _is_secret(item["name"]):
            continue
        matches.append({
            "repo": item["repository"]["full_name"],
            "path": item["path"],
            "url": item.get("html_url"),
        })
    return {
        "source": "github",
        "query": query,
        "total": data.get("total_count", 0),
        "count": len(matches),
        "matches": matches,
    }


def log(repo_name: str, limit: int = 20) -> dict:
    full = _full_name(repo_name)
    data = _get(f"/repos/{full}/commits", per_page=max(1, min(int(limit), 100)))
    return {
        "source": "github",
        "repo": full,
        "count": len(data),
        "commits": [
            {
                "sha": c["sha"][:7],
                "author": (c.get("commit", {}).get("author") or {}).get("name"),
                "date": (c.get("commit", {}).get("author") or {}).get("date", "")[:10],
                "subject": (c.get("commit", {}).get("message") or "").split("\n")[0][:120],
            }
            for c in data
        ],
    }
