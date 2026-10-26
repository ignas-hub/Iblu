"""Google Analytics 4 reporting: landing pages, pages, sources, a trend, realtime.

Ignas asked to "analyze landing page performance". That is a question for the
GA4 **Data API** (``analyticsdata`` v1beta) — ``runReport`` over the landing
page dimension — plus the **Admin API** (``analyticsadmin`` v1beta) once, to
find the numeric property id the Data API wants. The first attempt enabled the
*Google Analytics API* in the Cloud project, which is the Universal Analytics
(GA3) surface and answers nothing for a GA4 property; the two GA4 APIs are what
this module calls, and a 403 saying either is not enabled is translated into
the two console links that fix it.

The scope. ``analytics.readonly`` is not in the primary token today. Adding a
scope to a token's request is the direction that breaks: the moment it appears
in ``scopes_for('blt')``, every refresh of the existing token fails with "Not
all requested scopes were granted" until he signs in again (the Forms lesson,
``google_auth.OPTIONAL_SCOPES``). So it is gated behind ``IBLU_ANALYTICS=true``
in ``.env``, which he flips immediately before running
``scripts/connect_google.py --manual`` once. Until then every call here is a
403 with "insufficient scopes", and that 403 is turned into those exact
instructions rather than a stack trace.

ONE MCP tool with an ``action`` parameter, per HANDOFF.md §12 (one tool per
domain × side-effect class): a new ``@mcp.tool`` costs a permission click
forever, so the views share an entry point. There is no write tool, and not
only because nothing has been asked for: a GA4 report is a read of someone
else's visitors, there is nothing in it IBLU should change, and the token
holds the *readonly* scope so that it could not even if a prompt tried.

Numbers are GA's numbers. Nothing here divides one metric by another to make a
rate; ``engagementRate`` and ``bounceRate`` are GA's fractions shown as
percentages, a metric GA did not send for a row is ``None`` and never ``0``,
and ``totals`` appears only when GA returned one. Zero rows is reported as
``rows: []`` with a note, because an empty table looks like a failure and is
not one. GA4 renamed ``conversions`` to ``keyEvents``; the newer name is tried
first and the older one used if the API rejects it. Likewise
``landingPagePlusQueryString`` falls back to ``landingPage``, and realtime's
``unifiedScreenName`` to no dimension at all; the result names what was used.

The honest limit. ``analytics.readonly`` is a Google *Cloud* API scope on the
same footing as ``pubsub``, and whether BLT's Workspace puts it under "Google
Cloud session control" (periodic forced reauthentication, surfacing as
``invalid_rapt`` on refresh — see ``google_auth``) is not knowable from here.
The first ``invalid_rapt`` on the primary after this scope is granted is the
test. If it comes, the answer is a separate token for Analytics, not removing
the scope from a token that has started working.

What each action reads:

  properties     Admin ``accountSummaries.list``: every GA4 property the token
                 can see, with the numeric id the other actions need.
  landing_pages  ``runReport`` by landingPagePlusQueryString: sessions, users,
                 new users, engagement rate, average session duration, bounce
                 rate, key events. Sessions descending. ``path_filter`` is a
                 CONTAINS filter on the landing page.
  pages          By pagePath: views, users, average session duration,
                 engagement rate. ``path_filter`` on the path.
  sources        By sessionSource + sessionMedium: sessions, users, engagement
                 rate, key events. ``path_filter`` adds landingPage as a
                 dimension and filters on it (GA requires a filtered dimension
                 to be in the report).
  trend          By date, ascending: sessions, users, key events.
                 ``path_filter`` as for sources — one landing page over time.
  realtime       ``runRealtimeReport``: active users right now by screen/page.
  report         Caller-supplied ``dimensions`` and ``metrics`` (API names;
                 at most 9 and 10), same range, limit and row shaping.
"""

from __future__ import annotations

import copy
import logging
import re

from ..config import resolve_account, settings
from .drive import _http_detail, _http_status

logger = logging.getLogger("iblu_keeper.tools.analytics")

