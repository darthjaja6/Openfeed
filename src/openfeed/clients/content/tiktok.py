"""yt-dlp wrapper for TikTok creator patrol and mp4 download.

TikTok's opencli `user` adapter is intentionally not used here: it is a DOM
scraper and has returned unrelated video links after login. yt-dlp is the
production primitive for creator recent videos, single-video metadata, and
native-video mp4 downloads.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field


logger = logging.getLogger("tiktok")


class TikTokYtDlpError(RuntimeError):
    """yt-dlp failed or returned unusable TikTok metadata."""


class TikTokPhotoImage(BaseModel):
    """One image inside a TikTok photo-mode post."""

    model_config = ConfigDict(extra="forbid")

    url: str
    width: int | None = None
    height: int | None = None


class TikTokVideoMetadata(BaseModel):
    """Normalized subset of yt-dlp metadata needed by downstream tasks."""

    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    media_kind: Literal["video", "photo", "audio_or_cover_only"] = "audio_or_cover_only"
    title: str = ""
    uploader: str = ""
    duration: float | None = None
    timestamp: int | None = None
    upload_date: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    repost_count: int | None = None
    thumbnail_url: str | None = None
    ext: str | None = None
    vcodec: str | None = None
    acodec: str | None = None
    has_video_stream: bool
    photo_count: int = 0
    photo_images: list[TikTokPhotoImage] = Field(default_factory=list)
    audio_url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


def _base_cmd() -> list[str]:
    return ["yt-dlp", "--no-warnings"]


def _run(cmd: list[str], *, timeout: int, cwd: Path | None = None) -> tuple[str, str, float]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
        )
    except FileNotFoundError as exc:
        raise TikTokYtDlpError("yt-dlp binary not found in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise TikTokYtDlpError(f"yt-dlp timed out after {timeout}s") from exc

    elapsed = time.monotonic() - started
    if result.returncode != 0:
        detail = _stderr_tail(result.stderr) or (result.stdout or "")[:300]
        raise TikTokYtDlpError(f"yt-dlp exit={result.returncode}: {detail}")
    return result.stdout or "", result.stderr or "", elapsed


def _stderr_tail(stderr: str) -> str:
    for line in reversed((stderr or "").strip().splitlines()):
        if line.strip():
            return line.strip()[:300]
    return ""


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            items.append(parsed)
    return items


def _has_video_stream(item: dict[str, Any]) -> bool:
    vcodec = item.get("vcodec")
    if vcodec and vcodec != "none":
        return True
    for fmt in item.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        fmt_vcodec = fmt.get("vcodec")
        if fmt_vcodec and fmt_vcodec != "none":
            return True
    return False


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize(item: dict[str, Any]) -> TikTokVideoMetadata | None:
    video_id = str(item.get("id") or "").strip()
    url = str(item.get("webpage_url") or item.get("url") or "").strip()
    if not video_id or not url:
        return None
    has_video = _has_video_stream(item)
    return TikTokVideoMetadata(
        id=video_id,
        url=url,
        media_kind="video" if has_video else "audio_or_cover_only",
        title=str(item.get("title") or ""),
        uploader=str(item.get("uploader") or item.get("channel") or ""),
        duration=_to_float(item.get("duration")),
        timestamp=_to_int(item.get("timestamp")),
        upload_date=str(item.get("upload_date") or "") or None,
        view_count=_to_int(item.get("view_count")),
        like_count=_to_int(item.get("like_count")),
        comment_count=_to_int(item.get("comment_count")),
        repost_count=_to_int(item.get("repost_count")),
        thumbnail_url=str(item.get("thumbnail") or "") or None,
        ext=str(item.get("ext") or "") or None,
        vcodec=str(item.get("vcodec") or "") or None,
        acodec=str(item.get("acodec") or "") or None,
        has_video_stream=has_video,
        audio_url=_audio_url_from_raw(item),
        raw=item,
    )


def _audio_url_from_raw(item: dict[str, Any]) -> str | None:
    for fmt in item.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        if fmt.get("vcodec") == "none" and fmt.get("url"):
            return str(fmt["url"])
    return None


def _extract_item_struct_from_html(html: str) -> dict[str, Any] | None:
    match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(.*?)</script>',
        html,
    )
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    item = (
        data.get("__DEFAULT_SCOPE__", {})
        .get("webapp.video-detail", {})
        .get("itemInfo", {})
        .get("itemStruct")
    )
    return item if isinstance(item, dict) else None


def _photo_images_from_item_struct(item_struct: dict[str, Any]) -> list[TikTokPhotoImage]:
    image_post = item_struct.get("imagePost") or {}
    images = image_post.get("images") or []
    out: list[TikTokPhotoImage] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        urls = ((image.get("imageURL") or {}).get("urlList") or [])
        url = str(urls[0] if urls else "").strip()
        if not url:
            continue
        out.append(TikTokPhotoImage(
            url=url,
            width=_to_int(image.get("imageWidth")),
            height=_to_int(image.get("imageHeight")),
        ))
    return out


def _apply_page_media(item: TikTokVideoMetadata, item_struct: dict[str, Any]) -> TikTokVideoMetadata:
    photo_images = _photo_images_from_item_struct(item_struct)
    video = item_struct.get("video") or {}
    music = item_struct.get("music") or {}
    title = (
        ((item_struct.get("imagePost") or {}).get("title"))
        or item_struct.get("desc")
        or item.title
    )
    if photo_images:
        return item.model_copy(update={
            "media_kind": "photo",
            "title": str(title or ""),
            "photo_count": len(photo_images),
            "photo_images": photo_images,
            "audio_url": str(music.get("playUrl") or item.audio_url or "") or None,
        })
    if video.get("playAddr") or video.get("downloadAddr") or item.has_video_stream:
        return item.model_copy(update={
            "media_kind": "video",
            "title": str(title or ""),
            "audio_url": str(music.get("playUrl") or item.audio_url or "") or None,
        })
    return item.model_copy(update={
        "media_kind": "audio_or_cover_only",
        "title": str(title or ""),
        "audio_url": str(music.get("playUrl") or item.audio_url or "") or None,
    })


def tiktok_list_user_videos(
    handle: str,
    *,
    limit: int = 10,
    start_index: int = 1,
    allow_empty: bool = False,
    timeout: int = 180,
) -> list[TikTokVideoMetadata]:
    """Return recent TikTok videos for `handle` using yt-dlp playlist metadata."""
    clean_handle = handle.strip().lstrip("@")
    if not clean_handle:
        raise TikTokYtDlpError("empty TikTok handle")
    if limit <= 0:
        raise TikTokYtDlpError("TikTok playlist limit must be positive")
    if start_index <= 0:
        raise TikTokYtDlpError("TikTok playlist start_index must be positive")
    url = f"https://www.tiktok.com/@{clean_handle}"
    end_index = start_index + limit - 1
    cmd = _base_cmd() + [
        "--skip-download",
        "--playlist-start",
        str(start_index),
        "--playlist-end",
        str(end_index),
        "--dump-json",
        url,
    ]
    stdout, _stderr, elapsed = _run(cmd, timeout=timeout)
    items = [_normalize(item) for item in _json_lines(stdout)]
    videos = [item for item in items if item is not None]
    logger.info("tiktok playlist @%s: %d items in %.1fs", clean_handle, len(videos), elapsed)
    if not videos and not allow_empty:
        raise TikTokYtDlpError(f"no TikTok videos returned for @{clean_handle}")
    return videos


def tiktok_probe_video(url: str, *, timeout: int = 120) -> TikTokVideoMetadata:
    """Backward-compatible alias for `tiktok_probe_media`."""
    return tiktok_probe_media(url, timeout=timeout)


def tiktok_probe_media(url: str, *, timeout: int = 120) -> TikTokVideoMetadata:
    """Fetch metadata for one TikTok URL and classify video/photo/unknown.

    yt-dlp's normal JSON does not expose TikTok `imagePost.images`, so this
    asks yt-dlp to save the page and parses TikTok's rehydration JSON from it.
    """
    cmd = _base_cmd() + [
        "--skip-download",
        "--no-playlist",
        "--write-pages",
        "--dump-json",
        url,
    ]
    with tempfile.TemporaryDirectory(prefix="openfeed-tiktok-") as tmp:
        tmp_path = Path(tmp)
        stdout, _stderr, elapsed = _run(cmd, timeout=timeout, cwd=tmp_path)
        item: TikTokVideoMetadata | None = None
        for raw in _json_lines(stdout):
            item = _normalize(raw)
            if item is not None:
                break
        if item is None:
            raise TikTokYtDlpError(f"no metadata returned for TikTok URL: {url}")

        for dump_path in tmp_path.glob("*.dump"):
            item_struct = _extract_item_struct_from_html(
                dump_path.read_text(encoding="utf-8", errors="ignore")
            )
            if item_struct:
                item = _apply_page_media(item, item_struct)
                break
    logger.info(
        "tiktok probe %s: kind=%s video=%s photos=%d in %.1fs",
        item.id,
        item.media_kind,
        item.has_video_stream,
        item.photo_count,
        elapsed,
    )
    return item


def fetch_tiktok_photo_images(
    item: TikTokVideoMetadata,
    *,
    max_images: int = 5,
    timeout: int = 20,
) -> list[bytes]:
    """Fetch photo-mode image bytes for multimodal LLM review."""
    images: list[bytes] = []
    for image in item.photo_images[:max_images]:
        request = Request(
            image.url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Referer": item.url,
            },
        )
        with urlopen(request, timeout=timeout) as response:
            images.append(response.read())
    return images


def tiktok_download(
    url: str,
    target_path: Path,
    *,
    max_filesize_mb: int = 80,
    timeout: int = 180,
) -> Path:
    """Download one TikTok URL as an mp4 suitable for native video cards."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.unlink(missing_ok=True)
    cmd = _base_cmd() + [
        "--no-progress",
        "--no-playlist",
        "--max-filesize",
        f"{max_filesize_mb}m",
        "-f",
        "b[vcodec=h264][acodec=aac]/bv*[vcodec=h264]+ba[acodec=aac]/b[vcodec=h264]",
        "--merge-output-format",
        "mp4",
        "-o",
        str(target_path),
        url,
    ]
    started = time.monotonic()
    try:
        _run(cmd, timeout=timeout)
    except TikTokYtDlpError:
        target_path.unlink(missing_ok=True)
        raise
    elapsed = time.monotonic() - started
    if not target_path.exists() or target_path.stat().st_size <= 0:
        raise TikTokYtDlpError(f"download finished but target is missing/empty: {target_path}")
    logger.info(
        "tiktok download ok: %s in %.1fs (%.1f MB)",
        target_path.name,
        elapsed,
        target_path.stat().st_size / 1e6,
    )
    return target_path
