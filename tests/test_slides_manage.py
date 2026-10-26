"""`tools.slides_manage` — the write half of the Slides domain.

Fakes only: a hand-built Slides API presentation and a fake service that
records every batchUpdate body, create and pages().get. `settings` is
swapped whole (frozen dataclass, DRY_RUN pinned by conftest). No network.
"""

from __future__ import annotations

import inspect

import pytest

from iblu_keeper.tools import slides_manage as SM


class _Live:
    use_mock = False


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Pages:
    def __init__(self, parent):
        self._parent = parent

    def get(self, presentationId, pageObjectId):  # noqa: N803
        self._parent.page_gets.append(pageObjectId)
        return _Call(self._parent._pages.get(pageObjectId, {}))


class _FakeSlides:
    """Scripted decks (popped per get), scripted batchUpdate replies (popped
    per batch), scripted pages for pages().get(); records everything."""

    def __init__(self, decks, replies=None, pages=None):
        self._decks = list(decks)
        self._replies = list(replies or [])
        self._pages = pages or {}
        self.gets: list[str] = []
        self.batches: list[list[dict]] = []
        self.batch_ids: list[str] = []
        self.created: list[dict] = []
        self.page_gets: list[str] = []

    def presentations(self):
        return self

    def pages(self):
        return _Pages(self)

    def get(self, presentationId):  # noqa: N803
        self.gets.append(presentationId)
        assert self._decks, "presentations().get() called more times than scripted"
        return _Call(self._decks.pop(0))

    def batchUpdate(self, presentationId, body):  # noqa: N802, N803
        self.batches.append(body["requests"])
        self.batch_ids.append(presentationId)
        if self._replies:
            return _Call(self._replies.pop(0))
        return _Call({"replies": [{} for _ in body["requests"]]})

    def create(self, body):
        self.created.append(body)
        return _Call({"presentationId": "NEW", "title": body["title"], "slides": [{"objectId": "first"}]})


class _FakeDrive:
    def __init__(self):
        self.updates: list[dict] = []

    def files(self):
        return self

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return _Call({"id": kwargs["fileId"], "name": kwargs["body"].get("name", "")})


def _shape(oid, text=None, placeholder=None, shape_type="TEXT_BOX"):
    el = {"objectId": oid, "shape": {"shapeType": shape_type}}
    if placeholder:
        el["shape"]["placeholder"] = {"type": placeholder}
    if text is not None:
        el["shape"]["text"] = {"textElements": [
            {"paragraphMarker": {}}, {"textRun": {"content": text}},
        ]}
    return el


def _slide(oid, elements, notes_id=None, notes_text=None):
    props: dict = {"layoutObjectId": "L"}
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
    "slides": [
        _slide("s1", [
            _shape("t1", "Welcome\n", "CENTERED_TITLE"),
            _shape("b1", "Q4 kickoff\n", "SUBTITLE"),
        ], notes_id="n1", notes_text="Say hello\n"),
        _slide("s2", [
            _shape("t2", "Numbers\n", "TITLE"),
            _shape("b2", None, "BODY"),
            {"objectId": "img2", "image": {"sourceUrl": "https://img/x.png"}},
        ], notes_id="n2"),
        _slide("s3", [
            {"objectId": "g3", "elementGroup": {"children": [_shape("g3a", "Grouped\n")]}},
        ]),
    ],
}
URL = "https://docs.google.com/presentation/d/P1/edit"

# Enough to get every action past its input checks.
_VALID = {
    "create": dict(title="T"),
    "add_slide": dict(presentation_id_or_url="P1", layout="TITLE_ONLY", title="t"),
    "set_text": dict(presentation_id_or_url="P1", slide=1, placeholder="title", text="x"),
    "replace_text": dict(presentation_id_or_url="P1", find="a", replace_with="b"),
    "add_text_box": dict(presentation_id_or_url="P1", slide=1, text="x"),
    "add_image": dict(presentation_id_or_url="P1", slide=1, image_url="https://i/x.png"),
    "duplicate_slide": dict(presentation_id_or_url="P1", slide=1),
    "move_slide": dict(presentation_id_or_url="P1", slide=1, at=2),
    "delete_slide": dict(presentation_id_or_url="P1", slide=1, confirm=True),
    "speaker_notes": dict(presentation_id_or_url="P1", slide=1, text="n"),
    "rename": dict(presentation_id_or_url="P1", title="New"),
}


