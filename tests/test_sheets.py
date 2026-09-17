"""Tests for `tools.sheets` — full Sheets API read/write. Fakes only, no network."""

from __future__ import annotations

import json

import pytest

from iblu_keeper.tools import sheets


class _Exec:
    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc

    def execute(self):
        if self._raise is not None:
            raise self._raise
        return self._payload


class _FakeValues:
    def __init__(self, svc):
        self.svc = svc

    def update(self, **kwargs):
        self.svc.calls.append(("values.update", kwargs))
        return _Exec({"updatedRange": kwargs.get("range"), "updatedCells": 3})

    def append(self, **kwargs):
        self.svc.calls.append(("values.append", kwargs))
        return _Exec({"updates": {"updatedRange": kwargs.get("range"), "updatedCells": 2}})

    def clear(self, **kwargs):
        self.svc.calls.append(("values.clear", kwargs))
        return _Exec({"clearedRange": kwargs.get("range")})

    def batchGet(self, **kwargs):  # noqa: N802 - matches googleapiclient method name
        self.svc.calls.append(("values.batchGet", kwargs))
        return _Exec({
            "valueRanges": [{"range": r, "values": [["x"]]} for r in kwargs.get("ranges", [])]
        })


_DEFAULT_META = {
    "properties": {"title": "My Sheet"},
    "sheets": [{"properties": {
        "sheetId": 0, "title": "Sheet1",
        "gridProperties": {"rowCount": 100, "columnCount": 10, "frozenRowCount": 1},
    }}],
}


class _FakeSheetsService:
    def __init__(self, meta=None, get_exc=None):
        self.calls: list[tuple[str, dict]] = []
        self._meta = meta if meta is not None else _DEFAULT_META
        self._get_exc = get_exc

    def spreadsheets(self):
        return self

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        if self._get_exc is not None:
            return _Exec(raise_exc=self._get_exc)
        return _Exec(self._meta)

    def values(self):
        return _FakeValues(self)

    def batchUpdate(self, **kwargs):  # noqa: N802
        self.calls.append(("batchUpdate", kwargs))
        return _Exec({"replies": [{"ok": True}]})

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        title = kwargs["body"]["properties"]["title"]
        return _Exec({
            "spreadsheetId": "NEW123",
            "properties": {"title": title},
            "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
        })


class _Live:
    use_mock = False


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(sheets, "settings", _Live())
    return _Live()


def _install(monkeypatch, fake_service):
    monkeypatch.setattr(sheets, "_sheets", lambda account=None: fake_service)
    return fake_service


# --------------------------------------------------------------------------- #
# read() — metadata, default range, formatting
# --------------------------------------------------------------------------- #
def test_read_default_range_uses_first_sheets_used_range(monkeypatch, live):
    svc = _install(monkeypatch, _FakeSheetsService())
    result = sheets.read("SSID")
    assert result["title"] == "My Sheet"
    assert result["sheets"][0] == {
        "sheet_id": 0, "title": "Sheet1", "row_count": 100, "column_count": 10,
        "frozen_row_count": 1, "frozen_column_count": 0,
    }
    batch_call = [c for c in svc.calls if c[0] == "values.batchGet"][0]
    assert batch_call[1]["ranges"] == ["'Sheet1'!A1:J100"]
    assert result["values"] == [{"range": "'Sheet1'!A1:J100", "values": [["x"]]}]


def test_read_explicit_ranges_are_used_verbatim(monkeypatch, live):
    svc = _install(monkeypatch, _FakeSheetsService())
    sheets.read("SSID", ranges=["Sheet1!A1:B2"])
    batch_call = [c for c in svc.calls if c[0] == "values.batchGet"][0]
    assert batch_call[1]["ranges"] == ["Sheet1!A1:B2"]


def test_read_include_formatting_calls_grid_get(monkeypatch, live):
    svc = _install(monkeypatch, _FakeSheetsService())
    result = sheets.read("SSID", ranges=["Sheet1!A1:B2"], include_formatting=True)
    grid_calls = [c for c in svc.calls if c[0] == "get" and "ranges" in c[1]]
    assert len(grid_calls) == 1
    assert grid_calls[0][1]["includeGridData"] is True
    assert "sheets_data" in result
    assert "values" not in result


def test_read_formatting_over_5000_cells_is_refused(monkeypatch, live):
    _install(monkeypatch, _FakeSheetsService())
    with pytest.raises(ValueError, match="5,000|5000"):
        sheets.read("SSID", ranges=["Sheet1!A1:Z1000"], include_formatting=True)


def test_read_mock_mode_never_touches_network():
    result = sheets.read("SSID")  # DRY_RUN=true globally (conftest)
    assert result["_mock"] is True


# --------------------------------------------------------------------------- #
# write() — each action builds the right API call
# --------------------------------------------------------------------------- #
def test_write_update_values(monkeypatch, live):
    svc = _install(monkeypatch, _FakeSheetsService())
    result = sheets.write(
        spreadsheet_id_or_url="SSID", action="update_values",
        range="Sheet1!A1", values=[[1, 2]],
    )
    call = [c for c in svc.calls if c[0] == "values.update"][0][1]
    assert call["spreadsheetId"] == "SSID"
    assert call["range"] == "Sheet1!A1"
    assert call["valueInputOption"] == "USER_ENTERED"
    assert call["body"] == {"values": [[1, 2]]}
    assert result["status"] == "updated"


