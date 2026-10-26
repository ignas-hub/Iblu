"""Tests for `tools.sheets_read` — structure and search of a Sheet. Fakes only."""

from __future__ import annotations

import pytest

from iblu_keeper.tools import sheets_read as sr


class _Exec:
    def __init__(self, payload=None):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeValues:
    def __init__(self, svc):
        self.svc = svc

    def get(self, **kwargs):
        self.svc.calls.append(("values.get", kwargs))
        rng = kwargs.get("range", "")
        tab = rng.split("!")[0].strip("'")
        return _Exec({"values": self.svc.rows.get(tab, [])})


def _tab(title, sheet_id, rows=100, cols=10, hidden=False):
    p = {"sheetId": sheet_id, "title": title, "index": sheet_id,
         "gridProperties": {"rowCount": rows, "columnCount": cols}}
    if hidden:
        p["hidden"] = True
    return {"properties": p}


class _FakeSheets:
    def __init__(self, meta=None, rows=None):
        self.calls: list[tuple[str, dict]] = []
        self.meta = meta if meta is not None else {
            "properties": {"title": "Budget", "locale": "en_GB", "timeZone": "Europe/Zagreb"},
            "sheets": [_tab("Data", 0), _tab("Notes", 1, hidden=True)],
        }
        self.rows = rows or {}

    def spreadsheets(self):
        return self

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return _Exec(self.meta)

    def values(self):
        return _FakeValues(self)


class _Live:
    use_mock = False


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(sr, "settings", _Live())


def _install(monkeypatch, fake):
    monkeypatch.setattr(sr, "_sheets", lambda account=None: fake)
    return fake


def _calls(fake, name):
    return [c[1] for c in fake.calls if c[0] == name]


def test_unknown_action_raises_naming_the_valid_ones(live):
    """A typo'd action would otherwise fall through to whichever branch came last."""
    with pytest.raises(ValueError, match="unknown action.*tabs"):
        sr.sheets_read("tab", "SSID")


def test_mock_mode_never_builds_a_service(monkeypatch):
    """DRY_RUN must mean no network; a service build here is a leak."""
    def _boom(account=None):
        raise AssertionError("service built in mock mode")
    monkeypatch.setattr(sr, "_sheets", _boom)
    result = sr.sheets_read("tabs", "SSID")
    assert result["_mock"] is True
    assert result["status"] == "not_read_mock"


def test_tabs_lists_every_tab_with_grid_and_hidden(monkeypatch, live):
    """Without hidden and grid size the caller cannot tell a 2-row tab from a 20k one."""
    _install(monkeypatch, _FakeSheets())
    result = sr.sheets_read("tabs", "SSID")
    assert result["count"] == 2
    assert result["tabs"][0] == {"title": "Data", "sheet_id": 0, "index": 0,
                                 "rows": 100, "cols": 10, "hidden": False}
    assert result["tabs"][1]["hidden"] is True
    assert result["status"] == "read"


def test_info_returns_locale_timezone_and_tabs(monkeypatch, live):
    """Dates in a sheet mean nothing without its locale and timezone."""
    fake = _install(monkeypatch, _FakeSheets())
    result = sr.sheets_read("info", "https://docs.google.com/spreadsheets/d/ABC123/edit#gid=0")
    assert result["spreadsheet_id"] == "ABC123"
    assert result["title"] == "Budget"
    assert result["locale"] == "en_GB"
    assert result["timezone"] == "Europe/Zagreb"
    assert result["tab_count"] == 2
    assert "properties(title,locale,timeZone)" in _calls(fake, "get")[0]["fields"]


def test_find_searches_every_tab_case_insensitively_and_reports_cells(monkeypatch, live):
    """A search that misses 'Q3' because the cell says 'q3' is worse than none."""
    _install(monkeypatch, _FakeSheets(rows={
        "Data": [["Name", "Total"], ["Invoice q3", 100]],
        "Notes": [[], ["", "see Q3 invoice"]],
    }))
    result = sr.sheets_read("find", "SSID", query="Q3")
    assert result["count"] == 2
    assert result["matches"][0] == {"tab": "Data", "cell": "A2", "value": "Invoice q3"}
    assert result["matches"][1] == {"tab": "Notes", "cell": "B2", "value": "see Q3 invoice"}
    assert result["truncated"] is False
    assert result["skipped"] == []


def test_find_fetches_the_tabs_grid_as_its_range(monkeypatch, live):
    """An unbounded range would pull every allocated cell; the grid is the bound."""
    fake = _install(monkeypatch, _FakeSheets())
    sr.sheets_read("find", "SSID", query="x", tab="Data")
    assert _calls(fake, "values.get")[0]["range"] == "'Data'!A1:J100"


