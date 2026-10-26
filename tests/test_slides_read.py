"""`tools.slides_read` — the read half of the Slides domain.

Fakes only: a hand-built Slides API presentation and a fake
`presentations()` service that records every get. `settings` is swapped
whole (frozen dataclass, DRY_RUN pinned by conftest). No network.
"""

from __future__ import annotations

import pytest

from iblu_keeper.tools import slides_read as SR
from iblu_keeper.tools.repo import MAX_CHARS


class _Live:
    use_mock = False


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeSlides:
    """Scripted `presentations().get()` responses, popped in order."""

    def __init__(self, decks):
        self._decks = list(decks)
        self.gets: list[str] = []

    def presentations(self):
        return self

    def get(self, presentationId):  # noqa: N803
        self.gets.append(presentationId)
        assert self._decks, "presentations().get() called more times than scripted"
        return _Call(self._decks.pop(0))


class _FakeDrive:
    def __init__(self, meta):
        self._meta = meta
        self.calls: list[dict] = []

    def files(self):
        return self

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return _Call(self._meta)


def _shape(oid, text=None, placeholder=None, shape_type="TEXT_BOX"):
    el = {"objectId": oid, "shape": {"shapeType": shape_type}}
    if placeholder:
        el["shape"]["placeholder"] = {"type": placeholder}
    if text is not None:
        el["shape"]["text"] = {"textElements": [
            {"paragraphMarker": {}}, {"textRun": {"content": text}},
        ]}
    return el


def _cell(text):
    return {"text": {"textElements": [{"textRun": {"content": text}}]}}


def _slide(oid, layout, elements, notes_id=None, notes_text=None):
    props: dict = {"layoutObjectId": layout}
    if notes_id:
        page = {"notesProperties": {"speakerNotesObjectId": notes_id}, "pageElements": []}
        if notes_text is not None:
            page["pageElements"] = [_shape(notes_id, notes_text, "BODY")]
        props["notesPage"] = page
    return {"objectId": oid, "slideProperties": props, "pageElements": elements}


DECK = {
    "presentationId": "P1",
    "title": "Q4 plan",
    "pageSize": {
        "width": {"magnitude": 9144000, "unit": "EMU"},
        "height": {"magnitude": 5143500, "unit": "EMU"},
    },
    "layouts": [
        {"objectId": "L_T", "layoutProperties": {"name": "TITLE", "displayName": "Title slide"}},
        {"objectId": "L_TB", "layoutProperties": {"name": "TITLE_AND_BODY", "displayName": "Title and body"}},
    ],
    "slides": [
        _slide("s1", "L_T", [
            _shape("t1", "Welcome\n", "CENTERED_TITLE"),
            _shape("b1", "Q4 kickoff\n", "SUBTITLE"),
        ], notes_id="n1", notes_text="Say hello\n"),
        _slide("s2", "L_TB", [
            _shape("t2", "Numbers\n", "TITLE"),
            _shape("b2", "Revenue up\nCosts flat\n", "BODY"),
            {"objectId": "img2", "image": {"sourceUrl": "https://img/x.png", "contentUrl": "https://lh/x"}},
            {"objectId": "tbl2", "table": {"rows": 2, "columns": 2, "tableRows": [
                {"tableCells": [_cell("a\n"), _cell("b\n")]},
                {"tableCells": [_cell("c\n"), _cell("d\n")]},
            ]}},
        ], notes_id="n2"),
        _slide("s3", "L_NONE", [
            {"objectId": "g3", "elementGroup": {"children": [_shape("g3a", "Grouped\n")]}},
        ]),
    ],
}

URL = "https://docs.google.com/presentation/d/P1/edit#slide=id.p"


