#!/usr/bin/env python3
"""Answer the open question: can the assistant read your Chat?

Uses your saved single-user OAuth token (from scripts/connect_google.py) to call
the Google Chat API and list your spaces/DMs. Prints a clear PASS/FAIL.

Usage (after connect_google.py has produced data/token.json):
    export GOOGLE_OAUTH_CLIENT_ID=...
    export GOOGLE_OAUTH_CLIENT_SECRET=...
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
    print(f"Account: {settings.google_user_email}")
    if not settings.has_google_credentials:
        print("\nFAIL: not connected yet.")
        print("Run `python scripts/connect_google.py` first to authorize your account.")
        return 1

    try:
        service = build_service("chat", "v1")
        resp = service.spaces().list(pageSize=50).execute()
        spaces = resp.get("spaces", [])
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: the Chat API call was rejected.\n\n    {exc}\n")
        print("This likely means the Chat API can't read this account's chats this")
        print("way. Keep DRY_RUN=true and we'll use a fallback backend for Chat")
        print("(Gmail/Calendar are unaffected).")
        return 2

    print(f"\nPASS: the Chat API returned {len(spaces)} space(s):\n")
    for s in spaces:
        name = s.get("displayName") or "(direct message)"
        print(f"  - {name}  [{s.get('spaceType', s.get('type', '?'))}]  {s.get('name')}")
    if not spaces:
        print("  (none — the API worked but this account is in no visible spaces)")
    print("\nIf your real DMs/spaces appear above, Chat works. Set DRY_RUN=false to go live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
