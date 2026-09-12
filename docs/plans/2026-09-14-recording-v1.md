# IBLU Phase 2 — Recording v1 ("the spy") — build plan for Claude Code

Written 2026-09-11 (design session Ignas × Claude), updated 2026-09-12 with Ignas's answers (§13). Target: **recording live Monday 2026-09-14, 07:00 Europe/Zagreb**.
Repo: github.com/ignas-hub/Iblu, branch `main` · Box: `ignas@178.104.122.152`, live checkout `/home/ignas/iblu` (verify: `grep -E 'WorkingDirectory|ExecStart|EnvironmentFile' /etc/systemd/system/iblu-mcp.service`).
Budget: 3 Claude Code sessions ≈ 4–5 h total, plus 30 min of acceptance on Sunday.

---

## 0. Rules for the executing agent (Claude Code)

1. Before touching code: read `STATE.md`, `git log --oneline -20`, `README.md`, and this file. Where the repo contradicts this plan, say so and stop — the repo wins.
2. Step 0 of Session 1: commit this document unchanged as `docs/plans/2026-09-14-recording-v1.md`.
3. **Secrets are pasted by Ignas, never by you.** For every secret (API key, webhook URL) print the exact `read -rs` command from §10, ask Ignas to run it in his own SSH terminal, then verify presence with `grep -c` — never `cat`, `echo`, or log a value. This happens as one of the first steps of Session 1, before any code.
4. Absolute paths only. Real paths come from the **live** systemd unit, not from `deploy/` templates (those still assume `/opt/iblu-keeper`).
5. Never commit `.env`, `data/token.json`, or any credential. Generate non-pasted secrets with `python -c "import secrets; print(secrets.token_urlsafe(32))"` piped straight into `.env`, without printing them.
6. No `sudo` inside services or timers (`User=ignas`). `sudo` only interactively, for apt and the Postgres role.
7. Fail loud. If `DRY_RUN=true`, the tick job and every collector exit non-zero and write nothing. **No mock rows in the database, ever** (see `DEBUG_FINDINGS.md`).
8. Never send a real Chat message or email unless Ignas says "go" in the session. `--force-ping` always asks first.
9. Stop at every CHECKPOINT and wait for Ignas.
10. Keep repo conventions: type hints, `logging`, tool responses through the `stamped()` envelope, annotations on every `@mcp.tool`, `pytest` green in mock mode **without** a database.
11. Every commit that changes tools, config, deployment, or phase status updates the `STATE.md` snapshot in the same commit (maintenance rule in STATE.md).

## 1. Scope

**IN — must be live Monday:** Postgres + migrations; real `context_log` / `context_search`; three collectors (my sent mail, my Chat messages, calendar changes) for `ignas@blanklabel.team` only; two tappable quiz pings per weekday via a "Secretary" Google Chat space; answers stored as `work_log` entries; one tick timer; nightly DB backup.

**OUT — week 2+ (§12):** Secretary calendar and day reconstruction ("blocks"), mobile web app, LLM analyst pass, the Choco and Deadlift mailboxes/Chats, Slack, Chrome / server / BT / Screen Time collectors, weekly & monthly quiz, `context_compact`.

## 2. Decisions (with reasons — do not re-litigate in the session)

- **D1 — Signals ≠ memory.** Observations go to `signals`; durable facts and quiz answers go to `context_entries`. The June constraint ("storage is durable-only, live data is never stored as memory") stays intact: the brief never reads `signals` directly; only conclusions become entries.
- **D2 — One taxonomy across all ventures.** Every row carries `venture` + `work_type` (+ optional free-text `project`). No per-company schema. The interesting questions ("where does sales time go, across everything?") only work with shared tags. Adding a venture = one `INSERT`, not a migration. Machina is `venture='deadlift', project='machina'`.
- **D3 — Ping delivery = incoming webhook in a Google Chat space "Secretary".** Iblu sends as Ignas (user OAuth): a message you send yourself never notifies you, and cards need app auth. A webhook posts as an app, notifies, supports cards with link buttons, and needs no service-account key (kept out deliberately — HANDOFF §3).
- **D4 — Tap = link button** → `GET https://mcp.iblugames.com/q/<signed-token>` → row written → tiny "Saved" page. Cost: one browser hop per tap. Free-text alternative: reply in the ping's thread; the tick job reads it. Both land in the same table the same way.
- **D5 — Questions are composed by an LLM pass** over the window's signals + calendar (scripts fetch, the model judges) using the Claude API key Ignas pastes in Session 1. Deterministic fallback templates if the API call fails — Monday must not depend on the API being up.
- **D6 — Scheduling = one systemd timer**, every 10 min, 07:00–19:50 Mon–Fri, running `python -m iblu_keeper.jobs.tick` (collect → read replies → maybe ping). All state in the DB; restarts are safe.
- **D7 — Two pings.** Midday window 12:30–14:00 (covers 06:00→now); evening 17:00–18:30 (covers midday send→now). Send at the first tick where: no event in progress, ≥5 min since the last event ended, ≥10 min until the next starts. At window end, send regardless.
- **D8 — Store a snippet + the native ID, never full content.** Snippets ≤300 chars, quoted text and signatures stripped.
- **D9 — Corrections supersede, never delete.** Re-tapping a question writes a new entry and sets `superseded_by` on the old one.
- **D10 — psycopg 3 directly, no ORM.** SQL migrations in `db/migrations/NNN_*.sql`, tracked in `schema_migrations`. Timestamps stored as `timestamptz` (UTC); scheduling in `Europe/Zagreb`.
- **D11 — Pings and collectors run Monday to Friday only in v1** (`PING_DAYS=MON,TUE,WED,THU,FRI`), env-configurable — Ignas did not object to the Mon–Fri suggestion; flip the env var to add weekends.

