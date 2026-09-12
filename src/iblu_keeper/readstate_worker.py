"""Background worker that maintains an in-memory Chat read-state cache.

Subscribes to Cloud Pub/Sub topic ``iblu-chat-events`` (populated by Google
Workspace Events for ``google.workspace.chat.spaceReadState.v1.updated``).
Each event carries a ``spaceReadState`` payload for one space; we update our
cache accordingly.

Public surface:

    start_worker() -> None
        Idempotent. Kick off the pull loop in a daemon thread if configured
        and not already running. Safe to call at server startup; a no-op if
        Pub/Sub is not configured (missing GCP_PROJECT_ID, missing scope,
        or DRY_RUN).

    get_last_read_time(space_id: str) -> str
        Cached ``lastReadTime`` (RFC3339 string) for a space, or "" if unknown.

    is_ready() -> bool
        True once the worker has confirmed a live pull stream to Pub/Sub.

    seed_from_scan(entries: dict[str, str]) -> None
        Bulk-load initial cache from a full spaces.list scan. Merges with
        anything the pull loop may have already stored.

Concurrency: a single background thread runs the streaming pull; the cache
is a plain dict protected by a lock. Reads are ~50 ns; writes are rare
(only on real read-state changes).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from .config import settings

logger = logging.getLogger("iblu_keeper.readstate_worker")


# --------------------------------------------------------------------------- #
# Module state (all guarded by _lock)
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_cache: dict[str, str] = {}                # space_id → lastReadTime (RFC3339)
_last_event_at: float | None = None        # epoch of most recent update
_stream_future: Any = None                 # google.cloud.pubsub StreamingPullFuture
_worker_thread: threading.Thread | None = None
_ready = threading.Event()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def is_ready() -> bool:
    """True once the pull stream is live and receiving (or ready to)."""
    return _ready.is_set()


def get_last_read_time(space_id: str) -> str:
    """Return cached lastReadTime for space or empty string."""
    with _lock:
        return _cache.get(space_id, "")


def seed_from_scan(entries: dict[str, str]) -> None:
    """Bulk-merge {space_id: last_read_time} into the cache."""
    with _lock:
        for sid, lrt in entries.items():
            if lrt:
                _cache[sid] = lrt


def snapshot() -> dict[str, Any]:
    """Debug/health snapshot of worker state."""
    with _lock:
        return {
            "cached_spaces": len(_cache),
            "last_event_at": _last_event_at,
            "ready": _ready.is_set(),
            "worker_alive": bool(_worker_thread and _worker_thread.is_alive()),
        }


def start_worker() -> None:
    """Start the Pub/Sub pull thread + subscription-refresh thread.

    Idempotent; no-op if not configured (missing project id or DRY_RUN).
    """
    global _worker_thread

    if settings.use_mock:
        logger.info("readstate_worker skipped (DRY_RUN)")
        return

    project_id = os.getenv("GCP_PROJECT_ID", "").strip()
    sub_name = os.getenv("PUBSUB_SUB", "iblu-chat-events-sub")
    topic_name = os.getenv("PUBSUB_TOPIC", "iblu-chat-events")
    if not project_id:
        logger.info("readstate_worker skipped (no GCP_PROJECT_ID)")
        return

    with _lock:
        if _worker_thread and _worker_thread.is_alive():
            logger.info("readstate_worker already running")
            return

        t = threading.Thread(
            target=_run_pull_loop,
            args=(project_id, sub_name),
            name="iblu-readstate-pull",
            daemon=True,
        )
        _worker_thread = t
        t.start()

        # Refresh loop: workspace-events subscriptions expire after 7 days
        # when includeResource is off. Re-create at startup + every 6 days.
        r = threading.Thread(
            target=_run_refresh_loop,
            args=(project_id, topic_name),
            name="iblu-readstate-refresh",
            daemon=True,
        )
        r.start()

    logger.info(
        "readstate_worker started (project=%s sub=%s topic=%s)",
        project_id, sub_name, topic_name,
    )


def _run_refresh_loop(project_id: str, topic_name: str) -> None:
    """Ensure exactly one active Workspace Events subscription exists.

    On startup: list subscriptions targeting our user for the readstate event
    type; if none is active, create one. Re-run every 6 days to renew.
    """
    while True:
        try:
            _ensure_subscription(project_id, topic_name)
        except Exception:  # noqa: BLE001
            logger.exception("readstate subscription refresh failed; retrying in 1h")
            time.sleep(3600)
            continue
        # 6 days — subscription's own TTL is 7 days.
        time.sleep(6 * 24 * 3600)


def _ensure_subscription(project_id: str, topic_name: str) -> None:
    """Idempotent: create a readstate subscription if none is live for us."""
    import requests

    from .google_auth import get_credentials
    from .tools.chat import GoogleChatBackend

    creds = get_credentials()
    if not creds.valid:
        from google.auth.transport.requests import Request

        creds.refresh(Request())

    user_id = GoogleChatBackend()._ensure_self_id()
    if not user_id:
        logger.warning("readstate refresh: could not resolve self user id")
        return

    target = f"//cloudidentity.googleapis.com/users/{user_id}"
    topic_path = f"projects/{project_id}/topics/{topic_name}"
    event_type = "google.workspace.chat.spaceReadState.v1.updated"
    headers = {"Authorization": f"Bearer {creds.token}"}

    # List existing subscriptions filtered to our target + event type.
    list_url = (
        "https://workspaceevents.googleapis.com/v1/subscriptions"
        f'?filter=event_types:"{event_type}"'
    )
    try:
        r = requests.get(list_url, headers=headers, timeout=15)
        if r.status_code >= 300:
            logger.warning("readstate list subs failed %s: %s", r.status_code, r.text)
        else:
            for sub in r.json().get("subscriptions", []) or []:
                if (
                    sub.get("targetResource") == target
                    and sub.get("state") == "ACTIVE"
                    and sub.get("notificationEndpoint", {}).get("pubsubTopic") == topic_path
                ):
                    logger.info(
                        "readstate sub already live: %s (expires %s)",
                        sub.get("name"), sub.get("expireTime"),
                    )
                    return
    except Exception:  # noqa: BLE001
        logger.exception("readstate list-subs raised; will still try create")

    # Create fresh.
    body = {
        "targetResource": target,
        "eventTypes": [event_type],
        "notificationEndpoint": {"pubsubTopic": topic_path},
    }
    resp = requests.post(
        "https://workspaceevents.googleapis.com/v1/subscriptions",
        headers={**headers, "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    if resp.status_code >= 300:
        logger.error("readstate subscription create FAILED %s: %s", resp.status_code, resp.text)
        return
    op = resp.json()
    sub = op.get("response", {}) if op.get("done") else {}
    logger.info(
        "readstate sub created: %s (expires %s)",
        sub.get("name", "?"), sub.get("expireTime", "?"),
    )


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _fetch_last_read_time(space_id: str) -> str:
    """Fetch lastReadTime for a space via Chat API. '' on failure."""
    from googleapiclient.errors import HttpError  # type: ignore

    from .google_auth import build_service

    service = build_service("chat", "v1")
    try:
        state = (
            service.users()
            .spaces()
            .getSpaceReadState(name=f"users/me/{space_id}/spaceReadState")
            .execute()
        )
        return state.get("lastReadTime", "") or ""
    except HttpError as exc:
        logger.warning("getSpaceReadState(%s) failed: %s", space_id, exc)
        return ""


def _handle_message(message) -> None:  # noqa: ANN001 - pubsub Message
    """Decode a Pub/Sub message and update the cache.

    Our subscription runs with ``includeResource=False`` (for 7-day TTL), so
    the payload carries only the resource NAME:

        {"spaceReadState": {"name": "users/{uid}/spaces/{sid}/spaceReadState"}}

    We fetch the actual lastReadTime via getSpaceReadState. One extra HTTP
    call per event, but read-state events are rare (only on real reads).
    """
    global _last_event_at

    try:
        attrs = dict(message.attributes) if message.attributes else {}
        event_type = attrs.get("ce-type", "")
        body_raw = message.data.decode("utf-8") if message.data else "{}"

        import json

        body = json.loads(body_raw)

        srs = body.get("spaceReadState") or body.get("space_read_state") or {}
        name = srs.get("name", "") or srs.get("Name", "")

        # Extract space id from "users/{uid}/spaces/{sid}/spaceReadState".
        space_id = ""
        parts = name.split("/")
        if "spaces" in parts:
            i = parts.index("spaces")
            if i + 1 < len(parts):
                space_id = f"spaces/{parts[i + 1]}"

        if not space_id:
            logger.warning(
                "readstate event without usable name (attrs=%r body=%r)",
                attrs, body,
            )
            return

        last_read = _fetch_last_read_time(space_id)
        if last_read:
            with _lock:
                _cache[space_id] = last_read
                _last_event_at = time.time()
            logger.info(
                "readstate event: %s lastRead=%s (type=%s)",
                space_id, last_read, event_type,
            )
        else:
            logger.warning("readstate event for %s but no lastReadTime fetched", space_id)
    except Exception:  # noqa: BLE001
        logger.exception("readstate message handler crashed; acking to avoid loop")
    finally:
        message.ack()


def _run_pull_loop(project_id: str, sub_name: str) -> None:
    """Long-running: open a streaming pull and stay open forever.

    Reconnects with exponential backoff on any exception.
    """
    global _stream_future

    from google.api_core import exceptions as gax_exc  # type: ignore
    from google.cloud import pubsub_v1  # type: ignore

    from .google_auth import get_credentials

    backoff = 5.0
    while True:
        try:
            creds = get_credentials()
            subscriber = pubsub_v1.SubscriberClient(credentials=creds)
            sub_path = subscriber.subscription_path(project_id, sub_name)

            logger.info("readstate_worker opening streaming pull on %s", sub_path)
            future = subscriber.subscribe(sub_path, callback=_handle_message)
            with _lock:
                _stream_future = future
            _ready.set()
            backoff = 5.0  # reset on successful open

            try:
                future.result()  # blocks forever until stream fails
            except (gax_exc.GoogleAPICallError, Exception) as exc:  # noqa: BLE001
                logger.warning("readstate stream ended: %s", exc)
            finally:
                _ready.clear()
                try:
                    future.cancel()
                except Exception:  # noqa: BLE001
                    pass

        except Exception:  # noqa: BLE001
            logger.exception("readstate_worker crashed; will retry")

        time.sleep(backoff)
        backoff = min(backoff * 2, 300.0)
