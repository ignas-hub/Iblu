"""`tools.analytics` — GA4 reporting by view, one read tool.

Fakes only. `settings` is swapped whole (frozen dataclass, DRY_RUN pinned by
conftest) and the Data/Admin service builders return a `Recorder` that logs
every call's kwargs and answers with canned GA JSON. No network.
"""

from __future__ import annotations

import json

import pytest

from iblu_keeper.tools import analytics as A

from _fake_google import Recorder


class _Live:
    use_mock = False
    primary_alias = "blt"


def _http_error(status: int, message: str, reason: str | None = None):
    from googleapiclient.errors import HttpError

    class _Resp:
        def __init__(self, status):
            self.status = status
            self.reason = "Error"

    err: dict = {"message": message}
    if reason:
        err["errors"] = [{"reason": reason}]
    return HttpError(_Resp(status), json.dumps({"error": err}).encode())


def _report(dims, mets, rows, totals=None, row_count=None):
    """A GA4 runReport response in the shape the API actually sends."""
    resp: dict = {
        "dimensionHeaders": [{"name": d} for d in dims],
        "metricHeaders": [{"name": m, "type": "TYPE_INTEGER"} for m in mets],
        "rows": [
            {
                "dimensionValues": [{"value": v} for v in r[0]],
                "metricValues": [{"value": v} for v in r[1]],
            }
            for r in rows
        ],
        "rowCount": len(rows) if row_count is None else row_count,
    }
    if totals is not None:
        resp["totals"] = [{
            "dimensionValues": [{"value": "RESERVED_TOTAL"}],
            "metricValues": [{"value": v} for v in totals],
        }]
    return resp


LP_METRICS = [
    "sessions", "totalUsers", "newUsers", "engagementRate",
    "averageSessionDuration", "bounceRate", "keyEvents",
]


@pytest.fixture
def live(monkeypatch):
    def _install(data=None, admin=None):
        data_rec = Recorder(data or {})
        admin_rec = Recorder(admin or {})
        monkeypatch.setattr(A, "settings", _Live())
        monkeypatch.setattr(A, "_data", lambda account=None: data_rec)
        monkeypatch.setattr(A, "_admin", lambda account=None: admin_rec)
        return data_rec, admin_rec
    return _install


def _no_api(monkeypatch):
    def fail(account=None):
        raise AssertionError("no service may be built")
    monkeypatch.setattr(A, "_data", fail)
    monkeypatch.setattr(A, "_admin", fail)


# --- dispatch, refusals, mock ------------------------------------------------------


def test_unknown_action_names_the_options():
    """A typo must not fall through to some default report."""
    with pytest.raises(A.AnalyticsError, match="unknown action.*landing_pages"):
        A.analytics_read("landing", property_id="123")


def test_mock_mode_reads_nothing(monkeypatch):
    """The suite runs in DRY_RUN; a mock that built a service would be a live call."""
    _no_api(monkeypatch)
    out = A.analytics_read("landing_pages", property_id="properties/123")
    assert out["_mock"] is True and out["status"] == "not_read_mock"
    assert out["property_id"] == "123" and out["rows"] == []
    assert A.analytics_read("properties")["status"] == "not_read_mock"


def test_property_id_is_checked_before_the_mock_return(monkeypatch):
    """A refusal that only fires live would make the mock suite pass a call the server rejects."""
    _no_api(monkeypatch)
    with pytest.raises(A.AnalyticsError, match="property_id is required.*properties"):
        A.analytics_read("landing_pages")
    with pytest.raises(A.AnalyticsError, match="numeric"):
        A.analytics_read("pages", property_id="My Site")


def test_report_validates_its_lists_before_the_mock_return(monkeypatch):
    """An empty or oversized list is a 400 from GA; refusing it here saves the round trip."""
    _no_api(monkeypatch)
    with pytest.raises(A.AnalyticsError, match="non-empty dimensions"):
        A.analytics_read("report", property_id="1", dimensions=[], metrics=["sessions"])
    with pytest.raises(A.AnalyticsError, match="non-empty metrics"):
        A.analytics_read("report", property_id="1", dimensions=["date"], metrics=None)
    with pytest.raises(A.AnalyticsError, match="at most 9 dimensions"):
        A.analytics_read(
            "report", property_id="1", dimensions=[f"d{i}" for i in range(10)],
            metrics=["sessions"],
        )
    with pytest.raises(A.AnalyticsError, match="does not take path_filter"):
        A.analytics_read("realtime", property_id="1", path_filter="/x")


