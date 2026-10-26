"""`tools.forms_read` — the read half of the Forms domain.

Fakes only: a hand-built Forms API form, scripted response pages, and a fake
service that records every call's kwargs. `settings` is swapped whole
(frozen dataclass, DRY_RUN pinned by conftest). No network.
"""

from __future__ import annotations

import json

import pytest

from iblu_keeper.tools import forms_read as FR


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
    """`forms().get / responses().list / responses().get`, recorded.

    `pages` is the scripted sequence of responses().list results, popped in
    order. `fail` is an exception raised by every call."""

    def __init__(self, form=None, pages=None, single=None, fail=None):
        self.form, self.pages, self.single, self.fail = form, list(pages or []), single, fail
        self.calls: list[tuple[str, dict]] = []

    def forms(self):
        return self

    def responses(self):
        return self

    def get(self, **kwargs):
        if "responseId" in kwargs:
            self.calls.append(("responses.get", kwargs))
            return _Call(self.single, self.fail)
        self.calls.append(("forms.get", kwargs))
        return _Call(self.form, self.fail)

    def list(self, **kwargs):
        self.calls.append(("responses.list", kwargs))
        assert self.pages, "responses().list() called more times than scripted"
        return _Call(self.pages.pop(0), self.fail)

    def batchUpdate(self, **kwargs):  # noqa: N802
        raise AssertionError("forms_read must never write")

    def kwargs(self, key):
        return [kw for k, kw in self.calls if k == key]


class _HttpErr(Exception):
    """Enough of googleapiclient's HttpError for `_http_status` / `_http_detail`."""

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
    "info": {"title": "Team lunch", "description": "Pick a day", "documentTitle": "Team lunch (file)"},
    "settings": {"quizSettings": {"isQuiz": False}},
    "responderUri": "https://docs.google.com/forms/d/e/PUBLISHED/viewform",
    "linkedSheetId": "SHEET1",
    "items": [
        _q("i_name", "Name", "q_name", {"textQuestion": {}}, required=True),
        _q("i_notes", "Notes", "q_notes", {"textQuestion": {"paragraph": True}}),
        _q("i_day", "Day", "q_day", {"choiceQuestion": {
            "type": "RADIO", "options": [{"value": "Mon"}, {"value": "Tue"}, {"isOther": True}],
        }}, required=True),
        _q("i_food", "Food", "q_food", {"choiceQuestion": {
            "type": "CHECKBOX", "options": [{"value": "Pizza"}, {"value": "Sushi"}],
        }}),
        _q("i_city", "City", "q_city", {"choiceQuestion": {
            "type": "DROP_DOWN", "options": [{"value": "Vilnius"}, {"value": "Kaunas"}],
        }}),
        _q("i_rate", "Rating", "q_rate", {"scaleQuestion": {
            "low": 1, "high": 3, "lowLabel": "Bad", "highLabel": "Great",
        }}),
        _q("i_date", "Date", "q_date", {"dateQuestion": {"includeYear": True}}),
        _q("i_time", "Time", "q_time", {"timeQuestion": {}}),
        _q("i_file", "Receipt", "q_file", {"fileUploadQuestion": {"folderId": "X"}}),
        {"itemId": "i_sec", "title": "Part two", "pageBreakItem": {}},
        {"itemId": "i_pb", "pageBreakItem": {}},
        {"itemId": "i_text", "title": "Thanks", "textItem": {}},
        {"itemId": "i_img", "title": "Logo", "imageItem": {"image": {}}},
        {"itemId": "i_vid", "title": "Intro", "videoItem": {"video": {}}},
    ],
}


def _text(*values):
    return {"textAnswers": {"answers": [{"value": v} for v in values]}}


