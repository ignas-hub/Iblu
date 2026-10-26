"""Changing Google Slides — the write half of the Slides domain.

Until today IBLU could not touch a deck at all. Ignas asked for "as many
tools as possible from Google Workspace" after a fortnight of requesting
them one at a time, and Slides was the one domain still blocked — not on
design, on the Slides API switch in the Cloud project, which he flipped this
morning. The primary token's ``drive`` scope already covers presentations.
``slides_read`` is the read half; this module only writes.

ONE MCP tool with an ``action`` parameter, per HANDOFF.md §12 (one tool per
domain × side-effect class): every ``@mcp.tool`` costs Ignas a permission
click forever, and a write must never hide inside a read tool to dodge one.

Indices. Slide numbers are 1-based and are translated to objectIds from the
live presentation on every call; nothing is cached between calls, so a deck
reordered in the Slides UI between two calls is still edited in the right
place. ``title`` / ``body`` placeholders are resolved by placeholder type on
the live slide (TITLE or CENTERED_TITLE → title; BODY or SUBTITLE → body) and
refused, naming what the slide does have, when there is none. Every action
that changes text reports the ``object_id`` it changed.

What each action writes:

  create           presentations.create — a new deck; returns its id, url and
                   the first slide's objectId.
  add_slide        createSlide with a predefined ``layout`` (TITLE_AND_BODY,
                   TITLE_ONLY, BLANK, SECTION_HEADER, TITLE), at ``at`` or the
                   end; then, if ``title`` / ``body`` text is given, the new
                   slide is fetched and its placeholders filled by type. The
                   placeholders are read back rather than pre-assigned with
                   placeholderIdMappings because a mapping to a type the
                   theme's layout lacks is a 400, and themes vary.
  set_text         deleteText ALL + insertText on one shape — by ``object_id``
                   or by ``placeholder`` "title" / "body". Replaces the text
                   entirely.
  replace_text     replaceAllText across the deck; returns occurrencesChanged.
  add_text_box     createShape TEXT_BOX + insertText; ``x``/``y``/``width``/
                   ``height`` in points, default a centred box.
  add_image        createImage from a public url; ``width`` in points, aspect
                   ratio preserved (the API fits the image inside the box).
  duplicate_slide  duplicateObject — the copy lands right after the original.
  move_slide       updateSlidesPosition to a new 1-based position ``at``.
  delete_slide     deleteObject on one slide. Requires ``confirm=True``. This
                   is the ONE delete here, because a slide is not a file: the
                   deck stays in Drive, and Drive's version history keeps the
                   slide.
  speaker_notes    deleteText ALL + insertText on the slide's notes shape.
  rename           Drive files.update name.

**Never a presentation delete.** Deleting the file is Drive's trash
(``drive_manage(action='trash')``), recoverable for 30 days; nothing here
calls ``files.delete``, and nothing ever will.
"""

from __future__ import annotations

import logging
import uuid

from ..config import resolve_account, settings
from .drive import _drive, _file_id, _mock, _viewable_url, friendly_google_errors
from .slides_read import (
    BODY_PLACEHOLDERS,
    TITLE_PLACEHOLDERS,
    _Deck,
    _element_text,
    _fetch,
    _flatten,
    _placeholder_type,
    _slides,
)

logger = logging.getLogger("iblu_keeper.tools.slides_manage")

ACTIONS = (
    "create", "add_slide", "set_text", "replace_text", "add_text_box",
    "add_image", "duplicate_slide", "move_slide", "delete_slide",
    "speaker_notes", "rename",
)
LAYOUTS = ("TITLE_AND_BODY", "TITLE_ONLY", "BLANK", "SECTION_HEADER", "TITLE")
_PLACEHOLDERS = ("title", "body")
_SLIDES_MIME = "application/vnd.google-apps.presentation"

# Standard 16:9 page, used only when the API reports no page size.
_DEFAULT_PAGE_PT = (720.0, 405.0)

_NEEDS_SLIDE = (
    "set_text", "add_text_box", "add_image", "duplicate_slide", "move_slide",
    "delete_slide", "speaker_notes",
)

