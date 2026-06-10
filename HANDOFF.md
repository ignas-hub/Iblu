# iblu-keeper — Handoff (Session 1)

_Last updated: 2026-06-10_

This document captures the state of **iblu-keeper** after the first build
session: what exists, what's been validated against the real Google account,
the decisions made, and exactly what's left to do. It's written so both Ignas
(non-developer) and any future Claude/developer session can pick up cleanly.

---

## 1. What iblu-keeper is

A self-hosted personal AI assistant for Ignas. Claude connects to a custom
**MCP server** that can read/send Google Chat, read/draft/send Gmail, and create
Calendar events. A **Streamlit dashboard** ("command center") is the review UI.
Full vision and phased plan are in `README.md`.

This session delivered **Phase 1** (stateless: no memory yet).

---

## 2. Current status — ✅ working & validated

All three integrations were tested **against Ignas's real `ignas@blanklabel.team`
account** and confirmed working:

| Capability | Status | Notes |
|---|---|---|
| **Gmail** | ✅ Working | Read inbox, get message, draft, send |
| **Calendar** | ✅ Working | Read & create events on primary calendar |
| **Google Chat** | ✅ Working | Lists 100 spaces, reads & sends messages |
| MCP server (FastMCP) | ✅ Working | HTTP, bearer-token auth, `/health` endpoint |
| Dashboard (Streamlit) | ✅ Built | Google login, health/status, test forms |
| Mock / dry-run mode | ✅ Working | Runs with fake data when `DRY_RUN=true` |
| Smoke tests | ✅ 10/10 pass | `pytest -q` (mock mode, no creds) |

Code is on the **`main`** branch of `ignas-hub/Iblu` (also on
`claude/new-session-1mbyla`).

---

## 3. Key decisions made this session

1. **Auth: single-user OAuth, NOT service account + domain-wide delegation.**
   Per a security review, we avoided downloadable service-account keys and
   domain-wide delegation. The assistant now uses a normal OAuth **refresh
   token** that can act **only** as the one account that approved it — no
   domain-wide power, no Workspace-admin dependency.
   - Implemented in `src/iblu_keeper/google_auth.py`.
   - Created once via a browser "Allow"; token stored at `data/token.json`.

2. **Domain: `iblugames.com`.**
   - Dashboard → `keeper.iblugames.com`
   - MCP server → `mcp.iblugames.com`
   - OAuth redirect URI registered: `https://keeper.iblugames.com/`

3. **Google Chat requires an app "Configuration"** (Cloud console → Chat API →
   Configuration) even for personal read access. This was completed; before it,
   the API returned "Chat app not found." This was the handover's "open
   question" — now resolved: **Chat works via user OAuth + configured app.**

---

## 4. Google Cloud setup (done)

- **Project:** `iblu-keeper`
- **APIs enabled:** Gmail, Calendar, Google Chat, (People API still OFF — see §6)
- **OAuth consent screen:** Internal
- **OAuth client (Web):** Client ID `751405989189-shlh26ll...apps.googleusercontent.com`
  - Redirect URIs include `https://keeper.iblugames.com/`
- **Chat app:** configured (name, avatar, functionality, placeholder endpoint)
- **Granted scopes:** userinfo.email, openid, chat.spaces.readonly,
  chat.memberships.readonly, chat.messages, gmail.modify, gmail.send,
  calendar.events (plus drive & gmail.readonly that the account already had).

> 🔐 **Secrets handling:** The OAuth **client secret** and the **refresh token**
> were handled during setup. The client secret was shared in chat during setup —
> **recommend rotating it** (Cloud console → Credentials → the client →
> *Reset secret*) before/at production, then update `.env`.

---

## 5. ⚠️ Important: the token is NOT yet on a server

The working `data/token.json` was generated in a **temporary cloud workspace**
that is ephemeral. It is **git-ignored** (never committed). Before the assistant
can run 24/7, the token must exist on the real server. Two options:
- **Re-run the consent on the server** once deployed (cleanest), or
- Copy a freshly generated `token.json` to the server at deploy time.

