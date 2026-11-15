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
        "digitalocean": { ... },               # see _digitalocean_snapshot
    }

``digitalocean`` is read live from the DigitalOcean API with the read-only
token in ``DIGITALOCEAN_TOKEN`` (account, balance, droplets, apps, managed
databases, domains, volumes, load balancers, Kubernetes clusters). It has its
own ``status`` (``ok`` / ``error`` / ``not_configured``) and never raises; a
DO failure leaves the Drive-based hosts report intact and vice versa.

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


def _expected_hours(schedule: str) -> float:
    """Rough expected interval (hours) for a cron 5-field schedule.

    Heuristic — not a full cron parser. Good enough to catch obvious skips:
      "0 4 * * 0"      → 168  (day-of-week set → weekly)
      "5 */6 * * *"    →   6  (hour is */N)
      "0 * * * *"      →   1  (hour is *, minute is fixed)
      "*/15 * * * *"   →   0.25
      anything else    →  24  (assume daily unless we can prove longer)
    """
    parts = schedule.split()
    if len(parts) != 5:
        return 168.0
    minute, hour, _dom, _mon, dow = parts
    if dow not in ("*", "?"):
        return 168.0
    if hour.startswith("*/"):
        try:
            return float(hour[2:])
        except ValueError:
            pass
    if hour == "*" and minute.startswith("*/"):
        try:
            return max(float(minute[2:]) / 60, 0.05)
        except ValueError:
            pass
    if hour == "*":
        return 1.0
    return 24.0


def _classify_task(task: dict) -> tuple[str, float]:
    """Return (status, expected_hours) for one scheduled_tasks entry.

    Handles two shapes: cron (``kind: "cron"`` with ``schedule``) and systemd
    timer (``kind: "timer"`` with ``next_run``). For timers we don't try to
    parse OnCalendar= — freshness comes from last_run, and a not-yet-fired
    timer whose ``next_run`` is still in the future is ``pending``, not
    ``never_ran``.

    ``pending``   — timer installed but hasn't fired yet AND has a future next_run.
    ``never_ran`` — cron file present with no log entry ever.
    ``stale``     — last run > 2× expected interval + 1h grace.
    ``healthy``   — within the expected window.
    """
    hours_since = task.get("hours_since_run", -1)
    schedule = task.get("schedule", "")
    expected = _expected_hours(schedule)

    if hours_since is None or hours_since < 0:
        if task.get("kind") == "timer" and task.get("next_run"):
            next_dt = _parse_iso(task["next_run"])
            if next_dt and next_dt > datetime.now(timezone.utc):
                return "pending", expected
        return "never_ran", expected

    if hours_since > (2 * expected + 1):
        return "stale", expected
    return "healthy", expected


