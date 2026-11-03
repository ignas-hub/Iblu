"""The HTTPS endpoint Google Chat calls with the app's events.

Registered in `server.py` as:

    from .chat_app_route import chat_events
    mcp.custom_route("/chat/events", methods=["POST"])(chat_events)

Chat shows the user a red "something went wrong" for any non-200, so like
`/q` this route never returns a 500: an internal failure is logged with its
traceback and answered with a one-line text, HTTP 200. Only a request that
is not from Google Chat gets a 401 — and no detail beyond "unauthorized".
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import settings

logger = logging.getLogger("iblu_keeper.chat_app_route")

FAILED_TEXT = "IBLU could not process that tap — reply in the thread."


async def chat_events(request: Request) -> JSONResponse:
    from . import chat_app

    authorization = request.headers.get("authorization", "")
    try:
        chat_app.verify_chat_request(
            authorization, getattr(settings, "chat_app_project_number", "") or ""
        )
    except chat_app.ChatAppAuthError as exc:
        # The reason stays in the log; the caller learns only that it failed.
        logger.info("chat events: rejected request (%s)", exc)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        event = await request.json()
        if not isinstance(event, dict):
            raise ValueError("event body is not a JSON object")
        return JSONResponse(chat_app.handle_event(event))
    except Exception:
        logger.exception("chat events: failed to handle an event")
        return JSONResponse({"text": FAILED_TEXT}, status_code=200)
