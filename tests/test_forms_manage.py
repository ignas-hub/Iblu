"""`tools.forms_manage` — the write half of the Forms domain.

Fakes only: a hand-built Forms API form and a fake service that records
every `create` body and `batchUpdate` request list. `settings` is swapped
whole (frozen dataclass, DRY_RUN pinned by conftest). No network.
"""

from __future__ import annotations

import inspect
import json

import pytest

from iblu_keeper.tools import forms_manage as FM


class _Live:
    use_mock = False
    primary_alias = "blt"


class _Call:
    def __init__(self, result=None, exc=None):
        self._result, self._exc = result, exc

    def execute(self):
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeForms:
    """`forms().get / create / batchUpdate`, recorded. `fail` raises on every call."""

    def __init__(self, form=None, fail=None):
        self.form, self.fail = form, fail
        self.gets = 0
        self.creates: list[dict] = []
        self.batches: list[tuple[str, list[dict]]] = []

    def forms(self):
        return self

    def get(self, formId):  # noqa: N803
        self.gets += 1
        return _Call(self.form, self.fail)

    def create(self, body):
        self.creates.append(body)
        return _Call({
            "formId": "NEWF", "info": body["info"],
            "responderUri": "https://docs.google.com/forms/d/e/PUB/viewform",
        }, self.fail)

    def batchUpdate(self, formId, body):  # noqa: N802, N803
        self.batches.append((formId, body["requests"]))
        replies = []
        for req in body["requests"]:
            if "createItem" in req:
                replies.append({"createItem": {"itemId": "NEW_ITEM", "questionId": ["NEW_Q"]}})
            else:
                replies.append({})
        return _Call({"replies": replies}, self.fail)

    def responses(self):
        raise AssertionError("forms_manage never reads responses")

    def requests(self):
        return [reqs for _fid, reqs in self.batches]


class _HttpErr(Exception):
    def __init__(self, status, message):
        super().__init__(f"<HttpError {status}: {message}>")
        self.resp = type("R", (), {"status": status})()
        self.content = json.dumps({"error": {"message": message, "code": status}}).encode()


def _q(item_id, title, qid, body, required=False):
    return {"itemId": item_id, "title": title, "questionItem": {
        "question": {"questionId": qid, "required": required, **body},
    }}


FORM = {
    "formId": "F1",
    "info": {"title": "Team lunch"},
    "items": [
        _q("i_name", "Name", "q_name", {"textQuestion": {}}, required=True),
        _q("i_day", "Day", "q_day", {"choiceQuestion": {
            "type": "RADIO", "options": [{"value": "Mon"}, {"value": "Tue"}],
        }}),
        {"itemId": "i_sec", "title": "Part two", "pageBreakItem": {}},
    ],
}
COUNT = 3


@pytest.fixture
def live(monkeypatch):
    def _install(**kw):
        fake = _FakeForms(**{"form": FORM, **kw})
        monkeypatch.setattr(FM, "settings", _Live())
        monkeypatch.setattr(FM, "_forms", lambda account=None: fake)
        return fake
    return _install


def _boom(monkeypatch):
    def fail(account=None):
        raise AssertionError("_forms() must not be called")
    monkeypatch.setattr(FM, "_forms", fail)


# --- dispatch and mock -----------------------------------------------------------


def test_unknown_action_names_the_options():
    """'delete' is not an action; the message must say what is, not guess."""
    with pytest.raises(FM.FormsManageError, match="unknown action.*add_question"):
        FM.forms_manage("delete", "F1")


def test_mock_mode_writes_nothing_for_every_action(monkeypatch):
    """The suite runs in DRY_RUN; a mock that reached Google would create real forms."""
    _boom(monkeypatch)
    for action in FM.ACTIONS:
        out = FM.forms_manage(
            action, "F1", title="T", question_type="short_text", item_id="i_name",
            at=1, confirm=True,
        )
        assert out["_mock"] is True and out["touched_item_ids"] == [], action
        assert out["status"].startswith("not_") and out["status"].endswith("_mock"), action


def test_every_action_but_create_needs_a_form_and_refuses_the_responder_link(monkeypatch):
    """A write with no form id would be a 400 from Google; a /d/e/ id is the responder's, not the form's."""
    _boom(monkeypatch)
    with pytest.raises(FM.FormsManageError, match="set_title needs form_id_or_url"):
        FM.forms_manage("set_title", title="x")
    with pytest.raises(FM.FormsManageError, match="responder link"):
        FM.forms_manage("set_title", "https://docs.google.com/forms/d/e/PUB/viewform", title="x")


