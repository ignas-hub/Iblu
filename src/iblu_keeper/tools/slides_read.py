"""Reading Google Slides — everything that looks, nothing that touches.

Slides was the one Workspace surface IBLU could not see into at all: a deck
could be found in Drive and exported as flat text, and that was it. Which
slide says what, what is on slide 7, whether the speaker notes were ever
written — each of those was Ignas opening the deck himself. He asked for "as
many tools as possible from Google Workspace" after a fortnight of requesting
them one by one, and Slides was blocked only on the API switch in the Cloud
project, which he flipped today. This module is the read half of what that
switch unlocked; ``slides_manage`` is the write half.

It is ONE MCP tool with an ``action`` parameter, not four, because of the rule
in HANDOFF.md §12: one tool per domain × side-effect class. Every new
``@mcp.tool`` costs Ignas a permission click forever, and a changed schema
costs it again. Nothing in this module writes — it only calls
``presentations.get`` and Drive ``files.get`` — so it belongs in the
read-only group he can allow once.

Actions (all read-only):

  outline   Every slide: 1-based index, objectId, layout name, title text,
            the first ~120 characters of body text, whether notes exist.
  slide     One slide by 1-based index: every text element with its objectId
            and full text, images with their source url, tables as rows,
            and the speaker notes.
  text      The whole deck as plain text with ``--- slide N ---`` markers,
            20,000 characters per call, paged with ``offset`` exactly like
            the ``repo`` tool.
  info      Title, slide count, page size, url, and last-modified from Drive.

Indices. Slide numbers here are 1-based and are translated to objectIds from
the live presentation on every call — nothing is cached between calls, so a
deck reordered in the Slides UI is still read in the right place. The
objectIds in every result are the ones ``slides_manage`` takes.
"""

from __future__ import annotations

import logging

from ..config import resolve_account, settings
from .drive import _drive, _file_id, _mock, _viewable_url, friendly_google_errors
from .repo import MAX_CHARS, window

logger = logging.getLogger("iblu_keeper.tools.slides_read")

ACTIONS = ("outline", "slide", "text", "info")

_SLIDES_MIME = "application/vnd.google-apps.presentation"
_EMU_PER_PT = 12700
OUTLINE_BODY_CHARS = 120

# Placeholder types, as the Slides API names them, grouped by what Ignas
# calls them. Used by ``slides_manage`` to resolve "title" / "body".
TITLE_PLACEHOLDERS = ("TITLE", "CENTERED_TITLE")
BODY_PLACEHOLDERS = ("BODY", "SUBTITLE")


class SlidesReadError(ValueError):
    """Bad input to `slides_read`. The message always names what was expected."""


def _slides(account: str | None = None):
    from ..google_auth import build_service

    return build_service("slides", "v1", account=account)


# --------------------------------------------------------------------------- #
# Text extraction — pure functions over Slides API page elements
# --------------------------------------------------------------------------- #
def _text_of(text_obj: dict | None) -> str:
    """The plain text of a ``TextContent`` (shape text or a table cell)."""
    if not text_obj:
        return ""
    parts: list[str] = []
    for te in text_obj.get("textElements", []) or []:
        run = te.get("textRun")
        if run is not None:
            parts.append(run.get("content", "") or "")
            continue
        auto = te.get("autoText")
        if auto is not None:
            parts.append(auto.get("content", "") or "")
    return "".join(parts)


def _placeholder_type(el: dict) -> str | None:
    shape = el.get("shape") or {}
    ph = shape.get("placeholder")
    return ph.get("type") if ph else None


def _table_rows(el: dict) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in (el.get("table") or {}).get("tableRows", []) or []:
        rows.append([
            _text_of(cell.get("text")).rstrip("\n")
            for cell in row.get("tableCells", []) or []
        ])
    return rows


def _flatten(elements: list[dict]) -> list[dict]:
    """Page elements in reading order, with groups opened up."""
    out: list[dict] = []
    for el in elements or []:
        group = el.get("elementGroup")
        if group is not None:
            out.extend(_flatten(group.get("children", []) or []))
        else:
            out.append(el)
    return out


def _element_text(el: dict) -> str:
    """All the text an element carries, newline-joined, no trailing newline."""
    if "shape" in el:
        return _text_of(el["shape"].get("text")).rstrip("\n")
    if "table" in el:
        return "\n".join("\t".join(r) for r in _table_rows(el))
    if "wordArt" in el:
        return (el["wordArt"].get("renderedText") or "").rstrip("\n")
    return ""


