from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openfeed.models.image_cache import ImageCacheIndex
from openfeed.models.queue import QueueItem
from openfeed.models.video_cache import VideoCacheIndex


@dataclass(frozen=True)
class MediaReadiness:
    ready: bool
    reason: str
    needs_local_media: bool = False
    permanent_failure: bool = False
    stale_rendered_card: bool = False


def _paths_exist(paths: list[str] | None) -> bool:
    return bool(paths) and all(Path(path).exists() for path in paths)


def _rendered_card_media_exists(item: QueueItem) -> bool:
    payload = item.rendered_card
    if payload is None:
        return False
    if payload.content_subtype == "video":
        return bool(payload.video_path) and Path(payload.video_path).exists()
    if payload.content_subtype == "gallery":
        return _paths_exist(payload.image_paths)
    return True


def _video_ready(
    item: QueueItem,
    video_idx: VideoCacheIndex,
    content_id: str,
) -> MediaReadiness:
    entry = video_idx.videos.get(content_id)
    has_rendered = item.rendered_card is not None
    if entry is None:
        return MediaReadiness(
            ready=False,
            reason="video_cache_missing",
            needs_local_media=True,
            stale_rendered_card=has_rendered,
        )
    if entry.state == "permanently_failed":
        return MediaReadiness(
            ready=False,
            reason="video_permanently_failed",
            needs_local_media=True,
            permanent_failure=True,
            stale_rendered_card=has_rendered,
        )
    if entry.state == "ready":
        if entry.local_path and Path(entry.local_path).exists():
            return MediaReadiness(
                ready=True,
                reason="video_ready",
                needs_local_media=True,
                stale_rendered_card=has_rendered and not _rendered_card_media_exists(item),
            )
        return MediaReadiness(
            ready=False,
            reason="video_ready_missing_local_file",
            needs_local_media=True,
            stale_rendered_card=has_rendered,
        )
    return MediaReadiness(
        ready=False,
        reason="video_not_ready",
        needs_local_media=True,
        stale_rendered_card=has_rendered,
    )


def _image_ready(
    item: QueueItem,
    image_idx: ImageCacheIndex,
    content_id: str,
) -> MediaReadiness:
    entry = image_idx.images.get(content_id)
    has_rendered = item.rendered_card is not None
    if entry is None:
        return MediaReadiness(
            ready=False,
            reason="image_cache_missing",
            needs_local_media=True,
            stale_rendered_card=has_rendered,
        )
    if entry.state == "permanently_failed":
        return MediaReadiness(
            ready=False,
            reason="image_permanently_failed",
            needs_local_media=True,
            permanent_failure=True,
            stale_rendered_card=has_rendered,
        )
    if entry.state == "ready":
        if _paths_exist(entry.image_paths):
            return MediaReadiness(
                ready=True,
                reason="image_ready",
                needs_local_media=True,
                stale_rendered_card=has_rendered and not _rendered_card_media_exists(item),
            )
        return MediaReadiness(
            ready=False,
            reason="image_ready_missing_local_file",
            needs_local_media=True,
            stale_rendered_card=has_rendered,
        )
    return MediaReadiness(
        ready=False,
        reason="image_not_ready",
        needs_local_media=True,
        stale_rendered_card=has_rendered,
    )


def queue_item_media_readiness(
    item: QueueItem,
    video_idx: VideoCacheIndex,
    image_idx: ImageCacheIndex,
) -> MediaReadiness:
    content = item.content
    if content.platform == "youtube":
        return _video_ready(item, video_idx, content.content_id)
    if content.platform == "tiktok":
        if content.tiktok is None:
            return MediaReadiness(False, "tiktok_digest_missing")
        if content.tiktok.media_kind == "video":
            return _video_ready(item, video_idx, content.content_id)
        if content.tiktok.media_kind == "photo":
            return _image_ready(item, image_idx, content.content_id)
        return MediaReadiness(False, "tiktok_media_kind_not_pushable")
    return MediaReadiness(True, "no_local_media_required")
