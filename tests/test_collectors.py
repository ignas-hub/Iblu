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
        (dict(subject="Radovi — kupaonica"), ("jakusi", None)),
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
