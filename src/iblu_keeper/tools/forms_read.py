"""Google Forms, the read half: what a form asks, and what people answered.

Ignas asked for "as many tools as possible from Google Workspace" and enabled
the Forms API in the Cloud project on 2026-10-26. Forms is where he collects
answers from other people — sign-ups, votes, feedback — and until now the
only way to see them from here was the linked Sheet, when there was one.
This module reads the form itself and its responses straight from the
Forms API, so "who answered the lunch poll" is one call, not a hunt for the
right spreadsheet.

ONE MCP tool with an ``action`` parameter, per HANDOFF.md §12 (one tool per
domain × side-effect class): a new ``@mcp.tool`` costs a permission click
forever, so the five reads share an entry point. Everything here is
read-only; the writes live in ``tools.forms_manage``.

What each action returns:

  info        title, description, documentTitle, responder URL, edit URL,
              question count, whether it is a quiz.
  structure   every item in order — itemId, index, title, type, required,
              and the options of a choice question. Types: short_text,
              paragraph, multiple_choice, checkbox, dropdown, scale, date,
              time, file_upload, grid, section, page_break, text, image,
              video.
  responses   newest first, paged with ``limit`` / ``page_token``. Each
              response: responseId, submitted time, respondent email when the
              form collects it, and answers as {question title: value(s)},
              resolved against the live structure on every call.
  response    one response by ``response_id``, same shape.
  summary     for every choice / scale question, counts per option across
              ALL responses, plus the total. Pages through everything, and
              refuses above MAX_SUMMARY_RESPONSES rather than loop forever.

The scope situation, honestly. The Forms API is enabled, but the OAuth token
on the box was issued before ``forms.body`` and ``forms.responses.readonly``
were requested, so until Ignas re-authorises every live call here answers
403 "insufficient authentication scopes". That 403 is caught and turned into
one sentence saying what to do (``SCOPE_GUIDANCE``); nothing here can fix it
on his behalf, because consent is his to give. Mock mode (DRY_RUN=true)
never touches the API.

The ``/forms/d/e/<long id>/viewform`` responder link carries a *published*
id the API does not accept; only the edit link (``/forms/d/<id>/edit``) or
the bare form id works, and the refusal says so.
"""

from __future__ import annotations

import logging
import re

from ..config import resolve_account, settings
from .drive import _file_id, _http_detail, _http_status, _mock, friendly_access_error

logger = logging.getLogger("iblu_keeper.tools.forms_read")

ACTIONS = ("info", "structure", "responses", "response", "summary")

MAX_PAGE = 500
MAX_SUMMARY_RESPONSES = 2000

SCOPE_GUIDANCE = (
    "Forms needs a scope the token does not hold yet. Set IBLU_FORMS=true in "
    ".env, then run `python scripts/connect_google.py --manual` once (works "
    "from a phone) and restart iblu-mcp.service."
)

_FORM_URL_RE = re.compile(r"docs\.google\.com/forms/d/([a-zA-Z0-9_-]+)")
_RESPONDER_URL_RE = re.compile(r"docs\.google\.com/forms/d/e/([a-zA-Z0-9_-]+)")

_CHOICE_TYPES = {"RADIO": "multiple_choice", "CHECKBOX": "checkbox", "DROP_DOWN": "dropdown"}
_COUNTABLE = ("multiple_choice", "checkbox", "dropdown", "scale", "grid_row")
_LIST_VALUED = ("checkbox", "file_upload")


class FormsReadError(ValueError):
    """Bad input to `forms_read`, or a Forms call the token cannot make yet."""


# --------------------------------------------------------------------------- #
# Shared with forms_manage: id extraction, the service, the scope 403
# --------------------------------------------------------------------------- #
def form_id(url_or_id: str) -> str:
    """The form id from an edit URL or a bare id. Refuses the responder link."""
    s = (url_or_id or "").strip()
    if _RESPONDER_URL_RE.search(s):
        raise FormsReadError(
            "that is the form's responder link (/forms/d/e/...), whose id the "
            "Forms API does not accept. Use the edit link "
            "(https://docs.google.com/forms/d/<id>/edit) or the bare form id."
        )
    m = _FORM_URL_RE.search(s)
    if m:
        return m.group(1)
    return _file_id(s)