ACTIONS = (
    "properties", "landing_pages", "pages", "sources", "trend", "realtime", "report",
)
NEEDS_PROPERTY = tuple(a for a in ACTIONS if a != "properties")
PATH_FILTERABLE = ("landing_pages", "pages", "sources", "trend")
MAX_DIMENSIONS = 9
MAX_METRICS = 10
MAX_LIMIT = 10000
CLOUD_PROJECT = "751405989189"

SCOPE_GUIDANCE = (
    "Analytics needs a scope the token does not hold yet. Set IBLU_ANALYTICS=true "
    "in .env, then run `python scripts/connect_google.py --manual` once (works "
    "from a phone) and restart iblu-mcp.service."
)
API_GUIDANCE = (
    "The Google Analytics Data API or Admin API is not enabled in the Cloud "
    "project — enable both at console.developers.google.com/apis/api/"
    f"analyticsdata.googleapis.com/overview?project={CLOUD_PROJECT} and "
    f".../analyticsadmin.googleapis.com/overview?project={CLOUD_PROJECT}."
)

# The newer GA4 name first; the older one if the API says the field is not
# valid. ``None`` means "drop the dimension" (realtime without a breakdown).
_FALLBACKS: dict[str, str | None] = {
    "landingPagePlusQueryString": "landingPage",
    "keyEvents": "conversions",
    "unifiedScreenName": None,
}

# API name -> the key a human reads. Anything not listed is snake_cased.
_READABLE = {
    "landingPagePlusQueryString": "landing_page",
    "landingPage": "landing_page",
    "pagePath": "page",
    "sessionSource": "source",
    "sessionMedium": "medium",
    "unifiedScreenName": "screen",
    "date": "date",
    "sessions": "sessions",
    "totalUsers": "users",
    "newUsers": "new_users",
    "activeUsers": "active_users",
    "screenPageViews": "views",
    "engagementRate": "engagement_rate_pct",
    "bounceRate": "bounce_rate_pct",
    "averageSessionDuration": "avg_session_seconds",
    "keyEvents": "key_events",
    "conversions": "conversions",
}

_PROPERTY_RE = re.compile(r"^(?:properties/)?(\d+)$")


class AnalyticsError(ValueError):
    """Bad input to `analytics_read`, or GA refusing it. Always says what to do."""


def _data(account: str | None = None):
    from ..google_auth import build_service

    return build_service("analyticsdata", "v1beta", account=account)


def _admin(account: str | None = None):
    from ..google_auth import build_service

    return build_service("analyticsadmin", "v1beta", account=account)


def _mock(payload: dict) -> dict:
    return {"_mock": True, "note": "MOCK MODE — Analytics NOT read.", **payload}


# --------------------------------------------------------------------------- #
# Errors GA sends, and what each one means for him
# --------------------------------------------------------------------------- #
def _reasons(exc) -> str:
    """The ``errors[].reason`` / ``status`` strings from the body, lower-cased."""
    try:
        import json as _json

        content = getattr(exc, "content", None)
        body = (_json.loads(content.decode("utf-8")) if content else {}).get("error") or {}
        parts = [str(body.get("status") or "")]
        parts += [str(e.get("reason") or "") for e in body.get("errors") or []]
        return " ".join(parts).lower()
    except Exception:  # noqa: BLE001
        return ""


def _friendly(exc: Exception, account: str | None, what: str) -> AnalyticsError:
    """Translate a GA HttpError into the sentence that fixes it."""
    alias = account or settings.primary_alias
    status = _http_status(exc)
    detail = _http_detail(exc)
    low = f"{detail} {_reasons(exc)}".lower()
    if status == 403 and (
        "has not been used" in low or "is disabled" in low or "accessnotconfigured" in low
    ):
        return AnalyticsError(API_GUIDANCE)
    if status == 403 and ("insufficient" in low or "scope" in low):
        return AnalyticsError(SCOPE_GUIDANCE)
    if status in (403, 404):
        return AnalyticsError(
            f"The '{alias}' account may not have access to {what} (HTTP {status}). "
            "Use action='properties' to list the GA4 properties this token can "
            "see, or pass the `account` parameter to try another account. "
            f"Google said: {detail or exc}"
        )
    if status == 400:
        return AnalyticsError(f"Google Analytics rejected the request: {detail or exc}")
    return AnalyticsError(
        f"Google Analytics error reading {what} (HTTP {status or '?'}): {detail or exc}"
    )