# --- landing_pages: the request -----------------------------------------------------


def test_landing_pages_body_is_the_pinned_request(live):
    """The dimension, metric set, ordering, limit and range are the whole contract with GA."""
    data, _ = live({"properties.runReport": _report(
        ["landingPagePlusQueryString"], LP_METRICS, [],
    )})
    A.analytics_read("landing_pages", property_id="123", start="7daysAgo", end="yesterday")
    (call,) = data.calls_to("properties.runReport")
    assert call["property"] == "properties/123"
    body = call["body"]
    assert body["dimensions"] == [{"name": "landingPagePlusQueryString"}]
    assert body["metrics"] == [{"name": m} for m in LP_METRICS]
    assert body["orderBys"] == [{"metric": {"metricName": "sessions"}, "desc": True}]
    assert body["limit"] == 25
    assert body["dateRanges"] == [{"startDate": "7daysAgo", "endDate": "yesterday"}]
    assert body["metricAggregations"] == ["TOTAL"]
    assert "dimensionFilter" not in body


def test_path_filter_is_a_contains_on_the_landing_page(live):
    """A wrong matchType (EXACT) silently returns nothing for '/pricing' vs '/pricing?x'."""
    data, _ = live({"properties.runReport": _report(
        ["landingPagePlusQueryString"], LP_METRICS, [],
    )})
    out = A.analytics_read("landing_pages", property_id="123", path_filter="/pricing", limit=5)
    body = data.calls_to("properties.runReport")[0]["body"]
    assert body["dimensionFilter"] == {"filter": {
        "fieldName": "landingPagePlusQueryString",
        "stringFilter": {"matchType": "CONTAINS", "value": "/pricing", "caseSensitive": False},
    }}
    assert body["limit"] == 5 and out["path_filter"] == "/pricing"