@pytest.fixture
def live(monkeypatch):
    def _install(*decks, meta=None):
        fake = _FakeSlides(decks or (DECK,))
        drive = _FakeDrive(meta or {})
        monkeypatch.setattr(SR, "settings", _Live())
        monkeypatch.setattr(SR, "_slides", lambda account=None: fake)
        monkeypatch.setattr(SR, "_drive", lambda account=None: drive)
        fake.drive = drive
        return fake
    return _install


def _boom(monkeypatch):
    def fail(account=None):
        raise AssertionError("the service builder must not be called")
    monkeypatch.setattr(SR, "_slides", fail)
    monkeypatch.setattr(SR, "_drive", fail)


# --- dispatch and mock ----------------------------------------------------------


def test_unknown_action_names_the_options():
    """A typo must not fall through to a default view."""
    with pytest.raises(SR.SlidesReadError, match="unknown action.*outline"):
        SR.slides_read("summary", "P1")


def test_mock_mode_reads_nothing(monkeypatch):
    """The suite runs in DRY_RUN; a mock that built a service would be a live call."""
    _boom(monkeypatch)
    for action in SR.ACTIONS:
        out = SR.slides_read(action, "P1", slide=1)
        assert out["_mock"] is True and out["status"] == "not_read_mock", action


def test_slide_action_needs_a_positive_slide_number_before_any_fetch(monkeypatch):
    """A missing or zero index would otherwise be a KeyError deep in the deck, after a fetch."""
    _boom(monkeypatch)
    with pytest.raises(SR.SlidesReadError, match="needs slide"):
        SR.slides_read("slide", "P1")
    with pytest.raises(SR.SlidesReadError, match="1 or more"):
        SR.slides_read("slide", "P1", slide=0)


# --- outline --------------------------------------------------------------------


def test_outline_lists_index_object_id_layout_title_body_and_notes(live):
    """These are the handles slides_manage takes; a wrong objectId edits the wrong slide."""
    live()
    out = SR.slides_read("outline", URL)
    assert out["count"] == 3 and out["status"] == "read" and out["title"] == "Q4 plan"
    assert out["slides"][0] == {
        "index": 1, "object_id": "s1", "layout": "Title slide", "title": "Welcome",
        "body": "Q4 kickoff", "has_notes": True,
    }
    s2 = out["slides"][1]
    assert s2["index"] == 2 and s2["object_id"] == "s2" and s2["layout"] == "Title and body"
    assert s2["title"] == "Numbers" and s2["has_notes"] is False
    assert s2["body"].startswith("Revenue up Costs flat")
    s3 = out["slides"][2]
    assert s3["title"] is None and s3["body"] == "Grouped", "grouped text must not be invisible"


def test_outline_body_is_cut_to_120_characters(live):
    """An outline that carried every slide's full text would be the `text` action, unpaged."""
    long = {**DECK, "slides": [_slide("s1", "L_TB", [_shape("b", "word " * 100, "BODY")])]}
    live(long)
    body = SR.slides_read("outline", "P1")["slides"][0]["body"]
    assert len(body) <= SR.OUTLINE_BODY_CHARS + 1 and body.endswith("…")


# --- slide ----------------------------------------------------------------------


def test_slide_translates_the_1_based_index_to_the_object_id(live):
    """Off by one here reads the slide before the one asked for."""
    live()
    out = SR.slides_read("slide", URL, slide=2)
    assert out["object_id"] == "s2" and out["index"] == 2 and out["slide_count"] == 3
    assert [t["object_id"] for t in out["text_elements"]] == ["t2", "b2"]
    assert out["text_elements"][0] == {
        "object_id": "t2", "placeholder": "TITLE", "shape_type": "TEXT_BOX", "text": "Numbers",
    }
    assert out["text_elements"][1]["text"] == "Revenue up\nCosts flat"
    assert out["images"] == [{"object_id": "img2", "source_url": "https://img/x.png", "content_url": "https://lh/x"}]
    assert out["tables"] == [{"object_id": "tbl2", "rows": [["a", "b"], ["c", "d"]]}]
    assert out["notes"] == "" and out["notes_object_id"] == "n2"


