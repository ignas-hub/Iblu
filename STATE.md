# IBLU — Current State (living document)

Mission: docs/MISSION.md — read before anything else; every change in this repo serves it.

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

## Snapshot — as of 2026-09-13 (recording v1 live + mission layer)

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

**Tools currently exposed (38).** Permission policy (set 2026-09-13): only
`gmail_send_email` and `chat_send_message` ask; the other 35 are auto-allow,
including `gmail_reply`. Enforced by `tests/test_tool_permissions.py` — which keeps the annotations
honest but CANNOT set the client's behaviour: claude.ai stores Allow/Ask per
tool per connector, and a tool added after the connector was last configured
always defaults to Ask. Prefer a new parameter on an existing tool over a new
`@mcp.tool`; see HANDOFF.md §12. Phase 2 added `context_log` and
`context_search`; `context_get_summary` and `context_log_conversation` are no
longer stubs and now read/write Postgres. In mock mode (`DRY_RUN=true`) all four
return `{"status": "mock"}` and never touch the database.

**Env keys added this session** (names only; values in `.env`, never committed):
`DATABASE_URL`, `ANTHROPIC_API_KEY`, `IBLU_LLM_MODEL`, `IBLU_TIMEZONE`,
`PING_SIGNING_SECRET`, `PING_ENABLED`, `PING_DAYS`, `PING_MIDDAY`,
`PING_EVENING`, `SECRETARY_SPACE`, `SECRETARY_WEBHOOK_URL`.
Values containing shell metacharacters **must be quoted** in `.env` — the
webhook URL contains `&`, and unquoted it is silently truncated when the
file is sourced by a shell.

**Mission layer (session 4):** `docs/MISSION.md` is the source of truth for
what IBLU is for. Migration 003 adds `context_brief.mission` / `mission_sha` /
`mission_seeded_at`; `python -m iblu_keeper.db seed-mission` copies the file in
and is idempotent by sha. The `get_context` tool returns the mission first,
then the brief and the window summary, and flags `mission_stale` when the file
has moved ahead of the database (`None` when the file cannot be read — that is
"could not check", not "current"). The ping composer loads the mission from the
database into its system prompt; an empty mission logs one warning and
continues. The four "what IBLU must become" lines lead the MCP server's
connect-time instructions, with the full text one `get_context` call away.

**Analyst read (stage 2, started):** `context_review(window)` reports the
venture split, the work-type split (tapped answers only — inferred data never
reaches it), threads touched >=3 times as delegation candidates, the share of
signals in threads Ignas did not start, and the ping answer rate against its
80% target. Every response carries `coverage` so a thin window cannot read as a
confident finding; `response_format='markdown'` gives the speakable form.
Deterministic SQL, no LLM — scripts fetch, the LLM judges.

**Weekly review (stage 2):** `python -m iblu_keeper.jobs.weekly [--dry]`
posts the attention review into the Secretary space as plain text (nothing to
tap — it is meant to be read), and records it as a `decision` entry so the
conclusion is durable. `deploy/iblu-weekly.timer` fires Sundays 18:00
Europe/Zagreb, `Persistent=true` so a missed week still arrives. Below 10
signals it says the week was too quiet to conclude from rather than dressing
noise as insight.

**Recent themes (see git log for detail):** real-time Chat unread via Workspace
Events + Pub/Sub, Drive/Docs edit tools, freshness/anti-replay envelope
(`fetched_at` + `request_id`), mock-mode safety (no silent fake data — see
DEBUG_FINDINGS.md).

**Recording (sessions 1–2 done):** three collectors write to `signals` —
`gmail_sent`, `chat_sent`, `calendar_changes`, each with its own watermark in
`collector_state`. `python -m iblu_keeper.jobs.tick` runs them; it refuses to
run in mock mode. `deploy/iblu-tick.timer` fires it every 10 min, 07:00–19:50
Mon–Fri (Europe/Zagreb); `deploy/iblu-backup.timer` dumps the database nightly
at 03:15 and keeps 14 days in `/home/ignas/backups/iblu`.

Two collector rules worth remembering, both learned the hard way:
- **`in:sent` is not "I wrote it".** Google Group traffic (`contracts@`,
  `finance@`) is filed under Sent for group members, so the collector compares
  the parsed `From` address against the account. Six of the first seven
  "sent" messages were other people's.
- **Silence is not presence.** The calendar collector seeds its baseline
  silently on first run, so pre-existing events are never reported as new.

**Pings (session 3 done):** two tappable quiz pings per weekday, delivered by
incoming webhook into the Google Chat space `iblu` (`SECRETARY_SPACE`). Midday
12:30–14:00 covers 06:00→now; evening 17:00–18:30 covers the midday send→now.
Within the window the tick waits for a moment that is not during a meeting, not
within 5 min after one and not within 10 min before the next; at the window's
end it sends regardless. Questions are composed by `claude-sonnet-5` over the
window's signals, with deterministic fallback templates if the API is
unavailable — `pings.composer` records which ran. Tapping an option hits
`GET /q/<signed-token>` and writes a `work_log` entry; re-tapping the same
question supersedes the previous answer. Free-text replies in the ping's Chat
thread are read on the next tick.

**Two rules the ping layer must keep:**
- **The Secretary space is never collected.** Answering a ping is not work;
  left in, the recorder would report talking to itself as the biggest
  attention sink.
- **`/q` never returns a 500.** The link is unauthenticated by design (the
  signed token is the authority), so every failure — forged, expired, missing
  ping — renders a plain page, and every token failure mode is
  indistinguishable from the outside.

**Known open items:** external DM partners who are not in Google Contacts
cannot be named by the People API, so their `counterpart` stays `users/<id>`
(1 space today). Phase 3 (goals/priorities) not started. Week-2 backlog is in
the plan §12 — `blocks`, the Secretary calendar, the mobile web app, the
analyst pass, multi-account, weekly/monthly quiz.

---

## Maintenance rule (for whoever edits IBLU, human or Claude)
When you finish a change that alters tools, auth, deployment, or phase status,
**update the snapshot above** (date + commit + any tool/▲ changes) in the same
commit. Keep it short — detail lives in README/commits. The pointers in the top
section must always stay valid even if the snapshot ages.
