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

### 12. Tool surface: one tool per domain × side-effect class

Restated 2026-09-13 evening by Ignas, superseding the earlier "prefer a
parameter, a tool costs a click" framing. The rule is now:

> **One MCP tool per domain × side-effect class.** Reads for a domain live in
> one tool; sends and writes for that domain live in another. Never a god tool.
> A new tool costs one Allow click, once — and that click never shapes
> architecture. New capabilities over existing data go in as parameters or
> views of the read tool for that domain.

The click is real but it is a one-time cost, and paying it is the right trade
when the alternative is a read tool that can also send, or a single `do()` with
a twenty-value `action` enum. What the click must never do is push a *write*
into a read tool to avoid a prompt.

The mechanics behind the click, verified 2026-09-13, still hold and are worth
keeping straight:

1. **claude.ai does not derive Allow/Ask from MCP annotations.** Five tools with
   byte-identical annotations showed different states in the connector UI, split
   purely by when each was first registered: tools present when the connector
   was last configured were Allow, tools that appeared afterwards defaulted to
   Ask. This is by design — the MCP spec says clients "MUST consider tool
   annotations to be untrusted unless they come from trusted servers", and
   Anthropic's permission docs state that MCP toolsets "default to always_ask …
   so that new tools added to an MCP server do not execute without approval".
   There is no server-side override: no annotation, no `_meta`, no capability.
2. **Annotations still matter — they drive the *grouping*.** The tools with
   `readOnlyHint: true` are exactly the "Read-only tools" group, which is what
   makes the group-level "always allow" control usable in one action instead of
   tool by tool. That grouping is the reason the domain × side-effect split
   above is the right shape: it keeps every read in the group Ignas can allow
   once. `tests/test_tool_permissions.py` keeps the annotations honest; it
   cannot enforce client behaviour and says so.
3. **Changing an existing tool's definition appears to reset it to Ask, too.**
   Observed 2026-09-17: after a restart, Ignas's "needs approval" list held
   the three genuinely new tools *and* seven old ones — `gdoc_read`,
   `get_context`, `gmail_list_attachments`, `gmail_read_attachment`,
   `gmail_mark_unread`, `chat_mark_read`, `server_health`. Every one of those
   had a changed schema or docstring since it was last approved (the
   `account` parameter, `structure`, the priorities in `get_context`). Treat a
   parameter change as costing the same click as a new tool, and say so.
4. **When a tool ships or changes, say so in the handover**, with the words "you will
   need to set this to Always allow in Settings > Connectors", rather than
   claiming a restart or reconnect will apply it. It will not.
5. **Three tools ask**, and only these three, because each puts a message in
   front of another human and cannot be taken back: `gmail_send_email`,
   `chat_send_message`, `gmail_reply`. A threaded reply lands in someone's inbox
   exactly as a new mail does — the thread makes it more likely to be read, not
   less.

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

### 14. "Not written by me" has two different causes — do not conflate them

A mailbox sends under aliases, and a Google Group alias appears in that same
send-as list. Both produce a From address that is not the primary one, and they
need opposite treatment:

- **Alias send — keep.** The Choco mailbox invoices as `ap@chocoagency.com`;
  BLT sends as `finance@blanklabel.team`. Ignas wrote these. Checking only the
  primary address discarded all of them.
- **Group delivery — drop.** Mail delivered through a Google Group to a member
  is filed in that member's Sent with the group in From. Someone else wrote it.

The discriminator is Google's own rewrite of the From display name:
`'Original Author' via GroupName`. Only a Group does that.

Do NOT key this on `list-unsubscribe` / `precedence: list`, even though group
mail carries them: forwarding a newsletter preserves the original's list
headers, so that rule discards genuine forwards. One was found in the Choco
mailbox the moment aliases were switched on.

Measured over 90 days: BLT keeps 14 and drops 26, Choco keeps 14 and drops 26,
Deadlift keeps all 40 (single address, no groups).

### 15. One mirror calendar, on BLT, covering every venture

Decided 2026-09-13 by Ignas. The Secretary calendar that mirrors `blocks` is a
single calendar on blanklabel.team covering all ventures — not one per
Workspace. The analyst writes Deadlift- and Choco-detected work into it.

One place to look, and cross-venture questions ("did Deadlift eat my BLT week?")
are answered by reading one calendar rather than joining three.

