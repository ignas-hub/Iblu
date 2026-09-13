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
| **1** | Stateless MCP server + Streamlit dashboard. No memory. | done |
| **2** | Postgres memory layer + signal collectors + weekday quiz pings. | **live** |
| 3 | Goals & priorities context (e.g. "spend 30% of time on sales"). | schema sketched |

Phase 2 is being built to the plan in
[`docs/plans/2026-09-14-recording-v1.md`](docs/plans/2026-09-14-recording-v1.md)
— read that first for the data model, decisions and acceptance criteria.

---

## Architecture

Two components, one repo:

### 1. MCP server (Python, FastMCP)
Remote MCP server Claude connects to over HTTPS. **36 tools** organised by
service, with MCP tool annotations (`readOnlyHint` / `destructiveHint`) so
Claude.ai picks safe permission defaults automatically:

| Tool | What it does | Permission default |
|---|---|---|
| `server_health` | Reports `mode` (live/mock), `auth.ok`, `server_time`. Claude calls this when results look stale. | auto-allow |
| `chat_list_conversations` | Recent Chat spaces, sender-attributed previews | auto-allow |
| `chat_list_unread` | Spaces with new messages from others since you last read | auto-allow |
| `chat_get_messages` | Message history for a space (paginated) | auto-allow |
| `chat_send_message` | Send a message to a space | **ask** |
| `chat_draft_message` | Save a local draft (does NOT send) | auto-allow |
| `chat_mark_read` | Set `lastReadTime = now` for a space | auto-allow |
| `gmail_search` | Gmail search syntax (paginated) | auto-allow |
| `gmail_list_unread` | Newest unread mail (paginated) | auto-allow |
| `gmail_get_message` | One email body + headers | auto-allow |
| `gmail_list_attachments` | Attachments on a message | auto-allow |
| `gmail_read_attachment` | Download + extract text from PDF / DOCX / text attachments | auto-allow |
| `gmail_draft_email` | Save a Gmail draft (does NOT send) | auto-allow |
| `gmail_send_email` | Send an email immediately | **ask** |
| `gmail_reply` | Threaded reply (proper In-Reply-To / References) | auto-allow |
| `gmail_mark_read` / `gmail_mark_unread` | Toggle UNREAD label | auto-allow |
| `gdoc_read` | Fetch a Google Doc / Sheet / Slides as plain text (URL or ID) | auto-allow |
| `calendar_create_event` | Create a Calendar event | auto-allow |
| `calendar_add_label` | Attach a custom label to a Calendar event | auto-allow |
| `gdoc_create` | Create a new Google Doc | auto-allow |
| `gdoc_append` | Append text to an existing Doc | auto-allow |
| `gdoc_replace_text` | Find-and-replace inside a Doc | auto-allow |
| `gdoc_rename` / `gdoc_move` | Rename a Doc / move it between folders | auto-allow |
| `drive_list_folder` | List a Drive folder (Shared Drives supported) | auto-allow |
| `drive_create_folder` | Create a Drive folder | auto-allow |
| `drive_create_file` | Create a text-content file in Drive | auto-allow |
| `drive_upload_from_url` | Fetch a URL and save it to Drive | auto-allow |
| `drive_save_gmail_attachment` | Save an email attachment straight to Drive | auto-allow |
| `get_infra_status` | Latest infrastructure health from the Drive collector | auto-allow |
| `get_context` | Mission first, then the memory brief and the window summary — call before deciding anything | auto-allow |
| `context_log` | Store a durable memory entry (fact / decision / preference / work_log) | auto-allow |
| `context_search` | Full-text + filtered search over stored entries | auto-allow |
| `context_get_summary` | What the recorder saw in a window: signal counts, pings, work_log | auto-allow |
| `context_log_conversation` | Deprecated — use `context_log` | auto-allow |

**Every tool response is stamped** with `fetched_at` (ISO-8601 UTC) and
`request_id` (UUID), plus a `query` echo of the kwargs used — so the
caller can verify the data is fresh and the request was understood
correctly. Read tools that list results also return `next_page_token`
when more pages exist; pass it back as `page_token` for the next call.
Read tools accept `response_format="markdown"` for compact,
voice-friendly output.

