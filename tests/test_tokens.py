"""Tap-link token signing (plan §7.5).

These links are unauthenticated URLs in a chat card, so the token is the only
thing standing between a stranger and Ignas's work log.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from iblu_keeper.pings import tokens

SECRET = "test-secret-not-the-real-one"
NOW = datetime(2026, 9, 14, 12, 40, tzinfo=timezone.utc)


def test_roundtrip():
    t = tokens.make_token(42, "sink", "B", SECRET, sent_at=NOW)
    out = tokens.read_token(t, SECRET, now=NOW + timedelta(hours=1))
    assert out["ping_id"] == 42 and out["qid"] == "sink" and out["key"] == "B"


def test_signature_is_required():
    t = tokens.make_token(1, "sink", "A", SECRET, sent_at=NOW)
    payload, _ = t.split(".")
    with pytest.raises(tokens.InvalidToken):
        tokens.read_token(f"{payload}.deadbeef", SECRET, now=NOW)


def test_a_different_secret_cannot_forge():
    t = tokens.make_token(1, "sink", "A", SECRET, sent_at=NOW)
    with pytest.raises(tokens.InvalidToken):
        tokens.read_token(t, "some-other-secret", now=NOW)


def test_payload_cannot_be_edited_to_answer_a_different_question():
    """Re-encoding the payload invalidates the signature."""
    import base64, json

    t = tokens.make_token(1, "sink", "A", SECRET, sent_at=NOW)
    payload_b64, signature = t.split(".")
    data = json.loads(tokens._b64decode(payload_b64))
    data["k"] = "D"  # answer a different option
    forged = tokens._b64encode(json.dumps(data, separators=(",", ":"), sort_keys=True).encode())
    with pytest.raises(tokens.InvalidToken):
        tokens.read_token(f"{forged}.{signature}", SECRET, now=NOW)


def test_expiry_is_inside_the_signature_so_it_cannot_be_extended():
    import base64, json

    t = tokens.make_token(1, "sink", "A", SECRET, sent_at=NOW, ttl=timedelta(hours=1))
    payload_b64, signature = t.split(".")
    data = json.loads(tokens._b64decode(payload_b64))
    data["exp"] = int((NOW + timedelta(days=365)).timestamp())
    forged = tokens._b64encode(json.dumps(data, separators=(",", ":"), sort_keys=True).encode())
    with pytest.raises(tokens.InvalidToken):
        tokens.read_token(f"{forged}.{signature}", SECRET, now=NOW + timedelta(hours=2))


def test_expires_after_its_ttl():
    t = tokens.make_token(1, "sink", "A", SECRET, sent_at=NOW)
    tokens.read_token(t, SECRET, now=NOW + timedelta(hours=35))  # still fine
    with pytest.raises(tokens.InvalidToken):
        tokens.read_token(t, SECRET, now=NOW + timedelta(hours=37))


@pytest.mark.parametrize(
    "bad",
    ["", "nodot", "a.b.c", "!!!.???", "." , "x.", ".y"],
)
def test_garbage_never_raises_anything_but_invalidtoken(bad):
    """The /q route must never 500 on user input — every path lands here."""
    with pytest.raises(tokens.InvalidToken):
        tokens.read_token(bad, SECRET, now=NOW)


def test_no_secret_means_no_tokens():
    with pytest.raises(ValueError):
        tokens.make_token(1, "sink", "A", "", sent_at=NOW)
    with pytest.raises(tokens.InvalidToken):
        tokens.read_token("a.b", "", now=NOW)


def test_tokens_are_url_safe():
    for i in range(50):
        t = tokens.make_token(i, "displaced", "C", SECRET, sent_at=NOW)
        assert "/" not in t and "+" not in t and "=" not in t


def test_tap_url_shape():
    t = tokens.make_token(7, "split", "A", SECRET, sent_at=NOW)
    assert tokens.tap_url("https://mcp.iblugames.com/", t) == f"https://mcp.iblugames.com/q/{t}"
