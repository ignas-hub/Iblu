"""Translate Google API HttpErrors into actionable messages.

When a Google API call fails inside a tool, the raw ``HttpError`` stack trace
isn't useful to a non-developer. This module wraps each call site and turns
the failure into a concise, actionable message Claude (or the user) can act
on. The wrapped form preserves the original status code and Google's own
error reason so debugging is still possible.

Use as a context manager around each API call::

    with google_api("send_email"):
        service.users().messages().send(...).execute()

If the call fails, a RuntimeError with a friendly message is raised. The
operation name is included so the message tells the user what was being
attempted when it failed.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger("iblu_keeper.google_errors")


_GUIDANCE = {
    400: (
        "The request was rejected by Google as invalid. This usually means a "
        "parameter is wrong (e.g. a bad email address, malformed timestamp, "
        "or unknown resource id). Check the inputs and try again."
    ),
    401: (
        "Google rejected our authentication. The MCP server's OAuth token is "
        "expired, revoked, or its client secret was rotated. Call the "
        "`server_health` tool to confirm; the human may need to re-run the "
        "OAuth bootstrap."
    ),
    403: (
        "Google denied access. Either the OAuth scope is missing for this "
        "operation, the Workspace admin has disabled the relevant feature "
        "(e.g. directory sharing), or the user lacks permission on the "
        "specific resource. Check `server_health` and the OAuth scopes."
    ),
    404: (
        "Google could not find that resource — the id is wrong, was deleted, "
        "or the authenticated account doesn't have visibility into it. "
        "Verify the id; for shared files, ensure the doc is shared with "
        "ignas@blanklabel.team."
    ),
    409: (
        "Conflict — the resource is already in the requested state, or a "
        "concurrent change happened. Fetch the latest state and retry if "
        "necessary."
    ),
    429: (
        "Google rate-limited the request (too many calls in a short window). "
        "Wait ~60 seconds and try again. If this is persistent, the calling "
        "pattern is too aggressive — reduce frequency or batch operations."
    ),
    500: (
        "Google had an internal error. This is transient; wait a moment and "
        "retry once."
    ),
    502: "Google reported a bad gateway (transient). Retry once after a brief pause.",
    503: "Google service unavailable (transient). Retry once after ~30 seconds.",
    504: "Google reported a gateway timeout (transient). Retry once after a brief pause.",
}


def _summary(status: int, reason: str | None, detail: str | None) -> str:
    """Friendly text for an HTTP status, falling back to a generic line."""
    guidance = _GUIDANCE.get(status)
    if guidance:
        return guidance
    if status >= 500:
        return (
            f"Google reported a server-side error (HTTP {status}). "
            "This is transient; retry once after a brief pause."
        )
    return (
        f"Google rejected the request (HTTP {status}, reason={reason or '?'}). "
        f"Detail: {detail or '(none)'}."
    )


def with_google_errors(operation: str):
    """Decorator form of :func:`google_api` for wrapping whole tool functions.

    Applied at the @mcp.tool layer in server.py, this catches any HttpError
    raised by the tool's Google API calls and replaces it with an actionable
    RuntimeError. The model-visible error message tells Claude (and the user)
    what failed and what to try next.
    """
    from functools import wraps

    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            with google_api(operation):
                return fn(*args, **kwargs)

        return wrapper

    return deco


@contextmanager
def google_api(operation: str) -> Iterator[None]:
    """Wrap a Google API call so HttpErrors become actionable RuntimeErrors.

    ``operation`` is a short label for the action being attempted (e.g.
    ``"send_email"``, ``"list_spaces"``) — it's included in the raised
    message so the caller knows what was happening when it failed.
    """
    # Local import — keeps this module importable even when the Google client
    # isn't installed (the tests don't need it).
    try:
        from googleapiclient.errors import HttpError  # type: ignore
    except Exception:  # pragma: no cover
        HttpError = Exception  # noqa: N806

    try:
        yield
    except HttpError as exc:  # type: ignore[misc]
        status = getattr(exc.resp, "status", None) or getattr(exc, "status_code", None)
        try:
            status = int(status) if status is not None else 0
        except Exception:  # noqa: BLE001
            status = 0
        # Try to pull Google's reason + detail from the error body.
        reason = None
        detail = None
        try:
            import json as _json

            body = _json.loads(exc.content.decode("utf-8")) if exc.content else {}
            err = body.get("error", {}) if isinstance(body, dict) else {}
            detail = err.get("message")
            errors_list = err.get("errors") or []
            if errors_list:
                reason = errors_list[0].get("reason")
        except Exception:  # noqa: BLE001 - best-effort
            pass

        summary = _summary(status, reason, detail)
        msg = (
            f"Google API call '{operation}' failed (HTTP {status or '?'}). "
            f"{summary}"
        )
        if detail and detail not in summary:
            msg += f" Google said: {detail}"
        logger.warning("%s", msg)
        raise RuntimeError(msg) from exc