This is handled as part of deployment (§7).

---

## 6. Known limitations / open items

1. **DM names show as IDs.** Google's Chat API returns only `users/<id>` (no
   display name) for human members of 1:1 and group DMs via user OAuth.
   *Named spaces are unaffected.* To resolve DM people→names later:
   enable the **People API** (or Admin Directory API) on the project and add a
   `directory.readonly` scope, then map IDs→names. Tracked as a future
   enhancement; `GoogleChatBackend` already degrades gracefully.

2. **Token portability** — see §5.

3. **Client secret rotation** — see §4 note.

4. **Phase 2 (memory) & Phase 3 (goals)** — not built. Stubs and DB schema
   sketch are in place (`tools/context.py`, `db/schema.sql`).

---

## 7. What's left to do — DEPLOYMENT (next session)

Goal: get the MCP server + dashboard running on Ignas's **Hetzner** box
(`ignas@178.104.122.152`) behind HTTPS so Claude can connect 24/7.

Full step-by-step is in **`README.md` → "Deployment — Hetzner"**. In short:

1. **DNS (Cloudflare):** point `keeper.iblugames.com` and `mcp.iblugames.com`
   A-records at `178.104.122.152`.
2. **On the server:** clone repo to `/opt/iblu-keeper`, create venv,
   `pip install -e .`.
3. **Config:** create `.env` (set `MCP_API_KEY`, `GOOGLE_OAUTH_CLIENT_ID/SECRET`,
   dashboard OAuth, `DRY_RUN=false`). `chmod 600 .env`.
4. **Token:** put a valid `data/token.json` on the box (re-run consent or copy).
5. **systemd:** enable `iblu-mcp` and `iblu-dashboard` services
   (`deploy/*.service`).
6. **HTTPS:** Caddy (`deploy/Caddyfile`) or nginx (`deploy/nginx.conf.example`)
   with the iblugames.com hostnames. Cloudflare SSL mode "Full (strict)".
7. **Connect Claude:** add a custom connector pointing at
   `https://mcp.iblugames.com/mcp` with header
   `Authorization: Bearer <MCP_API_KEY>`.
8. **Verify:** open `https://keeper.iblugames.com/` (dashboard) and
   `https://mcp.iblugames.com/health`.

### Step-by-step things ONLY Ignas can do (non-dev)
- Add the two Cloudflare DNS records.
- Confirm SSH access to the Hetzner box (and whether it already runs Caddy/nginx,
  since the box is shared with other projects).
- Approve a fresh "Allow" on the server if we re-run consent there.
- (Optional but recommended) rotate the OAuth client secret.

Everything else (code, services, proxy config) can be driven by a Claude session
over SSH.

---

## 8. How to run things (reference)

```bash
# Local dev (mock mode, no Google):
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env            # leave DRY_RUN=true
python -m iblu_keeper.server    # http://127.0.0.1:8000/health
streamlit run dashboard/app.py  # http://localhost:8501

# Authorize the real account (browser needed):
python scripts/connect_google.py

# Confirm Chat access:
python scripts/test_chat_access.py

# Tests:
pytest -q
```

---

## 9. Repo map (where things live)

- `src/iblu_keeper/server.py` — MCP server + tools + bearer auth + `/health`
- `src/iblu_keeper/google_auth.py` — single-user OAuth (load/refresh token)
- `src/iblu_keeper/tools/{chat,gmail,calendar,context}.py` — tool logic
- `src/iblu_keeper/tools/chat.py` — **swappable** Chat backend (Mock / Google)
- `dashboard/app.py` — Streamlit command center
- `scripts/connect_google.py` — one-time account authorization
- `scripts/test_chat_access.py` — Chat access probe
- `deploy/` — systemd units, Caddyfile, nginx example
- `db/schema.sql` — Phase 2 Postgres sketch
- `README.md` — full setup + deployment guide
