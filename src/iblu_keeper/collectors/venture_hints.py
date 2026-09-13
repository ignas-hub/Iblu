"""Venture / project inference rules.

Deliberately a plain data table, not code: Ignas extends this file as new
clients and projects appear, and nothing else has to change. Everything
inferred here is written with `venture_confidence='inferred'` (plan §8.4) —
only Ignas's own answers ever count as 'fact'.

Matching order (first hit wins):
  1. exact email domain            ignas@deadlift.io        -> deadlift
  2. domain -> project             machina.deadlift.io      -> project 'machina'
  3. keyword in space/subject text "Opera weekly"           -> choco
  4. default for the account       ignas@blanklabel.team    -> blt
"""

from __future__ import annotations

import re

# Email domain -> venture code.
DOMAINS: dict[str, str] = {
    "deadlift.io": "deadlift",
    "chocoagency.com": "choco",
}

# Host / subdomain -> project (free text).
DOMAIN_PROJECTS: dict[str, str] = {
    "machina.deadlift.io": "machina",
}

# Lower-cased keyword found in a space name, subject or title -> venture code.
SPACE_KEYWORDS: dict[str, str] = {
    "opera": "choco",
    "choco": "choco",
    "deadlift": "deadlift",
    "machina": "deadlift",
    "jakusi": "jakusi",
    "radovi": "jakusi",
}

# Lower-cased keyword -> project (free text).
KEYWORD_PROJECTS: dict[str, str] = {
    "machina": "machina",
}

# Fallback when nothing else matches: whose mailbox produced the signal.
DEFAULT_BY_ACCOUNT: dict[str, str] = {
    "ignas@blanklabel.team": "blt",
    "ignacio@chocoagency.com": "choco",
    "admin@deadlift.io": "deadlift",
}

_EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+\.[\w.-]+)")


def _domains_in(text: str) -> list[str]:
    return [m.lower() for m in _EMAIL_RE.findall(text or "")]


def infer(
    account: str,
    counterpart: str | None = None,
    subject: str | None = None,
    text: str | None = None,
) -> tuple[str | None, str | None]:
    """Return `(venture, project)` — either may be None.

    `counterpart` is an email address, person name or space name; `subject` is
    the email subject / space display name / event title; `text` is the snippet.
    """
    haystack = " ".join(p for p in (counterpart, subject, text) if p)
    lowered = haystack.lower()

    venture: str | None = None
    project: str | None = None

    # 1 + 2 — email domains are the strongest signal we have.
    for domain in _domains_in(haystack):
        if domain in DOMAIN_PROJECTS:
            project = DOMAIN_PROJECTS[domain]
        # Match the domain and any parent of it (mail.deadlift.io -> deadlift.io).
        parts = domain.split(".")
        for i in range(len(parts) - 1):
            candidate = ".".join(parts[i:])
            if candidate in DOMAINS:
                venture = DOMAINS[candidate]
                break
        if venture:
            break

    # 3 — keyword match on the human-readable bits.
    if venture is None:
        for keyword, code in SPACE_KEYWORDS.items():
            if keyword in lowered:
                venture = code
                break
    if project is None:
        for keyword, proj in KEYWORD_PROJECTS.items():
            if keyword in lowered:
                project = proj
                break

    # 4 — fall back to whose account this came from.
    if venture is None:
        venture = DEFAULT_BY_ACCOUNT.get(account)

    return venture, project
