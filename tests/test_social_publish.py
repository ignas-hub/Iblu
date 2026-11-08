"""social_publish: the rules that keep a public post to what Ignas approved.

Spec acceptance tests 2 and 3 live here (dry run walks every step and logs the
calls; no confirm → refused; an edited slide → refused until a new preview).
Drive, Chromium, Postgres and Meta are replaced by in-memory fakes — no row is
written to the real database and nothing reaches Meta.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from iblu_keeper.tools import social_meta
from iblu_keeper.tools import social_publish as sp
from iblu_keeper.tools import social_render

ACCENT = "#9E1B22"


def artboard(text: str, blob: str | None = None) -> str:
    img = f'<img src="/_blob/{blob}">' if blob else ""
    return (
        "<!doctype html><html><head><script src='./support.js'></script></head><body><x-dc>"
        "<helmet><link href='https://fonts.googleapis.com/x' rel='stylesheet'>"
        "<style>body{margin:0;background:#EFE7DC}</style></helmet>"
        f'<div style="color: {{{{accent}}}}; font-family: \'Bodoni Moda\'">{text}</div>{img}'
        "<script>alert(1)</script>"
        "</x-dc>"
        f"<script type='text/x-dc' data-props='{{\"accent\":{{\"default\":\"{ACCENT}\"}}}}'>x</script>"
        "</body></html>"
    )


def jpeg(w=1080, h=1350, colour=(200, 180, 160)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "JPEG", quality=92)
    return buf.getvalue()


class FakeSource:
    def __init__(self):
        self.files = {f"C23-S{i}.dc.html": artboard(f"slide {i}").encode() for i in range(1, 6)}
        self.saved: dict[str, bytes] = {}

    def slide(self, name):
        if name not in self.files:
            raise sp.SocialError(f"{name!r} is not in project")
        return self.files[name]

    def fonts(self):
        return {f: b"ttf" for f in social_render.FONT_FILES}

    def assets_for(self, html):
        return {}

    def save_render(self, post_id, n, jpg):
        self.saved[f"{post_id}/{post_id}-{n}.jpg"] = jpg
        return {"id": f"drive-{n}", "url": f"https://drive.google.com/file/d/drive-{n}/view"}


class FakeStore:
    def __init__(self):
        self.previews, self.pubs, self.scheds = {}, [], []

    def insert_preview(self, row):
        self.previews[row["preview_id"]] = dict(row, media_deleted_at=None)

    def get_preview(self, pid):
        return self.previews.get(pid)

    def published_post(self, post_id):
        return {p["platform"]: p.get("permalink") for p in self.pubs
                if self.previews[p["preview_id"]]["post_id"] == post_id
                and p["status"] in ("published", "publishing") and not p["dry_run"]}

    def claim_platforms(self, pid, platforms, *, force, dry_run, schedule_id):
        taken = set(self.published_post(self.previews[pid]["post_id"]))
        dup = [p for p in platforms if p in taken]
        if dup and not force:
            raise sp.SocialRefused(f"already published to {dup}")
        ids = {}
        for p in platforms:
            self.pubs.append({"id": len(self.pubs), "preview_id": pid, "platform": p, "status": "publishing",
                              "dry_run": dry_run, "started_at": datetime.now(timezone.utc),
                              "finished_at": None})
            ids[p] = len(self.pubs) - 1
        return ids

    def finish_publication(self, pub_id, **f):
        self.pubs[pub_id].update(f, finished_at=datetime.now(timezone.utc))

    def publications(self, pid):
        return [p for p in self.pubs if p["preview_id"] == pid]

    def insert_schedule(self, row):
        self.scheds.append(dict(row, id=len(self.scheds) + 1, status="pending", result={},
                                post_id=self.previews[row["preview_id"]]["post_id"]))
        return len(self.scheds)

    def schedules(self, *, preview_id=None, active_only=False):
        return [s for s in self.scheds if (not preview_id or s["preview_id"] == preview_id)
                and (not active_only or s["status"] in ("pending", "publishing"))]

    def cancel_schedule(self, sid):
        for s in self.scheds:
            if s["id"] == sid and s["status"] == "pending":
                s["status"] = "cancelled"
                return s
        return None


class FakeHTTP:
    """Records live calls; any POST reaching it in dry run is the bug under test."""

    def __init__(self):
        self.sent = []

    def _resp(self, body, code=200):
        class R:
            status_code = code
            text = ""

            def json(self_inner):
                return body
        return R()

    def get(self, url, params=None, headers=None, timeout=None):
        self.sent.append(("GET", url, params, headers))
        if params and params.get("fields") == "access_token":
            return self._resp({"access_token": "PAGE-TOKEN-SECRET", "id": "1"})
        if params and params.get("fields") == "status_code":
            return self._resp({"status_code": "FINISHED"})
        if params and params.get("fields") == "permalink":
            return self._resp({"permalink": "https://www.instagram.com/p/abc/"})
        if params and params.get("fields") == "permalink_url":
            return self._resp({"permalink_url": "https://www.facebook.com/123/posts/456"})
        return self._resp({"data": [{"quota_usage": 3, "config": {"quota_total": 100, "quota_duration": 86400}}]})

    def post(self, url, data=None, headers=None, timeout=None):
        self.sent.append(("POST", url, data, headers))
        return self._resp({"id": f"id{len(self.sent)}"})


class Settings:
    use_mock = False
    dry_run = False
    social_dry_run = True
    meta_system_user_token = ""
    meta_page_id = "PAGE1"
    meta_ig_user_id = "IG1"
    meta_graph_version = "v26.0"
    social_public_base_url = "https://mcp.iblugames.com/media"


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = Settings()
    s.social_media_root = str(tmp_path / "media")
    (tmp_path / "media").mkdir()
    monkeypatch.setattr(sp, "settings", s)
    http = FakeHTTP()
    logged = []
    clock = {"now": datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)}
    deps = sp.Deps(
        store=FakeStore(), source=FakeSource(),
        render=lambda pages: [jpeg() for _ in pages],
        graph=lambda dry: social_meta.GraphClient(s.meta_system_user_token, s.meta_graph_version,
                                                  dry_run=dry, session=http, sleep=lambda _: None),
        log_work=lambda content, meta: logged.append((content, meta)) or {"id": "ctx1"},
        now=lambda: clock["now"],
    )
    return {"deps": deps, "settings": s, "http": http, "logged": logged, "clock": clock, "root": tmp_path / "media"}


def _preview(env, n=5, caption="No. 23. #blanklabel"):
    return sp.social_publish(action="preview", post_id="C23", slides=[f"C23-S{i}.dc.html" for i in range(1, n + 1)],
                             caption=caption, deps=env["deps"])


# --- conversion (port of tools/render.py) -------------------------------------


def test_convert_follows_the_reference_renderer():
    html = artboard("Hello", blob="a" * 32)
    out = social_render.convert("X.dc.html", html, {f: b"t" for f in social_render.FONT_FILES},
                                {"a" * 32: ("map.svg", b"<svg/>")})
    assert ACCENT in out and "{{accent}}" not in out
    assert "<script" not in out and "<helmet>" not in out and "<x-dc>" not in out
    assert "fonts.googleapis.com" not in out
    assert "body{margin:0" in out, "the helmet's style must survive or every slide shifts 8px"
    assert "font-variation-settings:'opsz' 18" in out
    assert "data:image/svg+xml;base64," in out and "/_blob/" not in out
    assert out.count("@font-face") == 3


def test_convert_names_a_missing_accent_or_image():
    fonts = {f: b"t" for f in social_render.FONT_FILES}
    with pytest.raises(social_render.RenderError, match="accent"):
        social_render.convert("A.dc.html", "<x-dc>x</x-dc>", fonts, {})
    with pytest.raises(social_render.RenderError, match="/_blob/"):
        social_render.convert("B.dc.html", artboard("x", blob="b" * 32), fonts, {})


# --- validation ----------------------------------------------------------------


def test_caption_limits():
    assert sp.validate_inputs("C23", ["a.dc.html"], "x" * 2200)["char_count"] == 2200
    with pytest.raises(sp.SocialError, match="2200"):
        sp.validate_inputs("C23", ["a.dc.html"], "x" * 2201)
    with pytest.raises(sp.SocialError, match="30"):
        sp.validate_inputs("C23", ["a.dc.html"], " ".join(f"#t{i}" for i in range(31)))
    six = sp.validate_inputs("C23", ["a.dc.html"], " ".join(f"#t{i}" for i in range(6)))
    assert six["hashtag_count"] == 6 and "house rule is 5" in six["warnings"][0]
    assert sp.validate_inputs("C23", ["a.dc.html"], "#a #b #c #d #e")["warnings"] == []


def test_slide_count_and_names():
    with pytest.raises(sp.SocialError, match="at most 10"):
        sp.validate_inputs("C23", [f"S{i}.dc.html" for i in range(11)], "")
    with pytest.raises(sp.SocialError):
        sp.validate_inputs("C23", [], "")
    with pytest.raises(sp.SocialError, match="dc.html"):
        sp.validate_inputs("C23", ["../../etc/passwd"], "")
    with pytest.raises(sp.SocialError, match="post_id"):
        sp.validate_inputs("C 23/..", ["a.dc.html"], "")


def test_preview_refuses_a_wrong_size_image(env):
    env["deps"].render = lambda pages: [jpeg(1080, 1080) for _ in pages]
    with pytest.raises(sp.SocialError, match="1080x1350"):
        _preview(env)
    assert not any(env["root"].iterdir()), "a refused preview must leave nothing hosted"


def test_publish_at_rules():
    now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    # Naive = Zagreb (UTC+1 in November).
    assert sp.parse_publish_at("2026-11-07T14:00", now) == datetime(2026, 11, 7, 13, 0, tzinfo=timezone.utc)
    assert sp.parse_publish_at("2026-11-07T13:00Z", now) == datetime(2026, 11, 7, 13, 0, tzinfo=timezone.utc)
    with pytest.raises(sp.SocialError, match="10 minutes"):
        sp.parse_publish_at("2026-11-07T13:05", now)
    with pytest.raises(sp.SocialError, match="30 days"):
        sp.parse_publish_at("2026-12-08T13:05", now)


# --- preview -------------------------------------------------------------------


def test_preview_renders_saves_hosts_and_returns_what_he_needs(env):
    out = _preview(env)
    assert out["status"] == "rendered" and out["format"] == "carousel"
    assert len(out["images"]) == 5
    assert out["images"][0]["public_url"].startswith("https://mcp.iblugames.com/media/")
    assert out["images"][0]["public_url"].endswith("/C23-1.jpg")
    assert set(env["deps"].source.saved) == {f"C23/C23-{i}.jpg" for i in range(1, 6)}
    assert out["hashtag_count"] == 1 and out["char_count"] == len("No. 23. #blanklabel")
    token = out["images"][0]["public_url"].split("/")[-2]
    assert len(token) == 32
    assert sp.media_file(token, "C23-1.jpg") is not None
    assert sp.media_file(token, "../C23-1.jpg") is None
    assert sp.media_file(token, "") is None


# --- acceptance 2: dry run walks every step ----------------------------------------


def test_dry_run_publish_walks_every_step_and_posts_nothing(env):
    pv = _preview(env)
    out = sp.social_publish(action="publish", preview_id=pv["preview_id"], confirm=True, deps=env["deps"])
    assert out["dry_run"] is True
    ig = out["platforms"]["instagram"]["calls"]
    paths = [(c["method"], c["path"]) for c in ig]
    assert paths[:5] == [("POST", "IG1/media")] * 5, "one carousel item per slide"
    assert ("POST", "IG1/media") == paths[5] and ig[5]["params"]["media_type"] == "CAROUSEL"
    assert ig[5]["params"]["children"] == "DRY_RUN-1,DRY_RUN-2,DRY_RUN-3,DRY_RUN-4,DRY_RUN-5"
    assert ("POST", "IG1/media_publish") in paths
    fb = [(c["method"], c["path"]) for c in out["platforms"]["facebook"]["calls"]]
    assert fb.count(("POST", "PAGE1/photos")) == 5 and ("POST", "PAGE1/feed") in fb
    feed = next(c for c in out["platforms"]["facebook"]["calls"] if c["path"] == "PAGE1/feed")
    assert [feed["params"][f"attached_media[{i}]"] for i in range(5)] == [
        '{"media_fbid":"DRY_RUN-%d"}' % i for i in range(1, 6)]
    assert not [s for s in env["http"].sent if s[0] == "POST"], "dry run must never POST to Meta"
    assert out["platforms"]["instagram"]["permalink"] is None
    assert env["logged"] == [], "a dry run is not a publish and must not enter the work log"
    st = sp.social_read(action="status", preview_id=pv["preview_id"], deps=env["deps"])
    assert st["platforms"]["instagram"]["status"] == "dry_run"


def test_dry_run_with_a_token_still_proves_it_with_gets(env):
    env["settings"].meta_system_user_token = "SYSTEM-TOKEN-SECRET"
    pv = _preview(env)
    out = sp.social_publish(action="publish", preview_id=pv["preview_id"], platforms=["facebook"],
                            confirm=True, deps=env["deps"])
    assert [s[0] for s in env["http"].sent] == ["GET"], "only the Page-token GET runs in dry run"
    assert "SECRET" not in repr(out), "no token may appear in tool output"


# --- acceptance 3: confirm, and the hash ------------------------------------------


def test_publish_without_confirm_is_refused(env):
    pv = _preview(env)
    for confirm in (False, None, "true", 1):
        with pytest.raises(sp.SocialRefused, match="confirm=true"):
            sp.social_publish(action="publish", preview_id=pv["preview_id"], confirm=confirm, deps=env["deps"])
    with pytest.raises(sp.SocialRefused, match="confirm=true"):
        sp.social_publish(action="schedule", preview_id=pv["preview_id"], publish_at="2026-11-08T10:00",
                          deps=env["deps"])
    assert env["deps"].store.pubs == []


def test_editing_a_slide_after_preview_is_refused_until_a_new_preview(env):
    pv = _preview(env)
    env["deps"].source.files["C23-S3.dc.html"] = artboard("slide 3, edited").encode()
    with pytest.raises(sp.SocialRefused, match="C23-S3.dc.html"):
        sp.social_publish(action="publish", preview_id=pv["preview_id"], confirm=True, deps=env["deps"])
    assert env["deps"].store.pubs == []
    fresh = _preview(env)
    out = sp.social_publish(action="publish", preview_id=fresh["preview_id"], confirm=True, deps=env["deps"])
    assert out["platforms"]["instagram"]["status"] == "dry_run"


def test_a_hosted_jpg_swapped_on_disk_is_refused(env):
    pv = _preview(env)
    token = pv["images"][1]["public_url"].split("/")[-2]
    (env["root"] / token / "C23-2.jpg").write_bytes(jpeg(colour=(0, 0, 0)))
    with pytest.raises(sp.SocialRefused, match="not the one Ignas previewed"):
        sp.social_publish(action="publish", preview_id=pv["preview_id"], confirm=True, deps=env["deps"])


def test_a_preview_older_than_a_day_is_refused(env):
    pv = _preview(env)
    env["clock"]["now"] += timedelta(hours=24, minutes=1)
    with pytest.raises(sp.SocialRefused, match="24 hours"):
        sp.social_publish(action="publish", preview_id=pv["preview_id"], confirm=True, deps=env["deps"])


# --- live path (against the fake HTTP) ----------------------------------------------


def test_live_publish_logs_work_and_refuses_a_second_post(env):
    env["settings"].social_dry_run = False
    env["settings"].meta_system_user_token = "SYSTEM-TOKEN-SECRET"
    pv = _preview(env)
    out = sp.social_publish(action="publish", preview_id=pv["preview_id"], confirm=True, deps=env["deps"])
    assert out["platforms"]["instagram"]["permalink"] == "https://www.instagram.com/p/abc/"
    assert out["platforms"]["facebook"]["permalink"] == "https://www.facebook.com/123/posts/456"
    content, meta = env["logged"][0]
    assert meta["post_id"] == "C23" and set(meta["platforms"]) == {"instagram", "facebook"}
    assert "SECRET" not in repr(out)
    # The page token, not the system token, posts to the Page.
    fb_posts = [s for s in env["http"].sent if s[0] == "POST" and "PAGE1/" in s[1]]
    assert all(h["Authorization"] == "Bearer PAGE-TOKEN-SECRET" for *_, h in fb_posts)
    assert all("access_token" not in (d or {}) for _, _, d, _ in env["http"].sent)

    with pytest.raises(sp.SocialRefused, match="already published"):
        sp.social_publish(action="publish", preview_id=pv["preview_id"], platforms=["instagram"],
                          confirm=True, deps=env["deps"])
    again = sp.social_publish(action="publish", preview_id=pv["preview_id"], platforms=["instagram"],
                              confirm=True, force=True, deps=env["deps"])
    assert again["platforms"]["instagram"]["status"] == "published"


def test_single_image_uses_the_single_image_call(env):
    env["settings"].social_dry_run = False
    env["settings"].meta_system_user_token = "T"
    pv = _preview(env, n=1)
    assert pv["format"] == "single_image"
    out = sp.social_publish(action="publish", preview_id=pv["preview_id"], platforms=["instagram"],
                            confirm=True, deps=env["deps"])
    first = out["platforms"]["instagram"]["calls"][0]
    assert first["params"]["caption"] and "is_carousel_item" not in first["params"]


def test_live_publish_without_meta_config_says_what_is_missing(env):
    env["settings"].social_dry_run = False
    pv = _preview(env)
    with pytest.raises(sp.SocialRefused, match="META_SYSTEM_USER_TOKEN"):
        sp.social_publish(action="publish", preview_id=pv["preview_id"], confirm=True, deps=env["deps"])


def test_an_expired_token_is_named_in_plain_language(env):
    env["settings"].social_dry_run = False
    env["settings"].meta_system_user_token = "T"
    http = env["http"]
    http.post = lambda *a, **k: http._resp(
        {"error": {"message": "Error validating access token: Session has expired", "code": 190}}, 400)
    pv = _preview(env)
    out = sp.social_publish(action="publish", preview_id=pv["preview_id"], platforms=["instagram"],
                            confirm=True, deps=env["deps"])
    ig = out["platforms"]["instagram"]
    assert ig["status"] == "failed" and "expired or was revoked" in ig["error"]
    assert ig["meta_message"] == "Error validating access token: Session has expired"
    st = sp.social_read(action="status", preview_id=pv["preview_id"], deps=env["deps"])
    assert "Session has expired" in st["platforms"]["instagram"]["error"], "Meta's message, verbatim"
    assert env["logged"] == []


def test_a_missing_permission_is_named():
    msg = social_meta._explain(200, None, "Requires pages_manage_posts permission", "pages_manage_posts")
    assert "pages_manage_posts" in msg


def test_container_error_is_reported_verbatim(env):
    env["settings"].social_dry_run = False
    env["settings"].meta_system_user_token = "T"
    http = env["http"]
    orig = http.get
    http.get = lambda url, params=None, **k: (http._resp({"status_code": "ERROR"})
                                               if params and params.get("fields") == "status_code"
                                               else orig(url, params, **k))
    pv = _preview(env)
    out = sp.social_publish(action="publish", preview_id=pv["preview_id"], platforms=["instagram"],
                            confirm=True, deps=env["deps"])
    assert out["platforms"]["instagram"]["meta_message"] == "ERROR"
    assert not any("media_publish" in s[1] for s in http.sent), "never publish a failed container"


# --- schedule / cancel ----------------------------------------------------------------


def test_schedule_list_and_cancel(env):
    pv = _preview(env)
    out = sp.social_publish(action="schedule", preview_id=pv["preview_id"], publish_at="2026-11-07T13:30",
                            platforms=["instagram", "facebook"], confirm=True, deps=env["deps"])
    assert out["status"] == "scheduled" and out["publish_at_zagreb"].startswith("2026-11-07T13:30")
    listed = sp.social_read(action="list_scheduled", deps=env["deps"])
    assert [s["schedule_id"] for s in listed["items"]] == [out["schedule_id"]]
    st = sp.social_read(action="status", preview_id=pv["preview_id"], deps=env["deps"])
    assert st["platforms"]["facebook"]["status"] == "scheduled"
    sp.social_publish(action="cancel", schedule_id=out["schedule_id"], deps=env["deps"])
    assert sp.social_read(action="list_scheduled", deps=env["deps"])["items"] == []
    with pytest.raises(sp.SocialRefused, match="not pending"):
        sp.social_publish(action="cancel", schedule_id=out["schedule_id"], deps=env["deps"])
    assert env["deps"].store.pubs == [], "cancelling posts nothing"


def test_limits_reads_the_quota(env):
    env["settings"].meta_system_user_token = "T"
    out = sp.social_read(action="limits", deps=env["deps"])
    assert out["quota_usage"] == 3 and out["quota_total"] == 100
    env["settings"].meta_system_user_token = ""
    assert "META_SYSTEM_USER_TOKEN" in sp.social_read(action="limits", deps=env["deps"])["error"]


def test_mock_mode_touches_nothing():
    out = sp.social_read(action="list_scheduled")
    assert out["_mock"] is True and out["status"] == "not_list_scheduled_mock"


def test_tokens_are_scrubbed_from_error_text():
    assert "abc123" not in social_meta.scrub("https://graph.facebook.com/x?access_token=abc123&y=1")


def test_dry_run_walks_every_step_before_meta_is_set_up(env):
    """Found in the first end-to-end run: with no META_* in .env yet, the dry
    run failed at 'META_IG_USER_ID is not set' instead of showing the calls."""
    env["settings"].meta_ig_user_id = ""
    env["settings"].meta_page_id = ""
    pv = _preview(env, n=2)
    out = sp.social_publish(action="publish", preview_id=pv["preview_id"], confirm=True, deps=env["deps"])
    assert out["platforms"]["instagram"]["status"] == "dry_run"
    assert out["platforms"]["facebook"]["status"] == "dry_run"
    assert out["platforms"]["instagram"]["calls"][0]["path"] == "{ig-user-id}/media"
    assert any(c["path"] == "{page-id}/feed" for c in out["platforms"]["facebook"]["calls"])


# --- the read/write split ---------------------------------------------------------


def test_each_tool_refuses_the_others_actions(env):
    """The Claude app's Ask/Allow is per tool: a read inside social_publish
    would ask every time, a write inside social_read would never ask."""
    for action in sp.READ_ACTIONS:
        if action == "recent_posts":
            continue  # in both tools on purpose (Ignas, 2026-11-08)
        with pytest.raises(sp.SocialError, match="social_read"):
            sp.social_publish(action=action, deps=env["deps"])
    for action in sp.WRITE_ACTIONS:
        with pytest.raises(sp.SocialError, match="social_publish"):
            sp.social_read(action=action, deps=env["deps"])


# --- recent_posts -----------------------------------------------------------------

IG_MEDIA = {"data": [
    {"id": "1", "caption": "Winter event", "media_type": "CAROUSEL_ALBUM", "timestamp": "2026-11-07T19:59:53+0000",
     "permalink": "https://www.instagram.com/p/A/",
     "children": {"data": [{"media_type": "IMAGE"}] * 4 + [{"media_type": "VIDEO"}]}},
    {"id": "2", "media_type": "IMAGE", "timestamp": "2026-10-13T15:57:06+0000",
     "permalink": "https://www.instagram.com/p/B/"},
    {"id": "3", "caption": "reel", "media_type": "VIDEO", "timestamp": "2026-10-01T10:00:00+0000",
     "permalink": "https://www.instagram.com/p/C/"},
]}
FB_POSTS = {"data": [
    {"id": "p1", "message": "Winter event", "created_time": "2026-11-07T19:59:48+0000",
     "permalink_url": "https://www.facebook.com/x/posts/1",
     "attachments": {"data": [{"media_type": "album", "subattachments": {"data": [{"media_type": "photo"}] * 5}}]}},
    {"id": "p2", "created_time": "2026-10-13T15:57:02+0000", "permalink_url": "https://www.facebook.com/x/posts/2",
     "attachments": {"data": [{"media_type": "photo"}]}},
    {"id": "p3", "message": "just text", "created_time": "2026-10-01T10:00:00+0000",
     "permalink_url": "https://www.facebook.com/x/posts/3"},
]}


def _feeds(env):
    http = env["http"]
    orig = http.get

    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("/IG1/media"):
            http.sent.append(("GET", url, params, headers))
            return http._resp(IG_MEDIA)
        if url.endswith("/PAGE1/posts"):
            http.sent.append(("GET", url, params, headers))
            return http._resp(FB_POSTS)
        return orig(url, params, headers=headers, timeout=timeout)
    http.get = get


def test_recent_posts_reads_both_platforms(env):
    env["settings"].meta_system_user_token = "SYSTEM-TOKEN-SECRET"
    _feeds(env)
    out = sp.social_read(action="recent_posts", deps=env["deps"])
    ig = out["platforms"]["instagram"]["posts"]
    assert [p["image_count"] for p in ig] == [4, 1, 0], "carousel counts its images, a video has none"
    assert ig[0]["item_count"] == 5 and ig[1]["caption"] == ""
    assert ig[0]["date"] == "2026-11-07T19:59:53+0000" and ig[0]["permalink"] == "https://www.instagram.com/p/A/"
    fb = out["platforms"]["facebook"]["posts"]
    assert [p["image_count"] for p in fb] == [5, 1, 0]
    assert fb[0]["caption"] == "Winter event" and fb[2]["permalink"].endswith("/3")
    ig_call = next(s for s in env["http"].sent if s[1].endswith("/IG1/media"))
    assert ig_call[2]["limit"] == "20"
    fb_call = next(s for s in env["http"].sent if s[1].endswith("/PAGE1/posts"))
    assert fb_call[3]["Authorization"] == "Bearer PAGE-TOKEN-SECRET", "Page posts are read with the Page token"
    assert "SECRET" not in repr(out)


def test_recent_posts_never_writes_even_when_publishing_is_live(env):
    env["settings"].social_dry_run = False
    env["settings"].meta_system_user_token = "T"
    _feeds(env)
    sp.social_read(action="recent_posts", platforms=["instagram", "facebook"], limit=3, deps=env["deps"])
    assert env["http"].sent and all(m == "GET" for m, *_ in env["http"].sent)
    assert env["deps"].store.pubs == [] and env["logged"] == []


def test_recent_posts_reports_each_platform_separately(env):
    env["settings"].meta_system_user_token = "T"
    env["settings"].meta_page_id = ""
    _feeds(env)
    out = sp.social_read(action="recent_posts", deps=env["deps"])
    assert out["platforms"]["instagram"]["count"] == 3
    assert "META_PAGE_ID" in out["platforms"]["facebook"]["error"]


# --- code review 2026-11-08 ------------------------------------------------------


def test_a_second_preview_of_a_published_post_is_still_a_duplicate(env):
    """Review finding: the guard was keyed on preview_id, so re-previewing C23
    could post C23 again without force."""
    env["settings"].social_dry_run = False
    env["settings"].meta_system_user_token = "T"
    first = _preview(env)
    sp.social_publish(action="publish", preview_id=first["preview_id"], platforms=["instagram"],
                      confirm=True, deps=env["deps"])
    again = _preview(env)
    with pytest.raises(sp.SocialRefused, match="already published"):
        sp.social_publish(action="publish", preview_id=again["preview_id"], platforms=["instagram"],
                          confirm=True, deps=env["deps"])
    with pytest.raises(sp.SocialRefused, match="already published"):
        sp.social_publish(action="schedule", preview_id=again["preview_id"], platforms=["instagram"],
                          publish_at="2026-11-08T10:00", confirm=True, deps=env["deps"])


def test_a_crash_on_the_first_platform_does_not_strand_the_second(env):
    """Review finding: a non-Meta error left the untried platform 'publishing'
    for 15 minutes, then labelled 'may or may not have posted'."""
    env["settings"].social_dry_run = False
    env["settings"].meta_system_user_token = "T"
    http = env["http"]
    http.post = lambda *a, **k: http._resp({"no_id_here": True})  # 200 without an id -> KeyError
    pv = _preview(env)
    with pytest.raises(KeyError):
        sp.social_publish(action="publish", preview_id=pv["preview_id"], confirm=True, deps=env["deps"])
    rows = {p["platform"]: p for p in env["deps"].store.pubs}
    assert rows["instagram"]["status"] == "failed"
    assert rows["facebook"]["status"] == "failed"
    assert rows["facebook"]["error"].startswith("not attempted: instagram failed first")


def test_a_second_schedule_for_the_same_slot_is_refused(env):
    pv = _preview(env)
    sp.social_publish(action="schedule", preview_id=pv["preview_id"], publish_at="2026-11-07T18:00",
                      confirm=True, deps=env["deps"])
    with pytest.raises(sp.SocialRefused, match="cancel it first"):
        sp.social_publish(action="schedule", preview_id=pv["preview_id"], publish_at="2026-11-07T19:00",
                          platforms=["facebook"], confirm=True, deps=env["deps"])


def test_a_non_dict_error_body_is_a_meta_error():
    class H:
        def get(self, *a, **k):
            class R:
                status_code = 502
                text = ""
                def json(self):
                    return ["bad", "gateway"]
            return R()
    c = social_meta.GraphClient("T", "v26.0", dry_run=False, session=H())
    with pytest.raises(social_meta.MetaError):
        c.get("x", needs="instagram_basic")


def test_duplicate_slide_names_in_drive_are_refused():
    src = sp.DriveSource.__new__(sp.DriveSource)
    src.root, src.account, src._svc = "root", None, None
    src._children = {"root": {"project": {"id": "proj", "mimeType": "application/vnd.google-apps.folder"}},
                     "proj": {"C23-S1.dc.html": {"id": "a"}}}
    src._dupes = {("proj", "C23-S1.dc.html")}
    src._blob_map, src._asset_cache = None, {}
    with pytest.raises(sp.SocialError, match="two files named"):
        src.slide("C23-S1.dc.html")


def test_social_publish_also_answers_recent_posts_and_never_writes(env):
    """Ignas, 2026-11-08: recent_posts in social_publish as well — the same
    GET-only read, live posting switched on or not."""
    env["settings"].social_dry_run = False
    env["settings"].meta_system_user_token = "T"
    _feeds(env)
    out = sp.social_publish(action="recent_posts", limit=20, deps=env["deps"])
    assert [p["image_count"] for p in out["platforms"]["instagram"]["posts"]] == [4, 1, 0]
    assert [p["image_count"] for p in out["platforms"]["facebook"]["posts"]] == [5, 1, 0]
    first = out["platforms"]["instagram"]["posts"][0]
    assert {"date", "caption", "permalink", "image_count"} <= set(first)
    assert all(m == "GET" for m, *_ in env["http"].sent)
    assert env["deps"].store.pubs == [] and env["logged"] == []
    assert out == sp.social_read(action="recent_posts", limit=20, deps=env["deps"])
