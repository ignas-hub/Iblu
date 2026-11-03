"""IBLU as a Google Chat app: the card buttons answer in place.

Every card button used to be `onClick.openLink` to `/q/<token>`. On the
iPhone Chat and Gmail apps that opens a browser, and Google puts a "Redirect
Notice" interstitial in front of it, so one tap became three. Chat only runs
a button *in place* (`onClick.action`) for a registered Chat app — a webhook
cannot — and the tap then arrives as a `CARD_CLICKED` event at the app's
HTTPS endpoint, which answers by rewriting the card.

So this module is the app side, and nothing else changes hands:

  * The signed token from `pings/tokens.py` is still the only authority. It
    rides in a button parameter (`t`) instead of a URL, and
    `pings.answers.record_tap` is still the one function that records a tap.
  * The app's identity is a service account that holds NO roles, has only
    the `chat.bot` scope, and cannot act as any user. That is why it is
    acceptable here where domain-wide delegation (HANDOFF §3.1) was not:
    the key can post to spaces the app was added to, and nothing else.
  * Google signs every event it sends us with a JWT from
    `chat@system.gserviceaccount.com`, audience = our Cloud project NUMBER.
    `verify_chat_request` checks that before anything is read.

The `/q/<token>` route in `server.py` stays for cards already on his phone
and for the ping cards, which still go out through the webhook.
"""

from __future__ import annotations

import copy
import logging
import os

import google.auth.transport.requests
import google.oauth2.id_token
from google.oauth2 import service_account
from googleapiclient.discovery import build

from . import db
from .config import settings
from .pings import answers
from .pings.deliver import DeliveryError, _scrub
from .pings.tokens import InvalidToken

logger = logging.getLogger("iblu_keeper.chat_app")

# Google signs Chat-app events with this account; its public certs live at
# the service-accounts metadata URL, not under accounts.google.com, so the
# verifier must be told where to look.
CHAT_ISSUER = "chat@system.gserviceaccount.com"
CHAT_CERTS_URL = (
    "https://www.googleapis.com/service_accounts/v1/metadata/x509/" + CHAT_ISSUER
)
CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"

# The one function name every IBLU button invokes; the token says the rest.
TAP_FUNCTION = "tap"
TOKEN_PARAM = "t"

EXPIRED_LINE = "Link expired — reply in the thread"
GONE_LINE = "That check is gone — nothing was recorded"
FAILED_LINE = "Couldn't save that — try again, or reply in the thread"

GREETING = "IBLU is here. It posts the day card, the Friday stage card and the pings; the buttons answer in place."
NOT_A_CHAT = "IBLU answers card buttons and reads replies in a card's thread — it does not chat."


class ChatAppAuthError(Exception):
    """The request did not come from Google Chat for our project. One shape
    for every failure, so a probe cannot learn which check it tripped."""


# ---------------------------------------------------------------------------
# inbound: is this really Google Chat talking to us?
# ---------------------------------------------------------------------------


def verify_chat_request(authorization_header: str, project_number: str) -> dict:
    """Verify the bearer JWT Chat attaches to every event. Returns the claims.

    The audience is the Cloud project NUMBER (what Chat puts in `aud` for an
    HTTP-endpoint app), and the issuer must be the Chat system account. An
    empty project number is refused outright: `verify_token` skips the
    audience check when given none, which would accept any Google-signed
    token. The token is never logged.
    """
    if not project_number:
        raise ChatAppAuthError("CHAT_APP_PROJECT_NUMBER is not set")
    header = (authorization_header or "").strip()
    if not header.lower().startswith("bearer "):
        raise ChatAppAuthError("missing bearer token")
    token = header[7:].strip()
    if not token:
        raise ChatAppAuthError("missing bearer token")

    try:
        claims = google.oauth2.id_token.verify_token(
            token,
            request=google.auth.transport.requests.Request(),
            audience=project_number,
            certs_url=CHAT_CERTS_URL,
        )
    except Exception as exc:  # noqa: BLE001 - every failure is the same failure
        raise ChatAppAuthError("token verification failed") from exc

    if not isinstance(claims, dict):
        raise ChatAppAuthError("token verification returned no claims")
    issuer = claims.get("iss")
    email = claims.get("email")
    if issuer != CHAT_ISSUER and email != CHAT_ISSUER:
        raise ChatAppAuthError("token not issued by Google Chat")
    return claims


