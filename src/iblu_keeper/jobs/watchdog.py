"""Is IBLU itself still working?

    python -m iblu_keeper.jobs.watchdog [--dry] [--no-alert]

`analyst/sensecheck.py` checks whether the *data* makes sense. This checks
whether the *machine* is still running: services up, timers armed, the tick
recent, tokens refreshing, disk not full, backups being written.

It exists because of a specific failure. Deadlift and Choco stopped
authenticating on a Monday morning and stayed broken for 135 consecutive ticks.
Every one of those ticks logged the error correctly. Nothing was watching, and
nothing would have been, because noticing required somebody to go and look.

Two design rules, both learned from that:

  * **Everything becomes an observation first, and alerts are a view of the
    observation log.** One pipeline — check, record, announce — rather than a
    parallel set of alarms with their own memory.
  * **Announce once, then only after a cooling-off period.** An alert that
    repeats every thirty minutes is muted within a day, which is the same as no
    alert at all, only louder.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import db
from ..config import settings
from ..store import observations as obs

logger = logging.getLogger("iblu_keeper.jobs.watchdog")

# The units that must be running for IBLU to do anything unattended.
UNITS = (
    ("iblu-mcp.service", "the MCP server Claude connects to"),
    ("iblu-tick.timer", "the ten-minute recorder"),
    ("iblu-analyst.timer", "the day reconstruction"),
    ("iblu-weekly.timer", "the Friday review"),
    ("iblu-backup.timer", "the nightly database dump"),
)

DISK_WARN_PCT = 85
DISK_ERROR_PCT = 93

# The tick runs Mon–Fri 07:00–19:50. Outside that, "no recent tick" is correct
# behaviour, so staleness is only meaningful within the window plus a grace
# period for a run that is merely late.
TICK_GRACE = timedelta(minutes=45)
BACKUP_STALE_HOURS = 30          # nightly at 03:15, so 30h means one was missed
BACKUP_DIR = "/home/ignas/backups/iblu"

# How long a problem stays quiet after being announced. Long enough not to nag,
# short enough that a still-broken system says so again the same day.
ALERT_COOLDOWN = timedelta(hours=6)

# Alerts are for a human to read, so they wait for waking hours. An error found
# at 03:00 is announced at 08:00 — it is not lost, it is queued.
ALERT_FROM, ALERT_UNTIL = 8, 21


def _flag(kind: str, summary: str, *, severity="warn", detail=None, evidence=None,
          fp_parts=()) -> dict:
    return {
        "source": "tick", "kind": kind, "summary": summary, "severity": severity,
        "detail": detail, "evidence": evidence or {}, "detected_by": "rule",
        "fp": obs.fingerprint("watchdog", kind, *fp_parts),
    }


# --- the checks -------------------------------------------------------------


def check_units() -> list[dict]:
    """systemd units that should be running and are not."""
    found = []
    for unit, what in UNITS:
        verb = "is-active" if unit.endswith(".service") else "is-enabled"
        try:
            result = subprocess.run(
                ["systemctl", verb, unit], capture_output=True, text=True, timeout=15
            )
            state = (result.stdout or result.stderr).strip()
        except Exception as exc:  # noqa: BLE001
            state = f"could not be queried ({exc})"
        if state not in ("active", "enabled"):
            found.append(_flag(
                "unit_down", f"{unit} is {state} — {what} is not running",
                severity="error",
                detail="Nothing unattended happens without this unit. Bring it "
                       f"back with `sudo systemctl restart {unit}`.",
                evidence={"unit": unit, "state": state},
                fp_parts=(unit,),
            ))
    return found


def check_disk() -> list[dict]:
    usage = shutil.disk_usage("/")
    pct = round(100 * usage.used / usage.total)
    free_gb = round(usage.free / 1024**3, 1)
    if pct >= DISK_ERROR_PCT:
        return [_flag(
            "disk_full", f"the disk is {pct}% full — {free_gb} GB left",
            severity="error",
            detail="Postgres stops accepting writes when the volume fills, and "
                   "the recorder fails silently from that point on.",
            evidence={"used_pct": pct, "free_gb": free_gb},
            fp_parts=("disk",),
        )]
    if pct >= DISK_WARN_PCT:
        return [_flag(
            "disk_filling", f"the disk is {pct}% full — {free_gb} GB left",
            evidence={"used_pct": pct, "free_gb": free_gb}, fp_parts=("disk",),
        )]
    return []


def _tick_is_due(now_local: datetime) -> bool:
    """Should a tick have run by now? Mon–Fri 07:00–19:50 (deploy/iblu-tick.timer)."""
    if now_local.weekday() > 4:
        return False
    return 7 <= now_local.hour < 20


def check_tick_freshness(conn, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(settings.iblu_timezone))
    if not _tick_is_due(local):
        return []
    row = conn.execute("SELECT max(last_run_at) AS at FROM collector_state").fetchone()
    last = row["at"] if row else None
    if last is None:
        return [_flag("tick_never_ran", "no collector has ever recorded a run",
                      severity="error", fp_parts=("tick",))]
    behind = now - last
    if behind > timedelta(minutes=10) + TICK_GRACE:
        return [_flag(
            "tick_stale",
            f"the last tick was {int(behind.total_seconds() // 60)} minutes ago, "
            f"during working hours",
            severity="error",
            detail="The timer fires every ten minutes on weekdays. This long a "
                   "gap means the timer, the service or the box is not running.",
            evidence={"last_run_at": str(last), "minutes_behind": int(behind.total_seconds() // 60)},
            fp_parts=("tick",),
        )]
    return []


def check_backups(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    try:
        files = [
            os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
            if f.endswith(".sql.gz")
        ]
    except OSError as exc:
        return [_flag("backups_unreadable", f"the backup directory cannot be read ({exc})",
                      severity="error", fp_parts=("backup",))]
    if not files:
        return [_flag("backups_missing", "there are no database dumps at all",
                      severity="error", fp_parts=("backup",))]
    newest = max(os.path.getmtime(f) for f in files)
    age = now - datetime.fromtimestamp(newest, timezone.utc)
    if age > timedelta(hours=BACKUP_STALE_HOURS):
        return [_flag(
            "backups_stale",
            f"the newest database dump is {age.days}d {age.seconds // 3600}h old",
            severity="error",
            detail="The dump runs nightly at 03:15. Everything IBLU has recorded "
                   "lives in one Postgres instance on one box.",
            evidence={"age_hours": round(age.total_seconds() / 3600)},
            fp_parts=("backup",),
        )]
    return []


def check_google_auth() -> list[dict]:
    """Can every configured account still refresh? The failure that started this."""
    found = []
    for account in settings.configured_accounts():
        alias = account["alias"]
        try:
            from ..google_auth import get_credentials_for

            get_credentials_for(alias)
        except Exception as exc:  # noqa: BLE001
            # The message can carry a URL; scrub it the same way delivery does.
            from ..pings.deliver import _scrub

            found.append(_flag(
                "google_auth_failed",
                f"the {alias} Google account can no longer refresh its token",
                severity="error",
                detail=f"{_scrub(exc)}\n\nRe-authorize with "
                       f"`python scripts/connect_google.py --account {alias}` "
                       f"(needs a browser sign-in as that account).",
                evidence={"alias": alias},
                fp_parts=("auth", alias),
            ))
    return found


def check_database(conn) -> list[dict]:
    """The analyst should have produced something for the last working day."""
    row = conn.execute(
        "SELECT max(local_date) AS d FROM blocks WHERE superseded_by IS NULL"
    ).fetchone()
    latest = row["d"] if row else None
    if latest is None:
        return []          # nothing reconstructed yet is a young system, not a fault
    today = datetime.now(ZoneInfo(settings.iblu_timezone)).date()
    behind = (today - latest).days
    if behind > 3:
        return [_flag(
            "analyst_stale",
            f"the most recent reconstructed day is {latest} — {behind} days ago",
            evidence={"latest": str(latest), "days_behind": behind},
            fp_parts=("analyst",),
        )]
    return []


def run_checks(conn) -> list[dict]:
    """Every check. One failing check never stops the others."""
    found: list[dict] = []
    for name, fn in (
        ("units", lambda: check_units()),
        ("disk", lambda: check_disk()),
        ("tick", lambda: check_tick_freshness(conn)),
        ("backups", lambda: check_backups()),
        ("auth", lambda: check_google_auth()),
        ("analyst", lambda: check_database(conn)),
    ):
        try:
            found += fn()
        except Exception:  # noqa: BLE001
            logger.warning("watchdog: the %s check itself failed", name, exc_info=True)
    return found


# --- alerting ---------------------------------------------------------------


def alertable(conn, *, now: datetime | None = None, cooldown: timedelta = ALERT_COOLDOWN):
    """Open `error` findings that have not been announced recently.

    Deliberately errors only. A warning is something to read on Sunday; an
    error is something that means IBLU is not recording, and only the second
    kind earns a notification on his phone.
    """
    now = now or datetime.now(timezone.utc)
    return conn.execute(
        """
        SELECT id, source, kind, summary, detail, occurrences, first_seen_at
          FROM observations
         WHERE status = 'open' AND severity = 'error'
           AND (alerted_at IS NULL OR alerted_at < %s)
         ORDER BY first_seen_at
         LIMIT 5
        """,
        (now - cooldown,),
    ).fetchall()


def in_alert_window(now_local: datetime) -> bool:
    """Alerts wait for waking hours. An error at 03:00 is queued, not lost."""
    return ALERT_FROM <= now_local.hour < ALERT_UNTIL


def compose_alert(rows: list[dict], now_local: datetime) -> str:
    """The message. Short, specific, and it says what to do about it.

    No Gain framing here, deliberately. Everything else IBLU writes is shaped
    around progress measured backward; this one is "something is broken" and
    dressing that up would cost the seconds that matter.
    """
    head = "⚠️ *IBLU is not working properly*" if len(rows) > 1 else "⚠️ *IBLU problem*"
    lines = [head, ""]
    for r in rows:
        age = now_local - r["first_seen_at"].astimezone(now_local.tzinfo)
        since = (
            f"{age.days}d" if age.days
            else f"{age.seconds // 3600}h" if age.seconds >= 3600
            else f"{age.seconds // 60}m"
        )
        lines.append(f"• *{r['summary']}*")
        lines.append(f"  going on {since}, seen {r['occurrences']}x")
        detail = (r["detail"] or "").strip().splitlines()
        if detail:
            lines.append(f"  {detail[-1][:180]}")
    lines.append("")
    lines.append("_`python -m iblu_keeper.store.observations` for the full list._")
    return "\n".join(lines)


def send_alert(text: str) -> str:
    """Post to the Secretary space. The ONLY space IBLU may ever post to."""
    import requests

    from ..pings.deliver import _scrub

    url = settings.secretary_webhook_url
    if not url:
        raise RuntimeError("SECRETARY_WEBHOOK_URL is not set — nowhere to alert")
    separator = "&" if "?" in url else "?"
    try:
        response = requests.post(
            f"{url}{separator}threadKey=iblu-health",
            json={"text": text},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"webhook unreachable: {_scrub(exc)}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"webhook returned {response.status_code}")
    return (response.json() or {}).get("name", "")


def run(dry: bool = False, alert: bool = True) -> int:
    if settings.use_mock:
        logger.error("watchdog: refusing to run in mock mode")
        return 1
    if not db.is_configured():
        logger.error("watchdog: no DATABASE_URL")
        return 1

    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(settings.iblu_timezone))

    with db.get_conn() as conn:
        found = run_checks(conn)
        for finding in found:
            if dry:
                logger.info("would record: [%s] %s", finding["severity"], finding["summary"])
            else:
                try:
                    obs.record(conn, **finding)
                except Exception:  # noqa: BLE001
                    logger.warning("watchdog: could not record %s", finding["kind"], exc_info=True)

        # Housekeeping: leads that stopped recurring stop being shown. Never
        # errors, and never rule findings — those retire only by not
        # reproducing, which the sense-check checks properly.
        if not dry:
            aged = obs.age_out_llm_leads(conn)
            if aged:
                logger.info("watchdog: aged out %d unrepeated lead(s)", aged)

        errors = [f for f in found if f["severity"] == "error"]
        logger.info(
            "watchdog: %d finding(s), %d error(s)%s",
            len(found), len(errors), " [dry]" if dry else "",
        )

        if not alert or dry:
            return 0
        if not in_alert_window(local):
            logger.info("watchdog: outside the alert window — queued until %02d:00", ALERT_FROM)
            return 0

        rows = alertable(conn, now=now)
        if not rows:
            return 0
        try:
            send_alert(compose_alert([dict(r) for r in rows], local))
        except Exception as exc:  # noqa: BLE001
            # If the alert cannot be sent, do NOT stamp alerted_at — the next
            # run must try again. A silent alerter is the thing being guarded
            # against.
            logger.error("watchdog: could not send the alert: %s", exc)
            return 0
        conn.execute(
            "UPDATE observations SET alerted_at = now() WHERE id = ANY(%s)",
            ([r["id"] for r in rows],),
        )
        logger.info("watchdog: alerted on %d finding(s)", len(rows))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m iblu_keeper.jobs.watchdog",
        description="Is IBLU itself still working?",
    )
    parser.add_argument("--dry", action="store_true",
                        help="print what would be recorded; write nothing, send nothing")
    parser.add_argument("--no-alert", action="store_true",
                        help="record findings but never post to Chat")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    try:
        return run(dry=args.dry, alert=not args.no_alert)
    finally:
        db.close_pool()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