def test_rejected_landing_page_dimension_falls_back_to_landing_page(live):
    """If GA refuses landingPagePlusQueryString the filter must move with the dimension, or the retry 400s too."""
    calls = {"n": 0}

    def reply(kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(400, "Field landingPagePlusQueryString is not a valid dimension.")
        return _report(["landingPage"], LP_METRICS, [(["/"], ["9", "8", "7", "0.5", "10", "0.5", "1"])])

    data, _ = live({"properties.runReport": reply})
    out = A.analytics_read("landing_pages", property_id="123", path_filter="/")
    first, second = data.calls_to("properties.runReport")
    assert first["body"]["dimensions"] == [{"name": "landingPagePlusQueryString"}]
    assert second["body"]["dimensions"] == [{"name": "landingPage"}]
    assert second["body"]["dimensionFilter"]["filter"]["fieldName"] == "landingPage"
    assert out["fallback"] == ["GA rejected landingPagePlusQueryString; used landingPage"]
    assert out["rows"][0]["landing_page"] == "/" and out["dimensions"] == ["landingPage"]


def test_rejected_key_events_falls_back_to_conversions(live):
    """Older properties answer only to `conversions`; without the retry they would get no report at all."""
    calls = {"n": 0}

    def reply(kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(400, "Field keyEvents is not a valid metric.")
        return _report(["date"], ["sessions", "totalUsers", "conversions"], [])

    data, _ = live({"properties.runReport": reply})
    out = A.analytics_read("trend", property_id="123")
    second = data.calls_to("properties.runReport")[1]["body"]
    assert second["metrics"] == [{"name": "sessions"}, {"name": "totalUsers"}, {"name": "conversions"}]
    assert out["fallback"] == ["GA rejected keyEvents; used conversions"]


def test_a_400_that_names_no_fallback_is_reported_not_retried(live):
    """Retrying an unrelated 400 forever would hide the actual message from GA."""
    def reply(kwargs):
        raise _http_error(400, "Start date is after end date.")
    live({"properties.runReport": reply})
    with pytest.raises(A.AnalyticsError, match="rejected the request.*Start date"):
        A.analytics_read("landing_pages", property_id="123", start="today", end="yesterday")


# --- row shaping -------------------------------------------------------------------


def test_rows_use_readable_keys_and_ga_numbers_rounded(live):
    """Rates are GA's fractions as percentages, never a quotient computed here; seconds to two places."""
    live({"properties.runReport": _report(
        ["landingPagePlusQueryString"], LP_METRICS,
        [(["/pricing?ref=x"], ["120", "100", "60", "0.6134", "187.456", "0.3866", "7"])],
        totals=["120", "100", "60", "0.6134", "187.456", "0.3866", "7"],
    )})
    out = A.analytics_read("landing_pages", property_id="123")
    assert out["rows"] == [{
        "landing_page": "/pricing?ref=x",
        "sessions": 120, "users": 100, "new_users": 60,
        "engagement_rate_pct": 61.3, "avg_session_seconds": 187.46,
        "bounce_rate_pct": 38.7, "key_events": 7,
    }]
    assert out["row_count"] == 1 and out["matching_rows"] == 1
    assert out["date_range"] == {"start": "28daysAgo", "end": "today"}
    assert out["totals"]["sessions"] == 120 and out["totals"]["engagement_rate_pct"] == 61.3
    assert "note" not in out and out["status"] == "read"


def test_an_absent_metric_is_none_never_zero(live):
    """A 0 in a row GA never sent would be a measurement that was not made."""
    resp = _report(["pagePath"], ["screenPageViews", "totalUsers", "averageSessionDuration", "engagementRate"],
                   [(["/a"], ["10", "", "12.5"])])
    live({"properties.runReport": resp})
    out = A.analytics_read("pages", property_id="123")
    row = out["rows"][0]
    assert row["views"] == 10 and row["users"] is None
    assert row["avg_session_seconds"] == 12.5 and row["engagement_rate_pct"] is None


def test_zero_rows_says_so_instead_of_an_empty_table(live):
    """An empty list with no note reads as a failed call."""
    live({"properties.runReport": _report(["sessionSource", "sessionMedium"],
                                          ["sessions", "totalUsers", "engagementRate", "keyEvents"], [])})
    out = A.analytics_read("sources", property_id="123")
    assert out["rows"] == [] and out["row_count"] == 0
    assert out["note"] == "no data in this range"


def test_totals_only_when_ga_sends_them(live):
    """A totals row invented from the visible rows would be a sum over a truncated table."""
    live({"properties.runReport": _report(["date"], ["sessions", "totalUsers", "keyEvents"],
                                          [(["20261020"], ["5", "4", "1"])], row_count=40)})
    out = A.analytics_read("trend", property_id="123", limit=1)
    assert "totals" not in out and out["matching_rows"] == 40
    assert out["rows"][0]["date"] == "2026-10-20"


def test_trend_is_ascending_by_date_and_a_filter_adds_the_landing_page_dimension(live):
    """GA requires a filtered dimension to be in the report; a trend filtered on an absent field is a 400."""
    data, _ = live({"properties.runReport": _report(["date", "landingPage"],
                                                    ["sessions", "totalUsers", "keyEvents"], [])})
    A.analytics_read("trend", property_id="123", path_filter="/pricing")
    body = data.calls_to("properties.runReport")[0]["body"]
    assert body["dimensions"] == [{"name": "date"}, {"name": "landingPage"}]
    assert body["orderBys"] == [{"dimension": {"dimensionName": "date"}, "desc": False}]
    assert body["dimensionFilter"]["filter"]["fieldName"] == "landingPage"
    assert body["metrics"] == [{"name": "sessions"}, {"name": "totalUsers"}, {"name": "keyEvents"}]


def test_report_passes_the_callers_lists_through(live):
    """The escape hatch must send exactly the names asked for, with the same shaping as the views."""
    data, _ = live({"properties.runReport": _report(
        ["country"], ["sessions", "sessionKeyEventRate"], [(["LT"], ["3", "0.25"])],
    )})
    out = A.analytics_read("report", property_id="123", dimensions="country",
                           metrics=["sessions", "sessionKeyEventRate"], limit=100)
    body = data.calls_to("properties.runReport")[0]["body"]
    assert body["dimensions"] == [{"name": "country"}]
    assert body["metrics"] == [{"name": "sessions"}, {"name": "sessionKeyEventRate"}]
    assert body["orderBys"] == [{"metric": {"metricName": "sessions"}, "desc": True}]
    assert out["rows"] == [{"country": "LT", "sessions": 3, "session_key_event_rate_pct": 25.0}]


# --- realtime ----------------------------------------------------------------------


def test_realtime_uses_the_realtime_endpoint_and_drops_the_dimension_on_rejection(live):
    """runReport cannot answer 'right now'; and a rejected breakdown should still yield the headline number."""
    calls = {"n": 0}

    def reply(kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(400, "Field unifiedScreenName is not a valid dimension.")
        return _report([], ["activeUsers"], [([], ["12"])])

    data, _ = live({"properties.runRealtimeReport": reply})
    out = A.analytics_read("realtime", property_id="123")
    assert data.calls_to("properties.runReport") == []
    first, second = data.calls_to("properties.runRealtimeReport")
    assert first["body"]["dimensions"] == [{"name": "unifiedScreenName"}]
    assert "dimensions" not in second["body"] and "dateRanges" not in second["body"]
    assert out["rows"] == [{"active_users": 12}]
    assert out["fallback"] == ["GA rejected unifiedScreenName; reported without it"]


# --- properties --------------------------------------------------------------------


def test_properties_lists_every_property_with_its_numeric_id(live):
    """The Data API wants the number, not the display name; this is where he finds it."""
    _, admin = live(admin={"accountSummaries.list": {"accountSummaries": [
        {"account": "accounts/11", "displayName": "Blank Label", "propertySummaries": [
            {"property": "properties/123", "displayName": "blanklabel.team", "propertyType": "PROPERTY_TYPE_ORDINARY"},
            {"property": "properties/456", "displayName": "Landing"},
        ]},
        {"account": "accounts/22", "displayName": "Empty"},
    ]}})
    out = A.analytics_read("properties")
    assert admin.calls_to("accountSummaries.list") == [{"pageSize": 200}]
    assert out["count"] == 2 and out["status"] == "read"
    assert out["properties"][0] == {
        "account": "Blank Label", "account_id": "11", "property": "blanklabel.team",
        "property_id": "123", "property_type": "PROPERTY_TYPE_ORDINARY",
    }
    assert out["properties"][1]["property_id"] == "456"


def test_no_properties_is_said_not_shown_as_an_empty_list(live):
    """An empty list after a successful call needs a sentence, or it reads as a scope failure."""
    live(admin={"accountSummaries.list": {}})
    out = A.analytics_read("properties")
    assert out["properties"] == [] and out["note"] == "this token can see no GA4 properties"


# --- the two 403s ------------------------------------------------------------------


def test_insufficient_scope_403_gives_the_reauthorise_steps(live):
    """Until he re-authorises, every call is this 403; the steps are the fix and must be exact."""
    def reply(kwargs):
        raise _http_error(403, "Request had insufficient authentication scopes.", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
    live({"properties.runReport": reply})
    with pytest.raises(A.AnalyticsError) as excinfo:
        A.analytics_read("landing_pages", property_id="123")
    assert str(excinfo.value) == (
        "Analytics needs a scope the token does not hold yet. Set IBLU_ANALYTICS=true "
        "in .env, then run `python scripts/connect_google.py --manual` once (works "
        "from a phone) and restart iblu-mcp.service."
    )


def test_api_not_enabled_403_gives_both_console_links(live):
    """He enabled the UA API believing it was GA; the two GA4 links are what he needs."""
    def reply(kwargs):
        raise _http_error(
            403,
            "Google Analytics Admin API has not been used in project 751405989189 "
            "before or it is disabled.",
            "accessNotConfigured",
        )
    live(admin={"accountSummaries.list": reply})
    with pytest.raises(A.AnalyticsError) as excinfo:
        A.analytics_read("properties")
    assert str(excinfo.value) == (
        "The Google Analytics Data API or Admin API is not enabled in the Cloud "
        "project — enable both at console.developers.google.com/apis/api/"
        "analyticsdata.googleapis.com/overview?project=751405989189 and "
        ".../analyticsadmin.googleapis.com/overview?project=751405989189."
    )


def test_a_plain_403_names_the_account_and_points_at_properties(live):
    """A property in another Workspace is a bare 403; the alias and the listing are the way out."""
    def reply(kwargs):
        raise _http_error(403, "User does not have sufficient permissions for this property.")
    live({"properties.runReport": reply})
    with pytest.raises(A.AnalyticsError, match="'blt'.*property 123.*properties"):
        A.analytics_read("landing_pages", property_id="123")