# ---------------------------------------------------------------------------
# the tap result, in words
# ---------------------------------------------------------------------------


def tap_result_lines(result: dict) -> tuple[str, str]:
    """`(headline, sub)` for a recorded tap — the exact wording `/q` shows.

    Factored out of `server.py::ping_tap`, which still carries its own copy;
    TODO: make `/q` call this so the two can never drift.
    """
    if result.get("qid") == "daycard":
        if result["key"] == "A":
            n = result.get("confirmed") or 0
            sub = (
                f"Confirmed {n} block(s). The Secretary calendar updates at the next analyst run."
                if n else "Already up to date — nothing left to confirm."
            )
        else:
            sub = "Reply in this thread with what was wrong."
        return f"✓ Saved — {result['label']}", sub

    if result.get("qid") == "stage":
        if result["moved"]:
            sub = "That rung is a Gain. It opens next Friday's review."
        elif result["superseded"]:
            sub = "Already recorded at that stage."
        else:
            sub = "Noted — the stage is confirmed, not guessed."
        return f"✓ {result['label']}", sub

    sub = "Updated your earlier answer." if result["superseded"] else ""
    if result.get("qid") == "attended":
        note = "The Secretary calendar updates at the next analyst run."
        sub = f"{sub} {note}".strip() if sub else note
    return f"✓ Saved — {result['key']} · {result['label']}", sub


# ---------------------------------------------------------------------------
# inbound: the event
# ---------------------------------------------------------------------------


def _event_parameters(event: dict) -> tuple[str, dict]:
    """`(function, {key: value})` from whichever shape Chat sent.

    The reference shows `common.invokedFunction` + `common.parameters` (a map)
    for the current format and `action.actionMethodName` +
    `action.parameters` (a list of {key, value}) for the older one. Both are
    read; the map wins when both are present.
    """
    common = event.get("common") or {}
    action = event.get("action") or {}
    function = common.get("invokedFunction") or action.get("actionMethodName") or ""

    params: dict = {}
    listed = action.get("parameters")
    if isinstance(listed, list):
        for item in listed:
            if isinstance(item, dict) and "key" in item:
                params[str(item["key"])] = item.get("value")
    mapped = common.get("parameters")
    if isinstance(mapped, dict):
        params.update(mapped)
    elif isinstance(mapped, list):
        for item in mapped:
            if isinstance(item, dict) and "key" in item:
                params[str(item["key"])] = item.get("value")
    return str(function), params


def _replace_tapped_line(cards: list, token: str, line: str) -> bool:
    """Swap the buttonList holding `token`'s button for a text line, in place.

    Returns False when no button carries the token — the card Chat echoed
    back may not be ours, or may have been rebuilt.
    """
    for card_v2 in cards:
        card = (card_v2 or {}).get("card") or {}
        for section in card.get("sections") or []:
            widgets = section.get("widgets") or []
            for index, widget in enumerate(widgets):
                buttons = ((widget or {}).get("buttonList") or {}).get("buttons") or []
                for button in buttons:
                    params = (((button.get("onClick") or {}).get("action") or {})
                              .get("parameters") or [])
                    for param in params:
                        if (isinstance(param, dict)
                                and param.get("key") == TOKEN_PARAM
                                and param.get("value") == token):
                            widgets[index] = {"textParagraph": {"text": line}}
                            return True
    return False


def _original_text(event: dict) -> str:
    """The posted message text, with the `<users/…>` mention markup kept.

    `formattedText` carries mentions as `<users/ID>`; `text` renders them as
    a plain "@Name" that would post literally on update.
    """
    message = event.get("message") or {}
    return message.get("formattedText") or message.get("text") or ""


