"""Mail a machine sent, filed under his name.

    python -m iblu_keeper.collectors.automated [--days N] [--dry]

`config.automated_senders` handles the case where the machine's address is
known: `machina@deadlift.io` is a verified send-as alias on his Deadlift
mailbox, so everything the Machina monitoring job posts lands in `in:sent` and
the alias check admits it as his own work.

That list only helps for addresses somebody has already identified. This module
is the part that needs no list, and it rests on one observation: **a human
cannot compose three separate messages with the same subject inside two
minutes.** A script does it in three seconds. The five 04:58 "Machina: Tagger
accuracy below target" messages arrived across three seconds, and became a
thirty-minute block described as "alerts read at dawn".

Deliberately narrow. The first draft used a five-minute window, which would
also have swallowed a genuine morning of forwarding one contract to three
people separately — under-reporting his work to fix over-reporting it. Two
minutes cannot be composed by hand, so the rule stays a fact rather than a
guess.

Nothing is deleted. The rows get `excluded_reason` (migration 009), so the
analyst skips them, `jobs/audit.py` can still count them, and reversing the
call is an UPDATE rather than a re-collection.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("iblu_keeper.collectors.automated")

# How close together, and how many. A human cannot do this; a cron does it in
# one pass. Measured each side of a row, so BURST_WINDOW=60s means "three
# messages sharing a subject inside a two-minute span".
BURST_WINDOW = timedelta(seconds=60)
BURST_MIN = 3

REASON = (
    "automated fan-out: {n} messages sharing this subject from the same mailbox "
    "within {span}s — sent by a script, not composed"
)


def mark_fan_out(
    conn,
    *,
    since: datetime | None = None,
    window: timedelta = BURST_WINDOW,
    threshold: int = BURST_MIN,
    dry: bool = False,
) -> list[dict]:
    """Exclude bursts of identical-subject sent mail. Returns what it marked.

    The peer count deliberately ignores `excluded_reason`, so a second run sees
    the same burst and reaches the same conclusion. Counting only unexcluded
    peers would make the function eat its own evidence: mark three, then find
    two peers next time, then fall under the threshold and start marking
    nothing.
    """
    since = since or datetime.now(timezone.utc) - timedelta(days=2)
    rows = conn.execute(
        """
        SELECT s.id, s.account, s.subject, s.occurred_at,
               (SELECT count(*) FROM signals p
                 WHERE p.source = 'gmail' AND p.actor = 'me'
                   AND p.account = s.account AND p.subject = s.subject
                   AND p.occurred_at BETWEEN s.occurred_at - %(window)s
                                         AND s.occurred_at + %(window)s
               ) AS peers
          FROM signals s
         WHERE s.source = 'gmail' AND s.actor = 'me'
           AND s.excluded_reason IS NULL
           AND s.subject IS NOT NULL AND s.subject <> ''
           AND s.occurred_at >= %(since)s
         ORDER BY s.occurred_at
        """,
        {"since": since, "window": window},
    ).fetchall()

    burst = [dict(r) for r in rows if int(r["peers"]) >= threshold]
    if not burst or dry:
        return burst

    reason = REASON.format(n=threshold, span=int(window.total_seconds() * 2))
    conn.execute(
        "UPDATE signals SET excluded_reason = %s WHERE id = ANY(%s)",
        (reason, [r["id"] for r in burst]),
    )
    return burst


def backfill_senders(conn, *, since: datetime, dry: bool = False) -> dict:
    """Recover `meta.from` for old signals, then apply the named-sender rule.

    `gmail_sent` only started recording the sender today, so rows written
    before that cannot be matched against `config.automated_senders` at all —
    the five singleton Machina notices ("Machina test email", "Ad ...
    submitted") are machine output that no burst rule will ever catch, and
    nothing in the database says who sent them.

    One metadata fetch per row, so it is bounded by `--days`. A message that
    has since been deleted is skipped rather than fatal.
    """
    from ..config import settings
    from ..google_auth import build_service

    rows = conn.execute(
        """
        SELECT id, account, source_ref, subject FROM signals
         WHERE source = 'gmail' AND actor = 'me'
           AND meta->>'from' IS NULL AND occurred_at >= %s
         ORDER BY occurred_at
        """,
        (since,),
    ).fetchall()

    by_email = {a["email"]: a["alias"] for a in settings.configured_accounts()}
    automated = settings.automated_sender_addresses
    services: dict[str, object] = {}
    filled = excluded = unreadable = 0

    for row in rows:
        alias = by_email.get(row["account"])
        if alias is None:
            unreadable += 1
            continue
        if alias not in services:
            services[alias] = build_service(
                "gmail", "v1",
                account=None if alias == settings.primary_alias else alias,
            )
        try:
            message = (
                services[alias].users().messages()
                .get(userId="me", id=row["source_ref"], format="metadata",
                     metadataHeaders=["From"])
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 — a deleted message is not a fault
            logger.debug("cannot read %s: %s", row["source_ref"], exc)
            unreadable += 1
            continue

        sender = ""
        for header in message.get("payload", {}).get("headers", []):
            if header.get("name", "").lower() == "from":
                from email.utils import parseaddr

                sender = parseaddr(header.get("value", ""))[1].strip().lower()
        if not sender:
            unreadable += 1
            continue

        filled += 1
        if dry:
            if sender in automated:
                excluded += 1
                logger.info("would exclude %s <- %s: %s", row["id"], sender,
                            (row["subject"] or "")[:60])
            continue

        reason = (
            f"automated sender: {sender} is a service identity, not an address "
            "Ignas writes from"
        ) if sender in automated else None
        conn.execute(
            """
            UPDATE signals
               -- Both casts are required: Postgres cannot infer a parameter's
               -- type inside jsonb_build_object, and refuses the statement
               -- with IndeterminateDatatype rather than guessing.
               SET meta = meta || jsonb_build_object('from', %s::text),
                   excluded_reason = COALESCE(excluded_reason, %s::text)
             WHERE id = %s
            """,
            (sender, reason, row["id"]),
        )
        if reason:
            excluded += 1

    return {"seen": len(rows), "filled": filled, "excluded": excluded,
            "unreadable": unreadable}


def main(argv: list[str] | None = None) -> int:
    from .. import db

    parser = argparse.ArgumentParser(
        prog="python -m iblu_keeper.collectors.automated",
        description="Exclude sent mail a machine wrote under his name.",
    )
    parser.add_argument("--days", type=int, default=2,
                        help="how far back to look (default 2)")
    parser.add_argument("--dry", action="store_true",
                        help="report what would be excluded; change nothing")
    parser.add_argument("--backfill-senders", action="store_true",
                        help="recover meta.from from Gmail for older rows, then "
                             "apply the named-sender rule")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    if not db.is_configured():
        print("no DATABASE_URL")
        return 1
    try:
        with db.get_conn() as conn:
            since = datetime.now(timezone.utc) - timedelta(days=args.days)
            if args.backfill_senders:
                stats = backfill_senders(conn, since=since, dry=args.dry)
                print(
                    f"senders: {stats['filled']}/{stats['seen']} recovered, "
                    f"{stats['excluded']} excluded, {stats['unreadable']} unreadable"
                )
            marked = mark_fan_out(conn, since=since, dry=args.dry)
            verb = "would exclude" if args.dry else "excluded"
            print(f"{verb} {len(marked)} signal(s)")
            for row in marked:
                print(f"  {row['occurred_at']:%Y-%m-%d %H:%M} {row['account']} "
                      f"({row['peers']}x) {(row['subject'] or '')[:70]}")
        return 0
    finally:
        db.close_pool()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
