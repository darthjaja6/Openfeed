"""Thumbnail generation for Ticlawk content cards.

Ticlawk accepts an optional multipart `thumbnail` file for every content
subtype. OpenFeed treats it as required before push: native video/gallery
cards use source media where possible, while HTML cards are snapshotted in a
mobile viewport.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import logging
import os
import re
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.error import URLError

from openfeed.card_producers.base import CardPayload
from openfeed.models.content_item import ContentItem
from openfeed.utils.video_frames import extract_youtube_frames


_logger = logging.getLogger("producer.ticlawk.thumbnails")

_CACHE_DIR = Path("state/thumbnail_cache")
_MAX_IMAGE_BYTES = 15 * 1024 * 1024
_HTML_VIEWPORT = (390, 844)
_HTML_TIMEOUT_SECONDS = 20
_CHROME_SEMAPHORE = threading.Semaphore(2)


def ensure_thumbnail(item: ContentItem, payload: CardPayload) -> CardPayload | None:
    """Populate `payload.thumbnail_path`.

    Returns the same payload on success. Returns None when no readable
    thumbnail can be produced; callers should skip the push rather than create
    a new Ticlawk card without preview media.
    """
    if _is_readable_image(payload.thumbnail_path):
        return payload

    path: Path | None = None
    if payload.content_subtype == "gallery":
        path = _thumbnail_from_gallery(payload)
    elif payload.content_subtype == "video":
        path = _thumbnail_from_video(item, payload)
    elif payload.content_subtype == "html":
        path = _thumbnail_from_html(item, payload)

    if path is None:
        path = _fallback_title_thumbnail(item, payload)

    if path is None or not _is_readable_image(str(path)):
        _logger.warning(
            "thumbnail unavailable for %s/%s (%s)",
            item.platform, item.content_id, payload.content_subtype,
        )
        return None
    payload.thumbnail_path = str(path)
    return payload


def _is_readable_image(path: str | None) -> bool:
    if not path:
        return False
    p = Path(path)
    return p.exists() and p.is_file() and p.stat().st_size > 0


def _stem(item: ContentItem) -> str:
    digest = hashlib.sha1(f"{item.platform}:{item.content_id}".encode("utf-8")).hexdigest()[:16]
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", item.content_id).strip("-")[:48]
    return f"{item.platform}-{safe_id or 'content'}-{digest}"


def _thumbnail_from_gallery(payload: CardPayload) -> Path | None:
    if not payload.image_paths:
        return None
    for raw in payload.image_paths:
        path = Path(raw)
        if _is_readable_image(str(path)):
            return path
    return None


def _thumbnail_from_video(item: ContentItem, payload: CardPayload) -> Path | None:
    source = _source_thumbnail(item)
    if source is not None:
        return source
    if payload.video_path:
        return _frame_from_video(item, Path(payload.video_path))
    return None


def _source_thumbnail(item: ContentItem) -> Path | None:
    if item.platform == "youtube":
        url = (
            item.youtube.url
            if item.youtube and item.youtube.url
            else f"https://www.youtube.com/watch?v={item.content_id}"
        )
        frames = extract_youtube_frames(url)
        if frames:
            return frames[0]
        if item.youtube and item.youtube.thumbnail_url:
            return _download_image(item, item.youtube.thumbnail_url)
    if item.platform == "tiktok" and item.tiktok and item.tiktok.thumbnail_url:
        return _download_image(item, item.tiktok.thumbnail_url, referer=item.tiktok.url)
    return None


def _download_image(item: ContentItem, url: str, *, referer: str | None = None) -> Path | None:
    out_dir = _CACHE_DIR / "source"
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = _extension_from_url(url)
    out = out_dir / f"{_stem(item)}{ext}"
    if _is_readable_image(str(out)):
        return out
    headers = {"User-Agent": "Mozilla/5.0 OpenFeed thumbnail fetch"}
    if referer:
        headers["Referer"] = referer
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:  # noqa: S310
            body = resp.read(_MAX_IMAGE_BYTES + 1)
            if len(body) > _MAX_IMAGE_BYTES:
                _logger.warning("thumbnail too large for %s: %s", item.content_id, url)
                return None
            content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].lower()
    except (OSError, URLError) as exc:
        _logger.info("thumbnail download failed for %s: %s", item.content_id, exc)
        return None
    out = out.with_suffix(_extension_from_content_type(content_type) or ext)
    out.write_bytes(body)
    return out


def _extension_from_url(url: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def _extension_from_content_type(content_type: str) -> str | None:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(content_type)


def _frame_from_video(item: ContentItem, video_path: Path) -> Path | None:
    if not video_path.exists():
        return None
    out_dir = _CACHE_DIR / "video"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{_stem(item)}.jpg"
    if _is_readable_image(str(out)):
        return out
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        _logger.warning("ffmpeg not found; cannot extract thumbnail for %s", item.content_id)
        return None
    for ts in ("1", "0.25", "0"):
        try:
            proc = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", ts, "-i", str(video_path), "-frames:v", "1",
                    "-vf", "scale=720:-2", str(out),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _logger.info("ffmpeg thumbnail failed for %s: %s", item.content_id, exc)
            return None
        if proc.returncode == 0 and _is_readable_image(str(out)):
            return out
    return None


def _thumbnail_from_html(item: ContentItem, payload: CardPayload) -> Path | None:
    if not payload.html:
        return None
    return _screenshot_html(item, _wrap_html(payload.html))


def _fallback_title_thumbnail(item: ContentItem, payload: CardPayload) -> Path | None:
    title = html_lib.escape(payload.title or item.content_id)
    subtype = html_lib.escape(payload.content_subtype)
    doc = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    html, body {{ margin: 0; width: 100%; height: 100%; background: #101014; color: #f7f2ea; }}
    body {{ display: grid; place-items: center; padding: 34px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .card {{ position: relative; width: 100%; height: 100%; padding: 32px; border: 1px solid rgba(255,255,255,.18); background: linear-gradient(145deg, #15151b, #25212a); display: flex; flex-direction: column; justify-content: flex-end; }}
    .kind {{ position: absolute; top: 32px; left: 32px; font-size: 12px; letter-spacing: .12em; text-transform: uppercase; color: #b8b2a9; }}
    h1 {{ margin: 0; font-size: 30px; line-height: 1.1; font-weight: 760; letter-spacing: 0; }}
  </style>
</head>
<body><div class="card"><div class="kind">{subtype}</div><h1>{title}</h1></div></body>
</html>"""
    return _screenshot_html(item, doc, suffix="-fallback")


