#!/usr/bin/env python3
"""One-time: authorize iblu-keeper to access ONLY your Google account.

Run this once on a computer with a web browser (e.g. your Mac). It opens a
Google "Allow" page; after you approve, it saves an OAuth refresh token to
data/token.json. That token is scoped to just your account — it cannot touch
anyone else, and no service-account key is involved.

Usage:
    python scripts/connect_google.py                      # the primary account
    python scripts/connect_google.py --account choco      # another Workspace

Each Google Workspace needs its OWN OAuth client: IBLU's consent screen is
"Internal" to blanklabel.team, so ignacio@chocoagency.com and admin@deadlift.io
cannot authorise it. One Cloud project + Internal OAuth client per domain, its
id/secret in .env as GOOGLE_ACCOUNT_<ALIAS>_CLIENT_ID / _CLIENT_SECRET, and one
token file per alias (data/token.<alias>.json).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def main() -> int:
    import argparse

    from google_auth_oauthlib.flow import InstalledAppFlow

    from iblu_keeper.config import settings
    from iblu_keeper.google_auth import SCOPES, _save_token

    parser = argparse.ArgumentParser(description="Authorize one Google account.")
    parser.add_argument(
        "--account",
        default=None,
        help="alias to authorize (e.g. choco, deadlift). Default: the primary account.",
    )
    args = parser.parse_args()

    alias = (args.account or settings.primary_alias).lower()
    account = settings.account(alias)

    print(f"iblu-keeper — connect the {alias!r} Google account")
    print("-" * 50)
    if account["email"]:
        print(f"Expected account: {account['email']}")
    if not account["client_id"] or not account["client_secret"]:
        env = "GOOGLE_OAUTH" if alias == settings.primary_alias else f"GOOGLE_ACCOUNT_{alias.upper()}"
        print(f"FAIL: set {env}_CLIENT_ID and {env}_CLIENT_SECRET first.")
        print("Each Workspace needs its own Cloud project + Internal OAuth client.")
        return 1

    client_config = {
        "installed": {
            "client_id": account["client_id"],
            "client_secret": account["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost:8765/"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=list(SCOPES))

    # Headless-friendly: don't try to launch a browser here. Print the auth
    # URL and wait for Google to redirect to http://localhost:8765/. From a
    # Mac SSH session started with:
    #     ssh -L 8765:localhost:8765 ignas@178.104.122.152
    # opening that URL in the Mac browser sends the callback through the
    # tunnel to this script.
    print(
        "\nOpen the URL below in a browser (from your Mac in an SSH tunnel).\n"
        "Sign in as the account you want the assistant to use, click 'Allow'.\n"
    )
    creds = flow.run_local_server(
        port=8765,
        prompt="consent",
        access_type="offline",
        open_browser=False,
    )

    # Refuse to save a token for the wrong account — authorising as the wrong
    # identity would silently attribute one person's mail to another.
    if account["email"]:
        try:
            import google.oauth2.credentials  # noqa: F401
            from googleapiclient.discovery import build

            who = build("oauth2", "v2", credentials=creds).userinfo().get().execute()
            signed_in = (who.get("email") or "").lower()
            if signed_in and signed_in != account["email"].lower():
                print(
                    f"\nFAIL: you signed in as {signed_in}, but alias {alias!r} "
                    f"expects {account['email']}. Nothing was saved."
                )
                return 1
        except Exception as exc:  # noqa: BLE001 - the check is best effort
            print(f"(could not verify which account signed in: {exc})")

    _save_token(creds, account["token_file"])
    print(f"\nPASS: saved the {alias!r} token to {account['token_file']}")
    print("This file is a private credential — keep it safe, never commit it.")
    print(f"\nAdd {alias} to GOOGLE_ACCOUNTS in .env, then check it:")
    print(f"    python -c \"from iblu_keeper.google_auth import build_service; "
          f"print(build_service('gmail','v1',account='{alias}')"
          f".users().getProfile(userId='me').execute()['emailAddress'])\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