# --- the scope 403 ------------------------------------------------------------------


def test_a_scope_403_becomes_the_exact_guidance(live):
    """The token predates forms.body; without this he gets 'may not have access' and tries the wrong account."""
    live(fail=_HttpErr(403, "Request had insufficient authentication scopes."))
    with pytest.raises(FM.FormsManageError) as info:
        FM.forms_manage("set_quiz", "F1", enabled=True)
    assert str(info.value) == (
        "Forms needs a scope the token does not hold yet. Set IBLU_FORMS=true in "
        ".env, then run `python scripts/connect_google.py --manual` once (works "
        "from a phone) and restart iblu-mcp.service."
    )
    with pytest.raises(FM.FormsManageError, match="IBLU_FORMS=true"):
        FM.forms_manage("create", title="New")


# --- create / set_title / set_quiz ---------------------------------------------------


def test_create_sends_only_a_title_then_the_description_as_a_second_call(live):
    """forms.create rejects a description in the body; it has to be an updateFormInfo afterwards."""
    fake = live()
    out = FM.forms_manage("create", title="  Sign-up ", description="Who is coming")
    assert fake.creates == [{"info": {"title": "Sign-up", "documentTitle": "Sign-up"}}]
    assert fake.batches == [("NEWF", [{"updateFormInfo": {
        "info": {"description": "Who is coming"}, "updateMask": "description",
    }}])]
    assert out["form_id"] == "NEWF" and out["status"] == "created"
    assert out["url"] == "https://docs.google.com/forms/d/NEWF/edit"
    assert out["responder_url"].endswith("/viewform")
    assert out["touched_item_ids"] == [] and out["applied"] == 1

    fake = live()
    assert FM.forms_manage("create", title="Bare")["applied"] == 0
    assert fake.batches == []
    with pytest.raises(FM.FormsManageError, match="create needs title"):
        FM.forms_manage("create", title="  ")


def test_set_title_masks_exactly_the_fields_given(live):
    """A mask naming description when only title was given blanks the description."""
    fake = live()
    FM.forms_manage("set_title", "F1", title="Lunch v2")
    FM.forms_manage("set_title", "F1", description="")
    FM.forms_manage("set_title", "F1", title="A", description="B")
    assert fake.requests() == [
        [{"updateFormInfo": {"info": {"title": "Lunch v2"}, "updateMask": "title"}}],
        [{"updateFormInfo": {"info": {"description": ""}, "updateMask": "description"}}],
        [{"updateFormInfo": {"info": {"title": "A", "description": "B"}, "updateMask": "title,description"}}],
    ]
    assert fake.gets == 0, "no item index is needed, so no fetch"
    with pytest.raises(FM.FormsManageError, match="title and/or description"):
        FM.forms_manage("set_title", "F1")


def test_set_quiz_updates_the_setting_with_its_mask(live):
    fake = live()
    out = FM.forms_manage("set_quiz", "F1", enabled=False)
    assert fake.requests() == [[{"updateSettings": {
        "settings": {"quizSettings": {"isQuiz": False}}, "updateMask": "quizSettings.isQuiz",
    }}]]
    assert out["is_quiz"] is False and out["status"] == "updated"
    FM.forms_manage("set_quiz", "F1")
    assert fake.requests()[1][0]["updateSettings"]["settings"] == {"quizSettings": {"isQuiz": True}}


# --- add_question ---------------------------------------------------------------------


def test_add_question_appends_at_the_live_end_and_returns_the_new_item_id(live):
    """createItem needs location.index; the end is the live item count, which only a fetch knows."""
    fake = live()
    out = FM.forms_manage("add_question", "https://docs.google.com/forms/d/F1/edit",
                          title="Email", question_type="short_text", required=True)
    assert fake.gets == 1
    assert fake.batches == [("F1", [{"createItem": {
        "item": {"title": "Email", "questionItem": {"question": {
            "required": True, "textQuestion": {"paragraph": False},
        }}},
        "location": {"index": COUNT},
    }}])]
    assert out["item_id"] == "NEW_ITEM" and out["question_id"] == "NEW_Q"
    assert out["touched_item_ids"] == ["NEW_ITEM"] and out["position"] == COUNT + 1
    assert out["status"] == "added"