_MOCK_STATUS = {
    "create": "not_created_mock", "add_slide": "not_added_mock",
    "set_text": "not_set_mock", "replace_text": "not_replaced_mock",
    "add_text_box": "not_added_mock", "add_image": "not_added_mock",
    "duplicate_slide": "not_duplicated_mock", "move_slide": "not_moved_mock",
    "delete_slide": "not_deleted_mock", "speaker_notes": "not_set_mock",
    "rename": "not_renamed_mock",
}
_STATUS = {
    "create": "created", "add_slide": "added", "set_text": "set",
    "replace_text": "replaced", "add_text_box": "added", "add_image": "added",
    "duplicate_slide": "duplicated", "move_slide": "moved",
    "delete_slide": "deleted", "speaker_notes": "set", "rename": "renamed",
}


class SlidesManageError(ValueError):
    """Bad input to `slides_manage`. The message always names what was expected."""


def _new_id(prefix: str) -> str:
    """An objectId for an element this module creates — 5–50 chars of
    ``[a-zA-Z0-9_]``, as the API requires — so the result can name it."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _send(service, pres_id: str, requests: list[dict], account: str | None) -> dict:
    with friendly_google_errors(account, "this presentation"):
        return service.presentations().batchUpdate(
            presentationId=pres_id, body={"requests": requests},
        ).execute()


def _pt(value: float) -> dict:
    return {"magnitude": float(value), "unit": "PT"}


def _element_properties(page_id: str, x: float, y: float, w: float, h: float) -> dict:
    return {
        "pageObjectId": page_id,
        "size": {"width": _pt(w), "height": _pt(h)},
        "transform": {
            "scaleX": 1, "scaleY": 1,
            "translateX": float(x), "translateY": float(y), "unit": "PT",
        },
    }


# --------------------------------------------------------------------------- #
# Questions asked of the live slide
# --------------------------------------------------------------------------- #
def _shape_ids(page: dict) -> list[str]:
    return [
        f"{el.get('objectId', '')} ({_placeholder_type(el) or el['shape'].get('shapeType', 'shape')})"
        for el in _flatten(page.get("pageElements", []) or [])
        if "shape" in el
    ]


def _find_element(page: dict, object_id: str) -> dict | None:
    for el in _flatten(page.get("pageElements", []) or []):
        if el.get("objectId") == object_id:
            return el
    return None


def _placeholder_on(page: dict, which: str) -> tuple[dict | None, int]:
    """(first placeholder of that kind in page order, how many there were)."""
    wanted = TITLE_PLACEHOLDERS if which == "title" else BODY_PLACEHOLDERS
    found = [
        el for el in _flatten(page.get("pageElements", []) or [])
        if _placeholder_type(el) in wanted
    ]
    return (found[0] if found else None), len(found)


def _replace_text_requests(el: dict | None, object_id: str, text: str) -> list[dict]:
    """deleteText ALL only when there is text to delete (the API rejects it on
    an empty shape), then insertText at 0 when there is text to insert."""
    requests: list[dict] = []
    if el is not None and _element_text(el).strip():
        requests.append({"deleteText": {"objectId": object_id, "textRange": {"type": "ALL"}}})
    if text:
        requests.append({"insertText": {"objectId": object_id, "insertionIndex": 0, "text": text}})
    return requests


def _page_size(d: _Deck) -> tuple[float, float, bool]:
    w, h = d.page_size_pt()
    if w is None or h is None:
        return _DEFAULT_PAGE_PT[0], _DEFAULT_PAGE_PT[1], True
    return w, h, False


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def create(title: str, account: str | None) -> dict:
    service = _slides(account)
    with friendly_google_errors(account, "Google Slides"):
        pres = service.presentations().create(body={"title": title}).execute()
    pres_id = pres.get("presentationId")
    if not pres_id:
        raise RuntimeError(f"Slides create returned no presentationId (response={pres!r}).")
    slides = pres.get("slides", []) or []
    logger.info("slides_manage create: id=%s title=%r", pres_id, title)
    return {
        "id": pres_id, "action": "create", "title": pres.get("title", title),
        "first_slide_object_id": slides[0].get("objectId", "") if slides else None,
        "slide_count": len(slides),
        "url": _viewable_url(pres_id, _SLIDES_MIME), "status": "created",
    }


def add_slide(
    service, pres_id: str, layout: str, title: str | None, body: str | None,
    at: int | None, account: str | None,
) -> dict:
    d = _fetch(service, pres_id, account)
    if at is not None and not 1 <= at <= d.count + 1:
        raise SlidesManageError(f"at must be between 1 and {d.count + 1} (got {at})")
    insertion = (at - 1) if at is not None else d.count
    resp = _send(service, pres_id, [{"createSlide": {
        "insertionIndex": insertion,
        "slideLayoutReference": {"predefinedLayout": layout},
    }}], account)
    replies = resp.get("replies", []) or []
    new_id = ((replies[0] if replies else {}).get("createSlide") or {}).get("objectId", "")
    if not new_id:
        raise RuntimeError(f"createSlide returned no objectId (response={resp!r}).")

    filled: dict[str, str] = {}
    unfilled: list[str] = []
    applied = 1
    if title is not None or body is not None:
        with friendly_google_errors(account, "this presentation"):
            page = service.presentations().pages().get(
                presentationId=pres_id, pageObjectId=new_id,
            ).execute()
        requests: list[dict] = []
        for which, value in (("title", title), ("body", body)):
            if value is None:
                continue
            el, _n = _placeholder_on(page, which)
            if el is None:
                unfilled.append(which)
                continue
            requests.extend(_replace_text_requests(el, el["objectId"], value))
            filled[which] = el["objectId"]
        if requests:
            _send(service, pres_id, requests, account)
            applied += len(requests)
    out = {
        "object_id": new_id, "index": insertion + 1, "layout": layout,
        "filled": filled, "applied": applied,
    }
    if unfilled:
        out["unfilled"] = unfilled
        out["note"] = (
            f"the {layout} layout in this theme has no "
            f"{' or '.join(unfilled)} placeholder; add that text with "
            "action='add_text_box'"
        )
    return out


def set_text(
    service, pres_id: str, slide: int, object_id: str | None, placeholder: str | None,
    text: str, account: str | None,
) -> dict:
    d = _fetch(service, pres_id, account)
    s = d.slide_at(slide, SlidesManageError)
    note = None
    if object_id:
        el = _find_element(s, object_id)
        if el is None:
            raise SlidesManageError(
                f"no element {object_id!r} on slide {slide}; its shapes are: "
                f"{', '.join(_shape_ids(s)) or 'none'}"
            )
        if "shape" not in el:
            raise SlidesManageError(
                f"{object_id!r} on slide {slide} is not a text shape (a table cell "
                "or image); set_text only replaces a shape's text"
            )
    else:
        el, n = _placeholder_on(s, placeholder or "")
        if el is None:
            raise SlidesManageError(
                f"slide {slide} has no {placeholder} placeholder; its shapes are: "
                f"{', '.join(_shape_ids(s)) or 'none'}. Pass object_id instead, or "
                "add a text box."
            )
        object_id = el["objectId"]
        if n > 1:
            note = f"slide {slide} has {n} {placeholder} placeholders; the first was used"
    requests = _replace_text_requests(el, object_id, text)
    if requests:
        _send(service, pres_id, requests, account)
    out = {
        "slide": slide, "slide_object_id": s.get("objectId", ""),
        "object_id": object_id, "placeholder": placeholder,
        "applied": len(requests),
    }
    if note:
        out["note"] = note
    if not requests:
        out["note"] = "the shape was already empty and the new text is empty; nothing sent"
    return out


def replace_text(service, pres_id: str, find: str, replace_with: str, match_case: bool, account: str | None) -> dict:
    resp = _send(service, pres_id, [{"replaceAllText": {
        "containsText": {"text": find, "matchCase": bool(match_case)},
        "replaceText": replace_with,
    }}], account)
    occurrences = sum(
        (r.get("replaceAllText") or {}).get("occurrencesChanged", 0)
        for r in resp.get("replies", []) or []
    )
    return {
        "find": find, "replace_with": replace_with, "match_case": bool(match_case),
        "occurrences": occurrences, "applied": 1,
    }


def _box(
    d: _Deck, x: float | None, y: float | None, w: float | None, h: float | None,
    default_w_frac: float, default_h_frac: float | None,
) -> tuple[dict, bool]:
    page_w, page_h, assumed = _page_size(d)
    width = float(w) if w is not None else round(page_w * default_w_frac, 2)
    if h is not None:
        height = float(h)
    elif default_h_frac is None:
        height = width
    else:
        height = round(page_h * default_h_frac, 2)
    left = float(x) if x is not None else round((page_w - width) / 2, 2)
    top = float(y) if y is not None else round((page_h - height) / 2, 2)
    return {"x": left, "y": top, "width": width, "height": height}, assumed


def add_text_box(
    service, pres_id: str, slide: int, text: str,
    x: float | None, y: float | None, width: float | None, height: float | None,
    account: str | None,
) -> dict:
    d = _fetch(service, pres_id, account)
    s = d.slide_at(slide, SlidesManageError)
    box, assumed = _box(d, x, y, width, height, 0.6, 0.2)
    object_id = _new_id("textbox")
    requests = [
        {"createShape": {
            "objectId": object_id, "shapeType": "TEXT_BOX",
            "elementProperties": _element_properties(
                s["objectId"], box["x"], box["y"], box["width"], box["height"],
            ),
        }},
        {"insertText": {"objectId": object_id, "insertionIndex": 0, "text": text}},
    ]
    _send(service, pres_id, requests, account)
    out = {
        "slide": slide, "slide_object_id": s.get("objectId", ""),
        "object_id": object_id, "box_pt": box, "applied": len(requests),
    }
    if assumed:
        out["note"] = "the API reported no page size; a 720x405pt page was assumed"
    return out


def add_image(
    service, pres_id: str, slide: int, image_url: str,
    x: float | None, y: float | None, width: float | None, account: str | None,
) -> dict:
    d = _fetch(service, pres_id, account)
    s = d.slide_at(slide, SlidesManageError)
    # A square box of the requested width: the API scales the image to fit
    # inside it and centres it, so the aspect ratio survives.
    box, assumed = _box(d, x, y, width, None, 0.5, None)
    object_id = _new_id("image")
    requests = [{"createImage": {
        "objectId": object_id, "url": image_url,
        "elementProperties": _element_properties(
            s["objectId"], box["x"], box["y"], box["width"], box["height"],
        ),
    }}]
    _send(service, pres_id, requests, account)
    out = {
        "slide": slide, "slide_object_id": s.get("objectId", ""),
        "object_id": object_id, "image_url": image_url, "box_pt": box,
        "applied": 1,
        "fit": "scaled to fit inside the box, aspect ratio preserved",
    }
    if assumed:
        out["note"] = "the API reported no page size; a 720x405pt page was assumed"
    return out


def duplicate_slide(service, pres_id: str, slide: int, account: str | None) -> dict:
    d = _fetch(service, pres_id, account)
    s = d.slide_at(slide, SlidesManageError)
    resp = _send(service, pres_id, [{"duplicateObject": {"objectId": s["objectId"]}}], account)
    replies = resp.get("replies", []) or []
    new_id = ((replies[0] if replies else {}).get("duplicateObject") or {}).get("objectId", "")
    return {
        "slide": slide, "source_object_id": s.get("objectId", ""),
        "object_id": new_id, "index": slide + 1, "slide_count": d.count + 1,
        "applied": 1,
    }


def move_slide(service, pres_id: str, slide: int, at: int, account: str | None) -> dict:
    d = _fetch(service, pres_id, account)
    s = d.slide_at(slide, SlidesManageError)
    if not 1 <= at <= d.count:
        raise SlidesManageError(f"at must be between 1 and {d.count} (got {at})")
    current, target = slide - 1, at - 1
    base = {"slide": slide, "object_id": s.get("objectId", ""), "index": at}
    if current == target:
        return {**base, "applied": 0, "note": f"slide {slide} is already at position {at}"}
    # insertionIndex is counted on the arrangement BEFORE the move, so a
    # slide moving later is inserted one past where it will end up.
    insertion = target + 1 if target > current else target
    _send(service, pres_id, [{"updateSlidesPosition": {
        "slideObjectIds": [s["objectId"]], "insertionIndex": insertion,
    }}], account)
    return {**base, "applied": 1}


def delete_slide(service, pres_id: str, slide: int, account: str | None) -> dict:
    d = _fetch(service, pres_id, account)
    s = d.slide_at(slide, SlidesManageError)
    if d.count == 1:
        raise SlidesManageError(
            "delete_slide refuses to delete the only slide: a presentation must "
            "keep at least one. To remove the deck itself use "
            "drive_manage(action='trash')."
        )
    _send(service, pres_id, [{"deleteObject": {"objectId": s["objectId"]}}], account)
    logger.info("slides_manage delete_slide: %s slide %d (%s)", pres_id, slide, s.get("objectId"))
    return {
        "slide": slide, "object_id": s.get("objectId", ""),
        "slide_count": d.count - 1, "applied": 1,
        "undo": "File > Version history in the Slides UI restores the deck as it was",
    }


def speaker_notes(service, pres_id: str, slide: int, text: str, account: str | None) -> dict:
    d = _fetch(service, pres_id, account)
    s = d.slide_at(slide, SlidesManageError)
    nid, el = d.notes_shape(s)
    if not nid:
        raise SlidesManageError(
            f"slide {slide} reports no speaker-notes shape id; the API cannot "
            "place notes on it"
        )
    requests = _replace_text_requests(el, nid, text)
    if requests:
        _send(service, pres_id, requests, account)
    out = {
        "slide": slide, "slide_object_id": s.get("objectId", ""),
        "object_id": nid, "applied": len(requests),
    }
    if not requests:
        out["note"] = "the notes were already empty and the new text is empty; nothing sent"
    return out


def rename(pres_id: str, title: str, account: str | None) -> dict:
    drive = _drive(account)
    with friendly_google_errors(account, "this presentation"):
        res = drive.files().update(
            fileId=pres_id, body={"name": title}, fields="id,name,mimeType",
            supportsAllDrives=True,
        ).execute()
    return {"title": res.get("name", title), "applied": 1}


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
def slides_manage(
    action: str,
    presentation_id_or_url: str | None = None,
    slide: int | None = None,
    title: str | None = None,
    body: str | None = None,
    text: str | None = None,
    layout: str = "TITLE_AND_BODY",
    at: int | None = None,
    object_id: str | None = None,
    placeholder: str | None = None,
    find: str | None = None,
    replace_with: str | None = None,
    match_case: bool = False,
    image_url: str | None = None,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    confirm: bool = False,
    account: str | None = None,
) -> dict:
    """Change a Google Slides deck. One tool, eleven verbs — see the module docstring.

    ``slide`` is a 1-based slide number (set_text, add_text_box, add_image,
    duplicate_slide, move_slide, delete_slide, speaker_notes). ``title`` is
    the deck title for create and rename, and the title placeholder's text
    for add_slide; ``body`` is add_slide's body text. ``text`` is the new text
    for set_text, add_text_box and speaker_notes. ``at`` is a 1-based position
    for add_slide and move_slide. ``delete_slide`` needs ``confirm=True``.
    There is no presentation delete — that is ``drive_manage`` trash.
    ``account`` — which Drive: ``blt`` (default), ``deadlift``, ``choco``.
    """
    if action not in ACTIONS:
        raise SlidesManageError(
            f"unknown action {action!r} — expected one of: {', '.join(ACTIONS)}"
        )
    if action != "create" and not (presentation_id_or_url and str(presentation_id_or_url).strip()):
        raise SlidesManageError(f"{action} needs presentation_id_or_url")

    # Refusals that need no presentation come before mock mode, so a refusal
    # never depends on which mode the server is in.
    if action in _NEEDS_SLIDE:
        if slide is None:
            raise SlidesManageError(f"{action} needs slide (a 1-based slide number)")
        try:
            slide = int(slide)
        except (TypeError, ValueError):
            raise SlidesManageError(f"slide must be an integer (got {slide!r})")
        if slide < 1:
            raise SlidesManageError(f"slide must be 1 or more (got {slide})")
    if at is not None:
        try:
            at = int(at)
        except (TypeError, ValueError):
            raise SlidesManageError(f"at must be an integer (got {at!r})")
        if at < 1:
            raise SlidesManageError(f"at must be 1 or more (got {at})")
    if action in ("create", "rename") and not (title and title.strip()):
        raise SlidesManageError(f"{action} needs title")
    if action == "add_slide":
        layout = (layout or "").strip().upper()
        if layout not in LAYOUTS:
            raise SlidesManageError(
                f"unknown layout {layout!r} — expected one of: {', '.join(LAYOUTS)}"
            )
        if layout == "BLANK" and (title is not None or body is not None):
            raise SlidesManageError(
                "a BLANK slide has no placeholders to put title or body text in; "
                "add it, then use action='add_text_box'"
            )
    if action == "set_text":
        if text is None:
            raise SlidesManageError("set_text needs text (use '' to clear)")
        if bool(object_id) == bool(placeholder):
            raise SlidesManageError("set_text needs exactly one of object_id or placeholder")
        if placeholder is not None:
            placeholder = placeholder.strip().lower()
            if placeholder not in _PLACEHOLDERS:
                raise SlidesManageError(
                    f"placeholder must be one of {', '.join(_PLACEHOLDERS)} (got {placeholder!r})"
                )
    if action == "replace_text":
        if not find:
            raise SlidesManageError("replace_text needs find")
        if replace_with is None:
            raise SlidesManageError("replace_text needs replace_with (use '' to delete the text)")
    if action == "add_text_box" and not (text and text.strip()):
        raise SlidesManageError("add_text_box needs text")
    if action == "speaker_notes" and text is None:
        raise SlidesManageError("speaker_notes needs text (use '' to clear)")
    if action == "add_image" and not (
        image_url and image_url.lower().startswith(("http://", "https://"))
    ):
        raise SlidesManageError("add_image needs a public http(s) image_url")
    for name, value in (("width", width), ("height", height)):
        if value is None:
            continue
        try:
            ok = float(value) > 0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise SlidesManageError(f"{name} must be a positive number of points (got {value!r})")
    if action == "move_slide" and at is None:
        raise SlidesManageError("move_slide needs at (the new 1-based position)")
    if action == "delete_slide" and confirm is not True:
        raise SlidesManageError(
            f"delete_slide removes slide {slide} from the deck. It is the one "
            "delete this tool allows, because the deck stays in Drive and its "
            "version history keeps the slide — but it still needs confirm=True."
        )

    account = resolve_account(account)
    if settings.use_mock:
        return _mock({
            "action": action, "id": presentation_id_or_url, "slide": slide,
            "object_id": object_id, "status": _MOCK_STATUS[action],
        })

    if action == "create":
        return create(title.strip(), account)  # type: ignore[union-attr]

    pres_id = _file_id(presentation_id_or_url)  # type: ignore[arg-type]
    base = {"id": pres_id, "action": action, "url": _viewable_url(pres_id, _SLIDES_MIME)}
    if action == "rename":
        return {**base, **rename(pres_id, title.strip(), account), "status": _STATUS[action]}  # type: ignore[union-attr]

    service = _slides(account)
    if action == "add_slide":
        out = add_slide(service, pres_id, layout, title, body, at, account)
    elif action == "set_text":
        out = set_text(service, pres_id, slide, object_id, placeholder, text, account)  # type: ignore[arg-type]
    elif action == "replace_text":
        out = replace_text(service, pres_id, find, replace_with, match_case, account)  # type: ignore[arg-type]
    elif action == "add_text_box":
        out = add_text_box(service, pres_id, slide, text, x, y, width, height, account)  # type: ignore[arg-type]
    elif action == "add_image":
        out = add_image(service, pres_id, slide, image_url, x, y, width, account)  # type: ignore[arg-type]
    elif action == "duplicate_slide":
        out = duplicate_slide(service, pres_id, slide, account)  # type: ignore[arg-type]
    elif action == "move_slide":
        out = move_slide(service, pres_id, slide, at, account)  # type: ignore[arg-type]
    elif action == "delete_slide":
        out = delete_slide(service, pres_id, slide, account)  # type: ignore[arg-type]
    else:  # speaker_notes
        out = speaker_notes(service, pres_id, slide, text, account)  # type: ignore[arg-type]
    return {**base, **out, "status": _STATUS[action]}
