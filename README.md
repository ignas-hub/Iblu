# iblu-keeper

A self-hosted personal AI assistant system for Ignas.

**End state:** Ignas talks to Claude (often by voice, while driving). Claude can
read/send Google Chat, read/draft/send Gmail, manage Google Calendar, remember
long-term context, know Ignas's goals, and propose actions and replies. A web
dashboard ("command center") shows incoming messages, proposed replies, and
suggested actions for review and approval.

No third-party SaaS — everything runs on Ignas's own infrastructure plus Google
Workspace plus this custom code.

## Phased plan

| Phase | Scope | Status |
|------|-------|--------|
| **1** | Stateless MCP server + Streamlit dashboard. No memory. | **this repo** |
| 2 | Postgres memory layer (log conversations, "last day" summaries). | stubs in place |
| 3 | Goals & priorities context (e.g. "spend 30% of time on sales"). | schema sketched |

Phase 1 is built so Phases 2–3 plug in without rewrites: modular structure,
swappable Chat backend, DB schema sketch (`db/schema.sql`), and `context.*`
tool stubs with final signatures.

---

## Architecture

Two components, one repo:

### 1. MCP server (Python, FastMCP)
Remote MCP server Claude connects to over HTTPS. Tools exposed:

- **Google Chat:** `chat.list_conversations`, `chat.get_messages`,
  `chat.send_message`, `chat.draft_message`
- **Gmail:** `gmail.search`, `gmail.get_message`, `gmail.draft_email`, `gmail.send_email`
- **Calendar:** `calendar.create_event`
- **Context (Phase 2 stubs):** `context.log_conversation`, `context.get_summary`

Auth to Google: **single-user OAuth** — a refresh token for one account
(`ignas@blanklabel.team`), created once via a browser "Allow". No service
account and no domain-wide delegation, so the authorization can touch only that
one account. Auth from Claude to the server: a **bearer token** (`MCP_API_KEY`).

> **Chat is behind a swappable interface** (`src/iblu_keeper/tools/chat.py`).
> It is *not yet validated* that the Google Chat API + service account can
> read/send Ignas's personal DMs/spaces. Until then the server runs in
> **mock mode**. If the Chat API path fails, implement a new `ChatBackend`
> and return it from `get_backend()` — no other code changes.

### 2. Streamlit dashboard (command center)
Google OAuth login (restricted to one account), MCP health/status, recent Chat
messages, recent email summaries, pending drafts, and a test form.

### Dry-run / mock mode
When `DRY_RUN=true` **or** no Google credentials are present, every tool returns
deterministic fake data and never calls Google. This lets the whole system be
developed and demoed before the service-account access test concludes.

---

## Repository layout

```
iblu-keeper/
├── src/iblu_keeper/
│   ├── config.py            # env-driven settings (singleton)
│   ├── google_auth.py       # single-user OAuth token (load + auto-refresh)
│   ├── server.py            # FastMCP server + bearer auth + /health
│   ├── tools/
│   │   ├── chat.py          # swappable ChatBackend (Mock / GoogleChat)
│   │   ├── gmail.py
│   │   ├── calendar.py
│   │   └── context.py       # Phase 2 stubs (stable signatures)
│   └── store/drafts.py      # local JSONL draft store (-> Postgres in Phase 2)
├── dashboard/app.py         # Streamlit command center
├── scripts/
│   ├── connect_google.py    # one-time: authorize your account (saves token.json)
│   └── test_chat_access.py  # checks whether Chat can read your spaces/DMs
├── db/schema.sql            # Phase 2 Postgres sketch (unused now)
├── db/migrations/           # Phase 2 migrations
├── deploy/                  # systemd units, Caddyfile, nginx example
├── tests/test_smoke.py      # runs in mock mode, no creds needed
├── requirements.txt / pyproject.toml
└── .env.example
```

---

## Local development

```bash
# 1. Clone and enter
git clone <this-repo-url> iblu-keeper && cd iblu-keeper

# 2. Virtualenv + install (editable, so `iblu-mcp` works)
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

# 3. Config
cp .env.example .env
python -c "import secrets; print('MCP_API_KEY=' + secrets.token_urlsafe(32))"   # paste into .env
# Leave DRY_RUN=true for now (mock mode, no Google needed).

# 4. Run the MCP server  (http://127.0.0.1:8000)
python -m iblu_keeper.server
#   health check:
curl http://127.0.0.1:8000/health

# 5. In a second terminal, run the dashboard (http://localhost:8501)
source .venv/bin/activate
streamlit run dashboard/app.py

# 6. Tests (mock mode — no credentials required)
pytest -q
```

The dashboard runs in a **dev-login** bypass until OAuth is configured; just
click "Continue (dev login)".

---

## Going live with Google (when ready)

Single-user OAuth — no service account, no domain-wide delegation.

1. **Google Cloud project** → enable the **Gmail API**, **Google Calendar API**,
   and **Google Chat API**.
2. **OAuth consent screen** → set User Type **Internal** (Workspace) so the
   sensitive Gmail/Chat scopes work without app verification.
