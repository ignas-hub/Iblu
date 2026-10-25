"""The Friday stage card — the question that produces the Gain.

Six weeks after the 2026-09-13 baselines, `project_stage_history` had zero rows.
Every weekly review could only list tasks, and he said so: "random tasks
closed... they do not show how I'm getting closer to my goals". The mission's
unit of progress is a rung toward autonomous; nothing recorded one because
nothing asked. These tests pin the asking.

Pure functions only. The suite runs DRY_RUN=true with no database, and the
parts worth pinning — which rung is proposed, what a tap key means, that the
wording passes the Gap gate — need none.
"""

from __future__ import annotations

from iblu_keeper.jobs import stage_check as S

LADDER = [
    {"code": "research", "label": "Researching", "ordinal": 1},
    {"code": "initiate", "label": "Initiating", "ordinal": 2},
    {"code": "build", "label": "Building", "ordinal": 3},
    {"code": "trial", "label": "Trialing", "ordinal": 4},
    {"code": "implement", "label": "Implementing", "ordinal": 5},
    {"code": "deliver", "label": "Delivering", "ordinal": 6},
    {"code": "maintain", "label": "Maintaining", "ordinal": 7},
    {"code": "autonomous", "label": "Autonomous", "ordinal": 8},
]


def _proposal(letter="A", frm="deliver", nxt="maintain", signals=106):
    return {
        "letter": letter, "project": "email-writer", "name": "Email Writer",
        "venture": "blt", "from_stage": frm, "from_ordinal": 6,
        "from_since": "2026-09-13T00:00:00+00:00", "next_stage": nxt,
        "next_label": "Maintaining", "top_stage": "autonomous",
        "evidence": {"signals": signals, "sources": 3,
                     "subjects": ["fix(reply-handler): …", "Email Writer"]},
    }


# --- which rung is proposed -------------------------------------------------


def test_the_proposal_is_one_rung_up():
    assert S._next_stage(LADDER, "deliver")["code"] == "maintain"
    assert S._next_stage(LADDER, "research")["code"] == "initiate"


def test_autonomous_has_no_rung_above_it():
    """Asking 'did it stay autonomous?' every Friday is noise."""
    assert S._next_stage(LADDER, "autonomous") is None


def test_an_unknown_stage_proposes_nothing():
    assert S._next_stage(LADDER, "limbo") is None


# --- the card -----------------------------------------------------------------


def test_the_card_carries_the_value_before_the_question():
    """He does not answer cards that give nothing back — ten in a row went
    unanswered. The text names the stage, how long it has held, and the week's
    evidence BEFORE asking anything."""
    text = S.render_text([_proposal()])
    assert "Email Writer — deliver since 13 Sep" in text
    assert "106 signals this week" in text
    assert "looks like → maintain" in text
    assert "A rung you confirm is next Friday's first line" in text


def test_each_project_offers_up_top_and_stayed(monkeypatch):
    monkeypatch.setattr(S, "make_token", lambda cid, qid, key, secret: f"T-{key}", raising=False)
    card = S.build_card(7, [_proposal()], "", "https://x", "secret")
    buttons = card["cardsV2"][0]["card"]["sections"][0]["widgets"][2]["buttonList"]["buttons"]
    labels = [b["text"] for b in buttons]
    assert labels == ["→ maintain", "→ autonomous", "stayed deliver"]


def test_one_rung_from_the_top_does_not_offer_the_same_choice_twice():
    """At maintain, 'up' and 'autonomous' are the same rung."""
    card = S.build_card(7, [_proposal(frm="maintain", nxt="autonomous")], "",
                        "https://x", "s" * 32)
    buttons = card["cardsV2"][0]["card"]["sections"][0]["widgets"][2]["buttonList"]["buttons"]
    assert [b["text"] for b in buttons] == ["→ autonomous", "stayed maintain"]


def test_tap_keys_are_letter_plus_choice():
    """The key rides in a signed token in a URL on a phone — it stays short."""
    from iblu_keeper.pings.tokens import make_token, read_token

    secret = "s" * 32
    card = S.build_card(7, [_proposal(letter="B")], "", "https://x", secret)
    url = card["cardsV2"][0]["card"]["sections"][0]["widgets"][2]["buttonList"]["buttons"][0]["onClick"]["openLink"]["url"]
    token = url.rsplit("/q/", 1)[1]
    data = read_token(token, secret)
    assert data["qid"] == "stage"
    assert data["key"] == "B" + S.CHOICE_UP
    assert data["ping_id"] == 7


