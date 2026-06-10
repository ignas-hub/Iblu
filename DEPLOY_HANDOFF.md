# Deploy Handoff → Claude on the Hetzner server

**You are Claude Code running on Ignas's Hetzner box.** The repo is already
cloned at `/home/ignas/iblu`, on `main` tracking `origin/main`. Your job this
session is to **deploy iblu-keeper** so Claude.ai can connect to it 24/7.

> First thing: `git pull` — this doc and recent work may be newer than your
> clone. Then read `README.md` (full guide) and `HANDOFF.md` (project state).

---

## Who you're working with
**Ignas is NOT a developer.** Explain each step in plain language, one at a time.
Confirm before anything destructive or outward-facing. He drives a lot and may
be on mobile — keep instructions copy-pasteable and short.

## What this project is (1 paragraph)
`iblu-keeper` = a personal AI assistant. An **MCP server** (FastMCP, Python)
exposes Google Chat / Gmail / Calendar tools that Claude connects to over HTTPS;
a **Streamlit dashboard** is the review UI. Phase 1 only (no memory yet). Auth to
Google is **single-user OAuth** (one refresh token, scoped to `ignas@blanklabel.team`
only — no service account, no domain-wide delegation).

---

## ✅ Already done (don't redo)
- Code is complete and **validated against the real account**: Gmail, Calendar,
  and Google Chat all confirmed working via single-user OAuth.
- Google Cloud project `iblu-keeper`: APIs enabled (Gmail, Calendar, Chat),
  OAuth consent screen (Internal), **OAuth Web client created**, **Chat app
  configured**.
- OAuth **Client ID:** `751405989189-shlh26ll813vmdftt5r5s1md8tf6n0jh.apps.googleusercontent.com`
- Registered redirect URI: `https://keeper.iblugames.com/`
- Domain plan: dashboard → `keeper.iblugames.com`, MCP → `mcp.iblugames.com`.

## ❌ Not done yet (your job)
1. Python venv + install on this box.
2. `.env` with real secrets.
3. A valid `data/token.json` on this box (see "Token bootstrap" — the previous
   token lived in an ephemeral cloud workspace and is **not** here).
4. systemd services running.
5. HTTPS reverse proxy + DNS.
6. Connect Claude.ai and verify.

---

## ⚠️ Environment notes specific to THIS box
- **Paths differ from the README.** Repo is at **`/home/ignas/iblu`**, not
  `/opt/iblu-keeper`. Adjust the systemd units (`deploy/*.service`):
  `WorkingDirectory=/home/ignas/iblu`,
  `EnvironmentFile=/home/ignas/iblu/.env`,
  `ExecStart=/home/ignas/iblu/.venv/bin/...`.
- **Shared box.** It runs Ignas's other projects too. **Before** binding ports
  or installing a reverse proxy, check what's already there:
  `sudo ss -tlnp`, and check for existing Caddy/nginx (`systemctl status caddy nginx`).
  Don't clobber existing config. Ports 8000 (MCP) and 8501 (dashboard) are
  defaults — change them if taken.
- **Headless** — no browser here. The OAuth consent must use the manual
  copy-paste flow below (the `connect_google.py` browser flow won't work
  unattended).

---

## Token bootstrap (headless OAuth) — the tricky part
You need `data/token.json` (a refresh token). Do this:

1. Make sure `.env` has `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`
   (Ignas provides the secret; or rotate it in Cloud console → Credentials →
   the client → *Reset secret*, recommended since it was shared in chat earlier).
2. Build the consent URL (scopes must match `src/iblu_keeper/google_auth.py:SCOPES`):
   ```python
   from urllib.parse import urlencode
   import sys; sys.path.insert(0, "src")
   from iblu_keeper.google_auth import SCOPES
   print("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
       "client_id": "<CLIENT_ID>",
       "redirect_uri": "https://keeper.iblugames.com/",
       "response_type": "code", "scope": " ".join(SCOPES),
       "access_type": "offline", "prompt": "consent",
       "include_granted_scopes": "true"}))
   ```
3. Give Ignas the URL. He opens it → **Allow** → lands on a page that may not
   load → he copies the **full address-bar URL** (contains `?...code=...`) and
   pastes it back to you.
4. Extract `code` and exchange it (must use the SAME redirect_uri):
   ```python
   import requests, json, os
   tok = requests.post("https://oauth2.googleapis.com/token", data={
       "code": CODE, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
       "redirect_uri": "https://keeper.iblugames.com/",
       "grant_type": "authorization_code"}, timeout=30).json()
   os.makedirs("data", exist_ok=True)
   json.dump({"token": tok["access_token"], "refresh_token": tok["refresh_token"],
       "token_uri": "https://oauth2.googleapis.com/token",
       "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
       "scopes": tok["scope"].split()}, open("data/token.json","w"))
   os.chmod("data/token.json", 0o600)
   ```
   Confirm `refresh_token` is present. **Never commit `data/token.json`** (it's
   git-ignored).
5. Verify: `python scripts/test_chat_access.py` should PASS (set
   `GOOGLE_OAUTH_CLIENT_ID/SECRET` in env or `.env`). On this real box there is
   no TLS-interception proxy, so the Google client works without CA tweaks.

---

## Deployment steps (summary — full detail in README "Deployment")
```bash
cd /home/ignas/iblu && git pull
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip && pip install -e .

cp .env.example .env && chmod 600 .env
python -c "import secrets; print('MCP_API_KEY='+secrets.token_urlsafe(32))"  # into .env
# Set in .env: MCP_API_KEY, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
#   DASHBOARD_OAUTH_CLIENT_ID/SECRET (same client is fine),
#   DASHBOARD_ALLOWED_EMAIL=ignas@blanklabel.team, DRY_RUN=false
```
Then: token bootstrap (above) → systemd units (paths fixed for `/home/ignas/iblu`)
→ Caddy/nginx for `mcp.iblugames.com` + `keeper.iblugames.com` → verify.

### What only Ignas can do (ask him)
- Add Cloudflare **A records**: `keeper.iblugames.com` and `mcp.iblugames.com`
  → this box's public IP (`178.104.122.152`). Cloudflare SSL mode "Full (strict)".
- Confirm `sudo` access and whether a reverse proxy already runs here.
- Click the OAuth **Allow** and paste back the redirect URL.
- (Recommended) rotate the OAuth client secret.

---

## Definition of done
- `curl https://mcp.iblugames.com/health` → `{"status":"ok","mode":"live",...}`
- `https://keeper.iblugames.com/` loads, Google login works (only Ignas).
- Claude.ai custom connector → `https://mcp.iblugames.com/mcp` with
  `Authorization: Bearer <MCP_API_KEY>` → tools appear and a test call works.
- Both systemd services `enabled` (survive reboot).

## Known limitation to mention, not fix now
DM/group chats show participant **IDs not names** (Google Chat API doesn't return
human display names via user OAuth). Resolving needs People/Directory API +
scope — a future enhancement. Named spaces are fine. See `HANDOFF.md` §6.

## Git / workflow
- Work on `main` (or a feature branch) and push; commits should be authored as
  `Claude <noreply@anthropic.com>`.
- Never commit `.env`, `data/token.json`, or any `*.json` credentials.
