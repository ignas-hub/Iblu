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

Auth to Google: a **service account** key (from env, file path or base64),
impersonating the single user `ignas@blanklabel.team`. Auth from Claude to the
server: a **bearer token** (`MCP_API_KEY`).

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
│   ├── google_auth.py       # service-account creds (file or base64), delegated to 1 user
│   ├── server.py            # FastMCP server + bearer auth + /health
│   ├── tools/
│   │   ├── chat.py          # swappable ChatBackend (Mock / GoogleChat)
│   │   ├── gmail.py
│   │   ├── calendar.py
│   │   └── context.py       # Phase 2 stubs (stable signatures)
│   └── store/drafts.py      # local JSONL draft store (-> Postgres in Phase 2)
├── dashboard/app.py         # Streamlit command center
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

1. **Google Cloud project** → enable the **Gmail API**, **Google Calendar API**,
   and **Google Chat API**.
2. **Service account** → create one, generate a JSON key.
3. **Domain-wide delegation** (scoped to one user): in the Workspace Admin
   console, authorize the service account's client ID for exactly these scopes:
   ```
   https://www.googleapis.com/auth/chat.spaces.readonly
   https://www.googleapis.com/auth/chat.messages
   https://www.googleapis.com/auth/gmail.modify
   https://www.googleapis.com/auth/gmail.send
   https://www.googleapis.com/auth/calendar.events
   ```
4. In `.env`, set `GOOGLE_SERVICE_ACCOUNT_FILE` (or `GOOGLE_SERVICE_ACCOUNT_B64`),
   confirm `GOOGLE_DELEGATED_USER=ignas@blanklabel.team`, and set **`DRY_RUN=false`**.
5. Restart the server. `/health` should now report `"mode":"live"`.

> Gmail + Calendar via service account is a known-good pattern. Chat is the open
> question — test `chat.list_conversations` first. If it can't reach personal
> DMs/spaces, keep `DRY_RUN=true` (or force the mock Chat backend) and swap in an
> alternative `ChatBackend` later.

### Dashboard Google OAuth (lock to one account)
1. Google Cloud → **OAuth consent screen** (Internal) + **OAuth client ID**
   (type: Web application).
2. Authorized redirect URI = your `DASHBOARD_OAUTH_REDIRECT_URI`
   (e.g. `https://app.iblu.com/`).
3. Put the client id/secret and `DASHBOARD_ALLOWED_EMAIL=ignas@blanklabel.team`
   in `.env`. Logins from any other account are rejected.

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
nano .env          # set MCP_API_KEY, Google creds, OAuth, DRY_RUN
chmod 600 .env

# Place the service-account key where .env points (default below):
sudo mkdir -p /etc/iblu-keeper
sudo cp service-account.json /etc/iblu-keeper/service-account.json
sudo chmod 600 /etc/iblu-keeper/service-account.json
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
- `.env` and all `*.json` key files are git-ignored. Never commit secrets.
- The MCP server refuses every request without the correct bearer token
  (except `/health` and `/`).
- The dashboard only admits `DASHBOARD_ALLOWED_EMAIL`.
- Services bind to localhost; only the reverse proxy is internet-facing.

## Out of scope (for now)
- Memory/Postgres implementation (stub only)
- Voice (handled by Claude apps, not this project)
- Any company (BT / n8n) integration — iblu-keeper is personal infrastructure
