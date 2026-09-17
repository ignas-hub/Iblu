"""Full-power Google Docs editing — raw ``batchUpdate`` passthrough.

``gdoc_append`` / ``gdoc_replace_text`` (in ``tools.drive``) cover the two
workflows Ignas needed on day one. They are not the ceiling: the Docs API's
``batchUpdate`` supports formatting (bold, highlight/backgroundColor, fonts,
links), paragraph styles (headings), bullets/numbered lists, tables, inline
images, document style, named ranges — everything the Google Docs UI can do.
``batch_update`` below sends any list of raw ``batchUpdate`` request objects
through unmodified, so nothing here needs to special-case a new formatting
feature Google adds later.

The one thing a model genuinely cannot supply is a character index — Docs
API ranges/locations are index-based, and an LLM can't count UTF-16 code
units in a document it hasn't seen structurally. So this module adds ONE
piece of magic on top of the raw passthrough: a text anchor. Anywhere a
request wants a ``range`` (``{"startIndex", "endIndex"}``) or a ``location``
(``{"index"}``), the caller may instead write
``{"text": "exact text", "occurrence": 1}`` and this module resolves it
against a single fetch of the live document.
"""

from __future__ import annotations

import bisect
import copy
import re

from ..config import resolve_account, settings
from . import drive as drive_tools

_NAMED_COLORS: dict[str, str] = {
    "yellow": "#FFFF00",
    "green": "#00FF00",
    "red": "#FF0000",
    "blue": "#00FFFF",
    "cyan": "#00FFFF",
    "orange": "#FFA500",
    "pink": "#FFC0CB",
    "gray": "#D9D9D9",
    "grey": "#D9D9D9",
    "purple": "#D9D2E9",
}

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


# --------------------------------------------------------------------------- #
# Anchor resolution — text -> document indices
# --------------------------------------------------------------------------- #
def _build_segments(doc: dict) -> list[dict]:
    """Flatten every ``textRun`` in the document (body only) in reading order.

    Each segment is ``{"start": <doc index of its first char>, "text": ...}``.
    Walks tables (rows -> cells -> content) and tables-of-contents too, so a
    run split across two adjacent ``textRun`` elements (a common shape after
    Google merges/splits style runs) is still found correctly — the search
    happens over the *concatenation* of segments, and a match's start/end
    offsets are mapped back to each segment's own ``startIndex`` rather than
    assumed to be a flat string starting at 0.
    """
    segments: list[dict] = []

    def walk(elements: list[dict]) -> None:
        for el in elements:
            para = el.get("paragraph")
            if para is not None:
                for pel in para.get("elements", []):
                    run = pel.get("textRun")
                    if run is None:
                        continue
                    content = run.get("content", "")
                    if not content:
                        continue
                    start = pel.get("startIndex")
                    if start is None:
                        continue
                    segments.append({"start": start, "text": content})
                continue
            table = el.get("table")
            if table is not None:
                for row in table.get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []))
                continue
            toc = el.get("tableOfContents")
            if toc is not None:
                walk(toc.get("content", []))

    walk(doc.get("body", {}).get("content", []))
    return segments


def _prefix_offsets(segments: list[dict]) -> list[int]:
    """Cumulative full-text offset at which each segment begins."""
    prefix = []
    total = 0
    for seg in segments:
        prefix.append(total)
        total += len(seg["text"])
    return prefix


def _find_matches(full_text: str, text: str) -> list[tuple[int, int]]:
    """Every non-overlapping occurrence of ``text`` in ``full_text``, in order."""
    matches = []
    start = 0
    while True:
        idx = full_text.find(text, start)
        if idx == -1:
            break
        matches.append((idx, idx + len(text)))
        start = idx + len(text)
    return matches


