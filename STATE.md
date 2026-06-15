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
4. **Why decisions were made / past debugging:** `HANDOFF.md`, `DEBUG_FINDINGS.md`.
5. **Is the live server healthy right now?** `GET https://mcp.iblugames.com/health`
   (add `?probe=1` for a live Google-auth check: `mode`, `auth.ok`, `account`).

If you are a Claude session with the GitHub connector, fetch items 2–4 live at
the start of a task rather than relying on memory.

---

## Snapshot — as of 2026-06-15 (commit `67a5dae`)

**What IBLU is:** a self-hosted personal assistant for Ignas. A FastMCP server
exposes Google Chat / Gmail / Calendar / Docs tools that Claude connects to over
HTTPS; a Streamlit dashboard (`keeper.iblugames.com`) is the review UI. Phase 1
(stateless; no long-term memory yet).

**Auth model:** single-user **OAuth** (one refresh token, account
`ignas@blanklabel.team`). No service account, no domain-wide delegation. The MCP
connector itself authenticates Claude.ai via FastMCP's Google provider (DCR).

**Deployment:** Hetzner box at `/home/ignas/iblu`, behind nginx/Caddy. MCP →
`mcp.iblugames.com`, dashboard → `keeper.iblugames.com`. Services via systemd
(`iblu-mcp`, `iblu-dashboard`). The repo is the source; the box updates on
`git pull && pip install -e . && systemctl restart`.

**Tools currently exposed (21):**
`chat_list_conversations`, `chat_get_messages`, `chat_send_message`,
`chat_draft_message`, `chat_list_unread`, `chat_mark_read`,
`gmail_search`, `gmail_get_message`, `gmail_draft_email`, `gmail_send_email`,
`gmail_list_unread`, `gmail_mark_read`, `gmail_mark_unread`, `gmail_reply`,
`gmail_list_attachments`, `gmail_read_attachment`, `gdoc_read`,
`calendar_create_event`, `context_log_conversation` (stub),
`context_get_summary` (stub), `server_health`.

**Recent themes (see git log for detail):** freshness/anti-replay envelope
(`fetched_at` + `request_id`, `Cache-Control: no-store`), pagination, read/unread
+ reply tools, People-API name resolution for Chat DMs, voice-mode protocol,
mock-mode safety (no silent fake data — see DEBUG_FINDINGS.md).

**Known open items:** Phase 2 (Postgres memory) and Phase 3 (goals/priorities)
not built (stubs present). Live-server config must have `DRY_RUN=false` + a valid
token, else live tools fail loudly by design.

---

## Maintenance rule (for whoever edits IBLU, human or Claude)
When you finish a change that alters tools, auth, deployment, or phase status,
**update the snapshot above** (date + commit + any tool/▲ changes) in the same
commit. Keep it short — detail lives in README/commits. The pointers in the top
section must always stay valid even if the snapshot ages.
