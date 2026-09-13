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

---

## 10. Corrections from the Phase 2 build (2026-09-12 / 13)

Nine things a later session must not rediscover the hard way. Sections 1–9 above
describe the June state; these supersede it where they conflict.

**Infrastructure**

1. **Postgres is a Docker container, not a host install.** There is no host
   Postgres on the box and no passwordless sudo, so IBLU has its own container
   `iblu-db` (`postgres:16-alpine`, volume `iblu-pgdata`, `--restart
   unless-stopped`, bound to `127.0.0.1:5432` only). The unrelated `radovi-db-1`
   container is not touched. The v1 plan's `apt install postgresql` path was
   **not** used. `pg_dump` therefore runs *inside* the container — there is no
   client on the host — which is why `deploy/iblu-backup.sh` shells through
   `docker exec`.

2. **`.env` values containing `&` must be single-quoted.** The Secretary webhook
   URL has `?key=...&token=...`; unquoted, `set -a; . ./.env` splits the line and
   silently drops everything after the ampersand, leaving a URL that 401s at send
   time. `python-dotenv` parses it correctly, which is exactly what made this
   invisible. systemd's `EnvironmentFile` strips matching outer quotes, so one
   quoting style satisfies all three consumers.

3. **`/q`, `tools/` and `server.py` run inside `iblu-mcp`** — changes there need
   `sudo systemctl restart iblu-mcp`. The tick job is a separate process that
   re-reads `.env` on every run, so collector, composer and delivery changes go
   live without a restart.

**Anthropic API**

4. **Never pass `temperature` (or any sampling parameter).** They were removed on
   `claude-sonnet-5` and return HTTP 400. The v1 plan §7.3 specifies
   `temperature=0`; it cannot be followed. Determinism comes from
   `output_config={"effort": ...}` plus a strict schema.

**Collectors**

5. **Gmail `in:sent` is not the same as "I wrote it".** Google Group traffic
   (`contracts@`, `finance@`) is filed under Sent for group members. Six of the
   first seven collected messages were written by other people — one was a
   colleague's invoice reply, recorded as 355 characters of Ignas's attention.
   The collector compares the *parsed* `From` address against the account; a
   substring check is not enough (`not-ignas@blanklabel.team.evil.com`).

6. **The Secretary Chat space is excluded from `chat_sent`.** Answering a ping is
   not work. Left in, the recorder would eventually report talking to itself as
   Ignas's biggest attention sink.

**Pings**

7. **Migration 002's unique index is scoped to `source='chat_reply'` and must
   stay narrow.** Free-text replies were being recorded once per tick, because
   `read_thread_replies` used `ON CONFLICT DO NOTHING` with no constraint to
   conflict on and watermarks deliberately rewind 5 minutes. A unique index
   across all sources would break corrections: taps intentionally write several
   rows per `source_ref` to form the supersede chain (plan D9).

8. **`deliver.send()` preflights the `/q` route and refuses to send on a 404.**
   The tick and the server are separate processes, so the timer can be live while
   the server runs a build without the tap route — the card sends, the buttons
   404, and nothing says so until Ignas taps one on his phone. Better no card
   than a card with dead buttons. A healthy route answers 410 to a bad token.

9. **Failed pings are retryable.** Plan §7.4 requires retry on the next tick with
   the same row, but `_already_handled` originally treated any non-pending status
   as done, so one webhook outage would have cost the whole day's ping.

**Question design**

10. **There is a fourth question type, `work_type`,** beyond the plan's §7.2 set.
    All three original types asked about *venture*; nothing asked what kind of
    work it was. Venture is recoverable after the fact from an email domain —
    `work_type` never is, so an unasked classification is lost permanently and
    "where does my sales time go?" becomes unanswerable. It is captured by tap,
    not inferred; an option carrying `verdict='classify'` without a `work_type`
    code fails validation. Two wording rules are enforced by schema validation
    rather than prompt alone, so a regressing model falls through to the
    deterministic templates: internal qids (`sink`) must not appear in the text a
    human reads, and unnamed references ("a gmail thread") are rejected.

**Testing**

11. **`tests/conftest.py` pins `DRY_RUN=true` for the whole suite.** `config.py`
    calls `load_dotenv()` at import and the live `.env` has `DRY_RUN=false`, so
    isolation depended on which module imported `iblu_keeper` first — adding a
    test file broke it and a test wrote a real row into the live database.
    `Settings` is a frozen dataclass: substitute the module-level `settings`
    object, never patch its attributes.

### 12. Adding a tool costs Ignas a click — prefer a parameter

Verified 2026-09-13. claude.ai does **not** derive its per-tool Allow/Ask
setting from MCP annotations. Five tools with byte-identical annotations showed
different states in the connector UI, split purely by when each was first
registered: tools present when the connector was last configured were Allow,
tools that appeared afterwards defaulted to Ask.

This is by design, not a bug. The MCP spec says clients "MUST consider tool
annotations to be untrusted unless they come from trusted servers", and
Anthropic's permission docs state that MCP toolsets "default to always_ask …
so that new tools added to an MCP server do not execute without approval".
There is no server-side override — no annotation, no `_meta`, no capability.

Consequences for this repo:

1. **Every new `@mcp.tool` costs Ignas a manual click**, forever, in a UI he
   has to find. A permission prompt mid-drive is exactly the friction the
   mission forbids, so the cost is real, not cosmetic.
2. **Prefer extending an existing tool with a parameter** over registering a
   new one. `context_review` should have been `context_get_summary(view=
   'review')` — it would have shipped already-allowed. Register a genuinely
   new tool only when the capability does not belong on any existing one.
3. **Annotations still matter** — they drive the *grouping* in that UI. The 16
   tools with `readOnlyHint: true` are exactly the "Read-only tools (16)"
   group, which is what makes the group-level "always allow" control usable in
   one action instead of tool by tool. `tests/test_tool_permissions.py` keeps
   them honest; it cannot enforce client behaviour and says so.
4. **When a new tool is unavoidable, say so in the handover**, with the words
   "you will need to set this to Always allow in Settings > Connectors", rather
   than claiming a restart or reconnect will apply it. It will not.

### 13. Never delete a `calendar_seen` row for an event that still exists

Found 2026-09-13. Cleaning up after a calendar acceptance test, the test event
was deleted from Google AND its `calendar_seen` baseline row was deleted. The
next tick fetched the calendar with `showDeleted=True`, found an event it had no
baseline for, saw its `created` timestamp was within 24 h, and emitted a fresh
`created` signal — re-creating exactly the row the cleanup had removed.

`calendar_seen` is the memory of what has already been reported. Deleting a row
does not erase history; it makes the collector forget it ever saw the event, so
the event gets reported again. To remove a test event: delete it in Google,
let one tick run so the collector records the cancellation, then delete the
`signals` rows only — and leave the baseline alone.
