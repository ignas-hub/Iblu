"""Everyday Google Docs edits, without hand-building ``batchUpdate`` requests.

``gdoc_batch_update`` (``tools.docs_edit``) can do anything the Docs API can,
and that is exactly its problem for the common case: "add a heading", "make
this a bullet list", "link that phrase", "bold this" each mean composing a
raw request object, with a nested ``range``/``location`` and a ``fields``
mask, and getting one key wrong costs a round trip. Ignas said it: "It's
crazy how many times I have to ask to add tools." This module is the batch
that ends that for Docs — the ten edits he actually asks for, each a verb.

ONE MCP tool with an ``action`` parameter, per HANDOFF.md §12 (one tool per
domain × side-effect class): a new ``@mcp.tool`` costs a permission click
forever, so the verbs share an entry point. ``find`` and ``outline`` are
read-only and live here too, because they exist to answer "where would this
edit land?" — they are views of the same document the writes act on.

Indices. A model cannot count UTF-16 code units in a document it has not
seen, so every action fetches the live document and computes its indices
from that fetch, every call. Nothing here caches an index between calls;
a document edited in the Docs UI between two calls is still edited in the
right place. Anchors (``anchor_text``) use the FIRST occurrence; the result
reports ``occurrences`` so the caller knows when the anchor was ambiguous.

What each action writes:

  heading       insertText of a new paragraph + updateParagraphStyle
                HEADING_<level>, at the end or after ``anchor_text``'s paragraph.
  bullets       insertText of one paragraph per item + updateParagraphStyle
                NORMAL_TEXT + createParagraphBullets (disc, or numbered).
  link          updateTextStyle link on the anchor text.
  style         updateTextStyle bold / italic / underline, on or off.
  page_break    insertPageBreak at the end or after ``anchor_text``'s paragraph.
  image         insertInlineImage from a public URL, at the end or right after
                ``anchor_text``; ``width_pt`` scales it, aspect preserved.
  insert_after  insertText immediately after ``anchor_text`` (inline; start
                ``text`` with a newline to begin a new paragraph).
  delete_text   deleteContentRange of the first occurrence of ``text``;
                refused above 2,000 characters.
  find          (read-only) every occurrence of ``text`` with indices + context.
  outline       (read-only) the headings, with levels.

Known Docs behaviour worth knowing: a paragraph inserted next to a bulleted
one inherits the bullet. ``heading`` does not strip it; use
``gdoc_batch_update`` with ``deleteParagraphBullets`` if that happens.
"""

from __future__ import annotations

import logging

from ..config import resolve_account, settings
from . import docs_edit
from .drive import _docs, _file_id, _mock, _viewable_url, friendly_google_errors

logger = logging.getLogger("iblu_keeper.tools.gdoc_edit")

ACTIONS = (
    "heading", "bullets", "link", "style", "page_break", "image",
    "insert_after", "delete_text", "find", "outline",
)
READ_ONLY = ("find", "outline")

MAX_DELETE_CHARS = 2000
MAX_FIND = 50
CONTEXT_CHARS = 60
_STYLES = ("bold", "italic", "underline")
_DOC_MIME = "application/vnd.google-apps.document"

_MOCK_STATUS = {
    "heading": "not_inserted_mock", "bullets": "not_inserted_mock",
    "link": "not_linked_mock", "style": "not_styled_mock",
    "page_break": "not_inserted_mock", "image": "not_inserted_mock",
    "insert_after": "not_inserted_mock", "delete_text": "not_deleted_mock",
    "find": "not_searched_mock", "outline": "not_read_mock",
}
_STATUS = {
    "heading": "inserted", "bullets": "inserted", "link": "linked",
    "style": "styled", "page_break": "inserted", "image": "inserted",
    "insert_after": "inserted", "delete_text": "deleted",
    "find": "found", "outline": "read",
}


class GdocEditError(ValueError):
    """Bad input to `gdoc_edit`. The message always names what was expected."""


def _u16(s: str) -> int:
    """Length in UTF-16 code units — the unit the Docs API counts indices in."""
    return len(s.encode("utf-16-le")) // 2


