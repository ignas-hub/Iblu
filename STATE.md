# IBLU — Current State (living document)

Mission: docs/MISSION.md — read before anything else; every change in this repo serves it.

Yearly priorities and baselines live in `context_entries` (tags `priority` /
`baseline`, one per venture). Read them before any Stage 3 work:

```
docker exec iblu-db psql -U iblu -d iblu_keeper -c "SELECT DISTINCT ON (venture) venture, left(content,120) FROM context_entries WHERE type='decision' AND 'priority' = ANY(tags) AND superseded_by IS NULL ORDER BY venture, created_at DESC;"
```

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

## Snapshot — as of 2026-09-13 (recording v1 live; priorities, stages and the reconstructed day)

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

**Tools currently exposed (39).** The `repo` tool reads Ignas's source from
two sources behind one namespace: `iblu` and `automations` come off this box's
disk (so they include uncommitted work), everything else from GitHub via a
fine-grained read-only token (`GITHUB_TOKEN`). Credential-shaped filenames are
refused from both; local paths cannot escape the repo root. A fine-grained
PAT is scoped to ONE resource owner, so each GitHub organisation needs its own
token: set `GITHUB_TOKEN`, `GITHUB_TOKEN_2`, ... and IBLU tries each until one
can see the repository — today three: `ignas-hub` (personal),
`BlankTracker` and `deadlift-machina`, 12 repositories in total. A bare name
like `machina` or `email-writer` is resolved against every visible owner, so the
caller never needs to know which account holds a project. GitHub's /search/code
endpoint returns nothing for
fine-grained tokens, so search downloads each repo as a single tarball and greps
it locally, cached 5 min — 3s cold across all repos, instant warm. Permission policy (set 2026-09-13): only
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

**Multi-account (week 2, code ready):** `GOOGLE_ACCOUNTS=blt,choco,deadlift`
plus `GOOGLE_ACCOUNT_<ALIAS>_CLIENT_ID/_SECRET/_EMAIL`; one token file per alias
(`data/token.<alias>.json`), authorized with
`python scripts/connect_google.py --account choco`. Each Workspace needs its own
Cloud project + Internal OAuth client — IBLU's consent screen is Internal to
blanklabel.team, so the other accounts cannot authorise it. `build_service(...,
account=...)` selects the identity, credentials are cached per alias (never
globally, so one account can never hand back another's token), and `gmail_sent`
runs once per account with its own watermark. All three Workspaces are live: `blt` (ignas@blanklabel.team),
`deadlift` (admin@deadlift.io) and `choco` (**ignacio@chocoagency.com** — note
that admin@chocoagency.com also exists and is NOT the tracked account).
All three collectors now run per account: `gmail_sent` counts every address
the mailbox may send as (minus Google Group deliveries), `chat_sent` uses a Chat
backend cached per account so each resolves its own self-id, and
`calendar_changes` namespaces its baseline by account (migration 004) because
two Workspaces can each have a calendar called 'primary'. Calendar *writes*
stay primary-only by design — the Secretary mirror is one calendar on BLT
covering every venture (HANDOFF.md §15), configured as `SECRETARY_CALENDAR_ID`
and verified writable. Note the OAuth token carries `calendar.events`, so IBLU
can read and write events on any calendar Ignas owns but CANNOT read calendar
metadata or ACLs (`calendars.get` returns 403) — sharing must be checked by hand — the Chat backend
resolves one self-id per process and a second calendar would need its own
`calendar_seen` namespace.

**Calendar management:** the `calendar` tool adds list / find_slot / create /
update / move / delete behind one name. `find_slot` computes free gaps from
`events.list` rather than the freeBusy API, deliberately — freeBusy would need a
wider OAuth scope and therefore re-consent on every account. Declined meetings
count as free time, because a declined meeting is not a commitment.

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

**The reconstructed day (`blocks`, built 2026-09-13):** `python -m
iblu_keeper.jobs.analyst` clusters each day's signals into blocks, judges each
one against the intent calendar (`present` / `displaced` / `ambiguous`) and
mirrors the result onto `SECRETARY_CALENDAR_ID` — one calendar on
blanklabel.team covering every venture. `deploy/iblu-analyst.timer` runs it
weekdays at 17:00 and 20:15; the later run supersedes the earlier one. Read it
from Claude with `calendar(action='day')`, rebuild with
`calendar(action='reconstruct')`. Three rules are load-bearing and documented in
HANDOFF §16: silence produces no block, an intent's venture comes from the event
rather than from whose calendar it is, and evidence belongs to the slice it
happened in.

**Slack (built 2026-09-13):** `slack_sent` collects what Ignas wrote in the BLT
and Deadlift workspaces via `search.messages`, which requires a **user** token
(`xoxp-`) and the single scope `search:read` — see HANDOFF §17. The collector
registry now carries a `scope` (`google` / `slack` / `primary`) instead of a
boolean, because Slack workspaces are a separate list from Google accounts.

**Governance (sessions 5–8, 2026-09-13):** the yearly priority and the
2026-09-13 baseline for each of the seven ventures live in `context_entries`;
`store/governance.py` is the one way to read them and `get_context` returns them
between the mission and the brief. `store/projects.py` + migration 007 hold
eight stages and 28 registered initiatives — a project cannot reach
`autonomous` without a stated finish line, enforced in code, because that is the
stage Ignas actually fails at. `context_log` returns a `gap_warning` on a goal
phrased as a distance rather than an outcome; it never blocks the write.

**The Gain layer:** the evening ping carries at most four cards — two attention
questions, one "what moved today?" (multi-tap, each tap independent) and one
body/mind card that stores two numbers and interprets nothing. The weekly
review runs Friday 18:00 over Monday→Friday and reads Gains → Truth → one
removal, with a fourth 30/90-day section on the last Friday of each month.
A language validator rejects gap phrasing and falls back to the deterministic
template rather than sending text that failed. Two validators exist and have
different jobs: `store/gap_check.py` guards what Ignas writes into IBLU;
`jobs/review_language.py` guards what IBLU writes back to him.

**What IBLU caught itself getting wrong:** `observations` (migration 008)
collects every rejected LLM response, failed invariant and collector error —
things that were previously a `logger.warning` and then gone. The analyst runs
two sense-check passes at the end of every run: deterministic invariants
(`detected_by='rule'`, a fact) and an LLM reading what the scripts produced
(`detected_by='llm'`, a lead). Never merge the two. Read them with:

```
python -m iblu_keeper.store.observations              # open findings
python -m iblu_keeper.store.observations --write-doc  # regenerate docs/OBSERVATIONS.md
python -m iblu_keeper.store.observations --resolve <id> --note "what was done"
```

`IBLU_CHECK_MODEL` (default `claude-opus-5`) is the model that checks the work;
`IBLU_LLM_MODEL` (Sonnet) is the one that composes pings. See HANDOFF §21.

**Known open items:** external DM partners who are not in Google Contacts
cannot be named by the People API, so their `counterpart` stays `users/<id>`
(1 space today). Phase 3 (goals/priorities) not started. Remaining week-2 backlog (plan §12): the mobile web app,
the analyst pass that fills `signals.summary`, the monthly review,
`context_compact`, and the Chrome / shell / BT / Screen Time collectors.

---

## Maintenance rule (for whoever edits IBLU, human or Claude)
When you finish a change that alters tools, auth, deployment, or phase status,
**update the snapshot above** (date + commit + any tool/▲ changes) in the same
commit. Keep it short — detail lives in README/commits. The pointers in the top
section must always stay valid even if the snapshot ages.
