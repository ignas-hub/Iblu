"""Who is X, and what is their email — the Workspace directory, read-only.

IBLU could read Ignas's mail, chat and calendar, and could not answer "what's
Marta's email" without him opening the admin console or guessing from an old
thread. The token has held `directory.readonly` since the Chat collector
needed it to turn `users/<id>` into a display name; nothing exposed it as a
question he could ask. He said it plainly: "It's crazy how many times I have
to ask to add tools." This is that question, answered. `sheets_read.py` and
`sheets_manage.py` are the same batch for Sheets.

One read-only MCP tool for the directory domain, per HANDOFF.md §12 (one tool
per domain x side-effect class). There is no write side: the directory is
the admin console's to change, not this server's.

Scope, deliberately narrow. `people().searchDirectoryPeople` with
`DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE` finds people *in the account's
Workspace* — coworkers, with their title, department and phone as the admin
entered them. It does not search personal contacts: that needs the `contacts`
scope, which the token does not hold and is not going to. A contact list is
the one address book Ignas curates by hand, and a tool that can read it is one
consent screen away from a tool that can rewrite it. The directory is
authoritative and somebody else's to maintain, which is why reading it is safe.

In dry-run mode this returns a mock dict with `status` `not_searched_mock`
and no network call is made.
"""

from __future__ import annotations

import logging

from ..config import resolve_account, settings
from . import drive as drive_tools

logger = logging.getLogger("iblu_keeper.tools.directory")

_READ_MASK = "names,emailAddresses,organizations,phoneNumbers,photos"
_SOURCE = "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"
_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50


def _mock(payload: dict) -> dict:
    return {
        "_mock": True, "note": "MOCK MODE — no People API call made.",
        "status": "not_searched_mock", **payload,
    }


def _people(account: str | None = None):
    from ..google_auth import build_service

    return build_service("people", "v1", account=account)


def _primary_first(items: list[dict] | None) -> list[dict]:
    """The entries with `metadata.primary` first, original order otherwise."""
    items = items or []
    return sorted(items, key=lambda it: not (it.get("metadata") or {}).get("primary", False))


def _flatten(person: dict) -> dict:
    names = _primary_first(person.get("names"))
    emails = _primary_first(person.get("emailAddresses"))
    orgs = _primary_first(person.get("organizations"))
    phones = _primary_first(person.get("phoneNumbers"))
    photos = [p for p in (person.get("photos") or []) if not p.get("default")]
    org = orgs[0] if orgs else {}
    return {
        "name": (names[0].get("displayName") if names else None) or None,
        "email": (emails[0].get("value") if emails else None) or None,
        "title": org.get("title") or None,
        "department": org.get("department") or None,
        "phone": (phones[0].get("value") if phones else None) or None,
        "photo_url": (photos[0].get("url") if photos else None) or None,
        "resource_name": person.get("resourceName"),
    }


def directory_search(
    query: str,
    limit: int = _DEFAULT_LIMIT,
    account: str | None = None,
) -> dict:
    """Find people in the account's Workspace directory by name or email.

    ``query`` is matched by Google against names, emails and the other
    directory fields ("marta", "m.kowal", "finance"). ``limit`` is 1-50.
    Returns ``people`` as ``{name, email, title, department, phone,
    photo_url, resource_name}``. Read-only.

    Personal contacts are not searched: that needs the ``contacts`` scope,
    which the token deliberately does not hold (see the module docstring).
    ``account`` — whose Workspace: ``blt`` (default), ``deadlift``, ``choco``.
    """
    account = resolve_account(account)
    if not query or not str(query).strip():
        raise ValueError("directory_search requires a non-empty `query`.")
    q = str(query).strip()
    n = _DEFAULT_LIMIT if limit is None else max(1, min(int(limit), _MAX_LIMIT))

    if settings.use_mock:
        return _mock({"query": q, "people": [], "count": 0})

    service = _people(account)
    with drive_tools.friendly_google_errors(account, "the Workspace directory"):
        resp = service.people().searchDirectoryPeople(
            query=q, readMask=_READ_MASK, sources=[_SOURCE], pageSize=n,
        ).execute()

    people = [_flatten(p) for p in resp.get("people", []) or []]
    return {
        "query": q,
        "people": people,
        "count": len(people),
        "limit": n,
        "status": "searched",
    }
