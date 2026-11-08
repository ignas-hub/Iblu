"""Every minute: publish what social_publish scheduled, and clean up hosted media.

Run by ``iblu-social.timer``. Three passes, each independent:

1. **Due schedules.** A pending row whose ``publish_at`` has passed is claimed
   (``FOR UPDATE SKIP LOCKED`` → ``publishing``, committed before any Meta
   call, so two overlapping runs can never post it twice) and published with
   the same logic as ``publish``: the hosted JPGs and the Drive sources are
   re-hashed against the preview. The 24-hour preview age was checked when
   Ignas confirmed the schedule and is not re-applied here — a schedule may be
   up to 30 days out. A schedule made under SOCIAL_DRY_RUN stays a dry run
   even if the switch has since been turned off: a test never goes live.
2. **Stuck rows.** A schedule or publication still ``publishing`` after 15
   minutes means the process died mid-call. Whether Meta posted is unknown,
   so it is marked failed with that sentence and NEVER retried automatically —
   a retry is a possible duplicate post on a public account.
3. **Media cleanup.** A token folder goes 24 hours after a successful publish,
   or 7 days after creation if never published — but never while a pending
   schedule still needs it.

A failed scheduled publish is recorded as an observation (rule): nobody asked
for it in a conversation, so nothing else would surface it.
"""

from __future__ import annotations

import logging
import shutil
import sys
from datetime import datetime, timezone

from .. import db
from ..config import settings
from ..store import observations
from ..tools import social_publish as sp

logger = logging.getLogger("iblu_keeper.jobs.social_worker")


def run_due(deps: sp.Deps | None = None) -> list[dict]:
    done = []
    while True:
        with db.get_conn() as conn:
            row = conn.execute(
                """UPDATE social_schedules SET status='publishing', started_at=now()
                   WHERE id = (SELECT id FROM social_schedules
                               WHERE status='pending' AND publish_at <= now()
                               ORDER BY publish_at LIMIT 1 FOR UPDATE SKIP LOCKED)
                   RETURNING *"""
            ).fetchone()
        if not row:
            return done
        result: dict
        try:
            # Inside the try: the schedule is already claimed, so any failure
            # here must land on its row, not leave it 'publishing'.
            deps = deps or sp.production_deps()
            pv = deps.store.get_preview(row["preview_id"])
            sp.verify_preview(deps, pv, check_age=False)
            result = sp.run_publish(deps, pv, list(row["platforms"]), force=False,
                                    dry_run=sp._effective_dry_run(row["dry_run"]), schedule_id=row["id"])
            failed = {p: r.get("error") for p, r in result["platforms"].items() if r["status"] == "failed"}
            final = "failed" if failed else "done"
        except Exception as exc:  # noqa: BLE001 — every outcome is written to the row
            failed = {"all": str(exc)}
            result = {"error": str(exc)}
            final = "failed"
        safe = {k: v for k, v in result.items() if k != "platforms"}
        if "platforms" in result:
            safe["platforms"] = {p: {k: v for k, v in r.items() if k != "calls"}
                                 for p, r in result["platforms"].items()}
        from psycopg.types.json import Jsonb

        with db.get_conn() as conn:
            conn.execute("UPDATE social_schedules SET status=%s, finished_at=now(), result=%s WHERE id=%s",
                         (final, Jsonb(safe), row["id"]))
        if failed:
            observations.record_safe(
                source="social_publish", kind="scheduled_publish_failed", severity="error",
                summary=f"scheduled post {row['id']} ({row['preview_id']}) did not publish",
                detail="; ".join(f"{p}: {e}" for p, e in failed.items()),
                evidence={"schedule_id": row["id"], "preview_id": row["preview_id"]},
                fp=f"social-schedule-{row['id']}",
            )
        done.append({"schedule_id": row["id"], "status": final, **safe})


def fail_stuck() -> int:
    note = ("interrupted while publishing — Meta may or may not have posted it. Check Instagram and "
            "the Facebook Page before publishing again; IBLU will not retry on its own.")
    with db.get_conn() as conn:
        a = conn.execute(
            """UPDATE social_schedules SET status='failed', finished_at=now(),
                   result = result || jsonb_build_object('error', %s::text)
               WHERE status='publishing' AND started_at < now() - %s""",
            (note, sp.STALE_PUBLISHING),
        ).rowcount
        b = conn.execute(
            """UPDATE social_publications SET status='failed', finished_at=now(), error=%s
               WHERE status='publishing' AND started_at < now() - %s""",
            (note, sp.STALE_PUBLISHING),
        ).rowcount
    if a or b:
        observations.record_safe(source="social_publish", kind="publish_interrupted", severity="error",
                                 summary="a social publish was interrupted mid-call",
                                 detail=f"{a} schedule(s) and {b} publication(s) marked failed. {note}",
                                 fp="social-publish-interrupted")
    return a + b


def cleanup_media(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT p.preview_id, p.media_token, p.created_at,
                      (SELECT max(finished_at) FROM social_publications u
                        WHERE u.preview_id = p.preview_id AND u.status = 'published') AS published_at,
                      EXISTS (SELECT 1 FROM social_schedules s
                        WHERE s.preview_id = p.preview_id AND s.status IN ('pending','publishing')) AS needed
               FROM social_previews p WHERE p.media_deleted_at IS NULL"""
        ).fetchall()
    gone = []
    for r in rows:
        if r["needed"]:
            continue
        expired = (r["published_at"] and now - r["published_at"] > sp.MEDIA_AFTER_PUBLISH) or (
            not r["published_at"] and now - r["created_at"] > sp.MEDIA_UNPUBLISHED)
        if not expired:
            continue
        shutil.rmtree(sp._media_dir(r["media_token"]), ignore_errors=True)
        with db.get_conn() as conn:
            conn.execute("UPDATE social_previews SET media_deleted_at = now() WHERE preview_id = %s",
                         (r["preview_id"],))
        gone.append(r["preview_id"])
    return gone


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings.use_mock:
        logger.error("DRY_RUN (mock mode) is on — the social worker writes nothing and exits.")
        return 2
    if not db.is_configured():
        logger.error("DATABASE_URL is not set.")
        return 2
    stuck = fail_stuck()
    due = run_due()
    gone = cleanup_media()
    if stuck or due or gone:
        logger.info("social worker: %d stuck marked failed, %d schedule(s) run, %d media folder(s) removed",
                    stuck, len(due), len(gone))
    return 0


if __name__ == "__main__":
    sys.exit(main())
