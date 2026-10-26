"""The shape of a spreadsheet, and what is in it — without reading all of it.

`tools/sheets.py` reads an A1 range and that is all it does. Every question
that comes *before* a range — which tabs are there, what are the column
headers, where in this workbook is "Q3 invoice", what does the first screen
look like — had to be answered by pulling the whole first sheet and squinting,
or by Ignas opening the file himself. He said it plainly: "It's crazy how many
times I have to ask to add tools." This module is the structure-and-search
layer that ends that for Sheets; `sheets_manage.py` is the matching write
layer; `directory.py` closes the same gap for "who is X".

It is deliberately ONE function with an `action` parameter, not six MCP tools.
Every new `@mcp.tool` costs Ignas a permission click in the connector UI,
forever, and changing a tool's schema costs the click again (HANDOFF.md §12:
one tool per domain x side-effect class). Everything here is read-only, so it
belongs in the read tool for the Sheets domain and nowhere else.

`find` pulls plain values, never formatting, so it is bounded by a different
number from the 5,000-cell formatting cap in `sheets.py`: `includeGridData` is
roughly fifty times heavier per cell than `values.get`. The guard has the same
shape — estimate from the tab's grid *before* fetching, refuse or skip when the
estimate is over the line — with `_MAX_SEARCH_CELLS` as the line.

In dry-run mode every action returns a mock dict with `status`
`not_read_mock` and no network call is made.
"""

from __future__ import annotations

import logging

from ..config import resolve_account, settings
from . import drive as drive_tools
from .sheets import _num_to_col, _sheets

logger = logging.getLogger("iblu_keeper.tools.sheets_read")

ACTIONS = ("tabs", "info", "find", "headers", "named_ranges", "preview")

# Plain-values ceiling for one `find`, per tab. See the module docstring for
# why this is not `sheets._MAX_FORMATTING_CELLS`.
_MAX_SEARCH_CELLS = 50_000
_MAX_FIND_MATCHES = 100
_DEFAULT_PREVIEW_ROWS = 10
_MAX_PREVIEW_ROWS = 200

_TAB_FIELDS = "sheets(properties(sheetId,title,index,hidden,gridProperties))"


def _mock(payload: dict) -> dict:
    return {
        "_mock": True, "note": "MOCK MODE — no Sheets call made.",
        "status": "not_read_mock", **payload,
    }


def _quote_tab(title: str) -> str:
    """A1 sheet prefix: single-quoted, with embedded quotes doubled."""
    return "'" + title.replace("'", "''") + "'"


def _tab_summary(sheet: dict) -> dict:
    p = sheet.get("properties", {})
    gp = p.get("gridProperties", {})
    return {
        "title": p.get("title"),
        "sheet_id": p.get("sheetId"),
        "index": p.get("index"),
        "rows": gp.get("rowCount"),
        "cols": gp.get("columnCount"),
        "hidden": bool(p.get("hidden", False)),
    }


def _resolve_tab(sheet_list: list[dict], tab: str | None) -> dict:
    """The `properties` of the named tab, or of the first tab when `tab` is None.

    Raises `ValueError` naming every tab when the name is not there, so the
    caller can pick again without a second round trip to `tabs`.
    """
    if not sheet_list:
        raise ValueError("This spreadsheet has no tabs.")
    if tab is None:
        return sheet_list[0].get("properties", {})
    wanted = tab.strip().lower()
    for sh in sheet_list:
        p = sh.get("properties", {})
        if (p.get("title") or "").strip().lower() == wanted:
            return p
    names = ", ".join(repr(sh.get("properties", {}).get("title")) for sh in sheet_list)
    raise ValueError(f"No tab named {tab!r}. Tabs: {names}.")


def _grid_cells(props: dict) -> int:
    gp = props.get("gridProperties", {})
    return int(gp.get("rowCount") or 0) * int(gp.get("columnCount") or 0)


