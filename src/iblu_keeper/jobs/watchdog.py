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

# The units that must be running for IBLU to do anything unattended, and how
# bad it is when one is not. `error` reaches his phone; `warn` waits to be read.
#
# `iblu-watchdog.timer` is deliberately absent. A watchdog cannot report its own
# death: once the timer is disabled no run happens to notice, so the check would
# only ever fire in the narrow window between `disable` and the next reboot
# while giving the false impression that the watchdog is self-monitoring. What
# actually catches a dead watchdog is `check_tick_freshness` going unreported
# and him noticing the silence. Do not "fix" this omission.
UNITS = (
    ("iblu-mcp.service", "the MCP server Claude connects to", "error"),
    ("iblu-tick.timer", "the ten-minute recorder", "error"),
    ("iblu-analyst.timer", "the day reconstruction", "error"),
    ("iblu-weekly.timer", "the Friday review", "error"),
    ("iblu-backup.timer", "the nightly database dump", "error"),
    # Installed 2026-09-30. Without it the reconstruction is never confirmed by
    # anybody, and shadow-calendar accuracy stays unmeasurable — which is the
    # state it was in for its first fortnight, unnoticed, because nothing
    # watched for a unit that had never been installed.
    ("iblu-daycard.timer",
     "the evening day card — the only thing that turns a guess into a fact",
     "error"),
    # A warning, not an error: recording continues without the dashboard. What
    # stops is his ability to look at any of it.
    ("iblu-dashboard.service", "the dashboard he reads his own data on", "warn"),
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

# How much of a finding's last line reaches his phone. Enough for a command he
# can run from it, which is usually the entire point of the message.
DETAIL_CHARS = 340


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
    for unit, what, severity in UNITS:
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
                severity=severity,
                detail=(
                    "Nothing unattended happens without this unit. Bring it "
                    f"back with `sudo systemctl restart {unit}`."
                    if severity == "error" else
                    "Recording carries on without it; what stops is being able "
                    f"to look at the data. `sudo systemctl restart {unit}`."
                ),
                evidence={"unit": unit, "state": state},
                fp_parts=(unit,),
            ))
    return found


def check_disk() -> list[dict]:
    usage = shutil.disk_usage("/")
    pct = round(100 * usage.used / usage.total)
    free_gb = round(usage.free / 1024**3, 1)
    # One kind for both severities. With `disk_filling` and `disk_full` as
    # separate kinds, a disk going from 87% to 94% "cleared" the warning — the
    # old fingerprint was absent from the new run — and the audit trail said a
    # problem had resolved at the moment it got worse.
    if pct >= DISK_ERROR_PCT:
        return [_flag(
            "disk_pressure", f"the disk is {pct}% full — {free_gb} GB left",
            severity="error",
            detail="Postgres stops accepting writes when the volume fills, and "
                   "the recorder fails silently from that point on.",
            evidence={"used_pct": pct, "free_gb": free_gb},
            fp_parts=("disk",),
        )]
    if pct >= DISK_WARN_PCT:
        return [_flag(
            "disk_pressure", f"the disk is {pct}% full — {free_gb} GB left",
            evidence={"used_pct": pct, "free_gb": free_gb}, fp_parts=("disk",),
        )]
    return []


def _tick_is_due(now_local: datetime) -> bool:
    """Should a tick have run by now? Mon–Fri 07:00–19:50 (deploy/iblu-tick.timer).

    "Due" starts one cadence plus the grace AFTER 07:00, not at 07:00. The
    watchdog fires on the hour and half hour, which is exactly when the day's
    first tick is scheduled — so at 07:00 it saw "last tick: yesterday 19:50,
    during working hours" and raised an error that was re-announced every six
    hours for two days. Until the first tick has had time to run, the gap
    since last night is not a gap.
    """
    if now_local.weekday() > 4:
        return False
    day_start = now_local.replace(hour=7, minute=0, second=0, microsecond=0)
    return day_start + timedelta(minutes=10) + TICK_GRACE <= now_local < now_local.replace(
        hour=20, minute=0, second=0, microsecond=0
    )


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

            # The hint is shaped by the failure, and `compose_alert` sends the
            # LAST line of this detail to his phone — so the line he reads is
            # the one that says what to do, not the raw exception.
            from ..google_auth import reauth_hint

            found.append(_flag(
                "google_auth_failed",
                f"the {alias} Google account can no longer refresh its token",
                severity="error",
                detail=f"{_scrub(exc)}\n\n{reauth_hint(alias, exc)}",
                evidence={"alias": alias},
                fp_parts=("auth", alias),
            ))
    return found


# How long without a readstate event before the subscription looks lapsed, and
# how recent his Chat activity must be for that silence to mean anything. Events
# arrive when he READS a space, so overnight silence is correct behaviour — the
# only honest version of this check needs evidence he has been in Chat at all.
READSTATE_SILENT_HOURS = 6
READSTATE_ACTIVE_WITHIN_HOURS = 3


