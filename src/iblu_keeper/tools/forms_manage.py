"""Google Forms, the write half: build and edit a form by verb.

Ignas asked for "as many tools as possible from Google Workspace" and enabled
the Forms API in the Cloud project on 2026-10-26. Making a form by hand means
a browser and a dozen clicks per question; this module lets him say "a
sign-up form with name, email, and which day" and get the edit link back.
Reads live in ``tools.forms_read``.

ONE MCP tool with an ``action`` parameter, per HANDOFF.md §12 (one tool per
domain × side-effect class): a new ``@mcp.tool`` costs a permission click
forever, so the eight writes share an entry point. Every action is one
``forms().create`` or one ``forms().batchUpdate`` whose ``requests`` list is
the shape the Forms API expects, and every result reports the itemIds it
touched.

What each action sends:

  create           forms.create with info.title (+ documentTitle), then
                   updateFormInfo for the description if given — create
                   accepts only a title.
  set_title        updateFormInfo with an updateMask naming only the fields
                   given (title and/or description).
  add_question     createItem at location.index — ``at`` is 1-based, omit it
                   to append. Types: short_text, paragraph, multiple_choice,
                   checkbox, dropdown, scale, date, time. Choice types need
                   ``options``; scale takes low / high / labels.
  update_question  updateItem with an updateMask of exactly the fields given
                   (title, description, required, options). Options only on a
                   choice question.
  move_question    moveItem from the item's live index to ``at`` (1-based).
  delete_question  deleteItem. Needs ``confirm=True``: a question is not a
                   file, there is no Trash for it, and its answers go with it;
                   the form's version history in Drive is the only way back.
  add_section      createItem of a pageBreakItem with title / description.
  set_quiz         updateSettings quizSettings.isQuiz.

Indices. The Forms API addresses items by position, not by itemId, so every
action that names an item fetches the live form first and finds the index
from that fetch, every call. Nothing is cached between calls.

No form delete, ever. A form is a Drive file; removing one is
``drive_manage(action='trash')``, which is recoverable, and that is the only
removal IBLU knows. The Forms API has no delete call, and none would be
exposed here if it did.

The scope situation, honestly. The Forms API is enabled, but the OAuth token
on the box predates the ``forms.body`` scope, so until Ignas re-authorises
every live call here answers 403 "insufficient authentication scopes". That
403 is caught and turned into one sentence saying what to do
(``forms_read.SCOPE_GUIDANCE``). Mock mode (DRY_RUN=true) never touches the
API.
"""

from __future__ import annotations

import logging

from ..config import resolve_account, settings
from .drive import _mock
from .forms_read import _forms, api_error, edit_url, form_id, item_type

logger = logging.getLogger("iblu_keeper.tools.forms_manage")

ACTIONS = (
    "create", "set_title", "add_question", "update_question", "move_question",
    "delete_question", "add_section", "set_quiz",
)
QUESTION_TYPES = (
    "short_text", "paragraph", "multiple_choice", "checkbox", "dropdown",
    "scale", "date", "time",
)
_CHOICE = {"multiple_choice": "RADIO", "checkbox": "CHECKBOX", "dropdown": "DROP_DOWN"}
_CHOICE_TYPES = tuple(_CHOICE)
_NEEDS_FORM = tuple(a for a in ACTIONS if a != "create")
_NEEDS_ITEM = ("update_question", "move_question", "delete_question")

_MOCK_STATUS = {
    "create": "not_created_mock", "set_title": "not_updated_mock",
    "add_question": "not_added_mock", "update_question": "not_updated_mock",
    "move_question": "not_moved_mock", "delete_question": "not_deleted_mock",
    "add_section": "not_added_mock", "set_quiz": "not_updated_mock",
}
_STATUS = {
    "create": "created", "set_title": "updated", "add_question": "added",
    "update_question": "updated", "move_question": "moved",
    "delete_question": "deleted", "add_section": "added", "set_quiz": "updated",
}

_WHAT = "this Google Form"