def _grid_to_a1(grid: dict, title_by_id: dict[int, str]) -> str:
    """A `GridRange` as A1. Open ends stay open (`A:C`, `1:5`, whole tab)."""
    title = title_by_id.get(grid.get("sheetId"), "")
    sr, er = grid.get("startRowIndex"), grid.get("endRowIndex")
    sc, ec = grid.get("startColumnIndex"), grid.get("endColumnIndex")
    start = (_num_to_col(sc + 1) if sc is not None else "") + (str(sr + 1) if sr is not None else "")
    end = (_num_to_col(ec) if ec is not None else "") + (str(er) if er is not None else "")
    cell_part = f"{start}:{end}" if (start or end) else ""
    prefix = _quote_tab(title) if title else ""
    if prefix and cell_part:
        return f"{prefix}!{cell_part}"
    return prefix or cell_part


def _get_meta(service, ss_id: str, fields: str, account: str | None) -> dict:
    with drive_tools.friendly_google_errors(account, "this spreadsheet"):
        return service.spreadsheets().get(spreadsheetId=ss_id, fields=fields).execute()


def _get_values(service, ss_id: str, rng: str, account: str | None) -> list[list]:
    with drive_tools.friendly_google_errors(account, "this spreadsheet"):
        resp = service.spreadsheets().values().get(spreadsheetId=ss_id, range=rng).execute()
    return resp.get("values", []) or []


