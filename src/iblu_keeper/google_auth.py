"""Google OAuth (single-user) credential handling.

The assistant accesses ONLY Ignas's account using a standard OAuth refresh
token — no service account and no domain-wide delegation. This means the
authorization is genuinely scoped to one account and can touch no one else.

The refresh token is created once via `scripts/connect_google.py` (you click
"Allow" in a browser) and stored as JSON at `GOOGLE_OAUTH_TOKEN_FILE`. From then
on the server refreshes short-lived access tokens automatically; the saved file
is updated in place when the token is refreshed.

Credentials are built lazily and cached, so importing this module never fails
just because no token exists yet (important for dry-run development).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Sequence

from .config import settings

# OAuth scopes required by the Phase 1 tools (+ identity).
SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    # Google Chat — read spaces/messages, read memberships (to name DMs by
    # person), and send messages as the user.
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/chat.messages",
    # Gmail — read/draft (modify) and send.
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    # Calendar — create/manage events.
    "https://www.googleapis.com/auth/calendar.events",
    # People API — resolve Chat user IDs (users/<id>) to display names via the
    # Workspace directory. Chat API never returns displayName under user OAuth,
    # so we need a separate lookup. Domain coworkers only; external users may
    # still return empty.
    "https://www.googleapis.com/auth/directory.readonly",
)

_lock = threading.Lock()
_cached_creds = None


class CredentialsUnavailable(RuntimeError):
    """Raised when a Google client is requested but no OAuth token is available."""


def _client_config() -> dict:
    """OAuth client config in the shape google-auth-oauthlib expects."""
    return {
        "installed": {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def _save_token(creds) -> None:
    path = settings.google_oauth_token_file
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    os.chmod(path, 0o600)


def _load_credentials():
    """Load saved OAuth user credentials, refreshing if expired."""
    from google.auth.transport.requests import Request  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore

    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise CredentialsUnavailable(
            "OAuth client not configured "
            "(set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET)."
        )

    path = settings.google_oauth_token_file
    if not os.path.exists(path):
        raise CredentialsUnavailable(
            f"No saved Google token at {path}. "
            "Run `python scripts/connect_google.py` once to authorize your account."
        )

    with open(path, "r", encoding="utf-8") as fh:
        info = json.load(fh)
    # Ensure client id/secret are present so refresh works even if the saved
    # token file omitted them.
    info.setdefault("client_id", settings.google_oauth_client_id)
    info.setdefault("client_secret", settings.google_oauth_client_secret)
    info.setdefault("token_uri", "https://oauth2.googleapis.com/token")

    creds = Credentials.from_authorized_user_info(info, list(SCOPES))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
        else:
            raise CredentialsUnavailable(
                "Saved Google token is invalid and cannot be refreshed. "
                "Re-run `python scripts/connect_google.py`."
            )
    return creds


def get_credentials(scopes: Sequence[str] | None = None):  # noqa: ARG001
    """Return the cached single-user OAuth credentials (refreshing as needed).

    `scopes` is accepted for API symmetry but ignored — the saved token already
    carries the granted scopes. Raises CredentialsUnavailable when not yet
    authorized; callers in dry-run mode should never reach this.
    """
    global _cached_creds
    with _lock:
        if _cached_creds is None or not getattr(_cached_creds, "valid", False):
            _cached_creds = _load_credentials()
        return _cached_creds


def build_service(api: str, version: str, scopes: Sequence[str] | None = None):
    """Build an authenticated googleapiclient service (e.g. ('gmail', 'v1'))."""
    from googleapiclient.discovery import build  # type: ignore

    creds = get_credentials(scopes)
    return build(api, version, credentials=creds, cache_discovery=False)