## 3. Prerequisites only Ignas can do

- **P1 — Secretary space** (before Session 3). Google Chat → create a space "Secretary" (only you). Space notifications → **All**. Apps & integrations → Webhooks → add "Iblu" → copy the URL — you paste it with the §10 command as `SECRETARY_WEBHOOK_URL`. Copy the space ID from the URL (`…/space/AAAA…` → `spaces/AAAA…`) — this one is not secret, Claude Code can write it as `SECRETARY_SPACE`.
- **P2 — Claude API key** (Session 1, first steps). Create it in Anthropic's developer console with a monthly spend cap. Claude Code prints the paste command (§10 step 1); you run it in your own SSH terminal; the key lands in `/home/ignas/iblu/.env` as `ANTHROPIC_API_KEY` without ever being displayed. Claude Code then checks it works with a one-token test call that prints only `api ok`.
- **P3 — sudo** on the box for `apt install postgresql` and the role creation (Session 1, interactive).
- **P4 — Venture list** in §4 is final for v1 (confirmed 2026-09-12); editable later with plain SQL.

## 4. Data model — `db/migrations/001_phase2_recording.sql`

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ventures (
    code       TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    active     BOOLEAN NOT NULL DEFAULT true,
    sort_order INT NOT NULL DEFAULT 100
);
INSERT INTO ventures (code, label, sort_order) VALUES
    ('blt',      'Blank Label Team',                                             10),
    ('choco',    'Choco Agency (Opera client; operationally BLT)',               20),
    ('deadlift', 'Deadlift.io (ads agency; Machina = its main software project)',30),
    ('jakusi',   'Jakusi — house / property (Opatija)',                          40),
    ('family',   'Family & personal life',                                       50),
    ('personal', 'Own tooling & infra (IBLU, accounting bot, servers)',          60)
ON CONFLICT DO NOTHING;

CREATE TABLE work_types (
    code       TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    sort_order INT NOT NULL DEFAULT 100
);
INSERT INTO work_types (code, label, sort_order) VALUES
    ('sales',    'Sales / BD / pitches',                      10),
    ('client',   'Existing-client communication & management',20),
    ('delivery', 'Doing the work (campaigns, creative, ads)', 30),
    ('people',   'Managing / hiring / team',                  40),
    ('finance',  'Finance, invoices, legal, admin',           50),
    ('build',    'Software, automation, infrastructure',      60),
    ('admin',    'Ops, tooling, scheduling, housekeeping',    70),
    ('life',     'Non-work: family, home, health',            80)
ON CONFLICT DO NOTHING;