class FormsManageError(ValueError):
    """Bad input to `forms_manage`, or a Forms call the token cannot make yet."""


# --------------------------------------------------------------------------- #
# API calls, each wrapped so a scope 403 says what to do
# --------------------------------------------------------------------------- #
def _fetch(service, fid: str, account: str | None) -> dict:
    try:
        return service.forms().get(formId=fid).execute()
    except Exception as exc:  # noqa: BLE001
        raise api_error(exc, account, _WHAT, FormsManageError) from exc


def _send(service, fid: str, requests: list[dict], account: str | None) -> dict:
    try:
        return service.forms().batchUpdate(
            formId=fid, body={"requests": requests},
        ).execute()
    except Exception as exc:  # noqa: BLE001
        raise api_error(exc, account, _WHAT, FormsManageError) from exc


def _index_of(form: dict, item_id: str) -> tuple[int, dict]:
    for index, item in enumerate(form.get("items") or []):
        if item.get("itemId") == item_id:
            return index, item
    raise FormsManageError(
        f"no item with item_id {item_id!r} in this form; "
        "forms_read(action='structure') lists the ids."
    )


def _position(at: int | None, count: int, *, insert: bool) -> int:
    """A 1-based ``at`` as a 0-based index. Inserting may land one past the
    last item (append); moving may not."""
    if at is None:
        return count
    last = count + 1 if insert else count
    if not 1 <= at <= last:
        raise FormsManageError(
            f"at must be between 1 and {last} for this form (got {at})"
        )
    return at - 1


# --------------------------------------------------------------------------- #
# Request builders — pure
# --------------------------------------------------------------------------- #
def _clean_options(options) -> list[str]:
    values = [str(o).strip() for o in (options or []) if str(o).strip()]
    if not values:
        raise FormsManageError("a choice question needs a non-empty options list")
    seen: set[str] = set()
    dupes = [v for v in values if v in seen or seen.add(v)]  # type: ignore[func-returns-value]
    if dupes:
        raise FormsManageError(
            f"options repeat ({', '.join(sorted(set(dupes)))}); Forms rejects duplicate options"
        )
    return values


def _question_body(
    question_type: str, options, required: bool,
    scale_low: int | None, scale_high: int | None,
    scale_low_label: str | None, scale_high_label: str | None,
) -> dict:
    q: dict = {"required": bool(required)}
    if question_type == "short_text":
        q["textQuestion"] = {"paragraph": False}
    elif question_type == "paragraph":
        q["textQuestion"] = {"paragraph": True}
    elif question_type in _CHOICE:
        q["choiceQuestion"] = {
            "type": _CHOICE[question_type],
            "options": [{"value": v} for v in _clean_options(options)],
        }
    elif question_type == "scale":
        low = 1 if scale_low is None else int(scale_low)
        high = 5 if scale_high is None else int(scale_high)
        if low not in (0, 1) or not 2 <= high <= 10 or high <= low:
            raise FormsManageError(
                f"scale needs low of 0 or 1 and high of 2-10 (got {low}-{high})"
            )
        sc: dict = {"low": low, "high": high}
        if scale_low_label:
            sc["lowLabel"] = scale_low_label
        if scale_high_label:
            sc["highLabel"] = scale_high_label
        q["scaleQuestion"] = sc
    elif question_type == "date":
        q["dateQuestion"] = {"includeYear": True}
    else:  # time
        q["timeQuestion"] = {}
    return q


def _create_item(item: dict, index: int) -> dict:
    return {"createItem": {"item": item, "location": {"index": index}}}


