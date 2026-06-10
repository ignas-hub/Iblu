"""Local draft store.

Chat and email drafts are stored locally for review in the dashboard. Phase 1
uses a newline-delimited JSON file under DATA_DIR; Phase 2 swaps the backend
for Postgres behind this same module-level API.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.getenv("IBLU_DATA_DIR", "data"))
DRAFTS_FILE = DATA_DIR / "drafts.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_draft(kind: str, payload: dict) -> dict:
    """Persist a draft and return the stored record (with id + timestamp).

    kind: "chat" or "email".
    payload: arbitrary fields describing the draft (conversation/to/text/...).
    """
    record = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "status": "pending",
        "created_at": _now(),
        **payload,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with DRAFTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def list_drafts(kind: str | None = None) -> list[dict]:
    """Return stored drafts, newest first, optionally filtered by kind."""
    if not DRAFTS_FILE.exists():
        return []
    records: list[dict] = []
    with DRAFTS_FILE.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kind is None or rec.get("kind") == kind:
                records.append(rec)
    records.reverse()
    return records
