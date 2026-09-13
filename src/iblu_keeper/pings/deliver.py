"""Getting the card onto Ignas's phone (plan §7.4).

Delivery is an incoming webhook, not the Chat API, for one blunt reason: a
message you send yourself never raises a notification, and cards with link
buttons need app authority. The webhook posts as an app, so it notifies — and
it needs no service-account key, which was kept out of this project on purpose.
"""

from __future__ import annotations

import re

import logging

import requests

from ..config import settings
from .compose import QuestionSet
from .tokens import make_token, tap_url

logger = logging.getLogger("iblu_keeper.pings.deliver")

TIMEOUT = 20

TITLES = {
    "midday": "Midday check",
    "evening": "Evening check",
    "test": "Test check",
}


class DeliveryError(RuntimeError):
    """The webhook refused the card. The ping row is marked failed and retried."""



def _scrub(exc: Exception) -> str:
    """An exception message with anything URL-shaped removed.

    `requests` embeds the failing request in a connection error, and the
    Secretary webhook carries its key in the query string. That message was
    being logged AND persisted into `pings.meta`, so one DNS hiccup put the
    webhook secret in two durable places.

    Three passes, because the first version only caught one shape and missed
    the real one: urllib3 reports the PATH, not the absolute URL
    ("Max retries exceeded with url: /v1/spaces/A/messages?key=..."). Redact
    absolute URLs, anything introduced as `url: `, and — as a backstop that
    does not depend on either shape — the value of any obviously secret
    parameter wherever it appears.
    """
    text = str(exc)
    text = re.sub(r"https?://\S+", "<url redacted>", text)
    text = re.sub(r"(url:\s*)\S+", r"\1<redacted>", text, flags=re.I)
    text = re.sub(
        r"\b(key|token|auth|secret|signature|password|access_token)=[^&\s\)\]]+",
        r"\1=<redacted>",
        text,
        flags=re.I,
    )
    return text

def preflight_tap_route() -> None:
    """Refuse to send a card whose buttons would 404.

    The tick job and the MCP server are separate processes: the timer can be
    live while the server is still running a build without the `/q` route, and
    the failure is invisible until Ignas taps a button on his phone and gets
    nothing. A deliberately invalid token should come back 410 (route present,
    token rejected); 404 means the route is not deployed.
    """
    base = settings.mcp_public_base_url.rstrip("/")
    try:
        response = requests.get(f"{base}/q/preflight", timeout=10)
    except requests.RequestException as exc:
        raise DeliveryError(f"tap endpoint unreachable at {base}/q/: {exc}") from exc

    if response.status_code == 404:
        raise DeliveryError(
            f"{base}/q/ returns 404 — the server has not picked up the tap route. "
            "Restart iblu-mcp before pings can be answered."
        )


def build_card(
    ping_id: int,
    kind: str,
    questions: QuestionSet,
    self_id: str,
    base_url: str,
    secret: str,
) -> dict:
    """The Chat message body: a mention (so it notifies) plus one card."""
    taps = sum(1 for _ in questions.questions)
    mention = f"<users/{self_id}> " if self_id else ""
    sections = []

    for question in questions.questions:
        buttons = []
        for option in question.options:
            token = make_token(ping_id, question.qid, option.key, secret)
            buttons.append({
                "text": f"{option.key} · {option.label}",
                "onClick": {"openLink": {"url": tap_url(base_url, token)}},
            })
        sections.append({
            "header": question.text,
            "widgets": [{"buttonList": {"buttons": buttons}}],
        })

    return {
        "text": f"{mention}{TITLES.get(kind, 'Check')} — {taps} taps.",
        "cardsV2": [{
            "cardId": f"ping-{ping_id}",
            "card": {"sections": sections},
        }],
    }


def send(
    ping_id: int,
    kind: str,
    questions: QuestionSet,
    self_id: str,
) -> dict:
    """POST the card. Returns `{message_ref, thread_ref}`; raises on failure."""
    if not settings.secretary_webhook_url:
        raise DeliveryError("SECRETARY_WEBHOOK_URL is not set")
    if not settings.ping_signing_secret:
        raise DeliveryError("PING_SIGNING_SECRET is not set — tap links would be unsignable")
    if not settings.mcp_public_base_url:
        raise DeliveryError("MCP_PUBLIC_BASE_URL is not set — tap links would be relative")

    preflight_tap_route()

    body = build_card(
        ping_id, kind, questions, self_id,
        settings.mcp_public_base_url, settings.ping_signing_secret,
    )

    # threadKey keeps each ping's replies in its own thread, so a free-text
    # answer can be attributed back to the question that prompted it.
    url = settings.secretary_webhook_url
    separator = "&" if "?" in url else "?"
    url = (
        f"{url}{separator}threadKey=ping-{ping_id}"
        "&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    )

    try:
        response = requests.post(url, json=body, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise DeliveryError(f"webhook unreachable: {_scrub(exc)}") from exc

    if response.status_code >= 400:
        # Never log the URL: it carries the webhook key and token.
        raise DeliveryError(f"webhook returned {response.status_code}: {response.text[:300]}")

    data = response.json()
    refs = {
        "message_ref": data.get("name"),
        "thread_ref": (data.get("thread") or {}).get("name"),
    }
    logger.info("deliver: ping %s sent as %s", ping_id, refs["message_ref"])
    return refs
