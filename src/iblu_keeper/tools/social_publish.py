"""social_publish — render a Blank Label Team post, show it, and only after
Ignas says yes, publish it to Instagram @blanklabel.team and its Facebook Page.

Spec: the Google Doc "IBLU — social_publish tool (Instagram + Facebook) —
build spec for Claude Code", 2026-11-07. One MCP tool, seven actions:
``preview``, ``publish``, ``schedule``, ``list_scheduled``, ``cancel``,
``status``, ``limits``.

What makes it safe (spec, SAFETY):

* ``publish`` and ``schedule`` refuse unless ``confirm=True``, and the tool is
  in the asking class (MUST_ASK) like every other send — publishing reaches
  the public. Claude shows the preview; Ignas says yes in chat; only then.
* **What he saw is what goes live.** A preview stores the SHA-256 of every
  JPG it hosts and of the artboard source each came from. publish re-hashes
  the hosted files and re-reads the sources from Drive; any difference —
  a slide edited after the preview, a file swapped on disk — is a refusal
  that says which slide, until a new preview.
* A preview older than 24 hours is refused. A second publish of one preview to
  one platform is refused unless ``force=True``.
* ``SOCIAL_DRY_RUN=true`` (the default) runs everything — render, Drive,
  hosting, the database — except Meta's POST calls, which are logged.

Under the global ``DRY_RUN`` (mock mode) every action returns a marked mock
and touches nothing, like every other IBLU tool.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import secrets
import shutil
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ..config import settings
from . import social_meta, social_render

logger = logging.getLogger("iblu_keeper.tools.social_publish")

# Two MCP tools, because the Claude app's Ask/Allow setting is per tool:
# `social_publish` can make a post public, so it asks; `social_read` only reads,
# so it must never ask (HANDOFF §31: one read tool and one write tool per domain).
WRITE_ACTIONS = ("preview", "publish", "schedule", "cancel")
READ_ACTIONS = ("recent_posts", "status", "list_scheduled", "limits")
ACTIONS = WRITE_ACTIONS + READ_ACTIONS
PLATFORMS = ("instagram", "facebook")

ZAGREB = ZoneInfo("Europe/Zagreb")
CAPTION_MAX = 2200
HASHTAG_MAX = 30
HASHTAG_HOUSE = 5
SLIDES_MAX = 10
PREVIEW_TTL = timedelta(hours=24)
SCHEDULE_MIN = timedelta(minutes=10)
SCHEDULE_MAX = timedelta(days=30)
MEDIA_AFTER_PUBLISH = timedelta(hours=24)
MEDIA_UNPUBLISHED = timedelta(days=7)
STALE_PUBLISHING = timedelta(minutes=15)

_POST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_SLIDE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,120}\.dc\.html$")
_TOKEN = re.compile(r"^[A-Za-z0-9]{32}$")
_MEDIA_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}-\d{1,2}\.jpg$")
# Instagram counts a hashtag as '#' followed by word characters.
_HASHTAG = re.compile(r"(?<![\w&])#\w+", re.UNICODE)


class SocialError(ValueError):
    """Bad input. The message names what was expected."""


class SocialRefused(SocialError):
    """A safety rule said no. The message says which, and what to do."""


# --- pure helpers (tested directly) ------------------------------------------


def count_hashtags(caption: str) -> int:
    return len(_HASHTAG.findall(caption or ""))


def validate_inputs(post_id: str | None, slides: list[str] | None, caption: str | None) -> dict:
    """Checks that need no network. Returns ``{hashtag_count, char_count, warnings}``."""
    if not post_id or not _POST_ID.match(post_id):
        raise SocialError("post_id must be letters, digits, '-' or '_' (e.g. 'C23'), at most 40 characters.")
    if not slides:
        raise SocialError("slides must list 1 to 10 .dc.html file names from the project folder, in order.")
    if isinstance(slides, str):
        slides = [slides]
    if len(slides) > SLIDES_MAX:
        raise SocialError(f"{len(slides)} slides — Instagram allows at most {SLIDES_MAX} in a carousel.")
    bad = [s for s in slides if not isinstance(s, str) or not _SLIDE.match(s)]
    if bad:
        raise SocialError(f"not a .dc.html file name in the project folder: {bad}")
    if len(set(slides)) != len(slides):
        raise SocialError("the same slide is listed twice.")
    if caption is None:
        raise SocialError("caption is required (it may be empty only if you pass an empty string on purpose).")
    chars = len(caption)
    if chars > CAPTION_MAX:
        raise SocialError(f"caption is {chars} characters — Instagram's limit is {CAPTION_MAX}.")
    tags = count_hashtags(caption)
    if tags > HASHTAG_MAX:
        raise SocialError(f"caption has {tags} hashtags — Instagram's limit is {HASHTAG_MAX}.")
    warnings = []
    if tags > HASHTAG_HOUSE:
        warnings.append(f"{tags} hashtags — the house rule is {HASHTAG_HOUSE}.")
    return {"hashtag_count": tags, "char_count": chars, "warnings": warnings}


def normalise_platforms(platforms: list[str] | str | None) -> list[str]:
    if platforms is None:
        return list(PLATFORMS)
    if isinstance(platforms, str):
        platforms = [p.strip() for p in platforms.split(",")]
    out = []
    for p in platforms:
        p = (p or "").strip().lower()
        if p not in PLATFORMS:
            raise SocialError(f"unknown platform {p!r} — expected {list(PLATFORMS)}")
        if p not in out:
            out.append(p)
    if not out:
        raise SocialError(f"name at least one platform: {list(PLATFORMS)}")
    return out


def parse_publish_at(value: str, now: datetime) -> datetime:
    """ISO 8601; Europe/Zagreb unless an offset is given; 10 min to 30 days out."""
    if not value:
        raise SocialError("publish_at is required, ISO 8601, e.g. 2026-11-09T18:30 (Zagreb time).")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise SocialError(f"publish_at {value!r} is not ISO 8601 (e.g. 2026-11-09T18:30).") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZAGREB)
    if dt < now + SCHEDULE_MIN:
        raise SocialError("publish_at must be at least 10 minutes in the future.")
    if dt > now + SCHEDULE_MAX:
        raise SocialError("publish_at must be at most 30 days out.")
    return dt.astimezone(timezone.utc)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_fingerprint(html: bytes, assets: dict[str, tuple[str, bytes]]) -> str:
    """The artboard plus every image it references. An edited slide or a
    swapped image both change this."""
    h = hashlib.sha256(html)
    for bid in sorted(assets):
        h.update(bid.encode() + b"\0" + hashlib.sha256(assets[bid][1]).digest())
    return h.hexdigest()


def new_media_token() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


def jpg_size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        if im.format != "JPEG":
            raise SocialError(f"rendered file is {im.format}, not JPEG")
        return im.size


# --- the outside world, behind small seams so the rules can be tested --------


class Store(Protocol):
    def insert_preview(self, row: dict) -> None: ...
    def get_preview(self, preview_id: str) -> dict | None: ...
    def claim_platforms(self, preview_id: str, platforms: list[str], *, force: bool,
                        dry_run: bool, schedule_id: int | None) -> dict[str, int]: ...
    def published_post(self, post_id: str) -> dict[str, str | None]: ...
    def finish_publication(self, pub_id: int, **fields: Any) -> None: ...
    def publications(self, preview_id: str) -> list[dict]: ...
    def insert_schedule(self, row: dict) -> int: ...
    def schedules(self, *, preview_id: str | None = None, active_only: bool = False) -> list[dict]: ...
    def cancel_schedule(self, schedule_id: int) -> dict | None: ...


class Source(Protocol):
    """Drive: read artboards, fonts and assets; save renders."""

    def slide(self, name: str) -> bytes: ...
    def fonts(self) -> dict[str, bytes]: ...
    def assets_for(self, html: str) -> dict[str, tuple[str, bytes]]: ...
    def save_render(self, post_id: str, n: int, jpg: bytes) -> dict: ...


@dataclass
class Deps:
    store: Store
    source: Source
    render: Any  # callable(list[str]) -> list[bytes]
    graph: Any  # callable(dry_run: bool) -> social_meta.GraphClient
    log_work: Any  # callable(content: str, meta: dict) -> dict | None
    now: Any = lambda: datetime.now(timezone.utc)


def _media_root() -> Path:
    return Path(settings.social_media_root).resolve()


def _media_dir(token: str) -> Path:
    if not _TOKEN.match(token or ""):
        raise SocialError("bad media token")
    root = _media_root()
    path = (root / token).resolve()
    if path.parent != root:
        raise SocialError("media path escapes SOCIAL_MEDIA_ROOT")
    return path


def media_file(token: str, name: str) -> Path | None:
    """For the ``/media/<token>/<name>`` route: the file, or None. Never lists."""
    if not _TOKEN.match(token or "") or not _MEDIA_FILE.match(name or ""):
        return None
    path = _media_dir(token) / name
    return path if path.is_file() else None


def _public_url(token: str, name: str) -> str:
    base = (settings.social_public_base_url or "").rstrip("/")
    if not base:
        raise SocialError("SOCIAL_PUBLIC_BASE_URL is not set — Instagram needs a public URL for each image.")
    return f"{base}/{token}/{name}"


# --- actions -----------------------------------------------------------------


def _refresh(deps: Deps) -> None:
    refresh = getattr(deps.source, "refresh", None)
    if refresh:
        refresh()


def preview(deps: Deps, post_id: str, slides: list[str], caption: str) -> dict:
    checks = validate_inputs(post_id, slides, caption)
    if isinstance(slides, str):
        slides = [slides]

    _refresh(deps)
    fonts = deps.source.fonts()
    sources, pages = [], []
    for name in slides:
        html = deps.source.slide(name)
        text = html.decode("utf-8")
        assets = deps.source.assets_for(text)
        sources.append(source_fingerprint(html, assets))
        pages.append(social_render.convert(name, text, fonts, assets))

    jpgs = deps.render(pages)
    if len(jpgs) != len(slides):
        raise social_render.RenderError(f"rendered {len(jpgs)} images for {len(slides)} slides")
    for name, jpg in zip(slides, jpgs):
        size = jpg_size(jpg)
        if size != (social_render.WIDTH, social_render.HEIGHT):
            raise SocialError(f"{name} rendered at {size[0]}x{size[1]}; every image must be exactly 1080x1350.")

    token = new_media_token()
    folder = _media_dir(token)
    folder.mkdir(parents=True, exist_ok=False)
    rows = []
    try:
        for n, (name, jpg, src) in enumerate(zip(slides, jpgs, sources), 1):
            fname = f"{post_id}-{n}.jpg"
            (folder / fname).write_bytes(jpg)
            (folder / fname).chmod(0o644)
            drive = deps.source.save_render(post_id, n, jpg)
            rows.append({
                "n": n, "slide": name, "file": fname,
                "source_sha256": src, "jpg_sha256": sha256(jpg), "bytes": len(jpg),
                "width": social_render.WIDTH, "height": social_render.HEIGHT,
                "public_url": _public_url(token, fname),
                "drive_file_id": drive.get("id"), "drive_url": drive.get("url"),
            })
        folder.chmod(0o755)
        preview_id = "pv_" + secrets.token_hex(8)
        created = deps.now()
        deps.store.insert_preview({
            "preview_id": preview_id, "post_id": post_id, "caption": caption,
            "slides": rows, "hashtag_count": checks["hashtag_count"],
            "char_count": checks["char_count"], "warnings": checks["warnings"],
            "media_token": token, "created_at": created,
        })
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    return {
        "status": "rendered",
        "preview_id": preview_id,
        "post_id": post_id,
        "format": "carousel" if len(rows) > 1 else "single_image",
        "images": [{"n": r["n"], "slide": r["slide"], "drive_url": r["drive_url"],
                    "public_url": r["public_url"], "sha256": r["jpg_sha256"]} for r in rows],
        "caption": caption,
        "hashtag_count": checks["hashtag_count"],
        "char_count": checks["char_count"],
        "warnings": checks["warnings"],
        "expires_at": (created + PREVIEW_TTL).isoformat(),
        "dry_run": settings.social_dry_run,
        "next": "Show Ignas every image and the caption. Publish or schedule ONLY after he says yes in chat.",
    }


def _load_preview(deps: Deps, preview_id: str) -> dict:
    if not preview_id:
        raise SocialError("preview_id is required — run action='preview' first.")
    row = deps.store.get_preview(preview_id)
    if not row:
        raise SocialError(f"no preview {preview_id!r}.")
    return row


def verify_preview(deps: Deps, pv: dict, *, check_age: bool = True) -> None:
    """Every refusal rule about the preview itself. Raises SocialRefused."""
    if check_age and deps.now() - pv["created_at"] > PREVIEW_TTL:
        raise SocialRefused(
            f"preview {pv['preview_id']} is older than 24 hours — run preview again and show it to Ignas."
        )
    if pv.get("media_deleted_at"):
        raise SocialRefused(f"preview {pv['preview_id']}'s hosted images were cleaned up — run preview again.")
    folder = _media_dir(pv["media_token"])
    _refresh(deps)
    for s in pv["slides"]:
        path = folder / s["file"]
        if not path.is_file():
            raise SocialRefused(f"slide {s['n']} ({s['slide']}): the hosted image is gone — run preview again.")
        if sha256(path.read_bytes()) != s["jpg_sha256"]:
            raise SocialRefused(
                f"slide {s['n']} ({s['slide']}): the hosted JPG is not the one Ignas previewed — run preview again."
            )
    for s in pv["slides"]:
        html = deps.source.slide(s["slide"])
        current = source_fingerprint(html, deps.source.assets_for(html.decode("utf-8")))
        if current != s["source_sha256"]:
            raise SocialRefused(
                f"slide {s['n']} ({s['slide']}) changed in Drive after the preview — what Ignas saw is "
                "no longer what would go live. Run preview again and show him the new images."
            )


def _effective_dry_run(row_dry_run: bool | None = None) -> bool:
    return bool(settings.social_dry_run or row_dry_run)


def _require_meta_config(platforms: list[str]) -> None:
    missing = []
    if not settings.meta_system_user_token:
        missing.append("META_SYSTEM_USER_TOKEN")
    if "instagram" in platforms and not settings.meta_ig_user_id:
        missing.append("META_IG_USER_ID")
    if "facebook" in platforms and not settings.meta_page_id:
        missing.append("META_PAGE_ID")
    if missing:
        raise SocialRefused(
            f"live publishing needs {', '.join(missing)} in the server .env — nothing was sent."
        )


def run_publish(deps: Deps, pv: dict, platforms: list[str], *, force: bool, dry_run: bool,
                schedule_id: int | None = None) -> dict:
    """The publish logic shared by ``publish`` and the scheduler. Assumes the
    preview has been verified and confirmation given."""
    if not dry_run:
        _require_meta_config(platforms)
    claims = deps.store.claim_platforms(pv["preview_id"], platforms, force=force,
                                        dry_run=dry_run, schedule_id=schedule_id)
    urls = [s["public_url"] for s in pv["slides"]]
    # A dry run must walk every step before the Meta side is set up, so the
    # ids it does not have yet appear as the spec's own placeholders.
    ig_id = settings.meta_ig_user_id or ("{ig-user-id}" if dry_run else "")
    page_id = settings.meta_page_id or ("{page-id}" if dry_run else "")
    results: dict[str, dict] = {}
    for idx, platform in enumerate(platforms):
        client = deps.graph(dry_run)
        try:
            if platform == "instagram":
                out = social_meta.publish_instagram(client, ig_id, urls, pv["caption"])
            else:
                out = social_meta.publish_facebook(client, page_id, urls, pv["caption"])
            status = "dry_run" if dry_run else "published"
            deps.store.finish_publication(claims[platform], status=status, calls=client.calls, **out)
            results[platform] = {"status": status, "permalink": out["permalink"], "calls": client.calls}
        except social_meta.MetaError as exc:
            deps.store.finish_publication(claims[platform], status="failed", calls=client.calls,
                                          error=str(exc))
            results[platform] = {"status": "failed", "error": str(exc),
                                 "meta_message": exc.meta_message, "calls": client.calls}
        except Exception as exc:  # noqa: BLE001 — no claimed row may stay 'publishing'
            why = f"{type(exc).__name__}: {social_meta.scrub(str(exc))}"
            deps.store.finish_publication(claims[platform], status="failed", calls=client.calls, error=why)
            # The platforms after this one were claimed but never tried: say so,
            # rather than leaving them for fail_stuck's "may or may not have posted".
            for later in platforms[idx + 1:]:
                deps.store.finish_publication(claims[later], status="failed", calls=[],
                                              error=f"not attempted: {platform} failed first ({why})")
            raise

    published = {p: r["permalink"] for p, r in results.items() if r["status"] == "published"}
    logged = None
    entry = (f"Published {pv['post_id']} to {', '.join(published)}: "
             + "; ".join(f"{p} {u}" for p, u in published.items()))
    if published:
        logged = deps.log_work(entry, {"post_id": pv["post_id"], "preview_id": pv["preview_id"],
                                       "platforms": list(published), "permalinks": published,
                                       "schedule_id": schedule_id})
    return {
        "preview_id": pv["preview_id"], "post_id": pv["post_id"], "dry_run": dry_run,
        "platforms": results,
        "work_log": logged if published else None,
        **({"note": "SOCIAL_DRY_RUN is on: every Meta POST above was logged, not sent. Nothing is public."}
           if dry_run else {}),
    }


def publish(deps: Deps, preview_id: str, platforms, force: bool) -> dict:
    """Assumes confirm was checked by ``social_publish`` (the only caller)."""
    plats = normalise_platforms(platforms)
    pv = _load_preview(deps, preview_id)
    verify_preview(deps, pv)
    return run_publish(deps, pv, plats, force=bool(force), dry_run=_effective_dry_run())


def schedule(deps: Deps, preview_id: str, publish_at: str, platforms) -> dict:
    """Assumes confirm was checked by ``social_publish`` (the only caller)."""
    plats = normalise_platforms(platforms)
    now = deps.now()
    when = parse_publish_at(publish_at, now)
    pv = _load_preview(deps, preview_id)
    # The 24-hour rule applies to his yes, which is now; at fire time the hash
    # rules are checked again (a 30-day schedule could never pass a 24h age
    # check at fire time).
    verify_preview(deps, pv)
    done = deps.store.published_post(pv["post_id"])
    already = [p for p in plats if p in done]
    if already:
        raise SocialRefused(f"post {pv['post_id']} is already published to {already} "
                            f"({', '.join(done[p] or '?' for p in already)}); use publish with force=true "
                            "if a second post is really wanted.")
    pending = [s for s in deps.store.schedules(preview_id=preview_id)
               if s["status"] in ("pending", "publishing") and set(s["platforms"]) & set(plats)]
    if pending:
        raise SocialRefused(
            f"schedule {pending[0]['id']} already covers this preview on {sorted(set(pending[0]['platforms']) & set(plats))}"
            " — cancel it first to change the time."
        )
    dry = _effective_dry_run()
    sid = deps.store.insert_schedule({"preview_id": preview_id, "platforms": plats,
                                      "publish_at": when, "dry_run": dry})
    logged = None
    if not dry:
        logged = deps.log_work(
            f"Scheduled {pv['post_id']} for {when.astimezone(ZAGREB):%Y-%m-%d %H:%M} Zagreb on {', '.join(plats)}",
            {"post_id": pv["post_id"], "preview_id": preview_id, "platforms": plats,
             "schedule_id": sid, "publish_at": when.isoformat()},
        )
    return {
        "status": "scheduled", "schedule_id": sid, "preview_id": preview_id, "post_id": pv["post_id"],
        "platforms": plats, "publish_at_utc": when.isoformat(),
        "publish_at_zagreb": when.astimezone(ZAGREB).isoformat(), "dry_run": dry,
        "work_log": logged,
        **({"note": "SOCIAL_DRY_RUN is on: when this fires, Meta POSTs are logged, not sent."} if dry else {}),
    }


def _schedule_view(s: dict) -> dict:
    return {
        "schedule_id": s["id"], "preview_id": s["preview_id"], "post_id": s.get("post_id"),
        "platforms": list(s["platforms"]), "status": s["status"],
        "publish_at_zagreb": s["publish_at"].astimezone(ZAGREB).isoformat(),
        "publish_at_utc": s["publish_at"].astimezone(timezone.utc).isoformat(),
        "dry_run": s["dry_run"],
        **({"result": s["result"]} if s.get("result") else {}),
    }


def list_scheduled(deps: Deps) -> dict:
    items = [_schedule_view(s) for s in deps.store.schedules(active_only=True)]
    return {"items": items, "count": len(items)}


def cancel(deps: Deps, schedule_id: int | None) -> dict:
    if schedule_id is None:
        raise SocialError("schedule_id is required — see list_scheduled.")
    row = deps.store.cancel_schedule(int(schedule_id))
    if row is None:
        raise SocialRefused(f"schedule {schedule_id} is not pending (already publishing, done, "
                            "cancelled, or unknown) — nothing was changed.")
    return {"status": "cancelled", **_schedule_view(row)}


def status(deps: Deps, preview_id: str) -> dict:
    pv = _load_preview(deps, preview_id)
    pubs = deps.store.publications(preview_id)
    scheds = deps.store.schedules(preview_id=preview_id)
    per: dict[str, dict] = {}
    for platform in PLATFORMS:
        state: dict = {"status": "rendered"}
        if any(s["status"] == "pending" and platform in s["platforms"] for s in scheds):
            nxt = min(s["publish_at"] for s in scheds if s["status"] == "pending" and platform in s["platforms"])
            state = {"status": "scheduled", "publish_at_zagreb": nxt.astimezone(ZAGREB).isoformat()}
        mine = [p for p in pubs if p["platform"] == platform]
        if mine:
            last = max(mine, key=lambda p: p["started_at"])
            state = {"status": last["status"]}
            if last["status"] == "published":
                state["permalink"] = last.get("permalink")
            if last["status"] == "failed":
                state["error"] = last.get("error")
            if last["status"] == "dry_run":
                state["note"] = "dry run only — nothing was posted"
            state["at"] = (last.get("finished_at") or last["started_at"]).isoformat()
        per[platform] = state
    return {
        "preview_id": preview_id, "post_id": pv["post_id"],
        "created_at": pv["created_at"].isoformat(),
        "expires_at": (pv["created_at"] + PREVIEW_TTL).isoformat(),
        "media_hosted": not pv.get("media_deleted_at"),
        "platforms": per,
        "schedules": [_schedule_view(s) for s in scheds],
    }


def limits(deps: Deps) -> dict:
    client = deps.graph(False)  # a GET; it is the same in dry run
    try:
        out = social_meta.publishing_limit(client, settings.meta_ig_user_id)
    except social_meta.MetaError as exc:
        return {"status": "error", "error": str(exc), "meta_message": exc.meta_message}
    return {"status": "ok", **out, "note": "Instagram allows 100 API-published posts per rolling 24 hours."}


def recent_posts(deps: Deps, platforms=None, limit: int = social_meta.RECENT_LIMIT) -> dict:
    """The newest posts on each platform, read straight from Meta. GET only:
    it cannot post, change or delete anything, so it lives in the tool that
    never asks."""
    plats = normalise_platforms(platforms)
    limit = max(1, min(int(limit or social_meta.RECENT_LIMIT), 50))
    client = deps.graph(True)  # dry_run=True: any POST would be refused to the network
    out: dict = {}
    for platform in plats:
        try:
            if platform == "instagram":
                posts = social_meta.instagram_recent(client, settings.meta_ig_user_id, limit)
                account = "Instagram @blanklabel.team"
            else:
                posts = social_meta.facebook_recent(client, settings.meta_page_id, limit)
                account = "Facebook Page: Blank Label Team"
            out[platform] = {"account": account, "count": len(posts), "posts": posts}
        except social_meta.MetaError as exc:
            out[platform] = {"error": str(exc), "meta_message": exc.meta_message}
    if any(c["method"] != "GET" for c in client.calls):  # belt and braces
        raise RuntimeError("recent_posts attempted a non-GET call — refusing.")
    return {"platforms": out, "note": "Newest first. Read live from Meta; nothing was changed."}


# --- production wiring ---------------------------------------------------------


class PgStore:
    """``Store`` on IBLU's Postgres (migration 016)."""

    def _conn(self):
        from .. import db

        return db.get_conn()

    def insert_preview(self, row: dict) -> None:
        from psycopg.types.json import Jsonb

        with self._conn() as conn:
            conn.execute(
                """INSERT INTO social_previews (preview_id, post_id, caption, slides, hashtag_count,
                       char_count, warnings, media_token, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (row["preview_id"], row["post_id"], row["caption"], Jsonb(row["slides"]),
                 row["hashtag_count"], row["char_count"], Jsonb(row["warnings"]),
                 row["media_token"], row["created_at"]),
            )

    def get_preview(self, preview_id: str) -> dict | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM social_previews WHERE preview_id = %s",
                                (preview_id,)).fetchone()

    def claim_platforms(self, preview_id, platforms, *, force, dry_run, schedule_id):
        """Atomically refuse duplicates and mark each platform 'publishing'.
        The preview row lock serialises two publishes of the same preview."""
        with self._conn() as conn:
            post_id = conn.execute("SELECT post_id FROM social_previews WHERE preview_id = %s FOR UPDATE",
                                   (preview_id,)).fetchone()["post_id"]
            # Keyed on the POST, not the preview: a second preview of the same
            # post_id must not become a second public post (code review 2026-11-08).
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("social:" + post_id,))
            taken = {r["platform"]: r["status"] for r in conn.execute(
                """SELECT u.platform, u.status FROM social_publications u
                   JOIN social_previews p USING (preview_id)
                   WHERE p.post_id = %s AND u.status IN ('published', 'publishing') AND NOT u.dry_run""",
                (post_id,),
            ).fetchall()}
            in_flight = [p for p in platforms if taken.get(p) == "publishing"]
            if in_flight:
                raise SocialRefused(f"{in_flight}: a publish of this preview is already in progress.")
            dup = [p for p in platforms if p in taken]
            if dup and not force:
                raise SocialRefused(
                    f"post {post_id} is already published to {dup}. Refused: a second post needs "
                    "force=true, and Ignas's explicit yes for a duplicate."
                )
            ids = {}
            for p in platforms:
                ids[p] = conn.execute(
                    """INSERT INTO social_publications (preview_id, platform, status, dry_run, forced, schedule_id)
                       VALUES (%s, %s, 'publishing', %s, %s, %s) RETURNING id""",
                    (preview_id, p, dry_run, bool(force and p in dup), schedule_id),
                ).fetchone()["id"]
            return ids

    def finish_publication(self, pub_id, **f):
        from psycopg.types.json import Jsonb

        with self._conn() as conn:
            conn.execute(
                """UPDATE social_publications SET status=%s, calls=%s, remote_id=%s, permalink=%s,
                       error=%s, finished_at=now() WHERE id=%s""",
                (f["status"], Jsonb(f.get("calls") or []), f.get("remote_id"), f.get("permalink"),
                 f.get("error"), pub_id),
            )

    def published_post(self, post_id):
        """platform -> permalink for every live publication of this post_id."""
        with self._conn() as conn:
            return {r["platform"]: r["permalink"] for r in conn.execute(
                """SELECT u.platform, u.permalink FROM social_publications u
                   JOIN social_previews p USING (preview_id)
                   WHERE p.post_id = %s AND u.status IN ('published', 'publishing') AND NOT u.dry_run""",
                (post_id,),
            ).fetchall()}

    def publications(self, preview_id):
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM social_publications WHERE preview_id = %s ORDER BY started_at", (preview_id,)
            ).fetchall()

    def insert_schedule(self, row):
        with self._conn() as conn:
            return conn.execute(
                """INSERT INTO social_schedules (preview_id, platforms, publish_at, dry_run)
                   VALUES (%s,%s,%s,%s) RETURNING id""",
                (row["preview_id"], row["platforms"], row["publish_at"], row["dry_run"]),
            ).fetchone()["id"]

    def schedules(self, *, preview_id=None, active_only=False):
        where, args = [], []
        if preview_id:
            where.append("s.preview_id = %s")
            args.append(preview_id)
        if active_only:
            where.append("s.status IN ('pending', 'publishing')")
        sql = ("SELECT s.*, p.post_id FROM social_schedules s JOIN social_previews p USING (preview_id)"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY s.publish_at")
        with self._conn() as conn:
            return conn.execute(sql, args).fetchall()

    def cancel_schedule(self, schedule_id):
        with self._conn() as conn:
            row = conn.execute(
                """UPDATE social_schedules SET status='cancelled', cancelled_at=now()
                   WHERE id=%s AND status='pending' RETURNING *""", (schedule_id,)
            ).fetchone()
            if row:
                row["post_id"] = conn.execute("SELECT post_id FROM social_previews WHERE preview_id=%s",
                                              (row["preview_id"],)).fetchone()["post_id"]
            return row


class DriveSource:
    """``Source`` on the Shared Drive folder BLT-Instagram-Design-Export."""

    def __init__(self, folder_id: str, account: str | None):
        self.root = folder_id
        self.account = account or None
        self._svc = None
        self._children: dict[str, dict[str, dict]] = {}
        self._blob_map: dict | None = None
        self._asset_cache: dict[str, tuple[str, bytes]] = {}
        self._dupes: set[tuple[str, str]] = set()

    def refresh(self) -> None:
        """Forget folder listings and images: the next read sees Drive as it is
        now. Called once at the start of a preview or a verification — the hash
        check exists to notice an edit."""
        self._children.clear()
        self._dupes.clear()
        self._blob_map = None
        self._asset_cache.clear()

    @property
    def svc(self):
        # Built on first use: a status or list_scheduled call never needs Drive,
        # and building it on every call cost ~2 MB and a round trip each time.
        if self._svc is None:
            from .drive import _drive

            self._svc = _drive(account=self.account)
        return self._svc

    def _list(self, folder: str) -> dict[str, dict]:
        if folder not in self._children:
            out, token = {}, None
            while True:
                resp = self.svc.files().list(
                    q=f"'{folder}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, md5Checksum)",
                    pageSize=1000, pageToken=token,
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                ).execute()
                for f in resp.get("files", []):
                    if f["name"] in out:
                        self._dupes.add((folder, f["name"]))
                    out.setdefault(f["name"], f)
                token = resp.get("nextPageToken")
                if not token:
                    break
            self._children[folder] = out
        return self._children[folder]

    def _sub(self, name: str) -> str:
        f = self._list(self.root).get(name)
        if not f or f["mimeType"] != "application/vnd.google-apps.folder":
            raise SocialError(f"the export folder has no '{name}' subfolder.")
        return f["id"]

    def _download(self, file_id: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, self.svc.files().get_media(fileId=file_id, supportsAllDrives=True))
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue()

    def slide(self, name: str) -> bytes:
        project = self._sub("project")
        f = self._list(project).get(name)
        if not f:
            raise SocialError(f"{name!r} is not in BLT-Instagram-Design-Export/project.")
        if (project, name) in self._dupes:
            raise SocialError(f"there are two files named {name!r} in the project folder — trash or "
                              "rename one so it is clear which slide is meant.")
        return self._download(f["id"])

    def fonts(self) -> dict[str, bytes]:
        tools = self._list(self._sub("tools"))
        missing = [n for n in social_render.FONT_FILES if n not in tools]
        if missing:
            raise SocialError(f"tools folder is missing {missing}.")
        return {n: self._download(tools[n]["id"]) for n in social_render.FONT_FILES}

    def _blobs(self) -> dict:
        if self._blob_map is None:
            import json

            f = self._list(self._sub("project")).get("blob-map.json") or self._list(self.root).get("blob-map.json")
            if not f:
                raise SocialError("blob-map.json is in neither project/ nor the export root.")
            self._blob_map = json.loads(self._download(f["id"]))
        return self._blob_map

    def assets_for(self, html: str) -> dict[str, tuple[str, bytes]]:
        ids = social_render.blob_ids(html)
        if not ids:
            return {}
        blobs = self._blobs()
        assets = self._list(self._sub("assets"))
        out = {}
        for bid in ids:
            fname = blobs.get(bid)
            if not fname or fname not in assets:
                raise SocialError(f"image /_blob/{bid} has no file in assets/ (blob-map.json → {fname!r}).")
            if bid not in self._asset_cache:
                self._asset_cache[bid] = (fname, self._download(assets[fname]["id"]))
            out[bid] = self._asset_cache[bid]
        return out

    def _folder(self, parent: str, name: str) -> str:
        f = self._list(parent).get(name)
        if f:
            return f["id"]
        created = self.svc.files().create(
            body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]},
            fields="id, name, mimeType", supportsAllDrives=True,
        ).execute()
        self._list(parent)[name] = created
        return created["id"]

    def save_render(self, post_id: str, n: int, jpg: bytes) -> dict:
        """rendered/<post_id>/<post_id>-<n>.jpg — overwriting an earlier render."""
        from googleapiclient.http import MediaInMemoryUpload

        folder = self._folder(self._sub("rendered"), post_id)
        name = f"{post_id}-{n}.jpg"
        media = MediaInMemoryUpload(jpg, mimetype="image/jpeg", resumable=False)
        existing = self._list(folder).get(name)
        if existing:
            res = self.svc.files().update(fileId=existing["id"], media_body=media,
                                          fields="id, webViewLink", supportsAllDrives=True).execute()
        else:
            res = self.svc.files().create(body={"name": name, "parents": [folder]}, media_body=media,
                                          fields="id, webViewLink", supportsAllDrives=True).execute()
            self._list(folder)[name] = {"id": res["id"], "name": name, "mimeType": "image/jpeg"}
        return {"id": res["id"], "url": res.get("webViewLink") or f"https://drive.google.com/file/d/{res['id']}/view"}


def _log_work(content: str, meta: dict) -> dict | None:
    from . import context

    try:
        return context.log_entry(type="work_log", content=content, venture="blt", project="instagram",
                                 tags=["social_publish"], meta=meta)
    except Exception:  # noqa: BLE001 — the post is live; a failed log must not hide that
        logger.exception("social_publish: could not write the work_log entry")
        from ..store import observations

        observations.record_safe(source="social_publish", kind="work_log_failed",
                                 summary="a publish succeeded but its work_log entry was not written",
                                 detail=content, evidence=meta)
        return None


def production_deps() -> Deps:
    from ..config import resolve_account

    acct = resolve_account(settings.social_drive_account or None)
    return Deps(
        store=PgStore(),
        source=DriveSource(settings.social_drive_folder_id, acct),
        render=social_render.render_pages,
        graph=lambda dry: social_meta.GraphClient(settings.meta_system_user_token,
                                                  settings.meta_graph_version, dry_run=dry),
        log_work=_log_work,
    )


def _dispatch(action: str, deps: Deps | None, **kw) -> dict:
    if settings.use_mock and deps is None:
        return {"status": f"not_{action}_mock", "_mock": True, "action": action,
                "note": "DRY_RUN (mock mode) is on server-wide: nothing was read, rendered, stored or sent."}
    deps = deps or production_deps()
    if action == "preview":
        return preview(deps, kw["post_id"], kw["slides"], kw["caption"])
    if action == "publish":
        return publish(deps, kw["preview_id"], kw["platforms"], kw["force"])
    if action == "schedule":
        return schedule(deps, kw["preview_id"], kw["publish_at"], kw["platforms"])
    if action == "cancel":
        return cancel(deps, kw["schedule_id"])
    if action == "recent_posts":
        return recent_posts(deps, kw.get("platforms"), kw.get("limit") or social_meta.RECENT_LIMIT)
    if action == "list_scheduled":
        return list_scheduled(deps)
    if action == "status":
        return status(deps, kw["preview_id"])
    return limits(deps)


def social_publish(*, action: str, post_id=None, slides=None, caption=None, preview_id=None,
                   platforms=None, confirm=False, force=False, publish_at=None, schedule_id=None,
                   deps: Deps | None = None) -> dict:
    """The asking tool: preview, publish, schedule, cancel."""
    if action in READ_ACTIONS:
        raise SocialError(f"{action!r} is a read — use social_read, which never asks.")
    if action not in WRITE_ACTIONS:
        raise SocialError(f"unknown action {action!r} — social_publish does {list(WRITE_ACTIONS)}; "
                          f"social_read does {list(READ_ACTIONS)}")
    if action == "preview":
        validate_inputs(post_id, slides, caption)
    if action in ("publish", "schedule") and confirm is not True:
        raise SocialRefused(
            f"refused: {action} needs confirm=true, and confirm=true only after Ignas has seen the "
            "preview and said yes in chat."
        )
    return _dispatch(action, deps, post_id=post_id, slides=slides, caption=caption,
                     preview_id=preview_id, platforms=platforms, confirm=confirm, force=force,
                     publish_at=publish_at, schedule_id=schedule_id)


def social_read(*, action: str, preview_id=None, platforms=None, limit=None,
                deps: Deps | None = None) -> dict:
    """The read tool: recent_posts, status, list_scheduled, limits. Nothing here
    posts, changes or deletes anything."""
    if action in WRITE_ACTIONS:
        raise SocialError(f"{action!r} changes something — use social_publish, which asks first.")
    if action not in READ_ACTIONS:
        raise SocialError(f"unknown action {action!r} — social_read does {list(READ_ACTIONS)}; "
                          f"social_publish does {list(WRITE_ACTIONS)}")
    return _dispatch(action, deps, preview_id=preview_id, platforms=platforms, limit=limit)
