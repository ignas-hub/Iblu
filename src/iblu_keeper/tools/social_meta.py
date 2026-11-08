"""Meta Graph API: an Instagram carousel / single image, a Facebook Page
multi-photo post, and the Instagram publishing quota.

The call sequence is the spec's, step for step (META GRAPH API CALLS). Two
properties every caller relies on:

* **Dry run stops at POST.** With ``dry_run`` the client performs read-only
  GETs when a token is configured (so a dry run still proves the token and its
  permissions) and RECORDS every POST instead of sending it. A POST in dry run
  returns an id spelled ``DRY_RUN-…`` so the flow can continue; that id is
  never stored as a permalink or presented as a post.
* **The token never leaves this module.** It travels in the Authorization
  header, never in a URL, and ``calls`` — the log returned to the tool and
  stored in ``social_publications.calls`` — holds method, path and params only.

Errors keep Meta's own message verbatim (status reports it as-is) and add one
plain-language sentence naming the permission or setting that is missing.
There is no fallback: a token problem stops the publish.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

import requests

logger = logging.getLogger("iblu_keeper.tools.social_meta")

GRAPH = "https://graph.facebook.com"
POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 300

# The permissions the system-user token must hold (spec, Meta side, step 4).
REQUIRED_PERMISSIONS = (
    "instagram_basic", "instagram_content_publish", "pages_show_list",
    "pages_read_engagement", "pages_manage_posts", "business_management",
)

_SECRETS = re.compile(r"(access_token|token|appsecret_proof)=([^&\s\"']+)", re.I)


class MetaError(RuntimeError):
    """A Graph API call failed. ``meta_message`` is Meta's text, verbatim."""

    def __init__(self, plain: str, meta_message: str = "", code: int | None = None,
                 subcode: int | None = None, call: str = ""):
        self.plain = plain
        self.meta_message = meta_message
        self.code = code
        self.subcode = subcode
        self.call = call
        text = plain if not meta_message else f"{plain} Meta said: {meta_message}"
        super().__init__(text)


def scrub(text: str) -> str:
    return _SECRETS.sub(lambda m: f"{m.group(1)}=<redacted>", text or "")


def _explain(code: int | None, subcode: int | None, message: str, needs: str) -> str:
    """One plain sentence for Ignas. ``needs`` is the permission this call uses."""
    if code == 190:
        return (
            "The Meta token is expired or was revoked (Meta error 190). In Business "
            "settings > Users > System users, generate a new token for the IBLU system "
            f"user with {', '.join(REQUIRED_PERMISSIONS)}, and replace "
            "META_SYSTEM_USER_TOKEN in the server .env."
        )
    lowered = (message or "").lower()
    if code == 10 or (code is not None and 200 <= code <= 299) or "permission" in lowered:
        return (
            f"The Meta token is missing the {needs} permission (Meta error {code}). "
            "Regenerate the system-user token with that permission ticked, and check the "
            "system user is assigned the Facebook Page (full control) and the Instagram account."
        )
    if code == 100 and "does not exist" in lowered:
        return (
            f"Meta cannot see that object with this token (Meta error 100) — usually the "
            f"system user is not assigned the asset, or META_PAGE_ID / META_IG_USER_ID is "
            f"wrong. This call needs {needs}."
        )
    if code == 9 or code == 4 or code == 32 or code == 613:
        return f"Meta is rate-limiting this account (Meta error {code}). Try again later."
    return f"Meta refused the call (error {code}{'/' + str(subcode) if subcode else ''})."


