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
    # Public HTTPS URL where this MCP server is reachable (used as the OAuth
    # base_url so Google can redirect back after sign-in).
    mcp_public_base_url: str = field(
        default_factory=lambda: os.getenv("MCP_PUBLIC_BASE_URL", "")
    )

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
            "DASHBOARD_OAUTH_REDIRECT_URI", "https://keeper.iblugames.com/"
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

    # --- Infrastructure status collector (read-only Drive fetch) ---
    # A folder in Drive that holds the collector's ``latest.md`` and
    # ``latest.json`` outputs; the ``get_infra_status`` tool downloads them
    # and derives a compact status summary.
    infra_folder_id: str = field(
        default_factory=lambda: os.getenv("INFRA_FOLDER_ID", "")
    )
    # IANA name (e.g. "Europe/Zagreb", "UTC"). Reported in the tool's
    # ``collected_at`` field so callers know which timezone the collector runs in.
    infra_hub_timezone: str = field(
        default_factory=lambda: os.getenv("INFRA_HUB_TIMEZONE", "UTC")
    )

    # --- Phase 2: recording v1 (docs/plans/2026-09-14-recording-v1.md) ---
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))

    # Local time zone for every scheduling decision. Storage stays UTC.
    iblu_timezone: str = field(
        default_factory=lambda: os.getenv("IBLU_TIMEZONE", "Europe/Zagreb")
    )

    # GitHub read-only access, so the repo tool can reach repositories that
    # do not live on this box. Fine-grained token, Contents+Metadata read only.
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""))
    github_owner: str = field(
        default_factory=lambda: os.getenv("GITHUB_OWNER", "ignas-hub")
    )

    # Ping composer (Anthropic Messages API).
    anthropic_api_key: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "")
    )
    iblu_llm_model: str = field(
        default_factory=lambda: os.getenv("IBLU_LLM_MODEL", "claude-sonnet-5")
    )

    # Secretary Chat space: webhook posts the card, space id reads the replies.
    secretary_webhook_url: str = field(
        default_factory=lambda: os.getenv("SECRETARY_WEBHOOK_URL", "")
    )
    secretary_space: str = field(
        default_factory=lambda: os.getenv("SECRETARY_SPACE", "")
    )

    # Tap-link signing. Without it the /q endpoint refuses every token.
    ping_signing_secret: str = field(
        default_factory=lambda: os.getenv("PING_SIGNING_SECRET", "")
    )

    # Ping scheduling. Default OFF so a half-configured box never pings.
    ping_enabled: bool = field(default_factory=lambda: _bool("PING_ENABLED", False))
    ping_days: str = field(
        default_factory=lambda: os.getenv("PING_DAYS", "MON,TUE,WED,THU,FRI")
    )
    ping_midday: str = field(
        default_factory=lambda: os.getenv("PING_MIDDAY", "12:30-14:00")
    )
    ping_evening: str = field(
        default_factory=lambda: os.getenv("PING_EVENING", "17:00-18:30")
    )

    @property
    def ping_day_set(self) -> frozenset[str]:
        """PING_DAYS parsed into upper-case three-letter day codes."""
        return frozenset(
            d.strip().upper()[:3] for d in self.ping_days.split(",") if d.strip()
        )

    @property
    def can_ping(self) -> bool:
        """True only when every piece needed to send and record a ping exists."""
        return bool(
            self.ping_enabled
            and self.secretary_webhook_url
            and self.ping_signing_secret
            and self.mcp_public_base_url
        )

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
        """Return mock data ONLY when dry-run is explicitly enabled.

        IMPORTANT: mock mode must be an INTENTIONAL choice (DRY_RUN=true), never
        a silent fallback. Previously this also returned True when credentials
        were missing, which caused a misconfigured *production* server to
        silently serve stale fake data and report writes as succeeding. Now, if
        DRY_RUN is false but credentials are missing/invalid, the live path runs
        and fails loudly (CredentialsUnavailable) instead of lying.
        """
        return self.dry_run

    @property
    def misconfigured_live(self) -> bool:
        """True when we intend to be live (DRY_RUN=false) but have no token."""
        return not self.dry_run and not self.has_google_credentials


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# Convenient module-level singleton.
settings = get_settings()
