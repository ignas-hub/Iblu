"""`tools.gdoc_edit` — everyday Docs edits by verb.

Fakes only: a hand-built Docs API document and a fake `documents()` service
that records every batchUpdate body. `settings` is swapped whole (frozen
dataclass, DRY_RUN pinned by conftest). No network.
"""

from __future__ import annotations

import pytest

from iblu_keeper.tools import gdoc_edit as GE


class _Live:
    use_mock = False


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeDocs:
    """Scripted `documents().get()` responses (popped in order) + recorded batches."""

    def __init__(self, docs):
        self._docs = list(docs)
        self.batches: list[list[dict]] = []
        self.gets = 0

    def documents(self):
        return self

    def get(self, documentId):  # noqa: N803
        self.gets += 1
        assert self._docs, "documents().get() called more times than scripted"
        return _Call(self._docs.pop(0))

    def batchUpdate(self, documentId, body):  # noqa: N802, N803
        self.batches.append(body["requests"])
        return _Call({"replies": [{} for _ in body["requests"]]})


def _para(start, text, style=None):
    el = {
        "startIndex": start, "endIndex": start + len(text),
        "paragraph": {"elements": [
            {"startIndex": start, "endIndex": start + len(text), "textRun": {"content": text}},
        ]},
    }
    if style:
        el["paragraph"]["paragraphStyle"] = {"namedStyleType": style}
    return el


# Indices: "Title\n" 1..7, "Hello world\n" 7..19, "foo and foo\n" 19..31,
# "Last line\n" 31..41. body_end = 41; the final newline is at 40.
DOC = {"body": {"content": [
    {"startIndex": 0, "endIndex": 1, "sectionBreak": {}},
    _para(1, "Title\n", "HEADING_1"),
    _para(7, "Hello world\n"),
    _para(19, "foo and foo\n"),
    _para(31, "Last line\n"),
]}}
END = 41

EMPTY_DOC = {"body": {"content": [
    {"startIndex": 0, "endIndex": 1, "sectionBreak": {}},
    _para(1, "\n"),
]}}


@pytest.fixture
def live(monkeypatch):
    def _install(*docs):
        fake = _FakeDocs(docs or (DOC,))
        monkeypatch.setattr(GE, "settings", _Live())
        monkeypatch.setattr(GE, "_docs", lambda account=None: fake)
        return fake
    return _install


# --- dispatch and mock ----------------------------------------------------------


def test_unknown_action_names_the_options():
    """A typo must not fall through to a default edit."""
    with pytest.raises(GE.GdocEditError, match="unknown action.*heading"):
        GE.gdoc_edit("header", "D1", text="x")


def test_mock_mode_edits_nothing(monkeypatch):
    """The suite runs in DRY_RUN; a mock that fetched or wrote would be a live call."""
    def fail(account=None):
        raise AssertionError("_docs() must not be called")
    monkeypatch.setattr(GE, "_docs", fail)
    out = GE.gdoc_edit("heading", "D1", text="Summary")
    assert out["_mock"] is True and out["status"] == "not_inserted_mock"
    assert GE.gdoc_edit("delete_text", "D1", text="x")["status"] == "not_deleted_mock"


# --- heading ----------------------------------------------------------------------


def test_heading_at_end_inserts_before_the_final_newline_and_styles_the_new_paragraph(live):
    """Inserting AT the end index is rejected by Google; the style range must point at the new paragraph, not the old last one."""
    fake = live()
    out = GE.gdoc_edit("heading", "D1", text="Summary", level=2)
    insert, style = fake.batches[0]
    assert insert == {"insertText": {"location": {"index": END - 1}, "text": "\nSummary"}}
    assert style["updateParagraphStyle"]["range"] == {"startIndex": END, "endIndex": END + 7}
    assert style["updateParagraphStyle"]["paragraphStyle"] == {"namedStyleType": "HEADING_2"}
    assert style["updateParagraphStyle"]["fields"] == "namedStyleType"
    assert out["status"] == "inserted" and out["applied"] == 2 and out["placement"] == "end"


def test_heading_after_an_anchor_lands_at_its_paragraphs_end(live):
    """After the match's end would split 'Hello world' in two; after the paragraph is what 'after' means."""
    fake = live()
    GE.gdoc_edit("heading", "D1", text="Summary", level=1, anchor_text="Hello")
    insert, style = fake.batches[0]
    assert insert == {"insertText": {"location": {"index": 19}, "text": "Summary\n"}}
    assert style["updateParagraphStyle"]["range"] == {"startIndex": 19, "endIndex": 26}


