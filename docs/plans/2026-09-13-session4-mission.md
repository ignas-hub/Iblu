# IBLU — Session 4: Mission integration — task for Claude Code

Written 2026-09-13 after the Recording v1 session report (main @ d20c15d, 126 tests, 35 tools, recording live, tick timer → Mon 2026-09-14 07:00). This session adds **only** the mission layer — the text that says what Iblu is for, and the plumbing that puts it in front of every LLM that touches Iblu. Budget ≈ 45–60 min.

**Nothing from the v1 plan (`docs/plans/2026-09-14-recording-v1.md`) is to be redone.** Sessions 1–3 are complete; A1–A10 pass; `drive_create_file` exists (7f025ca).

---

## 0. Rules (same as plan §0, plus what the v1 report taught)

1. Read first: `STATE.md`, `git log --oneline -20`, `docs/plans/2026-09-14-recording-v1.md`, `HANDOFF.md`, `DEBUG_FINDINGS.md`, this file. Repo wins over this file — say so and stop where they conflict.
2. Step 0: commit this document unchanged as `docs/plans/2026-09-13-session4-mission.md`.
3. No secrets are involved in this session. If one turns out to be, print the `read -rs` form for Ignas — never ask him to paste anything into chat.
4. `/q`, `tools/`, and `server.py` run inside `iblu-mcp` → changes there need `sudo systemctl restart iblu-mcp`. The tick is a separate process and re-reads `.env` every run.
5. Never add `temperature` (or other sampling params) to an Anthropic call in this repo — `claude-sonnet-5` returns 400. Use `output_config` + strict schema as the composer already does.
6. `.env` values containing `&` stay single-quoted.
7. Tests run under `tests/conftest.py` with `DRY_RUN=true`; substitute the module-level `settings` object, never patch attributes of the frozen dataclass. Nothing writes to the live DB from a test. **No mock rows in the database, ever.**
8. `STATE.md` snapshot updated in the same commit as any tool/config/deploy change. Stop at the CHECKPOINT (§5) and report in the same format as the 2026-09-13 report.

## 1. Scope

**IN:** `docs/MISSION.md` (text in §6, verbatim) · `STATE.md` pointer · migration 003 (`context_brief.mission`) · `db seed-mission` command · `get_context` tool (create if absent, extend if present) · mission in the LLM composer's system prompt · 4-line mission summary in the FastMCP server `instructions` · tests · README tool table · deploy + restart · disk check (report only).

**OUT:** analyst pass, `context_compact`, the whole §12 week-2 backlog, Claude Project instructions and Claude settings (Ignas does those by hand), any change to ping scheduling or question types.

## 2. Design decisions (do not re-litigate)

- **M1 — The mission gets its own column, not a section inside `context_brief.content`.** The June constraint is "only the compactor writes to `context_brief`"; keeping the mission in a separate column makes it structurally impossible for a future `context_compact` to rewrite or drop it. `content` stays the compactor's.
- **M2 — `docs/MISSION.md` is the source of truth; the DB row is the runtime copy.** `seed-mission` is idempotent: it stores `sha256(file)` next to the text and rewrites only when the sha changes. `get_context` reports `mission_stale=true` when the file on disk differs from the DB copy, so drift is visible instead of silent.
- **M3 — The composer reads the mission from the DB, not the file.** Same copy the tools serve. Empty mission → one warning line, composer continues — Monday must never depend on this.
- **M4 — Server `instructions` get only the four "WHAT IBLU MUST BECOME" lines** plus "Full mission: call `get_context`." Connect-time tokens are paid on every session; the full text lives one tool call away.

## 3. Steps

**3.1 — `docs/MISSION.md`** — §6 verbatim, no edits. `STATE.md`: first line under the title becomes `Mission: docs/MISSION.md — read before anything else; every change in this repo serves it.`

**3.2 — `db/migrations/003_mission.sql`**
```sql
ALTER TABLE context_brief
    ADD COLUMN mission           TEXT NOT NULL DEFAULT '',
    ADD COLUMN mission_sha       TEXT,
    ADD COLUMN mission_seeded_at TIMESTAMPTZ;
INSERT INTO schema_migrations (version) VALUES ('003_mission');
```