# --------------------------------------------------------------------------- #
# One fetch of the live presentation, and the questions asked of it
# --------------------------------------------------------------------------- #
class _Deck:
    def __init__(self, pres: dict):
        self.pres = pres
        self.id = pres.get("presentationId", "")
        self.title = pres.get("title", "")
        self.slides: list[dict] = pres.get("slides", []) or []
        self.layout_names: dict[str, str] = {}
        for layout in pres.get("layouts", []) or []:
            props = layout.get("layoutProperties") or {}
            self.layout_names[layout.get("objectId", "")] = (
                props.get("displayName") or props.get("name") or ""
            )

    @property
    def count(self) -> int:
        return len(self.slides)

    def page_size_pt(self) -> tuple[float | None, float | None]:
        size = self.pres.get("pageSize") or {}

        def pt(dim: dict | None) -> float | None:
            if not dim or dim.get("magnitude") is None:
                return None
            mag = float(dim["magnitude"])
            return round(mag / _EMU_PER_PT, 2) if dim.get("unit", "EMU") == "EMU" else mag

        return pt(size.get("width")), pt(size.get("height"))

    def slide_at(self, index: int, error: type[ValueError] = SlidesReadError) -> dict:
        """The slide at a 1-based index, from THIS fetch. Raises naming the range."""
        try:
            i = int(index)
        except (TypeError, ValueError):
            raise error(f"slide must be an integer 1-{self.count} (got {index!r})")
        if self.count == 0:
            raise error("this presentation has no slides")
        if not 1 <= i <= self.count:
            raise error(f"slide must be between 1 and {self.count} (got {i})")
        return self.slides[i - 1]

    def layout_of(self, slide: dict) -> str:
        lid = (slide.get("slideProperties") or {}).get("layoutObjectId", "")
        return self.layout_names.get(lid, "") or lid or ""

    @staticmethod
    def notes_shape(slide: dict) -> tuple[str | None, dict | None]:
        """(speakerNotesObjectId, the shape element if it exists yet).

        The API says the shape "may not always exist on the notes page";
        inserting text with that objectId creates it.
        """
        notes = (slide.get("slideProperties") or {}).get("notesPage") or {}
        nid = (notes.get("notesProperties") or {}).get("speakerNotesObjectId")
        if not nid:
            return None, None
        for el in _flatten(notes.get("pageElements", []) or []):
            if el.get("objectId") == nid:
                return nid, el
        return nid, None

    def notes_text(self, slide: dict) -> str:
        _nid, el = self.notes_shape(slide)
        return _element_text(el) if el else ""

    @staticmethod
    def title_text(slide: dict) -> str | None:
        for el in _flatten(slide.get("pageElements", []) or []):
            if _placeholder_type(el) in TITLE_PLACEHOLDERS:
                return _element_text(el).strip()
        return None

    def body_text(self, slide: dict) -> str:
        parts = [
            _element_text(el)
            for el in _flatten(slide.get("pageElements", []) or [])
            if _placeholder_type(el) not in TITLE_PLACEHOLDERS
        ]
        return "\n".join(p for p in parts if p.strip())

    def slide_text(self, slide: dict) -> str:
        """Everything on the slide, title first, then the notes if any."""
        parts = [
            _element_text(el)
            for el in _flatten(slide.get("pageElements", []) or [])
        ]
        text = "\n".join(p for p in parts if p.strip())
        notes = self.notes_text(slide).strip()
        if notes:
            text = (text + "\n" if text else "") + "[notes] " + notes
        return text


def _fetch(service, pres_id: str, account: str | None) -> _Deck:
    with friendly_google_errors(account, "this presentation"):
        raw = service.presentations().get(presentationId=pres_id).execute()
    return _Deck(raw)