def test_slide_out_of_range_is_refused_naming_the_count(live):
    live()
    with pytest.raises(SR.SlidesReadError, match="between 1 and 3"):
        SR.slides_read("slide", "P1", slide=4)


def test_slide_reads_notes_and_grouped_text(live):
    """Notes live on a separate page and grouped shapes are nested; both are easy to miss."""
    fake = live(DECK, DECK)
    assert SR.slides_read("slide", "P1", slide=1)["notes"] == "Say hello"
    out = SR.slides_read("slide", "P1", slide=3)
    assert [t["object_id"] for t in out["text_elements"]] == ["g3a"]
    assert fake.gets == ["P1", "P1"]


# --- text -----------------------------------------------------------------------


def test_text_joins_every_slide_with_markers_and_includes_notes_and_tables(live):
    live()
    out = SR.slides_read("text", "P1")
    text = out["text"]
    assert "--- slide 1 ---\nWelcome\nQ4 kickoff\n[notes] Say hello" in text
    assert "--- slide 2 ---\nNumbers\nRevenue up\nCosts flat\na\tb\nc\td" in text
    assert "--- slide 3 ---\nGrouped" in text
    assert out["truncated"] is False and out["chars"] == len(text) and out["slide_count"] == 3


def test_text_is_paged_at_20000_chars_and_continues_from_offset(live):
    """Without the window a long deck is silently cut and the last slides never read."""
    big = {**DECK, "slides": [_slide("s1", "L_TB", [_shape("b", "x" * 30_000, "BODY")])]}
    live(big, big)
    first = SR.slides_read("text", "P1", max_chars=1_000_000)
    assert first["truncated"] is True and len(first["text"]) == MAX_CHARS
    assert first["next_offset"] == MAX_CHARS and "offset=20000" in first["read_the_rest"]
    second = SR.slides_read("text", "P1", offset=first["next_offset"])
    assert second["continues_from"] == MAX_CHARS and second["truncated"] is False
    assert first["text"] + second["text"] == SR.deck_text(SR._Deck(big))


# --- info -----------------------------------------------------------------------


def test_info_reports_page_size_in_points_and_drive_modified_time(live):
    """EMU is the API's unit; 9144000 EMU is 720pt, and a raw EMU number is useless to a human."""
    fake = live(meta={
        "modifiedTime": "2026-10-26T09:00:00Z",
        "lastModifyingUser": {"emailAddress": "ignas@blanklabel.team"},
        "owners": [{"emailAddress": "ignas@blanklabel.team"}],
    })
    out = SR.slides_read("info", URL)
    assert out["page_size"] == {"width_pt": 720.0, "height_pt": 405.0}
    assert out["slide_count"] == 3 and out["title"] == "Q4 plan"
    assert out["modified_time"] == "2026-10-26T09:00:00Z"
    assert out["last_modified_by"] == "ignas@blanklabel.team"
    assert out["url"] == "https://docs.google.com/presentation/d/P1/edit"
    sent = fake.drive.calls[0]
    assert sent["fileId"] == "P1" and sent["supportsAllDrives"] is True


def test_a_presentation_url_is_reduced_to_its_id(live):
    """Passing the whole URL as presentationId is a 404 that looks like an access problem."""
    fake = live()
    SR.slides_read("outline", URL)
    assert fake.gets == ["P1"]


def test_indices_come_from_a_fresh_fetch_every_call(live):
    """A deck reordered in the Slides UI between two calls must still be read in the right place."""
    reordered = {**DECK, "slides": [DECK["slides"][1], DECK["slides"][0]]}
    fake = live(DECK, reordered)
    assert SR.slides_read("slide", "P1", slide=1)["object_id"] == "s1"
    assert SR.slides_read("slide", "P1", slide=1)["object_id"] == "s2"
    assert len(fake.gets) == 2
