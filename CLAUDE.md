# IBLU — notes for a Claude Code session

IBLU is Ignas's personal chief-of-staff: a FastMCP server plus Postgres on one
Hetzner box, which records what he actually spent his attention on and asks him
about the gaps. `docs/MISSION.md` is why it exists, `STATE.md` is where it got
to, `HANDOFF.md` is the decision record — read those before changing design.

This file is the part that binds every session.

## The observation log

IBLU writes down what it catches about itself, in the `observations` table.
Two witnesses, never merged:

- **`detected_by='rule'` is a fact.** An invariant failed. It reproduces.
- **`detected_by='llm'` is a lead.** The sense-check pass thought something
  looked wrong. It may be a real defect, a day since rebuilt, or the model
  being wrong.

`jobs/session_brief.py` puts the open findings in front of you at session
start, through the `SessionStart` hook in `.claude/settings.json`. That is the
whole point of the log: most of what IBLU catches needs a code change, and
Ignas cannot make one. Anything he *can* act on — a dead unit, an expired
token, a full disk — the watchdog messages him about instead. **Never widen the
Chat alert to leads.** He spent a fortnight being woken by findings he could do
nothing about, and that is what this split exists to stop.

When the brief shows something:

1. **Reproduce it against real data first.** A lead is a hypothesis. Query the
   signals, read the block, check the headers. Several have turned out to
   describe a day that was rebuilt hours later.
2. **Write the failing test before the fix.** A day-shaped bug becomes a
   scenario in `src/iblu_keeper/testing/scenario_data.py`
   (`python -m iblu_keeper.testing.scenarios`); anything else becomes a pytest
   case. The scenario must fail for the stated reason before you touch the fix.
3. **Fix the cause, not the day.** Rebuilding one day makes the finding go away
   and leaves the defect in. Ask what class of day produces it.
4. **Close it with what you did:**
   `python -m iblu_keeper.store.observations --resolve <id> --note "..."`.
   Resolving never deletes — the history is the point.
5. **Say so when you did not check.** "I could not reproduce #1103 and left it
   open" is a useful sentence. Silently closing a lead is not.

A `logger.warning` that reports a defect is a bug in itself: it rotates out of
the journal addressed to nobody. Make it an observation
(`observations.record_safe`) so it reaches a session that can fix it.

## Hard rules

**Secrets.** Ignas pastes them; you never do. Print the exact `read -rs`
command for him to run. Verify presence with `grep -c` or `test -s` — never
`cat`, `echo`, or log a value. Never commit `.env`, `data/token.json`, or any
credential. `.env` values containing `&` stay single-quoted.

**Never fabricate data.** `DRY_RUN=true` means exit non-zero and write nothing.
No mock rows in the database, ever. If a number cannot be measured, say it
cannot be measured — `jobs/audit.py` refuses to print a percentage with an
empty denominator, and that is the standard.

**Never send on his behalf** — no real Chat message or email — unless he says
"go". The ping and watchdog systems sending on their timers are the designed
exception. The Secretary space is the *only* Chat space IBLU may ever post to.

**Corrections supersede, never delete** (D9). A block he has confirmed is
protected by its `source` (`ping`/`human`), not by its confidence.

**Don't touch:** docker volumes, running containers, `~/Radovi`, `~/.claude`,
`/home/ignas/iblu/data/`. No `sudo` inside services or timers (`User=ignas`).

**Anthropic calls:** never pass `temperature` or other sampling params — the
API returns 400. Opus (`IBLU_CHECK_MODEL`) judges and sense-checks; Sonnet
(`IBLU_LLM_MODEL`) composes.

**MCP tool annotations control grouping only, never permissions.** Changing a
tool's schema resets it to "Ask" in his client.

## When he has to do something by hand

Give him a **direct URL** and the **exact clicks** — the literal button labels,
in order, and the value to set. Say which account to be signed in as first. He
runs three Google Workspaces and several Cloud projects, so "open Settings" is
ambiguous about which, and a wrong guess costs a round trip. If no deep link
exists, say so and give a way for him to confirm he is on the right page.

## Working shape

- `python -m pytest -q` — the suite, and it should be green before a commit.
- `python -m iblu_keeper.testing.scenarios` — the golden days.
- `python -m iblu_keeper.jobs.audit` — accuracy of the reconstruction.
- `python -m iblu_keeper.jobs.session_brief --text` — this brief, on demand.
- `.venv/bin/python` on the box; migrations are SQL in `db/migrations`, applied
  with `python -m iblu_keeper.db migrate` (`--only N` for one).

He reads reports, not commentary. Say what changed, what you measured, and what
you could not measure.
