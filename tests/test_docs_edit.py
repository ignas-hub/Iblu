"""Tests for `tools.docs_edit` — the raw batchUpdate passthrough + text anchors.

Fakes only: a hand-built Docs API document shape and a fake `documents()`
service. No network, no live Google calls.
"""

from __future__ import annotations

import pytest

from iblu_keeper.tools import docs_edit


# --------------------------------------------------------------------------- #
# Fake document: non-zero structural offsets + a text run split across two
# textRun elements ("World!" is split into "Wor" + "ld!\n").
# --------------------------------------------------------------------------- #
FAKE_DOC = {
    "body": {
        "content": [
            {
                "startIndex": 1, "endIndex": 8,
                "paragraph": {"elements": [
                    {"startIndex": 1, "endIndex": 8, "textRun": {"content": "Hello \n"}},
                ]},
            },
            {
                "startIndex": 8, "endIndex": 15,
                "paragraph": {"elements": [
                    {"startIndex": 8, "endIndex": 11, "textRun": {"content": "Wor"}},
                    {"startIndex": 11, "endIndex": 15, "textRun": {"content": "ld!\n"}},
                ]},
            },
        ]
    }
}

# A document with "foo" appearing three times, for occurrence tests.
# full text: "foo bar foo baz foo\n"
FOO_DOC = {
    "body": {
        "content": [
            {
                "startIndex": 1, "endIndex": 21,
                "paragraph": {"elements": [
                    {"startIndex": 1, "endIndex": 21,
                     "textRun": {"content": "foo bar foo baz foo\n"}},
                ]},
            },
        ]
    }
}


def _fetch(doc):
    return lambda: doc


# --------------------------------------------------------------------------- #
# _build_segments / offset mapping — the split-run + non-zero-offset case
# --------------------------------------------------------------------------- #
def test_build_segments_walks_split_text_runs_with_real_offsets():
    segments = docs_edit._build_segments(FAKE_DOC)
    assert segments == [
        {"start": 1, "text": "Hello \n"},
        {"start": 8, "text": "Wor"},
        {"start": 11, "text": "ld!\n"},
    ]


def test_anchor_resolves_across_a_split_text_run():
    """"World" spans the "Wor" + "ld!\n" boundary; indices must land on the
    real per-segment startIndex, not a flat 0-based string assumption."""
    req = {"updateTextStyle": {
        "range": {"text": "World", "occurrence": 1},
        "textStyle": {"bold": True},
        "fields": "bold",
    }}
    resolved = docs_edit._resolve_anchors_in_batch([req], _fetch(FAKE_DOC))
    assert len(resolved) == 1
    rng = resolved[0]["updateTextStyle"]["range"]
    assert rng == {"startIndex": 8, "endIndex": 13}


# --------------------------------------------------------------------------- #
# occurrence: 1 vs 2 vs "all"
# --------------------------------------------------------------------------- #
def test_occurrence_1_picks_the_first_match():
    req = {"insertText": {"location": {"text": "foo", "occurrence": 1}, "text": "X"}}
    resolved = docs_edit._resolve_anchors_in_batch([req], _fetch(FOO_DOC))
    assert len(resolved) == 1
    assert resolved[0]["insertText"]["location"]["index"] == 1  # first "foo" at doc index 1


def test_occurrence_2_picks_the_second_match():
    req = {"insertText": {"location": {"text": "foo", "occurrence": 2}, "text": "X"}}
    resolved = docs_edit._resolve_anchors_in_batch([req], _fetch(FOO_DOC))
    assert len(resolved) == 1
    # full text: "foo bar foo baz foo\n" -> second "foo" at full-text offset 8 -> doc index 1+8=9
    assert resolved[0]["insertText"]["location"]["index"] == 9


def test_occurrence_all_expands_into_one_request_per_match():
    req = {"insertText": {"location": {"text": "foo", "occurrence": "all"}, "text": "X"}}
    resolved = docs_edit._resolve_anchors_in_batch([req], _fetch(FOO_DOC))
    assert len(resolved) == 3