# --------------------------------------------------------------------------- #
# One fetch of the live document, and the questions the actions ask of it
# --------------------------------------------------------------------------- #
class _Doc:
    def __init__(self, doc: dict):
        self.doc = doc
        self.segments = docs_edit._build_segments(doc)
        self.prefix = docs_edit._prefix_offsets(self.segments)
        self.full_text = "".join(s["text"] for s in self.segments)
        body = doc.get("body", {}).get("content", []) or []
        self.body_end = max((el.get("endIndex", 1) for el in body), default=1)

    def locate(self, anchor: str) -> tuple[int, int, int, int, int]:
        """First occurrence of ``anchor``: (doc_start, doc_end, occurrences,
        full-text start, full-text end). Raises, naming the text, if absent."""
        if not anchor:
            raise GdocEditError("anchor_text must be a non-empty string")
        matches = docs_edit._find_matches(self.full_text, anchor)
        if not matches:
            raise GdocEditError(f"anchor text not found in document: {anchor!r}")
        foff_start, foff_end = matches[0]
        start = docs_edit._map_offset(self.segments, self.prefix, foff_start, end=False)
        end = docs_edit._map_offset(self.segments, self.prefix, foff_end, end=True)
        return start, end, len(matches), foff_start, foff_end

    def paragraph_end(self, index: int) -> int:
        """``endIndex`` of the paragraph (body or table cell) holding ``index``."""
        found: list[int] = []

        def walk(elements: list[dict]) -> None:
            for el in elements:
                if "paragraph" in el:
                    s, e = el.get("startIndex"), el.get("endIndex")
                    if s is not None and e is not None and s <= index < e:
                        found.append(e)
                    continue
                if "table" in el:
                    for row in el["table"].get("tableRows", []):
                        for cell in row.get("tableCells", []):
                            walk(cell.get("content", []))
                    continue
                if "tableOfContents" in el:
                    walk(el["tableOfContents"].get("content", []))

        walk(self.doc.get("body", {}).get("content", []) or [])
        if not found:
            raise GdocEditError("could not find the paragraph containing the anchor text")
        return found[0]

    def block_target(self, anchor: str | None) -> dict:
        """Where a NEW PARAGRAPH goes.

        At the end of the document the only legal place is just before the
        body's final newline, so the block is inserted with a leading newline
        and the new paragraph starts one index later. After an anchor it goes
        at the end of the anchor's paragraph with a trailing newline — unless
        that paragraph is the document's last, whose end index Google rejects
        as a location (found live by ``gdoc_insert_table``); then it is the
        end-of-document case again.
        """
        occurrences = 0
        if anchor:
            start, _end, occurrences, _fs, _fe = self.locate(anchor)
            para_end = self.paragraph_end(start)
            if para_end < self.body_end:
                return {
                    "index": para_end, "lead": "", "trail": "\n",
                    "paragraph_start": para_end, "occurrences": occurrences,
                    "placement": "after_anchor",
                }
        index = self.body_end - 1
        lead = "\n" if self.body_end > 2 else ""
        return {
            "index": index, "lead": lead, "trail": "",
            "paragraph_start": index + _u16(lead), "occurrences": occurrences,
            "placement": "end",
        }

    def inline_target(self, anchor: str | None) -> dict:
        """Where an INLINE element goes: a ``location`` right after the anchor,
        or ``endOfSegmentLocation`` when there is none (or the anchor's
        paragraph is the last, whose end the API rejects as a location)."""
        if not anchor:
            return {"location": {"endOfSegmentLocation": {}}, "occurrences": 0}
        start, _end, occurrences, _fs, _fe = self.locate(anchor)
        para_end = self.paragraph_end(start)
        if para_end >= self.body_end:
            return {"location": {"endOfSegmentLocation": {}}, "occurrences": occurrences}
        return {"location": {"location": {"index": para_end}}, "occurrences": occurrences}


def _fetch(docs, doc_id: str, account: str | None) -> _Doc:
    with friendly_google_errors(account, "this Google Doc"):
        raw = docs.documents().get(documentId=doc_id).execute()
    return _Doc(raw)


def _send(docs, doc_id: str, requests: list[dict], account: str | None) -> dict:
    with friendly_google_errors(account, "this Google Doc"):
        return docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests},
        ).execute()


# --------------------------------------------------------------------------- #
# Request builders — pure, each takes the fetched document
# --------------------------------------------------------------------------- #
def _heading_requests(d: _Doc, text: str, level: int, anchor: str | None) -> tuple[list[dict], dict]:
    t = d.block_target(anchor)
    payload = t["lead"] + text + t["trail"]
    start = t["paragraph_start"]
    return [
        {"insertText": {"location": {"index": t["index"]}, "text": payload}},
        {"updateParagraphStyle": {
            "range": {"startIndex": start, "endIndex": start + _u16(text)},
            "paragraphStyle": {"namedStyleType": f"HEADING_{level}"},
            "fields": "namedStyleType",
        }},
    ], t