def _map_offset(segments: list[dict], prefix: list[int], offset: int, *, end: bool) -> int:
    """Map a full-text offset back to a real document index.

    ``end=True`` means ``offset`` is an EXCLUSIVE end boundary (one past the
    match's last character) — resolved via the segment containing the last
    character itself, so a match ending exactly on a structural boundary
    (end of a run, end of a table cell) still lands on the correct segment's
    own end index rather than a neighbour's unrelated start index.
    """
    if not segments:
        raise ValueError(
            "gdoc_batch_update: this document has no text content to anchor to."
        )
    probe = max(offset - 1, 0) if end else offset
    idx = bisect.bisect_right(prefix, probe) - 1
    idx = max(0, min(idx, len(segments) - 1))
    seg = segments[idx]
    local = offset - prefix[idx]
    return seg["start"] + local


class _DocIndex:
    """Lazily fetches + indexes a document exactly once per `batch_update` call."""

    def __init__(self, fetch_doc):
        self._fetch_doc = fetch_doc
        self._segments: list[dict] | None = None
        self._prefix: list[int] | None = None
        self._full_text: str | None = None

    def _ensure(self) -> None:
        if self._segments is not None:
            return
        doc = self._fetch_doc()
        self._segments = _build_segments(doc)
        self._prefix = _prefix_offsets(self._segments)
        self._full_text = "".join(s["text"] for s in self._segments)

    def matches(self, text: str) -> list[tuple[int, int]]:
        self._ensure()
        return _find_matches(self._full_text, text)  # type: ignore[arg-type]

    def to_doc_index(self, offset: int, *, end: bool) -> int:
        self._ensure()
        return _map_offset(self._segments, self._prefix, offset, end=end)  # type: ignore[arg-type]


def _find_anchor_path(node, path: tuple = ()):
    """Find the first ``range``/``location`` value in ``node`` that is an anchor.

    An anchor is a dict holding ``"text"`` and NOT the real index keys
    (``startIndex``/``endIndex`` for a range, ``index`` for a location) —
    i.e. it hasn't been resolved yet. Returns ``(path, kind, anchor_dict)``
    or ``None``. ``path`` is the sequence of keys/indices from the request
    root down to (and including) the ``"range"``/``"location"`` key itself,
    so the same path can later be used to overwrite it in place.
    """
    if isinstance(node, dict):
        for key in ("range", "location"):
            val = node.get(key)
            if isinstance(val, dict) and "text" in val:
                if key == "range" and ("startIndex" in val or "endIndex" in val):
                    continue
                if key == "location" and "index" in val:
                    continue
                return path + (key,), key, val
        for k, v in node.items():
            found = _find_anchor_path(v, path + (k,))
            if found:
                return found
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found = _find_anchor_path(item, path + (i,))
            if found:
                return found
    return None


def _set_at_path(root, path: tuple, value) -> None:
    node = root
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _resolve_anchors_in_batch(requests: list[dict], fetch_doc) -> list[dict]:
    """Expand every text/location anchor in ``requests`` into real indices.

    Requests without an anchor pass through completely unmodified. A request
    whose anchor has ``occurrence: "all"`` is expanded into one copy per
    match, applied in DESCENDING index order (within that expansion only) so
    an earlier edit never shifts the index of a later one in the same
    expansion.
    """
    index = _DocIndex(fetch_doc)
    resolved: list[dict] = []

    for req in requests:
        found = _find_anchor_path(req)
        if found is None:
            resolved.append(req)
            continue

        path, kind, anchor = found
        text = anchor.get("text")
        if not text:
            raise ValueError(
                "gdoc_batch_update: an anchor must include non-empty 'text'."
            )
        occurrence = anchor.get("occurrence", 1)
        all_matches = index.matches(text)
        if not all_matches:
            raise ValueError(
                f"gdoc_batch_update: anchor text not found in document: {text!r}"
            )

        is_all = isinstance(occurrence, str) and occurrence.strip().lower() == "all"
        if is_all:
            chosen = list(all_matches)
        else:
            try:
                occ_idx = int(occurrence) - 1
            except (TypeError, ValueError):
                raise ValueError(
                    f"gdoc_batch_update: invalid occurrence {occurrence!r} for "
                    f"anchor text {text!r} — use a 1-based integer or 'all'."
                )
            if occ_idx < 0 or occ_idx >= len(all_matches):
                raise ValueError(
                    f"gdoc_batch_update: occurrence {occurrence!r} out of range "
                    f"for anchor text {text!r} ({len(all_matches)} match(es) found)."
                )
            chosen = [all_matches[occ_idx]]

        # Descending index order so earlier (higher-index) edits are applied
        # before later ones, and never shift indices we still need.
        chosen_sorted = sorted(chosen, key=lambda m: m[0], reverse=True)

        for foff_start, foff_end in chosen_sorted:
            doc_start = index.to_doc_index(foff_start, end=False)
            doc_end = index.to_doc_index(foff_end, end=True)
            new_req = copy.deepcopy(req)
            segment_id = anchor.get("segmentId")
            if kind == "range":
                new_value: dict = {"startIndex": doc_start, "endIndex": doc_end}
            else:
                position = anchor.get("position", "before")
                new_value = {"index": doc_end if position == "after" else doc_start}
            if segment_id is not None:
                new_value["segmentId"] = segment_id
            _set_at_path(new_req, path, new_value)
            resolved.append(new_req)

    return resolved