-- Observations. Never read directly into the brief (D1).
CREATE TABLE signals (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source             TEXT NOT NULL CHECK (source IN ('gmail','chat','calendar')),
    kind               TEXT NOT NULL,            -- gmail/chat: 'sent' ; calendar: 'created'|'moved'|'changed'|'cancelled'
    account            TEXT NOT NULL,            -- 'ignas@blanklabel.team' (multi-account in week 2)
    occurred_at        TIMESTAMPTZ NOT NULL,
    actor              TEXT NOT NULL DEFAULT 'me' CHECK (actor IN ('me','other')),
    initiator          TEXT CHECK (initiator IN ('me','other')),   -- who started the thread
    counterpart        TEXT,                     -- email / person / space display name
    container          TEXT,                     -- gmail thread id / chat space name / calendar id
    subject            TEXT,                     -- email subject / space name / event title
    snippet            TEXT,                     -- <=300 chars of what I wrote / the change
    ask_snippet        TEXT,                     -- <=300 chars of the message I was responding to
    length_chars       INT,
    venture            TEXT REFERENCES ventures(code),
    venture_confidence TEXT NOT NULL DEFAULT 'inferred' CHECK (venture_confidence IN ('fact','inferred')),
    work_type          TEXT REFERENCES work_types(code),
    project            TEXT,                     -- free text, e.g. 'machina', 'bt', 'email-writer'
    summary            TEXT,                     -- one line per exchange; filled by the analyst pass (week 2)
    source_ref         TEXT NOT NULL,            -- native id
    meta               JSONB NOT NULL DEFAULT '{}'::jsonb,
    collected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, source_ref)
);
CREATE INDEX signals_occurred_idx  ON signals (occurred_at DESC);
CREATE INDEX signals_venture_idx   ON signals (venture, occurred_at DESC);
CREATE INDEX signals_container_idx ON signals (container, occurred_at DESC);

-- Durable facts, decisions, preferences, and quiz answers (work_log).
CREATE TABLE context_entries (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type               TEXT NOT NULL,            -- 'work_log'|'fact'|'preference'|'decision'|'correction'|'conversation_note'
    content            TEXT NOT NULL,
    importance         SMALLINT NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    tags               TEXT[] NOT NULL DEFAULT '{}',
    venture            TEXT REFERENCES ventures(code),
    work_type          TEXT REFERENCES work_types(code),
    project            TEXT,                     -- free text: 'iblu','bt','email-writer','accounting-bot','machina',...
    source             TEXT NOT NULL DEFAULT 'claude',   -- 'claude'|'ping'|'chat_reply'|'analyst'
    source_ref         TEXT,                     -- 'ping:<id>:<qid>' / chat message name / ...
    occurred_at        TIMESTAMPTZ,              -- when the thing happened (work_log); NULL for timeless facts
    meta               JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ,
    superseded_by      UUID REFERENCES context_entries(id),
    last_referenced_at TIMESTAMPTZ
);
CREATE INDEX ce_type_idx     ON context_entries (type, created_at DESC);
CREATE INDEX ce_occurred_idx ON context_entries (occurred_at DESC);
CREATE INDEX ce_tags_idx     ON context_entries USING GIN (tags);
CREATE INDEX ce_fts_idx      ON context_entries USING GIN (to_tsvector('simple', content));

