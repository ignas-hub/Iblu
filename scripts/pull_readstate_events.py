"""Pull-drain the iblu-chat-events subscription and print event payloads.

Diagnostic tool. Reads all currently-queued messages (up to `max_messages`,
default 10) with a short timeout, prints each one, then acks and exits.

Usage:
  python scripts/pull_readstate_events.py            # pull up to 10, 5s timeout
  python scripts/pull_readstate_events.py 50 10      # up to 50, 10s timeout
"""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

    project_id = os.getenv("GCP_PROJECT_ID", "").strip()
    if not project_id:
        print("FAIL: set GCP_PROJECT_ID in .env", file=sys.stderr)
        sys.exit(1)
    sub_name = os.getenv("PUBSUB_SUB", "iblu-chat-events-sub")

    max_msgs = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

    from google.cloud import pubsub_v1  # type: ignore

    from iblu_keeper.google_auth import get_credentials

    creds = get_credentials()
    subscriber = pubsub_v1.SubscriberClient(credentials=creds)
    sub_path = subscriber.subscription_path(project_id, sub_name)

    print(f"pulling up to {max_msgs} messages from {sub_path} (timeout {timeout}s)")
    from google.api_core.exceptions import DeadlineExceeded

    try:
        resp = subscriber.pull(
            request={"subscription": sub_path, "max_messages": max_msgs},
            timeout=timeout,
        )
    except DeadlineExceeded:
        print("no messages queued (timed out)")
        return

    if not resp.received_messages:
        print("no messages queued")
        return

    ack_ids = []
    for i, rm in enumerate(resp.received_messages, 1):
        m = rm.message
        print(f"\n--- message {i} ---")
        print(f"  publish_time: {m.publish_time.isoformat() if m.publish_time else '?'}")
        print(f"  attributes:")
        for k, v in (m.attributes or {}).items():
            print(f"    {k}: {v}")
        try:
            payload = json.loads(m.data.decode("utf-8")) if m.data else {}
            print(f"  data:")
            print(json.dumps(payload, indent=4))
        except Exception:  # noqa: BLE001
            print(f"  data (raw): {m.data!r}")
        ack_ids.append(rm.ack_id)

    subscriber.acknowledge(request={"subscription": sub_path, "ack_ids": ack_ids})
    print(f"\nacked {len(ack_ids)} messages")


if __name__ == "__main__":
    main()
