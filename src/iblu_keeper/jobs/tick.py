"""The tick job — the heartbeat of the recorder (plan §6).

    python -m iblu_keeper.jobs.tick [--dry] [--force-ping KIND] [--yes]

Run by `iblu-tick.timer` every 10 minutes, 07:00–19:50 Mon–Fri. Each run:

  1. refuse to do anything in mock mode;
  2. run the collectors (a failing one is recorded, not fatal);
  3. read Secretary thread replies  [session 3];
  4. decide whether to send a ping  [session 3];
  5. exit 0 with one summary line.

All state lives in the database, so a missed or repeated run is harmless.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .. import db
from ..config import settings

logger = logging.getLogger("iblu_keeper.jobs.tick")

PING_KINDS = ("midday", "evening", "test")


def _configure_logging() -> None:
    # Under systemd, stdout goes to the journal; keep it one line per fact.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _guard() -> str | None:
    """Return a refusal reason, or None when it is safe to run.

    Mock mode is the important one: DRY_RUN=true means every Google call
    returns invented data, and invented data must never be written to a real
    database (plan rule 7 / DEBUG_FINDINGS.md).
    """
    if settings.use_mock:
        return (
            "refusing to run in mock mode (DRY_RUN=true) — collectors would "
            "write fabricated rows into the database"
        )
    if not db.is_configured():
        return "refusing to run without DATABASE_URL — nowhere to record"
    if settings.misconfigured_live:
        return (
            "refusing to run: DRY_RUN=false but no usable Google token — "
            "every collector would fail"
        )
    return None


def _summary(results: dict) -> str:
    short = {
        "gmail_sent": "gmail",
        "chat_sent": "chat",
        "calendar_changes": "cal",
        "slack_sent": "slack",
        "git_commits": "git",
    }
    parts = []
    for name, value in results.items():
        # Keys are "<collector>" or "<collector>:<account alias>".
        base, _, alias = name.partition(":")
        label = short.get(base, base) + (f":{alias}" if alias else "")
        parts.append(
            f"{label}={value:+d}" if isinstance(value, int) else f"{label}=ERROR"
        )
    return " ".join(parts)


def run(dry: bool = False, force_ping: str | None = None, assume_yes: bool = False) -> int:
    """One tick. Returns the process exit code."""
    refusal = _guard()
    if refusal:
        logger.error("tick: %s", refusal)
        return 1

    from ..collectors import run_all

    with db.get_conn() as conn:
        results = run_all(conn, dry=dry)

    failed = [name for name, value in results.items() if not isinstance(value, int)]

    # A collector that ran cleanly this tick clears its own earlier failure.
    # A single network blip on one Deadlift tick (SSLEOFError, recovered ten
    # minutes later) otherwise stayed an open error and was re-announced.
    succeeded = [name for name, value in results.items() if isinstance(value, int)]
    if succeeded and not dry:
        from ..store import observations as obs

        try:
            with db.get_conn() as conn:
                for name in succeeded:
                    row = conn.execute(
                        "SELECT id FROM observations WHERE status = 'open' AND fingerprint = %s",
                        (obs.fingerprint("tick", "collector_failed", name),),
                    ).fetchone()
                    if row:
                        obs.resolve(conn, row["id"], "cleared: the collector ran cleanly on a later tick")
        except Exception:  # noqa: BLE001 — bookkeeping must not fail the tick
            logger.warning("tick: could not clear recovered collector failures", exc_info=True)
    for name in failed:
        logger.error("tick: collector %s failed: %s", name, results[name])
        # One collector failing never stops the others, which is exactly why it
        # can go unnoticed for days. The journal rotates; this does not.
        from ..store import observations as obs

        obs.record_safe(
            source="tick", kind="collector_failed", severity="error",
            summary=f"collector {name} failed during the tick",
            detail=str(results[name])[:1000],
            evidence={"collector": name},
            fp=obs.fingerprint("tick", "collector_failed", name),
        )

    # --- free-text replies in the Secretary thread -------------------------
    replies = 0
    try:
        from ..pings.answers import read_thread_replies

        with db.get_conn() as conn:
            replies = read_thread_replies(conn, dry=dry)
    except Exception as exc:  # noqa: BLE001 - reading replies is not critical
        logger.warning("tick: could not read Secretary replies: %s", exc)

    # --- ping decision -----------------------------------------------------
    from ..pings.runner import run_pings

    ping_note = run_pings(dry=dry, force_kind=force_ping)

    logger.info(
        "tick: %s replies=+%d ping=%s%s",
        _summary(results),
        replies,
        ping_note,
        " [dry]" if dry else "",
    )
    # A collector outage is reported loudly but does not fail the timer: the
    # next tick retries, and a non-zero exit would just spam systemd.
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m iblu_keeper.jobs.tick",
        description="Collect signals, read replies, maybe send a ping.",
    )
    parser.add_argument(
        "--dry",
        action="store_true",
        help="read only: run collectors, print what would be written, write nothing",
    )
    parser.add_argument(
        "--force-ping",
        choices=PING_KINDS,
        metavar="KIND",
        help="compose and send a ping now, ignoring window and gap rules",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt for --force-ping",
    )
    args = parser.parse_args(argv)

    _configure_logging()

    if args.force_ping and not args.yes:
        # Never send anything to a real person without an explicit yes.
        answer = input(f"send a {args.force_ping} ping to Secretary now? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("cancelled")
            return 0

    try:
        return run(dry=args.dry, force_ping=args.force_ping, assume_yes=args.yes)
    finally:
        db.close_pool()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