Auth to Google: **single-user OAuth** — a refresh token for one account
(`ignas@blanklabel.team`), created once via a browser "Allow". No service
account and no domain-wide delegation, so the authorization can touch only that
one account. Auth from Claude to the server: **Google OAuth via FastMCP's
Google provider** (Claude.ai requires dynamic client registration).

> **Chat is behind a swappable interface** (`src/iblu_keeper/tools/chat.py`).
> It is *not yet validated* that the Google Chat API can read/send Ignas's
> personal DMs/spaces with single-user OAuth. Until then the server runs in
> **mock mode**. If the Chat API path fails, implement a new `ChatBackend`
> and return it from `get_backend()` — no other code changes.

### 2. Streamlit dashboard (command center)
Google OAuth login (restricted to one account), MCP health/status, recent Chat
messages, recent email summaries, pending drafts, and a test form.

### Dry-run / mock mode
When `DRY_RUN=true` **or** no Google credentials are present, every tool returns
deterministic fake data and never calls Google. This lets the whole system be
developed and demoed before the Google access test concludes.

---

## Voice usage

Ignas uses Claude in voice / read-aloud mode a lot (driving, walking).
The server's `instructions` field tells connected Claude clients to:

1. **Never read tool names, parameters, IDs, or JSON aloud.** The model
   speaks only the natural-language summary — "You have three unread:
   Tamara asked 25 minutes ago about the invoice…" — never "I called
   `gmail_list_unread` with `limit=3` and got back an item with id…".
2. **Use `response_format='markdown'`** on read tools so the response
   is already in a compact, voice-shaped form (with relative time
   stamps like *"25m ago"*).
3. **Call `server_health` first** if the user says the data looks
   stale or wrong, and report the result in plain English.
4. **Re-fetch on every "current state" question.** Never quote a prior
   tool result from earlier in the conversation as if it were current —
   the inbox / chat / calendar change constantly.

Sample prompts that exercise the voice flow:

> *"Read me my three most recent unread emails."*
> *"Reply to Tamara saying I'll check tonight."*
> *"What did Ante just say in our DM?"*
> *"Open the latest PandaDoc contract and summarize it."*
> *"Mark the Anthropic receipt as read."*
> *"Create a 30-minute event tomorrow at 2pm called 'Opera sync'."*

## Worked examples (JSON shape)

### Reading unread mail with pagination

```jsonc
// gmail_list_unread(limit=3)
{
  "fetched_at": "2026-06-15T10:30:00Z",
  "request_id": "8ded5dae3eef4fb4b7833ca09875bedc",
  "query": {"limit": 3, "query": null},
  "count": 3,
  "next_page_token": "10703968986170101144",
  "items": [
    {"id": "19eca0b9", "from": "Tamara Lovric <t@blanklabel.team>",
     "subject": "Invoice", "snippet": "Hi, please see attached…",
     "date": "Mon, 15 Jun 2026 10:05:00 +0000"},
    /* … */
  ]
}
```

To fetch older unread, call again with `page_token=<next_page_token>`.

### Reading attachments

```text
gmail_list_attachments(message_id="19eca0b9")
  → [{filename: "invoice.pdf", attachment_id: "AT_xy7", size_bytes: 696786, mime_type: "application/pdf"}]

gmail_read_attachment(message_id="19eca0b9", attachment_id="AT_xy7", max_chars=3000)
  → {text: "STATEMENT OF WORK NO. 36 …", filename: "invoice.pdf", truncated: false}
```

### Reading a Google Doc by URL

```text
gdoc_read(url_or_id="https://docs.google.com/document/d/1gGGvGZN…/edit")
  → {name: "Opera BLT team catch up — Notes by Gemini", text: "📝 Notes…", truncated: false}
```

### Voice-friendly markdown output

