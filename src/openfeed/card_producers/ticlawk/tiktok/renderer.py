"""TikTok renderer for Ticlawk cards."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from openfeed.card_producers.base import CardPayload, RenderContext
from openfeed.models.content_item import ContentItem
from openfeed.models.image_cache import ImageCacheIndex
from openfeed.models.video_cache import VideoCacheIndex


_logger = logging.getLogger("producer.ticlawk.tiktok")

_IMAGE_CACHE_INDEX_PATH = Path("state/image_cache_index.json")
_VIDEO_CACHE_INDEX_PATH = Path("state/video_cache_index.json")
_MAX_GALLERY_IMAGES = 20
_MAX_GALLERY_BYTES = 100 * 1024 * 1024


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_image_cache_index() -> ImageCacheIndex:
    if not _IMAGE_CACHE_INDEX_PATH.exists():
        return ImageCacheIndex(generated_at=_utc_now_iso(), images={})
    return ImageCacheIndex.model_validate(
        json.loads(_IMAGE_CACHE_INDEX_PATH.read_text(encoding="utf-8"))
    )


def _load_video_cache_index() -> VideoCacheIndex:
    if not _VIDEO_CACHE_INDEX_PATH.exists():
        return VideoCacheIndex(generated_at=_utc_now_iso(), videos={})
    return VideoCacheIndex.model_validate(
        json.loads(_VIDEO_CACHE_INDEX_PATH.read_text(encoding="utf-8"))
    )


def _bounded_gallery_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    total = 0
    for path in paths:
        if len(out) >= _MAX_GALLERY_IMAGES:
            break
        size = path.stat().st_size
        if out and total + size > _MAX_GALLERY_BYTES:
            break
        if not out and size > _MAX_GALLERY_BYTES:
            return []
        out.append(path)
        total += size
    return out


class TikTokRenderer:
    def render(self, item: ContentItem, ctx: RenderContext) -> CardPayload | None:
        del ctx
        if item.tiktok is None:
            return None
        if item.tiktok.media_kind == "video":
            return self._render_video(item)
        if item.tiktok.media_kind == "photo":
            return self._render_gallery(item)
        _logger.info("tiktok %s media_kind=%s has no renderer",
                     item.content_id, item.tiktok.media_kind)
        return None

    def _render_video(self, item: ContentItem) -> CardPayload | None:
        assert item.tiktok is not None
        cache_idx = _load_video_cache_index()
        cache_entry = cache_idx.videos.get(item.content_id)
        if cache_entry is None or cache_entry.state != "ready" or not cache_entry.local_path:
            _logger.info(
                "tiktok video %s not ready in video_cache (state=%s)",
                item.content_id, cache_entry.state if cache_entry else "missing",
            )
            return None
        local_path = Path(cache_entry.local_path)
        if not local_path.exists():
            _logger.warning("tiktok video %s cache index ready but file missing at %s",
                            item.content_id, local_path)
            return None

        title = (item.tiktok.title or item.content_id).strip()
        return CardPayload(
            title=title,
            content_subtype="video",
            video_path=str(local_path),
        )

    def _render_gallery(self, item: ContentItem) -> CardPayload | None:
        assert item.tiktok is not None
        cache_idx = _load_image_cache_index()
        cache_entry = cache_idx.images.get(item.content_id)
        if (
            cache_entry is None
            or cache_entry.state != "ready"
            or not cache_entry.image_paths
        ):
            _logger.info(
                "tiktok photo %s not ready in image_cache (state=%s)",
                item.content_id, cache_entry.state if cache_entry else "missing",
            )
            return None
        paths = [Path(p) for p in cache_entry.image_paths if Path(p).exists()]
        if not paths:
            _logger.warning("tiktok photo %s cache index ready but files missing", item.content_id)
            return None
        paths = _bounded_gallery_paths(paths)
        if not paths:
            _logger.warning("tiktok photo %s has no gallery images within ticlawk limits",
                            item.content_id)
            return None

        title = (item.tiktok.title or item.content_id).strip()
        return CardPayload(
            title=title,
            content_subtype="gallery",
            image_paths=[str(path) for path in paths],
        )
