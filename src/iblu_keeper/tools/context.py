"""Memory / context tools — Phase 2 STUBS.

These interfaces exist now so Claude's tool surface is stable and the server
wiring doesn't change when the Postgres memory layer lands in Phase 2. For now
they are no-ops / return placeholders, and they log through the same path the
real implementation will use.

Phase 2 will back these with the `conversations` / `summaries` tables sketched
in db/schema.sql.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("iblu_keeper.context")


def log_conversation(
    conversation: str,
    role: str,
    text: str,
    source: str = "chat",
) -> dict:
    """Record a turn of conversation for long-term memory (Phase 2).

    Phase 1: writes to the application log only. The signature is final so
    Phase 2 can persist to Postgres without changing callers.
    """
    ts = datetime.now(timezone.utc).isoformat()
    logger.info("context.log_conversation source=%s conv=%s role=%s", source, conversation, role)
    return {
        "status": "stub",
        "note": "Phase 2 will persist this to Postgres.",
        "logged_at": ts,
        "conversation": conversation,
        "role": role,
        "source": source,
    }


def get_summary(window: str = "1d") -> dict:
    """Return a summary of recent activity (Phase 2).

    window: e.g. "1d", "7d". Phase 1 returns a placeholder.
    """
    return {
        "status": "stub",
        "window": window,
        "summary": "Memory layer not implemented yet (Phase 2). No history stored.",
        "items": [],
    }