def _forms(account: str | None = None):
    from ..google_auth import build_service

    return build_service("forms", "v1", account=account)


def edit_url(fid: str) -> str:
    return f"https://docs.google.com/forms/d/{fid}/edit"


def scope_blocked(exc: Exception) -> bool:
    """True for the 403 Google returns when the token lacks a Forms scope."""
    if _http_status(exc) != 403:
        return False
    low = f"{_http_detail(exc)} {exc}".lower()
    return "insufficient" in low or "scope" in low


def api_error(exc: Exception, account: str | None, what: str, cls=FormsReadError) -> Exception:
    """The exception to raise for a failed Forms call.

    A scope 403 becomes ``cls(SCOPE_GUIDANCE)`` — the one case Ignas can act
    on, said in one sentence. Everything else goes through
    ``friendly_access_error`` so the account gets named.
    """
    if scope_blocked(exc):
        return cls(SCOPE_GUIDANCE)
    return friendly_access_error(exc, account, what)


def _fetch_form(service, fid: str, account: str | None) -> dict:
    try:
        return service.forms().get(formId=fid).execute()
    except Exception as exc:  # noqa: BLE001
        raise api_error(exc, account, "this Google Form") from exc


# --------------------------------------------------------------------------- #
# Structure — pure functions over the form JSON
# --------------------------------------------------------------------------- #
def _question(item: dict) -> dict | None:
    return (item.get("questionItem") or {}).get("question")


def item_type(item: dict) -> str:
    q = _question(item)
    if q is not None:
        if "textQuestion" in q:
            return "paragraph" if (q["textQuestion"] or {}).get("paragraph") else "short_text"
        if "choiceQuestion" in q:
            return _CHOICE_TYPES.get((q["choiceQuestion"] or {}).get("type", ""), "choice")
        if "scaleQuestion" in q:
            return "scale"
        if "dateQuestion" in q:
            return "date"
        if "timeQuestion" in q:
            return "time"
        if "fileUploadQuestion" in q:
            return "file_upload"
        return "question"
    if "questionGroupItem" in item:
        return "grid"
    if "pageBreakItem" in item:
        return "section" if (item.get("title") or "").strip() else "page_break"
    if "textItem" in item:
        return "text"
    if "imageItem" in item:
        return "image"
    if "videoItem" in item:
        return "video"
    return "unknown"


def _option_values(options: list[dict] | None) -> list[str]:
    out = []
    for o in options or []:
        if o.get("isOther"):
            out.append("Other")
        else:
            out.append(o.get("value", ""))
    return out


def structure(form: dict) -> list[dict]:
    """Every item in order, typed, with what a caller needs to edit or read it."""
    out: list[dict] = []
    for index, item in enumerate(form.get("items") or []):
        entry: dict = {
            "index": index + 1,
            "itemId": item.get("itemId", ""),
            "title": item.get("title", "") or "",
            "type": item_type(item),
        }
        if item.get("description"):
            entry["description"] = item["description"]
        q = _question(item)
        if q is not None:
            entry["questionId"] = q.get("questionId", "")
            entry["required"] = bool(q.get("required"))
            if "choiceQuestion" in q:
                entry["options"] = _option_values((q["choiceQuestion"] or {}).get("options"))
            if "scaleQuestion" in q:
                sc = q["scaleQuestion"] or {}
                entry["scale"] = {
                    "low": sc.get("low"), "high": sc.get("high"),
                    "low_label": sc.get("lowLabel", ""), "high_label": sc.get("highLabel", ""),
                }
        elif "questionGroupItem" in item:
            group = item["questionGroupItem"] or {}
            rows = []
            for rq in group.get("questions") or []:
                rows.append({
                    "questionId": rq.get("questionId", ""),
                    "title": (rq.get("rowQuestion") or {}).get("title", ""),
                    "required": bool(rq.get("required")),
                })
            entry["rows"] = rows
            entry["columns"] = _option_values(
                ((group.get("grid") or {}).get("columns") or {}).get("options")
            )
            entry["required"] = any(r["required"] for r in rows)
        out.append(entry)
    return out


