"""Is IBLU itself still working?

The failure this exists for: Deadlift and Choco stopped authenticating on a
Monday morning and stayed broken for 135 consecutive ticks. Every tick logged
the error correctly. Nothing was watching.

So these tests care about two things — that each check FIRES when it should
(a quiet watchdog and a broken one look identical), and that alerting is
throttled, because an alert that repeats every half hour is muted within a day.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from iblu_keeper.jobs import watchdog as W

UTC = timezone.utc
TZ = ZoneInfo("Europe/Zagreb")


class _Conn:
    def __init__(self, answers: dict[str, list]):
        self.answers = answers
        self.executed: list[str] = []

    def execute(self, sql, args=()):
        flat = " ".join(sql.split())
        self.executed.append(flat)
        rows = next((v for k, v in self.answers.items() if k in flat), [])

        class _Cur:
            def fetchone(self_inner):
                return rows[0] if rows else None

            def fetchall(self_inner):
                return rows

        return _Cur()


# --- the checks fire ------------------------------------------------------


def test_a_stopped_unit_is_an_error(monkeypatch):
    class _R:
        stdout, stderr = "inactive", ""

    monkeypatch.setattr(W.subprocess, "run", lambda *a, **k: _R())
    found = W.check_units()
    assert found and all(f["severity"] == "error" for f in found)
    assert any("iblu-mcp.service" in f["summary"] for f in found)


def test_healthy_units_produce_nothing(monkeypatch):
    class _R:
        def __init__(self, cmd, *a, **k):
            self.stdout = "active" if cmd[2].endswith(".service") else "enabled"
            self.stderr = ""

    monkeypatch.setattr(W.subprocess, "run", lambda *a, **k: _R(a[0]))
    assert W.check_units() == []


def test_a_unit_that_cannot_be_queried_is_still_reported(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("systemctl not found")

    monkeypatch.setattr(W.subprocess, "run", _boom)
    assert len(W.check_units()) == len(W.UNITS)


def test_a_full_disk_is_an_error_and_a_filling_one_is_a_warning(monkeypatch):
    import collections

    Usage = collections.namedtuple("Usage", "total used free")
    monkeypatch.setattr(W.shutil, "disk_usage",
                        lambda _: Usage(total=100, used=95, free=5))
    assert W.check_disk()[0]["severity"] == "error"

    monkeypatch.setattr(W.shutil, "disk_usage",
                        lambda _: Usage(total=100, used=88, free=12))
    assert W.check_disk()[0]["severity"] == "warn"

    monkeypatch.setattr(W.shutil, "disk_usage",
                        lambda _: Usage(total=100, used=40, free=60))
    assert W.check_disk() == []


def test_a_stale_tick_during_working_hours_is_an_error():
    now = datetime(2026, 9, 15, 12, 0, tzinfo=TZ).astimezone(UTC)   # Tuesday noon
    conn = _Conn({"max(last_run_at)": [{"at": now - timedelta(hours=3)}]})
    [f] = W.check_tick_freshness(conn, now=now)
    assert f["severity"] == "error" and f["kind"] == "tick_stale"


def test_a_quiet_weekend_is_not_a_stale_tick():
    """The timer is Mon–Fri. Silence on a Sunday is the timer working."""
    now = datetime(2026, 9, 13, 12, 0, tzinfo=TZ).astimezone(UTC)   # Sunday
    conn = _Conn({"max(last_run_at)": [{"at": now - timedelta(days=2)}]})
    assert W.check_tick_freshness(conn, now=now) == []


def test_a_quiet_night_is_not_a_stale_tick():
    now = datetime(2026, 9, 15, 3, 0, tzinfo=TZ).astimezone(UTC)
    conn = _Conn({"max(last_run_at)": [{"at": now - timedelta(hours=8)}]})
    assert W.check_tick_freshness(conn, now=now) == []


def test_a_tick_merely_late_is_tolerated():
    now = datetime(2026, 9, 15, 12, 0, tzinfo=TZ).astimezone(UTC)
    conn = _Conn({"max(last_run_at)": [{"at": now - timedelta(minutes=20)}]})
    assert W.check_tick_freshness(conn, now=now) == []


def test_missing_backups_are_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "BACKUP_DIR", str(tmp_path))
    [f] = W.check_backups()
    assert f["kind"] == "backups_missing" and f["severity"] == "error"


def test_an_old_backup_is_an_error(monkeypatch, tmp_path):
    import os
    import time

    dump = tmp_path / "iblu_keeper_2026-09-01.sql.gz"
    dump.write_text("x")
    old = time.time() - 60 * 60 * 40
    os.utime(dump, (old, old))
    monkeypatch.setattr(W, "BACKUP_DIR", str(tmp_path))
    [f] = W.check_backups()
    assert f["kind"] == "backups_stale"


def test_a_fresh_backup_is_silent(monkeypatch, tmp_path):
    (tmp_path / "iblu_keeper_2026-09-15.sql.gz").write_text("x")
    monkeypatch.setattr(W, "BACKUP_DIR", str(tmp_path))
    assert W.check_backups() == []


def test_a_broken_google_account_is_an_error_and_leaks_no_url(monkeypatch):
    class _Settings:
        def configured_accounts(self):
            return [{"alias": "choco"}]

    monkeypatch.setattr(W, "settings", _Settings())
    import iblu_keeper.google_auth as ga

    def _boom(alias, scopes=None):
        raise RuntimeError(
            "refresh failed for url: https://oauth2.googleapis.com/token?key=SECRET1"
        )

    monkeypatch.setattr(ga, "get_credentials_for", _boom)
    [f] = W.check_google_auth()
    assert f["severity"] == "error"
    assert "SECRET1" not in f["detail"], "a secret reached the observation row"
    assert "connect_google.py --account choco" in f["detail"]


def test_one_failing_check_never_stops_the_others(monkeypatch):
    monkeypatch.setattr(W, "check_units", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(W, "check_disk", lambda: [W._flag("disk_filling", "disk", severity="warn")])
    monkeypatch.setattr(W, "check_backups", lambda: [])
    monkeypatch.setattr(W, "check_google_auth", lambda: [])
    monkeypatch.setattr(W, "check_tick_freshness", lambda conn, now=None: [])
    monkeypatch.setattr(W, "check_database", lambda conn: [])
    assert [f["kind"] for f in W.run_checks(_Conn({}))] == ["disk_filling"]


# --- alerting is throttled and timed --------------------------------------


def test_only_errors_reach_chat():
    """A warning is something to read on Sunday; an error means IBLU is not
    recording. Only the second earns a notification."""
    conn = _Conn({"FROM observations": []})
    alertable = W.alertable(conn)
    assert "severity = 'error'" in conn.executed[0]
    assert "alerted_at IS NULL OR alerted_at <" in conn.executed[0]


def test_alerts_wait_for_waking_hours():
    assert not W.in_alert_window(datetime(2026, 9, 15, 3, 0, tzinfo=TZ))
    assert not W.in_alert_window(datetime(2026, 9, 15, 22, 0, tzinfo=TZ))
    assert W.in_alert_window(datetime(2026, 9, 15, 9, 0, tzinfo=TZ))


def _row(**over):
    base = {
        "id": 1, "source": "tick", "kind": "google_auth_failed",
        "summary": "the choco Google account can no longer refresh its token",
        "detail": "Re-authorize with `python scripts/connect_google.py --account choco`.",
        "occurrences": 135,
        "first_seen_at": datetime(2026, 9, 14, 5, 0, tzinfo=UTC),
    }
    base.update(over)
    return base


def test_the_alert_says_what_broke_how_long_and_what_to_do():
    text = W.compose_alert([_row()], datetime(2026, 9, 15, 9, 0, tzinfo=TZ))
    assert "choco" in text
    assert "135x" in text
    assert "connect_google.py" in text


def test_the_alert_does_not_dress_a_failure_as_progress():
    """Everything else IBLU writes is shaped around gains. This one is not."""
    text = W.compose_alert([_row()], datetime(2026, 9, 15, 9, 0, tzinfo=TZ))
    assert text.startswith("⚠️")
    for word in ("gain", "progress", "great", "well done"):
        assert word not in text.lower()


def test_several_problems_are_one_message_not_several():
    text = W.compose_alert([_row(id=1), _row(id=2, summary="the disk is 95% full")],
                           datetime(2026, 9, 15, 9, 0, tzinfo=TZ))
    assert text.count("⚠️") == 1
    assert "95% full" in text


def test_the_alert_refuses_to_send_without_a_webhook(monkeypatch):
    class _NoHook:
        secretary_webhook_url = ""

    monkeypatch.setattr(W, "settings", _NoHook())
    with pytest.raises(RuntimeError, match="nowhere to alert"):
        W.send_alert("anything")


# --- no false alarms, and findings clear themselves (2026-09-17) ----------


def test_the_first_tick_of_the_day_is_not_overdue_at_the_moment_it_is_due():
    """At 07:00 the watchdog saw "last tick: yesterday 19:50" and raised an
    error that was re-announced every six hours for two days."""
    now = datetime(2026, 9, 17, 7, 0, tzinfo=TZ).astimezone(UTC)
    conn = _Conn({"max(last_run_at)": [{"at": now - timedelta(hours=11)}]})
    assert W.check_tick_freshness(conn, now=now) == []


def test_monday_morning_is_not_a_stale_tick_either():
    now = datetime(2026, 9, 14, 7, 30, tzinfo=TZ).astimezone(UTC)   # Monday
    conn = _Conn({"max(last_run_at)": [{"at": now - timedelta(days=2, hours=12)}]})
    assert W.check_tick_freshness(conn, now=now) == []


def test_a_morning_with_no_tick_at_all_is_still_caught():
    now = datetime(2026, 9, 17, 8, 30, tzinfo=TZ).astimezone(UTC)
    conn = _Conn({"max(last_run_at)": [{"at": now - timedelta(hours=12)}]})
    assert W.check_tick_freshness(conn, now=now)[0]["kind"] == "tick_stale"


def test_a_finding_that_no_longer_holds_is_retired(monkeypatch):
    resolved = []
    monkeypatch.setattr(W.obs, "resolve", lambda conn, oid, note: resolved.append(oid) or True)
    conn = _Conn({"FROM observations": [{"id": 931, "fingerprint": "old"},
                                        {"id": 932, "fingerprint": "still"}]})
    retired = W.retire_cleared(conn, [{"fp": "still"}])
    assert retired == 1 and resolved == [931]


def test_the_watchdog_only_retires_its_own_kinds():
    conn = _Conn({"FROM observations": []})
    W.retire_cleared(conn, [])
    assert "kind = ANY" in conn.executed[0]
    assert "sensecheck" not in conn.executed[0]
