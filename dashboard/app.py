"""iblu-keeper command center (Phase 1).

Streamlit dashboard:
  - Google OAuth login, restricted to a single allowed account.
  - MCP server health/status (is it up, mode, last check).
  - Recent Chat messages and email summaries.
  - A test form: send a test Chat message / create a test calendar event.

Phase 1 reads data by importing the tool functions directly (same codebase).
The health panel pings the running MCP server over HTTP so you can see whether
the actual server process is up. Later this grows into the approval UI
(proposed replies, suggested actions) — the draft store already feeds it.

Run:  streamlit run dashboard/app.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

# Make the src/ package importable when run via `streamlit run dashboard/app.py`.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Google bundles previously-granted scopes when the same user signs in for a
# narrower scope set, which makes requests-oauthlib's strict scope-equality
# check raise. We only need openid+email here; the extra scopes are harmless,
# so relax the check. Must be set BEFORE requests_oauthlib is imported.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import requests  # noqa: E402
import streamlit as st  # noqa: E402

from iblu_keeper.config import settings  # noqa: E402
from iblu_keeper.tools import chat as chat_tools  # noqa: E402
from iblu_keeper.tools import calendar as calendar_tools  # noqa: E402
from iblu_keeper.tools import gmail as gmail_tools  # noqa: E402
from iblu_keeper.store import drafts as draft_store  # noqa: E402

st.set_page_config(page_title="iblu-keeper", page_icon="🔵", layout="wide")


# --------------------------------------------------------------------------- #
# Google OAuth login (single allowed account)
# --------------------------------------------------------------------------- #
def _oauth_flow():
    from google_auth_oauthlib.flow import Flow

    client_config = {
        "web": {
            "client_id": settings.dashboard_oauth_client_id,
            "client_secret": settings.dashboard_oauth_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.dashboard_oauth_redirect_uri],
        }
    }
    flow = Flow.from_client_config(
        client_config,
        scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
        redirect_uri=settings.dashboard_oauth_redirect_uri,
    )
    # google-auth-oauthlib 1.4+ enables PKCE by default, but we build a fresh
    # Flow on each request — the verifier from the /authorize Flow is lost by
    # the time the /token Flow tries to exchange. We have a confidential client
    # (client_secret), so PKCE is optional; disable it to avoid the mismatch.
    flow.autogenerate_code_verifier = False
    flow.code_verifier = None
    return flow


def _userinfo(credentials) -> dict:
    resp = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {credentials.token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def require_login() -> str:
    """Gate the app behind Google OAuth. Returns the logged-in email."""
    if st.session_state.get("user_email"):
        return st.session_state["user_email"]

    # OAuth not configured -> dev bypass so the dashboard is usable locally.
    if not settings.dashboard_oauth_client_id or not settings.dashboard_oauth_client_secret:
        st.warning(
            "Google OAuth is not configured (DASHBOARD_OAUTH_CLIENT_ID / "
            "DASHBOARD_OAUTH_CLIENT_SECRET). Running in **dev login** mode — "
            "set these for the locked-down login."
        )
        if st.button("Continue (dev login)"):
            st.session_state["user_email"] = settings.dashboard_allowed_email
            st.rerun()
        st.stop()

    flow = _oauth_flow()
    params = st.query_params
    code = params.get("code")

    if not code:
        auth_url, _ = flow.authorization_url(
            prompt="consent", access_type="offline", include_granted_scopes="true"
        )
        st.title("🔵 iblu-keeper")
        st.subheader("Sign in")
        st.link_button("Sign in with Google", auth_url, type="primary")
        st.caption(f"Only {settings.dashboard_allowed_email} may access this dashboard.")
        st.stop()

    # Exchange the code for tokens and verify the account.
    try:
        flow.fetch_token(code=code)
        info = _userinfo(flow.credentials)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Login failed: {exc}")
        st.query_params.clear()
        st.stop()

    email = (info.get("email") or "").lower()
    if email != settings.dashboard_allowed_email.lower():
        st.error(f"Access denied for {email}. This dashboard is restricted.")
        st.stop()

    st.session_state["user_email"] = email
    st.query_params.clear()
    st.rerun()


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #
def health_panel() -> None:
    st.subheader("MCP server status")
    url = settings.dashboard_mcp_base_url.rstrip("/") + "/health"
    cols = st.columns([1, 1, 2])
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        up = resp.status_code == 200
    except Exception as exc:  # noqa: BLE001
        up = False
        data = {"error": str(exc)}

    cols[0].metric("Server", "🟢 Up" if up else "🔴 Down")
    cols[1].metric("Mode", data.get("mode", "—") if up else "—")
    cols[2].metric("Last checked", datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))
    with st.expander("Raw health response"):
        st.json(data)
    if data.get("mode") == "mock" or settings.use_mock:
        st.info("System is in **mock mode** — Chat/Gmail/Calendar return fake data "
                "until Google credentials are configured and DRY_RUN=false.")


def chat_panel() -> None:
    st.subheader("Recent Chat conversations")
    query = st.text_input("Filter by person / space name", key="chat_filter")
    convs = chat_tools.list_conversations(query or None)
    if not convs:
        st.caption("No conversations.")
        return
    for c in convs:
        with st.expander(f"{c['name']}  ·  {c['type']}  —  {c.get('last_message_preview','')}"):
            st.caption("Participants: " + ", ".join(c.get("participants", [])))
            for m in chat_tools.get_messages(c["id"], limit=10):
                st.markdown(f"**{m['sender']}** · _{m['create_time']}_\n\n{m['text']}")


def email_panel() -> None:
    st.subheader("Recent email")
    query = st.text_input("Gmail search", value="newer_than:7d", key="gmail_filter")
    for m in gmail_tools.search(query, limit=10):
        st.markdown(f"**{m['subject']}** — {m['from']}")
        st.caption(f"{m['date']} · {m['snippet']}")
        st.divider()


def drafts_panel() -> None:
    st.subheader("Pending drafts (awaiting review)")
    pending = draft_store.list_drafts()
    if not pending:
        st.caption("No drafts yet. Claude's draft_* tools will queue items here.")
        return
    for d in pending:
        if d["kind"] == "email":
            st.markdown(f"✉️ **{d.get('subject','')}** → {d.get('to','')}")
            st.caption(d.get("body", ""))
        else:
            st.markdown(f"💬 → {d.get('conversation','')}")
            st.caption(d.get("text", ""))
        st.divider()


def test_panel() -> None:
    st.subheader("Test actions")
    c1, c2 = st.columns(2)

    with c1:
        st.markdown("**Send a test Chat message**")
        conv = st.text_input("Conversation id", value="spaces/MOCK_AAAA", key="t_conv")
        text = st.text_input("Message", value="Test from iblu-keeper", key="t_text")
        if st.button("Send Chat message"):
            st.json(chat_tools.send_message(conv, text))

    with c2:
        st.markdown("**Create a test calendar event**")
        title = st.text_input("Title", value="iblu-keeper test event", key="t_title")
        start = st.text_input("Start (RFC 3339)", value="2026-06-11T14:00:00+03:00", key="t_start")
        end = st.text_input("End (RFC 3339)", value="2026-06-11T14:30:00+03:00", key="t_end")
        if st.button("Create event"):
            st.json(calendar_tools.create_event(title, start, end))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    email = require_login()

    with st.sidebar:
        st.title("🔵 iblu-keeper")
        st.caption(f"Signed in as {email}")
        st.caption("Phase 1 · command center")
        if st.button("Log out"):
            st.session_state.clear()
            st.rerun()

    st.title("Command center")
    health_panel()
    st.divider()
    left, right = st.columns(2)
    with left:
        chat_panel()
    with right:
        email_panel()
    st.divider()
    drafts_panel()
    st.divider()
    test_panel()


# Streamlit executes this script with __name__ == "__main__".
if __name__ == "__main__":
    main()