def question_index(form: dict) -> dict[str, dict]:
    """questionId → {label, type, options, itemId}. Labels are unique: a
    repeated title gets its questionId appended, an empty one is named by it."""
    seen: set[str] = set()
    out: dict[str, dict] = {}

    def label_for(title: str, qid: str) -> str:
        label = (title or "").strip() or f"(untitled {qid})"
        if label in seen:
            label = f"{label} [{qid}]"
        seen.add(label)
        return label

    for entry in structure(form):
        if entry["type"] == "grid":
            for row in entry.get("rows") or []:
                qid = row["questionId"]
                out[qid] = {
                    "label": label_for(f"{entry['title']} — {row['title']}", qid),
                    "type": "grid_row", "options": entry.get("columns") or [],
                    "itemId": entry["itemId"],
                }
            continue
        qid = entry.get("questionId")
        if not qid:
            continue
        options = entry.get("options")
        if entry["type"] == "scale":
            sc = entry["scale"]
            if sc["low"] is not None and sc["high"] is not None:
                options = [str(n) for n in range(int(sc["low"]), int(sc["high"]) + 1)]
        out[qid] = {
            "label": label_for(entry["title"], qid), "type": entry["type"],
            "options": options or [], "itemId": entry["itemId"],
        }
    return out


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
def _values(answer: dict) -> list[str]:
    text = (answer.get("textAnswers") or {}).get("answers")
    if text:
        return [a.get("value", "") for a in text]
    files = (answer.get("fileUploadAnswers") or {}).get("answers")
    if files:
        return [a.get("fileName") or a.get("fileId", "") for a in files]
    return []


def resolve_response(resp: dict, qindex: dict[str, dict]) -> dict:
    """One API response as {label: value(s)}. A question deleted since the
    answer was given is still shown, labelled as deleted — the answer is
    real even when the question is gone."""
    answers: dict[str, object] = {}
    for qid, answer in (resp.get("answers") or {}).items():
        meta = qindex.get(qid)
        label = meta["label"] if meta else f"(deleted question {qid})"
        values = _values(answer)
        if meta and meta["type"] in _LIST_VALUED:
            answers[label] = values
        elif len(values) == 1:
            answers[label] = values[0]
        else:
            answers[label] = values
    out: dict = {
        "responseId": resp.get("responseId", ""),
        "submitted": resp.get("lastSubmittedTime") or resp.get("createTime", ""),
        "respondent_email": resp.get("respondentEmail") or None,
        "answers": answers,
    }
    if resp.get("totalScore") is not None:
        out["score"] = resp["totalScore"]
    return out


def _newest_first(responses: list[dict]) -> list[dict]:
    return sorted(
        responses,
        key=lambda r: r.get("lastSubmittedTime") or r.get("createTime") or "",
        reverse=True,
    )


def _list_page(service, fid: str, page_size: int, page_token: str | None, account: str | None) -> dict:
    kwargs: dict = {"formId": fid, "pageSize": page_size}
    if page_token:
        kwargs["pageToken"] = page_token
    try:
        return service.forms().responses().list(**kwargs).execute()
    except Exception as exc:  # noqa: BLE001
        raise api_error(exc, account, "this Google Form's responses") from exc


def _all_responses(service, fid: str, account: str | None) -> list[dict]:
    """Every response, or a refusal above MAX_SUMMARY_RESPONSES.

    The page cap is the loop's second guard: a token that keeps coming back
    with empty pages would otherwise never trip the count."""
    out: list[dict] = []
    token = None
    max_pages = MAX_SUMMARY_RESPONSES // MAX_PAGE + 2
    for _ in range(max_pages):
        page = _list_page(service, fid, MAX_PAGE, token, account)
        out.extend(page.get("responses") or [])
        if len(out) > MAX_SUMMARY_RESPONSES:
            raise FormsReadError(
                f"this form has more than {MAX_SUMMARY_RESPONSES} responses; summary "
                "refuses to page through them all in one call. Use action='responses' "
                "with page_token, or the form's linked Sheet."
            )
        token = page.get("nextPageToken")
        if not token:
            return out
    raise FormsReadError(
        f"the responses listing kept returning a next page after {max_pages} pages "
        "without reaching the end; summary refuses to loop further."
    )


