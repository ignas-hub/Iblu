# Why so much was wrong, and what changes

Written 2026-09-27, after Ignas said: *"there were so many problems, bugs. You
have to improve."* He is right, and the record is not flattering.

## The record

Twenty-six commits since the recording layer went in. Who found the problems:

| Found by | Classes of problem |
|---|---|
| **Ignas, using it** | **6** |
| IBLU's own sense-check | 4 |
| A commissioned code review | 1 finding session, 11 defects |
| **The test suite** | **0** |

The suite grew from 236 tests to 746 over the same period and was green at
every single moment one of those bugs was live. That is the finding. Not "we
had bugs" — every system has bugs — but that **the tests could not see them**,
so the only working detector was the user.

## Why the tests were blind

Each of these is a real pattern in this repo, with a real example:

**They tested functions, not artifacts.** Nothing ever asserted "the
reconstructed day for 16 September is plausible" or "this question names
something he actually wrote". The product is a day and a set of questions;
neither was ever the subject of a test.

**They asserted on data that had not been validated yet.** The gains card was
tested as a dict. The dict was fine. Turning it into a `Question` raised,
because it had one option and a question needs two — so on 16 September no
evening ping was sent at all, on every tick for ninety minutes, and the tests
stayed green.

**Fakes reimplemented the logic under test.** `tests/test_ping_cards.py`'s
`FakeConn` matched a SQL prefix and then applied its own correct supersede
rule. If the real SQL had lost `AND superseded_by IS NULL`, every one of those
tests would still have passed. The tap path had never run against Postgres
until a reviewer pointed it out.

**Eight tests assert source text** via `inspect.getsource`. That is a
confession: the behaviour was not reachable, so the code was checked instead.

**Fixtures pinned the bug.** When the quiet-day gains card was "fixed" to
always appear, a test was written asserting its single option. The test encoded
the crash.

**Fixtures had an expiry.** Every git-collector test failed on 25 September
with nobody having touched the code: hard-coded September dates, a seven-day
look-back window.

## Why the code was wrong

The bugs themselves cluster into five kinds, and the kinds matter more than the
instances:

1. **One word meaning two things.** `confidence='fact'` meant both "the
   evidence agrees" and "Ignas confirmed it by tapping" — so a rebuild froze
   its own guesses as if he had blessed them. `personal` is a venture meaning
   *own tooling*, while `life` is a work type meaning *not work*; reading one
   as the other offered him his own code as a lived experience. Twice.
2. **Consumers ignoring labels the collectors set.** The Gmail collector marks
   inbound group mail `actor='other'` and always has. The ping composer
   selected every signal in the window regardless, so twelve PandaDoc
   notifications became "the thread that took your window. Was that yours?"
3. **Defaults leaking where they do not belong.** "It arrived in the BLT
   account" became "it is BLT work", which is true for mail and false for a
   calendar that holds school runs and flights.
4. **UTC where local was meant.** Twice: `occurred_at::date` grouping a day,
   and question times printed straight from Postgres — 13:10 for a message
   that arrived at 15:10.
5. **Silent degradation.** Fallbacks everywhere, alarms nowhere. The Anthropic
   balance ran out and every LLM step quietly switched to templates. The
   watchdog said nothing, because that failure was recorded at `info`.

## What changes

**Two new detectors, neither of which is a unit test.**

- `src/iblu_keeper/testing/scenarios.py` — whole days described as data, run
  through the real pipeline, asserting on the resulting blocks and questions.
  Every scenario is drawn from a defect that actually reached him.
- `python -m iblu_keeper.jobs.audit` — structural invariants, a question audit
  and a shadow-calendar accuracy measure, run over the real recorded data, with
  findings going to `observations` so the watchdog can raise them.

**Four rules, in HANDOFF.md, each earned:**

- Anything claiming to describe his attention filters `actor = 'me'`.
- A title says what was meant to happen; only a signal says what did.
- Validate before you truncate; test through the validator, not before it.
- A fixture carrying a date has an expiry — anchor it to `now`.

**And one standing check on myself:** when a term starts meaning two things —
`fact`, `personal`, `commitment` — that is the moment to split it, not after it
has produced two bugs.

## What this does not fix

The suite is still mostly function-level, and converting it wholesale would be
make-work. The scenarios and the audit are the layer that was missing; existing
tests stay as they are unless they are actively misleading. Three that pinned
bugs have been rewritten. The eight `inspect.getsource` assertions are on the
list to replace with behavioural equivalents, not yet done.
