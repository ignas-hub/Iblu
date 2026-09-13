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

    # --- Multi-account (week 2) ---
    #
    # Each Google Workspace needs its OWN OAuth client: IBLU's consent screen is
    # "Internal" to blanklabel.team, so ignacio@chocoagency.com and
    # admin@deadlift.io cannot authorise it. One Cloud project + Internal client
    # per domain, one token file per alias.
    google_accounts: str = field(
        default_factory=lambda: os.getenv("GOOGLE_ACCOUNTS", "blt")
    )

    @property
    def account_aliases(self) -> tuple[str, ...]:
        return tuple(
            a.strip().lower() for a in self.google_accounts.split(",") if a.strip()
        )

    def account(self, alias: str) -> dict[str, str]:
        """Per-account OAuth settings, falling back to the primary account.

        The default alias keeps using GOOGLE_OAUTH_* so the original account
        needs no migration and no re-consent.
        """
        alias = alias.lower()
        prefix = f"GOOGLE_ACCOUNT_{alias.upper()}"
        if alias == self.primary_alias:
            return {
                "alias": alias,
                "email": self.google_user_email,
                "client_id": self.google_oauth_client_id,
                "client_secret": self.google_oauth_client_secret,
                "token_file": self.google_oauth_token_file,
            }
        return {
            "alias": alias,
            "email": os.getenv(f"{prefix}_EMAIL", ""),
            "client_id": os.getenv(f"{prefix}_CLIENT_ID", ""),
            "client_secret": os.getenv(f"{prefix}_CLIENT_SECRET", ""),
            "token_file": os.getenv(
                f"{prefix}_TOKEN_FILE", f"data/token.{alias}.json"
            ),
        }

    @property
    def primary_alias(self) -> str:
        return self.account_aliases[0] if self.account_aliases else "blt"

    def configured_accounts(self) -> list[dict[str, str]]:
        """Only accounts with a client id, a secret and a saved token."""
        out = []
        for alias in self.account_aliases:
            acct = self.account(alias)
            if acct["client_id"] and acct["client_secret"] and os.path.exists(acct["token_file"]):
                out.append(acct)
        return out

    # --- Slack workspaces (week 3) ---
    #
    # Slack scopes `search.messages` to a USER token (xoxp-) — a bot token
    # cannot see it at all — so each workspace needs its own signed-in user,
    # not an app install. One token (+ display label + venture default) per
    # workspace, keyed by alias, same shape as the Google multi-account setup
    # above but deliberately a separate list: Blank Label and Deadlift are
    # both Slack workspaces yet neither is a `google_accounts` alias.
    slack_workspaces: str = field(
        default_factory=lambda: os.getenv("SLACK_WORKSPACES", "")
    )

    @property
    def slack_aliases(self) -> tuple[str, ...]:
        return tuple(
            a.strip().lower() for a in self.slack_workspaces.split(",") if a.strip()
        )

    def slack_workspace(self, alias: str) -> dict[str, str]:
        """Per-workspace Slack settings: token, display label, venture default.

        `venture` is the workspace's own fallback (e.g. the Deadlift workspace
        IS Deadlift) — used only when `venture_hints.infer` finds no stronger
        signal in the channel/message itself.
        """
        alias = alias.lower()
        return {
            "alias": alias,
            "token": os.getenv(f"SLACK_TOKEN_{alias.upper()}", ""),
            "label": os.getenv(f"SLACK_LABEL_{alias.upper()}", alias),
            "venture": os.getenv(f"SLACK_VENTURE_{alias.upper()}", ""),
        }

    def configured_slack(self) -> list[dict[str, str]]:
        """Only workspaces whose user token is actually set."""
        return [
            ws
            for alias in self.slack_aliases
            if (ws := self.slack_workspace(alias))["token"]
        ]

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

    @property
    def github_tokens(self) -> tuple[str, ...]:
        """Every GitHub token, in order: GITHUB_TOKEN, GITHUB_TOKEN_2, _3 ...

        A fine-grained PAT is scoped to ONE resource owner, so personal repos
        and each organisation need their own token. Rather than invent a
        mapping syntax that has to be kept in step with reality, IBLU simply
        tries each token and uses whichever can see the repository.
        """
        tokens = [self.github_token]
        for n in range(2, 10):
            tokens.append(os.getenv(f"GITHUB_TOKEN_{n}", ""))
        return tuple(t for t in tokens if t.strip())

    # Ping composer (Anthropic Messages API).
    anthropic_api_key: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "")
    )
    iblu_llm_model: str = field(
        default_factory=lambda: os.getenv("IBLU_LLM_MODEL", "claude-sonnet-5")
    )

    # The mirror calendar that will hold reconstructed `blocks` — one calendar
    # on blanklabel.team covering EVERY venture (HANDOFF.md §15). Deliberately
    # not the primary: it will contain Choco and Deadlift work, and a shared
    # primary would expose one client's activity to another's colleagues.
    secretary_calendar_id: str = field(
        default_factory=lambda: os.getenv("SECRETARY_CALENDAR_ID", "")
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


class UnknownAccountError(ValueError):
    """Raised when a tool's ``account`` parameter names an unconfigured alias."""


def resolve_account(account: str | None) -> str | None:
    """Validate a tool's ``account`` parameter against ``settings.account_aliases``.

    `None` (the default everywhere) means the primary account and is returned
    unchanged — this keeps every existing call site byte-for-byte unchanged.
    A known alias is returned lower-cased. An unknown alias raises
    `UnknownAccountError` naming every valid alias, so the message is written
    once here rather than repeated at each of the tool call sites in
    `tools/` and `server.py` that accept an `account` parameter.
    """
    if account is None:
        return None
    normalized = account.strip().lower()
    valid = settings.account_aliases
    if normalized not in valid:
        raise UnknownAccountError(
            f"Unknown account {account!r}. Valid accounts: {', '.join(valid)}."
        )
    return normalized