class GraphClient:
    def __init__(self, token: str, version: str, *, dry_run: bool,
                 session: Any = None, sleep: Callable[[float], None] = time.sleep):
        self._token = token or ""
        self.version = version
        self.dry_run = dry_run
        self.calls: list[dict] = []
        self._http = session or requests
        self._sleep = sleep
        self._fake = 0

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def _record(self, method: str, path: str, params: dict, **extra) -> dict:
        entry = {"method": method, "path": path, "params": dict(params), **extra}
        self.calls.append(entry)
        return entry

    def _request(self, method: str, path: str, params: dict, *, needs: str,
                 token: str | None = None) -> dict:
        tok = token if token is not None else self._token
        if not tok:
            raise MetaError(
                "META_SYSTEM_USER_TOKEN is not set in the server .env, so IBLU cannot "
                "talk to Meta. Follow 'What Ignas does on the Meta side' in the spec."
            )
        url = f"{GRAPH}/{self.version}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {tok}"}
        entry = self._record(method, path, params, dry_run=False)
        try:
            if method == "GET":
                resp = self._http.get(url, params=params, headers=headers, timeout=60)
            else:
                resp = self._http.post(url, data=params, headers=headers, timeout=60)
        except requests.RequestException as exc:
            raise MetaError(f"Could not reach Meta: {scrub(str(exc))}", call=f"{method} {path}") from None
        entry["status_code"] = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": scrub(resp.text[:500])}
        if resp.status_code >= 400 or (isinstance(body, dict) and "error" in body):
            err = body.get("error", {}) if isinstance(body, dict) else {}
            msg = err.get("message") or body.get("raw", "") or f"HTTP {resp.status_code}"
            code, sub = err.get("code"), err.get("error_subcode")
            entry["error"] = msg
            raise MetaError(_explain(code, sub, msg, needs), msg, code, sub, f"{method} {path}")
        safe = {k: v for k, v in body.items() if k != "access_token"} if isinstance(body, dict) else body
        entry["response"] = safe
        return body

    def get(self, path: str, params: dict | None = None, *, needs: str, token: str | None = None) -> dict:
        return self._request("GET", path, params or {}, needs=needs, token=token)

    def post(self, path: str, params: dict, *, needs: str, token: str | None = None) -> dict:
        if self.dry_run:
            self._fake += 1
            fake = f"DRY_RUN-{self._fake}"
            self._record("POST", path, params, dry_run=True, would_return={"id": fake})
            logger.info("social dry-run: would POST %s %s", path, sorted(params))
            return {"id": fake}
        return self._request("POST", path, params, needs=needs, token=token)


# --- flows -------------------------------------------------------------------


def publish_instagram(client: GraphClient, ig_user_id: str, image_urls: list[str],
                      caption: str) -> dict:
    """Returns ``{remote_id, permalink}`` (both None in dry run)."""
    if not ig_user_id:
        raise MetaError("META_IG_USER_ID is not set in the server .env. Discover it once with "
                        "GET /{page-id}?fields=instagram_business_account.")
    needs = "instagram_content_publish"
    if len(image_urls) == 1:
        container = client.post(f"{ig_user_id}/media",
                                {"image_url": image_urls[0], "caption": caption}, needs=needs)["id"]
    else:
        children = [
            client.post(f"{ig_user_id}/media",
                        {"image_url": url, "is_carousel_item": "true"}, needs=needs)["id"]
            for url in image_urls
        ]
        container = client.post(
            f"{ig_user_id}/media",
            {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption},
            needs=needs,
        )["id"]

    if client.dry_run:
        client._record("GET", f"{container}", {"fields": "status_code"}, dry_run=True,
                       note=f"would poll every {POLL_INTERVAL_S}s until FINISHED, up to {POLL_TIMEOUT_S}s")
    else:
        waited = 0
        while True:
            status = client.get(container, {"fields": "status_code"}, needs="instagram_basic").get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise MetaError(f"Instagram could not prepare the post: container status {status}.",
                                str(status), call=f"GET {container}")
            if waited >= POLL_TIMEOUT_S:
                raise MetaError(f"Instagram was still processing after {POLL_TIMEOUT_S // 60} minutes "
                                f"(last status {status}). Nothing was published.")
            client._sleep(POLL_INTERVAL_S)
            waited += POLL_INTERVAL_S

    media_id = client.post(f"{ig_user_id}/media_publish", {"creation_id": container}, needs=needs)["id"]
    if client.dry_run:
        client._record("GET", f"{media_id}", {"fields": "permalink"}, dry_run=True)
        return {"remote_id": None, "permalink": None}
    permalink = client.get(media_id, {"fields": "permalink"}, needs="instagram_basic").get("permalink")
    return {"remote_id": media_id, "permalink": permalink}


def publish_facebook(client: GraphClient, page_id: str, image_urls: list[str], caption: str) -> dict:
    """Multi-photo Page post. Returns ``{remote_id, permalink}`` (None in dry run)."""
    if not page_id:
        raise MetaError("META_PAGE_ID is not set in the server .env.")
    # Step 1 is a GET: in dry run it runs for real when a token exists, which
    # proves pages_show_list without posting anything.
    if client.has_token:
        page_token = client.get(page_id, {"fields": "access_token"}, needs="pages_show_list").get("access_token")
        if not page_token:
            raise MetaError("Meta returned no Page access token — the system user is not assigned "
                            "the Facebook Page with full control, or the token lacks pages_show_list.")
    elif client.dry_run:
        client._record("GET", page_id, {"fields": "access_token"}, dry_run=True,
                       note="skipped: no META_SYSTEM_USER_TOKEN configured yet")
        page_token = ""
    else:
        client.get(page_id, {"fields": "access_token"}, needs="pages_show_list")  # raises: no token
        page_token = ""

    needs = "pages_manage_posts"
    photo_ids = [
        client.post(f"{page_id}/photos", {"url": url, "published": "false"}, needs=needs, token=page_token)["id"]
        for url in image_urls
    ]
    feed: dict[str, str] = {"message": caption}
    for i, pid in enumerate(photo_ids):
        feed[f"attached_media[{i}]"] = '{"media_fbid":"%s"}' % pid
    post_id = client.post(f"{page_id}/feed", feed, needs=needs, token=page_token)["id"]
    if client.dry_run:
        client._record("GET", post_id, {"fields": "permalink_url"}, dry_run=True)
        return {"remote_id": None, "permalink": None}
    permalink = client.get(post_id, {"fields": "permalink_url"}, needs="pages_read_engagement",
                           token=page_token).get("permalink_url")
    return {"remote_id": post_id, "permalink": permalink}