def _derive_status(latest_json: dict) -> tuple[str, str]:
    """Return (status, summary) derived from host ts freshness AND cron health.

    Overall status:
    - ``healthy``  → every host fresh AND every scheduled task healthy.
    - ``degraded`` → any host stale OR any scheduled task stale/missing, but
                     at least one host is fresh.
    - ``error``    → no fresh hosts (or missing hosts field).

    Summary always leads with hosts; appends a cron note when relevant.
    """
    hosts = latest_json.get("hosts") or []
    if not isinstance(hosts, list) or not hosts:
        return "error", "no hosts in latest.json"

    now = datetime.now(timezone.utc)
    fresh: list[str] = []
    stale: list[str] = []
    for h in hosts:
        name = h.get("host", "?")
        # Collectors disagree on the field name: the Hetzner hub and play write
        # `ts`, appsprod, appsdev and the n8n VM write `timestamp`. Reading
        # only `ts` showed three live hosts as stale in every report.
        ts = _parse_iso(h.get("ts") or h.get("timestamp") or "")
        if ts is None:
            stale.append(name)
            continue
        age_hours = (now - ts).total_seconds() / 3600
        (fresh if age_hours <= _STALE_HOST_HOURS else stale).append(name)

    # Aggregate scheduled tasks across all hosts.
    bad_tasks: list[str] = []
    total_tasks = 0
    for h in hosts:
        host_name = h.get("host", "?")
        for task in h.get("scheduled_tasks", []) or []:
            total_tasks += 1
            tstatus, _exp = _classify_task(task)
            if tstatus != "healthy":
                bad_tasks.append(f"{task.get('name', '?')}@{host_name}={tstatus}")

    total = len(hosts)
    fresh_n = len(fresh)
    task_note = f"; cron issues: {', '.join(bad_tasks)}" if bad_tasks else ""

    if fresh_n == total and not bad_tasks:
        return "healthy", f"{fresh_n}/{total} hosts healthy, {total_tasks} tasks ok"
    if fresh_n == 0:
        return "error", f"0/{total} hosts fresh (all stale >{_STALE_HOST_HOURS}h){task_note}"
    stale_names = ", ".join(stale) if stale else "none"
    return (
        "degraded",
        f"{fresh_n}/{total} hosts healthy, {len(stale)} stale ({stale_names}){task_note}",
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


# --------------------------------------------------------------------------- #
# DigitalOcean (read-only)
# --------------------------------------------------------------------------- #
_DO_API = "https://api.digitalocean.com/v2"
_DO_TIMEOUT_SEC = 10
_DO_PER_PAGE = 200


def _do_get(token: str, path: str) -> dict:
    """One GET against the DO API; raises on HTTP or transport errors."""
    import requests

    resp = requests.get(
        f"{_DO_API}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params={"per_page": _DO_PER_PAGE},
        timeout=_DO_TIMEOUT_SEC,
    )
    if resp.status_code == 401:
        raise RuntimeError("DigitalOcean rejected the token (401) — regenerate it")
    if resp.status_code == 403:
        raise RuntimeError("DigitalOcean token lacks read scope for " + path)
    resp.raise_for_status()
    return resp.json()


def _ipv4(droplet: dict, kind: str) -> str:
    for net in (droplet.get("networks") or {}).get("v4") or []:
        if net.get("type") == kind:
            return net.get("ip_address", "")
    return ""


def _digitalocean_snapshot(token: str) -> dict:
    """Everything the read-only token can see, flattened to what a chat needs.

    Each resource list is fetched independently so one failing endpoint (a
    token without that scope, an API hiccup) is reported under ``errors`` and
    the rest still comes back. ``status`` is ``ok`` only when every call
    succeeded; ``notes`` lists droplets, apps and databases that are not in
    their normal running state.
    """
    out: dict = {"status": "ok", "errors": [], "notes": []}

    def fetch(key: str, path: str, shape):
        try:
            out[key] = shape(_do_get(token, path))
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{key}: {exc}")
            out[key] = None

    fetch("account", "/account", lambda d: {
        "email": (d.get("account") or {}).get("email", ""),
        "status": (d.get("account") or {}).get("status", ""),
        "droplet_limit": (d.get("account") or {}).get("droplet_limit"),
        "team": ((d.get("account") or {}).get("team") or {}).get("name", ""),
    })
    fetch("balance", "/customers/my/balance", lambda d: {
        "month_to_date_usage_usd": d.get("month_to_date_usage"),
        "month_to_date_balance_usd": d.get("month_to_date_balance"),
        "account_balance_usd": d.get("account_balance"),
        "generated_at": d.get("generated_at"),
    })
    fetch("droplets", "/droplets", lambda d: [{
        "name": x.get("name"),
        "status": x.get("status"),
        "region": (x.get("region") or {}).get("slug"),
        "size": x.get("size_slug"),
        "public_ip": _ipv4(x, "public"),
        "private_ip": _ipv4(x, "private"),
        "image": " ".join(filter(None, [(x.get("image") or {}).get("distribution"),
                                        (x.get("image") or {}).get("name")])),
        "tags": x.get("tags") or [],
        "created_at": x.get("created_at"),
    } for x in d.get("droplets") or []])
    fetch("apps", "/apps", lambda d: [{
        "name": (x.get("spec") or {}).get("name"),
        "live_url": x.get("live_url"),
        "region": (x.get("region") or {}).get("slug"),
        "deployment_phase": (x.get("active_deployment") or {}).get("phase"),
        "updated_at": x.get("updated_at"),
    } for x in d.get("apps") or []])
    fetch("databases", "/databases", lambda d: [{
        "name": x.get("name"),
        "engine": x.get("engine"),
        "version": x.get("version"),
        "status": x.get("status"),
        "size": x.get("size"),
        "region": x.get("region"),
        "nodes": x.get("num_nodes"),
    } for x in d.get("databases") or []])
    fetch("domains", "/domains", lambda d: [x.get("name") for x in d.get("domains") or []])
    fetch("volumes", "/volumes", lambda d: [{
        "name": x.get("name"),
        "size_gb": x.get("size_gigabytes"),
        "region": (x.get("region") or {}).get("slug"),
        "attached_to": x.get("droplet_ids") or [],
    } for x in d.get("volumes") or []])
    fetch("load_balancers", "/load_balancers", lambda d: [{
        "name": x.get("name"),
        "status": x.get("status"),
        "ip": x.get("ip"),
    } for x in d.get("load_balancers") or []])
    fetch("kubernetes_clusters", "/kubernetes/clusters", lambda d: [{
        "name": x.get("name"),
        "state": (x.get("status") or {}).get("state"),
        "region": x.get("region"),
        "version": x.get("version"),
    } for x in d.get("kubernetes_clusters") or []])

    for x in out.get("droplets") or []:
        if x.get("status") != "active":
            out["notes"].append(f"droplet {x.get('name')} is {x.get('status')}")
    for x in out.get("apps") or []:
        if x.get("deployment_phase") not in (None, "ACTIVE"):
            out["notes"].append(f"app {x.get('name')} deployment {x.get('deployment_phase')}")
    for x in out.get("databases") or []:
        if x.get("status") != "online":
            out["notes"].append(f"database {x.get('name')} is {x.get('status')}")

    if out["errors"]:
        out["status"] = "error"
    counts = {k: len(out[k]) for k in ("droplets", "apps", "databases", "domains",
                                        "volumes", "load_balancers", "kubernetes_clusters")
              if isinstance(out.get(k), list)}
    out["summary"] = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items() if v) \
        or "nothing provisioned"
    if out["notes"]:
        out["summary"] += "; " + "; ".join(out["notes"])
    return out


