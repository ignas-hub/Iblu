"""Tests for `tools.sheets_manage` — named reshaping verbs for a Sheet. Fakes only."""

from __future__ import annotations

import pytest

from iblu_keeper.tools import sheets_manage as sm


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
        return _Exec({"values": self.svc.rows})

    def clear(self, **kwargs):
        self.svc.calls.append(("values.clear", kwargs))
        return _Exec({"clearedRange": kwargs.get("range")})

    def append(self, **kwargs):
        self.svc.calls.append(("values.append", kwargs))
        return _Exec({"updates": {"updatedRange": "'Data'!A5:B6", "updatedCells": 4}})


def _tab(title, sheet_id, rows=1000, cols=26):
    return {"properties": {"sheetId": sheet_id, "title": title, "index": sheet_id,
                           "gridProperties": {"rowCount": rows, "columnCount": cols}}}


class _FakeSheets:
    def __init__(self, meta=None, rows=None, replies=None):
        self.calls: list[tuple[str, dict]] = []
        self.meta = meta if meta is not None else {"sheets": [_tab("Data", 0), _tab("Notes", 1)]}
        self.rows = rows if rows is not None else [["h1", "h2"], ["b", 2], ["a", 1]]
        self.replies = replies if replies is not None else [{}]

    def spreadsheets(self):
        return self

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return _Exec(self.meta)

    def values(self):
        return _FakeValues(self)

    def batchUpdate(self, **kwargs):  # noqa: N802
        self.calls.append(("batchUpdate", kwargs))
        return _Exec({"replies": self.replies})

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        first = (kwargs["body"].get("sheets") or [{"properties": {"title": "Sheet1"}}])[0]
        return _Exec({"spreadsheetId": "NEW1", "sheets": [first]})


class _Live:
    use_mock = False


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(sm, "settings", _Live())


def _install(monkeypatch, fake):
    monkeypatch.setattr(sm, "_sheets", lambda account=None: fake)
    return fake


def _requests(fake):
    return [c[1]["body"]["requests"] for c in fake.calls if c[0] == "batchUpdate"]


def _writes(fake):
    return [c for c in fake.calls if c[0] in ("batchUpdate", "values.clear", "values.append", "create")]


def test_unknown_action_raises_naming_the_valid_ones(live):
    """A typo'd action must not fall through to the last branch (sort)."""
    with pytest.raises(ValueError, match="unknown action.*create"):
        sm.sheets_manage("sortt", "SSID")


@pytest.mark.parametrize("action", ["delete_tab", "delete", "delete_spreadsheet"])
def test_delete_is_refused_by_name_and_points_at_drive(monkeypatch, live, action):
    """An unknown-action error for 'delete_tab' reads as 'not yet'; it must read as 'never'."""
    fake = _install(monkeypatch, _FakeSheets())
    with pytest.raises(ValueError, match="deliberately not implemented.*Drive"):
        sm.sheets_manage(action, "SSID", tab="Data")
    assert fake.calls == []


def test_mock_mode_writes_nothing(monkeypatch):
    """DRY_RUN must mean no network; a service build here is a leak."""
    def _boom(account=None):
        raise AssertionError("service built in mock mode")
    monkeypatch.setattr(sm, "_sheets", _boom)
    assert sm.sheets_manage("add_tab", "SSID", tab="X")["status"] == "not_modified_mock"
    assert sm.sheets_manage("create", title="T")["status"] == "not_created_mock"


def test_create_sends_title_and_optional_first_tab(monkeypatch, live):
    """Without the first tab in the create body, a 'Sheet1' tab has to be renamed afterwards."""
    fake = _install(monkeypatch, _FakeSheets())
    result = sm.sheets_manage("create", title="Ledger", tab="2026")
    body = [c[1]["body"] for c in fake.calls if c[0] == "create"][0]
    assert body == {"properties": {"title": "Ledger"}, "sheets": [{"properties": {"title": "2026"}}]}
    assert result["spreadsheet_id"] == "NEW1"
    assert result["url"] == "https://docs.google.com/spreadsheets/d/NEW1/edit"
    assert result["first_tab"] == "2026"
    assert result["status"] == "created"


def test_non_create_actions_require_a_spreadsheet(live):
    """Without an id the API error is a bare 400; the message should name the parameter."""
    with pytest.raises(ValueError, match="spreadsheet_id_or_url"):
        sm.sheets_manage("add_tab", tab="X")


def test_add_tab_sends_addsheet_and_returns_the_new_id(monkeypatch, live):
    """The caller needs the new sheetId for any follow-up batchUpdate."""
    fake = _install(monkeypatch, _FakeSheets(replies=[{"addSheet": {"properties": {"sheetId": 42}}}]))
    result = sm.sheets_manage("add_tab", "SSID", tab="Q4")
    assert _requests(fake) == [[{"addSheet": {"properties": {"title": "Q4"}}}]]
    assert result["sheet_id"] == 42
    assert result["status"] == "added"


