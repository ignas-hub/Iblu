"""Google service-account credential loading.

The service-account key is loaded from an env var — either a file path
(`GOOGLE_SERVICE_ACCOUNT_FILE`) or a base64 blob (`GOOGLE_SERVICE_ACCOUNT_B64`).

Access is scoped to a single Workspace user via domain-wide delegation
(impersonation of `GOOGLE_DELEGATED_USER`, default ignas@blanklabel.team).
Delegation is intentionally NOT used broadly — only this one user.

Credentials are built lazily and cached, so importing this module never fails
just because credentials are absent (important for dry-run development).
"""

from __future__ import annotations

import base64
import json
from functools import lru_cache
from typing import Sequence

from .config import settings

# OAuth scopes required by the Phase 1 tools.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
)


class CredentialsUnavailable(RuntimeError):
    """Raised when a Google client is requested but no credentials are configured."""


def _load_service_account_info() -> dict:
    """Return the parsed service-account key dict, or raise CredentialsUnavailable."""
    if settings.google_sa_b64:
        try:
            raw = base64.b64decode(settings.google_sa_b64)
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise CredentialsUnavailable(
                f"GOOGLE_SERVICE_ACCOUNT_B64 could not be decoded: {exc}"
            ) from exc

    if settings.google_sa_file:
        try:
            with open(settings.google_sa_file, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError as exc:
            raise CredentialsUnavailable(
                f"Service-account file not found: {settings.google_sa_file}"
            ) from exc

    raise CredentialsUnavailable(
        "No Google service-account credentials configured "
        "(set GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_B64)."
    )


@lru_cache(maxsize=8)
def _delegated_credentials(scopes: tuple[str, ...], subject: str):
    """Build (and cache) delegated service-account credentials."""
    # Imported lazily so the package imports fine without google libs installed.
    from google.oauth2 import service_account  # type: ignore

    info = _load_service_account_info()
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=list(scopes)
    )
    # Impersonate the single allowed user.
    return creds.with_subject(subject)


def get_credentials(scopes: Sequence[str] | None = None, subject: str | None = None):
    """Return delegated credentials impersonating the configured user.

    Raises CredentialsUnavailable when nothing is configured — callers in
    dry-run mode should never reach this.
    """
    scope_tuple = tuple(scopes) if scopes else SCOPES
    user = subject or settings.google_delegated_user
    return _delegated_credentials(scope_tuple, user)


def build_service(api: str, version: str, scopes: Sequence[str] | None = None):
    """Build an authenticated googleapiclient service (e.g. ('gmail', 'v1'))."""
    from googleapiclient.discovery import build  # type: ignore

    creds = get_credentials(scopes)
    return build(api, version, credentials=creds, cache_discovery=False)