@pytest.fixture
def live(monkeypatch):
    def _install(*decks, replies=None, pages=None):
        fake = _FakeSlides(decks or (DECK,), replies, pages)
        drive = _FakeDrive()
        monkeypatch.setattr(SM, "settings", _Live())
        monkeypatch.setattr(SM, "_slides", lambda account=None: fake)
        monkeypatch.setattr(SM, "_drive", lambda account=None: drive)
        monkeypatch.setattr(SM, "_new_id", lambda prefix: f"{prefix}_fixed")
        fake.drive = drive
        return fake
    return _install


def _boom(monkeypatch):
    def fail(account=None):
        raise AssertionError("the service builder must not be called")
    monkeypatch.setattr(SM, "_slides", fail)
    monkeypatch.setattr(SM, "_drive", fail)


# --- dispatch and mock ----------------------------------------------------------


def test_unknown_action_names_the_options():
    """'delete' is not an action; the message must say what is, not guess."""
    with pytest.raises(SM.SlidesManageError, match="unknown action.*delete_slide"):
        SM.slides_manage("delete", "P1")


def test_every_action_has_valid_kwargs_pinned():
    """A new action without a mock-mode case here would be the one that reaches Google."""
    assert set(_VALID) == set(SM.ACTIONS)


def test_mock_mode_writes_nothing_for_every_action(monkeypatch):
    """The suite runs in DRY_RUN; a mock that reached Google would edit a real deck."""
    _boom(monkeypatch)
    for action, kwargs in _VALID.items():
        out = SM.slides_manage(action, **kwargs)
        assert out["_mock"] is True, action
        assert out["status"] == SM._MOCK_STATUS[action], action
        assert out["status"].startswith("not_") and out["status"].endswith("_mock"), action


def test_every_action_but_create_needs_a_presentation(monkeypatch):
    _boom(monkeypatch)
    with pytest.raises(SM.SlidesManageError, match="needs presentation_id_or_url"):
        SM.slides_manage("rename", title="x")
    with pytest.raises(SM.SlidesManageError, match="create needs title"):
        SM.slides_manage("create")


# --- create / rename ---------------------------------------------------------------


def test_create_returns_the_id_url_and_first_slide_object_id(live):
    """The first slide's objectId is what the next set_text call needs; without it, another fetch."""
    fake = live()
    out = SM.slides_manage("create", title="  Q4 review ")
    assert fake.created == [{"title": "Q4 review"}]
    assert out["id"] == "NEW" and out["first_slide_object_id"] == "first"
    assert out["url"] == "https://docs.google.com/presentation/d/NEW/edit"
    assert out["status"] == "created" and fake.gets == []


def test_rename_is_a_drive_update_of_the_name_only(live):
    """Slides has no rename; a body with anything else in it would clobber that field."""
    fake = live()
    out = SM.slides_manage("rename", URL, title="Q4 final")
    sent = fake.drive.updates[0]
    assert sent["fileId"] == "P1" and sent["body"] == {"name": "Q4 final"}
    assert sent["supportsAllDrives"] is True
    assert out["status"] == "renamed" and out["title"] == "Q4 final"
    assert fake.batches == [], "rename must not touch the Slides API"


# --- add_slide ---------------------------------------------------------------------


def test_add_slide_creates_at_the_end_then_fills_the_placeholders_it_finds(live):
    """Placeholder ids come from the created slide, not guessed: a mapping to a type the theme lacks is a 400."""
    fake = live(
        replies=[{"replies": [{"createSlide": {"objectId": "new1"}}]}],
        pages={"new1": {"pageElements": [
            _shape("nt", None, "TITLE"), _shape("nb", None, "BODY"),
        ]}},
    )
    out = SM.slides_manage("add_slide", "P1", layout="title_and_body", title="Agenda", body="One\nTwo")
    assert fake.batches[0] == [{"createSlide": {
        "insertionIndex": 3,
        "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"},
    }}]
    assert fake.page_gets == ["new1"]
    assert fake.batches[1] == [
        {"insertText": {"objectId": "nt", "insertionIndex": 0, "text": "Agenda"}},
        {"insertText": {"objectId": "nb", "insertionIndex": 0, "text": "One\nTwo"}},
    ]
    assert out["object_id"] == "new1" and out["index"] == 4
    assert out["filled"] == {"title": "nt", "body": "nb"} and out["status"] == "added"
    assert "unfilled" not in out


