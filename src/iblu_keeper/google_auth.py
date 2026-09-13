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
import logging
import os
import threading
from typing import Sequence

from .config import settings

# OAuth scopes required by the Phase 1 tools (+ identity).
SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    # Google Chat — read spaces/messages, read memberships (to name DMs by
    # person), send messages as the user, and read/update per-space read state
    # (which messages the user has already seen).
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.users.readstate",
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
    # Drive — read Google Docs / Sheets / Slides via Drive Export so the
    # gdoc_read tool can fetch shared links the user gets in email.
    # Full drive scope (not drive.readonly) because that's what the existing
    # OAuth grant has; switching to drive.readonly would require re-consent.
    "https://www.googleapis.com/auth/drive",
    # Docs — structured editing of existing Google Docs (insertText,
    # replaceAllText, batchUpdate). Required for append/find-replace; the
    # broader drive scope alone only supports whole-file overwrite.
    "https://www.googleapis.com/auth/documents",
    # Pub/Sub — pull messages from the iblu-chat-events subscription that the
    # Workspace Events API publishes spaceReadState.updated events to. Also
    # used to (re)create Workspace Events subscriptions per space.
    "https://www.googleapis.com/auth/pubsub",
)

logger = logging.getLogger("iblu_keeper.google_auth")

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


def _save_token(creds, path: str | None = None) -> None:
    path = path or settings.google_oauth_token_file
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    os.chmod(path, 0o600)


def _load_credentials(alias: str | None = None):
    """Load saved OAuth user credentials for one account, refreshing if expired.

    `alias` selects the Google account. Each Workspace needs its own OAuth
    client — IBLU's consent screen is Internal to blanklabel.team, so the Choco
    and Deadlift accounts cannot authorise it — hence one client id/secret and
    one token file per alias. Omitting `alias` uses the primary account, which
    keeps the original single-account path byte-for-byte unchanged.
    """
    from google.auth.transport.requests import Request  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore

    account = settings.account(alias or settings.primary_alias)
    client_id = account["client_id"]
    client_secret = account["client_secret"]
    path = account["token_file"]

    if not client_id or not client_secret:
        raise CredentialsUnavailable(
            f"OAuth client not configured for account {account['alias']!r} "
            f"(set GOOGLE_ACCOUNT_{account['alias'].upper()}_CLIENT_ID and "
            f"_CLIENT_SECRET, or GOOGLE_OAUTH_* for the primary account)."
        )

    if not os.path.exists(path):
        raise CredentialsUnavailable(
            f"No saved Google token at {path} for account {account['alias']!r}. "
            f"Run `python scripts/connect_google.py --account {account['alias']}` "
            "once to authorize it."
        )

    with open(path, "r", encoding="utf-8") as fh:
        info = json.load(fh)
    # The .env client id/secret are the source of truth — OVERRIDE whatever is
    # baked into the token file. This lets a ROTATED client secret be picked up
    # by updating .env alone (refresh tokens survive secret rotation; they're
    # tied to the client_id, not the secret), without re-running consent. Using
    # setdefault here previously caused stale-secret `invalid_client` refresh
    # failures after a secret rotation.
    info["client_id"] = client_id
    info["client_secret"] = client_secret
    info.setdefault("token_uri", "https://oauth2.googleapis.com/token")

    creds = Credentials.from_authorized_user_info(info, list(SCOPES))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:  # noqa: BLE001
                logger.error("Google token refresh FAILED: %s", exc)
                raise CredentialsUnavailable(
                    f"Google token refresh failed for {account['alias']!r}: "
                    f"{exc}. If the OAuth client secret was rotated, update it "
                    "in .env; otherwise re-run "
                    f"`python scripts/connect_google.py --account {account['alias']}`."
                ) from exc
            logger.info("Google token refreshed for %s.", account["alias"])
            _save_token(creds, path)
        else:
            raise CredentialsUnavailable(
                f"Saved Google token for {account['alias']!r} is invalid and "
                "cannot be refreshed. Re-run "
                f"`python scripts/connect_google.py --account {account['alias']}`."
            )
    return creds


def auth_status() -> dict:
    """Probe whether live Google credentials currently work.

    Returns {"ok": bool, "account": str|None, "error": str|None}. Safe to call
    from the health endpoint — it triggers a token refresh if needed but does
    not raise.
    """
    if settings.dry_run:
        return {"ok": False, "account": None, "error": "DRY_RUN is enabled (mock mode)"}
    if not settings.has_google_credentials:
        return {"ok": False, "account": None,
                "error": "No OAuth client configured or token file missing"}
    try:
        creds = get_credentials()
        return {"ok": bool(creds and creds.valid),
                "account": settings.google_user_email, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "account": None, "error": str(exc)}


def get_credentials(scopes: Sequence[str] | None = None):  # noqa: ARG001
    """Return the cached single-user OAuth credentials (refreshing as needed).

    `scopes` is accepted for API symmetry but ignored — the saved token already
    carries the granted scopes. Raises CredentialsUnavailable when not yet
    authorized; callers in dry-run mode should never reach this.
    """
    return get_credentials_for(None, scopes)


# One cached credential per account alias. Keyed rather than global, so a second
# account cannot quietly hand back the first account's token — which would
# attribute one person's mail to another.
_creds_by_alias: dict[str, object] = {}


def get_credentials_for(alias: str | None, scopes: Sequence[str] | None = None):
    """Credentials for one account. `None` means the primary account."""
    key = (alias or settings.primary_alias).lower()
    with _lock:
        cached = _creds_by_alias.get(key)
        if cached is None or not getattr(cached, "valid", False):
            cached = _load_credentials(key)
            _creds_by_alias[key] = cached
            global _cached_creds
            if key == settings.primary_alias:
                _cached_creds = cached
        return cached


def build_service(
    api: str,
    version: str,
    scopes: Sequence[str] | None = None,
    account: str | None = None,
):
    """Build an authenticated googleapiclient service (e.g. ('gmail', 'v1')).

    `account` selects which Google account to act as; omitting it uses the
    primary one, so every existing caller is unaffected.
    """
    from googleapiclient.discovery import build  # type: ignore

    creds = get_credentials_for(account, scopes)
    return build(api, version, credentials=creds, cache_discovery=False)
