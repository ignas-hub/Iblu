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


def test_a_stopped_unit_that_records_is_an_error(monkeypatch):
    """Whatever must run for IBLU to record unattended earns a phone alert."""
    class _R:
        stdout, stderr = "inactive", ""

    monkeypatch.setattr(W.subprocess, "run", lambda *a, **k: _R())
    found = {f["evidence"]["unit"]: f for f in W.check_units()}
    assert found
    for unit in ("iblu-mcp.service", "iblu-tick.timer", "iblu-analyst.timer",
                 "iblu-backup.timer", "iblu-daycard.timer"):
        assert found[unit]["severity"] == "error", unit
        assert "Nothing unattended happens" in found[unit]["detail"]


def test_a_stopped_dashboard_is_only_a_warning(monkeypatch):
    """Recording carries on without it; what stops is him being able to look.

    The severity is the difference between "IBLU is not recording" and "IBLU is
    recording and you cannot see it", and only the first is worth a 4am buzz.
    """
    class _R:
        stdout, stderr = "inactive", ""

    monkeypatch.setattr(W.subprocess, "run", lambda *a, **k: _R())
    found = {f["evidence"]["unit"]: f for f in W.check_units()}
    assert found["iblu-dashboard.service"]["severity"] == "warn"
    assert "look at the data" in found["iblu-dashboard.service"]["detail"]


def test_the_watchdog_does_not_claim_to_watch_itself():
    """A dead watchdog cannot report its own death.

    Listing `iblu-watchdog.timer` would only ever fire in the window between
    `systemctl disable` and the next reboot, while implying the watchdog is
    self-monitoring. It is not, and pretending otherwise is worse than the gap.
    """
    assert "iblu-watchdog.timer" not in {u for u, _what, _sev in W.UNITS}


def test_every_installed_unit_is_watched():
    """The daycard timer went uninstalled for a fortnight and nothing noticed.

    Pinned against the repo's own deploy/ directory rather than the live box,
    so it holds in CI and in a worktree: if a future session adds a unit file,
    this fails until the watchdog is told about it.
    """
    import pathlib

    deploy = pathlib.Path(__file__).resolve().parents[1] / "deploy"
    on_disk = {p.name for p in deploy.glob("iblu-*.timer")}
    on_disk |= {p.name for p in deploy.glob("iblu-*.service")}
    watched = {u for u, _what, _sev in W.UNITS}

    # A .timer and its .service are one unit to watch, not two: an enabled
    # timer is the thing that proves the pair will fire.
    paired = {name for name in on_disk
              if name.endswith(".service") and name[:-8] + ".timer" in on_disk}
    expected = on_disk - paired - {"iblu-watchdog.timer"}
    assert expected <= watched, f"installed but unwatched: {sorted(expected - watched)}"


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


def test_a_worsening_disk_is_not_reported_as_cleared():
    """87% -> 94% used to resolve the warning with "it no longer holds", at the
    moment the problem got worse, because each severity had its own kind."""
    import collections

    Usage = collections.namedtuple("Usage", "total used free")
    import iblu_keeper.jobs.watchdog as W2

    original = W2.shutil.disk_usage
    try:
        W2.shutil.disk_usage = lambda _: Usage(total=100, used=87, free=13)
        [warn] = W2.check_disk()
        W2.shutil.disk_usage = lambda _: Usage(total=100, used=94, free=6)
        [err] = W2.check_disk()
    finally:
        W2.shutil.disk_usage = original
    assert warn["severity"] == "warn" and err["severity"] == "error"
    assert warn["fp"] == err["fp"], "the same condition must keep one fingerprint"


