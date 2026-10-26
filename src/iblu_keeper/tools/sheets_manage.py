"""Changing a spreadsheet's shape — the dozen things that need no raw request.

`tools/sheets.py`'s `write` can do all of this already, through a raw
`batchUpdate` passthrough. In practice it meant that "add a tab", "bold the
header", "sort by column C" each became a request body to get right, and when
it was wrong Ignas asked for a tool. He said it plainly: "It's crazy how many
times I have to ask to add tools." This module is the batch that ends that
for Sheets writes: each action here is a named verb with a known body, and the
test suite pins the body. `sheets_read.py` is the matching structure-and-search
layer; `directory.py` closes the same gap for "who is X".

It is deliberately ONE function with an `action` parameter, not twelve MCP
tools. Every new `@mcp.tool` costs Ignas a permission click in the connector
UI, forever, and a schema change costs it again (HANDOFF.md §12: one tool per
domain x side-effect class). Everything here changes a spreadsheet, so it all
lives in the write tool for the Sheets domain, and nothing here reads on its
own account — reading is `sheets_read`.

Two refusals are policy, not accident:

  * **No `delete_tab`, no delete spreadsheet.** A tab's data has no trash;
    a spreadsheet's does, and that is a Drive action which lives with the other
    Drive verbs. A tool that can both reshape a sheet and make one vanish is
    worse than one that cannot.
  * **Nothing here changes more than `_MAX_CHANGED_CELLS` (50,000) cells in one
    call.** Clear, append, insert and sort are estimated *before* the request
    is sent and refused over the line. `find_replace` is the one verb whose
    footprint cannot be counted before the fact (Sheets has no dry run for it);
    it reports `occurrences_changed` after instead, and the docstring says so.

`clear` of a whole tab or an open-ended range (``A:C``, ``1:1000``) needs
``confirm=True``: a bounded range is a decision, an unbounded one is usually a
typo.

In dry-run mode every action returns a mock dict with `status`
`not_created_mock` / `not_modified_mock` and no network call is made.
"""

from __future__ import annotations

import logging
import re

from ..config import resolve_account, settings
from . import drive as drive_tools
from .sheets import _col_to_num, _estimate_cells, _sheets

logger = logging.getLogger("iblu_keeper.tools.sheets_manage")

ACTIONS = (
    "create", "add_tab", "rename_tab", "clear", "append", "find_replace",
    "insert_rows", "insert_columns", "freeze", "format_header",
    "set_column_width", "sort",
)

# Asked for by name often enough to deserve a specific answer.
_REFUSED_ACTIONS = ("delete_tab", "delete", "delete_spreadsheet", "remove_tab", "trash")

_MAX_CHANGED_CELLS = 50_000

_BOUNDED_A1_RE = re.compile(r"^\s*([A-Za-z]+)(\d+)(?::([A-Za-z]+)(\d+))?\s*$")
_COL_RE = re.compile(r"^[A-Za-z]{1,3}$")


class SheetsManageError(ValueError):
    """Bad input to a `sheets_manage` action. The message names what was expected."""


def _mock(payload: dict) -> dict:
    return {"_mock": True, "note": "MOCK MODE — Sheets operation NOT performed.", **payload}


