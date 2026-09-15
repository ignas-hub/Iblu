"""Collector unit tests — pure logic, no Google APIs and no database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from iblu_keeper.collectors import default_since, gmail_sent
from iblu_keeper.collectors import calendar_changes as cal
from iblu_keeper.collectors.venture_hints import infer

ME = "ignas@blanklabel.team"


# --- authorship: `in:sent` is not the same as "I wrote it" ----------------


@pytest.mark.parametrize(
    "from_header,expected",
    [
        ("ignas@blanklabel.team", True),
        ("Ignas Gee <ignas@blanklabel.team>", True),
        ("  IGNAS@BlankLabel.Team  ", True),
        # Google Group traffic: filed under in:sent, written by someone else.
        ("\"'PandaDoc' via Contracts\" <contracts@blanklabel.team>", False),
        ("\"'Aleksandra' via Accounting\" <finance@blanklabel.team>", False),
        # A substring check would wrongly pass this one.
        ("Someone <not-ignas@blanklabel.team.evil.com>", False),
        ("", False),
    ],
)
def test_only_mail_i_actually_wrote_counts(from_header, expected):
    assert gmail_sent._is_mine({"from": from_header}, ME) is expected


ALIASES = {
    "ignacio@chocoagency.com",
    "ap@chocoagency.com",
    "contracts@blanklabel.team",
}


@pytest.mark.parametrize(
    "from_header,expected,why",
    [
        # A mailbox often sends under an alias — the Choco account invoices as
        # ap@ — and those are still Ignas's work.
        ("Accounts Payable <ap@chocoagency.com>", True, "verified alias send"),
        ("Ignacio Choco <ignacio@chocoagency.com>", True, "primary address"),
        # But a group alias is ALSO in the send-as list, so the alias check
        # alone would re-admit group traffic. Google's `via` rewrite is what
        # separates them.
        ("\"'Diana Saavedra' via ap\" <ap@chocoagency.com>", False, "group delivery"),
        ("\"'PandaDoc' via Contracts\" <contracts@blanklabel.team>", False, "group delivery"),
        ("Someone <outsider@example.com>", False, "not one of my addresses"),
    ],
)
def test_alias_sends_count_but_group_deliveries_do_not(from_header, expected, why):
    got = gmail_sent._is_mine({"from": from_header}, "ignacio@chocoagency.com", ALIASES)
    assert got is expected, why


def test_a_forwarded_newsletter_is_still_mine():
    """Forwarding preserves the original's list headers.

    Keying group detection on list-unsubscribe/precedence discarded genuine
    forwards — found in the Choco mailbox the moment aliases were switched on.
    """
    headers = {
        "from": "Accounts Payable <ap@chocoagency.com>",
        "subject": "Fwd: desfrutandoavida — Expected invoice",
        "list-unsubscribe": "<https://example.com/unsub>",
        "precedence": "list",
    }
    assert gmail_sent._is_mine(headers, "ignacio@chocoagency.com", ALIASES) is True


def test_group_detection_is_only_about_the_via_rewrite():
    assert gmail_sent._is_group_delivery({"from": "\"'X' via Team\" <t@x.com>"}) is True
    assert gmail_sent._is_group_delivery({"from": "Real Person <t@x.com>"}) is False
    assert gmail_sent._is_group_delivery({}) is False


def test_address_parsing():
    assert gmail_sent._address_of("Ignas Gee <ignas@blanklabel.team>") == ME
    assert gmail_sent._address_of(None) == ""


def test_recipient_helpers():
    header = "Ana <a@x.com>, Bo <b@y.com>"
    assert gmail_sent._first_address(header) == "Ana <a@x.com>"
    assert gmail_sent._addresses(header) == ["Ana <a@x.com>", "Bo <b@y.com>"]
    assert gmail_sent._addresses(None) == []


def test_internal_date_is_epoch_millis():
    at = gmail_sent._occurred_at({"internalDate": "1789123944000"})
    assert at.tzinfo is timezone.utc
    assert at.year == 2026


# --- watermarks -----------------------------------------------------------


def test_first_run_looks_back_a_bounded_window_not_all_history():
    since = default_since(None, fallback_hours=24)
    age = datetime.now(timezone.utc) - since
    assert timedelta(hours=23) < age < timedelta(hours=25)


def test_existing_watermark_is_respected():
    mark = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert default_since(mark) == mark


# --- calendar diffing -----------------------------------------------------


def _event(start="2026-09-14T10:00:00Z", end="2026-09-14T11:00:00Z",
           summary="Opera sync", status="confirmed", attendees=None, response=None):
    ev = {
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "summary": summary,
        "status": status,
    }
    if attendees is not None or response is not None:
        ev["attendees"] = [
            {"email": a} for a in (attendees or [])
        ] + ([{"email": ME, "self": True, "responseStatus": response}] if response else [])
    return ev


def test_fingerprint_is_stable_and_change_sensitive():
    a = cal._payload(_event())
    b = cal._payload(_event())
    assert cal._fingerprint(a) == cal._fingerprint(b)
    moved = cal._payload(_event(start="2026-09-15T10:00:00Z"))
    assert cal._fingerprint(a) != cal._fingerprint(moved)


def test_attendee_order_does_not_change_the_fingerprint():
    a = cal._payload(_event(attendees=["b@x.com", "a@x.com"]))
    b = cal._payload(_event(attendees=["a@x.com", "b@x.com"]))
    assert cal._fingerprint(a) == cal._fingerprint(b)


def test_all_day_events_have_a_start():
    ev = {"start": {"date": "2026-09-14"}, "end": {"date": "2026-09-15"}}
    assert cal._edge(ev["start"]) == "2026-09-14"


@pytest.mark.parametrize(
    "after,expected",
    [
        (_event(start="2026-09-15T10:00:00Z"), "moved"),
        (_event(status="cancelled"), "cancelled"),
        (_event(response="declined"), "cancelled"),
        (_event(summary="Renamed"), "changed"),
    ],
)
def test_change_classification(after, expected):
    assert cal._classify(cal._payload(_event()), cal._payload(after)) == expected


def test_human_diff_reads_like_a_sentence():
    before = cal._payload(_event())
    after = cal._payload(_event(start="2026-09-15T14:00:00Z", end="2026-09-15T15:00:00Z"))
    assert "→" in cal._describe(before, after)


# --- venture inference ----------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        (dict(counterpart="ana@deadlift.io"), ("deadlift", None)),
        (dict(counterpart="x@mail.deadlift.io"), ("deadlift", None)),
        (dict(subject="Opera weekly sync"), ("choco", None)),
        (dict(subject="Machina deploy"), ("deadlift", "machina")),
        # 'radovi' is now also a seeded project alias (plan §1.5,
        # PROJECT_KEYWORDS in venture_hints.py) — project resolves from day
        # one, on top of the venture inference this test already pinned.
        (dict(subject="Radovi — kupaonica"), ("jakusi", "radovi")),
        (dict(subject="totally unrelated"), ("blt", None)),
    ],
)
def test_venture_inference(kwargs, expected):
    assert infer(account=ME, **kwargs) == expected


def test_inference_never_claims_certainty():
    """v1 always writes venture_confidence='inferred' — nothing here is a fact."""
    from iblu_keeper.collectors import insert_signal  # noqa: F401
    import inspect
    from iblu_keeper import collectors

    source = inspect.getsource(collectors.insert_signal)
    assert '"venture_confidence": "inferred"' in source


# --- ping question wording -------------------------------------------------


def test_internal_keys_never_reach_the_person_answering():
    """'sink' is the qid, not a word a human should read on their phone."""
    import pydantic
    from iblu_keeper.pings.compose import Question

    option = {"key": "A", "label": "Planned & mine",
              "payload": {"kind": "sink", "verdict": "planned_mine"}}

    plain = Question(
        qid="sink",
        text="Ante Cetinic contract thread — 3 msgs, 09:07-09:22. Was that yours to do?",
        options=[option, option],
    )
    assert "sink" not in plain.text.lower()

    for jargon in ("Ante Cetinic thread — biggest sink?",
                   "Your biggest attention sink this morning?"):
        with pytest.raises(pydantic.ValidationError):
            Question(qid="sink", text=jargon, options=[option, option])


def test_fallback_questions_are_plain_english():
    from datetime import datetime, timezone
    from iblu_keeper.pings.compose import JARGON, compose_fallback

    now = datetime.now(timezone.utc)
    signals = [{
        "id": 1, "source": "chat", "occurred_at": now, "counterpart": "Ante Cetinic",
        "container": "spaces/X", "subject": "Ante Cetinic", "snippet": "hi",
        "initiator": "other", "venture": "blt",
    }]
    questions = compose_fallback(signals, [], now, now)
    for question in questions.questions:
        for word in JARGON:
            assert word not in question.text.lower(), question.text
        # every question must actually be a question
        assert question.text.rstrip().endswith("?")


def test_vague_thread_references_are_rejected():
    """"a gmail thread" cannot be resolved back to one of forty, six weeks on."""
    import pydantic
    from iblu_keeper.pings.compose import Question

    option = {"key": "A", "label": "Planned & mine",
              "payload": {"kind": "sink", "verdict": "planned_mine"}}

    named = Question(
        qid="sink",
        text="Temu contract thread with Giedre — 4 msgs. Was that yours to do?",
        options=[option, option],
    )
    assert "Giedre" in named.text

    for vague in ("Mostly Ante Cetinic work + a gmail thread — right?",
                  "You spent the morning on some messages. Yours?"):
        with pytest.raises(pydantic.ValidationError):
            Question(qid="sink", text=vague, options=[option, option])


def test_a_work_type_option_must_carry_the_code_it_claims_to_record():
    """A classification question whose options record nothing is worse than none."""
    import pydantic
    from iblu_keeper.pings.compose import Option

    Option(key="A", label="Sales / BD / pitch",
           payload={"kind": "work_type", "verdict": "classify", "work_type": "sales"})

    with pytest.raises(pydantic.ValidationError):
        Option(key="A", label="Sales / BD / pitch",
               payload={"kind": "work_type", "verdict": "classify"})


def test_fallback_always_asks_what_kind_of_work_it_was():
    """work_type is the one thing no collector can ever recover after the fact."""
    from datetime import datetime, timezone
    from iblu_keeper.pings.compose import compose_fallback

    now = datetime.now(timezone.utc)
    signals = [{
        "id": 1, "source": "chat", "occurred_at": now, "counterpart": "Ante Cetinic",
        "container": "spaces/X", "subject": "Ante Cetinic", "snippet": "hi",
        "initiator": "other", "venture": "blt",
    }]
    questions = compose_fallback(signals, [], now, now)
    work_type_questions = [q for q in questions.questions if q.qid == "work_type"]
    assert work_type_questions, "the fallback must still capture work_type"
    codes = {o.payload.work_type for o in work_type_questions[0].options}
    assert None not in codes and len(codes) >= 3


def test_gostellar_is_recognised_by_name():
    """Greta's agency has no known email domain yet, so the name is the only
    hint there is — see venture_hints.SPACE_KEYWORDS."""
    from iblu_keeper.collectors.venture_hints import infer

    venture, _ = infer("ignas@blanklabel.team", subject="GoStellar — Alexan intro")
    assert venture == "gostellar"
    venture, _ = infer("ignas@blanklabel.team", subject="Kassari ads review")
    assert venture == "gostellar"


def test_the_bookkeepers_domain_means_company_admin_not_personal_finance():
    """"Lamb invoices" reads personal until you know whose books they are."""
    from iblu_keeper.collectors.venture_hints import infer

    venture, _ = infer(
        "ignas@blanklabel.team",
        counterpart="lamb@lamb-knjigovodstvo.hr",
        subject="Dokumenti mjesec 08. Blank Label d.o.o.",
    )
    assert venture == "blt"