R_OLD = {
    "responseId": "r_old", "createTime": "2026-10-20T10:00:00Z",
    "lastSubmittedTime": "2026-10-20T10:00:00Z", "respondentEmail": "ana@x.com",
    "answers": {
        "q_name": _text("Ana"), "q_day": _text("Mon"), "q_food": _text("Pizza", "Sushi"),
        "q_rate": _text("3"),
        "q_file": {"fileUploadAnswers": {"answers": [{"fileId": "f1", "fileName": "receipt.pdf"}]}},
    },
}
R_NEW = {
    "responseId": "r_new", "createTime": "2026-10-21T09:00:00Z",
    "lastSubmittedTime": "2026-10-21T09:00:00Z",
    "answers": {
        "q_name": _text("Ben"), "q_day": _text("Wednesday please"), "q_food": _text("Pizza"),
        "q_rate": _text("3"), "q_gone": _text("an answer to a question since deleted"),
    },
}


@pytest.fixture
def live(monkeypatch):
    def _install(**kw):
        fake = _FakeForms(**{"form": FORM, **kw})
        monkeypatch.setattr(FR, "settings", _Live())
        monkeypatch.setattr(FR, "_forms", lambda account=None: fake)
        return fake
    return _install


# --- dispatch, ids and mock -------------------------------------------------------


def test_unknown_action_names_the_options():
    """A typo must not fall through to a default read."""
    with pytest.raises(FR.FormsReadError, match="unknown action.*structure"):
        FR.forms_read("answers", "F1")


def test_mock_mode_reads_nothing(monkeypatch):
    """The suite runs in DRY_RUN; a mock that built a service would be a live call."""
    def fail(account=None):
        raise AssertionError("_forms() must not be called")
    monkeypatch.setattr(FR, "_forms", fail)
    for action in FR.ACTIONS:
        out = FR.forms_read(action, "F1", response_id="r1")
        assert out["_mock"] is True and out["status"] == "not_read_mock", action


def test_form_id_takes_the_edit_link_or_a_bare_id_and_refuses_the_responder_link():
    """`_file_id` in drive.py only knows document/spreadsheets/presentation URLs; a Forms edit link would pass through whole, and the /d/e/ responder id is not a form id at all."""
    assert FR.form_id("https://docs.google.com/forms/d/ABC-123_x/edit") == "ABC-123_x"
    assert FR.form_id("https://docs.google.com/forms/d/ABC/edit#responses") == "ABC"
    assert FR.form_id("  ABC  ") == "ABC"
    with pytest.raises(FR.FormsReadError, match="responder link"):
        FR.form_id("https://docs.google.com/forms/d/e/1FAIpQLSd-long/viewform")


def test_response_needs_an_id_before_any_fetch(monkeypatch):
    """Without the check the API would be asked for response None and answer with a 400 nobody can read."""
    def fail(account=None):
        raise AssertionError("_forms() must not be called")
    monkeypatch.setattr(FR, "settings", _Live())
    monkeypatch.setattr(FR, "_forms", fail)
    with pytest.raises(FR.FormsReadError, match="response_id"):
        FR.forms_read("response", "F1")


# --- the scope 403 --------------------------------------------------------------------


def test_a_scope_403_becomes_the_exact_guidance(live):
    """The token predates the Forms scopes; without this he gets a generic 'may not have access' that sends him to the wrong account."""
    live(fail=_HttpErr(403, "Request had insufficient authentication scopes."))
    with pytest.raises(FR.FormsReadError) as info:
        FR.forms_read("info", "F1")
    assert str(info.value) == (
        "Forms needs a scope the token does not hold yet. Set IBLU_FORMS=true in "
        ".env, then run `python scripts/connect_google.py --manual` once (works "
        "from a phone) and restart iblu-mcp.service."
    )
    assert str(info.value) == FR.SCOPE_GUIDANCE


def test_an_ordinary_403_still_names_the_account(live):
    """A form in another Workspace is an access problem, not a scope problem; conflating them would tell him to re-authorise for nothing."""
    live(fail=_HttpErr(403, "The caller does not have permission"))
    with pytest.raises(RuntimeError, match="'blt' account may not have access"):
        FR.forms_read("info", "F1")