def readstate_health(timeout: float = 5.0) -> dict | None:
    """The MCP server's own view of its readstate worker, or None if unreachable.

    Asked over the loopback bind rather than the public URL: going out through
    DNS, TLS and the reverse proxy would report a proxy outage as a worker fault.
    `settings.mcp_host` is the same value the server passes to uvicorn, which on
    this box is a docker bridge address rather than 127.0.0.1 — hardcoding
    localhost here silently returns None forever.
    """
    import requests

    try:
        response = requests.get(
            f"http://{settings.mcp_host}:{settings.mcp_port}/health", timeout=timeout
        )
        if response.status_code >= 400:
            return None
        return (response.json() or {}).get("readstate_worker")
    except Exception:  # noqa: BLE001 — unreachable is an answer, not a crash
        return None


def check_readstate(conn, now: datetime | None = None) -> list[dict]:
    """Is the Chat read-state cache still being fed?

    It has no systemd unit of its own — it is a thread inside iblu-mcp.service,
    which is why nothing watched it. When Pub/Sub stops delivering, nothing
    breaks: `chat.py::_get_last_read_time` falls back to one Chat API call per
    space, across 277 spaces, and `chat_list_unread` just gets slow. Silent
    degradation, the same shape as the collector that swallowed a NameError for
    days.
    """
    now = now or datetime.now(timezone.utc)
    health = readstate_health()
    if health is None:
        # Deliberately a warning, and deliberately not silent. If the server is
        # down, `check_units` raises its own error and that is the one that
        # reaches his phone; this would be noise at best. But a unit that is
        # active while its health endpoint refuses is a real and separate fault,
        # and it should be visible somewhere.
        return [_flag(
            "readstate_unreachable",
            "the MCP server's health endpoint did not answer, so the Chat "
            "read-state worker cannot be checked",
            detail=f"Tried http://{settings.mcp_host}:{settings.mcp_port}/health. "
                   "If iblu-mcp.service is also reported down, fix that first — "
                   "this finding is a consequence of it.",
            fp_parts=("readstate", "unreachable"),
        )]

    if not health.get("worker_alive"):
        return [_flag(
            "readstate_worker_dead",
            "the Chat read-state worker thread has died",
            severity="error",
            detail="It is a thread inside iblu-mcp.service, so it cannot restart "
                   "itself: `sudo systemctl restart iblu-mcp.service`. Until then "
                   "chat_list_unread works but makes one Chat API call per space.",
            evidence={k: health.get(k) for k in ("ready", "cached_spaces")},
            fp_parts=("readstate", "dead"),
        )]

    # Silence only means something if he has been using Chat. `last_event_at` is
    # an epoch float from `readstate_worker.snapshot()`.
    last_event = health.get("last_event_at")
    if not last_event:
        return []
    silent_for = now - datetime.fromtimestamp(float(last_event), timezone.utc)
    if silent_for < timedelta(hours=READSTATE_SILENT_HOURS):
        return []

    row = conn.execute(
        "SELECT max(occurred_at) AS at FROM signals WHERE source = 'chat'"
    ).fetchone()
    newest_chat = row["at"] if row else None
    if newest_chat is None or now - newest_chat > timedelta(
        hours=READSTATE_ACTIVE_WITHIN_HOURS
    ):
        return []          # he has not been in Chat either — silence is correct

    return [_flag(
        "readstate_stale",
        f"no Chat read-state event for {int(silent_for.total_seconds() // 3600)}h "
        "although Chat is being used",
        detail="The Workspace Events subscription expires after 7 days and is "
               "re-created every 6. If that refresh loop died, events stop and "
               "chat_list_unread silently falls back to per-space API calls. "
               "`sudo systemctl restart iblu-mcp.service` re-creates it.",
        evidence={
            "silent_hours": round(silent_for.total_seconds() / 3600, 1),
            "newest_chat_signal": str(newest_chat),
        },
        fp_parts=("readstate", "stale"),
    )]


# How many unanswered day cards before the ground-truth loop is judged broken.
# Two is enough to distinguish "he was busy that evening" from "this is not
# working", and few enough that it is said while it can still be fixed.
DAYCARD_UNANSWERED_LIMIT = 2