def test_find_skips_a_tab_over_the_cell_cap_and_names_it(monkeypatch, live):
    """Silently skipping a tab makes 'not found' a lie; the skip must be reported."""
    fake = _install(monkeypatch, _FakeSheets(meta={
        "sheets": [_tab("Small", 0), _tab("Huge", 1, rows=100_000, cols=26)],
    }, rows={"Small": [["needle"]]}))
    result = sr.sheets_read("find", "SSID", query="needle")
    assert result["count"] == 1
    assert result["skipped"] == [{"tab": "Huge", "cells": 2_600_000,
                                  "reason": "over the 50000-cell search limit"}]
    assert all("Huge" not in c["range"] for c in _calls(fake, "values.get"))


def test_find_refuses_when_the_named_tab_is_over_the_cap(monkeypatch, live):
    """Asked for one tab explicitly, a skip would return an empty list that looks like 'no match'."""
    fake = _install(monkeypatch, _FakeSheets(meta={
        "sheets": [_tab("Huge", 1, rows=100_000, cols=26)],
    }))
    with pytest.raises(ValueError, match="50000-cell"):
        sr.sheets_read("find", "SSID", query="needle", tab="Huge")
    assert _calls(fake, "values.get") == []


def test_find_stops_at_100_matches_and_says_truncated(monkeypatch, live):
    """A sheet full of the query would otherwise return thousands of rows."""
    _install(monkeypatch, _FakeSheets(rows={"Data": [["hit"] * 10 for _ in range(50)]}))
    result = sr.sheets_read("find", "SSID", query="hit", tab="Data")
    assert result["count"] == 100
    assert result["truncated"] is True


def test_find_requires_a_query(monkeypatch, live):
    """An empty substring matches every cell, which is 100 rows of nothing useful."""
    _install(monkeypatch, _FakeSheets())
    with pytest.raises(ValueError, match="query"):
        sr.sheets_read("find", "SSID", query="  ")


def test_headers_reads_row_one_of_the_named_tab(monkeypatch, live):
    """Headers from the wrong tab, or the wrong row, would mislabel every column."""
    fake = _install(monkeypatch, _FakeSheets(rows={"Notes": [["Who", "What", "When"]]}))
    result = sr.sheets_read("headers", "SSID", tab="notes")
    assert _calls(fake, "values.get")[0]["range"] == "'Notes'!1:1"
    assert result["tab"] == "Notes"
    assert result["headers"] == ["Who", "What", "When"]
    assert result["columns"] == {"A": "Who", "B": "What", "C": "When"}


def test_headers_default_to_the_first_tab_and_unknown_tab_names_the_tabs(monkeypatch, live):
    """A bad tab name should cost one correction, not a separate `tabs` call."""
    fake = _install(monkeypatch, _FakeSheets(rows={"Data": [["A"]]}))
    assert sr.sheets_read("headers", "SSID")["tab"] == "Data"
    with pytest.raises(ValueError, match="'Data', 'Notes'"):
        sr.sheets_read("headers", "SSID", tab="Nope")
    assert len(_calls(fake, "values.get")) == 1


def test_named_ranges_are_rendered_in_a1(monkeypatch, live):
    """A GridRange is useless to the caller; A1 is what the next call takes."""
    _install(monkeypatch, _FakeSheets(meta={
        "sheets": [_tab("Data", 7)],
        "namedRanges": [
            {"namedRangeId": "nr1", "name": "totals",
             "range": {"sheetId": 7, "startRowIndex": 1, "endRowIndex": 5,
                       "startColumnIndex": 0, "endColumnIndex": 3}},
            {"namedRangeId": "nr2", "name": "whole_cols",
             "range": {"sheetId": 7, "startColumnIndex": 1, "endColumnIndex": 2}},
        ],
    }))
    result = sr.sheets_read("named_ranges", "SSID")
    assert result["named_ranges"] == [
        {"name": "totals", "range": "'Data'!A2:C5", "named_range_id": "nr1"},
        {"name": "whole_cols", "range": "'Data'!B:B", "named_range_id": "nr2"},
    ]


def test_preview_requests_the_first_n_rows_and_caps_at_200(monkeypatch, live):
    """Default 10 keeps a preview small; a cap stops `rows=1e6` becoming a full dump."""
    fake = _install(monkeypatch, _FakeSheets(rows={"Data": [[1], [2], [3]]}))
    result = sr.sheets_read("preview", "SSID")
    assert _calls(fake, "values.get")[0]["range"] == "'Data'!1:10"
    assert result["rows"] == [[1], [2], [3]]
    assert result["row_count"] == 3
    sr.sheets_read("preview", "SSID", rows=1_000_000)
    assert _calls(fake, "values.get")[1]["range"] == "'Data'!1:200"


def test_tab_with_a_quote_is_quoted_safely(monkeypatch, live):
    """A tab called Ignas's would break the A1 prefix without doubling the quote."""
    fake = _install(monkeypatch, _FakeSheets(meta={"sheets": [_tab("Ignas's", 0)]}))
    sr.sheets_read("preview", "SSID", tab="Ignas's", rows=1)
    assert _calls(fake, "values.get")[0]["range"] == "'Ignas''s'!1:1"