def test_add_slide_at_a_position_and_reports_what_the_layout_could_not_hold(live):
    """`at` is 1-based for Ignas and 0-based for Google; body text with nowhere to go must be said, not dropped."""
    fake = live(
        replies=[{"replies": [{"createSlide": {"objectId": "new2"}}]}],
        pages={"new2": {"pageElements": [_shape("nt", None, "TITLE")]}},
    )
    out = SM.slides_manage("add_slide", "P1", layout="TITLE_ONLY", at=2, title="Mid", body="lost?")
    assert fake.batches[0][0]["createSlide"]["insertionIndex"] == 1
    assert fake.batches[1] == [{"insertText": {"objectId": "nt", "insertionIndex": 0, "text": "Mid"}}]
    assert out["index"] == 2 and out["filled"] == {"title": "nt"}
    assert out["unfilled"] == ["body"] and "add_text_box" in out["note"]


def test_add_slide_refuses_an_unknown_layout_and_text_on_blank_before_any_call(monkeypatch):
    """Google's error for a bad layout names its enum, not the word used; BLANK has nowhere for text."""
    _boom(monkeypatch)
    with pytest.raises(SM.SlidesManageError, match="unknown layout 'TWO_COLUMNS'"):
        SM.slides_manage("add_slide", "P1", layout="two_columns")
    with pytest.raises(SM.SlidesManageError, match="BLANK"):
        SM.slides_manage("add_slide", "P1", layout="BLANK", title="x")


def test_add_slide_with_no_text_never_fetches_the_new_page(live):
    fake = live(replies=[{"replies": [{"createSlide": {"objectId": "new3"}}]}])
    out = SM.slides_manage("add_slide", "P1", layout="BLANK")
    assert fake.page_gets == [] and len(fake.batches) == 1
    assert out["filled"] == {} and out["applied"] == 1


# --- set_text -----------------------------------------------------------------------


def test_set_text_resolves_the_title_placeholder_and_replaces_the_text_whole(live):
    """CENTERED_TITLE is a title too; insertText alone would prepend, not replace."""
    fake = live()
    out = SM.slides_manage("set_text", URL, slide=1, placeholder="Title", text="Hello")
    assert fake.batches[0] == [
        {"deleteText": {"objectId": "t1", "textRange": {"type": "ALL"}}},
        {"insertText": {"objectId": "t1", "insertionIndex": 0, "text": "Hello"}},
    ]
    assert out["object_id"] == "t1" and out["slide_object_id"] == "s1"
    assert out["applied"] == 2 and out["status"] == "set"


def test_set_text_on_an_empty_shape_skips_the_delete_and_clearing_skips_the_insert(live):
    """deleteText ALL on a shape with no text is rejected by Google; inserting '' is a 400 too."""
    fake = live(DECK, DECK)
    SM.slides_manage("set_text", "P1", slide=2, placeholder="body", text="Filled")
    assert fake.batches[0] == [{"insertText": {"objectId": "b2", "insertionIndex": 0, "text": "Filled"}}]
    out = SM.slides_manage("set_text", "P1", slide=1, object_id="b1", text="")
    assert fake.batches[1] == [{"deleteText": {"objectId": "b1", "textRange": {"type": "ALL"}}}]
    assert out["object_id"] == "b1" and out["applied"] == 1


def test_set_text_refuses_a_slide_with_no_such_placeholder_naming_what_it_has(live):
    """Silently picking some other shape is how the wrong text gets overwritten."""
    fake = live()
    with pytest.raises(SM.SlidesManageError, match=r"slide 3 has no body placeholder.*g3a"):
        SM.slides_manage("set_text", "P1", slide=3, placeholder="body", text="x")
    assert fake.batches == []


def test_set_text_refuses_an_object_id_off_the_slide_or_not_a_shape(live):
    """An objectId from another slide is valid to Google — the edit would land there."""
    fake = live(DECK, DECK)
    with pytest.raises(SM.SlidesManageError, match="no element 't1' on slide 2"):
        SM.slides_manage("set_text", "P1", slide=2, object_id="t1", text="x")
    with pytest.raises(SM.SlidesManageError, match="not a text shape"):
        SM.slides_manage("set_text", "P1", slide=2, object_id="img2", text="x")
    assert fake.batches == []


def test_set_text_needs_exactly_one_target_and_a_text_before_any_call(monkeypatch):
    _boom(monkeypatch)
    with pytest.raises(SM.SlidesManageError, match="exactly one of"):
        SM.slides_manage("set_text", "P1", slide=1, text="x")
    with pytest.raises(SM.SlidesManageError, match="exactly one of"):
        SM.slides_manage("set_text", "P1", slide=1, object_id="t1", placeholder="title", text="x")
    with pytest.raises(SM.SlidesManageError, match="placeholder must be one of"):
        SM.slides_manage("set_text", "P1", slide=1, placeholder="footer", text="x")
    with pytest.raises(SM.SlidesManageError, match="needs text"):
        SM.slides_manage("set_text", "P1", slide=1, placeholder="title")