def _bullets_requests(d: _Doc, items: list[str], numbered: bool, anchor: str | None) -> tuple[list[dict], dict]:
    t = d.block_target(anchor)
    block = "\n".join(items)
    payload = t["lead"] + block + t["trail"]
    start = t["paragraph_start"]
    rng = {"startIndex": start, "endIndex": start + _u16(block)}
    preset = "NUMBERED_DECIMAL_ALPHA_ROMAN" if numbered else "BULLET_DISC_CIRCLE_SQUARE"
    return [
        {"insertText": {"location": {"index": t["index"]}, "text": payload}},
        # New paragraphs copy the style of their neighbour; a list under a
        # heading would otherwise be a list OF headings.
        {"updateParagraphStyle": {
            "range": rng, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "fields": "namedStyleType",
        }},
        {"createParagraphBullets": {"range": rng, "bulletPreset": preset}},
    ], t


def _range_of(d: _Doc, anchor: str) -> tuple[dict, int]:
    start, end, occurrences, _fs, _fe = d.locate(anchor)
    return {"startIndex": start, "endIndex": end}, occurrences


def _parse_styles(style) -> list[str]:
    if isinstance(style, str):
        names = [s.strip().lower() for s in style.split(",")]
    elif isinstance(style, (list, tuple)):
        names = [str(s).strip().lower() for s in style]
    else:
        names = []
    names = [n for n in names if n]
    bad = [n for n in names if n not in _STYLES]
    if not names or bad:
        raise GdocEditError(
            f"style must be one or more of {', '.join(_STYLES)} (got {style!r})"
        )
    return names


def find(d: _Doc, text: str) -> dict:
    matches = docs_edit._find_matches(d.full_text, text)
    items = []
    for foff_start, foff_end in matches[:MAX_FIND]:
        before = d.full_text[max(0, foff_start - CONTEXT_CHARS):foff_start]
        after = d.full_text[foff_end:foff_end + CONTEXT_CHARS]
        items.append({
            "start_index": docs_edit._map_offset(d.segments, d.prefix, foff_start, end=False),
            "end_index": docs_edit._map_offset(d.segments, d.prefix, foff_end, end=True),
            "context": (before + "«" + text + "»" + after).replace("\n", "⏎"),
        })
    return {"text": text, "occurrences": len(matches), "items": items}