def test_add_tab_refuses_a_duplicate_case_insensitively(monkeypatch, live):
    """Google would reject 'Data' but happily make 'data'; two near-identical tabs is a mess."""
    fake = _install(monkeypatch, _FakeSheets())
    with pytest.raises(ValueError, match="already exists"):
        sm.sheets_manage("add_tab", "SSID", tab="data")
    assert _writes(fake) == []


def test_rename_tab_updates_only_the_title_field(monkeypatch, live):
    """A fields mask wider than 'title' would reset grid properties to defaults."""
    fake = _install(monkeypatch, _FakeSheets())
    result = sm.sheets_manage("rename_tab", "SSID", tab="Notes", new_title="Archive")
    assert _requests(fake) == [[{"updateSheetProperties": {
        "properties": {"sheetId": 1, "title": "Archive"}, "fields": "title",
    }}]]
    assert result["status"] == "renamed"
    with pytest.raises(ValueError, match="already exists"):
        sm.sheets_manage("rename_tab", "SSID", tab="Notes", new_title="Data")


def test_clear_bounded_range_clears_without_confirm(monkeypatch, live):
    """A bounded range is a decision; asking for confirm on it would train 'always say yes'."""
    fake = _install(monkeypatch, _FakeSheets())
    result = sm.sheets_manage("clear", "SSID", range="Notes!A2:C10")
    call = [c[1] for c in fake.calls if c[0] == "values.clear"][0]
    assert call["range"] == "'Notes'!A2:C10"
    assert result["confirmed"] is False
    assert result["status"] == "cleared"


@pytest.mark.parametrize("kwargs", [
    {"tab": "Data"}, {"range": "Data"}, {"range": "'Data'!A:C"}, {"range": "Data!1:1000"},
])
def test_clear_whole_tab_or_open_range_requires_confirm(monkeypatch, live, kwargs):
    """A missing cell part is usually a typo; clearing the whole tab on it is unrecoverable."""
    fake = _install(monkeypatch, _FakeSheets())
    with pytest.raises(ValueError, match="confirm=True"):
        sm.sheets_manage("clear", "SSID", **kwargs)
    assert _writes(fake) == []


def test_clear_whole_tab_with_confirm_clears_the_quoted_tab(monkeypatch, live):
    """With confirm the clear must hit the whole tab, not a default first-tab range."""
    fake = _install(monkeypatch, _FakeSheets())
    result = sm.sheets_manage("clear", "SSID", tab="Notes", confirm=True)
    call = [c[1] for c in fake.calls if c[0] == "values.clear"][0]
    assert call["range"] == "'Notes'"
    assert result["confirmed"] is True


def test_clear_over_50k_cells_is_refused_even_with_confirm(monkeypatch, live):
    """The cap is a ceiling, not a prompt; confirm must not lift it."""
    fake = _install(monkeypatch, _FakeSheets(meta={"sheets": [_tab("Big", 0, rows=5000, cols=26)]}))
    with pytest.raises(ValueError, match="50000-cell"):
        sm.sheets_manage("clear", "SSID", range="Big!A1:Z2000")
    with pytest.raises(ValueError, match="50000-cell"):
        sm.sheets_manage("clear", "SSID", tab="Big", confirm=True)
    assert _writes(fake) == []


def test_append_uses_user_entered_and_insert_rows(monkeypatch, live):
    """RAW would store '=SUM(A1)' as text and '2026-01-01' as a string."""
    fake = _install(monkeypatch, _FakeSheets())
    result = sm.sheets_manage("append", "SSID", tab="Data", rows=[["x", 1], ["y", 2]])
    call = [c[1] for c in fake.calls if c[0] == "values.append"][0]
    assert call["range"] == "'Data'"
    assert call["valueInputOption"] == "USER_ENTERED"
    assert call["insertDataOption"] == "INSERT_ROWS"
    assert call["body"] == {"values": [["x", 1], ["y", 2]]}
    assert result["rows_appended"] == 2
    assert result["status"] == "appended"


def test_append_over_50k_cells_is_refused_before_sending(monkeypatch, live):
    """A runaway rows list must be refused on size, not sent and half-applied."""
    fake = _install(monkeypatch, _FakeSheets())
    with pytest.raises(ValueError, match="50000-cell"):
        sm.sheets_manage("append", "SSID", tab="Data", rows=[[0] * 26] * 2000)
    assert _writes(fake) == []


def test_find_replace_body_and_occurrences(monkeypatch, live):
    """Without allSheets the API searches nothing; without the reply count the caller is blind."""
    fake = _install(monkeypatch, _FakeSheets(replies=[{"findReplace": {"occurrencesChanged": 3}}]))
    result = sm.sheets_manage("find_replace", "SSID", find="BLT", replace="Blank Label")
    assert _requests(fake)[0] == [{"findReplace": {
        "find": "BLT", "replacement": "Blank Label", "matchCase": False, "allSheets": True,
    }}]
    assert result["occurrences_changed"] == 3
    sm.sheets_manage("find_replace", "SSID", find="a", replace="", tab="Notes", match_case=True)
    assert _requests(fake)[1][0]["findReplace"] == {
        "find": "a", "replacement": "", "matchCase": True, "sheetId": 1,
    }