def publishing_limit(client: GraphClient, ig_user_id: str) -> dict:
    if not ig_user_id:
        raise MetaError("META_IG_USER_ID is not set in the server .env.")
    body = client.get(f"{ig_user_id}/content_publishing_limit",
                      {"fields": "config,quota_usage"}, needs="instagram_basic")
    data = (body.get("data") or [{}])[0]
    config = data.get("config") or {}
    return {
        "quota_usage": data.get("quota_usage"),
        "quota_total": config.get("quota_total"),
        "quota_duration_seconds": config.get("quota_duration"),
    }


# --- reads: recent posts ---------------------------------------------------------

RECENT_LIMIT = 20


def _ig_image_count(item: dict) -> int:
    kind = item.get("media_type")
    if kind == "CAROUSEL_ALBUM":
        return sum(1 for c in (item.get("children") or {}).get("data", []) if c.get("media_type") == "IMAGE")
    return 1 if kind == "IMAGE" else 0


def instagram_recent(client: GraphClient, ig_user_id: str, limit: int = RECENT_LIMIT) -> list[dict]:
    """The account's newest posts, newest first. GET only."""
    if not ig_user_id:
        raise MetaError("META_IG_USER_ID is not set in the server .env.")
    body = client.get(
        f"{ig_user_id}/media",
        {"fields": "id,caption,media_type,media_product_type,timestamp,permalink,"
                   "children{media_type}", "limit": str(limit)},
        needs="instagram_basic",
    )
    out = []
    for item in (body.get("data") or [])[:limit]:
        children = (item.get("children") or {}).get("data", [])
        out.append({
            "date": item.get("timestamp"),
            "caption": item.get("caption") or "",
            "permalink": item.get("permalink"),
            "image_count": _ig_image_count(item),
            "item_count": len(children) if item.get("media_type") == "CAROUSEL_ALBUM" else 1,
            "media_type": item.get("media_type"),
            "id": item.get("id"),
        })
    return out


def _fb_image_count(post: dict) -> tuple[int, int]:
    """(images, attachments) for a Page post. An album lists its photos as
    subattachments; a single photo is itself the attachment."""
    images = items = 0
    for att in (post.get("attachments") or {}).get("data", []):
        subs = (att.get("subattachments") or {}).get("data", [])
        if subs:
            items += len(subs)
            images += sum(1 for s in subs if s.get("media_type") == "photo")
        elif att.get("media_type"):
            items += 1
            images += 1 if att.get("media_type") == "photo" else 0
    return images, items


def facebook_recent(client: GraphClient, page_id: str, limit: int = RECENT_LIMIT) -> list[dict]:
    """The Page's newest posts, newest first. GET only — the Page token is
    fetched with a GET too, and never returned."""
    if not page_id:
        raise MetaError("META_PAGE_ID is not set in the server .env.")
    page_token = client.get(page_id, {"fields": "access_token"}, needs="pages_show_list").get("access_token")
    if not page_token:
        raise MetaError("Meta returned no Page access token — the system user is not assigned the "
                        "Facebook Page, or the token lacks pages_show_list.")
    body = client.get(
        f"{page_id}/posts",
        {"fields": "id,message,created_time,permalink_url,"
                   "attachments{media_type,subattachments.limit(100){media_type}}",
         "limit": str(limit)},
        needs="pages_read_engagement", token=page_token,
    )
    out = []
    for post in (body.get("data") or [])[:limit]:
        images, items = _fb_image_count(post)
        out.append({
            "date": post.get("created_time"),
            "caption": post.get("message") or "",
            "permalink": post.get("permalink_url"),
            "image_count": images,
            "item_count": items,
            "id": post.get("id"),
        })
    return out
