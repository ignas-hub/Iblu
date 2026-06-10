#!/usr/bin/env python3
"""One-time: authorize iblu-keeper to access ONLY your Google account.

Run this once on a computer with a web browser (e.g. your Mac). It opens a
Google "Allow" page; after you approve, it saves an OAuth refresh token to
data/token.json. That token is scoped to just your account — it cannot touch
anyone else, and no service-account key is involved.

Usage:
    export GOOGLE_OAUTH_CLIENT_ID=...apps.googleusercontent.com
    export GOOGLE_OAUTH_CLIENT_SECRET=...
    python scripts/connect_google.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def main() -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    from iblu_keeper.config import settings
    from iblu_keeper.google_auth import SCOPES, _client_config, _save_token

    print("iblu-keeper — connect your Google account")
    print("-" * 50)
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        print("FAIL: set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET first.")
        print("(These come from Point 3 — the OAuth client you created in Google Cloud.)")
        return 1

    flow = InstalledAppFlow.from_client_config(_client_config(), scopes=list(SCOPES))

    print("\nA browser window will open. Sign in as the account you want the")
    print("assistant to use, then click 'Allow'.\n")
    # Opens the browser and runs a tiny local server to catch the response.
    creds = flow.run_local_server(port=8765, prompt="consent", access_type="offline")

    _save_token(creds)
    print(f"\nPASS: saved your token to {settings.google_oauth_token_file}")
    print("This file is your private credential — keep it safe, never commit it.")
    print("\nNext: copy it to the server, or run the test:")
    print("    python scripts/test_chat_access.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