def _update_question_request(
    index: int, item_id: str, live: dict,
    title: str | None, description: str | None, required: bool | None, options,
) -> dict:
    item: dict = {"itemId": item_id}
    mask: list[str] = []
    if title is not None:
        item["title"] = title
        mask.append("title")
    if description is not None:
        item["description"] = description
        mask.append("description")
    question: dict = {}
    if required is not None:
        question["required"] = bool(required)
        mask.append("questionItem.question.required")
    if options is not None:
        kind = item_type(live)
        if kind not in _CHOICE_TYPES:
            raise FormsManageError(
                f"options can only be set on a choice question; {item_id!r} is {kind}"
            )
        question["choiceQuestion"] = {
            "type": _CHOICE[kind],
            "options": [{"value": v} for v in _clean_options(options)],
        }
        mask.append("questionItem.question.choiceQuestion.options")
    if question:
        item["questionItem"] = {"question": question}
    return {"updateItem": {
        "item": item, "location": {"index": index}, "updateMask": ",".join(mask),
    }}


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
def forms_manage(
    action: str,
    form_id_or_url: str | None = None,
    title: str | None = None,
    description: str | None = None,
    question_type: str | None = None,
    options: list[str] | None = None,
    required: bool | None = None,
    scale_low: int | None = None,
    scale_high: int | None = None,
    scale_low_label: str | None = None,
    scale_high_label: str | None = None,
    at: int | None = None,
    item_id: str | None = None,
    enabled: bool = True,
    confirm: bool = False,
    account: str | None = None,
) -> dict:
    """Create or edit a Google Form by verb — see the module docstring.

    ``form_id_or_url`` is the edit link or bare id (every action but
    ``create``). ``title`` / ``description`` serve create, set_title,
    add_question, update_question and add_section. ``question_type``,
    ``options``, ``required`` and the ``scale_*`` fields describe a question;
    ``at`` is a 1-based position (omit to append); ``item_id`` names the
    question to update, move or delete; ``enabled`` is for set_quiz;
    ``confirm`` is for delete_question. Never deletes a form — use
    ``drive_manage(action='trash')``. ``account`` — which Google account:
    ``blt`` (default), ``deadlift``, ``choco``.
    """
    if action not in ACTIONS:
        raise FormsManageError(
            f"unknown action {action!r} — expected one of: {', '.join(ACTIONS)}"
        )

    # Input checks that need no form come first, so a refusal never depends
    # on whether the server is in mock mode.
    fid = None
    if action in _NEEDS_FORM:
        if not form_id_or_url or not str(form_id_or_url).strip():
            raise FormsManageError(f"{action} needs form_id_or_url")
        try:
            fid = form_id(form_id_or_url)
        except ValueError as exc:
            raise FormsManageError(str(exc)) from exc
    if action in ("create", "add_question", "add_section") and not (title and title.strip()):
        raise FormsManageError(f"{action} needs title")
    if action == "set_title" and title is None and description is None:
        raise FormsManageError("set_title needs title and/or description")
    if action in _NEEDS_ITEM and not (item_id and str(item_id).strip()):
        raise FormsManageError(f"{action} needs item_id")
    if action == "add_question":
        if question_type not in QUESTION_TYPES:
            raise FormsManageError(
                f"question_type must be one of: {', '.join(QUESTION_TYPES)} "
                f"(got {question_type!r})"
            )
        if question_type in _CHOICE_TYPES:
            _clean_options(options)
    if action == "update_question" and all(
        v is None for v in (title, description, required, options)
    ):
        raise FormsManageError(
            "update_question needs at least one of title, description, required, options"
        )
    if action == "move_question" and at is None:
        raise FormsManageError("move_question needs at (1-based position)")
    if at is not None:
        try:
            at = int(at)
        except (TypeError, ValueError):
            raise FormsManageError("at must be a 1-based integer position")
        if at < 1:
            raise FormsManageError(f"at must be 1 or more (got {at})")
    if action == "delete_question" and not confirm:
        raise FormsManageError(
            f"delete_question removes question {item_id!r} and every answer to it "
            "from the form. A question is not a file: there is no Trash for it, and "
            "the form's version history in Drive is the only way back. Pass "
            "confirm=True to go ahead."
        )

    account = resolve_account(account)
    if settings.use_mock:
        return _mock({
            "action": action, "form_id": fid, "touched_item_ids": [],
            "status": _MOCK_STATUS[action],
        })

    service = _forms(account)

    if action == "create":
        body = {"info": {"title": title.strip(), "documentTitle": title.strip()}}  # type: ignore[union-attr]
        try:
            created = service.forms().create(body=body).execute()
        except Exception as exc:  # noqa: BLE001
            raise api_error(exc, account, "Google Forms", FormsManageError) from exc
        new_id = created.get("formId")
        if not new_id:
            raise RuntimeError(f"Forms create returned no formId (response={created!r}).")
        applied = 0
        if description:
            _send(service, new_id, [{"updateFormInfo": {
                "info": {"description": description}, "updateMask": "description",
            }}], account)
            applied = 1
        logger.info("forms_manage create: id=%s title=%r", new_id, title)
        return {
            "form_id": new_id, "action": action, "title": title.strip(),  # type: ignore[union-attr]
            "url": edit_url(new_id),
            "responder_url": created.get("responderUri", ""),
            "touched_item_ids": [], "applied": applied, "status": _STATUS[action],
        }

    assert fid is not None
    base = {"form_id": fid, "action": action, "url": edit_url(fid)}
    touched: list[str] = []
    out_extra: dict = {}

    if action == "set_title":
        info: dict = {}
        mask: list[str] = []
        if title is not None:
            info["title"] = title
            mask.append("title")
        if description is not None:
            info["description"] = description
            mask.append("description")
        requests = [{"updateFormInfo": {"info": info, "updateMask": ",".join(mask)}}]
    elif action == "set_quiz":
        requests = [{"updateSettings": {
            "settings": {"quizSettings": {"isQuiz": bool(enabled)}},
            "updateMask": "quizSettings.isQuiz",
        }}]
        out_extra["is_quiz"] = bool(enabled)
    else:
        form = _fetch(service, fid, account)
        count = len(form.get("items") or [])
        if action == "add_question":
            index = _position(at, count, insert=True)
            item: dict = {"title": title.strip(), "questionItem": {  # type: ignore[union-attr]
                "question": _question_body(
                    question_type, options, bool(required),  # type: ignore[arg-type]
                    scale_low, scale_high, scale_low_label, scale_high_label,
                ),
            }}
            if description:
                item["description"] = description
            requests = [_create_item(item, index)]
            out_extra["position"] = index + 1
        elif action == "add_section":
            index = _position(at, count, insert=True)
            item = {"title": title.strip(), "pageBreakItem": {}}  # type: ignore[union-attr]
            if description:
                item["description"] = description
            requests = [_create_item(item, index)]
            out_extra["position"] = index + 1
        elif action == "update_question":
            index, live = _index_of(form, item_id)  # type: ignore[arg-type]
            requests = [_update_question_request(
                index, item_id, live, title, description, required, options,  # type: ignore[arg-type]
            )]
            touched.append(item_id)  # type: ignore[arg-type]
        elif action == "move_question":
            index, _live = _index_of(form, item_id)  # type: ignore[arg-type]
            new_index = _position(at, count, insert=False)
            requests = [{"moveItem": {
                "originalLocation": {"index": index}, "newLocation": {"index": new_index},
            }}]
            touched.append(item_id)  # type: ignore[arg-type]
            out_extra["position"] = new_index + 1
        else:  # delete_question
            index, live = _index_of(form, item_id)  # type: ignore[arg-type]
            requests = [{"deleteItem": {"location": {"index": index}}}]
            touched.append(item_id)  # type: ignore[arg-type]
            out_extra["deleted_title"] = live.get("title", "")

    result = _send(service, fid, requests, account)
    for reply in result.get("replies") or []:
        created_item = reply.get("createItem") or {}
        if created_item.get("itemId"):
            touched.append(created_item["itemId"])
            out_extra["item_id"] = created_item["itemId"]
            qids = created_item.get("questionId") or []
            if qids:
                out_extra["question_id"] = qids[0]
    logger.info("forms_manage %s: form=%s items=%s", action, fid, touched)
    return {
        **base, **out_extra, "touched_item_ids": touched,
        "applied": len(requests), "status": _STATUS[action],
    }
