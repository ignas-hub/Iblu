"""Re-authorising from a phone.

The tunnelled flow needs a desktop browser and `ssh -L 8765:localhost:8765`.
`--manual` needs neither: it lets Google's redirect fail, because the
authorisation code is a string Google puts in the URL rather than something the
loopback server computes. The browser shows "cannot connect" and the code is
still in the address bar.

What is worth pinning is the parsing — the step where a phone, a cramped address
bar and a careless selection meet. Everything after it is Google's own flow.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "connect_google.py"
_spec = importlib.util.spec_from_file_location("connect_google", _PATH)
cg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cg)


@pytest.mark.parametrize("pasted,expected", [
    # What Safari actually leaves in the address bar: the slash in `4/0A...` is
    # percent-encoded, and unquoting it is not optional — Google rejects `4%2F0A`.
    ("http://localhost:8765/?code=4%2F0AVMBsJj-ABC_def&scope=openid+email",
     "4/0AVMBsJj-ABC_def"),
    # Some flows put `state` first, so the code is not simply "after the ?".
    ("http://localhost:8765/?state=xyz&code=4/0Aplain&scope=a", "4/0Aplain"),
    # Just the code, for when he selects only the interesting part.
    ("4/0AbareCode", "4/0AbareCode"),
    # iOS loves to wrap a long-pressed selection in quotes.
    ('  "4/0Aquoted"  ', "4/0Aquoted"),
])
def test_the_code_survives_however_it_was_copied(pasted, expected):
    assert cg.code_from_paste(pasted) == expected


def test_pressing_cancel_says_so_instead_of_failing_obscurely():
    with pytest.raises(ValueError, match="access_denied"):
        cg.code_from_paste("http://localhost:8765/?error=access_denied")


def test_the_consent_page_url_is_rejected_with_the_reason():
    """The likeliest mistake: copying the page he was on, not the one that failed."""
    with pytest.raises(ValueError, match="failed to load"):
        cg.code_from_paste("http://localhost:8765/")


def test_nothing_pasted_is_not_an_empty_code():
    with pytest.raises(ValueError, match="nothing pasted"):
        cg.code_from_paste("   ")


def test_both_flows_ask_for_a_refresh_token_and_the_same_account():
    """`access_type=offline` and `prompt=consent` are what make a token last.

    Shared between the tunnelled and manual paths deliberately: they differ only
    in how the code comes back, and a flag that silently dropped `offline` would
    produce an hour of working access followed by silence.
    """
    import inspect

    source = inspect.getsource(cg.main)
    assert '"access_type": "offline"' in source
    assert '"prompt": "consent"' in source
    assert "login_hint" in source
    # Both branches must use the one dict, not their own copies.
    assert source.count("auth_kwargs") >= 3


def test_a_token_with_no_refresh_token_is_refused():
    """An hour of access then silence is worse than a visible failure."""
    import inspect

    source = inspect.getsource(cg.main)
    assert 'getattr(creds, "refresh_token", None)' in source
    assert "Nothing was saved" in source


def test_the_wrong_account_is_still_refused_in_manual_mode():
    """The identity check sits after both branches, not inside one.

    Authorising as the wrong identity would silently file one person's mail
    under another, and a phone with three Workspaces signed in is exactly where
    that happens.
    """
    import inspect

    source = inspect.getsource(cg.main)
    check_at = source.index("you signed in as")
    assert source.index("args.manual") < check_at
    assert source.index("flow.run_local_server") < check_at