```text
gmail_list_unread(limit=3, response_format="markdown")
  →
1. **Invoice** — Tamara Lovric <t@blanklabel.team> · 25m ago
   > Hi, please see attached
   `id: 19eca0b9`
2. **Re: Proposal** — Alexan <alexan@sparkleadgo.com> · 1h ago
   > 15 min call about the bundling package?
   `id: 19eca0b9`
…
```

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
   - `http://localhost:8501/` and `https://keeper.iblugames.com/` (dashboard login)
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
Domain `iblugames.com` via Cloudflare (dashboard at `keeper.iblugames.com`, MCP
at `mcp.iblugames.com`); HTTPS via Caddy (recommended) or nginx+certbot;
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
sudo nano /etc/caddy/Caddyfile       # set real hostnames (mcp.iblugames.com / keeper.iblugames.com)
sudo systemctl reload caddy
```

### 5b. HTTPS with nginx (alternative)
```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/iblu-keeper
sudo nano /etc/nginx/sites-available/iblu-keeper     # set hostnames
sudo ln -s /etc/nginx/sites-available/iblu-keeper /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d mcp.iblugames.com -d keeper.iblugames.com
```

### 6. Cloudflare / DNS
- Add A records for `mcp.iblugames.com` and `keeper.iblugames.com` → `178.104.122.152`.
- SSL/TLS mode: **Full (strict)** (the origin has a real Let's Encrypt cert).
- If proxying (orange cloud) interferes with the cert challenge, set DNS-only
  while issuing certs, then re-enable.
- Open ports 80/443 on the box (`sudo ufw allow 80,443/tcp` if ufw is on).

### 7. Connect Claude
In claude.ai → custom connector, point at `https://mcp.iblugames.com/mcp` and supply
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

## Memory (Phase 2)

Durable memory lives in PostgreSQL 16, running as a dedicated Docker
container (`iblu-db`, volume `iblu-pgdata`, bound to `127.0.0.1:5432`).
Two stores, deliberately separate:

- **`signals`** — raw observations written by the collectors (my sent mail,
  my Chat messages, calendar changes). Evidence, never read straight into a
  summary.
- **`context_entries`** — durable facts, decisions, preferences and quiz
  answers. This is the memory surface `context_log` / `context_search` expose.

Corrections supersede rather than delete: a correcting entry sets
`superseded_by` on the row it replaces, and searches skip superseded rows.

```bash
python -m iblu_keeper.db status     # applied / pending migrations
python -m iblu_keeper.db migrate    # apply pending migrations
```

In mock mode (`DRY_RUN=true`) every context tool returns `{"status": "mock"}`
and never opens a database connection — a mock row must never reach the
database (see `DEBUG_FINDINGS.md`).

## Mission

[`docs/MISSION.md`](docs/MISSION.md) says what IBLU is for. It is the source of
truth; two copies follow it — the IBLU Claude Project instructions (pasted by
hand) and `context_brief.mission` inside the database:

```bash
python -m iblu_keeper.db seed-mission     # copy docs/MISSION.md into the DB
```

The copy is keyed by `sha256`, so re-seeding an unchanged file is free and
`get_context` reports `mission_stale=true` when the file has moved ahead of the
database — drift is visible rather than silent. The mission lives in its own
column, not inside `context_brief.content`, so a future compactor cannot
rewrite or drop it. The ping composer carries it in its system prompt, and the
four "what IBLU must become" lines lead the MCP server's connect-time
instructions.

**Change `docs/MISSION.md` first; the other two copies follow.**

## Recording (Phase 2)

IBLU records what Ignas actually did, then asks him twice a day to label it.

**Collectors** write observations to `signals`: mail he sent, Chat messages he
sent, and calendar changes. `python -m iblu_keeper.jobs.tick` runs them, driven
by `iblu-tick.timer` every 10 minutes, 07:00–19:50 Mon–Fri.

**Pings** are two tappable cards a day in the Google Chat space `iblu`. Each
asks at most three questions — the biggest attention sink (named), what
displaced a self-booked block, and the venture split — composed by an LLM over
the window's signals, with deterministic fallbacks if the API is down. Tapping
an option opens `/q/<signed-token>`, records the answer as a `work_log` entry
and shows a one-line confirmation. Replying in the thread works too.

```bash
python -m iblu_keeper.jobs.tick --dry                 # collect + compose, write nothing
python -m iblu_keeper.jobs.tick --force-ping test     # send a test card now (asks first)
```

Three invariants worth keeping:

- **`in:sent` is not "I wrote it".** Google Group traffic is filed under Sent
  for group members; the collector compares the parsed `From` address.
- **Silence is not presence.** An empty stretch is unknown, not idle — the
  calendar collector seeds its baseline silently on first run.
- **The Secretary space is never collected.** Answering a ping is not work.

## Out of scope (for now)
- Voice (handled by Claude apps, not this project)
- Any company (BT / n8n) integration — iblu-keeper is personal infrastructure