def outline(d: _Doc) -> list[dict]:
    """Headings in reading order: TITLE/SUBTITLE are level 0, HEADING_n is n."""
    out: list[dict] = []

    def walk(elements: list[dict]) -> None:
        for el in elements:
            para = el.get("paragraph")
            if para is not None:
                named = (para.get("paragraphStyle") or {}).get("namedStyleType") or ""
                if named in ("TITLE", "SUBTITLE") or named.startswith("HEADING_"):
                    text = "".join(
                        pel["textRun"].get("content", "")
                        for pel in para.get("elements", [])
                        if pel.get("textRun") is not None
                    ).strip()
                    level = 0 if named in ("TITLE", "SUBTITLE") else int(named.rsplit("_", 1)[1])
                    out.append({
                        "level": level, "style": named, "text": text,
                        "start_index": el.get("startIndex"), "end_index": el.get("endIndex"),
                    })
                continue
            if "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []))
                continue
            if "tableOfContents" in el:
                walk(el["tableOfContents"].get("content", []))

    walk(d.doc.get("body", {}).get("content", []) or [])
    return out


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
def gdoc_edit(
    action: str,
    doc_id_or_url: str,
    text: str | None = None,
    anchor_text: str | None = None,
    level: int = 1,
    items: list[str] | None = None,
    numbered: bool = False,
    url: str | None = None,
    style: str | None = None,
    on: bool = True,
    image_url: str | None = None,
    width_pt: float | None = None,
    account: str | None = None,
) -> dict:
    """Edit a Google Doc by verb — see the module docstring for each action.

    ``anchor_text`` is optional for heading, bullets, page_break and image
    (omit it to act at the end of the document) and required for link,
    style and insert_after. ``text`` is the heading / inserted / deleted /
    searched text. ``account`` — which Drive: ``blt`` (default),
    ``deadlift``, ``choco``.
    """
    if action not in ACTIONS:
        raise GdocEditError(
            f"unknown action {action!r} — expected one of: {', '.join(ACTIONS)}"
        )
    if not doc_id_or_url or not str(doc_id_or_url).strip():
        raise GdocEditError(f"{action} needs doc_id_or_url")

    # Input checks that need no document come first, so a refusal never
    # depends on whether the server is in mock mode.
    if action in ("heading", "insert_after", "delete_text", "find") and not text:
        raise GdocEditError(f"{action} needs text")
    if action in ("link", "style", "insert_after") and not anchor_text:
        raise GdocEditError(f"{action} needs anchor_text")
    if action == "heading":
        try:
            level = int(level)
        except (TypeError, ValueError):
            raise GdocEditError("heading level must be an integer 1-6")
        if not 1 <= level <= 6:
            raise GdocEditError(f"heading level must be 1-6 (got {level})")
    if action == "bullets":
        items = [str(i) for i in (items or []) if str(i).strip()]
        if not items:
            raise GdocEditError("bullets needs a non-empty items list")
    if action == "link" and not (url and url.strip()):
        raise GdocEditError("link needs url")
    styles: list[str] = _parse_styles(style) if action == "style" else []
    if action == "image":
        if not image_url or not image_url.lower().startswith(("http://", "https://")):
            raise GdocEditError("image needs a public http(s) image_url")
        if width_pt is not None and float(width_pt) <= 0:
            raise GdocEditError("width_pt must be positive")
    if action == "delete_text" and _u16(text or "") > MAX_DELETE_CHARS:
        raise GdocEditError(
            f"delete_text refuses to delete {_u16(text or '')} characters in one "
            f"call (limit {MAX_DELETE_CHARS}). Delete in smaller pieces, each "
            "one you have read."
        )

    account = resolve_account(account)
    if settings.use_mock:
        return _mock({
            "action": action, "doc_id": doc_id_or_url, "applied": 0,
            "status": _MOCK_STATUS[action],
        })

    doc_id = _file_id(doc_id_or_url)
    docs = _docs(account)
    d = _fetch(docs, doc_id, account)
    base = {
        "doc_id": doc_id, "action": action,
        "url": _viewable_url(doc_id, _DOC_MIME),
    }

    if action == "find":
        return {**base, **find(d, text or ""), "status": _STATUS[action]}
    if action == "outline":
        heads = outline(d)
        return {**base, "headings": heads, "count": len(heads), "status": _STATUS[action]}

    occurrences = 0
    placement = None
    requests: list[dict]
    if action == "heading":
        requests, t = _heading_requests(d, text or "", level, anchor_text)
        occurrences, placement = t["occurrences"], t["placement"]
    elif action == "bullets":
        requests, t = _bullets_requests(d, items or [], numbered, anchor_text)
        occurrences, placement = t["occurrences"], t["placement"]
    elif action == "link":
        rng, occurrences = _range_of(d, anchor_text or "")
        requests = [{"updateTextStyle": {
            "range": rng, "textStyle": {"link": {"url": url.strip()}},  # type: ignore[union-attr]
            "fields": "link",
        }}]
    elif action == "style":
        rng, occurrences = _range_of(d, anchor_text or "")
        requests = [{"updateTextStyle": {
            "range": rng,
            "textStyle": {name: bool(on) for name in styles},
            "fields": ",".join(styles),
        }}]
    elif action == "page_break":
        t = d.inline_target(anchor_text)
        occurrences = t["occurrences"]
        placement = "end" if "endOfSegmentLocation" in t["location"] else "after_anchor"
        requests = [{"insertPageBreak": dict(t["location"])}]
    elif action == "image":
        if anchor_text:
            _s, end, occurrences, _fs, _fe = d.locate(anchor_text)
            location: dict = {"location": {"index": end}}
            placement = "after_anchor"
        else:
            location = {"endOfSegmentLocation": {}}
            placement = "end"
        req: dict = {"uri": image_url, **location}
        if width_pt is not None:
            req["objectSize"] = {"width": {"magnitude": float(width_pt), "unit": "PT"}}
        requests = [{"insertInlineImage": req}]
    elif action == "insert_after":
        _s, end, occurrences, _fs, _fe = d.locate(anchor_text or "")
        requests = [{"insertText": {"location": {"index": end}, "text": text}}]
    else:  # delete_text
        start, end, occurrences, _fs, _fe = d.locate(text or "")
        # The body's final newline cannot be deleted; a match that reaches it
        # is trimmed to stop just before it.
        end = min(end, d.body_end - 1)
        if end <= start:
            raise GdocEditError(
                "delete_text would delete only the document's final newline, "
                "which the Docs API does not allow"
            )
        requests = [{"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}}]

    _send(docs, doc_id, requests, account)
    out = {**base, "applied": len(requests), "status": _STATUS[action]}
    if anchor_text or action == "delete_text":
        out["anchor_text"] = text if action == "delete_text" else anchor_text
        out["occurrences"] = occurrences
        if occurrences > 1:
            out["note"] = (
                f"anchor matched {occurrences} times; the first occurrence was "
                "used. Use gdoc_batch_update with an occurrence for another."
            )
    if placement:
        out["placement"] = placement
    return out
