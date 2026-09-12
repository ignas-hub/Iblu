"""Register a Google Workspace Events subscription for Chat read-state.

`spaceReadState.updated` is a USER-scoped event: one subscription targeting
`users/me` covers read-state changes across ALL the user's Chat spaces. So
this script creates exactly one subscription, no per-space fanout.

Usage:
  python scripts/register_readstate_subscription.py

Reads from .env:
  GCP_PROJECT_ID   (project ID string)
  PUBSUB_TOPIC     (defaults to "iblu-chat-events")

Requires OAuth token to have chat.users.readstate scope (already granted).
"""

from __future__ import annotations

import os
import sys


def _list_recent_spaces(limit: int = 10) -> None:
    from iblu_keeper.google_auth import build_service

    chat = build_service("chat", "v1")
    spaces = chat.spaces().list(pageSize=100).execute().get("spaces", [])
    spaces.sort(key=lambda s: s.get("lastActiveTime") or "", reverse=True)
    print(f"Recent {min(limit, len(spaces))} spaces (most-active first):\n")
    for s in spaces[:limit]:
        name = s.get("displayName") or s.get("name", "")
        stype = s.get("spaceType", "")
        print(f"  {s['name']}   [{stype}]   {name}")
    print(
        "\nRe-run with a space id, e.g.:\n"
        f"  python scripts/register_readstate_subscription.py {spaces[0]['name']}"
    )


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

    project_id = os.getenv("GCP_PROJECT_ID", "").strip()
    if not project_id:
        print("FAIL: set GCP_PROJECT_ID in .env", file=sys.stderr)
        sys.exit(1)

    topic = os.getenv("PUBSUB_TOPIC", "iblu-chat-events")
    topic_path = f"projects/{project_id}/topics/{topic}"

    # Workspace Events API — use REST directly. There's a Python client
    # (google-apps-events-subscriptions) but adding a dependency for a
    # 3-line HTTP call is not worth it.
    import requests

    from iblu_keeper.google_auth import get_credentials

    creds = get_credentials()
    if not creds.valid:
        from google.auth.transport.requests import Request

        creds.refresh(Request())

    # User-scoped event: targets `users/<numeric-id>`; covers all the user's
    # spaces. `users/me` is rejected — Workspace Events wants the full name.
    from iblu_keeper.tools.chat import GoogleChatBackend

    user_id = GoogleChatBackend()._ensure_self_id()
    if not user_id:
        print("FAIL: could not resolve self user id via tokeninfo", file=sys.stderr)
        sys.exit(1)

    # Target: user-scoped Chat events use the Cloud Identity resource shape,
    # NOT chat.googleapis.com. See:
    #   https://developers.google.com/workspace/events/guides/events-chat
    # includeResource is deliberately OFF — with it, TTL caps at 4h (24h with
    # domain-wide delegation, which we don't have). Off, TTL can be 7d.
    # We fetch lastReadTime via getSpaceReadState in the worker on each event
    # (one small HTTP call — negligible vs. current 5-sec poll cost).
    body = {
        "targetResource": f"//cloudidentity.googleapis.com/users/{user_id}",
        "eventTypes": ["google.workspace.chat.spaceReadState.v1.updated"],
        "notificationEndpoint": {"pubsubTopic": topic_path},
    }

    print(f"POST workspaceevents.googleapis.com/v1/subscriptions")
    print(f"  target: {body['targetResource']}")
    print(f"  topic:  {topic_path}")
    print()

    resp = requests.post(
        "https://workspaceevents.googleapis.com/v1/subscriptions",
        headers={
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )

    if resp.status_code >= 300:
        print(f"FAIL {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)

    op = resp.json()
    print(f"OK — operation: {op.get('name', '?')}")
    print(f"  done: {op.get('done', False)}")
    if op.get("response"):
        sub = op["response"]
        print(f"  subscription: {sub.get('name', '?')}")
        print(f"  state: {sub.get('state', '?')}")
        print(f"  expires: {sub.get('expireTime', 'n/a')}")


if __name__ == "__main__":
    main()
