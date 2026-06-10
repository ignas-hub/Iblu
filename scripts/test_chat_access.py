#!/usr/bin/env python3
"""Answer the open question: can the service account read Ignas's Chat?

This impersonates GOOGLE_DELEGATED_USER and calls the Google Chat API to list
spaces/DMs. It prints a clear PASS/FAIL so a non-developer can read the result.

Usage:
    export GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account.json
    export GOOGLE_DELEGATED_USER=ignas@blanklabel.team
    python scripts/test_chat_access.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Force a live attempt regardless of DRY_RUN.
os.environ["DRY_RUN"] = "false"


def main() -> int:
    from iblu_keeper.config import settings
    from iblu_keeper.google_auth import build_service

    print("iblu-keeper — Google Chat access test")
    print("-" * 50)
    print(f"Impersonating: {settings.google_delegated_user}")
    if not settings.has_google_credentials:
        print("\nFAIL: No service-account key found.")
        print("Set GOOGLE_SERVICE_ACCOUNT_FILE (or _B64) and try again.")
        return 1

    try:
        service = build_service(
            "chat",
            "v1",
            scopes=["https://www.googleapis.com/auth/chat.spaces.readonly"],
        )
        resp = service.spaces().list(pageSize=50).execute()
        spaces = resp.get("spaces", [])
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: the Chat API call was rejected.\n\n    {exc}\n")
        print("This likely means service-account + domain-wide delegation cannot")
        print("read this user's Chat. Keep DRY_RUN=true and we'll use a fallback")
        print("backend for Chat (Gmail/Calendar are unaffected).")
        return 2

    print(f"\nPASS: the Chat API returned {len(spaces)} space(s):\n")
    for s in spaces:
        name = s.get("displayName") or "(direct message)"
        print(f"  - {name}  [{s.get('spaceType', s.get('type', '?'))}]  {s.get('name')}")
    if not spaces:
        print("  (none — the API worked but this user is in no visible spaces)")
    print("\nIf your real DMs/spaces appear above, the service-account path works.")
    print("You can then set DRY_RUN=false to go live with Chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
