"""The analyst pass — reconstruct a day and mirror it (plan §12).

    python -m iblu_keeper.jobs.analyst [--date YYYY-MM-DD] [--days N] [--dry]
                                       [--no-mirror]

Run by `iblu-analyst.timer` at 17:00 on weekdays, after the day's work has
mostly happened and before the evening ping asks about it. Running it again
later is not just safe but expected: the evening's signals arrive after 17:00,
so the last run of a day is the one that counts, and every rerun supersedes
rather than edits.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

from .. import db
from ..config import settings

logger = logging.getLogger("iblu_keeper.jobs.analyst")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _guard() -> str | None:
    if settings.use_mock:
        return (
            "refusing to run in mock mode (DRY_RUN=true) — the reconstruction "
            "would be built from fabricated signals"
        )
    if not db.is_configured():
        return "refusing to run without DATABASE_URL — nothing to reconstruct from"
    return None


def _today() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(settings.iblu_timezone)).date()


def run(on: date | None = None, days: int = 1, dry: bool = False, mirror: bool = True) -> int:
    refusal = _guard()
    if refusal:
        logger.error("analyst: %s", refusal)
        return 1

    from ..analyst.blocks import reconstruct

    last = on or _today()
    targets = [last - timedelta(days=n) for n in reversed(range(days))]

    failures = 0
    with db.get_conn() as conn:
        for day in targets:
            try:
                # A savepoint per day, for the same reason as the collectors:
                # a database error on day 3 of a --days 5 backfill would
                # otherwise poison the transaction and roll back days 1 and 2
                # on the final commit, having reported them as written.
                with conn.transaction():
                    summary = reconstruct(conn, day, dry=dry, mirror=mirror)
            except Exception:  # one bad day must not lose the others
                logger.exception("analyst: %s failed", day)
                failures += 1
                continue
            minutes = summary["minutes"]
            logger.info(
                "analyst %s: signals=%d intents=%d blocks=%d "
                "present=%dm displaced=%dm ambiguous=%dm%s",
                day,
                summary["signals"],
                summary["intents"],
                summary["blocks"],
                minutes["present"],
                minutes["displaced"],
                minutes["ambiguous"],
                " [dry]" if dry else "",
            )
            # The analyst is the last thing to touch the day, so it is where
            # IBLU checks its own work: deterministic invariants first, then
            # the model reading what the scripts produced. Findings go to
            # `observations`; neither pass can fail this job.
            if not dry:
                try:
                    from ..analyst.sensecheck import run as sensecheck

                    checked = sensecheck(conn, day)
                    if checked["findings"]:
                        logger.info(
                            "sensecheck %s: %d finding(s) (%d rule, %d llm) — "
                            "see `python -m iblu_keeper.store.observations`",
                            day, checked["findings"], checked["rules"], checked["llm"],
                        )
                except Exception:  # noqa: BLE001 — a checker must not break its job
                    logger.warning("analyst: sense-check failed", exc_info=True)

            if dry:
                for row in summary.get("preview", []):
                    logger.info(
                        "  %s–%s %-9s %-10s %s",
                        row["start"], row["end"],
                        row["venture"] or "—",
                        row["attention"],
                        row["reasoning"],
                    )
    return 1 if failures and failures == len(targets) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m iblu_keeper.jobs.analyst",
        description="Reconstruct the day from signals and mirror it onto the Secretary calendar.",
    )
    parser.add_argument("--date", help="local date to rebuild (default: today)")
    # Two by default: today AND yesterday. Yesterday's evening is never seen by
    # yesterday's last run (20:15), so a family event at 21:00, or a chat sent
    # after the last tick, would otherwise stay judged on incomplete data
    # forever. The family-presence inference in particular refuses to guess
    # past the data horizon, so without this second pass those evenings would
    # stay "unknown" for good.
    parser.add_argument(
        "--days", type=int, default=2,
        help="rebuild this many days ending at --date (default 2: today and yesterday)",
    )
    parser.add_argument(
        "--dry", action="store_true",
        help="print the reconstruction and write nothing",
    )
    parser.add_argument(
        "--no-mirror", action="store_true",
        help="write blocks but do not touch the Secretary calendar",
    )
    args = parser.parse_args(argv)
    _configure_logging()

    on = date.fromisoformat(args.date) if args.date else None
    try:
        return run(on=on, days=max(1, args.days), dry=args.dry, mirror=not args.no_mirror)
    finally:
        db.close_pool()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