-- Singleton. Only the (future) compactor writes here.
CREATE TABLE context_brief (
    id               SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    content          TEXT NOT NULL DEFAULT '',
    generated_at     TIMESTAMPTZ,
    source_entry_ids UUID[] NOT NULL DEFAULT '{}'
);
INSERT INTO context_brief (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE pings (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('midday','evening','test')),
    local_date       DATE NOT NULL,
    window_start     TIMESTAMPTZ NOT NULL,
    window_end       TIMESTAMPTZ NOT NULL,
    covers_from      TIMESTAMPTZ NOT NULL,       -- the period the questions are about
    covers_to        TIMESTAMPTZ NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','answered','expired','failed')),
    sent_at          TIMESTAMPTZ,
    chat_message_ref TEXT,                       -- webhook response .name
    chat_thread_ref  TEXT,                       -- webhook response .thread.name
    questions        JSONB NOT NULL DEFAULT '[]'::jsonb,  -- snapshot: [{qid,text,options:[{key,label,payload}]}]
    composer         TEXT CHECK (composer IN ('llm','fallback')),
    meta             JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX pings_one_per_day ON pings (kind, local_date) WHERE kind IN ('midday','evening');

-- Calendar diff baseline.
CREATE TABLE calendar_seen (
    calendar_id  TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    updated      TIMESTAMPTZ,
    fingerprint  TEXT NOT NULL,                  -- sha256(start,end,summary,status,self_response,sorted attendee emails)
    payload      JSONB NOT NULL,                 -- compact copy of those fields
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (calendar_id, event_id)
);

CREATE TABLE collector_state (
    name        TEXT PRIMARY KEY,                -- 'gmail_sent'|'chat_sent'|'calendar_changes'|'secretary_replies'
    watermark   TIMESTAMPTZ,
    cursor      TEXT,
    last_run_at TIMESTAMPTZ,
    last_error  TEXT
);

INSERT INTO schema_migrations (version) VALUES ('001_phase2_recording');
```

**The schema that matters most — what one tap writes** (a month of capture is unanalysable if this is wrong): one `context_entries` row with `type='work_log'`, `source='ping'`, `source_ref='ping:<id>:<qid>'`, `occurred_at=covers_to`, `venture`/`work_type`/`project` from the option payload (nullable), `tags=['ping','<kind>']`, `content="<local_date> <HH:MM–HH:MM> · <question text> → <option label>"`, `meta={"ping_id","qid","choice_key","payload","answered_via":"tap","covers_from","covers_to"}`. The **question text and all option labels are snapshotted on the `pings` row**, so "B" always stays decodable.

## 5. Code layout (new files marked +)

```
src/iblu_keeper/
  + db.py                        psycopg pool from DATABASE_URL; get_conn(); migrate(); `python -m iblu_keeper.db migrate`
    tools/context.py             real: log_entry(), search_entries(), get_summary(); log_conversation() alias
  + collectors/__init__.py       run_all(conn) → per-collector counts; each collector: collect(conn) -> int
  + collectors/gmail_sent.py
  + collectors/chat_sent.py
  + collectors/calendar_changes.py
  + collectors/venture_hints.py  editable rules: domain / space keyword / default-by-account → venture + project (inferred)
  + pings/schedule.py            windows + gap logic (pure functions, unit-tested)
  + pings/compose.py             LLM composer + fallback → validated questions JSON
  + pings/deliver.py             webhook POST with threadKey; returns message + thread names
  + pings/answers.py             token sign/verify, record_tap(), read_thread_replies()
  + jobs/tick.py                 `python -m iblu_keeper.jobs.tick [--dry] [--force-ping midday|evening|test] [--yes]`
    server.py                    + custom_route GET /q/{token}; + tools context_log, context_search
+ db/migrations/001_phase2_recording.sql
+ deploy/iblu-tick.service  + deploy/iblu-tick.timer  + deploy/iblu-backup.sh  + deploy/iblu-backup.service  + deploy/iblu-backup.timer
+ tests/test_schedule.py  + tests/test_tokens.py  + tests/test_migrations.py (skipped when DATABASE_URL is unset)
+ docs/plans/2026-09-14-recording-v1.md   (this file)
```

Config additions (`config.py` + `.env.example`): `DATABASE_URL`, `SECRETARY_WEBHOOK_URL`, `SECRETARY_SPACE`, `PING_SIGNING_SECRET`, `PING_ENABLED` (default false), `PING_DAYS=MON,TUE,WED,THU,FRI`, `PING_MIDDAY=12:30-14:00`, `PING_EVENING=17:00-18:30`, `IBLU_TIMEZONE=Europe/Zagreb`, `ANTHROPIC_API_KEY`, `IBLU_LLM_MODEL` (default: the current Sonnet model id — verify in Anthropic docs before hardcoding), `MCP_PUBLIC_BASE_URL` (already exists; used to build tap links). Add `anthropic` to `requirements.txt`.

## 6. Tick job — `jobs/tick.py`

Order, every run: (1) guard — `settings.use_mock` → log "refusing to run in mock mode", exit 1; (2) collectors `gmail_sent`, `chat_sent`, `calendar_changes`, each with its own watermark in `collector_state`; a failing collector logs `last_error` and does not abort the others; (3) read Secretary thread replies for pings with `status='sent'` in the last 48 h; (4) if `PING_ENABLED`: ping decision for `midday` and `evening` (§7.1); (5) exit 0. One summary log line: `tick: gmail=+3 chat=+7 cal=+1 replies=+1 ping=midday:sent`.

Flags: `--dry` = run collectors read-only, print the composed card JSON, no DB writes, no sends. `--force-ping KIND` = ignore window and gap, compose and send now; asks "send? [y/N]" unless `--yes`; `KIND=test` bypasses the one-per-day rule.

Watermarks: `max(occurred_at)` seen minus 5 min overlap; idempotency via `UNIQUE(source, source_ref)` + `ON CONFLICT DO NOTHING`.

## 7. Pings

**7.1 Decision** (per kind, per tick, in `Europe/Zagreb`): skip if today ∉ `PING_DAYS`, or a non-pending `pings` row exists for (kind, today), or `now < window_start`. Fetch today's `primary` events live (`singleEvents=True`; ignore all-day, cancelled, and events where my `responseStatus == 'declined'`). `free_ok` = no event in progress AND (no event ended in the last 5 min) AND (no event starts within 10 min). Send if `free_ok` or `now >= window_end`. `covers_from` = 06:00 today (midday) or the midday `sent_at` — else 13:00 — (evening); `covers_to` = now.

**7.2 Question set** — max 3 questions, 2–4 options each, every option carries a structured `payload`:
- `sink` — the biggest attention sink in the window, **named** ("Womanizer permissions thread with Bella — 7 messages since 09:10"). Options: `A Planned & mine` · `B Unplanned, still mine` · `C Should be someone else's` · `D One-off, ignore`.
- `displaced` — only if the window contains a calendar block without attendees (a self-block) whose span shows no signals: "You had <block> planned. What took it?" Options: the top two signal clusters by volume · `Nothing — I did it` · `Other → reply in thread`.
- `split` — venture split of the window's signals: "Looks like BLT 70 / Deadlift 30. Right?" Options: `Right` · `More <v1>` · `More <v2>` · `Way off → reply`.

Payload shape: `{"kind":"sink|displaced|split","verdict":"planned_mine|unplanned_mine|someone_else|one_off|did_it|other|right|more|way_off","venture":"blt"|null,"work_type":"client"|null,"project":"machina"|null,"signal_ids":[…],"container":"<thread/space id>"|null,"event_id":"…"|null}`.

**7.3 LLM composer** (`pings/compose.py`): Anthropic Messages API, model `IBLU_LLM_MODEL`, `max_tokens=800`, `temperature=0`. Input: the `ventures` and `work_types` tables; window bounds; today's calendar (title, start, end, attendee count, self-block flag); the window's signals as compact lines `HH:MM src counterpart|space — subject — initiator=me|other — snippet≤120`. Output: strict JSON `{"questions":[{"qid":"sink","text":"…≤160 chars, names the specific thread/event…","options":[{"key":"A","label":"…≤40…","payload":{…}}]}]}`. Validate with pydantic (≤3 questions, ≤4 options, payload enum). Any failure — timeout, invalid JSON, API error — → fallback composer, `pings.composer='fallback'`. Fallback = the same three templates filled deterministically: sink = container with most signals; displaced = first attendee-less event in the window with zero signals in its span; split = share of signals by inferred venture.

**7.4 Delivery** (`pings/deliver.py`): `POST <SECRETARY_WEBHOOK_URL>&threadKey=ping-<id>&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD`. Body: `text` = `<users/<SELF_ID>> Midday check — 3 taps.` (SELF_ID = tokeninfo `sub`, already resolved by `GoogleChatBackend._ensure_self_id`; reuse it) + `cardsV2` with one section per question: header = question text, widgets = `buttonList` of `openLink` buttons labelled `A · Planned & mine`, etc. Store the response's `name` → `chat_message_ref` and `thread.name` → `chat_thread_ref`; `status='sent'`, `sent_at=now`. HTTP error → `status='failed'`, `meta.error`, retry next tick (same row).

**7.5 Tap endpoint** — `@mcp.custom_route("/q/{token}", methods=["GET"])`, unauthenticated like `/health`. Token = `base64url(json{"p":ping_id,"q":qid,"k":key,"exp":ts})` + `.` + `hmac_sha256(PING_SIGNING_SECRET, payload)`; `exp = sent_at + 36h`. Valid → `answers.record_tap()` → 200 HTML: `<meta name="viewport" content="width=device-width">`, one big line `✓ Saved — B · Unplanned, still mine`, nothing else. Expired or bad signature → 410 `Expired — reply in the Chat thread instead.` Never a 500 on user input.

**7.6 Record** (`answers.record_tap`): insert the `context_entries` row from §4. If a non-superseded entry with the same `source_ref` exists, set its `superseded_by` to the new id (D9). Set `pings.status='answered'` once ≥1 question has an entry.

**7.7 Free-text replies** (`answers.read_thread_replies`): `spaces.messages.list(parent=SECRETARY_SPACE, filter='createTime > "<watermark>"')`; keep messages whose `thread.name` equals a ping's `chat_thread_ref` and `sender.type == "HUMAN"`; insert `context_entries` `type='work_log'`, `source='chat_reply'`, `source_ref=<message name>`, `content=<text>`, `meta={"ping_id":…,"answered_via":"reply"}`. Watermark in `collector_state('secretary_replies')`.

## 8. Collectors (account `ignas@blanklabel.team`; reuse the existing Google service builders and `GoogleChatBackend` — do not duplicate auth code)

**8.1 `gmail_sent`** — `users.messages.list(q="in:sent after:<epoch>")` → `messages.get(format="full")`. Row: `kind='sent'`, `occurred_at=internalDate`, `counterpart=To` (first address), `container=threadId`, `subject`, `snippet` = first 300 chars of my text with the quoted part stripped (cut at the first line matching `^On .* wrote:$` or beginning with `>`; drop everything after a `-- ` signature line), `length_chars` of the unquoted text, `ask_snippet` = last message in the thread before mine not from me (`threads.get`, 300 chars, same stripping), `initiator='me'` if the thread's first message is from me else `'other'`, `venture`/`project` via `venture_hints` (inferred), `source_ref=<message id>`, `meta={"to":[…],"cc_count":n,"thread_len":n}`.

**8.2 `chat_sent`** — `spaces.list` (pageSize 100, paginate; skip spaces whose `lastActiveTime` < watermark) → per space `messages.list(filter='createTime > "<watermark>"', orderBy='createTime desc', pageSize=50)` → keep `sender.name == users/<SELF_ID>`. Row: `kind='sent'`, `occurred_at=createTime`, `counterpart` = space display name or resolved DM name, `container=<space name>`, `subject` = space display name, `snippet≤300`, `ask_snippet` = nearest earlier message in the same thread (else same space, within 4 h) not from me, `initiator` = `'me'` if that thread's first message is mine else `'other'`, `source_ref=<message name>`, `meta={"thread":<thread.name>}`.

**8.3 `calendar_changes`** — `events.list(calendarId='primary', timeMin=today-1d, timeMax=today+7d, singleEvents=True, showDeleted=True, maxResults=250)`. Fingerprint as in §4. Compare with `calendar_seen`: unseen event whose `created` is within 24 h → `kind='created'`; fingerprint changed → `'moved'` if start/end differ, `'cancelled'` if status cancelled or I declined, else `'changed'`. `snippet` = human diff (`"Tue 14:00–15:00 → Wed 10:00–11:00"`), `meta={"before":…,"after":…}` (compact fields only), `source_ref="<event id>:<new fingerprint>"`, `occurred_at=event.updated`, `subject=summary`, `container='primary'`. Then upsert `calendar_seen`. **First run seeds `calendar_seen` silently** (no signals) and sets `collector_state.cursor='seeded'`.

**8.4 `venture_hints.py`** — `{"domains": {"deadlift.io": "deadlift", "chocoagency.com": "choco"}, "domain_projects": {"machina.deadlift.io": "machina"}, "space_keywords": {"opera": "choco", "choco": "choco", "deadlift": "deadlift", "machina": "deadlift", "jakusi": "jakusi", "radovi": "jakusi"}, "keyword_projects": {"machina": "machina"}, "default_by_account": {"ignas@blanklabel.team": "blt"}}`. Always `venture_confidence='inferred'` in v1. Ignas extends this file; no code change needed.

## 9. MCP tool changes (30 → 32 tools; update the README table and the STATE.md snapshot)

- **NEW `context_log`**`(type, content, importance=3, tags=None, venture=None, work_type=None, project=None, source='claude', source_ref=None, occurred_at=None, supersedes=None)` → `{id, created_at}`. Annotations: `readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False`. Validates `venture` / `work_type` against the tables; the error lists valid codes. `supersedes` sets `superseded_by` on that entry.
- **NEW `context_search`**`(query=None, type=None, tags=None, venture=None, since=None, limit=20)` → matching entries (`to_tsvector('simple')` + filters), excludes superseded, bumps `last_referenced_at`. Read-only annotations.
- **`context_get_summary(window='1d')`** becomes real, no LLM: `{signals: {by_source, by_venture}, pings: [{kind, status, answered_questions}], work_log: [entries]}`.
- **`context_log_conversation`** keeps its signature; now inserts `type='conversation_note'`; docstring marks it "deprecated — use context_log".
- In mock mode all four return `{"status": "mock"}` exactly as the stubs do today and never touch the DB.

## 10. Deployment — Hetzner, from the live checkout

```bash
# ---- Session 1, step 0: commit this plan to docs/plans/ (Claude Code) ----

# ---- Session 1, step 1: secrets Ignas pastes HIMSELF ----
# Claude Code prints these two commands. Ignas runs them in his OWN SSH terminal
# (Termius / a second shell), NOT inside Claude Code. Each prompts for a paste and
# appends to .env without ever showing the value on screen.
read -rs -p 'Paste ANTHROPIC_API_KEY, then Enter: ' V; printf '\nANTHROPIC_API_KEY=%s\n' "$V" >> /home/ignas/iblu/.env; unset V; chmod 600 /home/ignas/iblu/.env; echo saved
read -rs -p 'Paste SECRETARY_WEBHOOK_URL, then Enter: ' V; printf 'SECRETARY_WEBHOOK_URL=%s\n' "$V" >> /home/ignas/iblu/.env; unset V; echo saved
# (the webhook line can wait until P1 is done — before Session 3 at the latest)

# Claude Code verifies presence only — never the values:
grep -cE '^ANTHROPIC_API_KEY=' /home/ignas/iblu/.env        # expect 1
# Claude Code generates the non-pasted secrets straight into .env, unprinted:
printf 'PING_SIGNING_SECRET=%s\n' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" >> /home/ignas/iblu/.env
# Claude Code confirms the key works, after `pip install -e .` (prints only "api ok"):
cd /home/ignas/iblu && set -a && . ./.env && set +a && .venv/bin/python -c "import anthropic,os; anthropic.Anthropic().messages.create(model=os.environ['IBLU_LLM_MODEL'],max_tokens=5,messages=[{'role':'user','content':'ping'}]); print('api ok')"

# ---- Session 1, step 2: Postgres ----
ss -ltnp | grep 5432 ; docker ps ; systemctl list-units --type=service | grep -i postgres   # reuse an existing Postgres if one is running
sudo apt install -y postgresql                       # Ubuntu 24.04 → PostgreSQL 16 (gen_random_uuid built in)
# password generated and written without printing:
PW=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))'); sudo -u postgres psql -c "CREATE ROLE iblu LOGIN PASSWORD '$PW';" -c "CREATE DATABASE iblu_keeper OWNER iblu;"; printf 'DATABASE_URL=postgresql://iblu:%s@127.0.0.1:5432/iblu_keeper\n' "$PW" >> /home/ignas/iblu/.env; unset PW
git pull && .venv/bin/pip install -e . && .venv/bin/python -m iblu_keeper.db migrate
sudo systemctl restart iblu-mcp && curl -s https://mcp.iblugames.com/health

# ---- Session 2 / 3 ----
# .env += PING_ENABLED=false (Session 2) → true (after A7), SECRETARY_SPACE, IBLU_LLM_MODEL,
#         IBLU_TIMEZONE=Europe/Zagreb, PING_DAYS, PING_MIDDAY, PING_EVENING   (none of these are secret)
mkdir -p /home/ignas/backups/iblu
sudo cp deploy/iblu-tick.service deploy/iblu-tick.timer deploy/iblu-backup.service deploy/iblu-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now iblu-tick.timer iblu-backup.timer
systemd-analyze calendar "Mon..Fri *-*-* 07..19:00/10:00 Europe/Zagreb"   # verify the expression before enabling
systemctl list-timers | grep iblu ; journalctl -u iblu-tick -n 50
```

Units: `iblu-tick.service` → `Type=oneshot`, `User=ignas`, `WorkingDirectory=/home/ignas/iblu`, `EnvironmentFile=/home/ignas/iblu/.env`, `ExecStart=/home/ignas/iblu/.venv/bin/python -m iblu_keeper.jobs.tick`, same hardening lines as `iblu-mcp.service`. `iblu-tick.timer` → `OnCalendar=Mon..Fri *-*-* 07..19:00/10:00 Europe/Zagreb`, `Persistent=false`, `AccuracySec=1min`. `iblu-backup.timer` → daily 03:15; `deploy/iblu-backup.sh` = `pg_dump "$DATABASE_URL" | gzip > /home/ignas/backups/iblu/iblu_keeper_$(date +%F).sql.gz` and delete files older than 14 days. No sudo anywhere in units.

## 11. Acceptance — Sunday evening; the timer stays enabled only if all pass

- **A1** `psql "$DATABASE_URL" -c '\dt'` lists `ventures, work_types, signals, context_entries, context_brief, pings, calendar_seen, collector_state, schema_migrations`.
- **A2** From Claude in the IBLU Project: `context_log(type='fact', content='acceptance test', venture='blt')` returns an id; `context_search(query='acceptance')` finds it; `context_get_summary('1d')` lists it.
- **A3** Send yourself an email → within 10 min a `signals` row with `source='gmail'`, snippet ≤300 chars, no quoted text.
- **A4** Post a reply in any Chat space → `signals` row `source='chat'`, `actor='me'`, `ask_snippet` filled.
- **A5** Move a calendar event → `signals` row `kind='moved'` with `before`/`after` in `meta`.
- **A6** `python -m iblu_keeper.jobs.tick --dry` prints the composed card JSON and writes nothing; `composer='llm'` (the API key is in use).
- **A7** `--force-ping test` → card in "Secretary" with a phone notification; tap an option → "Saved" page → `context_entries` row `source='ping'` with `meta->>'choice_key'`; tap a different option on the same question → the first row gets `superseded_by`; reply in the thread → a `source='chat_reply'` row within 10 min.
- **A8** `DRY_RUN=true python -m iblu_keeper.jobs.tick` → exit 1, "refusing to run in mock mode", zero rows written.
- **A9** `pytest` green on a machine without `DATABASE_URL` (DB tests skipped, not failed).
- **A10** `STATE.md` snapshot updated: date, commit, 32 tools, Phase 2 = "recording v1 live", new env keys listed (names only).

## 12. Week-2 backlog — agreed design, do NOT build now; keep v1 compatible with it

- **`blocks`** = the reconstructed day: `(id, local_date, start, end, venture, work_type, project, attention CHECK IN ('present','displaced','ambiguous'), confidence CHECK IN ('fact','inferred'), evidence JSONB (signal ids), calendar_event_id, superseded_by)`. 15-minute floor. **Silence never equals presence** — a block with no signals is `ambiguous` until Ignas confirms it. Written by a 17:00 analyst pass, confirmed via the evening ping and the web app.
- **Secretary Google Calendar** (`SECRETARY_CALENDAR_ID`) mirrors `blocks`; the intent calendar is never edited by Iblu. Attention, not location: a 3 h "football" intent block can become 30 min present / 90 min Deadlift / 60 min ambiguous.
- **Mobile web app** `secretary.iblugames.com`: card stack in time order, drag to resize, swipe to delete, long-press to merge, each card shows the reasoning ("90 min · 3 BT creator edits + Email Writer thread open"). Pick an OSS calendar engine then, not now.
- **Analyst pass** fills `signals.summary` in batch (one line per exchange: what was asked + what I did) and reconstructs `blocks`.
- **Multi-account** — the other two mailboxes are `ignacio@chocoagency.com` and `admin@deadlift.io`, each in its own Google Workspace. Iblu's OAuth consent screen is "Internal" to blanklabel.team, so those accounts cannot authorize the existing client: each domain needs its own Google Cloud project + Internal OAuth client + one-time Allow (≈1 h per account), tokens stored as `data/token.<alias>.json`; `signals.account` already exists; `default_by_account` gets `chocoagency.com → choco`, `deadlift.io → deadlift`. Then Slack collectors (Deadlift, BLT).
- **More collectors**: Chrome profiles (Mac launchd copies each profile's `History` SQLite, POSTs to Iblu; profile = fact, URL-level = inferred), server shell history + git commits (`HISTTIMEFORMAT`), BT audit logs from the production replica (resolve client/creator/campaign **at collection time**), Mac Screen Time (`knowledgeC.db`). iPhone Screen Time is parked last.
- **Weekly quiz** (touched ≥3× — mine / someone else's / needs a process / one-off; threads I didn't start; displacement) and **monthly review**; `context_compact` + `context_brief` regeneration. Event-driven pings only after ~3 weeks of baseline.

## 13. Ignas's answers (2026-09-12) — resolved, no open questions

1. Machina = Deadlift's main software project (machina.deadlift.io, vibe-coded by Ignas) → `venture='deadlift', project='machina'`, not its own venture.
2. Claude API key: yes — Ignas pastes it himself in Session 1, first steps, via the §10 command that Claude Code prints.
3. Ping days: Monday–Friday for v1 (Claude's suggestion, not objected to); `PING_DAYS` env var flips it.
4. Other mailboxes: `ignacio@chocoagency.com`, `admin@deadlift.io` — separate Google Workspaces; week 2 (§12).

## Session plan

- **Session 1 (≈75 min)** — step 0 commit plan; step 1 secrets pasted by Ignas + API test; Postgres, `db.py`, migration 001, real context tools, tests, STATE.md → CHECKPOINT: A1, A2.
- **Session 2 (≈90 min)** — three collectors, tick job, timer with `PING_ENABLED=false`, backup timer → CHECKPOINT: A3, A4, A5, A8, A9.
- **Session 3 (≈90 min)** — composer, delivery, `/q`, reply reader; webhook URL must be in `.env` by now → CHECKPOINT: A6, A7, A10 → set `PING_ENABLED=true`.
- **Monday 07:00** — first tick runs; first ping lands between 12:30 and 14:00.
