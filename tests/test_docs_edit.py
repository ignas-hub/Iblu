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


# --------------------------------------------------------------------------- #
# insert_table() — inserts a table AND fills it, in one call
# --------------------------------------------------------------------------- #
class _QueueDocsService:
    """Fake Docs service: scripted `documents().get()` responses (popped in
    order, one per call), and a `batchUpdate()` that records every body it
    was sent."""

    def __init__(self, get_responses: list[dict], batch_reply: dict | None = None):
        self._get_responses = list(get_responses)
        self._batch_reply = batch_reply if batch_reply is not None else {"replies": [{}]}
        self.batch_calls: list[dict] = []

    def documents(self):
        return self

    def get(self, documentId):  # noqa: N803
        assert self._get_responses, "documents().get() called more times than scripted"
        return _Exec(self._get_responses.pop(0))

    def batchUpdate(self, documentId, body):  # noqa: N802, N803
        self.batch_calls.append(body)
        return _Exec(self._batch_reply)


def _cell(start: int, text: str = "\n") -> dict:
    """A table cell whose sole paragraph starts at `start` and holds `text`."""
    end = start + len(text)
    return {
        "startIndex": start, "endIndex": end,
        "content": [{
            "startIndex": start, "endIndex": end,
            "paragraph": {"elements": [
                {"startIndex": start, "endIndex": end, "textRun": {"content": text}},
            ]},
        }],
    }


def _table(start: int, end: int, rows_of_cells: list[list[dict]]) -> dict:
    return {
        "startIndex": start, "endIndex": end,
        "table": {"tableRows": [{"tableCells": row} for row in rows_of_cells]},
    }


def _paragraph(start: int, end: int, text: str) -> dict:
    return {
        "startIndex": start, "endIndex": end,
        "paragraph": {"elements": [
            {"startIndex": start, "endIndex": end, "textRun": {"content": text}},
        ]},
    }


def _use_live(monkeypatch):
    class _Live:
        use_mock = False

    monkeypatch.setattr(docs_edit, "settings", _Live())


def _patch_docs(monkeypatch, fake_service):
    monkeypatch.setattr(docs_edit.drive_tools, "_docs", lambda account=None: fake_service)


# --- ragged rows / None / limits (validated by _normalize_table_rows) ------ #
def test_normalize_table_rows_pads_ragged_and_converts_none():
    normalized = docs_edit._normalize_table_rows([["a", 1], ["b"], [None, "d"]])
    assert normalized == [["a", "1"], ["b", ""], ["", "d"]]


def test_normalize_table_rows_rejects_empty_input():
    with pytest.raises(ValueError, match="non-empty"):
        docs_edit._normalize_table_rows([])


def test_normalize_table_rows_rejects_more_than_20_columns():
    with pytest.raises(ValueError, match="columns"):
        docs_edit._normalize_table_rows([["x"] * 21])


def test_normalize_table_rows_rejects_more_than_200_rows():
    with pytest.raises(ValueError, match="rows"):
        docs_edit._normalize_table_rows([["x"]] * 201)


# --- appending at the end of the document (no after_text) ------------------ #
_APPEND_BEFORE_DOC = {
    "body": {"content": [_paragraph(1, 7, "Hello\n")]},
}


def test_after_text_none_uses_end_of_segment_location(monkeypatch):
    _use_live(monkeypatch)
    doc_after_insert = {
        "body": {"content": [
            _paragraph(1, 7, "Hello\n"),
            _table(6, 9, [[_cell(7)]]),
        ]},
    }
    fake = _QueueDocsService([_APPEND_BEFORE_DOC, doc_after_insert])
    _patch_docs(monkeypatch, fake)

    docs_edit.insert_table("doc123", [["X"]])

    insert_req = fake.batch_calls[0]["requests"][0]["insertTable"]
    assert insert_req["endOfSegmentLocation"] == {}
    assert "location" not in insert_req


# --- after_text places the table at the containing paragraph's end -------- #
_ANCHOR_BEFORE_DOC = {
    "body": {"content": [
        _table(1, 9, [[_cell(2)]]),  # an existing table BEFORE the anchor
        _paragraph(9, 24, "Weekly summary\n"),
        # A paragraph AFTER the anchor. Without it the anchor is the last
        # paragraph, and its endIndex is the end of the document — which the
        # real API rejects as a location. The first version of this fixture
        # asserted exactly that rejected request.
        _paragraph(24, 30, "Notes\n"),
    ]},
}


