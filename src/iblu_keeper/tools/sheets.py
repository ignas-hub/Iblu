"""Full-power Google Sheets read/write.

Two tools, following the domain x side-effect split (HANDOFF §12): `read`
(read-only — values, metadata, formatting) and `write` (everything that
changes a spreadsheet — values, formulas, and raw `batchUpdate` requests, so
this is the tool that makes formatting/structure changes possible: bold
headers, merges, borders, conditional formatting, charts, data validation,
adding/removing sheets — the entire Sheets API, passed through unmodified).

In dry-run / no-credentials mode these return deterministic mock data — no
network call is ever made, and nothing is reported as succeeding.
"""

from __future__ import annotations

import logging
import re

from ..config import resolve_account, settings
from . import drive as drive_tools

logger = logging.getLogger("iblu_keeper.tools.sheets")

_MAX_FORMATTING_CELLS = 5000

_A1_RE = re.compile(
    r"^\s*(?:'?[^'!]+'?!)?([A-Za-z]+)(\d+)(?::([A-Za-z]+)(\d+))?\s*$"
)

_VALID_WRITE_ACTIONS = {
    "update_values", "append_values", "clear_values", "batch_update", "create",
}


def _mock(payload: dict) -> dict:
    return {"_mock": True, "note": "MOCK MODE — Sheets operation NOT performed.", **payload}


def _sheets(account: str | None = None):
    from ..google_auth import build_service

    return build_service("sheets", "v4", account=account)


