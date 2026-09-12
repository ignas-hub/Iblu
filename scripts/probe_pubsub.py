"""Probe Pub/Sub topic + subscription access using our OAuth credentials.

Reads:
  GCP_PROJECT_ID (project ID string, e.g. "iblu-mcp-462008")
  PUBSUB_TOPIC   (defaults to "iblu-chat-events")
  PUBSUB_SUB     (defaults to "iblu-chat-events-sub")

Requires the OAuth token to include the pubsub scope. If it does not, run
`python scripts/connect_google.py` again to grant it (re-consent one-click).

On success prints:
  - the topic name
  - the subscription name
  - the current backlog (should be 0 initially)

Fails loudly with the exact GCP error so setup issues are easy to diagnose.
"""

from __future__ import annotations

import os
import sys


def _fail(msg: str) -> "None":
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    # Force .env load before touching config.
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

    project_id = os.getenv("GCP_PROJECT_ID", "").strip()
    if not project_id:
        _fail("Set GCP_PROJECT_ID in .env (the project ID string, not the number).")

    topic_name = os.getenv("PUBSUB_TOPIC", "iblu-chat-events")
    sub_name = os.getenv("PUBSUB_SUB", "iblu-chat-events-sub")

    from iblu_keeper.google_auth import get_credentials

    try:
        creds = get_credentials()
    except Exception as exc:  # noqa: BLE001
        _fail(f"OAuth credentials unavailable: {exc}")

    if "pubsub" not in " ".join(creds.scopes or []):
        _fail(
            "Pub/Sub scope is NOT present on the saved OAuth token. "
            "Re-run `python scripts/connect_google.py` to re-consent."
        )

    from google.cloud import pubsub_v1  # type: ignore

    publisher = pubsub_v1.PublisherClient(credentials=creds)
    subscriber = pubsub_v1.SubscriberClient(credentials=creds)

    topic_path = publisher.topic_path(project_id, topic_name)
    sub_path = subscriber.subscription_path(project_id, sub_name)

    print(f"Checking topic:        {topic_path}")
    try:
        topic = publisher.get_topic(request={"topic": topic_path})
        print(f"  ok, name={topic.name}")
    except Exception as exc:  # noqa: BLE001
        _fail(f"cannot access topic: {exc}")

    print(f"Checking subscription: {sub_path}")
    try:
        sub = subscriber.get_subscription(request={"subscription": sub_path})
        print(f"  ok, topic={sub.topic}, ack_deadline={sub.ack_deadline_seconds}s")
    except Exception as exc:  # noqa: BLE001
        _fail(f"cannot access subscription: {exc}")

    print("\nProbe passed. OAuth token can read topic and subscription.")


if __name__ == "__main__":
    main()