def test_write_append_values(monkeypatch, live):
    svc = _install(monkeypatch, _FakeSheetsService())
    sheets.write(spreadsheet_id_or_url="SSID", action="append_values",
                  range="Sheet1!A1", values=[[9]])
    call = [c for c in svc.calls if c[0] == "values.append"][0][1]
    assert call["insertDataOption"] == "INSERT_ROWS"
    assert call["body"] == {"values": [[9]]}


def test_write_clear_values(monkeypatch, live):
    svc = _install(monkeypatch, _FakeSheetsService())
    sheets.write(spreadsheet_id_or_url="SSID", action="clear_values", range="Sheet1!A1:B2")
    call = [c for c in svc.calls if c[0] == "values.clear"][0][1]
    assert call["range"] == "Sheet1!A1:B2"


def test_write_batch_update_passes_requests_through_unmodified(monkeypatch, live):
    svc = _install(monkeypatch, _FakeSheetsService())
    requests = [{"repeatCell": {
        "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
        "fields": "userEnteredFormat.textFormat.bold",
    }}]
    result = sheets.write(spreadsheet_id_or_url="SSID", action="batch_update", requests=requests)
    call = [c for c in svc.calls if c[0] == "batchUpdate"][0][1]
    assert call["body"]["requests"] == requests
    assert result["applied"] == 1


def test_write_create_with_values_and_folder(monkeypatch, live):
    svc = _install(monkeypatch, _FakeSheetsService())

    drive_calls = []

    class _FakeDrive:
        def files(self):
            return self

        def get(self, **kwargs):
            drive_calls.append(("get", kwargs))
            return _Exec({"parents": ["OLD"]})

        def update(self, **kwargs):
            drive_calls.append(("update", kwargs))
            return _Exec({"id": "NEW123", "parents": ["FOLDER1"]})

    from iblu_keeper.tools import drive as drive_tools
    monkeypatch.setattr(drive_tools, "_drive", lambda account=None: _FakeDrive())

    result = sheets.write(
        action="create", title="New Sheet", values=[["a", "b"]], folder_id="FOLDER1",
    )
    assert result["spreadsheet_id"] == "NEW123"
    assert result["status"] == "created"
    update_call = [c for c in svc.calls if c[0] == "values.update"][0][1]
    assert update_call["range"] == "'Sheet1'!A1"
    assert any(c[0] == "update" for c in drive_calls)


def test_write_unknown_action_raises(live):
    with pytest.raises(ValueError, match="unknown action"):
        sheets.write(spreadsheet_id_or_url="SSID", action="delete_everything")


def test_write_update_values_requires_range_and_values(monkeypatch, live):
    _install(monkeypatch, _FakeSheetsService())
    with pytest.raises(ValueError, match="requires"):
        sheets.write(spreadsheet_id_or_url="SSID", action="update_values")


def test_write_mock_mode_never_touches_network():
    result = sheets.write(spreadsheet_id_or_url="SSID", action="update_values",
                           range="A1", values=[[1]])
    assert result["_mock"] is True


# --------------------------------------------------------------------------- #
# Friendly errors — disabled Sheets API 403, generic 403/404
# --------------------------------------------------------------------------- #
def _http_error(status: int, message: str):
    from googleapiclient.errors import HttpError

    class _Resp:
        def __init__(self, status):
            self.status = status
            self.reason = "Error"

    body = json.dumps({"error": {"message": message, "errors": [{"reason": "forbidden"}]}}).encode()
    return HttpError(_Resp(status), body)


def test_disabled_sheets_api_produces_named_message(monkeypatch, live):
    exc = _http_error(
        403,
        "Google Sheets API has not been used in project 12345 before or it "
        "is disabled.",
    )
    _install(monkeypatch, _FakeSheetsService(get_exc=exc))
    with pytest.raises(RuntimeError) as excinfo:
        sheets.read("SSID")
    msg = str(excinfo.value)
    assert "Google Sheets API" in msg
    assert "enabled" in msg
    assert "blt" in msg  # names the account alias


def test_generic_403_names_account_and_suggests_account_param(monkeypatch, live):
    exc = _http_error(403, "The caller does not have permission")
    _install(monkeypatch, _FakeSheetsService(get_exc=exc))
    with pytest.raises(RuntimeError) as excinfo:
        sheets.read("SSID")
    msg = str(excinfo.value)
    assert "blt" in msg
    assert "account" in msg


def test_generic_404_names_account(monkeypatch, live):
    exc = _http_error(404, "Requested entity was not found.")
    _install(monkeypatch, _FakeSheetsService(get_exc=exc))
    with pytest.raises(RuntimeError, match="blt"):
        sheets.read("SSID")


# --------------------------------------------------------------------------- #
# A1 helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("range_str, expected", [
    ("A1", 1),
    ("A1:B2", 4),
    ("Sheet1!A1:C10", 30),
    ("'My Sheet'!A1:J100", 1000),
])
def test_estimate_cells(range_str, expected):
    assert sheets._estimate_cells(range_str) == expected


def test_col_num_roundtrip():
    for n in (1, 2, 26, 27, 52, 703):
        assert sheets._col_to_num(sheets._num_to_col(n)) == n