def check_day_cards(conn) -> list[dict]:
    """Is the day card producing any ground truth at all?

    Everything else here asks whether a machine is running. This asks whether
    the one feedback loop in IBLU is closing, and it exists because that loop
    failed silently for its first four days: two cards delivered, both working
    — route healthy, buttons signed, a friendly 410 on a bad token — and zero
    taps, so `blocks` still held 290 rows of which not one was confirmed.

    Nothing noticed, because nothing was looking. The day card is the ONLY path
    from a reconstruction to a fact, and `jobs/audit.py` refuses to print an
    accuracy figure with an empty denominator — so a card nobody answers leaves
    the whole system permanently unable to say how right it is, which is
    exactly the state the card was built to end.

    A warning, never an error. He is the one not answering, and buzzing his
    phone about not answering is nagging. This is addressed to whoever can make
    the card easier to answer.
    """
    rows = conn.execute(
        """
        SELECT count(*) AS sent,
               count(*) FILTER (WHERE status = 'answered') AS answered
          FROM day_cards
         WHERE sent_at >= now() - interval '14 days'
        """
    ).fetchone()
    if rows is None:
        return []
    sent, answered = int(rows["sent"] or 0), int(rows["answered"] or 0)
    if answered or sent < DAYCARD_UNANSWERED_LIMIT:
        return []

    confirmed = conn.execute(
        "SELECT count(*) AS n FROM blocks "
        " WHERE source IN ('human','ping') AND superseded_by IS NULL"
    ).fetchone()
    return [_flag(
        "daycard_unanswered",
        f"{sent} day cards sent and none answered — the reconstruction still "
        "has nothing confirming it",
        detail="The day card is the only path from a guess to a fact, and "
               "`jobs/audit.py` will not print an accuracy figure without one. "
               "Check the card is answerable before assuming he is ignoring it: "
               "the tap route, the 20:30 timing, and whether the lines name "
               "anything he can recognise.",
        evidence={"sent_14d": sent, "confirmed_blocks": int(confirmed["n"] or 0)},
        fp_parts=("daycard", "unanswered"),
    )]


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


# Every kind this module records, so it only ever retires its own findings.
WATCHDOG_KINDS = (
    "unit_down", "disk_pressure", "disk_full", "disk_filling",
    "tick_never_ran", "tick_stale",
    "backups_unreadable", "backups_missing", "backups_stale",
    "google_auth_failed", "analyst_stale",
    "readstate_unreachable", "readstate_worker_dead", "readstate_stale",
    "daycard_unanswered",
)


def retire_cleared(conn, found: list[dict]) -> int:
    """Resolve open watchdog findings that this run did not see again."""
    still_true = {f["fp"] for f in found}
    rows = conn.execute(
        """
        SELECT id, fingerprint FROM observations
         WHERE status = 'open' AND detected_by = 'rule'
           AND kind = ANY(%s)
        """,
        (list(WATCHDOG_KINDS),),
    ).fetchall()
    retired = 0
    for row in rows:
        if row["fingerprint"] in still_true:
            continue
        if obs.resolve(conn, row["id"], "cleared: the watchdog checked again and it no longer holds"):
            retired += 1
    return retired


def run_checks(conn) -> list[dict]:
    """Every check. One failing check never stops the others."""
    found: list[dict] = []
    for name, fn in (
        ("units", lambda: check_units()),
        ("disk", lambda: check_disk()),
        ("tick", lambda: check_tick_freshness(conn)),
        ("backups", lambda: check_backups()),
        ("auth", lambda: check_google_auth()),
        ("readstate", lambda: check_readstate(conn)),
        ("daycards", lambda: check_day_cards(conn)),
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

    And deliberately **rule findings only**. The sense-check's LLM pass picks
    its own severity, so a lead could call itself an `error` and reach his
    phone — which is exactly what happened. Every alert he complained about
    over the last fortnight turned out to be one: a 780-minute block, a
    misattributed venture, and at 04:45 "this block rests on automated Machina
    alerts, not human work". All three were real defects. None of them was
    anything he could do at 04:45, because all three needed a code change.
    Those go to `jobs/session_brief.py` and reach a Claude Code session
    instead. Chat is reserved for what only he can fix: a dead unit, an expired
    token, a full disk.
    """
    now = now or datetime.now(timezone.utc)
    return conn.execute(
        """
        SELECT id, source, kind, summary, detail, occurrences, first_seen_at
          FROM observations
         WHERE status = 'open' AND severity = 'error'
           AND detected_by = 'rule'
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
            # The last line is the one that says what to do, and 180 characters
            # cut the phone-runnable command in half — "...Fro". A `read -rs` or
            # a `--manual` invocation is the single most useful thing in the
            # message, so it gets the room, and the cut lands on a word.
            from ..pings.compose import _trim_to_word

            lines.append(f"  {_trim_to_word(detail[-1], DETAIL_CHARS)}")
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

        # A watchdog finding that this run did not reproduce has cleared. The
        # first version never closed them, so a one-off stayed "open" and was
        # re-announced every six hours — six alerts in two days, several of
        # them the same false alarm.
        if not dry:
            retired = retire_cleared(conn, found)
            if retired:
                logger.info("watchdog: %d finding(s) cleared", retired)

        # Housekeeping: leads that stopped recurring stop being shown. Never
        # errors, and never rule findings — those retire only by not
        # reproducing, which the sense-check checks properly.
        if not dry:
            aged = obs.age_out_llm_leads(conn)
            if aged:
                logger.info("watchdog: aged out %d unrepeated lead(s)", aged)
            # Rule findings about a moment ("this call failed") have no other
            # way out: retire_cleared only knows its own kinds. Without this
            # they accumulate and a new session reads a fortnight-old rejection
            # as a current fault.
            cleared = obs.age_out_transient_facts(conn)
            if cleared:
                logger.info("watchdog: cleared %d finding(s) that stopped happening", cleared)

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