def _wrap_html(raw: str) -> str:
    source = raw.strip()
    if re.match(r"(?is)^\s*(<!doctype\s+html|<html\b)", source):
        return source
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <style>
    html, body {{ margin: 0; width: 100%; min-height: 100%; background: #050505; overflow: hidden; }}
    * {{ box-sizing: border-box; }}
  </style>
</head>
<body>{source}</body>
</html>"""


def _screenshot_html(item: ContentItem, doc: str, *, suffix: str = "") -> Path | None:
    chrome = _chrome_bin()
    if not chrome:
        _logger.warning("Chrome not found; cannot screenshot thumbnail for %s", item.content_id)
        return None
    out_dir = _CACHE_DIR / "html"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_stem(item)}{suffix}"
    html_path = out_dir / f"{stem}.html"
    out = out_dir / f"{stem}.png"
    if _is_readable_image(str(out)):
        return out
    html_path.write_text(doc, encoding="utf-8")
    width, height = _HTML_VIEWPORT
    with _CHROME_SEMAPHORE:
        try:
            proc = subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--hide-scrollbars",
                    f"--window-size={width},{height}",
                    "--virtual-time-budget=1000",
                    f"--screenshot={out}",
                    html_path.resolve().as_uri(),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_HTML_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _logger.warning("Chrome screenshot failed for %s: %s", item.content_id, exc)
            return None
    if proc.returncode != 0:
        _logger.warning(
            "Chrome screenshot failed for %s: %s",
            item.content_id, proc.stderr.decode("utf-8", errors="replace")[:300],
        )
        return None
    return out if _is_readable_image(str(out)) else None


def _chrome_bin() -> str | None:
    for name in (
        "OPENFEED_CHROME_BIN",
        "GOOGLE_CHROME_BIN",
        "CHROME_BIN",
    ):
        value = os.environ.get(name)
        if value:
            if Path(value).exists():
                return value
            path = shutil.which(value)
            if path:
                return path
    for candidate in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        path = shutil.which(candidate)
        if path:
            return path
    return None
