# IBLU — Current State (living document)

> **Read this first.** This is the single entry point for the current state of
> IBLU. It is kept in the repo so it travels with the code. Snapshots below are
> dated; when in doubt, trust the **live sources** listed here over any pasted
> copy.

## How to get the *current* state (never stale)
1. **This repo is the source of truth** — `github.com/ignas-hub/Iblu`, branch `main`.
2. **Recent changes:** read the latest commits (`git log --oneline -20`) — that's
   the always-current changelog.
3. **Architecture & setup:** `README.md` (kept up to date with the code).
4. **Goals & roadmap:** `README.md` → "End state" + "Phased plan" table.
5. **Why decisions were made / past debugging:** `HANDOFF.md`, `DEBUG_FINDINGS.md`.
6. **Is the live server healthy right now?** `GET https://mcp.iblugames.com/health`
   (add `?probe=1` for a live Google-auth check: `mode`, `auth.ok`, `account`).

If you are a Claude session with the GitHub connector, fetch items 2–5 live at
the start of a task rather than relying on memory.

---

## Snapshot — as of 2026-09-12 (Phase 2 session 1)

**What IBLU is:** a self-hosted personal assistant for Ignas. A FastMCP server
exposes Google Chat / Gmail / Calendar / Docs / Drive tools that Claude connects
to over HTTPS; a Streamlit dashboard (`keeper.iblugames.com`) is the review UI.
**Phase 2 ("recording v1") is being built** — build plan in
`docs/plans/2026-09-14-recording-v1.md`; target: recording live Monday
2026-09-14 07:00 Europe/Zagreb.

**Goals / direction** (full detail in README → "End state" + "Phased plan"):
- **Phase 1 (done):** stateless tools + dashboard, no memory.
- **Phase 2 (in progress):** Postgres memory — durable entries, signal
  collectors, two tappable quiz pings per weekday.
- **Phase 3:** goals/priorities context (e.g. "spend 30% of time on sales").
- **End state:** voice-first assistant that remembers context, knows Ignas's
  goals, and proposes actions/replies for review.

**Auth model:** single-user **OAuth** (one refresh token, account
`ignas@blanklabel.team`). No service account, no domain-wide delegation. The MCP
connector itself authenticates Claude.ai via FastMCP's Google provider (DCR).

**Deployment:** Hetzner box at `/home/ignas/iblu`, behind nginx/Caddy. MCP →
`mcp.iblugames.com`, dashboard → `keeper.iblugames.com`. Services via systemd
(`iblu-mcp`, `iblu-dashboard`). The repo is the source; the box updates on
`git pull && pip install -e . && systemctl restart`.

**Storage (new in Phase 2):** PostgreSQL 16 in a dedicated Docker container
`iblu-db` (`--restart unless-stopped`, volume `iblu-pgdata`, bound to
`127.0.0.1:5432` only, database `iblu_keeper`). Schema lives in
`db/migrations/`, applied with `python -m iblu_keeper.db migrate` and tracked in
`schema_migrations`. Before this, IBLU persisted nothing but `data/token.json`
and `data/drafts.jsonl`.

**Tools currently exposed (35).** Phase 2 added `context_log` and
`context_search`; `context_get_summary` and `context_log_conversation` are no
longer stubs and now read/write Postgres. In mock mode (`DRY_RUN=true`) all four
return `{"status": "mock"}` and never touch the database.

**Env keys added this session** (names only; values in `.env`, never committed):
`DATABASE_URL`, `ANTHROPIC_API_KEY`, `IBLU_LLM_MODEL`, `IBLU_TIMEZONE`,
`PING_SIGNING_SECRET`, `PING_ENABLED`, `PING_DAYS`, `PING_MIDDAY`,
`PING_EVENING`, `SECRETARY_SPACE`, `SECRETARY_WEBHOOK_URL`.

**Recent themes (see git log for detail):** real-time Chat unread via Workspace
Events + Pub/Sub, Drive/Docs edit tools, freshness/anti-replay envelope
(`fetched_at` + `request_id`), mock-mode safety (no silent fake data — see
DEBUG_FINDINGS.md).

**Known open items:** Phase 2 sessions 2–3 not built yet — collectors
(`gmail_sent`, `chat_sent`, `calendar_changes`), the tick job + systemd timer,
the ping composer/delivery/`/q` tap endpoint, and the nightly backup timer.
`PING_ENABLED=false` and `SECRETARY_*` are empty until the Secretary Chat space
exists. Phase 3 (goals/priorities) not started.

---

## Maintenance rule (for whoever edits IBLU, human or Claude)
When you finish a change that alters tools, auth, deployment, or phase status,
**update the snapshot above** (date + commit + any tool/▲ changes) in the same
commit. Keep it short — detail lives in README/commits. The pointers in the top
section must always stay valid even if the snapshot ages.