def test_insert_rows_is_zero_based_and_inherits_from_before(monkeypatch, live):
    """`at=3` means 'before row 3'; an off-by-one puts the blank rows in the wrong place."""
    fake = _install(monkeypatch, _FakeSheets())
    result = sm.sheets_manage("insert_rows", "SSID", tab="Data", at=3, count=2)
    assert _requests(fake) == [[{"insertDimension": {
        "range": {"sheetId": 0, "dimension": "ROWS", "startIndex": 2, "endIndex": 4},
        "inheritFromBefore": True,
    }}]]
    assert result["status"] == "inserted"


def test_insert_columns_takes_a_letter(monkeypatch, live):
    """Column 'C' is index 2; a 1-based index would shift every later column by one."""
    fake = _install(monkeypatch, _FakeSheets())
    sm.sheets_manage("insert_columns", "SSID", tab="Data", column="C", count=1)
    assert _requests(fake)[0][0]["insertDimension"]["range"] == {
        "sheetId": 0, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3,
    }
    with pytest.raises(ValueError, match="letter"):
        sm.sheets_manage("insert_columns", "SSID", tab="Data", column="3")


def test_insert_rows_over_50k_cells_is_refused(monkeypatch, live):
    """10,000 rows x 26 columns is 260k new cells; the cap must count the other dimension."""
    fake = _install(monkeypatch, _FakeSheets())
    with pytest.raises(ValueError, match="50000-cell"):
        sm.sheets_manage("insert_rows", "SSID", tab="Data", at=1, count=10_000)
    assert _writes(fake) == []


def test_freeze_masks_only_what_was_given(monkeypatch, live):
    """Sending frozenColumnCount=0 when only rows were asked for would unfreeze columns."""
    fake = _install(monkeypatch, _FakeSheets())
    sm.sheets_manage("freeze", "SSID", tab="Data", freeze_rows=2)
    assert _requests(fake)[0] == [{"updateSheetProperties": {
        "properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 2}},
        "fields": "gridProperties.frozenRowCount",
    }}]
    sm.sheets_manage("freeze", "SSID", tab="Data", freeze_rows=1, freeze_cols=1)
    assert _requests(fake)[1][0]["updateSheetProperties"]["fields"] == (
        "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"
    )


def test_format_header_bolds_row_one_and_freezes_it(monkeypatch, live):
    """The one common action: both halves in one batch, or it is two tools again."""
    fake = _install(monkeypatch, _FakeSheets())
    result = sm.sheets_manage("format_header", "SSID", tab="Notes")
    assert _requests(fake) == [[
        {"repeatCell": {
            "range": {"sheetId": 1, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }},
        {"updateSheetProperties": {
            "properties": {"sheetId": 1, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
    ]]
    assert result["status"] == "formatted"


def test_set_column_width_targets_one_column(monkeypatch, live):
    """An endIndex off by one resizes the neighbour too."""
    fake = _install(monkeypatch, _FakeSheets())
    sm.sheets_manage("set_column_width", "SSID", tab="Data", column="b", pixels=180)
    assert _requests(fake) == [[{"updateDimensionProperties": {
        "range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
        "properties": {"pixelSize": 180}, "fields": "pixelSize",
    }}]]


def test_sort_bounds_to_the_used_range_and_keeps_the_header(monkeypatch, live):
    """Sorting the allocated grid drags blank rows to the top; sorting row 1 loses the header."""
    fake = _install(monkeypatch, _FakeSheets(rows=[["h1", "h2"], ["b", 2], ["a", 1]]))
    result = sm.sheets_manage("sort", "SSID", tab="Data", column="B", ascending=False)
    assert _requests(fake) == [[{"sortRange": {
        "range": {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 3,
                  "startColumnIndex": 0, "endColumnIndex": 2},
        "sortSpecs": [{"dimensionIndex": 1, "sortOrder": "DESCENDING"}],
    }}]]
    assert result["rows_sorted"] == 2
    sm.sheets_manage("sort", "SSID", tab="Data", column="A", has_header=False)
    assert _requests(fake)[1][0]["sortRange"]["range"]["startRowIndex"] == 0


def test_sort_over_50k_used_cells_is_refused(monkeypatch, live):
    """The cap applies to data actually rearranged, measured, not the grid."""
    fake = _install(monkeypatch, _FakeSheets(rows=[[0] * 26] * 2001))
    with pytest.raises(ValueError, match="50000-cell"):
        sm.sheets_manage("sort", "SSID", tab="Data", column="A")
    assert _writes(fake) == []


def test_sort_column_outside_used_range_is_refused(monkeypatch, live):
    """Sorting by an empty column reorders the data by nothing and looks like success."""
    fake = _install(monkeypatch, _FakeSheets())
    with pytest.raises(ValueError, match="outside the used range"):
        sm.sheets_manage("sort", "SSID", tab="Data", column="Z")
    assert _writes(fake) == []