def test_the_alert_keeps_the_command_whole(monkeypatch):
    """180 characters cut the phone-runnable command to "...Fro".

    The last line of a finding's detail is the one that says what to do. A
    command he can run from the alert is usually the entire point of the
    message, so it must survive truncation intact and the cut must land on a
    word rather than mid-token.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from iblu_keeper.google_auth import reauth_hint

    exc = Exception("('invalid_grant: Token has been expired or revoked.', "
                    "{'error': 'invalid_grant'})")
    row = {
        "summary": "the blt Google account can no longer refresh its token",
        "detail": f"{exc}\n\n{reauth_hint('blt', exc)}",
        "occurrences": 16,
        "first_seen_at": datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc),
    }
    text = W.compose_alert(
        [row], datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
        .astimezone(ZoneInfo("Europe/Zagreb"))
    )
    assert "--account blt --manual" in text, "the phone command must not be cut"
    assert "Fro\n" not in text and not text.endswith("Fro")


def test_a_long_detail_is_still_cut_at_a_word():
    from datetime import datetime, timezone

    row = {
        "summary": "something broke",
        "detail": "x" + " verylongword" * 80,
        "occurrences": 1,
        "first_seen_at": datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc),
    }
    [line] = [
        l for l in W.compose_alert([row], datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)).splitlines()
        if "verylongword" in l
    ]
    assert line.endswith("…"), "a cut must say it was cut"
    assert len(line) <= W.DETAIL_CHARS + 4


# --- the Chat read-state worker (2026-10-01) -------------------------------
#
# It has no systemd unit of its own — it is a thread inside iblu-mcp.service,
# which is why nothing watched it. When Pub/Sub stops delivering, nothing
# breaks: `_get_last_read_time` falls back to one Chat API call per space,
# across 277 spaces, and chat_list_unread merely gets slow. Silent degradation.


NOW = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)


def _chat_seen(ago_hours: float) -> _Conn:
    return _Conn({"max(occurred_at)": [{"at": NOW - timedelta(hours=ago_hours)}]})


def _health(monkeypatch, snapshot):
    monkeypatch.setattr(W, "readstate_health", lambda *a, **k: snapshot)


def test_a_healthy_worker_says_nothing(monkeypatch):
    _health(monkeypatch, {
        "worker_alive": True, "ready": True, "cached_spaces": 4,
        "last_event_at": (NOW - timedelta(minutes=5)).timestamp(),
    })
    assert W.check_readstate(_chat_seen(0.5), now=NOW) == []


def test_a_dead_worker_thread_is_an_error(monkeypatch):
    """It cannot restart itself — it is a thread, so the whole server must go."""
    _health(monkeypatch, {"worker_alive": False, "ready": False, "cached_spaces": 0})
    [f] = W.check_readstate(_chat_seen(0.5), now=NOW)
    assert f["kind"] == "readstate_worker_dead"
    assert f["severity"] == "error"
    assert "restart iblu-mcp.service" in f["detail"]


def test_an_unreachable_health_endpoint_is_a_warning_not_an_error(monkeypatch):
    """`check_units` already raises the error when the server is down.

    Two findings about one cause is how the watchdog cried wolf before: six
    alerts in two days, several of them the same thing twice. A warning stays
    visible in the log without buzzing his phone a second time.
    """
    _health(monkeypatch, None)
    [f] = W.check_readstate(_chat_seen(0.5), now=NOW)
    assert f["kind"] == "readstate_unreachable"
    assert f["severity"] == "warn"
    assert "fix that first" in f["detail"]


def test_overnight_silence_is_correct_behaviour_not_a_fault(monkeypatch):
    """Events arrive when he READS a space. Asleep, there are none, correctly.

    Without this guard the check would fire every morning, which is the fastest
    way to teach him to ignore it.
    """
    _health(monkeypatch, {
        "worker_alive": True, "ready": True,
        "last_event_at": (NOW - timedelta(hours=9)).timestamp(),
    })
    assert W.check_readstate(_chat_seen(9), now=NOW) == []


def test_silence_while_he_is_using_chat_is_a_lapsed_subscription(monkeypatch):
    """The realistic failure: the 6-day subscription-refresh loop died."""
    _health(monkeypatch, {
        "worker_alive": True, "ready": True,
        "last_event_at": (NOW - timedelta(hours=8)).timestamp(),
    })
    [f] = W.check_readstate(_chat_seen(1), now=NOW)
    assert f["kind"] == "readstate_stale"
    assert f["severity"] == "warn", "inferential, so it does not wake him"
    assert "8h" in f["summary"]
    assert "expires after 7 days" in f["detail"]


def test_a_worker_that_has_never_seen_an_event_is_not_reported(monkeypatch):
    """A freshly restarted server has no last_event_at, and that is not a fault."""
    _health(monkeypatch, {"worker_alive": True, "ready": False, "last_event_at": None})
    assert W.check_readstate(_chat_seen(0.5), now=NOW) == []


def test_health_is_asked_over_the_configured_bind_not_localhost():
    """On this box the server binds to a docker bridge address, not 127.0.0.1.

    Hardcoding localhost returns None forever — the check would be permanently
    "unreachable" and the finding permanently wrong. Going out via the public
    URL instead would report a reverse-proxy outage as a worker fault.
    """
    import inspect

    source = inspect.getsource(W.readstate_health)
    # The URL it builds, not the prose explaining why — the docstring names the
    # wrong address deliberately, as the thing NOT to do.
    assert 'f"http://{settings.mcp_host}:{settings.mcp_port}/health"' in source
    body = source.split('"""')[-1]
    assert "127.0.0.1" not in body
    assert "mcp_public_base_url" not in body