# --------------------------------------------------------------------------- #
# Request shaping
# --------------------------------------------------------------------------- #
def _property_id(value) -> str:
    if value is None or not str(value).strip():
        raise AnalyticsError(
            "property_id is required — use action='properties' to find it"
        )
    m = _PROPERTY_RE.match(str(value).strip())
    if not m:
        raise AnalyticsError(
            f"property_id must be the numeric GA4 property id (got {value!r}); "
            "use action='properties' to find it"
        )
    return m.group(1)


def _names(value, what: str, cap: int) -> list[str]:
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = []
    items = [v for v in items if v]
    if not items:
        raise AnalyticsError(f"report needs a non-empty {what} list of GA4 API names")
    if len(items) > cap:
        raise AnalyticsError(f"report allows at most {cap} {what} (got {len(items)})")
    return items


def _contains(field: str, value: str) -> dict:
    return {"filter": {
        "fieldName": field,
        "stringFilter": {"matchType": "CONTAINS", "value": value, "caseSensitive": False},
    }}


def _body(
    dimensions: list[str], metrics: list[str], start: str, end: str, limit: int,
    order: dict, filter_field: str | None = None, path_filter: str | None = None,
) -> dict:
    body: dict = {
        "dateRanges": [{"startDate": start, "endDate": end}],
        "dimensions": [{"name": d} for d in dimensions],
        "metrics": [{"name": m} for m in metrics],
        "orderBys": [order],
        "limit": limit,
        "metricAggregations": ["TOTAL"],
    }
    if path_filter and filter_field:
        body["dimensionFilter"] = _contains(filter_field, path_filter)
    return body


def _by_metric(name: str) -> dict:
    return {"metric": {"metricName": name}, "desc": True}


def _apply_fallback(body: dict, detail: str) -> tuple[str, str | None] | None:
    """Swap one rejected field for its fallback, in place. Returns what changed."""
    for name, repl in _FALLBACKS.items():
        if name not in detail:
            continue
        used = False
        for key in ("dimensions", "metrics"):
            items = body.get(key) or []
            if any(i.get("name") == name for i in items):
                used = True
                body[key] = [
                    i if i.get("name") != name else {"name": repl}
                    for i in items if repl is not None or i.get("name") != name
                ]
        if not used:
            continue
        orders = []
        for o in body.get("orderBys") or []:
            if (o.get("dimension") or {}).get("dimensionName") == name:
                if repl is not None:
                    o = {**o, "dimension": {"dimensionName": repl}}
                else:
                    continue
            if (o.get("metric") or {}).get("metricName") == name:
                if repl is not None:
                    o = {**o, "metric": {"metricName": repl}}
                else:
                    continue
            orders.append(o)
        body["orderBys"] = orders
        flt = (body.get("dimensionFilter") or {}).get("filter") or {}
        if flt.get("fieldName") == name:
            if repl is None:
                body.pop("dimensionFilter", None)
            else:
                flt["fieldName"] = repl
        if not body.get("orderBys"):
            body.pop("orderBys", None)
        if not body.get("dimensions"):
            body.pop("dimensions", None)
        return name, repl
    return None


def _run(method, prop: str, body: dict, account: str | None, what: str) -> tuple[dict, dict, list[str]]:
    """Execute ``method(property=, body=)``, retrying with fallbacks on a 400
    that names a field GA4 no longer (or not yet) knows. Returns the response,
    the body that finally worked, and notes about any fallback used."""
    body = copy.deepcopy(body)
    notes: list[str] = []
    for _attempt in range(len(_FALLBACKS) + 1):
        try:
            return method(property=prop, body=body).execute(), body, notes
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AnalyticsError):
                raise
            swapped = None
            if _http_status(exc) == 400:
                # A fresh copy per retry: the client may still hold the
                # rejected body (a recorder in tests does), so never mutate it.
                body = copy.deepcopy(body)
                swapped = _apply_fallback(body, _http_detail(exc))
            if swapped is None:
                raise _friendly(exc, account, what) from exc
            name, repl = swapped
            notes.append(
                f"GA rejected {name}; used {repl}" if repl else
                f"GA rejected {name}; reported without it"
            )
            logger.info("analytics: %s", notes[-1])
    raise AnalyticsError(  # pragma: no cover — every fallback applies at most once
        f"Google Analytics kept rejecting the request: {'; '.join(notes)}"
    )