# --- info and structure --------------------------------------------------------------


def test_info_reports_titles_urls_and_counts(live):
    fake = live()
    out = FR.forms_read("info", "https://docs.google.com/forms/d/F1/edit")
    assert fake.kwargs("forms.get") == [{"formId": "F1"}]
    assert out["title"] == "Team lunch" and out["description"] == "Pick a day"
    assert out["document_title"] == "Team lunch (file)"
    assert out["responder_url"].endswith("/viewform")
    assert out["url"] == "https://docs.google.com/forms/d/F1/edit"
    assert out["question_count"] == 9 and out["item_count"] == 14
    assert out["is_quiz"] is False and out["linked_sheet_id"] == "SHEET1"
    assert out["status"] == "read"


def test_structure_types_every_kind_of_item(live):
    """A paragraph typed as short_text, or a dropdown as multiple_choice, is an edit that lands on the wrong kind of question."""
    live()
    out = FR.forms_read("structure", "F1")
    assert [i["type"] for i in out["items"]] == [
        "short_text", "paragraph", "multiple_choice", "checkbox", "dropdown",
        "scale", "date", "time", "file_upload", "section", "page_break",
        "text", "image", "video",
    ]
    assert [i["index"] for i in out["items"]][:3] == [1, 2, 3]
    assert out["count"] == 14


def test_structure_carries_required_options_and_scale_bounds(live):
    """The options are what update_question and the summary key on; 'Other' must appear as a choice, not vanish."""
    live()
    items = {i["itemId"]: i for i in FR.forms_read("structure", "F1")["items"]}
    assert items["i_name"]["required"] is True and items["i_notes"]["required"] is False
    assert items["i_day"]["options"] == ["Mon", "Tue", "Other"]
    assert items["i_city"]["options"] == ["Vilnius", "Kaunas"]
    assert items["i_rate"]["scale"] == {"low": 1, "high": 3, "low_label": "Bad", "high_label": "Great"}
    assert items["i_day"]["questionId"] == "q_day"
    assert "options" not in items["i_name"] and "required" not in items["i_sec"]


# --- responses -------------------------------------------------------------------------


def test_responses_are_newest_first_with_answers_keyed_by_question_title(live):
    """The API keys answers by questionId; a dict of ids is unreadable, and checkbox answers must stay a list."""
    live(pages=[{"responses": [R_OLD, R_NEW]}])
    out = FR.forms_read("responses", "F1")
    assert [r["responseId"] for r in out["responses"]] == ["r_new", "r_old"]
    old = out["responses"][1]
    assert old["submitted"] == "2026-10-20T10:00:00Z"
    assert old["respondent_email"] == "ana@x.com"
    assert old["answers"]["Name"] == "Ana"
    assert old["answers"]["Day"] == "Mon"
    assert old["answers"]["Food"] == ["Pizza", "Sushi"]
    assert old["answers"]["Receipt"] == ["receipt.pdf"]
    assert out["responses"][0]["respondent_email"] is None
    assert out["responses"][0]["answers"]["Food"] == ["Pizza"], "one tick is still a list"
    assert out["count"] == 2 and out["next_page_token"] is None


def test_responses_page_with_limit_and_token(live):
    """A limit the API never sees is a 5,000-row page; a token not passed back is the same page forever."""
    fake = live(pages=[{"responses": [R_OLD], "nextPageToken": "T2"}])
    out = FR.forms_read("responses", "F1", limit=7, page_token="T1")
    assert fake.kwargs("responses.list") == [{"formId": "F1", "pageSize": 7, "pageToken": "T1"}]
    assert out["next_page_token"] == "T2"
    fake = live(pages=[{"responses": []}])
    FR.forms_read("responses", "F1", limit=99999)
    assert fake.kwargs("responses.list")[0]["pageSize"] == FR.MAX_PAGE