# --------------------------------------------------------------------------- #
# Descending index order for a multi-match expansion
# --------------------------------------------------------------------------- #
def test_occurrence_all_is_applied_in_descending_index_order():
    req = {"insertText": {"location": {"text": "foo", "occurrence": "all"}, "text": "X"}}
    resolved = docs_edit._resolve_anchors_in_batch([req], _fetch(FOO_DOC))
    indices = [r["insertText"]["location"]["index"] for r in resolved]
    assert indices == sorted(indices, reverse=True)
    assert indices == [17, 9, 1]


# --------------------------------------------------------------------------- #
# Missing anchor raises, naming the text
# --------------------------------------------------------------------------- #
def test_missing_anchor_raises_naming_the_text():
    req = {"insertText": {"location": {"text": "nowhere-in-doc"}, "text": "X"}}
    with pytest.raises(ValueError, match="nowhere-in-doc"):
        docs_edit._resolve_anchors_in_batch([req], _fetch(FOO_DOC))


def test_occurrence_out_of_range_raises():
    req = {"insertText": {"location": {"text": "foo", "occurrence": 99}}}
    with pytest.raises(ValueError, match="occurrence"):
        docs_edit._resolve_anchors_in_batch([req], _fetch(FOO_DOC))


# --------------------------------------------------------------------------- #
# Raw requests without anchors pass through completely unmodified
# --------------------------------------------------------------------------- #
def test_requests_without_anchors_pass_through_unmodified():
    requests = [
        {"insertText": {"location": {"index": 5}, "text": "hi"}},
        {"replaceAllText": {
            "containsText": {"text": "foo", "matchCase": False},
            "replaceText": "bar",
        }},
        {"updateDocumentStyle": {"documentStyle": {"marginTop": {"magnitude": 20, "unit": "PT"}}, "fields": "marginTop"}},
    ]
    resolved = docs_edit._resolve_anchors_in_batch(requests, _fetch(FOO_DOC))
    assert resolved == requests


def test_location_anchor_with_position_after_uses_match_end():
    req = {"insertText": {"location": {"text": "Hello", "position": "after"}, "text": "!"}}
    resolved = docs_edit._resolve_anchors_in_batch([req], _fetch(FAKE_DOC))
    # "Hello" is doc indices 1..6 (endIndex exclusive) -> "after" = 6
    assert resolved[0]["insertText"]["location"]["index"] == 6


# --------------------------------------------------------------------------- #
# highlight() convenience helper — color resolution
# --------------------------------------------------------------------------- #
def test_resolve_color_accepts_name_and_hex():
    named = docs_edit._resolve_color("yellow")
    assert named == {"color": {"rgbColor": {"red": 1.0, "green": 1.0, "blue": 0.0}}}
    hexed = docs_edit._resolve_color("#0000FF")
    assert hexed == {"color": {"rgbColor": {"red": 0.0, "green": 0.0, "blue": 1.0}}}


def test_resolve_color_rejects_garbage():
    with pytest.raises(ValueError, match="unrecognized color"):
        docs_edit._resolve_color("not-a-color")


# --------------------------------------------------------------------------- #
# batch_update() — mock mode never touches the network
# --------------------------------------------------------------------------- #
def test_batch_update_mock_mode_is_a_noop(monkeypatch):
    class _Mock:
        use_mock = True

    monkeypatch.setattr(docs_edit, "settings", _Mock())
    result = docs_edit.batch_update("doc123", [{"insertText": {"location": {"index": 1}, "text": "x"}}])
    assert result["_mock"] is True
    assert result["applied"] == 0


def test_batch_update_requires_nonempty_requests(monkeypatch):
    class _Live:
        use_mock = False

    monkeypatch.setattr(docs_edit, "settings", _Live())
    with pytest.raises(ValueError, match="non-empty"):
        docs_edit.batch_update("doc123", [])


# --------------------------------------------------------------------------- #
# batch_update() — live path with a fake Docs service
# --------------------------------------------------------------------------- #
class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeDocsService:
    def __init__(self, doc, batch_reply):
        self._doc = doc
        self._batch_reply = batch_reply
        self.batch_calls: list[dict] = []

    def documents(self):
        return self

    def get(self, documentId):  # noqa: N803 - matches googleapiclient's kwarg name
        return _Exec(self._doc)

    def batchUpdate(self, documentId, body):  # noqa: N802, N803
        self.batch_calls.append(body)
        return _Exec(self._batch_reply)