def test_the_readstate_findings_can_retire_themselves():
    """Every kind this module records must be in WATCHDOG_KINDS.

    `retire_cleared` only closes its own kinds, so a finding missing from that
    tuple stays open forever and is re-announced every six hours — which is
    exactly what happened before retirement existed at all.
    """
    for kind in ("readstate_unreachable", "readstate_worker_dead", "readstate_stale"):
        assert kind in W.WATCHDOG_KINDS


# --- is the one feedback loop closing? (2026-10-05) ------------------------
#
# Two day cards delivered, both working — route healthy, buttons signed, a
# friendly 410 on a bad token — and zero taps. `blocks` held 290 rows, not one
# confirmed, so `jobs/audit.py` still could not print an accuracy figure. The
# loop the card was built to close had failed silently for four days and
# nothing was looking.


def _cards(sent: int, answered: int, confirmed: int = 0) -> _Conn:
    return _Conn({
        "FROM day_cards": [{"sent": sent, "answered": answered}],
        "FROM blocks": [{"n": confirmed}],
    })


def test_cards_going_unanswered_is_reported():
    [f] = W.check_day_cards(_cards(sent=2, answered=0))
    assert f["kind"] == "daycard_unanswered"
    assert f["evidence"] == {"sent_14d": 2, "confirmed_blocks": 0}
    assert "nothing confirming it" in f["summary"]


def test_it_never_wakes_him_about_not_answering():
    """He is the one not answering; buzzing his phone about it is nagging.

    This finding is addressed to whoever can make the card easier to answer,
    which is why it is a warning and reaches the session brief instead.
    """
    [f] = W.check_day_cards(_cards(sent=5, answered=0))
    assert f["severity"] == "warn"


def test_one_answered_card_clears_it():
    """`daycard.py` sets status='answered', so the condition really can clear.

    A check that can never become false is noise with extra steps.
    """
    assert W.check_day_cards(_cards(sent=4, answered=1)) == []


def test_a_single_unanswered_evening_is_not_a_broken_loop():
    """He was busy once. Two is the point at which it stops being an evening."""
    assert W.check_day_cards(_cards(sent=1, answered=0)) == []


def test_no_cards_at_all_is_the_timer_check_not_this_one():
    assert W.check_day_cards(_cards(sent=0, answered=0)) == []


def test_the_daycard_finding_can_retire_itself():
    assert "daycard_unanswered" in W.WATCHDOG_KINDS