**3.3 — `python -m iblu_keeper.db seed-mission [--file docs/MISSION.md]`** — read the file, `sha256`, compare with `context_brief.mission_sha` on row 1; if different, `UPDATE` `mission`, `mission_sha`, `mission_seeded_at=now()`. Print exactly one line: `mission seeded sha=<12 chars>` or `mission unchanged sha=<12 chars>`. Never print the text. Mock mode → exit 1 with `refusing to run in mock mode` (same rule as the tick).

**3.4 — `get_context`** (`tools/context.py` + `server.py`). Signature `get_context(window: str = "1d") -> dict`, returning, in this order: `mission`, `mission_sha`, `mission_stale` (file sha ≠ DB sha; `None` if the file is unreadable), `brief` (= `context_brief.content`, may be empty), `summary` (= the existing `context_get_summary(window)` payload). Read-only annotations, `stamped()` envelope, mock mode → `{"status": "mock"}` and no DB access. If a `get_context` already exists in the repo, extend it to this shape rather than adding a second tool. The live-data half from the June design (unread mail, calendar) is week 2 — keep the signature extensible, do not add it now.

**3.5 — Composer** (`pings/compose.py`): system prompt = full `context_brief.mission` + the existing instructions. Log one line per compose: `composer: mission sha=<12> loaded` or `composer: mission EMPTY — continuing`. The deterministic fallback composer is unchanged. Do not touch the question types, the `work_type` question, or the validation rules from the v1 report.

**3.6 — `server.py` FastMCP `instructions`**: prepend the four numbered lines of "WHAT IBLU MUST BECOME" from §6, then `Full mission: call get_context.` Keep the existing voice/freshness instructions intact below it.

**3.7 — Tests** (all green with `DATABASE_URL` unset; DB-backed ones skipped, not failed): `seed-mission` idempotency and sha logic (temp file, fake connection); `get_context` in mock mode returns `{"status":"mock"}`; composer system prompt contains the mission text when the DB copy is non-empty and logs the EMPTY line when it is not; `mission_stale` true/false/None cases.

**3.8 — Docs**: README tool table (+1 tool if `get_context` is new → 36), `STATE.md` snapshot (commit, tool count, migration 003, new command). `HANDOFF.md`: confirm the 2026-09-13 corrections are recorded — Docker Postgres `iblu-db` (no host Postgres, no apt path), no `temperature` on sonnet-5, `From`-address check on `in:sent` (Google Group traffic), `&` in `.env`, migration 002 unique index scoped to `chat_reply`, the `work_type` question, Secretary space excluded from `chat_sent`, `deliver.send()` preflight of `/q`, failed pings retryable. Add whichever are missing; that file is where they belong.

**3.9 — Deploy** (from `/home/ignas/iblu`):
```bash
git pull && .venv/bin/pip install -e .
.venv/bin/python -m iblu_keeper.db migrate
.venv/bin/python -m iblu_keeper.db seed-mission
sudo systemctl restart iblu-mcp && curl -s https://mcp.iblugames.com/health
.venv/bin/python -m iblu_keeper.jobs.tick --dry        # expect the "composer: mission sha=… loaded" line
```

**3.10 — Disk check, report only** (the v1 report says the disk is at 89 %, 4 GB free — with nightly dumps, Docker layers and journald that number matters):
```bash
df -h / ; docker system df ; du -sh /home/ignas/backups/iblu ; journalctl --disk-usage
```
Report the numbers and propose what to prune (dangling images, journald cap, anything else you find). **Delete nothing without Ignas's explicit yes in the session.**

## 4. Acceptance

- **B1** `docker exec iblu-db psql -U iblu -d iblu_keeper -c "select length(mission), mission_sha, mission_seeded_at from context_brief"` → length > 0, sha present.
- **B2** From Claude in the IBLU Project: `get_context()` returns the mission first, `mission_stale=false`; `context_get_summary` still works unchanged.
- **B3** `tick --dry` prints the composer prompt and logs `composer: mission sha=… loaded`; no DB writes.
- **B4** Edit one character in `docs/MISSION.md` on the box (do not commit) → `get_context()` shows `mission_stale=true` → `seed-mission` prints `seeded` → `false` again → `git checkout docs/MISSION.md`.
- **B5** `pytest` green without `DATABASE_URL`.
- **B6** `STATE.md` first line points to `docs/MISSION.md`; tool count and migration list correct; pushed.

