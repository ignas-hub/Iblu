"""Infrastructure status tool.

Reads the collector's ``latest.md`` (human-readable) and ``latest.json``
(structured) from a Drive folder and returns a compact status summary the
LLM can quote in chat. Derives ``status`` and ``summary`` from host ``ts``
freshness — the JSON emitted by the collector doesn't carry those fields
itself.

Contract per the request spec::

    get_infra_status() -> {
        "status": "healthy|degraded|error",
        "summary": "4/4 hosts healthy" | "3/4 hosts healthy, 1 stale (n8n)",
        "generated": "2026-07-19T11:02:20Z",   # from latest.json
        "latest_md": "# Infrastructure Status\\n...",
        "latest_json": { ... },                # parsed dict
        "last_updated_local": "~/.infra/latest.{md,json}",
        "collected_at": "Europe/Zagreb",       # from INFRA_HUB_TIMEZONE
    }

On failure returns ``{"error": "..."}``. Results are cached in-process for
five minutes so repeated calls in the same conversation don't hit Drive
each time.
"""

from __future__ import annotations

import json as _json
import logging
import time as _time
from datetime import datetime, timezone

from ..config import settings


logger = logging.getLogger("iblu_keeper.tools.infra")

_CACHE: dict[str, tuple[float, dict]] = {}  # folder_id → (unix_ts, result)
_CACHE_TTL_SEC = 300  # 5 minutes
_STALE_HOST_HOURS = 24  # a host without a fresh ts within N hours = stale


def _drive():
    from ..google_auth import build_service

    return build_service("drive", "v3")


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; None on failure. Accepts trailing 'Z'."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _derive_status(latest_json: dict) -> tuple[str, str]:
    """Return (status, summary) derived from host ts freshness.

    - ``healthy``  → every host reports a ts within the freshness window.
    - ``degraded`` → some hosts are stale but at least one is fresh.
    - ``error``    → the JSON is missing hosts entirely or none are fresh.
    """
    hosts = latest_json.get("hosts") or []
    if not isinstance(hosts, list) or not hosts:
        return "error", "no hosts in latest.json"

    now = datetime.now(timezone.utc)
    fresh: list[str] = []
    stale: list[str] = []
    for h in hosts:
        name = h.get("host", "?")
        ts = _parse_iso(h.get("ts", ""))
        if ts is None:
            stale.append(name)
            continue
        age_hours = (now - ts).total_seconds() / 3600
        (fresh if age_hours <= _STALE_HOST_HOURS else stale).append(name)

    total = len(hosts)
    fresh_n = len(fresh)
    if fresh_n == total:
        return "healthy", f"{fresh_n}/{total} hosts healthy"
    if fresh_n == 0:
        return "error", f"0/{total} hosts fresh (all stale >{_STALE_HOST_HOURS}h)"
    stale_names = ", ".join(stale)
    return (
        "degraded",
        f"{fresh_n}/{total} hosts healthy, {len(stale)} stale ({stale_names})",
    )


def _fetch_file_bytes(drive, folder_id: str, name: str) -> bytes | None:
    """Find a file by name inside ``folder_id`` and return its bytes.

    Returns None if the file isn't in the folder. Raises on Drive API errors
    (caller catches).
    """
    resp = drive.files().list(
        q=f"'{folder_id}' in parents and name = '{name}' and trashed = false",
        fields="files(id,name,mimeType)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    if not files:
        return None
    return drive.files().get_media(
        fileId=files[0]["id"], supportsAllDrives=True,
    ).execute()


def get_infra_status() -> dict:
    """Fetch + summarise the infrastructure collector's latest output."""
    folder = settings.infra_folder_id
    tz = settings.infra_hub_timezone or "UTC"

    if not folder:
        return {"error": "INFRA_FOLDER_ID is not set in .env"}

    now = _time.time()
    cached = _CACHE.get(folder)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        # Return a shallow copy so callers can't mutate cache state.
        return dict(cached[1])

    try:
        drive = _drive()
        md_bytes = _fetch_file_bytes(drive, folder, "latest.md")
        json_bytes = _fetch_file_bytes(drive, folder, "latest.json")
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_infra_status Drive error: %s", exc)
        return {"error": f"Drive fetch failed: {exc}"}

    missing = [n for n, b in (("latest.md", md_bytes), ("latest.json", json_bytes)) if not b]
    if missing:
        return {"error": f"missing files in infra folder: {', '.join(missing)}"}

    latest_md = md_bytes.decode("utf-8", errors="replace")
    try:
        latest_json = _json.loads(json_bytes.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"latest.json parse failed: {exc}"}

    status, summary = _derive_status(latest_json)
    result = {
        "status": status,
        "summary": summary,
        "generated": latest_json.get("generated") or latest_json.get("generated_at", ""),
        "latest_md": latest_md,
        "latest_json": latest_json,
        "last_updated_local": "~/.infra/latest.{md,json}",
        "collected_at": tz,
    }
    _CACHE[folder] = (now, result)
    return result
