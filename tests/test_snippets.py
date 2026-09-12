"""Snippet stripping — quoted text and signatures must never be stored (D8)."""

from __future__ import annotations

from iblu_keeper.collectors import snippets


def test_plain_text_passes_through():
    assert snippets.snippet("Yes, Tuesday works.") == "Yes, Tuesday works."


def test_quoted_reply_is_cut():
    body = (
        "Yes, Tuesday works.\n"
        "\n"
        "On Mon, 15 Jun 2026 at 10:05, Tamara <t@blanklabel.team> wrote:\n"
        "> Can we move the call?\n"
        "> Thanks\n"
    )
    assert snippets.snippet(body) == "Yes, Tuesday works."


def test_leading_quote_marker_cuts_immediately():
    assert snippets.snippet("> everything here is quoted") == ""


def test_signature_is_dropped():
    body = "Sent the invoice.\n\n-- \nIgnas\nBlank Label Team\n+385 91 000 0000"
    assert snippets.snippet(body) == "Sent the invoice."


def test_outlook_style_header_is_cut():
    body = "Approved.\n\n-----Original Message-----\nFrom: someone\nblah"
    assert snippets.snippet(body) == "Approved."


def test_forwarded_from_header_is_cut():
    body = "See below.\nFrom: Ana <ana@deadlift.io>\nSubject: old thread"
    assert snippets.snippet(body) == "See below."


def test_mobile_footer_is_cut():
    assert snippets.snippet("On my way.\n\nSent from my iPhone") == "On my way."


def test_truncates_at_a_word_boundary_with_ellipsis():
    out = snippets.snippet("word " * 200)
    assert len(out) <= snippets.SNIPPET_MAX + 1
    assert out.endswith("…")
    assert not out.endswith("wor…")


def test_whitespace_is_collapsed():
    assert snippets.snippet("a\n\n\n   b\t\tc") == "a b c"


def test_unquoted_length_measures_my_text_only():
    body = "Three words here\n\n> a very long quoted passage " + "x " * 500
    assert snippets.unquoted_length(body) == len("Three words here")


def test_empty_and_none_are_safe():
    assert snippets.snippet("") == ""
    assert snippets.snippet(None) == ""
    assert snippets.unquoted_length(None) == 0