# --- replace_text -------------------------------------------------------------------


def test_replace_text_sends_replace_all_text_and_sums_the_occurrences(live):
    """matchCase must travel with the request; the count is the only evidence anything changed."""
    fake = live(replies=[{"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]}])
    out = SM.slides_manage("replace_text", "P1", find="Q3", replace_with="Q4", match_case=True)
    assert fake.batches[0] == [{"replaceAllText": {
        "containsText": {"text": "Q3", "matchCase": True}, "replaceText": "Q4",
    }}]
    assert out["occurrences"] == 3 and out["status"] == "replaced"
    assert fake.gets == [], "a deck-wide replace needs no fetch"
    with pytest.raises(SM.SlidesManageError, match="replace_with"):
        SM.slides_manage("replace_text", "P1", find="x")


# --- add_text_box / add_image ---------------------------------------------------------


def test_add_text_box_defaults_to_a_centred_box_and_fills_it(live):
    """A box with no size or position is a 400; one off the page is invisible."""
    fake = live()
    out = SM.slides_manage("add_text_box", "P1", slide=2, text="Note")
    create, insert = fake.batches[0]
    assert create == {"createShape": {
        "objectId": "textbox_fixed", "shapeType": "TEXT_BOX",
        "elementProperties": {
            "pageObjectId": "s2",
            "size": {"width": {"magnitude": 432.0, "unit": "PT"}, "height": {"magnitude": 81.0, "unit": "PT"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": 144.0, "translateY": 162.0, "unit": "PT"},
        },
    }}
    assert insert == {"insertText": {"objectId": "textbox_fixed", "insertionIndex": 0, "text": "Note"}}
    assert out["object_id"] == "textbox_fixed" and out["box_pt"] == {"x": 144.0, "y": 162.0, "width": 432.0, "height": 81.0}


def test_add_text_box_honours_explicit_points(live):
    fake = live()
    SM.slides_manage("add_text_box", "P1", slide=1, text="x", x=10, y=20, width=300, height=50)
    props = fake.batches[0][0]["createShape"]["elementProperties"]
    assert props["transform"]["translateX"] == 10.0 and props["transform"]["translateY"] == 20.0
    assert props["size"]["width"]["magnitude"] == 300.0 and props["size"]["height"]["magnitude"] == 50.0


def test_add_image_fits_a_square_box_of_the_width_centred_and_refuses_non_http(live, monkeypatch):
    """The API scales the image into the box keeping its aspect; a square box is the only shape that never crops a width."""
    fake = live()
    out = SM.slides_manage("add_image", "P1", slide=1, image_url="https://img/x.png", width=200)
    assert fake.batches[0] == [{"createImage": {
        "objectId": "image_fixed", "url": "https://img/x.png",
        "elementProperties": {
            "pageObjectId": "s1",
            "size": {"width": {"magnitude": 200.0, "unit": "PT"}, "height": {"magnitude": 200.0, "unit": "PT"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": 260.0, "translateY": 102.5, "unit": "PT"},
        },
    }}]
    assert out["object_id"] == "image_fixed" and out["status"] == "added"
    _boom(monkeypatch)
    with pytest.raises(SM.SlidesManageError, match="image_url"):
        SM.slides_manage("add_image", "P1", slide=1, image_url="ftp://nope")
    with pytest.raises(SM.SlidesManageError, match="width must be"):
        SM.slides_manage("add_image", "P1", slide=1, image_url="https://i/x.png", width=0)


# --- duplicate / move / delete ---------------------------------------------------------


def test_duplicate_slide_copies_by_object_id_and_reports_where_the_copy_landed(live):
    fake = live(replies=[{"replies": [{"duplicateObject": {"objectId": "s2copy"}}]}])
    out = SM.slides_manage("duplicate_slide", "P1", slide=2)
    assert fake.batches[0] == [{"duplicateObject": {"objectId": "s2"}}]
    assert out["object_id"] == "s2copy" and out["source_object_id"] == "s2"
    assert out["index"] == 3 and out["slide_count"] == 4 and out["status"] == "duplicated"


def test_move_slide_counts_the_insertion_index_on_the_arrangement_before_the_move(live):
    """insertionIndex is pre-move: moving slide 1 to position 3 needs index 3, not 2, or it lands at 2."""
    fake = live(DECK, DECK, DECK)
    SM.slides_manage("move_slide", "P1", slide=1, at=3)
    SM.slides_manage("move_slide", "P1", slide=3, at=1)
    assert fake.batches[0] == [{"updateSlidesPosition": {"slideObjectIds": ["s1"], "insertionIndex": 3}}]
    assert fake.batches[1] == [{"updateSlidesPosition": {"slideObjectIds": ["s3"], "insertionIndex": 0}}]
    out = SM.slides_manage("move_slide", "P1", slide=2, at=2)
    assert len(fake.batches) == 2 and out["applied"] == 0 and "already" in out["note"]
    with pytest.raises(SM.SlidesManageError, match="between 1 and 3"):
        live()
        SM.slides_manage("move_slide", "P1", slide=1, at=4)


def test_delete_slide_needs_confirm_before_any_call(monkeypatch):
    """A delete that happens on the first try is a delete that happens by accident."""
    _boom(monkeypatch)
    with pytest.raises(SM.SlidesManageError, match="confirm=True"):
        SM.slides_manage("delete_slide", "P1", slide=2)
    monkeypatch.setattr(SM, "settings", _Live())
    with pytest.raises(SM.SlidesManageError, match="confirm=True"):
        SM.slides_manage("delete_slide", "P1", slide=2, confirm="yes")


def test_delete_slide_deletes_the_object_and_refuses_the_only_slide(live):
    """Google rejects deleting the last slide with an unhelpful 400; say why first."""
    fake = live()
    out = SM.slides_manage("delete_slide", "P1", slide=2, confirm=True)
    assert fake.batches[0] == [{"deleteObject": {"objectId": "s2"}}]
    assert out["object_id"] == "s2" and out["slide_count"] == 2 and out["status"] == "deleted"
    assert "Version history" in out["undo"]
    fake = live({**DECK, "slides": DECK["slides"][:1]})
    with pytest.raises(SM.SlidesManageError, match="only slide"):
        SM.slides_manage("delete_slide", "P1", slide=1, confirm=True)
    assert fake.batches == []


def test_no_presentation_delete_exists_and_the_docstring_says_so():
    """files.delete skips the Trash; the deck itself is only ever removed via drive_manage trash."""
    source = inspect.getsource(SM)
    assert [a for a in SM.ACTIONS if "delete" in a] == ["delete_slide"]
    assert "files().delete" not in source
    assert "presentations().delete" not in source
    assert "Never a presentation delete" in SM.__doc__


# --- speaker_notes ---------------------------------------------------------------------


def test_speaker_notes_replace_existing_notes_and_create_missing_ones(live):
    """The notes shape may not exist yet; deleteText on it fails, insertText creates it."""
    fake = live(DECK, DECK)
    out = SM.slides_manage("speaker_notes", "P1", slide=1, text="Pause here")
    assert fake.batches[0] == [
        {"deleteText": {"objectId": "n1", "textRange": {"type": "ALL"}}},
        {"insertText": {"objectId": "n1", "insertionIndex": 0, "text": "Pause here"}},
    ]
    assert out["object_id"] == "n1" and out["status"] == "set"
    out = SM.slides_manage("speaker_notes", "P1", slide=2, text="New notes")
    assert fake.batches[1] == [{"insertText": {"objectId": "n2", "insertionIndex": 0, "text": "New notes"}}]
    assert out["object_id"] == "n2"


# --- indices --------------------------------------------------------------------------


def test_indices_come_from_a_fresh_fetch_every_call(live):
    """A deck reordered in the Slides UI between two calls must still be edited in the right place."""
    reordered = {**DECK, "slides": [DECK["slides"][1], DECK["slides"][0], DECK["slides"][2]]}
    fake = live(DECK, reordered)
    SM.slides_manage("set_text", "P1", slide=1, placeholder="title", text="A")
    SM.slides_manage("set_text", "P1", slide=1, placeholder="title", text="B")
    assert len(fake.gets) == 2
    assert fake.batches[0][1]["insertText"]["objectId"] == "t1"
    assert fake.batches[1][1]["insertText"]["objectId"] == "t2"


def test_slide_out_of_range_is_refused_naming_the_count_without_writing(live):
    fake = live()
    with pytest.raises(SM.SlidesManageError, match="between 1 and 3"):
        SM.slides_manage("duplicate_slide", "P1", slide=9)
    assert fake.batches == []