3. **OAuth client ID** (type: **Web application**). Add redirect URIs:
   - `http://localhost:8765/` (used by `connect_google.py`)
   - `http://localhost:8501/` and `https://app.iblu.com/` (dashboard login)
4. Put the client id/secret in `.env`:
   ```
   GOOGLE_OAUTH_CLIENT_ID=...apps.googleusercontent.com
   GOOGLE_OAUTH_CLIENT_SECRET=...
   ```
5. **Authorize once** (on a machine with a browser, e.g. your Mac):
   ```bash
   python scripts/connect_google.py     # opens a browser → click Allow
   ```
   This saves `data/token.json` (your refresh token — scoped to only you).
6. Set **`DRY_RUN=false`** and restart the server. `/health` reports `"mode":"live"`.

> Chat is the open question — run `python scripts/test_chat_access.py` to confirm
> the Chat API can read your spaces/DMs. If it can't, keep `DRY_RUN=true` and swap
> in an alternative `ChatBackend` (Gmail/Calendar are unaffected).

### Dashboard login
The dashboard reuses the same OAuth client for its "Sign in with Google" login.
Set `DASHBOARD_OAUTH_CLIENT_ID` / `DASHBOARD_OAUTH_CLIENT_SECRET` (same values are
fine) and `DASHBOARD_ALLOWED_EMAIL=ignas@blanklabel.team` — logins from any other
account are rejected.

---

## Deployment — Hetzner (Ubuntu)

Target box: `ignas@178.104.122.152` (shared with other projects — not dedicated).
Domain `iblu.com` via Cloudflare; HTTPS via Caddy (recommended) or nginx+certbot;
services via systemd.

### 1. Install system packages
```bash
ssh ignas@178.104.122.152
sudo apt update
sudo apt install -y python3-venv python3-pip git
```

### 2. Get the code
```bash
sudo mkdir -p /opt/iblu-keeper && sudo chown $USER:$USER /opt/iblu-keeper
git clone <this-repo-url> /opt/iblu-keeper
cd /opt/iblu-keeper
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip && pip install -e .
```

### 3. Secrets
```bash
cp .env.example .env
nano .env          # set MCP_API_KEY, GOOGLE_OAUTH_* , DASHBOARD_OAUTH_* , DRY_RUN
chmod 600 .env

# Authorize your account once (see "Going live with Google"). You can run
# connect_google.py on your Mac and copy data/token.json up, or run it here if
# the box has a browser. The token file is your private credential:
chmod 600 data/token.json
```

### 4. systemd services
```bash
sudo cp deploy/iblu-mcp.service /etc/systemd/system/
sudo cp deploy/iblu-dashboard.service /etc/systemd/system/
# Adjust User=, WorkingDirectory=, and EnvironmentFile= inside the units if your
# paths/user differ (units assume /opt/iblu-keeper and user `ignas`).
sudo systemctl daemon-reload
sudo systemctl enable --now iblu-mcp iblu-dashboard
sudo systemctl status iblu-mcp iblu-dashboard
# Logs:
journalctl -u iblu-mcp -f
```
MCP listens on `127.0.0.1:8000`, dashboard on `127.0.0.1:8501` — both local-only;
the reverse proxy terminates TLS and exposes them.

### 5a. HTTPS with Caddy (recommended)
```bash
sudo apt install -y caddy            # see caddyserver.com for the official repo
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile       # set real hostnames (mcp.iblu.com / app.iblu.com)
sudo systemctl reload caddy
```

### 5b. HTTPS with nginx (alternative)
```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/iblu-keeper
sudo nano /etc/nginx/sites-available/iblu-keeper     # set hostnames
sudo ln -s /etc/nginx/sites-available/iblu-keeper /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d mcp.iblu.com -d app.iblu.com
```

### 6. Cloudflare / DNS
- Add A records for `mcp.iblu.com` and `app.iblu.com` → `178.104.122.152`.
- SSL/TLS mode: **Full (strict)** (the origin has a real Let's Encrypt cert).
- If proxying (orange cloud) interferes with the cert challenge, set DNS-only
  while issuing certs, then re-enable.
- Open ports 80/443 on the box (`sudo ufw allow 80,443/tcp` if ufw is on).

### 7. Connect Claude
In claude.ai → custom connector, point at `https://mcp.iblu.com/mcp` and supply
the bearer token (`MCP_API_KEY`) as the `Authorization: Bearer <token>` header.

---

## Security notes
- `.env`, `data/token.json`, and all `*.json` credential files are git-ignored.
  Never commit secrets.
- Google access is **single-user OAuth** — the token can act only as the one
  account that approved it, and carries no domain-wide power. Revoke anytime at
  https://myaccount.google.com/permissions (then re-run `connect_google.py`).
- The MCP server refuses every request without the correct bearer token
  (except `/health` and `/`).
- The dashboard only admits `DASHBOARD_ALLOWED_EMAIL`.
- Services bind to localhost; only the reverse proxy is internet-facing.

## Out of scope (for now)
- Memory/Postgres implementation (stub only)
- Voice (handled by Claude apps, not this project)
- Any company (BT / n8n) integration — iblu-keeper is personal infrastructure
