"""The golden scenarios themselves — each drawn from a real defect.

To add one: write a `Scenario` (or `QuestionScenario`), append it to
`SCENARIOS` (or `QUESTION_SCENARIOS`), run
`.venv/bin/python -m iblu_keeper.testing.scenarios --name your_scenario_name`.
That's the whole workflow — `tests/test_scenarios.py` picks it up
automatically because it parametrises over these two lists.
"""

from __future__ import annotations

from .scenarios import QuestionScenario, Scenario

# --- tiny, readable constructors — dicts, not a DSL -------------------------


def signal(time: str, **kw) -> dict:
    return {"time": time, **kw}


def intent(title: str, start: str, end: str, **kw) -> dict:
    return {"title": title, "start": start, "end": end, **kw}


def block(start: str, end: str, **kw) -> dict:
    return {"start": start, "end": end, **kw}


def maybe_event(instance_key: str, title: str, start: str, end: str) -> dict:
    return {"instance_key": instance_key, "title": title, "start": start, "end": end}


# ---------------------------------------------------------------------------
# day scenarios
# ---------------------------------------------------------------------------

SCENARIOS: list[Scenario] = [

    Scenario(
        name="school_run_is_displacement",
        description=(
            "BLT chat happening during a family 'Go school' commitment is "
            "displacement, not presence — a commitment claims the time even "
            "when what actually filled it was work."
        ),
        signals=[
            signal("09:00", source="chat", venture="blt", work_type="client",
                   subject="Vendor ping", counterpart="Marta", container="spaces/blt-ops"),
        ],
        intents=[
            intent("Go school", "08:45", "09:15", calendar_kind="family_calendar",
                   attendance="his", event_id="school1"),
        ],
        expect=[
            {"kind": "span", "start": "09:00", "end": "09:15",
             "attention": "displaced", "venture": "blt", "reasoning_contains": "Go school"},
        ],
    ),

    Scenario(
        name="quiet_family_event_is_assumed",
        description=(
            "A MAYBE family intent with nothing recorded during it, on a day "
            "the recorder was otherwise running, is assumed to have happened "
            "— present/family/inferred, reasoning says 'assumed'."
        ),
        signals=[
            signal("09:00", source="gmail", venture="blt", work_type="build"),
        ],
        intents=[
            intent("Futbolas", "16:00", "16:45", calendar_kind="family_calendar",
                   attendance="maybe", event_id="fut1"),
        ],
        expect=[
            {"kind": "span", "start": "16:00", "end": "16:45", "attention": "present",
             "venture": "family", "confidence": "inferred", "reasoning_contains": "assumed"},
        ],
    ),

    Scenario(
        name="busy_family_event_is_not_family",
        description=(
            "The same MAYBE span, but work covers most of it: no family block "
            "appears at all, and the work itself reads present — a maybe-event "
            "can never displace, confirmed or not."
        ),
        signals=[
            signal("16:00", source="chat", venture="blt", work_type="client",
                   subject="Client fire", counterpart="Ana", container="spaces/blt-ops"),
            signal("16:20", source="chat", venture="blt", work_type="client",
                   subject="Client fire", counterpart="Ana", container="spaces/blt-ops"),
        ],
        intents=[
            intent("Futbolas", "16:00", "16:45", calendar_kind="family_calendar",
                   attendance="maybe", event_id="fut2"),
        ],
        expect=[
            {"kind": "span", "start": "16:00", "end": "16:30", "attention": "present", "venture": "blt"},
            {"kind": "no_block_between", "start": "16:00", "end": "16:45", "venture": "family"},
        ],
    ),

    Scenario(
        name="mixed_stretch_gets_no_project",
        description=(
            "One cluster whose three signals each name a different project "
            "never gets to claim the whole stretch for the busiest one — the "
            "email-writer smear (LABEL_AGREEMENT: 60% of the votes, not a "
            "plurality)."
        ),
        signals=[
            signal("09:00", source="gmail", venture="blt", project="invoices"),
            signal("09:10", source="gmail", venture="blt", project="itin-setup"),
            signal("09:20", source="gmail", venture="blt", project="client-thread"),
        ],
        expect=[
            {"kind": "span", "start": "09:00", "end": "09:30", "venture": "blt", "project": None},
        ],
    ),

    Scenario(
        name="calendar_edit_alone_is_not_work",
        description=(
            "Three calendar-edit signals (source='calendar') and nothing else "
            "is real activity but not a stretch of attention — no block at "
            "all, not a 30-minute present/blt guess for ten seconds of admin."
        ),
        signals=[
            signal("07:05", source="calendar", venture="blt"),
            signal("07:06", source="calendar", venture="blt"),
            signal("07:07", source="calendar", venture="blt"),
        ],
        expect=[
            {"kind": "no_block_between", "start": "00:00", "end": "23:59"},
        ],
    ),

    Scenario(
        name="zero_signal_slice_in_a_meeting_is_unknown",
        description=(
            "A cluster cut at an intent boundary can leave an empty slice "
            "inside the meeting — that slice is ambiguous, never an inherited "
            "'present' borrowed from the surrounding stretch."
        ),
        signals=[
            # 10:07 (not on the 15-minute grid) widens, via TAIL + ceiling,
            # into a two-cell cluster (10:00-10:30) with real evidence only
            # in the first cell.
            signal("10:07", source="chat", venture="blt"),
        ],
        intents=[
            intent("Alexan Ignas Emory sync", "10:15", "10:45",
                   calendar_kind="workspace_primary", venture="family", event_id="sync1"),
        ],
        expect=[
            {"kind": "span", "start": "10:00", "end": "10:15", "attention": "present", "venture": "blt"},
            # Merges with the intent's own unaccounted tail (10:30-10:45,
            # same venture/attention/title) — one honest "nothing happened
            # during this meeting" block, not two.
            {"kind": "span", "start": "10:15", "end": "10:45", "attention": "ambiguous",
             "venture": "family", "work_type": None, "project": None, "evidence": [],
             "reasoning_contains": "was on the calendar"},
        ],
    ),

    Scenario(
        name="overlapping_calendar_events_do_not_double_count",
        description=(
            "Two calendar events sharing a span ('Emory hosting' and 'dinner "
            "with emory', both 15:45-) must not each produce their own "
            "ambiguous block for the same minutes — Ignas double-books "
            "constantly; this is the normal case, not an edge one."
        ),
        intents=[
            intent("Emory hosting", "15:45", "16:00",
                   calendar_kind="workspace_primary", venture="blt", event_id="host1"),
            intent("dinner with emory", "15:45", "17:30",
                   calendar_kind="workspace_primary", venture="family", event_id="dinner1"),
        ],
        expect=[
            {"kind": "no_overlaps"},
            {"kind": "covers", "start": "15:45", "end": "17:30"},
            {"kind": "total_minutes", "attention": "ambiguous", "equals": 105},
        ],
    ),

    Scenario(
        name="late_night_signal_stays_in_its_day",
        description=(
            "A signal at 23:58 widens (TAIL + flooring) past local midnight — "
            "the resulting block must be clipped to ITS day, never stored "
            "as a block that straddles two local dates."
        ),
        signals=[
            signal("23:58", source="gmail", venture="blt"),
        ],
        expect=[
            {"kind": "total_minutes", "equals": 15},
        ],
    ),

    Scenario(
        name="inbound_only_window_produces_no_attention",
        description=(
            "A window containing only inbound (actor='other') signals is "
            "demand, not his attention (HANDOFF §25) — it produces no "
            "evidence block at all, not a present/blt guess from someone "
            "else's messages."
        ),
        signals=[
            signal("11:00", source="gmail", venture="blt", actor="other", counterpart="PandaDoc"),
            signal("11:05", source="gmail", venture="blt", actor="other", counterpart="PandaDoc"),
        ],
        expect=[
            {"kind": "no_block_between", "start": "00:00", "end": "23:59"},
        ],
    ),

    Scenario(
        name="unattributed_work_still_blocks_family_inference",
        description=(
            "Work with no clear venture is never marked displaced — "
            "displacement needs a KNOWN venture — but it must still count "
            "against the family-inference density guard, or an afternoon of "
            "unattributable work gets claimed as family time."
        ),
        signals=[
            signal("07:00", source="gmail", venture="blt"),
            signal("10:00", source="gmail", venture=None),
            signal("10:10", source="gmail", venture=None),
            signal("10:20", source="gmail", venture=None),
            signal("10:30", source="gmail", venture=None),
            signal("10:40", source="gmail", venture=None),
        ],
        intents=[
            intent("Futbolas", "10:00", "11:10", calendar_kind="family_calendar",
                   attendance="his", event_id="fut9"),
        ],
        expect=[
            {"kind": "span", "start": "10:00", "end": "11:00", "attention": "present", "venture": None},
            # An honest, unconverted "nothing recorded during Futbolas"
            # remainder is fine — what must never appear is the INFERENCE
            # claiming this busy span as present/family.
            {"kind": "no_block_between", "start": "10:00", "end": "11:15",
             "venture": "family", "attention": "present"},
        ],
    ),

    # --- bonus scenarios, straight from HANDOFF §24 -------------------------

    Scenario(
        name="spouse_event_never_displaces",
        description=(
            "A shared-calendar event nobody confirmed as his own (his wife's "
            "night out) stays context forever — it must never turn his work "
            "into 'displaced', unlike a confirmed commitment."
        ),
        signals=[
            signal("10:05", source="chat", venture="blt"),
        ],
        intents=[
            intent("Greta nicoj", "10:00", "11:00", calendar_kind="family_calendar",
                   attendance="not_his", event_id="greta1"),
        ],
        expect=[
            {"kind": "span", "start": "10:00", "end": "10:15", "attention": "present",
             "venture": "blt", "intent_event_id": None},
            {"kind": "no_block_between", "start": "10:00", "end": "11:00", "intent_event_id": "greta1"},
        ],
    ),

    Scenario(
        name="confirmed_yes_skips_guardrails_and_marks_fact",
        description=(
            "Ignas tapping 'yes' on a family event is ground truth: it skips "
            "the density/minimum-length/watched-elsewhere guards (a 15-minute "
            "remainder would otherwise fail the 20-minute floor) and marks the "
            "remainder confidence='fact', reasoning 'you said you went', "
            "never 'assumed'."
        ),
        intents=[
            intent("Futbolas", "10:00", "10:15", calendar_kind="family_calendar",
                   attendance="his", attendance_answer="yes", event_id="fut10"),
        ],
        expect=[
            {"kind": "span", "start": "10:00", "end": "10:15", "attention": "present",
             "venture": "family", "confidence": "fact", "reasoning_contains": "you said you went"},
        ],
    ),

    Scenario(
        name="work_under_a_longer_intent_still_blocks_family_inference",
        description=(
            "A recital overlapped by a longer work meeting: the work slices are "
            "filed under the LONGER intent, so a density guard that counted only "
            "its own intent's blocks saw an empty recital and assumed he went. "
            "Found in review 2026-09-27."
        ),
        signals=(
            [signal("07:00", source="chat", venture="deadlift")]
            + [signal(f"14:{m:02d}", source="git", venture="deadlift")
               for m in (15, 30, 45)]
            + [signal("15:00", source="git", venture="deadlift"),
               signal("15:15", source="git", venture="deadlift")]
        ),
        intents=[
            intent("Deadlift working session", "08:00", "15:30",
                   calendar_kind="workspace_primary", venture="deadlift"),
            intent("Emory dance recital", "14:00", "16:00",
                   calendar_kind="family_calendar", venture="family",
                   attendance="his"),
        ],
        expect=[
            {"kind": "total_minutes", "venture": "family",
             "attention": "present", "equals": 0},
        ],
    ),


    Scenario(
        name="an_unwatched_day_produces_nothing",
        description=(
            "A day with no evidence inside the workday produces NO blocks — not "
            "one 780-minute 'nothing recorded' bar. Reported by Ignas from the "
            "watchdog alert on 2026-09-30: two Sundays had rendered as thirteen "
            "hours of grey on the Secretary calendar."
        ),
        workday=True,
        signals=[],
        intents=[],
        expect=[{"kind": "total_minutes", "equals": 0}],
    ),

    Scenario(
        name="a_watched_day_still_shows_its_holes",
        description=(
            "The distinction that makes the rule above safe: an unaccounted "
            "stretch on a day he WAS being watched is still worth showing, and "
            "is what the evening gap question asks about."
        ),
        workday=True,
        signals=[signal("09:00", source="chat", venture="blt")],
        expect=[{"kind": "total_minutes", "attention": "ambiguous", "at_least": 60}],
    ),

]