def test_after_text_inserts_at_containing_paragraphs_end_index(monkeypatch):
    _use_live(monkeypatch)
    # Paragraph "Weekly summary\n" spans 9..24, so the table must be inserted
    # at index 24 — the paragraph's endIndex, not the match's own end.
    doc_after_insert = {
        "body": {"content": [
            _table(1, 9, [[_cell(2)]]),
            _paragraph(9, 24, "Weekly summary\n"),
            _table(24, 27, [[_cell(25)]]),
            _paragraph(27, 33, "Notes\n"),
        ]},
    }
    fake = _QueueDocsService([_ANCHOR_BEFORE_DOC, doc_after_insert])
    _patch_docs(monkeypatch, fake)

    result = docs_edit.insert_table("doc123", [["X"]], after_text="Weekly summary")

    insert_req = fake.batch_calls[0]["requests"][0]["insertTable"]
    assert insert_req["location"] == {"index": 24}
    assert result["table_start_index"] == 24


def test_after_text_missing_anchor_raises_naming_the_text(monkeypatch):
    _use_live(monkeypatch)
    fake = _QueueDocsService([_APPEND_BEFORE_DOC])
    _patch_docs(monkeypatch, fake)

    with pytest.raises(ValueError, match="Weekly summary"):
        docs_edit.insert_table("doc123", [["X"]], after_text="Weekly summary")


# --- the new table is found by insertion index, not position in the doc --- #
def test_new_table_is_found_by_insertion_index_with_tables_before_and_after(monkeypatch):
    _use_live(monkeypatch)
    before_doc = {
        "body": {"content": [
            _table(1, 9, [[_cell(2)]]),  # existing table, well before the anchor
            _paragraph(9, 24, "Weekly summary\n"),
        ]},
    }
    doc_after_insert = {
        "body": {"content": [
            _table(1, 9, [[_cell(2)]]),        # existing table BEFORE the new one
            _paragraph(9, 24, "Weekly summary\n"),
            _table(24, 27, [[_cell(25)]]),      # the NEW table — startIndex == min_start
            _table(100, 110, [[_cell(101)]]),   # existing table AFTER the new one
        ]},
    }
    fake = _QueueDocsService([before_doc, doc_after_insert])
    _patch_docs(monkeypatch, fake)

    result = docs_edit.insert_table("doc123", [["X"]], after_text="Weekly summary")

    # Must pick the middle table (startIndex 24), not the first (1) or last (100).
    assert result["table_start_index"] == 24


# --- cell fill: descending index order, empty cells skipped ---------------- #
def test_fill_sends_cell_text_in_descending_index_order_and_skips_empty(monkeypatch):
    _use_live(monkeypatch)
    doc_after_insert = {
        "body": {"content": [
            _paragraph(1, 7, "Hello\n"),
            _table(6, 15, [
                [_cell(7), _cell(9)],
                [_cell(11), _cell(13)],
            ]),
        ]},
    }
    fake = _QueueDocsService([_APPEND_BEFORE_DOC, doc_after_insert])
    _patch_docs(monkeypatch, fake)

    docs_edit.insert_table(
        "doc123",
        [["Venture", ""], ["blt", "240"]],
        header=False,
    )

    fill_call = fake.batch_calls[1]["requests"]
    indices = [r["insertText"]["location"]["index"] for r in fill_call]
    texts = [r["insertText"]["text"] for r in fill_call]
    # cell (0,1) held "" and must be skipped entirely.
    assert indices == sorted(indices, reverse=True)
    assert indices == [13, 11, 7]
    assert texts == ["240", "blt", "Venture"]
    # Only two batchUpdate calls: insertTable + fill (no header re-fetch/style).
    assert len(fake.batch_calls) == 2