def _squash(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def outline(d: _Deck) -> list[dict]:
    out = []
    for i, s in enumerate(d.slides, start=1):
        out.append({
            "index": i,
            "object_id": s.get("objectId", ""),
            "layout": d.layout_of(s),
            "title": d.title_text(s),
            "body": _squash(d.body_text(s), OUTLINE_BODY_CHARS),
            "has_notes": bool(d.notes_text(s).strip()),
        })
    return out


def slide_detail(d: _Deck, index: int) -> dict:
    s = d.slide_at(index)
    texts, images, tables = [], [], []
    for el in _flatten(s.get("pageElements", []) or []):
        oid = el.get("objectId", "")
        if "shape" in el:
            texts.append({
                "object_id": oid,
                "placeholder": _placeholder_type(el),
                "shape_type": el["shape"].get("shapeType", ""),
                "text": _element_text(el),
            })
        elif "image" in el:
            images.append({
                "object_id": oid,
                "source_url": el["image"].get("sourceUrl"),
                "content_url": el["image"].get("contentUrl"),
            })
        elif "table" in el:
            tables.append({"object_id": oid, "rows": _table_rows(el)})
        elif "wordArt" in el:
            texts.append({
                "object_id": oid, "placeholder": None, "shape_type": "WORD_ART",
                "text": _element_text(el),
            })
    nid, _el = d.notes_shape(s)
    return {
        "index": int(index),
        "object_id": s.get("objectId", ""),
        "layout": d.layout_of(s),
        "title": d.title_text(s),
        "text_elements": texts,
        "images": images,
        "tables": tables,
        "notes": d.notes_text(s),
        "notes_object_id": nid,
    }


def deck_text(d: _Deck) -> str:
    chunks = []
    for i, s in enumerate(d.slides, start=1):
        chunks.append(f"--- slide {i} ---\n{d.slide_text(s)}")
    return "\n\n".join(chunks)


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
def slides_read(
    action: str,
    presentation_id_or_url: str,
    slide: int | None = None,
    max_chars: int = MAX_CHARS,
    offset: int = 0,
    account: str | None = None,
) -> dict:
    """Read a Google Slides deck. One tool, four views — see the module docstring.

    ``slide`` (1-based) is needed by the ``slide`` action. ``text`` takes
    ``max_chars`` and ``offset`` for paging: a truncated result carries
    ``next_offset``; call again with ``offset`` until ``truncated`` is false.
    ``account`` — which Drive: ``blt`` (default), ``deadlift``, ``choco``.
    """
    if action not in ACTIONS:
        raise SlidesReadError(
            f"unknown action {action!r} — expected one of: {', '.join(ACTIONS)}"
        )
    if not presentation_id_or_url or not str(presentation_id_or_url).strip():
        raise SlidesReadError(f"{action} needs presentation_id_or_url")
    # Refusals that need no presentation come before mock mode, so a refusal
    # never depends on which mode the server is in.
    if action == "slide":
        if slide is None:
            raise SlidesReadError("slide needs slide (a 1-based slide number)")
        try:
            slide = int(slide)
        except (TypeError, ValueError):
            raise SlidesReadError(f"slide must be an integer (got {slide!r})")
        if slide < 1:
            raise SlidesReadError(f"slide must be 1 or more (got {slide})")

    account = resolve_account(account)
    if settings.use_mock:
        return _mock({
            "action": action, "id": presentation_id_or_url, "slide": slide,
            "status": "not_read_mock",
        })

    pres_id = _file_id(presentation_id_or_url)
    service = _slides(account)
    d = _fetch(service, pres_id, account)
    base = {"id": pres_id, "title": d.title, "url": _viewable_url(pres_id, _SLIDES_MIME)}

    if action == "outline":
        items = outline(d)
        return {**base, "slides": items, "count": len(items), "status": "read"}

    if action == "slide":
        return {**base, **slide_detail(d, slide), "slide_count": d.count, "status": "read"}  # type: ignore[arg-type]

    if action == "text":
        return {
            **base, "slide_count": d.count,
            **window(deck_text(d), max_chars, offset),
            "status": "read",
        }

    # info
    width_pt, height_pt = d.page_size_pt()
    drive = _drive(account)
    with friendly_google_errors(account, "this presentation"):
        meta = drive.files().get(
            fileId=pres_id,
            fields="modifiedTime,lastModifyingUser(displayName,emailAddress),owners(emailAddress)",
            supportsAllDrives=True,
        ).execute()
    who = meta.get("lastModifyingUser") or {}
    return {
        **base,
        "slide_count": d.count,
        "page_size": {"width_pt": width_pt, "height_pt": height_pt},
        "modified_time": meta.get("modifiedTime", ""),
        "last_modified_by": who.get("emailAddress") or who.get("displayName") or "",
        "owners": [o.get("emailAddress", "") for o in meta.get("owners", []) or []],
        "status": "read",
    }