# --- the ladder in the review -----------------------------------------------


def _ladder(**kw):
    base = {
        "baseline": "2026-09-13",
        "this_week": [],
        "since_baseline": {"moves": 0, "rungs": 0, "projects": 0},
        "one_from_top": [{"code": "scout", "name": "Scout", "venture": "blt"}],
        "at_top": 0,
        "total_active": 28,
    }
    base.update(kw)
    return base


def test_a_week_with_no_confirmed_rung_says_so_and_where_it_starts():
    from iblu_keeper.jobs.weekly import _ladder_body

    text = _ladder_body(_ladder(), None)
    assert "no rung confirmed yet" in text
    assert "stage card" in text
    assert "One rung from autonomous: Scout." in text


def test_a_confirmed_rung_opens_the_review():
    from iblu_keeper.jobs.weekly import _ladder_body

    text = _ladder_body(_ladder(
        this_week=[{"name": "Email Writer", "from_stage": "deliver",
                    "to_stage": "maintain", "from_ord": 6, "to_ord": 7}],
        since_baseline={"moves": 1, "rungs": 1, "projects": 1},
    ), None)
    assert text.index("Climbed this week") < text.index("Since 2026-09-13")
    assert "Email Writer — deliver → maintain" in text
    assert "1 rung(s) climbed across 1 project(s), confirmed by Ignas" in text


def test_the_weeks_work_is_evidence_beneath_the_rung_not_the_headline():
    from iblu_keeper.jobs.weekly import _ladder_body

    ach = {
        "shipped": [{"repo": "email-writer", "commits": 44}],
        "closed": [{"name": "Noshinku", "started_by": "other"}],
        "time_back": [{"name": "Anamaria Relja", "was": 63, "now": 16}],
    }
    text = _ladder_body(_ladder(), ach)
    assert text.index("Since 2026-09-13") < text.index("Evidence this week")
    assert "Built: 44 commits — email-writer (44)" in text
    assert "Closed: 1 threads, 1 opened by someone else — Noshinku" in text
    assert "Quieter than last week: Anamaria Relja 63→16" in text


def test_the_ladder_wording_passes_the_gap_gate():
    """'One rung from autonomous' is a position, not a distance to a target."""
    from iblu_keeper.jobs.review_language import validate_language
    from iblu_keeper.jobs.weekly import _ladder_body

    body = _ladder_body(_ladder(
        this_week=[{"name": "Email Writer", "from_stage": "deliver",
                    "to_stage": "maintain", "from_ord": 6, "to_ord": 7}],
        since_baseline={"moves": 1, "rungs": 1, "projects": 1},
        at_top=1,
    ), {"shipped": [{"repo": "x", "commits": 3}], "closed": [], "time_back": []})
    assert validate_language("*Weekly review*\n\n## Gains\n" + body + "\n\n## Truth\nx") == []


# --- the tap writes history, and only the tap -----------------------------


def test_only_the_tap_moves_a_stage():
    """Scripts propose; he decides. `propose` must never touch `projects`."""
    import inspect

    src = inspect.getsource(S.propose)
    assert "UPDATE" not in src and "INSERT" not in src
    tap = inspect.getsource(S.record_stage_tap)
    assert "INSERT INTO project_stage_history" in tap
    assert "changed_by, note" in tap and "'ignas'" in tap
    assert "stage_confidence = 'fact'" in tap


def test_a_stayed_tap_confirms_without_a_history_row():
    """History is for moves. A confirmation that it stayed is still a fact."""
    import inspect

    tap = inspect.getsource(S.record_stage_tap)
    assert "if moved and not already:" in tap
    assert tap.index("if moved and not already:") < tap.index("stage_confidence = 'fact'")


def test_the_tap_route_knows_the_stage_qid():
    import inspect

    from iblu_keeper.pings import answers

    assert 'qid == "stage"' in inspect.getsource(answers.record_tap)
