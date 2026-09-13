"""The weekly review — the uncomfortable numbers, delivered unasked.

    python -m iblu_keeper.jobs.weekly [--dry] [--window 7d] [--yes]

Mission stage 2: "a weekly review: attention per venture and work type, what he
touched three times that should be someone else's, what to automate.
Uncomfortable numbers are the product."

On-demand review is a review that gets read when Ignas remembers to ask. This
posts it into the Secretary space on a Sunday evening whether he asks or not.

It reuses the ping delivery path (same webhook, same space) but sends plain
text, not a card: there is nothing to tap. The point is to be read.
"""

from __future__ import annotations

import argparse
import logging
import sys

import requests
from psycopg.types.json import Jsonb

from .. import db
from ..config import settings
from ..tools import review as review_tools

logger = logging.getLogger("iblu_keeper.jobs.weekly")

TIMEOUT = 20
# Below this, a "review" is noise dressed as insight.
MIN_SIGNALS = 10


def _guard() -> str | None:
    if settings.use_mock:
        return "refusing to run in mock mode (DRY_RUN=true)"
    if not db.is_configured():
        return "refusing to run without DATABASE_URL"
    if not settings.secretary_webhook_url:
        return "refusing to run without SECRETARY_WEBHOOK_URL"
    return None


def compose_review(window: str = "7d") -> tuple[str, dict]:
    """Return `(text, data)` — the message to post and the raw numbers."""
    data = review_tools.review(window)
    body = review_tools.as_markdown(data)

    signals = data.get("coverage", {}).get("signals", 0)
    if signals < MIN_SIGNALS:
        body += (
            f"\n\n_Only {signals} signals this week — too little to draw a "
            "conclusion from. Reported so the gap is visible, not hidden._"
        )

    header = f"*Weekly review — last {window}*\n\n"
    return header + body, data


def send(text: str) -> str:
    """Post the review into the Secretary space. Returns the message name."""
    url = settings.secretary_webhook_url
    separator = "&" if "?" in url else "?"
    url = f"{url}{separator}threadKey=weekly-review"

    try:
        response = requests.post(url, json={"text": text}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"webhook unreachable: {exc}") from exc
    if response.status_code >= 400:
        # Never log the URL — it carries the webhook key and token.
        raise RuntimeError(f"webhook returned {response.status_code}: {response.text[:200]}")
    return (response.json() or {}).get("name", "")


def run(window: str = "7d", dry: bool = False, assume_yes: bool = False) -> int:
    refusal = _guard()
    if refusal:
        logger.error("weekly: %s", refusal)
        return 1

    text, data = compose_review(window)

    if dry:
        print(text)
        logger.info(
            "weekly [dry]: %d signals, %d recurring threads — nothing sent",
            data["coverage"]["signals"], len(data["recurring"]),
        )
        return 0

    name = send(text)

    # The review is a durable conclusion about the week, so it belongs in
    # memory — not in `signals`, which is observation only (plan D1).
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO context_entries
                (type, content, importance, tags, source, source_ref, meta)
            VALUES ('decision', %s, 4, %s, 'analyst', %s, %s)
            """,
            (
                text,
                ["weekly_review", window],
                name or f"weekly:{data['since'][:10]}",
                Jsonb({
                    "window": window,
                    "coverage": data["coverage"],
                    "by_venture": data["by_venture"],
                    "pings": data["pings"],
                }),
            ),
        )

    logger.info("weekly: review sent (%s signals) as %s", data["coverage"]["signals"], name)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m iblu_keeper.jobs.weekly",
        description="Post the weekly attention review into the Secretary space.",
    )
    parser.add_argument("--window", default="7d", help="review window (default 7d)")
    parser.add_argument("--dry", action="store_true", help="print it, send nothing")
    parser.add_argument("--yes", action="store_true", help="skip the send confirmation")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if not args.dry and not args.yes and sys.stdin.isatty():
        if input("post the weekly review to Secretary now? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("cancelled")
            return 0

    try:
        return run(window=args.window, dry=args.dry, assume_yes=args.yes)
    finally:
        db.close_pool()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