@pytest.mark.parametrize("qtype, expected", [
    ("paragraph", {"textQuestion": {"paragraph": True}}),
    ("multiple_choice", {"choiceQuestion": {"type": "RADIO", "options": [{"value": "A"}, {"value": "B"}]}}),
    ("checkbox", {"choiceQuestion": {"type": "CHECKBOX", "options": [{"value": "A"}, {"value": "B"}]}}),
    ("dropdown", {"choiceQuestion": {"type": "DROP_DOWN", "options": [{"value": "A"}, {"value": "B"}]}}),
    ("scale", {"scaleQuestion": {"low": 1, "high": 5}}),
    ("date", {"dateQuestion": {"includeYear": True}}),
    ("time", {"timeQuestion": {}}),
])
def test_add_question_builds_each_type_the_way_the_api_spells_it(live, qtype, expected):
    """A checkbox sent as RADIO is a single-choice question nobody asked for."""
    fake = live()
    FM.forms_manage("add_question", "F1", title="Q", question_type=qtype, options=["A", " B "])
    question = fake.requests()[0][0]["createItem"]["item"]["questionItem"]["question"]
    assert question == {"required": False, **expected}


def test_add_question_scale_takes_bounds_and_labels_and_refuses_what_forms_rejects(live):
    """Forms allows low in {0,1} and high in 2..10; anything else is a 400 that names neither."""
    fake = live()
    FM.forms_manage("add_question", "F1", title="Rate", question_type="scale",
                    scale_low=0, scale_high=10, scale_low_label="Never", scale_high_label="Always")
    q = fake.requests()[0][0]["createItem"]["item"]["questionItem"]["question"]
    assert q["scaleQuestion"] == {"low": 0, "high": 10, "lowLabel": "Never", "highLabel": "Always"}
    with pytest.raises(FM.FormsManageError, match="scale needs"):
        FM.forms_manage("add_question", "F1", title="R", question_type="scale", scale_high=11)
    with pytest.raises(FM.FormsManageError, match="scale needs"):
        FM.forms_manage("add_question", "F1", title="R", question_type="scale", scale_low=2, scale_high=5)


def test_add_question_refuses_bad_input_before_any_fetch(monkeypatch):
    """Empty or duplicate options and an unknown type are 400s from Google; refusing first saves the round trip and does not depend on mode."""
    _boom(monkeypatch)
    monkeypatch.setattr(FM, "settings", _Live())
    with pytest.raises(FM.FormsManageError, match="question_type must be one of"):
        FM.forms_manage("add_question", "F1", title="Q", question_type="radio")
    with pytest.raises(FM.FormsManageError, match="non-empty options"):
        FM.forms_manage("add_question", "F1", title="Q", question_type="checkbox", options=[" "])
    with pytest.raises(FM.FormsManageError, match="options repeat \\(A\\)"):
        FM.forms_manage("add_question", "F1", title="Q", question_type="dropdown", options=["A", "B", "A"])
    with pytest.raises(FM.FormsManageError, match="add_question needs title"):
        FM.forms_manage("add_question", "F1", question_type="short_text")


def test_add_question_at_is_one_based_and_may_be_one_past_the_end(live):
    """at=1 must become index 0; at=count+1 is an append; at=count+2 is a 400."""
    fake = live()
    FM.forms_manage("add_question", "F1", title="Q", question_type="short_text", at=1, description="why")
    FM.forms_manage("add_question", "F1", title="Q", question_type="short_text", at=COUNT + 1)
    first, second = fake.requests()[0][0]["createItem"], fake.requests()[1][0]["createItem"]
    assert first["location"] == {"index": 0} and first["item"]["description"] == "why"
    assert second["location"] == {"index": COUNT}
    with pytest.raises(FM.FormsManageError, match=f"between 1 and {COUNT + 1}"):
        FM.forms_manage("add_question", "F1", title="Q", question_type="short_text", at=COUNT + 2)
    with pytest.raises(FM.FormsManageError, match="1 or more"):
        FM.forms_manage("add_question", "F1", title="Q", question_type="short_text", at=0)


# --- update / move / delete --------------------------------------------------------