def test_batch_update_resolves_anchor_then_sends_real_indices(monkeypatch):
    class _Live:
        use_mock = False

    monkeypatch.setattr(docs_edit, "settings", _Live())
    fake_service = _FakeDocsService(FAKE_DOC, {"replies": [{}]})
    monkeypatch.setattr(docs_edit.drive_tools, "_docs", lambda account=None: fake_service)

    result = docs_edit.batch_update("doc123", [
        {"updateTextStyle": {
            "range": {"text": "World", "occurrence": 1},
            "textStyle": {"bold": True}, "fields": "bold",
        }},
    ])

    assert result["doc_id"] == "doc123"
    assert result["applied"] == 1
    sent = fake_service.batch_calls[0]["requests"][0]
    assert sent["updateTextStyle"]["range"] == {"startIndex": 8, "endIndex": 13}


def test_highlight_builds_update_text_style_request(monkeypatch):
    class _Live:
        use_mock = False

    monkeypatch.setattr(docs_edit, "settings", _Live())
    fake_service = _FakeDocsService(FAKE_DOC, {"replies": [{}]})
    monkeypatch.setattr(docs_edit.drive_tools, "_docs", lambda account=None: fake_service)

    docs_edit.highlight("doc123", "World", color="yellow")
    sent = fake_service.batch_calls[0]["requests"][0]
    assert sent["updateTextStyle"]["textStyle"]["backgroundColor"] == {
        "color": {"rgbColor": {"red": 1.0, "green": 1.0, "blue": 0.0}}
    }


# --------------------------------------------------------------------------- #
# extract_structure() — used by gdoc_read(structure=True)
# --------------------------------------------------------------------------- #
def test_extract_structure_reports_paragraph_and_text_run_style():
    doc = {
        "body": {
            "content": [
                {
                    "startIndex": 1, "endIndex": 10,
                    "paragraph": {
                        "paragraphStyle": {"namedStyleType": "HEADING_1"},
                        "elements": [{
                            "startIndex": 1, "endIndex": 10,
                            "textRun": {
                                "content": "Title\n",
                                "textStyle": {
                                    "bold": True,
                                    "backgroundColor": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 0}}},
                                    "link": {"url": "https://example.com"},
                                },
                            },
                        }],
                    },
                },
            ]
        }
    }
    structure = docs_edit.extract_structure(doc)
    assert len(structure) == 1
    el = structure[0]
    assert el["type"] == "paragraph"
    assert el["named_style_type"] == "HEADING_1"
    run = el["text_runs"][0]
    assert run["text"] == "Title\n"
    assert run["bold"] is True
    assert run["background_color"] == "#FFFF00"
    assert run["link"] == "https://example.com"


# --- anchors stay correct across a whole batch (review, 2026-09-17) --------


def test_a_style_only_batch_keeps_its_written_order():
    from iblu_keeper.tools.docs_edit import order_for_stable_indices

    reqs = [
        {"updateTextStyle": {"range": {"startIndex": 1, "endIndex": 5}}},
        {"updateTextStyle": {"range": {"startIndex": 20, "endIndex": 25}}},
    ]
    assert order_for_stable_indices(reqs) == reqs


def test_a_batch_that_inserts_is_applied_bottom_up():
    """Anchors are resolved against the document before the batch, but Google
    applies requests in sequence — an insert above an anchored range would
    silently shift it."""
    from iblu_keeper.tools.docs_edit import order_for_stable_indices

    insert_high = {"insertText": {"location": {"index": 2}, "text": "NEW "}}
    style_low = {"updateTextStyle": {"range": {"startIndex": 30, "endIndex": 35}}}
    ordered = order_for_stable_indices([insert_high, style_low])
    assert ordered == [style_low, insert_high]


def test_requests_without_a_position_run_last_in_their_written_order():
    from iblu_keeper.tools.docs_edit import order_for_stable_indices

    replace_a = {"replaceAllText": {"containsText": {"text": "a"}, "replaceText": "b"}}
    doc_style = {"updateDocumentStyle": {"documentStyle": {}, "fields": "*"}}
    insert = {"insertText": {"location": {"index": 5}, "text": "x"}}
    assert order_for_stable_indices([replace_a, insert, doc_style]) == [insert, replace_a, doc_style]