Two consequences:

- IBLU needs multi-account calendar **reads** — each Workspace's meetings are
  evidence of where attention went — but **not** multi-account calendar writes.
  The `calendar` tool stays primary-only until something actually needs
  otherwise.
- The mirror must be a **separate calendar, never the BLT primary**. It will
  contain Choco and Deadlift work, and a shared primary would expose one
  client's activity to another's colleagues. `SECRETARY_CALENDAR_ID` names it.

### 16. `blocks` — the reconstructed day, and the three rules it encodes

Built 2026-09-13. `python -m iblu_keeper.jobs.analyst` clusters a day's signals
into blocks and mirrors them onto `SECRETARY_CALENDAR_ID`. Timer: weekdays
17:00 and 20:15 Europe/Zagreb (the later run supersedes the earlier one, so the
evening's signals are not lost).

Three things in there are deliberate and must survive any rewrite:

**Silence produces no block.** An unobserved hour is unknown, not idle. The
only time IBLU emits a block with no evidence is where the *calendar* claimed
the time and nothing happened — that block is `ambiguous`, and it is named
after the event it failed to account for ("? Womanizer alignment"), because
"unattributed" says nothing a human can act on.

**An intent's venture comes from the event, never from whose calendar it is.**
`venture_hints.infer` falls back to "whose mailbox was this", so calling it
with the account would give every event on Ignas's calendar `venture='blt'` —
making a flight, a dentist appointment and a client call all claim BLT's time,
and making `displaced` fire at random. `load_intents` passes `account=""` on
purpose. The consequence is the right one: an intent with no venture of its own
can never cause displacement. A flight is not a claim on his attention; a
client meeting is.

**Evidence belongs to the slice it happened in.** A cluster cut at an intent
boundary must split its signals, not hand the whole cluster's signals to each
half — the first version did, and a 15-minute block claimed the 32 messages of
the three hours around it. `build()` selects by timestamp; the reasoning line
is written *after* merging so its minutes are the surviving block's own.

Rebuilding a day never deletes: old blocks get `superseded_by`, and `_clear()`
removes only mirror events whose id IBLU itself recorded in
`blocks.calendar_event_id` — so a human's own entry on that calendar, or a row
whose event was already deleted by hand, can never turn into a wrong deletion.

### 17. Slack is collected with a USER token, not a bot token

Slack exposes `search.messages` only to a user token (`xoxp-`) — a bot token
cannot search at all, and no app install substitutes for it. So IBLU
authenticates as Ignas in each workspace. One scope: `search:read`.

`after:` is date-granular, so the query searches from the day *before* the
watermark and filters precisely in Python; `UNIQUE(source, source_ref)` makes
the overlap free. The search result already carries the channel's id, name and
type, so no `conversations.info` call per message — and therefore no
`channels:read`/`users:read` scope.

Venture attribution is inverted relative to the other collectors: a keyword hit
is `inferred`, but the **workspace itself** is `fact` (`SLACK_VENTURE_<ALIAS>`).
Deadlift's Slack *is* Deadlift; that is stronger evidence than a word in a
message.

### 18. The judge relabels; it never reshapes

Built 2026-09-13 (`analyst/judge.py`). The reconstruction is split in two on
purpose: `blocks.build()` decides *where* the day's boundaries are, and an LLM
decides *what each stretch was*. Boundaries are arithmetic over timestamps and
must be reproducible; "Re: Noshinku 3PL training is BLT client work, not Choco
delivery" is a judgement no rule table will ever make well. "Scripts fetch; the
LLM judges" is this line, drawn concretely.

Four things the judge may not do, each enforced *after* the call rather than
asked for politely in the prompt:

- **Move a boundary.** Start and end come back unchanged or the whole response
  is rejected. A model that can reshape the timeline can invent an hour of work.
- **Upgrade confidence.** Only a tap makes a block a `fact`.
- **Invent a code.** Every venture, work type and project it returns must
  already exist. (Projects are the exception while the registry is empty —
  before there is a registry there is nothing to contradict.)
- **Fill in an untracked stretch.** An untracked block has no evidence at all;
  guessing what an unobserved hour was would make every other number in IBLU
  unbelievable. This one is checked first, before any other field is applied.

The duration stays IBLU's: the judge supplies only the "why", and the `N min ·`
prefix is prepended afterwards. Letting it rewrite the whole reasoning line lost
the minutes, which is the one number a glance at the card actually needs.

Any violation, timeout or malformed response is a silent no-op — the computed
day stands and `llm=false` is recorded, so a later reader can tell which days
were judged and which were merely counted.

### 19. Gap warnings advise; they never block

Built 2026-09-13 (`store/gap_check.py`). When a `decision` or `preference` is
written in Gap language — a superlative, a comparison to another company, a
race, an obligation, a distance from an ideal — `context_log` returns a
`gap_warning` alongside the id, suggesting a backward-measurable rewrite.

**The entry is always written first.** A warning that could block would make
IBLU a grader, and IBLU measures; it does not grade. Ignas's goals are his to
phrase; the tool only says once what that phrasing will cost him when the year
is up and there is nothing to measure backward to.

The stricter "names no observable end state" check applies only to entries
tagged `priority`. An ordinary decision is allowed to be a sentence about a
choice.

Note there are two language checkers and they have different jobs: this one
guards what *Ignas writes into IBLU*; `jobs/review_language.py` guards what
*IBLU writes back to him*. Keep them separate — the first advises, the second
rejects and falls back to a deterministic template.

### 20. Sessions 5–8 vs. the brief: what the repo already had

The Sessions 5–8 brief (`docs/plans/2026-09-13-sessions5-8.md`) was written
against `d1e8958`, eight commits behind `main` at the time it was executed. Rule
1 of that brief says the repo wins. Where it won, for anyone reading the brief
later and wondering why the code does not match:

| The brief says | The repo had | Resolution |
|---|---|---|
| stages/projects = migration 004 | 004 is multi-account calendar | stages/projects became **007** |
| build `blocks` as migration 005 | 005 built, 006 added `intent_title` | extended, not rebuilt |
| `blocks.start_at` / `end_at` / `created_by` | `starts_at` / `ends_at` / `source` | repo names kept |
| `IBLU_ACCOUNTS='blanklabel:…'` | `GOOGLE_ACCOUNTS='blt,deadlift,choco'` + per-alias `GOOGLE_ACCOUNT_<ALIAS>_*` | repo names kept; primary alias is `blt`, not `blanklabel` |
| Session 6 = build multi-account | all three Workspaces already authorized and collecting | only the read tools' `account` parameter and per-account health remained |
| create the Secretary calendar | created and wired (`129b3fa`) | nothing to do |
| weekly review on Sunday | Sunday 18:00 | moved to **Friday 18:00** per the brief; window Mon 00:00 → Fri 18:00 |

One place the brief won over the repo, deliberately: **untracked blocks.** The
repo emitted no block at all for an unobserved stretch, on the reasoning that
inventing one invents a fact. The brief wants a `venture=NULL`, `ambiguous`
block for any unaccounted stretch of at least 30 minutes inside 07:00–20:00 —
and it is right, because the evening ping's `gap` question needs something to
supersede when Ignas says what the hour was. Both readings honour "silence is
never presence"; only the brief's gives him a way to answer.

### 21. IBLU keeps a log of what it caught itself getting wrong

Built 2026-09-13, after Ignas asked "do we have some log of observations?" The
answer was no, and the gap was bigger than it looked: roughly thirty places
noticed something real — an LLM response rejected, a collector erroring for one
account, a review failing its own language check — and wrote a `logger.warning`
that rotated out of the journal. Nobody could answer "what has IBLU been quietly
catching?", which is the first question any later session has.

`observations` (migration 008) is that answer, and `store/observations.py` is
the only way in. Four things about it are deliberate:

**Recording must never break the thing being observed.** `record_safe`
swallows every exception including the database being down. A watchdog that can
take down what it watches is worse than no watchdog.

**A rule and a model are different witnesses.** `detected_by='rule'` means an
invariant failed — a fact. `detected_by='llm'` means the sense-check pass
thought something looked wrong — a lead. Nothing downstream may merge them, and
the generated document says so at the top, because a later reader will act on
these.

**Deduplicated on a caller-supplied fingerprint**, never on the message text.
An exception string usually carries a timestamp or an id, so fingerprinting on
it opens a fresh row every run. For LLM findings the fingerprint is
`(kind, date)` only — the same problem described in slightly different words is
the same problem. This is also why `run_llm` forces `kind` into a closed list:
the first version let the model invent slugs and got `venture_misattribution`
and `misattributed_venture` as two separate findings of the same thing.

**Resolving is a state change**, never a delete, with a required note saying
what was done.

`analyst/sensecheck.py` runs both passes at the end of every analyst run.
`docs/OBSERVATIONS.md` is generated from the table (untracked — the database is
the source of truth) for a session that would rather read a file than query.

The check model is separate from the composer model: `IBLU_CHECK_MODEL`
defaults to `claude-opus-5` while `IBLU_LLM_MODEL` stays on Sonnet. The composer
runs every tick and writes four short questions — frequent, cheap, and wrong in
a way Ignas sees immediately. The judge and the sense-check run twice a day and
decide what the record *says* he did; a mistake there is silent and becomes the
history everything later is measured against.

**It earned its place on the first run.** It found a signal built from an
acceptance test — a mail to `a@b.com` titled "subj" — being counted as fifteen
minutes of Saturday admin, and it found that `analyst_blocks` had owned a
`collector_state` row since migration 005 and never written to it, so "has the
analyst run?" had no answer. Both are fixed; see migration 009 for why the test
signal was excluded rather than deleted.

### 22. What the 2026-09-13 code review found

Two reviewers over the analyst/review layer and the pings/data-integrity layer.
Eleven findings, all real, all fixed the same evening. Four are worth carrying
forward as rules rather than as fixes.

**One collector's database error used to roll back the whole tick.** Every
collector ran inside one shared transaction. `run_all` caught each exception and
carried on — which works for a Python error, but a Postgres error (a bad venture
code violating the foreign key, say) poisons the transaction: every later
statement raises `InFailedSqlTransaction`, *including* the `set_state` that
tries to record the failure, and the final `commit()` silently rolls back every
signal the earlier collectors had already inserted, while the tick's log line
still reports them as collected. "A failing collector never stops the others"
was true only for Python-level errors. Each collector now runs in its own
`SAVEPOINT` (`with conn.transaction():`), and so does each day of a multi-day
analyst backfill.

> **Rule: any loop that must survive one iteration failing needs a savepoint,
> not just a `try`.** A shared connection makes `except` a lie at the database
> level.

**The gains validator was defeated by its own truncation.** Labels are cut to
40 characters by a Pydantic *field* validator, which runs before the *model*
validator that checks whether the text is a plan. So "Signed three clients,
**will** announce the plan next month" became a 40-character prefix with the
disqualifying word chopped off, passed the gate, and would have reached his
phone as a gain. Two fixes: validate the full text before truncating, and
refuse any gains option that arrives already truncated — a sentence with its
ending removed cannot be checked, and a gain that does not fit on a button was
never a good option.

> **Rule: validate before you truncate.** Anywhere else this pattern appears,
> assume the same bug.

**The webhook key could reach the log and the database.** `requests` embeds the
failing request in a connection error, and `SECRETARY_WEBHOOK_URL` carries its
key in the query string. That message was logged *and* persisted into
`pings.meta`, so one DNS hiccup put the secret in two durable places. The
status-code branch had always been careful; the connection-error branch was
not. `pings.deliver._scrub` now redacts absolute URLs, anything introduced as
`url: `, and the value of any obviously secret parameter — three passes,
because the first version only caught absolute URLs and urllib3 actually
reports the *path*.

> **Rule: never interpolate an exception from an HTTP client into a message
> that is stored or logged without scrubbing it.**

**`occurred_at::date` truncates in the session timezone, which is UTC.**
Compared against a Zagreb date, anything logged between midnight and 02:00
local landed on the previous day and vanished from that evening's gains. Both
occurrences now compare against an explicit local-day range.

The rest, fixed without needing a rule: `_carve_out` copied reasoning and
evidence onto both halves of a split block (reintroducing the exact bug
`build()` exists to avoid, one step later); `_supersede` left old blocks live
when a rebuild produced no rows; `reconstruct` had no lock, so the 17:00 and
20:15 runs could overlap into two live generations; a 23:58 signal produced a
block ending at 00:15 filed under the wrong `local_date`; `mirror._clear`
treated a 503 as "already gone" and orphaned the event permanently; `review()`
ignored its own `until` bound; `minutes_from_blocks` counted a block that
straddled the window edge in full; the judge's free-text `reasoning` was the one
thing IBLU writes back that skipped the language gate — and email subjects are
interpolated into that prompt; the weekly review appended its low-signal
disclaimer *after* the final gate; migration 007 was not re-runnable.

**And one finding about the tests themselves.** `tests/test_ping_cards.py`'s
`FakeConn` matched on the first words of each statement and then applied its own
hand-written supersede logic — so if the real SQL lost `AND superseded_by IS
NULL`, every one of those tests would still have passed. The tap path had never
run against a real database. `tests/test_tap_roundtrip.py` now does, inside a
transaction that rolls back.

> **Rule: a fake that reimplements the logic under test proves nothing.** When
> the behaviour *is* the SQL, test the SQL.

### 23. A calendar title is not evidence, and nobody was watching the watchman

Two fixes on 2026-09-15, both traceable to the same root: IBLU could be wrong
for days without anything noticing.

**The judge was labelling blocks it had no evidence for.** "Go pickup Emory",
"Emory hosting" and "dinner with emory" came back as 420 minutes of
`blt/client`. Every one of those blocks had zero signals: `build()` never
assigns a work type or project to a calendar-derived block, so the labels came
from the model reading the title. The untracked guard missed it because a block
*with* an intent is not untracked — the intent is exactly what made it look
attributable.

The judge is now graded per block by what is behind it:

| grade | behind it | it may set |
|---|---|---|
| `none` | a calendar title only | the reasoning line, nothing else |
| `silent` | signals with no readable text | venture only |
| `full` | at least one signal with a subject or body | everything |

The grade is shown in the prompt *and* enforced after the response, because a
rule asked for politely is not a rule. A sense-check invariant checks it is
still working — scoped to calendar-derived blocks, since a cluster cut at an
intent boundary can legitimately leave a slice with no signals of its own while
inheriting the surrounding stretch's attribution. That is a continuation, not an
invention.

> **Rule: a title says what was meant to happen. Only a signal says what did.**

**And the watchdog.** `jobs/watchdog.py` checks the machine — units, disk, tick
freshness, backups, per-account token refresh, analyst freshness — and posts
open errors to the Secretary space. It exists because Deadlift and Choco were
dead for 135 consecutive ticks while logging the error correctly every time.

Four things about it are deliberate:

- **Checks become observations; alerts are a view of the log.** One pipeline —
  check, record, announce — not a second alarm system with its own memory.
- **Errors only.** A warning is something to read on Sunday. An error means
  IBLU is not recording.
- **Announce once, then only after six hours, and never between 21:00 and
  08:00.** An alert that repeats every half hour is muted within a day, which
  is the same as no alert at all, only louder. An error found at 03:00 is
  queued, not lost.
- **A failed send does not stamp `alerted_at`.** The next run must try again;
  a silent alerter is the exact thing being guarded against.

The tick is Mon–Fri 07:00–19:50, so `check_tick_freshness` only complains
inside that window. A quiet Sunday is the timer working.

### 24. Where a day's plan comes from, and when silence counts as family time

Built 2026-09-17, after Ignas looked at the Secretary calendar and judged about
half of it wrong or unaccounted. Measured on 2026-09-16: 210–270 unaccounted
minutes a day, and nearly all evidence was BLT chat and BLT mail.

**Intents now come from every calendar that describes his plan.** The three
workspace primaries, plus `INTENT_CALENDARS` (format
`venture:calendar_id[|account]`, comma-separated — `|` because calendar ids
contain `@`). Both configured calendars are `family`, never `personal`:
`personal` is the venture "Own tooling & infra", which is work. That mix-up has
now been caught twice.

The Deadlift and Choco primaries default to their own venture — they are work
calendars. **The BLT primary gets no account default**, because it holds
flights and school runs; an event there has a venture only if its title,
attendees or the classifier say so. The same meeting on two calendars counts
once (same `iCalUID`, or same title and times; primaries win).

**An Opus classifier labels events nothing else could** (`analyst/intents.py`):
venture, whether it is work, and — for the family calendars — whether it is
Ignas's own commitment. Results are cached in `intent_labels`, keyed on
calendar + event + title, so a rebuild cannot flip a label.

**A shared family calendar is mostly other people's plans.** "Greta nicoj" is
his wife's; "Futbolas" is his son's football; "Ignas LT" is a note of where he
is. None of those is an activity he committed to, and treating them as intents
would mark his work `displaced` whenever his wife had an appointment. So every
`INTENT_CALENDARS` event is *context* — no block, no displacement, no minutes —
unless the classifier says, at high confidence, that it is an activity he takes
part in. Whereabouts markers stay context even when they name him. Any event
longer than 12 hours is context on every calendar.

**The one place IBLU turns silence into presence** — Ignas's rule, and the
mission now says so: a family commitment, on a day the recorder was watching,
with little or no work during it, is inferred to have happened. The quiet parts
of the span become `present / family / inferred`, reasoning always saying
"assumed". Four guard-rails, each of which leaves the remainder `ambiguous`:

- work covering 60% or more of the span (then it probably did not happen) —
  counting every non-family slice with evidence, not only `displaced` ones,
  because work with no clear venture is never marked displaced;
- a remainder under 20 minutes (a gap between two bursts of chat);
- no work signal anywhere else that day (a quiet afternoon and a dead
  collector look identical);
- anything after the newest collected signal (the evening may not be in yet —
  which is also why the analyst now rebuilds yesterday as well as today).

Work meetings are untouched: silence during one stays `ambiguous`.

**Labels need agreement, not a plurality.** A project or work type needs 60%
of the slice's opinions and those opinions must cover half the slice; venture
needs a real majority. A 135-minute stretch of invoices, company setup and one
client thread had been tagged `email-writer` because that was the busiest chat
space. A zero-signal slice inside a meeting is `ambiguous`, not an inherited
`present`. And the judge may not quote a calendar title that is neither its own
block's intent nor in its own evidence.

**Git commits are evidence** (`collectors/git_commits.py`, migration 011):
local checkouts and GitHub, author-matched against `GIT_AUTHOR_EMAILS` — which
must include `ignas.ignas@gmail.com`, an address that is not one of his Google
accounts. Venture comes from the repo (`REPO_VENTURES`), confidence fact.
Commits AUTHORED by an agent (`noreply@anthropic.com`) are skipped — a cloud
session committing at 03:00 is output, not attention — while commits he made
with a Claude co-author are kept and flagged. A commit marks the END of a
stretch, so coding time is still under-counted; that is known.

**And a migration lesson, the second of its kind.** `db migrate` applied every
pending file, so applying a finished 011 also applied a 012 another worker was
still writing. `migrate --only <version>` now exists and `migrate` warns when
more than one is pending.

### 25. The ping asked about other people's mail, in the wrong timezone

Found 2026-09-25 by Ignas: the midday ping asked "Opera/DixiVobis contract
thread (PandaDoc, Sofie, 13:10–13:41) … Was that yours to do?" It was not his.
Two independent bugs in one question.

**Inbound mail counted as his attention.** `pings.runner._window_signals`
selected every signal in the window with no `actor = 'me'` filter. Twelve
PandaDoc notifications to the `contracts@blanklabel.team` group — all recorded
correctly as `actor='other'` by the collector, which did its job — became the
biggest "thread" of his window. The review has separated demand from attention
since it was written (`WHERE actor = 'me'`, and a separate `inbound_demand`);
the composer never did. Roughly 50 of 794 signals a week were other people's,
and they were competing to be the day's headline.

> **Rule: anything that claims to describe HIS attention filters
> `actor = 'me'`.** The collectors label it correctly; every consumer must
> honour the label.

**Times were rendered in UTC.** `signal_lines` formatted `occurred_at`
straight from Postgres, so the model was told 13:10 for a message that arrived
at 15:10 in Zagreb — and wrote that time into the question. `_local()` already
existed and was used elsewhere.

Three of his tapped answers rest on questions built this way. Today's was
superseded by a `correction` entry rather than deleted; the two from 09-15 were
left for him to judge, since "was chasing that signature yours to do?" may
still be a question he meant to answer.

**And a test that aged out.** Every test in `tests/test_git_commits.py` failed
on 2026-09-25 without anyone touching the code: the fixtures used hard-coded
mid-September dates and the collector only looks back seven days. Fixture
timestamps are now relative to `now`.

> **Rule: a fixture that carries a date is a fixture with an expiry.** Anchor
> it to `now` unless the test is specifically about a fixed point in time.
