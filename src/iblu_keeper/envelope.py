"""Response envelope: fetched_at / request_id / query echo on every tool response.

The MCP server's tools must make freshness self-evident and replays detectable.
Every tool response carries:

- ``fetched_at``: ISO-8601 UTC at the moment the tool ran.
- ``request_id``: UUID4 hex, unique per call.
- ``query``: echo of the kwargs the server actually used (sanity check that
  "what I asked" matches "what came back").

For list-returning tools the original list is placed under ``items`` and a
``count`` is added. For dict-returning tools the metadata is merged at top
level alongside whatever fields the tool already returns.

The envelope is applied via the ``stamped`` decorator. Tool functions are
written to return their natural shape (list or dict); the decorator does the
rest. The same decorator is used by the test suite to verify the envelope.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable


def now_iso() -> str:
    """ISO-8601 UTC timestamp with Z suffix (consistent with Google APIs)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def stamped(fn: Callable[..., Any]) -> Callable[..., dict]:
    """Decorator: wrap a tool's return value in the freshness envelope.

    The wrapped function returns a dict. If the underlying tool returned a
    list, it's placed under ``items`` with a ``count`` sibling. If it
    returned a dict, those fields are merged at the top level. Any other
    return type goes under ``result``.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        # Snap timestamp + id BEFORE the call so they reflect when the request
        # actually began, not when post-processing finishes.
        envelope: dict[str, Any] = {
            "fetched_at": now_iso(),
            "request_id": uuid.uuid4().hex,
            "query": {k: v for k, v in kwargs.items() if not k.startswith("_")},
        }
        result = fn(*args, **kwargs)
        if isinstance(result, list):
            envelope["items"] = result
            envelope["count"] = len(result)
        elif isinstance(result, dict):
            # Avoid silently overwriting metadata if a tool happens to include
            # one of these keys; keep the tool's value and rename the meta.
            for key in ("fetched_at", "request_id", "query"):
                if key in result:
                    envelope.setdefault(f"_envelope_{key}", envelope.pop(key))
            envelope.update(result)
        else:
            envelope["result"] = result
        return envelope

    return wrapper