# --------------------------------------------------------------------------- #
# Response shaping — GA's numbers, readable keys, nothing computed here
# --------------------------------------------------------------------------- #
def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _is_rate(name: str) -> bool:
    return name.endswith("Rate")


def _key(name: str) -> str:
    if name in _READABLE:
        return _READABLE[name]
    return _snake(name) + ("_pct" if _is_rate(name) else "")


def _number(name: str, raw) -> int | float | None:
    """GA sends every metric as a string. '' or missing is None, never 0."""
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip()
    try:
        if _is_rate(name):
            return round(float(text) * 100, 1)
        return int(text)
    except ValueError:
        try:
            return round(float(text), 2)
        except ValueError:
            return None


def _dimension_value(name: str, raw) -> str | None:
    if raw is None:
        return None
    text = str(raw)
    if name == "date" and re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _metric_dict(metric_names: list[str], values: list[dict]) -> dict:
    out: dict = {}
    for i, name in enumerate(metric_names):
        raw = values[i].get("value") if i < len(values) else None
        out[_key(name)] = _number(name, raw)
    return out


def _shape(resp: dict) -> dict:
    dims = [h.get("name", "") for h in resp.get("dimensionHeaders") or []]
    mets = [h.get("name", "") for h in resp.get("metricHeaders") or []]
    rows = []
    for r in resp.get("rows") or []:
        row: dict = {}
        dvals = r.get("dimensionValues") or []
        for i, name in enumerate(dims):
            raw = dvals[i].get("value") if i < len(dvals) else None
            row[_key(name)] = _dimension_value(name, raw)
        row.update(_metric_dict(mets, r.get("metricValues") or []))
        rows.append(row)
    out: dict = {
        "dimensions": dims, "metrics": mets, "rows": rows, "row_count": len(rows),
    }
    if resp.get("rowCount") is not None:
        out["matching_rows"] = resp.get("rowCount")
    totals = resp.get("totals") or []
    if totals:
        out["totals"] = _metric_dict(mets, totals[0].get("metricValues") or [])
    if not rows:
        out["note"] = "no data in this range"
    return out