def test_heading_after_the_last_paragraph_falls_back_to_the_end_rule(live):
    """The last paragraph's end index IS the body end, which Google rejects as a location (found live by gdoc_insert_table)."""
    fake = live()
    GE.gdoc_edit("heading", "D1", text="Summary", anchor_text="Last line")
    insert = fake.batches[0][0]
    assert insert["insertText"]["location"]["index"] == END - 1
    assert insert["insertText"]["text"] == "\nSummary"


def test_heading_in_an_empty_doc_has_no_leading_newline(live):
    """A leading newline in an empty doc leaves a blank first paragraph."""
    fake = live(EMPTY_DOC)
    GE.gdoc_edit("heading", "D1", text="Summary")
    assert fake.batches[0][0]["insertText"] == {"location": {"index": 1}, "text": "Summary"}


def test_heading_level_outside_the_docs_range_is_refused(monkeypatch):
    """HEADING_7 is not a Docs style; the refusal must not depend on mode."""
    with pytest.raises(GE.GdocEditError, match="level"):
        GE.gdoc_edit("heading", "D1", text="x", level=7)
    with pytest.raises(GE.GdocEditError, match="level"):
        GE.gdoc_edit("heading", "D1", text="x", level=0)


# --- bullets ----------------------------------------------------------------------


def test_bullets_insert_one_paragraph_per_item_and_create_the_list(live):
    """createParagraphBullets on the wrong range bullets the neighbours; the reset to NORMAL_TEXT stops a list of headings."""
    fake = live()
    out = GE.gdoc_edit("bullets", "D1", items=["one", "two", " "])
    insert, reset, bullets = fake.batches[0]
    assert insert["insertText"] == {"location": {"index": END - 1}, "text": "\none\ntwo"}
    rng = {"startIndex": END, "endIndex": END + 7}
    assert reset["updateParagraphStyle"]["range"] == rng
    assert reset["updateParagraphStyle"]["paragraphStyle"] == {"namedStyleType": "NORMAL_TEXT"}
    assert bullets["createParagraphBullets"] == {"range": rng, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}
    assert out["applied"] == 3


def test_numbered_bullets_use_the_numbered_preset_and_empty_items_are_refused(live):
    fake = live()
    GE.gdoc_edit("bullets", "D1", items=["a"], numbered=True, anchor_text="Hello")
    insert, _reset, bullets = fake.batches[0]
    assert insert["insertText"] == {"location": {"index": 19}, "text": "a\n"}
    assert bullets["createParagraphBullets"]["bulletPreset"] == "NUMBERED_DECIMAL_ALPHA_ROMAN"
    with pytest.raises(GE.GdocEditError, match="items"):
        GE.gdoc_edit("bullets", "D1", items=["", "  "])


# --- link / style ----------------------------------------------------------------


def test_link_styles_exactly_the_anchor_range(live):
    """A range one off links the wrong letters, silently."""
    fake = live()
    out = GE.gdoc_edit("link", "D1", anchor_text="world", url="https://example.com")
    assert fake.batches[0] == [{"updateTextStyle": {
        "range": {"startIndex": 13, "endIndex": 18},
        "textStyle": {"link": {"url": "https://example.com"}},
        "fields": "link",
    }}]
    assert out["status"] == "linked" and out["occurrences"] == 1


def test_style_turns_bold_on_or_off_and_refuses_unknown_styles(live):
    """`fields` must name the style or Google ignores the change; off must send False, not omit it."""
    fake = live(DOC, DOC)
    GE.gdoc_edit("style", "D1", anchor_text="Hello", style="bold")
    GE.gdoc_edit("style", "D1", anchor_text="Hello", style="italic, underline", on=False)
    on, off = fake.batches[0][0], fake.batches[1][0]
    assert on["updateTextStyle"]["textStyle"] == {"bold": True} and on["updateTextStyle"]["fields"] == "bold"
    assert off["updateTextStyle"]["textStyle"] == {"italic": False, "underline": False}
    assert off["updateTextStyle"]["fields"] == "italic,underline"
    with pytest.raises(GE.GdocEditError, match="style must be"):
        GE.gdoc_edit("style", "D1", anchor_text="Hello", style="blink")


# --- anchors ------------------------------------------------------------------------


def test_an_ambiguous_anchor_uses_the_first_and_reports_the_count(live):
    """Silently picking one of two matches is how the wrong paragraph gets edited."""
    fake = live()
    out = GE.gdoc_edit("style", "D1", anchor_text="foo", style="bold")
    assert fake.batches[0][0]["updateTextStyle"]["range"] == {"startIndex": 19, "endIndex": 22}
    assert out["occurrences"] == 2 and "first occurrence" in out["note"]


def test_a_missing_anchor_raises_naming_the_text_and_writes_nothing(live):
    fake = live()
    with pytest.raises(GE.GdocEditError, match="not-here"):
        GE.gdoc_edit("insert_after", "D1", anchor_text="not-here", text="x")
    assert fake.batches == []