def _col_to_num(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _num_to_col(n: int) -> str:
    s = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s or "A"


def _estimate_cells(range_str: str) -> int:
    """Rough cell count of an A1 range. Unparseable/open-ended -> a large
    number, so the >5,000 guard triggers rather than silently allowing an
    unbounded formatting fetch."""
    m = _A1_RE.match(range_str or "")
    if not m:
        return _MAX_FORMATTING_CELLS + 1
    c1, r1, c2, r2 = m.groups()
    if c2 is None:
        return 1
    col1, col2 = _col_to_num(c1), _col_to_num(c2)
    row1, row2 = int(r1), int(r2)
    return (abs(col2 - col1) + 1) * (abs(row2 - row1) + 1)


def read(
    spreadsheet_id_or_url: str,
    ranges: list[str] | None = None,
    include_formatting: bool = False,
    account: str | None = None,
) -> dict:
    """Read a Google Sheet: metadata + values, or full cell formatting.

    Without ``ranges``, reads the first sheet's whole used range (by its
    ``gridProperties``). With ``include_formatting=True``, returns per-cell
    formatting (``includeGridData``) for the requested ranges instead of
    plain values — capped at ~5,000 cells; narrow ``ranges`` if you hit the
    limit. ``account`` — which Drive to read from: ``blt`` (default),
    ``deadlift``, ``choco``.
    """
    account = resolve_account(account)
    if settings.use_mock:
        return {
            "spreadsheet_id": spreadsheet_id_or_url, "title": "Mock Sheet",
            "sheets": [], "values": [], "_mock": True,
            "note": "MOCK MODE — no Sheets call made.",
        }

    ss_id = drive_tools._file_id(spreadsheet_id_or_url)
    sheets = _sheets(account)

    with drive_tools.friendly_google_errors(account, "this spreadsheet"):
        meta = sheets.spreadsheets().get(
            spreadsheetId=ss_id,
            fields="properties.title,sheets(properties(sheetId,title,gridProperties,index))",
        ).execute()

    props = meta.get("properties", {})
    sheet_list = meta.get("sheets", [])
    sheets_summary = []
    for sh in sheet_list:
        p = sh.get("properties", {})
        gp = p.get("gridProperties", {})
        sheets_summary.append({
            "sheet_id": p.get("sheetId"),
            "title": p.get("title"),
            "row_count": gp.get("rowCount"),
            "column_count": gp.get("columnCount"),
            "frozen_row_count": gp.get("frozenRowCount", 0),
            "frozen_column_count": gp.get("frozenColumnCount", 0),
        })

    if not ranges:
        if sheet_list:
            first = sheet_list[0]["properties"]
            gp = first.get("gridProperties", {})
            rows = gp.get("rowCount", 1000)
            cols = gp.get("columnCount", 26)
            ranges = [f"'{first['title']}'!A1:{_num_to_col(cols)}{rows}"]
        else:
            ranges = []

    result: dict = {
        "spreadsheet_id": ss_id,
        "title": props.get("title", ""),
        "sheets": sheets_summary,
        "url": f"https://docs.google.com/spreadsheets/d/{ss_id}/edit",
    }

    if include_formatting:
        total_cells = sum(_estimate_cells(r) for r in ranges)
        if total_cells > _MAX_FORMATTING_CELLS:
            raise ValueError(
                f"sheets_read: requested formatting for ~{total_cells} cells, "
                f"which exceeds the {_MAX_FORMATTING_CELLS}-cell limit. Narrow "
                "`ranges` to a specific A1 range (e.g. 'Sheet1!A1:F20') and "
                "try again."
            )
        with drive_tools.friendly_google_errors(account, "this spreadsheet"):
            grid = sheets.spreadsheets().get(
                spreadsheetId=ss_id, ranges=ranges, includeGridData=True,
            ).execute()
        result["sheets_data"] = grid.get("sheets", [])
    else:
        with drive_tools.friendly_google_errors(account, "this spreadsheet"):
            batch = sheets.spreadsheets().values().batchGet(
                spreadsheetId=ss_id, ranges=ranges,
            ).execute()
        result["values"] = [
            {"range": vr.get("range"), "values": vr.get("values", [])}
            for vr in batch.get("valueRanges", [])
        ]

    return result


def write(
    spreadsheet_id_or_url: str | None = None,
    action: str = "update_values",
    range: str | None = None,
    values: list[list] | None = None,
    value_input_option: str = "USER_ENTERED",
    requests: list[dict] | None = None,
    title: str | None = None,
    folder_id: str | None = None,
    account: str | None = None,
) -> dict:
    """Write to a Google Sheet — values, formulas, or raw ``batchUpdate`` requests.

    ``action``: ``update_values`` | ``append_values`` | ``clear_values`` |
    ``batch_update`` | ``create``. See the ``sheets_write`` MCP tool
    docstring for per-action parameters. ``account`` — which Drive to write
    to: ``blt`` (default), ``deadlift``, ``choco``.
    """
    account = resolve_account(account)
    if action not in _VALID_WRITE_ACTIONS:
        raise ValueError(
            f"sheets_write: unknown action {action!r}. Valid actions: "
            f"{', '.join(sorted(_VALID_WRITE_ACTIONS))}."
        )

    if settings.use_mock:
        return _mock({
            "spreadsheet_id": spreadsheet_id_or_url, "action": action,
            "status": "not_modified_mock",
        })

    sheets = _sheets(account)

    if action == "create":
        if not title:
            raise ValueError("sheets_write: action='create' requires `title`.")
        with drive_tools.friendly_google_errors(account, "Google Sheets"):
            created = sheets.spreadsheets().create(
                body={"properties": {"title": title}},
                fields="spreadsheetId,properties.title,sheets.properties",
            ).execute()
        ss_id = created["spreadsheetId"]

        if values:
            first_sheet_title = created["sheets"][0]["properties"]["title"]
            with drive_tools.friendly_google_errors(account, "this spreadsheet"):
                sheets.spreadsheets().values().update(
                    spreadsheetId=ss_id,
                    range=f"'{first_sheet_title}'!A1",
                    valueInputOption=value_input_option,
                    body={"values": values},
                ).execute()

        if folder_id:
            drive = drive_tools._drive(account)
            fid = drive_tools._file_id(folder_id)
            with drive_tools.friendly_google_errors(account, "this spreadsheet"):
                meta = drive.files().get(
                    fileId=ss_id, fields="parents", supportsAllDrives=True,
                ).execute()
                prev = ",".join(meta.get("parents", []) or [])
                drive.files().update(
                    fileId=ss_id, addParents=fid, removeParents=prev,
                    fields="id,parents", supportsAllDrives=True,
                ).execute()

        return {
            "spreadsheet_id": ss_id, "title": title,
            "url": f"https://docs.google.com/spreadsheets/d/{ss_id}/edit",
            "status": "created",
        }

    if not spreadsheet_id_or_url:
        raise ValueError(f"sheets_write: action={action!r} requires `spreadsheet_id_or_url`.")
    ss_id = drive_tools._file_id(spreadsheet_id_or_url)

    if action == "update_values":
        if not range or values is None:
            raise ValueError("sheets_write: action='update_values' requires `range` and `values`.")
        with drive_tools.friendly_google_errors(account, "this spreadsheet"):
            resp = sheets.spreadsheets().values().update(
                spreadsheetId=ss_id, range=range,
                valueInputOption=value_input_option, body={"values": values},
            ).execute()
        return {
            "spreadsheet_id": ss_id, "action": action,
            "updated_range": resp.get("updatedRange"),
            "updated_cells": resp.get("updatedCells"),
            "status": "updated",
        }

    if action == "append_values":
        if not range or values is None:
            raise ValueError("sheets_write: action='append_values' requires `range` and `values`.")
        with drive_tools.friendly_google_errors(account, "this spreadsheet"):
            resp = sheets.spreadsheets().values().append(
                spreadsheetId=ss_id, range=range,
                valueInputOption=value_input_option, insertDataOption="INSERT_ROWS",
                body={"values": values},
            ).execute()
        updates = resp.get("updates", {})
        return {
            "spreadsheet_id": ss_id, "action": action,
            "updated_range": updates.get("updatedRange"),
            "updated_cells": updates.get("updatedCells"),
            "status": "appended",
        }

    if action == "clear_values":
        if not range:
            raise ValueError("sheets_write: action='clear_values' requires `range`.")
        with drive_tools.friendly_google_errors(account, "this spreadsheet"):
            resp = sheets.spreadsheets().values().clear(
                spreadsheetId=ss_id, range=range,
            ).execute()
        return {
            "spreadsheet_id": ss_id, "action": action,
            "cleared_range": resp.get("clearedRange"),
            "status": "cleared",
        }

    # action == "batch_update"
    if not requests:
        raise ValueError("sheets_write: action='batch_update' requires a non-empty `requests` list.")
    with drive_tools.friendly_google_errors(account, "this spreadsheet"):
        resp = sheets.spreadsheets().batchUpdate(
            spreadsheetId=ss_id, body={"requests": requests},
        ).execute()
    return {
        "spreadsheet_id": ss_id, "action": action,
        "applied": len(requests),
        "replies": resp.get("replies", []),
        "status": "updated",
    }