# Requests that change the document's length. After one of these runs, every
# index further down the document has moved.
LENGTH_CHANGING = frozenset({
    "insertText", "deleteContentRange", "insertTable", "insertTableRow",
    "insertTableColumn", "deleteTableRow", "deleteTableColumn",
    "insertInlineImage", "insertPageBreak", "insertSectionBreak",
    "replaceAllText", "deletePositionedObject", "createParagraphBullets",
    "deleteParagraphBullets", "createHeader", "createFooter", "createFootnote",
})


def _position_of(request: dict) -> int | None:
    """The document index a request acts at, if it has one."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "startIndex" in node and isinstance(node["startIndex"], int):
                found.append(node["startIndex"])
            elif "index" in node and isinstance(node["index"], int):
                found.append(node["index"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(request)
    return max(found) if found else None


def order_for_stable_indices(requests: list[dict]) -> list[dict]:
    """Reorder a resolved batch so anchors stay correct.

    Every anchor is resolved against the document as it was BEFORE the batch,
    but Google applies a batch's requests in sequence. So if request 1 inserts
    a paragraph above the text request 2 was anchored to, request 2 lands in
    the wrong place — silently, since the indices are still valid numbers.

    When nothing in the batch changes the document's length, order is
    irrelevant and is left exactly as written. When something does, every
    request that acts at a position is applied from the bottom of the document
    upwards, so an edit can only ever move text that has already been handled.
    Requests with no position (`replaceAllText`, `updateDocumentStyle`) keep
    their relative order and run last, after every positional edit.

    The consequence worth knowing: an anchor always refers to text that exists
    BEFORE the batch. Styling text inserted in the same batch has to use raw
    indices, or a second call.
    """
    if not any(next(iter(r), None) in LENGTH_CHANGING for r in requests):
        return list(requests)
    positional = [(i, r, _position_of(r)) for i, r in enumerate(requests)]
    with_pos = [x for x in positional if x[2] is not None]
    without = [x for x in positional if x[2] is None]
    # Descending position; ties keep their written order.
    with_pos.sort(key=lambda x: (-x[2], x[0]))
    return [r for _, r, _ in with_pos] + [r for _, r, _ in without]

# --------------------------------------------------------------------------- #
# The tool itself
# --------------------------------------------------------------------------- #
def batch_update(doc_id_or_url: str, requests: list[dict], account: str | None = None) -> dict:
    """Send raw Google Docs API ``batchUpdate`` request objects, unmodified.

    Text anchors (see module docstring) are resolved first, against a single
    fetch of the live document. ``account`` — which Drive to edit: ``blt``
    (default), ``deadlift``, ``choco``.
    """
    account = resolve_account(account)
    if settings.use_mock:
        return drive_tools._mock({
            "doc_id": doc_id_or_url, "applied": 0, "replies": [],
            "status": "not_modified_mock",
        })

    if not requests:
        raise ValueError("gdoc_batch_update: `requests` must be a non-empty list.")

    doc_id = drive_tools._file_id(doc_id_or_url)
    docs = drive_tools._docs(account)

    def fetch_doc() -> dict:
        with drive_tools.friendly_google_errors(account, "this Google Doc"):
            return docs.documents().get(documentId=doc_id).execute()

    anchored = any(_find_anchor_path(r) is not None for r in requests)
    resolved = _resolve_anchors_in_batch(requests, fetch_doc)
    if anchored:
        resolved = order_for_stable_indices(resolved)

    with drive_tools.friendly_google_errors(account, "this Google Doc"):
        result = docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": resolved},
        ).execute()

    return {
        "doc_id": doc_id,
        "applied": len(resolved),
        "replies": result.get("replies", []),
        "url": drive_tools._viewable_url(doc_id, "application/vnd.google-apps.document"),
    }


def _resolve_color(color: str) -> dict:
    key = color.strip().lower()
    hex_val = _NAMED_COLORS.get(key, color.strip())
    if not _HEX_RE.match(hex_val):
        raise ValueError(
            f"gdoc_batch_update: unrecognized color {color!r}. Use a #RRGGBB "
            f"hex value or one of: {', '.join(sorted(_NAMED_COLORS))}."
        )
    if not hex_val.startswith("#"):
        hex_val = "#" + hex_val
    r = int(hex_val[1:3], 16) / 255
    g = int(hex_val[3:5], 16) / 255
    b = int(hex_val[5:7], 16) / 255
    return {"color": {"rgbColor": {"red": r, "green": g, "blue": b}}}


def highlight(
    doc_id_or_url: str,
    text: str,
    color: str = "yellow",
    occurrence: int | str = 1,
    account: str | None = None,
) -> dict:
    """Convenience wrapper: highlight ``text`` with a background color.

    ``color`` accepts a ``#RRGGBB`` hex value or a name (yellow, green, red,
    blue, orange, pink, gray, purple). Builds and sends a single
    ``updateTextStyle`` request via `batch_update` — the raw tool surface is
    `batch_update` itself; this just saves constructing the request by hand.
    """
    request = {
        "updateTextStyle": {
            "range": {"text": text, "occurrence": occurrence},
            "textStyle": {"backgroundColor": _resolve_color(color)},
            "fields": "backgroundColor",
        }
    }
    return batch_update(doc_id_or_url, [request], account=account)


# --------------------------------------------------------------------------- #
# Table insertion + fill — insertTable creates EMPTY cells; filling them
# requires knowing each cell's document index after the table exists, which
# a model cannot compute. This does that index work server-side.
# --------------------------------------------------------------------------- #
_MAX_TABLE_COLUMNS = 20
_MAX_TABLE_ROWS = 200


def _normalize_table_rows(rows: list) -> list[list[str]]:
    """Validate + coerce ``rows`` into a rectangular list of strings.

    Every cell is converted with ``str()``; ``None`` becomes ``""``. Ragged
    rows are padded with ``""`` up to the widest row.
    """
    if not rows:
        raise ValueError("gdoc_insert_table: `rows` must be a non-empty list of rows.")
    if len(rows) > _MAX_TABLE_ROWS:
        raise ValueError(
            f"gdoc_insert_table: {len(rows)} rows exceeds the {_MAX_TABLE_ROWS}-row limit."
        )

    normalized: list[list[str]] = []
    width = 0
    for row in rows:
        if not isinstance(row, (list, tuple)):
            raise ValueError("gdoc_insert_table: each row must be a list of cell values.")
        cells = ["" if v is None else str(v) for v in row]
        width = max(width, len(cells))
        normalized.append(cells)

    if width == 0:
        raise ValueError("gdoc_insert_table: `rows` must contain at least one column.")
    if width > _MAX_TABLE_COLUMNS:
        raise ValueError(
            f"gdoc_insert_table: {width} columns exceeds the {_MAX_TABLE_COLUMNS}-column limit."
        )

    return [row + [""] * (width - len(row)) for row in normalized]


def _find_paragraph_end_containing(doc: dict, index: int) -> int:
    """The ``endIndex`` of the paragraph (body or table cell) that contains ``index``."""
    found: list[int] = []

    def walk(elements: list[dict]) -> None:
        for el in elements:
            if "paragraph" in el:
                start, end = el.get("startIndex"), el.get("endIndex")
                if start is not None and end is not None and start <= index < end:
                    found.append(end)
                continue
            if "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []))
                continue
            if "tableOfContents" in el:
                walk(el["tableOfContents"].get("content", []))

    walk(doc.get("body", {}).get("content", []))
    if not found:
        raise RuntimeError(
            "gdoc_insert_table: could not locate the paragraph containing the anchor text."
        )
    return found[0]


def _find_inserted_table(doc: dict, min_start: int) -> dict:
    """The ``table`` structural element with the smallest ``startIndex`` >= ``min_start``.

    Never assumes the new table is the first or last table in the document —
    the document may already contain other tables before and after it.
    """
    tables: list[dict] = []

    def walk(elements: list[dict]) -> None:
        for el in elements:
            if "table" in el:
                if el.get("startIndex") is not None:
                    tables.append(el)
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []))
                continue
            if "tableOfContents" in el:
                walk(el["tableOfContents"].get("content", []))

    walk(doc.get("body", {}).get("content", []))
    candidates = [t for t in tables if t["startIndex"] >= min_start]
    if not candidates:
        raise RuntimeError(
            "gdoc_insert_table: could not find the newly inserted table after insertion."
        )
    return min(candidates, key=lambda t: t["startIndex"])


def _send_docs_batch(docs, doc_id: str, requests: list[dict], account: str | None) -> dict:
    with drive_tools.friendly_google_errors(account, "this Google Doc"):
        return docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests},
        ).execute()


def insert_table(
    doc_id_or_url: str,
    rows: list,
    after_text: str | None = None,
    occurrence: int = 1,
    header: bool = True,
    account: str | None = None,
) -> dict:
    """Insert a table into a Google Doc AND fill it with ``rows`` in one call.

    Unlike a raw ``insertTable`` request through `batch_update`, this fills
    every cell — the index of each cell only exists after the table has
    actually been inserted, so this fetches the document again to find them
    rather than asking the caller to guess.

    ``rows`` is a list of lists of cell values (``str()``-converted; ``None``
    becomes ``""``); ragged rows are padded with ``""`` to the widest row.

    Placement: with ``after_text``, the table is inserted immediately after
    the END of the paragraph containing that text (``occurrence`` is
    1-based, like the anchors `batch_update` accepts; a missing anchor
    raises, naming the text). Without ``after_text``, the table is appended
    at the end of the document.

    If ``header`` is true (default) and there is more than one row, the
    first row's text is bolded automatically. For any other formatting —
    borders, cell background, column widths, alignment — follow up with
    `batch_update`, after reading the table's cell positions with
    `gdoc_read(structure=True)`.

    ``account`` — which Drive to edit: ``blt`` (default), ``deadlift``,
    ``choco``.
    """
    account = resolve_account(account)
    normalized = _normalize_table_rows(rows)
    num_rows = len(normalized)
    num_cols = len(normalized[0])

    if settings.use_mock:
        return drive_tools._mock({
            "doc_id": doc_id_or_url, "rows": num_rows, "columns": num_cols,
            "table_start_index": None, "status": "not_modified_mock",
        })

    doc_id = drive_tools._file_id(doc_id_or_url)
    docs = drive_tools._docs(account)

    def fetch_doc() -> dict:
        with drive_tools.friendly_google_errors(account, "this Google Doc"):
            return docs.documents().get(documentId=doc_id).execute()

    if after_text:
        doc = fetch_doc()
        segments = _build_segments(doc)
        prefix = _prefix_offsets(segments)
        full_text = "".join(s["text"] for s in segments)
        all_matches = _find_matches(full_text, after_text)
        if not all_matches:
            raise ValueError(
                f"gdoc_insert_table: anchor text not found in document: {after_text!r}"
            )
        try:
            occ_idx = int(occurrence) - 1
        except (TypeError, ValueError):
            raise ValueError(
                f"gdoc_insert_table: invalid occurrence {occurrence!r} — "
                "use a 1-based integer."
            )
        if occ_idx < 0 or occ_idx >= len(all_matches):
            raise ValueError(
                f"gdoc_insert_table: occurrence {occurrence!r} out of range for "
                f"anchor text {after_text!r} ({len(all_matches)} match(es) found)."
            )
        foff_start, _foff_end = all_matches[occ_idx]
        doc_start = _map_offset(segments, prefix, foff_start, end=False)
        insertion_index = _find_paragraph_end_containing(doc, doc_start)
        body = doc.get("body", {}).get("content", [])
        body_end = max((seg.get("endIndex", 1) for seg in body), default=1)
        if insertion_index >= body_end:
            # The anchor is in the LAST paragraph, so "the end of its
            # paragraph" is also the end of the document — and Google rejects
            # that as a location ("Index 105 must be less than the end index of
            # the referenced segment, 105"). Found on the first live test.
            # Appending at the end of the body is the same place, expressed the
            # way the API accepts.
            insert_request = {
                "insertTable": {
                    "rows": num_rows, "columns": num_cols,
                    "endOfSegmentLocation": {},
                }
            }
            min_start = body_end - 1
        else:
            insert_request = {
                "insertTable": {
                    "rows": num_rows, "columns": num_cols,
                    "location": {"index": insertion_index},
                }
            }
            min_start = insertion_index
    else:
        before_doc = fetch_doc()
        body = before_doc.get("body", {}).get("content", [])
        min_start = max((seg.get("endIndex", 1) for seg in body), default=1) - 1
        insert_request = {
            "insertTable": {
                "rows": num_rows, "columns": num_cols,
                "endOfSegmentLocation": {},
            }
        }

    _send_docs_batch(docs, doc_id, [insert_request], account)

    doc_after_insert = fetch_doc()
    table_el = _find_inserted_table(doc_after_insert, min_start)
    table_start = table_el["startIndex"]
    table = table_el["table"]

    fill: list[tuple[int, str]] = []
    for row_idx, row in enumerate(table.get("tableRows", [])):
        if row_idx >= num_rows:
            break
        for col_idx, cell in enumerate(row.get("tableCells", [])):
            if col_idx >= num_cols:
                break
            value = normalized[row_idx][col_idx]
            if not value:
                continue
            content = cell.get("content", [])
            if not content:
                continue
            start = content[0].get("startIndex")
            if start is None:
                continue
            fill.append((start, value))

    # Descending index order — inserting text at a higher index never shifts
    # a lower index still waiting to be used, within this same batch.
    fill.sort(key=lambda item: item[0], reverse=True)
    if fill:
        fill_requests = [
            {"insertText": {"location": {"index": start}, "text": value}}
            for start, value in fill
        ]
        _send_docs_batch(docs, doc_id, fill_requests, account)

    if header and num_rows > 1:
        doc_after_fill = fetch_doc()
        table_el2 = _find_inserted_table(doc_after_fill, min_start)
        header_row = table_el2["table"].get("tableRows", [])
        style_requests = []
        if header_row:
            for cell in header_row[0].get("tableCells", []):
                content = cell.get("content", [])
                if not content:
                    continue
                first = content[0]
                para = first.get("paragraph")
                if para is None:
                    continue
                start, end = first.get("startIndex"), first.get("endIndex")
                if start is None or end is None:
                    continue
                text = "".join(
                    pel["textRun"].get("content", "")
                    for pel in para.get("elements", [])
                    if pel.get("textRun") is not None
                )
                text_len = len(text.rstrip("\n"))
                if text_len <= 0:
                    continue
                range_end = start + text_len
                if range_end <= start:
                    continue
                style_requests.append({
                    "updateTextStyle": {
                        "range": {"startIndex": start, "endIndex": range_end},
                        "textStyle": {"bold": True},
                        "fields": "bold",
                    }
                })
        if style_requests:
            _send_docs_batch(docs, doc_id, style_requests, account)

    return {
        "doc_id": doc_id,
        "rows": num_rows,
        "columns": num_cols,
        "table_start_index": table_start,
        "url": drive_tools._viewable_url(doc_id, "application/vnd.google-apps.document"),
    }


# --------------------------------------------------------------------------- #
# Structure read — used by gdoc_read(structure=True)
# --------------------------------------------------------------------------- #
def _extract_color(color_style: dict | None) -> str | None:
    if not color_style:
        return None
    rgb = ((color_style.get("color") or {}).get("rgbColor")) or {}
    if not rgb:
        return None

    def c(v):
        return max(0, min(255, round((v or 0) * 255)))

    return f"#{c(rgb.get('red')):02X}{c(rgb.get('green')):02X}{c(rgb.get('blue')):02X}"


def _describe_text_run(run: dict) -> dict:
    style = run.get("textStyle", {}) or {}
    font_size = (style.get("fontSize") or {}).get("magnitude")
    link = (style.get("link") or {}).get("url")
    return {
        "text": run.get("content", ""),
        "bold": bool(style.get("bold", False)),
        "italic": bool(style.get("italic", False)),
        "underline": bool(style.get("underline", False)),
        "background_color": _extract_color(style.get("backgroundColor")),
        "foreground_color": _extract_color(style.get("foregroundColor")),
        "font_size": font_size,
        "link": link,
    }


def _describe_element(el: dict) -> dict:
    if "paragraph" in el:
        para = el["paragraph"]
        runs = [
            _describe_text_run(pel["textRun"])
            for pel in para.get("elements", [])
            if pel.get("textRun") is not None
        ]
        return {
            "type": "paragraph",
            "start_index": el.get("startIndex"),
            "end_index": el.get("endIndex"),
            "named_style_type": (para.get("paragraphStyle") or {}).get("namedStyleType"),
            "text_runs": runs,
        }
    if "table" in el:
        table = el["table"]
        rows = []
        for row in table.get("tableRows", []):
            cells = []
            for cell in row.get("tableCells", []):
                cells.append([_describe_element(c) for c in cell.get("content", [])])
            rows.append(cells)
        return {
            "type": "table",
            "start_index": el.get("startIndex"),
            "end_index": el.get("endIndex"),
            "rows": rows,
        }
    if "sectionBreak" in el:
        return {
            "type": "sectionBreak",
            "start_index": el.get("startIndex"),
            "end_index": el.get("endIndex"),
        }
    if "tableOfContents" in el:
        toc = el["tableOfContents"]
        return {
            "type": "tableOfContents",
            "start_index": el.get("startIndex"),
            "end_index": el.get("endIndex"),
            "content": [_describe_element(c) for c in toc.get("content", [])],
        }
    return {
        "type": "unknown",
        "start_index": el.get("startIndex"),
        "end_index": el.get("endIndex"),
    }


def extract_structure(doc: dict) -> list[dict]:
    """Turn a Docs API ``documents().get()`` response into a plain element list.

    Each element reports ``start_index``/``end_index``, its type (paragraph /
    table / sectionBreak / tableOfContents / unknown), a paragraph's
    ``named_style_type``, and — for paragraphs — each text run's text plus
    ``textStyle`` (bold, italic, underline, background/foreground color,
    font size, link).
    """
    return [_describe_element(el) for el in doc.get("body", {}).get("content", [])]