# --- header bolding ---------------------------------------------------------- #
def _header_fixture():
    before_doc = _APPEND_BEFORE_DOC
    doc_after_insert = {
        "body": {"content": [
            _paragraph(1, 7, "Hello\n"),
            _table(6, 15, [
                [_cell(7), _cell(9)],
                [_cell(11), _cell(13)],
            ]),
        ]},
    }
    doc_after_fill = {
        "body": {"content": [
            _paragraph(1, 7, "Hello\n"),
            _table(6, 40, [
                [_cell(7, "Venture\n"), _cell(16)],  # (0,1) stayed empty — "" was skipped
                [_cell(20, "blt\n"), _cell(26, "240\n")],
            ]),
        ]},
    }
    return before_doc, doc_after_insert, doc_after_fill


def test_header_true_bolds_only_row0_nonempty_cells(monkeypatch):
    _use_live(monkeypatch)
    before_doc, doc_after_insert, doc_after_fill = _header_fixture()
    fake = _QueueDocsService([before_doc, doc_after_insert, doc_after_fill])
    _patch_docs(monkeypatch, fake)

    docs_edit.insert_table("doc123", [["Venture", ""], ["blt", "240"]], header=True)

    # insertTable, fill, and a header-style batchUpdate == 3 calls.
    assert len(fake.batch_calls) == 3
    style_reqs = fake.batch_calls[2]["requests"]
    assert len(style_reqs) == 1
    style = style_reqs[0]["updateTextStyle"]
    assert style["range"] == {"startIndex": 7, "endIndex": 14}  # "Venture" (7 chars)
    assert style["textStyle"] == {"bold": True}
    assert style["fields"] == "bold"


def test_header_false_sends_no_style_request(monkeypatch):
    _use_live(monkeypatch)
    before_doc, doc_after_insert, _unused = _header_fixture()
    # Only two get() responses scripted — a 3rd get() call would raise.
    fake = _QueueDocsService([before_doc, doc_after_insert])
    _patch_docs(monkeypatch, fake)

    docs_edit.insert_table("doc123", [["Venture", ""], ["blt", "240"]], header=False)

    assert len(fake.batch_calls) == 2


def test_single_row_table_sends_no_header_style(monkeypatch):
    _use_live(monkeypatch)
    doc_after_insert = {
        "body": {"content": [
            _paragraph(1, 7, "Hello\n"),
            _table(6, 15, [[_cell(7), _cell(9)]]),
        ]},
    }
    # Only two get() responses scripted — a 3rd get() call would raise.
    fake = _QueueDocsService([_APPEND_BEFORE_DOC, doc_after_insert])
    _patch_docs(monkeypatch, fake)

    docs_edit.insert_table("doc123", [["Only", "Row"]], header=True)

    assert len(fake.batch_calls) == 2


# --- registration --------------------------------------------------------- #
def test_gdoc_insert_table_is_registered_with_correct_annotations():
    import re
    from pathlib import Path

    server_src = (
        Path(__file__).resolve().parents[1] / "src" / "iblu_keeper" / "server.py"
    ).read_text()
    m = re.search(
        r'@mcp\.tool\(name="gdoc_insert_table",\s*annotations=\{(.*?)\}\)',
        server_src, re.S,
    )
    assert m is not None, "gdoc_insert_table is not registered as an @mcp.tool"
    block = m.group(1)
    assert '"title": "Insert Table into Google Doc"' in block
    assert '"readOnlyHint": False' in block
    assert '"destructiveHint": False' in block
    assert '"idempotentHint": False' in block
    assert '"openWorldHint": False' in block


def test_an_anchor_in_the_last_paragraph_appends_instead_of_using_an_invalid_index(monkeypatch):
    """Found on the first live test: for the last paragraph, "the end of its
    paragraph" is the end of the document, and Google rejects that location
    ("Index 105 must be less than the end index of the referenced segment").
    Appending at the end of the body is the same place, said the way the API
    accepts."""
    _use_live(monkeypatch)
    before = {"body": {"content": [_paragraph(1, 16, "Closing line!!\n")]}}
    after = {"body": {"content": [
        _paragraph(1, 16, "Closing line!!\n"),
        _table(16, 19, [[_cell(17)]]),
    ]}}
    fake = _QueueDocsService([before, after])
    _patch_docs(monkeypatch, fake)

    result = docs_edit.insert_table("doc123", [["X"]], after_text="Closing line")

    insert_req = fake.batch_calls[0]["requests"][0]["insertTable"]
    assert "location" not in insert_req
    assert insert_req["endOfSegmentLocation"] == {}
    assert result["table_start_index"] == 16
