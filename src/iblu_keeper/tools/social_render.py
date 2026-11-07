"""Artboard → JPG. A port of the design export's ``tools/render.py``.

Each slide is one self-contained ``.dc.html`` artboard, 1080x1350, written by
the Claude Design canvas. Rendering it outside the canvas means undoing what
the canvas would have done at runtime:

* ``{{accent}}`` is a prop; its default sits in the ``data-props`` script as
  ``"default":"#XXXXXX"``. Substitute it.
* ``<helmet>`` loads Google Fonts over the network; drop it, keeping its
  ``<style>`` (``body{margin:0;background:…}`` — without it the default 8px
  body margin shifts every slide, which the reference renderer also avoided).
* every ``<script>`` and the ``<x-dc>`` wrapper go.
* Bodoni Moda (regular + italic variable fonts) and Jost come from the
  bundle's own ``.ttf`` files, plus ``[style*="Bodoni"]{font-variation-settings:'opsz' 18}``.
* ``/_blob/<id>`` resolves through ``blob-map.json`` to a file in ``assets/``.

Differences from the reference, both from the spec: JPEG quality 92 (it used
94), and the screenshot waits on ``document.fonts.ready`` instead of a fixed
400 ms. Fonts and images are inlined as data: URIs so the page needs neither
the network nor file access.

Chromium runs in a CHILD PROCESS (``python -m iblu_keeper.tools.social_render
<jobdir>``): Playwright's sync API cannot run inside the server's event loop,
and a 4 GB box should get Chromium's memory back the moment the render ends.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import subprocess
import sys
import tempfile
from pathlib import Path

WIDTH, HEIGHT = 1080, 1350
JPEG_QUALITY = 92
RENDER_TIMEOUT_S = 180

FONT_FILES = ("bodoni.ttf", "bodoni-i.ttf", "jost.ttf")

_ACCENT = re.compile(r'"default":"(#[0-9A-Fa-f]{6})"')
_XDC = re.compile(r"<x-dc>(.*)</x-dc>", re.S)
_HELMET = re.compile(r"<helmet>(.*?)</helmet>", re.S)
_STYLE = re.compile(r"<style>(.*?)</style>", re.S)
_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.S | re.I)
_BLOB = re.compile(r"/_blob/([0-9a-f]{32})")


class RenderError(RuntimeError):
    """An artboard could not be converted or rendered. The message names it."""


def blob_ids(html: str) -> list[str]:
    """The ``/_blob/<id>`` images an artboard references, in first-use order."""
    return list(dict.fromkeys(_BLOB.findall(html)))


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def font_faces(fonts: dict[str, bytes]) -> str:
    missing = [f for f in FONT_FILES if f not in fonts]
    if missing:
        raise RenderError(f"font file(s) missing from the tools folder: {', '.join(missing)}")
    uri = {name: _data_uri(fonts[name], "font/ttf") for name in FONT_FILES}
    return (
        f"@font-face{{font-family:'Bodoni Moda';src:url({uri['bodoni.ttf']});font-style:normal;font-weight:100 900}}"
        f"@font-face{{font-family:'Bodoni Moda';src:url({uri['bodoni-i.ttf']});font-style:italic;font-weight:100 900}}"
        f"@font-face{{font-family:'Jost';src:url({uri['jost.ttf']});font-weight:100 900}}"
        "[style*=\"Bodoni\"]{font-variation-settings:'opsz' 18}"
    )


def convert(name: str, html: str, fonts: dict[str, bytes], assets: dict[str, tuple[str, bytes]]) -> str:
    """One artboard's source → a standalone HTML page ready to screenshot.

    ``assets`` maps blob id → (file name, bytes). Every failure names the
    artboard and what it lacked; nothing is guessed.
    """
    acc = _ACCENT.search(html)
    if not acc:
        raise RenderError(f"{name}: no default accent (\"default\":\"#XXXXXX\") in its data-props script")
    body_m = _XDC.search(html)
    if not body_m:
        raise RenderError(f"{name}: no <x-dc> wrapper — not a canvas artboard")
    body = body_m.group(1)

    helmet = _HELMET.search(body)
    helmet_style = ""
    if helmet:
        helmet_style = "".join(_STYLE.findall(helmet.group(1)))
    body = _HELMET.sub("", body)
    body = _SCRIPT.sub("", body)
    body = body.replace("{{accent}}", acc.group(1))

    def _blob(m: re.Match) -> str:
        bid = m.group(1)
        if bid not in assets:
            raise RenderError(f"{name}: image /_blob/{bid} is not in blob-map.json / assets")
        fname, data = assets[bid]
        mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        return _data_uri(data, mime)

    body = _BLOB.sub(_blob, body)
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        f"{font_faces(fonts)}{helmet_style}"
        "</style></head><body>"
        f"{body}</body></html>"
    )


def render_pages(pages: list[str]) -> list[bytes]:
    """Screenshot each converted page in a child process. Returns JPEG bytes."""
    with tempfile.TemporaryDirectory(prefix="iblu-render-") as tmp:
        job = Path(tmp)
        for i, page in enumerate(pages, 1):
            (job / f"{i}.html").write_text(page, encoding="utf-8")
        (job / "job.json").write_text(json.dumps({"count": len(pages)}))
        proc = subprocess.run(
            [sys.executable, "-m", "iblu_keeper.tools.social_render", str(job)],
            capture_output=True, text=True, timeout=RENDER_TIMEOUT_S,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
            raise RenderError("the renderer failed: " + " | ".join(tail))
        out = []
        for i in range(1, len(pages) + 1):
            jpg = job / f"{i}.jpg"
            if not jpg.exists():
                raise RenderError(f"the renderer produced no image for slide {i}")
            out.append(jpg.read_bytes())
        return out


def _child(jobdir: str) -> int:
    from playwright.sync_api import sync_playwright

    job = Path(jobdir)
    count = json.loads((job / "job.json").read_text())["count"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
            for i in range(1, count + 1):
                page.set_content((job / f"{i}.html").read_text(encoding="utf-8"), wait_until="load")
                page.evaluate("document.fonts.ready.then(() => true)")
                page.screenshot(path=str(job / f"{i}.jpg"), type="jpeg", quality=JPEG_QUALITY)
        finally:
            browser.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through render_pages
    sys.exit(_child(sys.argv[1]))
