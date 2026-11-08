"""get_infra_status: a host that reported minutes ago must not be called stale.

Found 2026-11-08: three of five hosts' collectors write `timestamp`, two write
`ts`. The status read only `ts`, so appsprod, appsdev and the n8n VM showed as
"stale" in every report while reporting every few minutes.
"""

from datetime import datetime, timedelta, timezone

from iblu_keeper.tools import infra


def _hosts(**stamps):
    return {"hosts": [{"host": name, **stamp} for name, stamp in stamps.items()]}


def test_both_timestamp_spellings_count_as_fresh():
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    fn = infra._derive_status
    status, summary = fn(_hosts(hub={"ts": now}, appsprod={"timestamp": now}))
    assert "2/2 hosts healthy" in summary, summary


def test_an_old_report_is_still_stale_under_either_spelling():
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    fn = infra._derive_status
    _status, summary = fn(_hosts(hub={"ts": now}, appsprod={"timestamp": old}))
    assert "1 stale (appsprod)" in summary, summary