def test_update_question_masks_only_the_fields_given_at_the_live_index(live):
    """updateItem addresses by index, not itemId; a mask wider than the change wipes the rest of the question."""
    fake = live()
    out = FM.forms_manage("update_question", "F1", item_id="i_day", title="Which day?", required=True)
    assert fake.batches == [("F1", [{"updateItem": {
        "item": {"itemId": "i_day", "title": "Which day?",
                 "questionItem": {"question": {"required": True}}},
        "location": {"index": 1},
        "updateMask": "title,questionItem.question.required",
    }}])]
    assert out["touched_item_ids"] == ["i_day"] and out["status"] == "updated"

    fake = live()
    FM.forms_manage("update_question", "F1", item_id="i_day", options=["Mon", "Tue", "Wed"])
    sent = fake.requests()[0][0]["updateItem"]
    assert sent["item"]["questionItem"]["question"]["choiceQuestion"] == {
        "type": "RADIO", "options": [{"value": "Mon"}, {"value": "Tue"}, {"value": "Wed"}],
    }
    assert sent["updateMask"] == "questionItem.question.choiceQuestion.options"


def test_update_question_refuses_options_on_a_text_question_and_unknown_ids(live):
    """choiceQuestion on a textQuestion item is a 400 from Google; a wrong id must not land on index 0."""
    fake = live()
    with pytest.raises(FM.FormsManageError, match="only be set on a choice question"):
        FM.forms_manage("update_question", "F1", item_id="i_name", options=["a", "b"])
    with pytest.raises(FM.FormsManageError, match="no item with item_id 'nope'"):
        FM.forms_manage("update_question", "F1", item_id="nope", title="x")
    with pytest.raises(FM.FormsManageError, match="at least one of"):
        FM.forms_manage("update_question", "F1", item_id="i_name")
    assert fake.batches == []


def test_move_question_goes_from_the_live_index_to_the_given_position(live):
    """moveItem takes two indices; the original must come from the live form, not from the caller's memory."""
    fake = live()
    out = FM.forms_manage("move_question", "F1", item_id="i_sec", at=1)
    assert fake.requests() == [[{"moveItem": {
        "originalLocation": {"index": 2}, "newLocation": {"index": 0},
    }}]]
    assert out["position"] == 1 and out["touched_item_ids"] == ["i_sec"] and out["status"] == "moved"
    with pytest.raises(FM.FormsManageError, match=f"between 1 and {COUNT}"):
        FM.forms_manage("move_question", "F1", item_id="i_sec", at=COUNT + 1)
    with pytest.raises(FM.FormsManageError, match="move_question needs at"):
        FM.forms_manage("move_question", "F1", item_id="i_sec")


def test_delete_question_needs_confirm_and_says_why(monkeypatch, live):
    """Answers go with the question and there is no Trash; the refusal must not depend on mode."""
    _boom(monkeypatch)
    with pytest.raises(FM.FormsManageError, match="version history.*confirm=True"):
        FM.forms_manage("delete_question", "F1", item_id="i_day")
    fake = live()
    with pytest.raises(FM.FormsManageError, match="confirm=True"):
        FM.forms_manage("delete_question", "F1", item_id="i_day")
    assert fake.batches == []
    out = FM.forms_manage("delete_question", "F1", item_id="i_day", confirm=True)
    assert fake.requests() == [[{"deleteItem": {"location": {"index": 1}}}]]
    assert out["touched_item_ids"] == ["i_day"] and out["deleted_title"] == "Day"
    assert out["status"] == "deleted"


# --- add_section -------------------------------------------------------------------------


def test_add_section_is_a_page_break_item_with_a_title(live):
    """A section in the Forms UI is a titled pageBreakItem; a bare pageBreakItem is a blank page."""
    fake = live()
    out = FM.forms_manage("add_section", "F1", title="Part three", description="Almost done", at=2)
    assert fake.requests() == [[{"createItem": {
        "item": {"title": "Part three", "description": "Almost done", "pageBreakItem": {}},
        "location": {"index": 1},
    }}]]
    assert out["item_id"] == "NEW_ITEM" and out["position"] == 2 and out["status"] == "added"


# --- no form delete ------------------------------------------------------------------------


def test_there_is_no_form_delete_and_the_docstring_says_so():
    """A form is a Drive file; the only removal IBLU knows is drive_manage trash, which is recoverable."""
    source = inspect.getsource(FM)
    assert not any("delete" in a and "question" not in a for a in FM.ACTIONS)
    assert "forms().delete" not in source and "files().delete" not in source
    assert "No form delete" in FM.__doc__ and "drive_manage(action='trash')" in FM.__doc__