def test_an_answer_to_a_deleted_question_is_kept_and_labelled(live):
    """Dropping it would make the response look shorter than it was."""
    live(pages=[{"responses": [R_NEW]}])
    answers = FR.forms_read("responses", "F1")["responses"][0]["answers"]
    assert answers["(deleted question q_gone)"] == "an answer to a question since deleted"


def test_response_by_id_uses_the_same_shape(live):
    fake = live(single=R_OLD)
    out = FR.forms_read("response", "F1", response_id="r_old")
    assert fake.kwargs("responses.get") == [{"formId": "F1", "responseId": "r_old"}]
    assert out["response"]["answers"]["Day"] == "Mon" and out["status"] == "read"


def test_repeated_question_titles_do_not_collapse_into_one_key():
    """Two questions both called 'Name' would otherwise leave one answer silently overwritten."""
    form = {"items": [
        _q("a", "Name", "q1", {"textQuestion": {}}),
        _q("b", "Name", "q2", {"textQuestion": {}}),
        _q("c", "", "q3", {"textQuestion": {}}),
    ]}
    resp = {"responseId": "r", "answers": {"q1": _text("x"), "q2": _text("y"), "q3": _text("z")}}
    out = FR.resolve_response(resp, FR.question_index(form))
    assert out["answers"] == {"Name": "x", "Name [q2]": "y", "(untitled q3)": "z"}


# --- summary ------------------------------------------------------------------------------


def test_summary_counts_every_option_including_free_text_other(live):
    """A count that hides 'Other' answers under one bucket loses what people actually wrote; a scale must list every rung, including the ones nobody chose."""
    live(pages=[{"responses": [R_OLD, R_NEW]}])
    out = FR.forms_read("summary", "F1")
    by_title = {q["title"]: q for q in out["questions"]}
    assert out["total_responses"] == 2
    assert set(by_title) == {"Day", "Food", "City", "Rating"}, "text/date/time/file are not countable"
    assert by_title["Day"]["counts"] == {"Mon": 1, "Tue": 0, "Wednesday please": 1}
    assert by_title["Food"]["counts"] == {"Pizza": 2, "Sushi": 1}
    assert by_title["City"]["counts"] == {"Vilnius": 0, "Kaunas": 0}
    assert by_title["City"]["answered"] == 0 and by_title["City"]["unanswered"] == 2
    assert by_title["Rating"]["counts"] == {"1": 0, "2": 0, "3": 2}
    assert by_title["Rating"]["itemId"] == "i_rate"


def test_summary_pages_through_every_response(live):
    """Counting only the first page is a wrong number that looks right."""
    fake = live(pages=[
        {"responses": [R_OLD], "nextPageToken": "P2"},
        {"responses": [R_NEW]},
    ])
    out = FR.forms_read("summary", "F1")
    sent = fake.kwargs("responses.list")
    assert sent[0] == {"formId": "F1", "pageSize": FR.MAX_PAGE}
    assert sent[1] == {"formId": "F1", "pageSize": FR.MAX_PAGE, "pageToken": "P2"}
    assert out["total_responses"] == 2


def test_summary_refuses_above_the_cap_instead_of_looping(live):
    """A form with 50,000 responses would otherwise hold the tool call for minutes; and a token that never ends must not spin forever."""
    big = [dict(R_OLD, responseId=f"r{i}") for i in range(FR.MAX_PAGE)]
    pages = [{"responses": big, "nextPageToken": "more"} for _ in range(5)]
    fake = live(pages=pages)
    with pytest.raises(FR.FormsReadError, match="more than 2000 responses"):
        FR.forms_read("summary", "F1")
    assert len(fake.kwargs("responses.list")) == 5, "refused as soon as the count passed 2,000"

    fake = live(pages=[{"responses": [], "nextPageToken": "again"} for _ in range(10)])
    with pytest.raises(FR.FormsReadError, match="refuses to loop"):
        FR.forms_read("summary", "F1")
    assert len(fake.kwargs("responses.list")) == FR.MAX_SUMMARY_RESPONSES // FR.MAX_PAGE + 2
