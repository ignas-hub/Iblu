"""Signed tap-link tokens (plan §7.5).

A tap link is a bare URL sitting in a Chat card. It is unauthenticated by
design — the whole point is that answering costs one thumb-tap — so the token
itself has to carry the authority:

    base64url(payload_json) . base64url(hmac_sha256(secret, payload_json))

Rules that matter:
  * the signature is verified with `hmac.compare_digest` (constant time);
  * expiry is inside the signed payload, so it cannot be extended by editing
    the URL;
  * every failure mode returns the same `InvalidToken` — a caller must not be
    able to tell "bad signature" from "expired" from "malformed" by probing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("iblu_keeper.pings.tokens")

DEFAULT_TTL = timedelta(hours=36)


class InvalidToken(Exception):
    """Malformed, mis-signed or expired. Deliberately undifferentiated."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same failure
        raise InvalidToken("malformed token") from exc


def _sign(payload: bytes, secret: str) -> str:
    return _b64encode(
        hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    )


def make_token(
    ping_id: int,
    qid: str,
    key: str,
    secret: str,
    sent_at: datetime | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> str:
    """Mint a token for one option of one question of one ping."""
    if not secret:
        raise ValueError("PING_SIGNING_SECRET is empty — refusing to mint a token")
    issued = sent_at or datetime.now(timezone.utc)
    payload = json.dumps(
        {"p": ping_id, "q": qid, "k": key, "exp": int((issued + ttl).timestamp())},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{_b64encode(payload)}.{_sign(payload, secret)}"


def read_token(token: str, secret: str, now: datetime | None = None) -> dict:
    """Verify and decode. Raises `InvalidToken` for every failure mode."""
    if not secret:
        raise InvalidToken("no signing secret configured")
    if not token or token.count(".") != 1:
        raise InvalidToken("malformed token")

    payload_b64, signature = token.split(".", 1)
    payload = _b64decode(payload_b64)

    if not hmac.compare_digest(_sign(payload, secret), signature):
        raise InvalidToken("bad signature")

    try:
        data = json.loads(payload)
        ping_id, qid, key, exp = data["p"], data["q"], data["k"], int(data["exp"])
    except Exception as exc:  # noqa: BLE001
        raise InvalidToken("malformed payload") from exc

    moment = now or datetime.now(timezone.utc)
    if moment.timestamp() > exp:
        raise InvalidToken("expired")

    return {"ping_id": int(ping_id), "qid": str(qid), "key": str(key), "exp": exp}


def tap_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/q/{token}"