def _quote_tab(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _split_range(range_str: str) -> tuple[str | None, str]:
    """``'My Tab'!A1:B2`` -> ``("My Tab", "A1:B2")``; ``A1:B2`` -> ``(None, "A1:B2")``;
    ``'My Tab'`` -> ``("My Tab", "")``."""
    s = (range_str or "").strip()
    if "!" in s:
        tab, _, cells = s.rpartition("!")
    elif _BOUNDED_A1_RE.match(s) or re.match(r"^[A-Za-z]*\d*:[A-Za-z]*\d*$", s):
        return None, s
    else:
        tab, cells = s, ""
    tab = tab.strip()
    if len(tab) >= 2 and tab[0] == "'" and tab[-1] == "'":
        tab = tab[1:-1].replace("''", "'")
    return (tab or None), cells.strip()


def _column_index(column: str | None, what: str) -> int:
    """0-based index of a column letter. Refuses anything that is not one."""
    if not column or not _COL_RE.match(column.strip()):
        raise SheetsManageError(
            f"sheets_manage: {what} requires `column` as a letter (e.g. 'C'), got {column!r}."
        )
    return _col_to_num(column.strip()) - 1


def _refuse_over_cap(cells: int, what: str) -> None:
    if cells > _MAX_CHANGED_CELLS:
        raise SheetsManageError(
            f"sheets_manage: {what} would change ~{cells} cells, over the "
            f"{_MAX_CHANGED_CELLS}-cell limit for one call. Narrow it and try again."
        )


def _get_tabs(service, ss_id: str, account: str | None) -> list[dict]:
    with drive_tools.friendly_google_errors(account, "this spreadsheet"):
        meta = service.spreadsheets().get(
            spreadsheetId=ss_id,
            fields="sheets(properties(sheetId,title,index,gridProperties))",
        ).execute()
    return [sh.get("properties", {}) for sh in meta.get("sheets", [])]


def _find_tab(tabs: list[dict], tab: str | None) -> dict | None:
    if tab is None:
        return None
    wanted = tab.strip().lower()
    for p in tabs:
        if (p.get("title") or "").strip().lower() == wanted:
            return p
    return None


def _require_tab(tabs: list[dict], tab: str | None, what: str) -> dict:
    if not tab:
        raise SheetsManageError(f"sheets_manage: {what} requires `tab`.")
    props = _find_tab(tabs, tab)
    if props is None:
        names = ", ".join(repr(p.get("title")) for p in tabs)
        raise SheetsManageError(f"sheets_manage: no tab named {tab!r}. Tabs: {names}.")
    return props


def _grid(props: dict) -> tuple[int, int]:
    gp = props.get("gridProperties", {})
    return int(gp.get("rowCount") or 0), int(gp.get("columnCount") or 0)


def _batch(service, ss_id: str, requests: list[dict], account: str | None) -> dict:
    with drive_tools.friendly_google_errors(account, "this spreadsheet"):
        return service.spreadsheets().batchUpdate(
            spreadsheetId=ss_id, body={"requests": requests},
        ).execute()


def sheets_manage(
    action: str,
    spreadsheet_id_or_url: str | None = None,
    title: str | None = None,
    tab: str | None = None,
    new_title: str | None = None,
    range: str | None = None,  # noqa: A002 - matches the Sheets API parameter name
    rows: list[list] | None = None,
    find: str | None = None,
    replace: str | None = None,
    match_case: bool = False,
    at: int | None = None,
    count: int = 1,
    column: str | None = None,
    freeze_rows: int | None = None,
    freeze_cols: int | None = None,
    pixels: int | None = None,
    ascending: bool = True,
    has_header: bool = True,
    confirm: bool = False,
    account: str | None = None,
) -> dict:
    """Reshape a Google Sheet. Every action changes the spreadsheet.

    ``action`` and what it needs:

    - ``create`` — ``title``; optional ``tab`` names the first tab. Returns
      ``spreadsheet_id`` and ``url``. No ``spreadsheet_id_or_url``.
    - ``add_tab`` — ``tab``. Refused if a tab of that name exists.
    - ``rename_tab`` — ``tab`` -> ``new_title``. Refused if the new name exists.
    - ``clear`` — ``range`` (A1, may carry a tab prefix) and/or ``tab``.
      A whole tab or open-ended range needs ``confirm=True``.
    - ``append`` — ``rows`` (list of lists) under the data of ``tab``, as
      ``USER_ENTERED`` so formulas and dates are parsed.
    - ``find_replace`` — ``find`` -> ``replace`` in ``tab`` (or all tabs);
      ``match_case``. Returns ``occurrences_changed``. This is the one action
      whose footprint cannot be counted before sending.
    - ``insert_rows`` — ``count`` blank rows before 1-based row ``at`` in ``tab``.
    - ``insert_columns`` — ``count`` blank columns before ``column`` in ``tab``.
    - ``freeze`` — ``freeze_rows`` and/or ``freeze_cols`` of ``tab``.
    - ``format_header`` — bold row 1 of ``tab`` and freeze it.
    - ``set_column_width`` — ``column`` of ``tab`` to ``pixels``.
    - ``sort`` — the used range of ``tab`` by ``column``; ``ascending``;
      ``has_header`` keeps row 1 in place.

    Not here, on purpose: ``delete_tab`` and deleting a spreadsheet. A tab's
    data has no trash; a spreadsheet's does, and that is a Drive action.
    Anything that would change more than 50,000 cells is refused. Reads are
    ``sheets_read``. ``account`` — ``blt`` (default), ``deadlift``, ``choco``.
    """
    account = resolve_account(account)
    if action in _REFUSED_ACTIONS:
        raise SheetsManageError(
            f"sheets_manage: {action!r} is deliberately not implemented. Deleting a "
            "tab is unrecoverable (tab data has no trash), and deleting a spreadsheet "
            "is a Drive action — use the Drive tools to trash the file, where it can "
            "be restored."
        )
    if action not in ACTIONS:
        raise SheetsManageError(
            f"sheets_manage: unknown action {action!r}. Valid actions: {', '.join(ACTIONS)}."
        )

    if settings.use_mock:
        return _mock({
            "spreadsheet_id": spreadsheet_id_or_url, "action": action,
            "status": "not_created_mock" if action == "create" else "not_modified_mock",
        })

    if action != "create" and not spreadsheet_id_or_url:
        raise SheetsManageError(
            f"sheets_manage: action={action!r} requires `spreadsheet_id_or_url`."
        )

    service = _sheets(account)

    if action == "create":
        if not title or not title.strip():
            raise SheetsManageError("sheets_manage: action='create' requires `title`.")
        body: dict = {"properties": {"title": title.strip()}}
        if tab and tab.strip():
            body["sheets"] = [{"properties": {"title": tab.strip()}}]
        with drive_tools.friendly_google_errors(account, "Google Sheets"):
            created = service.spreadsheets().create(
                body=body, fields="spreadsheetId,properties.title,sheets.properties",
            ).execute()
        ss_id = created["spreadsheetId"]
        first = (created.get("sheets") or [{}])[0].get("properties", {}).get("title")
        return {
            "spreadsheet_id": ss_id, "title": title.strip(), "first_tab": first,
            "url": f"https://docs.google.com/spreadsheets/d/{ss_id}/edit",
            "status": "created",
        }

    ss_id = drive_tools._file_id(spreadsheet_id_or_url)
    url = f"https://docs.google.com/spreadsheets/d/{ss_id}/edit"
    tabs = _get_tabs(service, ss_id, account)
    base = {"spreadsheet_id": ss_id, "action": action, "url": url}

    if action == "add_tab":
        if not tab or not tab.strip():
            raise SheetsManageError("sheets_manage: action='add_tab' requires `tab`.")
        if _find_tab(tabs, tab) is not None:
            raise SheetsManageError(
                f"sheets_manage: a tab named {tab.strip()!r} already exists; not adding another."
            )
        resp = _batch(service, ss_id, [{"addSheet": {"properties": {"title": tab.strip()}}}],
                      account)
        new_props = ((resp.get("replies") or [{}])[0].get("addSheet") or {}).get("properties", {})
        return {**base, "tab": tab.strip(), "sheet_id": new_props.get("sheetId"),
                "status": "added"}

    if action == "rename_tab":
        props = _require_tab(tabs, tab, "action='rename_tab'")
        if not new_title or not new_title.strip():
            raise SheetsManageError("sheets_manage: action='rename_tab' requires `new_title`.")
        if _find_tab(tabs, new_title) is not None:
            raise SheetsManageError(
                f"sheets_manage: a tab named {new_title.strip()!r} already exists; not renaming."
            )
        _batch(service, ss_id, [{"updateSheetProperties": {
            "properties": {"sheetId": props["sheetId"], "title": new_title.strip()},
            "fields": "title",
        }}], account)
        return {**base, "tab": props.get("title"), "new_title": new_title.strip(),
                "status": "renamed"}

    if action == "clear":
        if not range and not tab:
            raise SheetsManageError(
                "sheets_manage: action='clear' requires `range` (A1) or `tab`."
            )
        range_tab, cells = _split_range(range or "")
        tab_name = range_tab or tab
        props = _require_tab(tabs, tab_name, "action='clear'") if tab_name else tabs[0]
        tab_title = props.get("title", "")
        bounded = bool(cells) and _BOUNDED_A1_RE.match(cells) is not None
        if bounded:
            _refuse_over_cap(_estimate_cells(cells), "action='clear'")
            full_range = f"{_quote_tab(tab_title)}!{cells}"
        else:
            scope = f"the whole tab {tab_title!r}" if not cells else f"the open-ended range {cells!r}"
            if not confirm:
                raise SheetsManageError(
                    f"sheets_manage: clearing {scope} needs `confirm=True`. A bounded "
                    "A1 range (e.g. 'A2:F200') clears without confirmation."
                )
            n_rows, n_cols = _grid(props)
            _refuse_over_cap(n_rows * n_cols, f"clearing {scope} (whole grid as upper bound)")
            full_range = _quote_tab(tab_title) + (f"!{cells}" if cells else "")
        with drive_tools.friendly_google_errors(account, "this spreadsheet"):
            resp = service.spreadsheets().values().clear(
                spreadsheetId=ss_id, range=full_range, body={},
            ).execute()
        return {**base, "cleared_range": resp.get("clearedRange", full_range),
                "confirmed": not bounded, "status": "cleared"}

    if action == "append":
        if not rows or not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
            raise SheetsManageError(
                "sheets_manage: action='append' requires `rows` as a non-empty list of lists."
            )
        props = _require_tab(tabs, tab, "action='append'") if tab else tabs[0]
        tab_title = props.get("title", "")
        _refuse_over_cap(len(rows) * max(len(r) for r in rows), "action='append'")
        with drive_tools.friendly_google_errors(account, "this spreadsheet"):
            resp = service.spreadsheets().values().append(
                spreadsheetId=ss_id, range=_quote_tab(tab_title),
                valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()
        updates = resp.get("updates", {})
        return {**base, "tab": tab_title, "rows_appended": len(rows),
                "updated_range": updates.get("updatedRange"),
                "updated_cells": updates.get("updatedCells"), "status": "appended"}

    if action == "find_replace":
        if find is None or find == "":
            raise SheetsManageError("sheets_manage: action='find_replace' requires `find`.")
        if replace is None:
            raise SheetsManageError(
                "sheets_manage: action='find_replace' requires `replace` (use '' to delete)."
            )
        req: dict = {"find": find, "replacement": replace, "matchCase": bool(match_case)}
        if tab:
            props = _require_tab(tabs, tab, "action='find_replace'")
            req["sheetId"] = props["sheetId"]
        else:
            req["allSheets"] = True
        resp = _batch(service, ss_id, [{"findReplace": req}], account)
        reply = ((resp.get("replies") or [{}])[0].get("findReplace") or {})
        return {**base, "find": find, "replace": replace, "tab": tab,
                "occurrences_changed": int(reply.get("occurrencesChanged") or 0),
                "rows_changed": int(reply.get("rowsChanged") or 0),
                "sheets_changed": int(reply.get("sheetsChanged") or 0),
                "status": "replaced"}

    if action in ("insert_rows", "insert_columns"):
        props = _require_tab(tabs, tab, f"action={action!r}")
        n = int(count or 0)
        if n < 1:
            raise SheetsManageError(f"sheets_manage: {action} requires `count` >= 1.")
        n_rows, n_cols = _grid(props)
        if action == "insert_rows":
            if at is None or int(at) < 1:
                raise SheetsManageError(
                    "sheets_manage: action='insert_rows' requires `at` as a 1-based row number."
                )
            start, dimension, other = int(at) - 1, "ROWS", n_cols
        else:
            start, dimension, other = _column_index(column, "action='insert_columns'"), "COLUMNS", n_rows
        _refuse_over_cap(n * other, f"action={action!r}")
        _batch(service, ss_id, [{"insertDimension": {
            "range": {"sheetId": props["sheetId"], "dimension": dimension,
                      "startIndex": start, "endIndex": start + n},
            "inheritFromBefore": start > 0,
        }}], account)
        return {**base, "tab": props.get("title"), "dimension": dimension,
                "at_index": start, "count": n, "status": "inserted"}

    if action == "freeze":
        props = _require_tab(tabs, tab, "action='freeze'")
        if freeze_rows is None and freeze_cols is None:
            raise SheetsManageError(
                "sheets_manage: action='freeze' requires `freeze_rows` and/or `freeze_cols`."
            )
        grid_props: dict = {}
        fields = []
        if freeze_rows is not None:
            grid_props["frozenRowCount"] = max(0, int(freeze_rows))
            fields.append("gridProperties.frozenRowCount")
        if freeze_cols is not None:
            grid_props["frozenColumnCount"] = max(0, int(freeze_cols))
            fields.append("gridProperties.frozenColumnCount")
        _batch(service, ss_id, [{"updateSheetProperties": {
            "properties": {"sheetId": props["sheetId"], "gridProperties": grid_props},
            "fields": ",".join(fields),
        }}], account)
        return {**base, "tab": props.get("title"), "frozen_rows": grid_props.get("frozenRowCount"),
                "frozen_cols": grid_props.get("frozenColumnCount"), "status": "frozen"}

    if action == "format_header":
        props = _require_tab(tabs, tab, "action='format_header'")
        sid = props["sheetId"]
        _batch(service, ss_id, [
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }},
            {"updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }},
        ], account)
        return {**base, "tab": props.get("title"), "bold_rows": 1, "frozen_rows": 1,
                "status": "formatted"}

    if action == "set_column_width":
        props = _require_tab(tabs, tab, "action='set_column_width'")
        idx = _column_index(column, "action='set_column_width'")
        if pixels is None or int(pixels) < 1:
            raise SheetsManageError(
                "sheets_manage: action='set_column_width' requires `pixels` >= 1."
            )
        _batch(service, ss_id, [{"updateDimensionProperties": {
            "range": {"sheetId": props["sheetId"], "dimension": "COLUMNS",
                      "startIndex": idx, "endIndex": idx + 1},
            "properties": {"pixelSize": int(pixels)},
            "fields": "pixelSize",
        }}], account)
        return {**base, "tab": props.get("title"), "column": column.strip().upper(),
                "pixels": int(pixels), "status": "resized"}

    # action == "sort"
    props = _require_tab(tabs, tab, "action='sort'")
    idx = _column_index(column, "action='sort'")
    tab_title = props.get("title", "")
    # Measure the used range rather than trust the allocated grid: the grid is
    # 1000 x 26 for an empty tab, and the cap should be about data, not slack.
    with drive_tools.friendly_google_errors(account, "this spreadsheet"):
        values = service.spreadsheets().values().get(
            spreadsheetId=ss_id, range=_quote_tab(tab_title),
        ).execute().get("values", []) or []
    used_rows = len(values)
    used_cols = max((len(r) for r in values), default=0)
    if idx >= used_cols:
        raise SheetsManageError(
            f"sheets_manage: column {column.strip().upper()!r} is outside the used range "
            f"of {tab_title!r} ({used_cols} columns)."
        )
    _refuse_over_cap(used_rows * used_cols, "action='sort'")
    start_row = 1 if has_header else 0
    if used_rows - start_row < 2:
        return {**base, "tab": tab_title, "column": column.strip().upper(),
                "rows_sorted": 0, "status": "unchanged",
                "note": "fewer than two data rows; nothing to sort"}
    _batch(service, ss_id, [{"sortRange": {
        "range": {"sheetId": props["sheetId"], "startRowIndex": start_row,
                  "endRowIndex": used_rows, "startColumnIndex": 0,
                  "endColumnIndex": used_cols},
        "sortSpecs": [{"dimensionIndex": idx,
                       "sortOrder": "ASCENDING" if ascending else "DESCENDING"}],
    }}], account)
    return {**base, "tab": tab_title, "column": column.strip().upper(),
            "ascending": bool(ascending), "has_header": bool(has_header),
            "rows_sorted": used_rows - start_row, "status": "sorted"}
