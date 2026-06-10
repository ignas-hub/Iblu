"""Central configuration.

All runtime config comes from environment variables (loaded from a local
`.env` during development via python-dotenv). Nothing secret is ever hard-coded
or committed. Import `settings` anywhere you need configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

try:  # python-dotenv is optional at runtime (systemd injects env directly)
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv not installed / no .env present
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- MCP server ---
    mcp_host: str = field(default_factory=lambda: os.getenv("MCP_HOST", "127.0.0.1"))
    mcp_port: int = field(default_factory=lambda: int(os.getenv("MCP_PORT", "8000")))
    mcp_api_key: str = field(default_factory=lambda: os.getenv("MCP_API_KEY", ""))

    # When DRY_RUN is true (or no Google credentials are present) the tools
    # return mock data and never touch Google APIs. This lets the whole system
    # be developed before the service-account access test concludes.
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))

    # --- Google OAuth (single-user) ---
    # The assistant accesses ONLY this user's account via an OAuth refresh
    # token (no service account, no domain-wide delegation). The token is
    # created once via scripts/connect_google.py and stored at the token file.
    google_oauth_client_id: str = field(
        default_factory=lambda: os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    )
    google_oauth_client_secret: str = field(
        default_factory=lambda: os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    )
    google_oauth_token_file: str = field(
        default_factory=lambda: os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "data/token.json")
    )
    # Informational only — the OAuth token itself determines the account.
    google_user_email: str = field(
        default_factory=lambda: os.getenv("GOOGLE_USER_EMAIL", "ignas@blanklabel.team")
    )

    # --- Dashboard ---
    dashboard_oauth_client_id: str = field(
        default_factory=lambda: os.getenv("DASHBOARD_OAUTH_CLIENT_ID", "")
    )
    dashboard_oauth_client_secret: str = field(
        default_factory=lambda: os.getenv("DASHBOARD_OAUTH_CLIENT_SECRET", "")
    )
    dashboard_oauth_redirect_uri: str = field(
        default_factory=lambda: os.getenv(
            "DASHBOARD_OAUTH_REDIRECT_URI", "http://localhost:8501/"
        )
    )
    dashboard_allowed_email: str = field(
        default_factory=lambda: os.getenv(
            "DASHBOARD_ALLOWED_EMAIL", "ignas@blanklabel.team"
        )
    )
    dashboard_cookie_secret: str = field(
        default_factory=lambda: os.getenv("DASHBOARD_COOKIE_SECRET", "")
    )
    dashboard_mcp_base_url: str = field(
        default_factory=lambda: os.getenv(
            "DASHBOARD_MCP_BASE_URL", "http://127.0.0.1:8000"
        )
    )

    # --- Phase 2 (unused in Phase 1) ---
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))

    @property
    def has_google_credentials(self) -> bool:
        """True when the OAuth client is configured AND a saved token exists."""
        return bool(
            self.google_oauth_client_id
            and self.google_oauth_client_secret
            and os.path.exists(self.google_oauth_token_file)
        )

    @property
    def use_mock(self) -> bool:
        """Tools should return mock data when dry-run is on OR creds are missing."""
        return self.dry_run or not self.has_google_credentials


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# Convenient module-level singleton.
settings = get_settings()