# ---------------------------------------------------------------------------
# question scenarios
# ---------------------------------------------------------------------------

QUESTION_SCENARIOS: list[QuestionScenario] = [

    QuestionScenario(
        name="evening_ping_survives_a_quiet_day",
        description=(
            "A quiet evening — no signals, no blocks, nothing to report — "
            "must still produce a valid, budgeted question set instead of "
            "crashing (2026-09-16: the gains card raised when the reply "
            "escape was its only option, and cost the whole evening ping)."
        ),
        kind="evening",
        gain_evidence={"learned": [], "progressed": [], "experienced": []},
        expect=[
            {"kind": "min_questions", "n": 1},
            {"kind": "min_options", "n": 2},
            {"kind": "tap_budget_ok"},
        ],
    ),

    QuestionScenario(
        name="question_time_matches_local_not_utc",
        description=(
            "A signal at 15:10 local must render as 15:10 in the composed "
            "question, not the UTC clock underneath it (HANDOFF §25: the "
            "ping once told him 13:10 for something that happened at 15:10 "
            "in Zagreb)."
        ),
        signals=[
            signal("15:10", source="gmail", venture="blt", counterpart="Ante",
                   subject="Ante Cetinic contract thread", container="thread/ante"),
            signal("15:20", source="gmail", venture="blt", counterpart="Ante",
                   subject="Ante Cetinic contract thread", container="thread/ante"),
        ],
        kind="midday",
        expect=[
            {"kind": "text_contains", "substring": "15:10"},
            {"kind": "no_text_contains", "substring": "13:10"},
        ],
    ),

    QuestionScenario(
        name="inbound_only_container_never_cited",
        description=(
            "A busy inbound-only container (PandaDoc notifications, "
            "actor='other') must never become the ping's headline — only "
            "what he actually touched counts as his attention (HANDOFF §25's "
            "'was that yours to do?' about someone else's mail)."
        ),
        signals=[
            *[signal(f"09:0{i}", source="gmail", venture="blt", actor="other",
                     counterpart="PandaDoc", subject="Opera/DixiVobis contract thread",
                     container="thread/pandadoc")
              for i in range(5)],
            signal("10:00", source="chat", venture="deadlift", counterpart="Ante",
                   subject="Machina release", container="spaces/deadlift"),
        ],
        kind="midday",
        expect=[
            {"kind": "no_container_cites_other_only"},
            {"kind": "no_text_contains", "substring": "PandaDoc"},
            {"kind": "no_text_contains", "substring": "DixiVobis"},
        ],
    ),

    QuestionScenario(
        name="llm_stub_respects_budget_and_split_gap",
        description=(
            "An LLM-composed evening ping still goes through the same tap "
            "budget as the fallback, and gets split/gap/gains/body_mind "
            "appended exactly like it — the model is never trusted to add "
            "those itself (compose_llm's own prompt forbids it)."
        ),
        signals=[
            signal("09:05", source="chat", venture="deadlift", counterpart="Ante",
                   subject="Machina release", container="spaces/deadlift"),
        ],
        blocks=[
            block("09:00", "11:00", venture="deadlift", work_type="build",
                  project="machina", attention="present", confidence="inferred"),
            block("11:00", "12:00", attention="ambiguous", confidence="inferred"),
        ],
        kind="evening",
        gain_evidence={
            "learned": [{"label": "Logged a decision", "evidence_ids": ["1"]}],
            "progressed": [], "experienced": [],
        },
        llm_response={
            "questions": [
                {"qid": "sink",
                 "text": "Machina release thread with Ante — 1 msg since 09:05. Was that yours to do?",
                 "options": [
                     {"key": "A", "label": "Planned & mine",
                      "payload": {"kind": "sink", "verdict": "planned_mine"}},
                     {"key": "B", "label": "One-off, ignore",
                      "payload": {"kind": "sink", "verdict": "one_off"}},
                 ]},
            ],
        },
        expect=[
            {"kind": "tap_budget_ok"},
            {"kind": "min_options", "n": 2},
        ],
    ),

    QuestionScenario(
        name="midday_never_exceeds_two_attention_questions",
        description=(
            "Midday only ever gets two attention questions total, however "
            "many clusters or a split candidate the window offers (plan §0's "
            "binding tap budget, enforced in one place regardless of "
            "composer)."
        ),
        signals=[
            signal("09:00", source="gmail", venture="blt", counterpart="Marta",
                   subject="Vendor invoice", container="thread/vendor"),
            signal("10:00", source="chat", venture="deadlift", counterpart="Ante",
                   subject="Machina release", container="spaces/deadlift"),
        ],
        blocks=[
            block("09:00", "10:00", venture="blt", work_type="client",
                  attention="present", confidence="inferred"),
        ],
        kind="midday",
        expect=[
            {"kind": "tap_budget_ok"},
            {"kind": "min_options", "n": 2},
        ],
    ),
]
