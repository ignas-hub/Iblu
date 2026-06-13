# Debug Findings — MCP "stale data / silent write failures" (2026-06-13)

Task brief: the live Iblu MCP server returned stale/days-old data and several
write actions (Chat send, Gmail send, Calendar create) silently "succeeded"
without taking effect.

## Root cause — NOT caching

The brief hypothesised a caching/TTL problem. **There is no response cache in
this codebase** (`grep -rniE 'cache|ttl'` finds only credential caching and
`cache_discovery=False`). The real causes are configuration + a design flaw:

### 1. The server was running in MOCK mode (primary cause)
Every reported symptom is *exactly* what mock mode produces. Reproduced locally
with `DRY_RUN=true`:
- `gmail.search("sparkleads")` → a fake "Re: sparkleads" (query echoed into a
  hard-coded result), never the live inbox.
- `gmail.list_unread()` → nonsensical "Re: is:unread".
- `gmail.send_email(...)` → `{"status":"sent","mock":true}` — **delivered nothing**.
- `calendar.create_event(...)` → `MOCK_EVENT_1` — **created nothing**.
- Chat list/send → fixed fake spaces, fake "sent".

Mock data is dated 2026-06-10, which is why it looked "days old".

**Why it was in mock mode:** the old `Settings.use_mock` returned `True` whenever
`DRY_RUN=true` **OR credentials were missing**. So a production server with
`DRY_RUN=true` in `.env` (the shipped default) — or with no valid token —
**silently served fake data and reported writes as succeeding.**

### 2. OAuth client secret was rotated → token refresh broke
Live refresh now fails with `invalid_client: The provided client secret is
invalid`. The secret was rotated (as recommended after it was shared in chat),
but `_load_credentials()` used `setdefault`, so the **stale secret baked into
`token.json` was kept** instead of the current one from `.env`. Refresh tokens
survive secret rotation, so this is fixable from `.env` alone.

### 3. (Hypothesis) Issue #5 "tools suddenly unregistered"
The MCP connector auth (`GoogleProvider`) uses the **same** client_id/secret. A
rotated secret would break Claude.ai session token refresh mid-session → tools
drop → reconnect (fresh OAuth) restores them. Matches the observed behavior.
Added structured logging so the next occurrence can be confirmed from logs.

## Fixes in this change

1. **No more silent mock.** `use_mock` is now driven *solely* by `DRY_RUN`.
   If `DRY_RUN=false` but the token is missing/invalid, live calls **raise a
   clear error** (`CredentialsUnavailable`) instead of returning fake data.
   New `Settings.misconfigured_live` flag surfaces this state.
2. **Rotated-secret recovery.** `_load_credentials()` now **overrides**
   client_id/secret from `.env` (source of truth), so a rotated secret is picked
   up without re-running consent. Refresh failures are logged and raise a clear,
   actionable error.
3. **Health diagnostics.** `/health` now reports `misconfigured_live` and a
   `warning`. `/health?probe=1` actively refreshes the token and reports
   `auth: {ok, account, error}` — instantly shows expired/rotated creds.
4. **Loud startup.** `main()` logs an ERROR if `DRY_RUN=false` without a usable
   token, and verifies credentials at boot in live mode.
5. **Unmistakable mock data.** All mock responses are tagged `_mock: true`;
   mock writes return `status: not_sent_mock` / `not_created_mock` (never a
   false "sent"/"created").
6. **Write logging.** Gmail/Calendar/Chat sends log the real Google id on
   success (proof of delivery) and a WARNING when mocked.

## What the LIVE server must do (operational — cannot be done from the repo)

1. In `/home/ignas/iblu/.env` set **`DRY_RUN=false`**.
2. Set **`GOOGLE_OAUTH_CLIENT_SECRET`** to the *current* (post-rotation) secret.
3. Ensure a valid `data/token.json` exists (re-run `scripts/connect_google.py`
   if needed; with fix #2 the existing token should refresh once the secret is
   correct).
4. Restart: `sudo systemctl restart iblu-mcp` (and `iblu-dashboard`).
5. Verify: `curl 'https://mcp.iblugames.com/health?probe=1'` →
   `"mode":"live"`, `"auth":{"ok":true,...}`.

## Assumptions
- Gmail/Calendar/Chat tool *logic* is correct when live (validated earlier this
  session against the real account); the failures were mode/config, not logic.
- Chat read-state (#2) and a true Chat draft (#4) are genuine feature gaps, not
  regressions — tracked as follow-ups (Google Chat API has no true server-side
  draft; closest is the local draft store already present).

## How to reproduce
```bash
# Mock symptoms:
DRY_RUN=true python -c "import sys;sys.path.insert(0,'src');\
from iblu_keeper.tools import gmail; print(gmail.send_email('a@b.com','t','t'))"
# Fail-loud when misconfigured (no silent fake data):
DRY_RUN=false GOOGLE_OAUTH_CLIENT_ID=x GOOGLE_OAUTH_CLIENT_SECRET=y \
GOOGLE_OAUTH_TOKEN_FILE=/nope python -c "import sys;sys.path.insert(0,'src');\
from iblu_keeper.tools import gmail; gmail.search('x')"   # -> CredentialsUnavailable
pytest -q   # 12 passing, incl. no-silent-mock + mock-tagging tests
```