def _digitalocean() -> dict:
    token = settings.digitalocean_token
    if not token:
        return {"status": "not_configured",
                "summary": "DIGITALOCEAN_TOKEN is not set in .env"}
    try:
        return _digitalocean_snapshot(token)
    except Exception as exc:  # noqa: BLE001  — belt and braces; never raise
        logger.warning("get_infra_status DigitalOcean error: %s", exc)
        return {"status": "error", "summary": f"DigitalOcean read failed: {exc}"}


def get_infra_status() -> dict:
    """Fetch + summarise the infrastructure collector's latest output."""
    folder = settings.infra_folder_id
    tz = settings.infra_hub_timezone or "UTC"

    now = _time.time()
    cache_key = folder or "(no folder)"
    cached = _CACHE.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        # Return a shallow copy so callers can't mutate cache state.
        return dict(cached[1])

    # DigitalOcean is independent of the Drive collector: a DO outage must not
    # hide the hosts report, and a missing Drive folder must not hide DO.
    do = _digitalocean()

    if not folder:
        return {"error": "INFRA_FOLDER_ID is not set in .env", "digitalocean": do}

    try:
        drive = _drive()
        md_bytes = _fetch_file_bytes(drive, folder, "latest.md")
        json_bytes = _fetch_file_bytes(drive, folder, "latest.json")
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_infra_status Drive error: %s", exc)
        return {"error": f"Drive fetch failed: {exc}", "digitalocean": do}

    missing = [n for n, b in (("latest.md", md_bytes), ("latest.json", json_bytes)) if not b]
    if missing:
        return {"error": f"missing files in infra folder: {', '.join(missing)}",
                "digitalocean": do}

    latest_md = md_bytes.decode("utf-8", errors="replace")
    try:
        latest_json = _json.loads(json_bytes.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"latest.json parse failed: {exc}", "digitalocean": do}

    status, summary = _derive_status(latest_json)
    if do.get("status") == "error":
        status = "degraded" if status == "healthy" else status
        summary += "; DigitalOcean: " + do.get("summary", "read failed")
    elif do.get("notes"):
        status = "degraded" if status == "healthy" else status
        summary += "; DigitalOcean: " + "; ".join(do["notes"])
    result = {
        "status": status,
        "summary": summary,
        "generated": latest_json.get("generated") or latest_json.get("generated_at", ""),
        "latest_md": latest_md,
        "latest_json": latest_json,
        "last_updated_local": "~/.infra/latest.{md,json}",
        "collected_at": tz,
        "digitalocean": do,
    }
    _CACHE[cache_key] = (now, result)
    return result


if __name__ == "__main__":  # pragma: no cover — operator check after pasting a token
    import sys

    snap = _digitalocean()
    print(f"status: {snap.get('status')}")
    print(f"summary: {snap.get('summary')}")
    for err in snap.get("errors") or []:
        print(f"error: {err}")
    acct = snap.get("account") or {}
    if acct:
        print(f"account: {acct.get('email')} ({acct.get('status')}) team={acct.get('team') or '-'}")
    bal = snap.get("balance") or {}
    if bal:
        print(f"month to date: ${bal.get('month_to_date_usage_usd')} usage, "
              f"${bal.get('month_to_date_balance_usd')} balance")
    for d in snap.get("droplets") or []:
        print(f"droplet: {d['name']} {d['status']} {d['region']} {d['size']} {d['public_ip']}")
    for a in snap.get("apps") or []:
        print(f"app: {a['name']} {a['deployment_phase']} {a['live_url']}")
    for db in snap.get("databases") or []:
        print(f"database: {db['name']} {db['engine']} {db['status']}")
    sys.exit(0 if snap.get("status") == "ok" else 1)