def _properties(account: str | None) -> dict:
    admin = _admin(account)
    items: list[dict] = []
    token: str | None = None
    for _page in range(50):
        kwargs: dict = {"pageSize": 200}
        if token:
            kwargs["pageToken"] = token
        try:
            resp = admin.accountSummaries().list(**kwargs).execute()
        except Exception as exc:  # noqa: BLE001
            raise _friendly(exc, account, "the Analytics account list") from exc
        for acct in resp.get("accountSummaries") or []:
            acct_name = acct.get("displayName", "")
            acct_id = (acct.get("account") or "").split("/")[-1]
            for p in acct.get("propertySummaries") or []:
                items.append({
                    "account": acct_name,
                    "account_id": acct_id,
                    "property": p.get("displayName", ""),
                    "property_id": (p.get("property") or "").split("/")[-1],
                    "property_type": p.get("propertyType", ""),
                })
        token = resp.get("nextPageToken") or None
        if not token:
            break
    out = {"action": "properties", "properties": items, "count": len(items), "status": "read"}
    if not items:
        out["note"] = "this token can see no GA4 properties"
    return out


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
def analytics_read(
    action: str,
    property_id: str | None = None,
    start: str = "28daysAgo",
    end: str = "today",
    limit: int = 25,
    path_filter: str | None = None,
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    account: str | None = None,
) -> dict:
    """Read a GA4 property by view — see the module docstring for each action.

    ``property_id`` is the numeric GA4 id (``properties`` finds it; every other
    action needs it). ``start``/``end`` take GA's relative forms (``28daysAgo``,
    ``yesterday``, ``today``) or ``YYYY-MM-DD`` and are passed through as given.
    ``path_filter`` is a case-insensitive CONTAINS on the landing page (or the
    page, for ``pages``). ``dimensions``/``metrics`` are for ``report`` only.
    ``account`` — which Google account: ``blt`` (default), ``deadlift``, ``choco``.
    """
    if action not in ACTIONS:
        raise AnalyticsError(
            f"unknown action {action!r} — expected one of: {', '.join(ACTIONS)}"
        )

    # Input checks that need no API come first, so a refusal never depends on
    # whether the server is in mock mode.
    prop = _property_id(property_id) if action in NEEDS_PROPERTY else None
    if path_filter is not None and not str(path_filter).strip():
        path_filter = None
    if path_filter and action not in PATH_FILTERABLE:
        raise AnalyticsError(
            f"{action} does not take path_filter (it applies to: "
            f"{', '.join(PATH_FILTERABLE)})"
        )
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise AnalyticsError("limit must be an integer")
    if not 1 <= limit <= MAX_LIMIT:
        raise AnalyticsError(f"limit must be 1-{MAX_LIMIT} (got {limit})")
    for label, value in (("start", start), ("end", end)):
        if not value or not str(value).strip():
            raise AnalyticsError(f"{label} must be a date (YYYY-MM-DD) or a GA relative date")
    start, end = str(start).strip(), str(end).strip()
    dims: list[str] = []
    mets: list[str] = []
    if action == "report":
        dims = _names(dimensions, "dimensions", MAX_DIMENSIONS)
        mets = _names(metrics, "metrics", MAX_METRICS)

    account = resolve_account(account)
    if settings.use_mock:
        return _mock({
            "action": action, "property_id": prop, "rows": [], "row_count": 0,
            "status": "not_read_mock",
        })

    if action == "properties":
        return _properties(account)

    assert prop is not None
    data = _data(account)
    what = f"GA4 property {prop}"
    base = {"action": action, "property_id": prop}

    if action == "realtime":
        body = {
            "dimensions": [{"name": "unifiedScreenName"}],
            "metrics": [{"name": "activeUsers"}],
            "orderBys": [_by_metric("activeUsers")],
            "limit": limit,
            "metricAggregations": ["TOTAL"],
        }
        resp, _used, notes = _run(
            data.properties().runRealtimeReport, f"properties/{prop}", body, account, what,
        )
        out = {**base, **_shape(resp), "status": "read"}
        if notes:
            out["fallback"] = notes
        return out

    if action == "landing_pages":
        body = _body(
            ["landingPagePlusQueryString"],
            ["sessions", "totalUsers", "newUsers", "engagementRate",
             "averageSessionDuration", "bounceRate", "keyEvents"],
            start, end, limit, _by_metric("sessions"),
            "landingPagePlusQueryString", path_filter,
        )
    elif action == "pages":
        body = _body(
            ["pagePath"],
            ["screenPageViews", "totalUsers", "averageSessionDuration", "engagementRate"],
            start, end, limit, _by_metric("screenPageViews"), "pagePath", path_filter,
        )
    elif action == "sources":
        body = _body(
            ["sessionSource", "sessionMedium"] + (["landingPage"] if path_filter else []),
            ["sessions", "totalUsers", "engagementRate", "keyEvents"],
            start, end, limit, _by_metric("sessions"), "landingPage", path_filter,
        )
    elif action == "trend":
        body = _body(
            ["date"] + (["landingPage"] if path_filter else []),
            ["sessions", "totalUsers", "keyEvents"],
            start, end, limit,
            {"dimension": {"dimensionName": "date"}, "desc": False},
            "landingPage", path_filter,
        )
    else:  # report
        body = _body(dims, mets, start, end, limit, _by_metric(mets[0]))

    resp, _used, notes = _run(
        data.properties().runReport, f"properties/{prop}", body, account, what,
    )
    out = {**base, "date_range": {"start": start, "end": end}}
    if path_filter:
        out["path_filter"] = path_filter
    out.update(_shape(resp))
    if notes:
        out["fallback"] = notes
    out["status"] = "read"
    return out