## 5. CHECKPOINT

Report B1–B6, the disk numbers with a prune proposal, and any place the repo contradicted this file. Then stop. Ignas takes it from there (Project instructions + settings are his).

## 6. `docs/MISSION.md` — commit verbatim

```markdown
# IBLU — Mission

Written 2026-09-13. This file is the source of truth. The IBLU Claude Project instructions and `context_brief.mission` inside Iblu are copies of it. Change this file first; then `python -m iblu_keeper.db seed-mission` and re-paste the Project instructions.

## What Iblu is

Iblu is Ignas's personal chief of staff, built as his own infrastructure (Hetzner box, custom MCP server, Postgres), not a SaaS. It sits between Ignas and everything that competes for his attention — three mailboxes, three Chats, two Slacks, calendars, servers, BT — across every venture he runs: Blank Label Team, Choco Agency, Deadlift (including Machina), Jakusi, family, and his own tooling.

## The problem it exists to solve

Ignas has limited capacity for attention and deep thinking, and the daily noise drowns the yearly priorities. He cannot see where his attention actually goes, so he cannot steer it. Nothing today tells him what to ignore, what to delegate, what to automate, or whether a week served the year.

## What Iblu must become

1. It knows the truth about where Ignas's attention goes — measured, not remembered.
2. It holds his priorities at every level — yearly, monthly, weekly, daily — and judges every incoming thing against the top of that hierarchy.
3. It keeps those priorities in front of him every day, inside his existing flow (Chat, voice), with no app to open.
4. It continuously removes work from him: it proposes what to delegate or automate, then does the automatable parts itself.

## Stages

- **0 — Hands (done).** Claude reads and acts on Gmail, Chat, Calendar, Drive through Iblu, by voice or text.
- **1 — Eyes (live Monday 2026-09-14).** The recorder: signals from sent mail, Chat and calendar changes; two quiz pings a day; answers stored as `work_log`, with `venture` and `work_type` tapped, not guessed.
- **2 — Analyst (weeks 2–4).** The reconstructed day (`blocks`), a Secretary calendar next to the intent calendar, a weekly review: attention per venture and work type, what he touched three times that should be someone else's, what to automate. Uncomfortable numbers are the product.
- **3 — Priorities (after about a month of evidence).** The yearly/monthly/weekly/daily hierarchy, written from evidence rather than guessed. A daily brief that scores the day's noise against it: ignore, delegate, or do. Reminders in the flow.
- **4 — Autonomy.** Iblu handles recurring work itself — drafts, replies, scheduling, follow-ups — under his review, and keeps proposing the next thing to take off his plate.

## Principles — never violated

- Attention, not location. Silence is never presence. A tracker that flatters is worse than none.
- Scripts fetch; the LLM judges. Iblu is not a pile of scripts.
- Storage is durable-only; live data is fetched fresh and never stored as memory.
- Corrections supersede, never delete.
- One taxonomy across all ventures: `venture` + `work_type` + `project`. Adding a venture is one row, not a migration.
- Evidence before hierarchy: measure first, then define priorities from what was measured.
- Progress is measured backward against the baseline (the Gain), not against the ideal (the Gap).
- The deep thinking is Iblu's job; Ignas decides between reasoned options. Iblu still disagrees when warranted.
- Personal infrastructure only: no company systems (BT, n8n) inside Iblu, no third-party SaaS in the loop, no service-account keys.

## How Iblu knows it is working (12 months)

- Ignas can answer "where did my attention go this month, per venture?" with numbers he trusts.
- His share of time on the top yearly priority, per venture, is visible weekly and moving the way he chose.
- Recurring tasks removed from him are counted monthly and the count grows.
- He answers the pings on at least 80 % of weekdays — the habit holds.

## How the LLM always has this in mind

One text, three homes:

1. **Repo** — this file. `STATE.md` points to it first; Claude Code reads it before any task.
2. **Claude Project** — the same text at the top of the IBLU Project instructions, so every chat starts from it, including voice, which loads no connectors.
3. **Iblu itself** — `context_brief.mission` is seeded from this file; `get_context` returns it first; the compactor cannot touch it; the ping composer and the analyst pass carry it in their system prompt, so every question and every finding is judged against it.

Rule: change this file first; the other two copies follow.
```