def _card_clicked(event: dict) -> dict:
    function, params = _event_parameters(event)
    token = params.get(TOKEN_PARAM)
    if function and function != TAP_FUNCTION:
        logger.info("chat_app: unknown function %r on a card click", function)

    headline, sub = "", ""
    if not token:
        line = EXPIRED_LINE
    else:
        try:
            with db.get_conn() as conn:
                result = answers.record_tap(conn, str(token))
        except InvalidToken as exc:
            # Expired or forged — never say which.
            logger.info("chat_app: tap rejected: %s", exc)
            line = EXPIRED_LINE
        except answers.UnknownPing as exc:
            logger.warning("chat_app: tap for a missing ping: %s", exc)
            line = GONE_LINE
        except Exception:
            logger.exception("chat_app: tap failed unexpectedly")
            line = FAILED_LINE
        else:
            headline, sub = tap_result_lines(result)
            line = f"{headline}<br>{sub}" if sub else headline

    message = event.get("message") or {}
    cards = copy.deepcopy(message.get("cardsV2") or [])
    if token and cards and _replace_tapped_line(cards, str(token), line):
        return {
            "actionResponse": {"type": "UPDATE_MESSAGE"},
            "text": _original_text(event),
            "cardsV2": cards,
        }

    # No card to rewrite (Chat did not echo it, or the button is not ours):
    # say the result in the thread rather than blanking the message.
    logger.info("chat_app: card not rewritten, answering in a new message")
    return {"actionResponse": {"type": "NEW_MESSAGE"}, "text": line.replace("<br>", "\n")}


def handle_event(event: dict) -> dict:
    """Dispatch one Chat event. Only `CARD_CLICKED` ever records anything."""
    kind = (event or {}).get("type") or ""
    if kind == "ADDED_TO_SPACE":
        return {"text": GREETING}
    if kind == "REMOVED_FROM_SPACE":
        return {}
    if kind == "MESSAGE":
        # Silent, deliberately. A reply he types in a card's thread ("C was
        # Deadlift") reaches the app as a MESSAGE event too, and an app that
        # answers every one with "I do not chat" would talk over the exact
        # corrections the day card asks for. Those replies are read by
        # `secretary_replies` through his own token; the app has nothing to
        # add and says nothing. NOT_A_CHAT is kept for a direct 1:1 message,
        # which the Secretary space never is.
        space_type = ((event.get("space") or {}).get("type") or "").upper()
        if space_type == "DM":
            return {"text": NOT_A_CHAT}
        return {}
    if kind == "CARD_CLICKED":
        return _card_clicked(event)
    logger.info("chat_app: ignoring event type %r", kind)
    return {}


# ---------------------------------------------------------------------------
# outbound: buttons and messages
# ---------------------------------------------------------------------------


def build_action_button(text: str, token: str) -> dict:
    """A card button that invokes us in place, carrying the signed token."""
    return {
        "text": text,
        "onClick": {
            "action": {
                "function": TAP_FUNCTION,
                "parameters": [{"key": TOKEN_PARAM, "value": token}],
            }
        },
    }


def app_configured() -> bool:
    """True when the app can post: a key file on disk and a space to post to."""
    key_file = getattr(settings, "chat_app_key_file", "") or ""
    space = getattr(settings, "secretary_space", "") or ""
    return bool(key_file and os.path.exists(key_file) and space)


def send_as_app(space: str, body: dict, thread_key: str | None = None) -> dict:
    """Post `body` to `space` as the Chat app. Returns `{message_ref, thread_ref}`.

    `thread_key` keeps a card's replies in their own thread, exactly as the
    webhook senders do with `?threadKey=`. Raises `DeliveryError`, scrubbed.
    """
    if not space:
        raise DeliveryError("SECRETARY_SPACE is not set — nowhere to post")
    if not space.startswith("spaces/"):
        raise DeliveryError("SECRETARY_SPACE must look like spaces/XXXX")

    payload = dict(body)
    kwargs: dict = {"parent": space}
    if thread_key:
        payload["thread"] = {"threadKey": thread_key}
        kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    kwargs["body"] = payload

    try:
        creds = service_account.Credentials.from_service_account_file(
            settings.chat_app_key_file, scopes=[CHAT_BOT_SCOPE],
        )
        service = build("chat", "v1", credentials=creds, cache_discovery=False)
        data = service.spaces().messages().create(**kwargs).execute()
    except DeliveryError:
        raise
    except Exception as exc:  # noqa: BLE001 - one error class for the senders
        raise DeliveryError(f"chat app send failed: {_scrub(exc)}") from exc

    refs = {
        "message_ref": (data or {}).get("name"),
        "thread_ref": ((data or {}).get("thread") or {}).get("name"),
    }
    logger.info("chat_app: posted %s in %s", refs["message_ref"], space)
    return refs
