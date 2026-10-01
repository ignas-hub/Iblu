#!/usr/bin/env python3
"""One-time: authorize iblu-keeper to access ONLY your Google account.

Run this once on a computer with a web browser (e.g. your Mac). It opens a
Google "Allow" page; after you approve, it saves an OAuth refresh token to
data/token.json. That token is scoped to just your account — it cannot touch
anyone else, and no service-account key is involved.

Usage:
    python scripts/connect_google.py                      # the primary account
    python scripts/connect_google.py --account choco      # another Workspace
    python scripts/connect_google.py --manual             # from a phone

`--manual` exists because the default flow needs two things a phone does not
have: a desktop browser, and an SSH tunnel carrying Google's redirect back to
`http://localhost:8765/` on this box.

It works by letting that redirect fail. The authorisation code is not something
the loopback server computes — it is a string Google puts in the redirect URL.
So the browser can go to `http://localhost:8765/?code=...`, show "cannot
connect", and the code is still sitting in the address bar. Copy the whole URL,
paste it here, and the token exchange happens on this box.

Nothing is weakened by this. The code is single-use, expires in about a minute,
and is worthless without the client secret, which never leaves the box. The
account check below still runs, so a wrong sign-in is refused exactly as it is
in the tunnelled flow.

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

REDIRECT_URI = "http://localhost:8765/"


def code_from_paste(pasted: str) -> str:
    """The authorisation code out of whatever he pasted.

    Accepts the whole redirect URL (what Safari leaves in the address bar after
    it fails to connect), or just the code on its own. Asking for the URL and
    accepting the code costs nothing and removes a step where a careless
    selection loses the leading `4/`.
    """
    from urllib.parse import parse_qs, unquote, urlparse

    pasted = pasted.strip().strip('"').strip("'")
    if not pasted:
        raise ValueError("nothing pasted")
    if "://" not in pasted and "code=" not in pasted:
        return unquote(pasted)

    query = parse_qs(urlparse(pasted).query if "://" in pasted else pasted)
    if query.get("error"):
        raise ValueError(
            f"Google refused the request: {query['error'][0]}. "
            "If that is access_denied, the Allow button was not pressed."
        )
    codes = query.get("code")
    if not codes or not codes[0].strip():
        raise ValueError(
            "no `code=` in that URL. Paste the address bar of the page that "
            "failed to load, not the consent page."
        )
    return codes[0].strip()


def main() -> int:
    import argparse

    from google_auth_oauthlib.flow import InstalledAppFlow

    from iblu_keeper.config import settings
    from iblu_keeper.google_auth import scopes_for, _save_token

    parser = argparse.ArgumentParser(description="Authorize one Google account.")
    parser.add_argument(
        "--account",
        default=None,
        help="alias to authorize (e.g. choco, deadlift). Default: the primary account.",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="print the URL and wait for the redirect to be pasted back — for a "
             "phone, or anywhere without an SSH tunnel to port 8765",
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
    # Non-primary accounts do NOT request the Pub/Sub scope. It is a Google
    # Cloud Platform scope, and a token that carries one falls under the
    # Workspace's "Google Cloud session control" policy — which is what made
    # Deadlift and Choco fail every refresh with `invalid_rapt` while BLT,
    # whose Workspace does not enforce that policy, ran for three months on a
    # single sign-in. Only the readstate worker uses Pub/Sub, and only on the
    # primary account.
    scopes = scopes_for(alias)
    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
    print(f"Requesting {len(scopes)} scopes"
          + ("" if alias == settings.primary_alias else " (no Pub/Sub — see comment above)"))

    # Pre-select the account on Google's own page. Without it the URL lands on
    # whichever identity the browser happens to be signed in as, and with three
    # Workspaces in one browser that is a coin toss — the check further down
    # then refuses the token and the whole flow has to be redone. Google treats
    # this as a hint, not a lock: he can still switch.
    auth_kwargs = {
        "prompt": "consent",
        "access_type": "offline",
        **({"login_hint": account["email"]} if account["email"] else {}),
    }

    if args.manual:
        flow.redirect_uri = REDIRECT_URI
        url, _ = flow.authorization_url(**auth_kwargs)
        print(
            f"\n1. Open this on the phone, signed in as {account['email']}:\n\n"
            f"{url}\n\n"
            "2. Tap Allow. The next page will fail to load — "
            "'Safari cannot open the page'. That is expected and correct: "
            f"nothing is listening on {REDIRECT_URI} from the phone.\n\n"
            "3. Copy the whole address from the address bar of THAT failed page "
            "and paste it below.\n"
        )
        try:
            pasted = input("redirect URL (or just the code): ")
        except (EOFError, KeyboardInterrupt):
            print("\nnothing pasted — nothing saved.")
            return 1
        try:
            code = code_from_paste(pasted)
        except ValueError as exc:
            print(f"\nFAIL: {exc}")
            return 1
        try:
            flow.fetch_token(code=code)
        except Exception as exc:  # noqa: BLE001 - the message is what he needs
            print(
                f"\nFAIL: Google would not exchange that code: {exc}\n"
                "A code is single-use and expires in about a minute. Run this "
                "again and paste the fresh one."
            )
            return 1
        creds = flow.credentials
    else:
        # Headless-friendly: don't try to launch a browser here. Print the auth
        # URL and wait for Google to redirect to http://localhost:8765/. From a
        # Mac SSH session started with:
        #     ssh -L 8765:localhost:8765 ignas@178.104.122.152
        # opening that URL in the Mac browser sends the callback through the
        # tunnel to this script.
        print(
            "\nOpen the URL below in a browser (from your Mac in an SSH tunnel).\n"
            "Sign in as the account you want the assistant to use, click 'Allow'.\n"
            "No tunnel? Use --manual instead.\n"
        )
        creds = flow.run_local_server(port=8765, open_browser=False, **auth_kwargs)

    # A flow that returns no refresh token has produced an hour of access and
    # then silence. Caught here rather than discovered on the next refresh.
    if not getattr(creds, "refresh_token", None):
        print(
            "\nFAIL: Google returned no refresh token, so this would stop "
            "working within the hour. Nothing was saved. Revoke IBLU's access "
            "at https://myaccount.google.com/permissions and run this again."
        )
        return 1

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