def test_indices_come_from_a_fresh_fetch_every_call(live):
    """A document edited in the Docs UI between two calls must still be edited in the right place."""
    shifted = {"body": {"content": [
        {"startIndex": 0, "endIndex": 1, "sectionBreak": {}},
        _para(1, "NEW LINE ABOVE\n"),
        _para(16, "Hello world\n"),
    ]}}
    fake = live(DOC, shifted)
    GE.gdoc_edit("insert_after", "D1", anchor_text="Hello", text="!")
    GE.gdoc_edit("insert_after", "D1", anchor_text="Hello", text="!")
    assert fake.gets == 2
    assert fake.batches[0][0]["insertText"]["location"]["index"] == 12
    assert fake.batches[1][0]["insertText"]["location"]["index"] == 21


# --- page_break / image / insert_after ---------------------------------------------


def test_page_break_at_end_or_after_the_anchors_paragraph(live):
    fake = live(DOC, DOC)
    GE.gdoc_edit("page_break", "D1")
    GE.gdoc_edit("page_break", "D1", anchor_text="Hello")
    assert fake.batches[0] == [{"insertPageBreak": {"endOfSegmentLocation": {}}}]
    assert fake.batches[1] == [{"insertPageBreak": {"location": {"index": 19}}}]


def test_image_is_inline_after_the_anchor_with_an_optional_width(live):
    """objectSize needs a unit; a bare number is a 400. Only width is sent so the aspect ratio survives."""
    fake = live(DOC, DOC)
    GE.gdoc_edit("image", "D1", image_url="https://img.example/a.png", width_pt=200, anchor_text="world")
    GE.gdoc_edit("image", "D1", image_url="https://img.example/a.png")
    with_anchor, at_end = fake.batches[0][0]["insertInlineImage"], fake.batches[1][0]["insertInlineImage"]
    assert with_anchor == {
        "uri": "https://img.example/a.png", "location": {"index": 18},
        "objectSize": {"width": {"magnitude": 200.0, "unit": "PT"}},
    }
    assert at_end == {"uri": "https://img.example/a.png", "endOfSegmentLocation": {}}
    with pytest.raises(GE.GdocEditError, match="image_url"):
        GE.gdoc_edit("image", "D1", image_url="ftp://nope")


def test_insert_after_is_inline_at_the_anchors_end(live):
    fake = live()
    GE.gdoc_edit("insert_after", "D1", anchor_text="Hello", text=" there")
    assert fake.batches[0] == [{"insertText": {"location": {"index": 12}, "text": " there"}}]


# --- delete_text ---------------------------------------------------------------------


def test_delete_text_removes_the_first_occurrence_only(live):
    """deleteContentRange of every match is `replaceAllText` with an empty string — a different, wider tool."""
    fake = live()
    out = GE.gdoc_edit("delete_text", "D1", text="foo")
    assert fake.batches[0] == [{"deleteContentRange": {"range": {"startIndex": 19, "endIndex": 22}}}]
    assert out["occurrences"] == 2 and out["status"] == "deleted"


def test_delete_text_refuses_more_than_2000_chars_before_fetching(monkeypatch):
    """A tool that can wipe a document in one call is a tool that one day will."""
    def fail(account=None):
        raise AssertionError("_docs() must not be called")
    monkeypatch.setattr(GE, "settings", _Live())
    monkeypatch.setattr(GE, "_docs", fail)
    with pytest.raises(GE.GdocEditError, match="2000"):
        GE.gdoc_edit("delete_text", "D1", text="x" * 2001)


def test_delete_text_never_removes_the_documents_final_newline(live):
    """Google refuses a range that includes the body's last newline; trimming it is what the caller meant."""
    fake = live()
    GE.gdoc_edit("delete_text", "D1", text="Last line\n")
    assert fake.batches[0][0]["deleteContentRange"]["range"] == {"startIndex": 31, "endIndex": 40}


# --- read-only views -------------------------------------------------------------------


def test_find_lists_every_occurrence_with_indices_and_context_without_writing(live):
    fake = live()
    out = GE.gdoc_edit("find", "D1", text="foo")
    assert fake.batches == []
    assert out["occurrences"] == 2
    assert [(i["start_index"], i["end_index"]) for i in out["items"]] == [(19, 22), (27, 30)]
    assert "«foo»" in out["items"][0]["context"] and "Hello world" in out["items"][0]["context"]


def test_outline_lists_headings_with_levels_without_writing(live):
    fake = live()
    out = GE.gdoc_edit("outline", "D1")
    assert fake.batches == []
    assert out["headings"] == [
        {"level": 1, "style": "HEADING_1", "text": "Title", "start_index": 1, "end_index": 7},
    ]
    assert out["count"] == 1 and out["status"] == "read"
