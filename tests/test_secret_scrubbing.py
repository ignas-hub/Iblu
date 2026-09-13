"""The webhook key must never reach a log line or a database row.

Found in review 2026-09-13: `requests` embeds the failing request in a
connection error, and `SECRETARY_WEBHOOK_URL` carries its key in the query
string. That message was logged AND written into `pings.meta`, so a single DNS
hiccup put the secret in two durable places.
"""

from __future__ import annotations

import requests

from iblu_keeper.pings.deliver import _scrub

SECRET = "SUPERSECRETKEY123"


def _err(text: str) -> str:
    return _scrub(requests.ConnectionError(text))


def test_the_real_urllib3_shape_is_scrubbed():
    """urllib3 reports the PATH, not an absolute URL — the shape the first
    version of this scrubber missed."""
    out = _err(
        "HTTPSConnectionPool(host='chat.googleapis.com', port=443): Max retries "
        f"exceeded with url: /v1/spaces/AAA/messages?key={SECRET}&threadKey=ping-5 "
        "(Caused by NewConnectionError)"
    )
    assert SECRET not in out


def test_an_absolute_url_is_scrubbed():
    assert SECRET not in _err(f"could not reach https://chat.googleapis.com/v1/x?key={SECRET}")


def test_a_bare_key_parameter_is_scrubbed_wherever_it_appears():
    """A backstop that does not depend on the message being URL-shaped."""
    for param in ("key", "token", "auth", "secret", "signature", "access_token"):
        assert SECRET not in _err(f"rejected: {param}={SECRET}")


def test_scrubbing_is_case_insensitive():
    assert SECRET not in _err(f"KEY={SECRET}")


def test_the_useful_part_of_the_message_survives():
    """A scrubbed message still has to be diagnosable."""
    out = _err(
        "HTTPSConnectionPool(host='chat.googleapis.com', port=443): Max retries "
        f"exceeded with url: /v1/spaces/A/messages?key={SECRET} "
        "(Caused by NewConnectionError: [Errno -2] Name or service not known)"
    )
    assert "chat.googleapis.com" in out
    assert "Name or service not known" in out


def test_a_message_with_no_secret_is_left_readable():
    assert _err("connection reset by peer") == "connection reset by peer"


def test_the_weekly_review_uses_the_same_scrubber():
    """One implementation, so the two cannot drift apart."""
    import inspect

    from iblu_keeper.jobs import weekly

    assert "_scrub" in inspect.getsource(weekly.send)