def summarise(form: dict, responses: list[dict]) -> list[dict]:
    """Counts per option for every choice / scale question (and grid row).

    A free-text "Other" answer counts under its own text, so the summary
    shows what people wrote rather than hiding it under "Other"."""
    qindex = question_index(form)
    out: list[dict] = []
    for qid, meta in qindex.items():
        if meta["type"] not in _COUNTABLE:
            continue
        counts: dict[str, int] = {opt: 0 for opt in meta["options"] if opt != "Other"}
        answered = 0
        for resp in responses:
            answer = (resp.get("answers") or {}).get(qid)
            if not answer:
                continue
            values = _values(answer)
            if not values:
                continue
            answered += 1
            for v in values:
                counts[v] = counts.get(v, 0) + 1
        out.append({
            "itemId": meta["itemId"], "questionId": qid, "title": meta["label"],
            "type": meta["type"], "counts": counts,
            "answered": answered, "unanswered": len(responses) - answered,
        })
    return out


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
def forms_read(
    action: str,
    form_id_or_url: str,
    response_id: str | None = None,
    limit: int = 50,
    page_token: str | None = None,
    account: str | None = None,
) -> dict:
    """Read a Google Form — see the module docstring for each action.

    ``form_id_or_url`` is the edit link or the bare id (not the responder
    link). ``response_id`` is for ``response``; ``limit`` and ``page_token``
    page ``responses``. ``account`` — which Google account: ``blt``
    (default), ``deadlift``, ``choco``.
    """
    if action not in ACTIONS:
        raise FormsReadError(
            f"unknown action {action!r} — expected one of: {', '.join(ACTIONS)}"
        )
    if not form_id_or_url or not str(form_id_or_url).strip():
        raise FormsReadError(f"{action} needs form_id_or_url")
    if action == "response" and not (response_id and str(response_id).strip()):
        raise FormsReadError("response needs response_id")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise FormsReadError("limit must be an integer")
    limit = max(1, min(limit, MAX_PAGE))
    fid = form_id(form_id_or_url)

    account = resolve_account(account)
    if settings.use_mock:
        return _mock({"action": action, "form_id": fid, "status": "not_read_mock"})

    service = _forms(account)
    form = _fetch_form(service, fid, account)
    base = {"form_id": fid, "action": action, "url": edit_url(fid)}
    info = form.get("info") or {}

    if action == "info":
        items = form.get("items") or []
        return {
            **base,
            "title": info.get("title", ""),
            "description": info.get("description", ""),
            "document_title": info.get("documentTitle", ""),
            "responder_url": form.get("responderUri", ""),
            "question_count": sum(
                1 for it in items if "questionItem" in it or "questionGroupItem" in it
            ),
            "item_count": len(items),
            "is_quiz": bool(((form.get("settings") or {}).get("quizSettings") or {}).get("isQuiz")),
            "linked_sheet_id": form.get("linkedSheetId") or None,
            "status": "read",
        }

    if action == "structure":
        items = structure(form)
        return {**base, "title": info.get("title", ""), "items": items,
                "count": len(items), "status": "read"}

    qindex = question_index(form)

    if action == "responses":
        page = _list_page(service, fid, limit, page_token, account)
        rows = [resolve_response(r, qindex) for r in _newest_first(page.get("responses") or [])]
        return {
            **base, "responses": rows, "count": len(rows),
            "next_page_token": page.get("nextPageToken") or None, "status": "read",
        }

    if action == "response":
        try:
            raw = service.forms().responses().get(formId=fid, responseId=response_id).execute()
        except Exception as exc:  # noqa: BLE001
            raise api_error(exc, account, "this Google Form response") from exc
        return {**base, "response": resolve_response(raw, qindex), "status": "read"}

    # summary
    responses = _all_responses(service, fid, account)
    questions = summarise(form, responses)
    return {
        **base, "title": info.get("title", ""),
        "total_responses": len(responses), "questions": questions,
        "status": "read",
    }