def sheets_read(
    action: str,
    spreadsheet_id_or_url: str,
    tab: str | None = None,
    query: str | None = None,
    rows: int = _DEFAULT_PREVIEW_ROWS,
    account: str | None = None,
) -> dict:
    """Structure and search of a Google Sheet. Read-only.

    ``action``:

    - ``tabs`` — every tab: title, sheet_id, rows x cols, hidden.
    - ``info`` — spreadsheet title, locale, timezone, the tabs summary, url.
    - ``find`` — ``query`` as a case-insensitive substring over every cell of
      ``tab`` (or every tab). Up to 100 matches of ``{tab, cell, value}``.
      A tab whose grid is over ``_MAX_SEARCH_CELLS`` is skipped and named in
      ``skipped`` — or refused when it was the one asked for.
    - ``headers`` — the first row of ``tab`` (default: first tab), as a list
      and as ``{column letter: header}``.
    - ``named_ranges`` — every named range as ``{name, range}`` in A1.
    - ``preview`` — the first ``rows`` rows of ``tab`` (default 10, max 200).

    Reading an A1 range is ``sheets.read`` (the existing ``sheets_read`` MCP
    tool) and is not duplicated here. ``account`` — ``blt`` (default),
    ``deadlift``, ``choco``.
    """
    account = resolve_account(account)
    if action not in ACTIONS:
        raise ValueError(
            f"sheets_read: unknown action {action!r}. Valid actions: {', '.join(ACTIONS)}."
        )
    if settings.use_mock:
        return _mock({"spreadsheet_id": spreadsheet_id_or_url, "action": action})

    ss_id = drive_tools._file_id(spreadsheet_id_or_url)
    service = _sheets(account)
    url = f"https://docs.google.com/spreadsheets/d/{ss_id}/edit"

    if action == "tabs":
        meta = _get_meta(service, ss_id, _TAB_FIELDS, account)
        tabs = [_tab_summary(sh) for sh in meta.get("sheets", [])]
        return {"spreadsheet_id": ss_id, "tabs": tabs, "count": len(tabs),
                "url": url, "status": "read"}

    if action == "info":
        meta = _get_meta(
            service, ss_id, "properties(title,locale,timeZone)," + _TAB_FIELDS, account,
        )
        props = meta.get("properties", {})
        tabs = [_tab_summary(sh) for sh in meta.get("sheets", [])]
        return {
            "spreadsheet_id": ss_id,
            "title": props.get("title", ""),
            "locale": props.get("locale"),
            "timezone": props.get("timeZone"),
            "tabs": tabs,
            "tab_count": len(tabs),
            "url": url,
            "status": "read",
        }

    if action == "named_ranges":
        meta = _get_meta(
            service, ss_id, "namedRanges,sheets(properties(sheetId,title))", account,
        )
        title_by_id = {
            sh.get("properties", {}).get("sheetId"): sh.get("properties", {}).get("title", "")
            for sh in meta.get("sheets", [])
        }
        named = [
            {
                "name": nr.get("name"),
                "range": _grid_to_a1(nr.get("range", {}), title_by_id),
                "named_range_id": nr.get("namedRangeId"),
            }
            for nr in meta.get("namedRanges", []) or []
        ]
        return {"spreadsheet_id": ss_id, "named_ranges": named, "count": len(named),
                "url": url, "status": "read"}

    meta = _get_meta(service, ss_id, _TAB_FIELDS, account)
    sheet_list = meta.get("sheets", [])

    if action == "headers":
        props = _resolve_tab(sheet_list, tab)
        title = props.get("title", "")
        values = _get_values(service, ss_id, f"{_quote_tab(title)}!1:1", account)
        header_row = values[0] if values else []
        return {
            "spreadsheet_id": ss_id,
            "tab": title,
            "headers": header_row,
            "columns": {_num_to_col(i + 1): h for i, h in enumerate(header_row)},
            "count": len(header_row),
            "url": url,
            "status": "read",
        }

    if action == "preview":
        props = _resolve_tab(sheet_list, tab)
        title = props.get("title", "")
        n = max(1, min(int(rows or _DEFAULT_PREVIEW_ROWS), _MAX_PREVIEW_ROWS))
        values = _get_values(service, ss_id, f"{_quote_tab(title)}!1:{n}", account)
        return {
            "spreadsheet_id": ss_id,
            "tab": title,
            "rows": values,
            "row_count": len(values),
            "requested": n,
            "url": url,
            "status": "read",
        }

    # action == "find"
    if not query or not query.strip():
        raise ValueError("sheets_read: action='find' requires a non-empty `query`.")
    needle = query.strip().lower()
    if tab is not None:
        targets = [_resolve_tab(sheet_list, tab)]
        if _grid_cells(targets[0]) > _MAX_SEARCH_CELLS:
            raise ValueError(
                f"sheets_read: tab {targets[0].get('title')!r} has a grid of "
                f"~{_grid_cells(targets[0])} cells, over the {_MAX_SEARCH_CELLS}-cell "
                "search limit. Read a narrower A1 range with sheets_read instead."
            )
    else:
        targets = [sh.get("properties", {}) for sh in sheet_list]

    matches: list[dict] = []
    skipped: list[dict] = []
    truncated = False
    for props in targets:
        title = props.get("title", "")
        cells = _grid_cells(props)
        if cells > _MAX_SEARCH_CELLS:
            skipped.append({"tab": title, "cells": cells,
                            "reason": f"over the {_MAX_SEARCH_CELLS}-cell search limit"})
            continue
        gp = props.get("gridProperties", {})
        rng = (
            f"{_quote_tab(title)}!A1:"
            f"{_num_to_col(int(gp.get('columnCount') or 26))}{int(gp.get('rowCount') or 1000)}"
        )
        for r, row in enumerate(_get_values(service, ss_id, rng, account), start=1):
            for c, value in enumerate(row, start=1):
                if value is None or value == "":
                    continue
                if needle in str(value).lower():
                    matches.append({"tab": title, "cell": f"{_num_to_col(c)}{r}",
                                    "value": value})
                    if len(matches) >= _MAX_FIND_MATCHES:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    return {
        "spreadsheet_id": ss_id,
        "query": query,
        "matches": matches,
        "count": len(matches),
        "truncated": truncated,
        "skipped": skipped,
        "url": url,
        "status": "read",
    }
