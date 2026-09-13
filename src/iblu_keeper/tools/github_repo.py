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
import io
import logging
import tarfile
import time
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
    return bool(settings.github_tokens)


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request(token: str, path: str, **params: Any):
    url = path if path.startswith("http") else f"{API}{path}"
    try:
        return requests.get(url, headers=_headers(token), params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise GitHubError(f"GitHub unreachable: {exc}") from exc


def _get(path: str, **params: Any) -> Any:
    """Try each configured token; return the first that can see the resource.

    A fine-grained PAT is scoped to one resource owner, so a personal token
    simply cannot see an organisation's repositories. Trying each in turn means
    the caller never has to know which token owns which project.
    """
    tokens = settings.github_tokens
    if not tokens:
        raise GitHubError(
            "no GitHub token configured — IBLU can only read repositories on "
            "this box. Add GITHUB_TOKEN (and GITHUB_TOKEN_2 etc. for "
            "organisations) to .env."
        )

    last: GitHubError | None = None
    for token in tokens:
        response = _request(token, path, **params)
        if response.status_code < 400:
            return response.json()
        if response.status_code == 401:
            last = GitHubError("GitHub rejected a token (401) — expired or revoked")
        elif response.status_code == 403:
            if response.headers.get("x-ratelimit-remaining") == "0":
                raise GitHubError("GitHub rate limit reached — try again shortly")
            last = GitHubError(
                "GitHub returned 403 — a token lacks Contents: Read on that "
                "repository, or it is outside every token's scope"
            )
        elif response.status_code == 404:
            last = GitHubError(
                f"not found with any of the {len(tokens)} configured token(s) — "
                "either it does not exist, or it belongs to an organisation "
                "that needs its own fine-grained token (GITHUB_TOKEN_2 ...)"
            )
        else:
            last = GitHubError(
                f"GitHub returned {response.status_code}: {response.text[:200]}"
            )
    raise last or GitHubError("GitHub request failed")


def _full_name(repo_name: str) -> str:
    """'machina' -> 'ignas-hub/machina'; 'org/thing' passes through."""
    return repo_name if "/" in repo_name else f"{settings.github_owner}/{repo_name}"


def list_repos(limit: int = 100) -> dict:
    """Every repository ANY configured token can see, newest push first.

    Merged across tokens and de-duplicated, so personal and organisation repos
    appear in one list.
    """
    seen: dict[str, dict] = {}
    errors: list[str] = []
    for token in settings.github_tokens:
        response = _request(
            token, "/user/repos", per_page=min(limit, 100), sort="pushed",
            affiliation="owner,collaborator,organization_member",
        )
        if response.status_code >= 400:
            errors.append(f"{response.status_code}")
            continue
        for r in response.json():
            seen.setdefault(r["full_name"], r)

    data = sorted(seen.values(), key=lambda r: r.get("pushed_at") or "", reverse=True)
    if not data and errors:
        raise GitHubError(f"no repositories readable (token errors: {', '.join(errors)})")
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


# Extensions worth grepping. Everything else is skipped so a search does not
# spend its request budget on lockfiles and images.
TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".sql", ".md", ".txt",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".json", ".html", ".css",
    ".rb", ".go", ".rs", ".java", ".php", ".prisma", ".env.example", "Dockerfile",
}
SKIP_PATH_PARTS = {
    "node_modules", ".venv", "venv", "dist", "build", ".next", "__pycache__",
    "vendor", "migrations/versions",
}
MAX_FILES_SCANNED = 250
MAX_SEARCH_BYTES = 400_000


def _tree(repo_full: str, ref: str | None = None) -> list[dict]:
    """Every file path in a repository — one request, not one per file."""
    if ref is None:
        ref = _get(f"/repos/{repo_full}").get("default_branch", "main")
    data = _get(f"/repos/{repo_full}/git/trees/{ref}", recursive="1")
    return [x for x in data.get("tree", []) if x.get("type") == "blob"]


def _searchable(path: str, size: int) -> bool:
    if size > MAX_SEARCH_BYTES:
        return False
    if any(part in SKIP_PATH_PARTS for part in path.split("/")):
        return False
    name = path.rsplit("/", 1)[-1]
    from .repo import _is_secret

    if _is_secret(name):
        return False
    return name in TEXT_SUFFIXES or any(name.endswith(sfx) for sfx in TEXT_SUFFIXES)


# A repo's text files, cached briefly. Searching by fetching each blob cost ~50s
# for four repositories; the tarball is ONE request, so the same search is a
# couple of seconds and a repeat search is instant.
_CACHE: dict[str, tuple[float, list[tuple[str, str]]]] = {}
CACHE_TTL = 300
MAX_TARBALL_BYTES = 60_000_000


def _repo_text(repo_full: str, ref: str | None = None) -> list[tuple[str, str]]:
    """`[(path, text)]` for every searchable file, via one tarball request."""
    key = f"{repo_full}@{ref or 'default'}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    path = f"/repos/{repo_full}/tarball" + (f"/{ref}" if ref else "")
    blob: bytes | None = None
    for token in settings.github_tokens:
        response = _request(token, path)
        if response.status_code < 400:
            blob = response.content
            break
    if blob is None:
        raise GitHubError(f"could not download {repo_full}")
    if len(blob) > MAX_TARBALL_BYTES:
        raise GitHubError(f"{repo_full} is too large to search ({len(blob)} bytes)")

    from .repo import _looks_binary

    files: list[tuple[str, str]] = []
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile() or member.size > MAX_SEARCH_BYTES:
                continue
            # The tarball root is "<owner>-<repo>-<sha>/"; strip it.
            rel = member.name.split("/", 1)[1] if "/" in member.name else member.name
            if not _searchable(rel, member.size):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            raw = handle.read()
            if _looks_binary(raw):
                continue
            files.append((rel, raw.decode("utf-8", errors="replace")))

    _CACHE[key] = (time.time(), files)
    return files


def search(repo_name: str | None, query: str, ref: str | None = None) -> dict:
    """Find text across one repository or all of them.

    GitHub's /search/code endpoint returns nothing for fine-grained tokens, so
    IBLU greps the repositories itself — downloading each as a single tarball
    rather than fetching files one at a time, and caching the result for a few
    minutes so follow-up searches are instant.
    """
    if not query or not query.strip():
        raise GitHubError("query must not be empty")
    needle = query.lower()

    targets = [_full_name(repo_name)] if repo_name else [
        r["name"] for r in list_repos()["repos"]
    ]

    matches: list[dict] = []
    scanned = 0
    truncated = False

    for full in targets:
        if len(matches) >= MAX_MATCHES:
            truncated = True
            break
        try:
            files = _repo_text(full, ref if repo_name else None)
        except GitHubError as exc:
            logger.warning("search: skipping %s: %s", full, exc)
            continue

        for path, text in files:
            scanned += 1
            for number, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    matches.append({
                        "repo": full, "path": path, "line": number,
                        "text": line.strip()[:200],
                    })
                    break  # one hit per file keeps the answer readable
            if len(matches) >= MAX_MATCHES:
                truncated = True
                break

    return {
        "source": "github",
        "query": query,
        "searched_repos": len(targets),
        "files_scanned": scanned,
        "truncated": truncated,
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
