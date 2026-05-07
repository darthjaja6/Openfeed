"""prepare_video — supply-side task that pre-downloads native video mp4s.

Why this exists separate from push: push has a tight 30s budget per tick,
and a single yt-dlp run plus eventual ticlawk upload can blow that. By
splitting download out and running it independently from supply_cycle, push
tick stays fast (it just uploads from the local cache). Push selects from
queue.json freely; YouTube items whose mp4 isn't ready yet get skipped at
push time and get another shot next tick — no blocking.

Each tick:
  1. Load queue.json + state/video_cache_index.json.
  2. Read the canonical per-topic queue order produced by queue_manage.
     The local media cache serves this near-term push window, not the whole queue.
  3. Find native video cards in that working set whose content_id is either:
       - not in the index yet (never tried)
       - in `failed` state past the backoff window
     `permanently_failed` and `ready` videos are skipped.
  4. If the local cache is already at/over the configured cap at tick start,
     evict ready mp4s outside the current working set. Otherwise pick top N
     (`max_per_tick`) by topic demand, preserving queue order within each topic;
     run yt-dlp in parallel up to `max_concurrent`, bounded by `tick_budget_seconds`.
  5. Per result: write file to `state/video_cache/<vid>.mp4`, update
     index entry to `ready` (or bump failure_count and set `failed` /
     `permanently_failed`).
  6. Atomic-rewrite the index.

Failure of any single video doesn't stall the others — each runs in its
own thread. Tick budget exhaustion is graceful: in-flight downloads
continue if subprocess lets them, but unstarted ones simply roll to next
tick.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from openfeed.utils.config_files import load_env

from openfeed.clients.content.tiktok import (
    TikTokYtDlpError,
    tiktok_download,
)
from openfeed.clients.content.youtube_download import (
    YouTubeDownloadError,
    YouTubeDownloadPermanentError,
    download as yt_download,
)
from openfeed.models.image_cache import ImageCacheEntry, ImageCacheIndex
from openfeed.models.queue import Queue, QueueStatus
from openfeed.models.runtime import (
    TikTokImageDownloadConfig,
    TikTokDownloadConfig,
    YouTubeDownloadConfig,
    load_runtime,
)
from openfeed.models.video_cache import (
    VideoCacheEntry, VideoCacheIndex,
)
from openfeed.utils import backpressure
from openfeed.utils.logging_setup import configure_task_logging
from openfeed.utils.state_io import atomic_write_json


logger = logging.getLogger("prepare_video")

_QUEUE_PATH = Path("state/queue.json")
_QUEUE_STATUS_PATH = Path("state/queue_status.json")
_INDEX_PATH = Path("state/video_cache_index.json")
_CACHE_DIR = Path("state/video_cache")
_IMAGE_INDEX_PATH = Path("state/image_cache_index.json")
_IMAGE_CACHE_DIR = Path("state/image_cache")
_CACHE_CAP_COOLDOWN_SECONDS = 10 * 60


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    configure_task_logging("prepare_video")


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _load_queue() -> Queue:
    if not _QUEUE_PATH.exists():
        return Queue(generated_at=_utc_now_iso(), topics={})
    return Queue.model_validate(json.loads(_QUEUE_PATH.read_text(encoding="utf-8")))


def _load_queue_status() -> QueueStatus | None:
    if not _QUEUE_STATUS_PATH.exists():
        return None
    return QueueStatus.model_validate(
        json.loads(_QUEUE_STATUS_PATH.read_text(encoding="utf-8"))
    )


def _load_index() -> VideoCacheIndex:
    if not _INDEX_PATH.exists():
        return VideoCacheIndex(generated_at=_utc_now_iso(), videos={})
    return VideoCacheIndex.model_validate(
        json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
    )


def _load_image_index() -> ImageCacheIndex:
    if not _IMAGE_INDEX_PATH.exists():
        return ImageCacheIndex(generated_at=_utc_now_iso(), images={})
    return ImageCacheIndex.model_validate(
        json.loads(_IMAGE_INDEX_PATH.read_text(encoding="utf-8"))
    )


def _save_index(index: VideoCacheIndex) -> None:
    index.generated_at = _utc_now_iso()
    atomic_write_json(_INDEX_PATH, index.model_dump())


def _save_image_index(index: ImageCacheIndex) -> None:
    index.generated_at = _utc_now_iso()
    atomic_write_json(_IMAGE_INDEX_PATH, index.model_dump())


def _cache_size_bytes() -> int:
    if not _CACHE_DIR.exists():
        return 0
    total = 0
    for path in _CACHE_DIR.rglob("*"):
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _evict_ready_videos_outside_working_set(
    index: VideoCacheIndex,
    protected_ids: set[str],
    *,
    cap_bytes: int,
) -> tuple[int, int]:
    if cap_bytes <= 0:
        return 0, 0
    cache_bytes = _cache_size_bytes()
    if cache_bytes < cap_bytes:
        return 0, 0
    target_bytes = int(cap_bytes * 0.8)
    candidates = []
    for video_id, entry in index.videos.items():
        if video_id in protected_ids:
            continue
        if entry.state != "ready" or not entry.local_path:
            continue
        path = Path(entry.local_path)
        if not path.exists():
            candidates.append((entry.downloaded_at or "", video_id, path, 0, True))
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        candidates.append((entry.downloaded_at or "", video_id, path, size, False))

    evicted = 0
    freed = 0
    for _downloaded_at, video_id, path, size, missing in sorted(candidates):
        if cache_bytes < target_bytes:
            break
        if not missing:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("[%s] local cache eviction failed: %s", video_id, exc)
                continue
        index.videos.pop(video_id, None)
        evicted += 1
        freed += size
        cache_bytes -= size

    if evicted:
        _save_index(index)
        logger.info(
            "evicted %d ready videos outside working set (%.1f MB)",
            evicted,
            freed / 1e6,
        )
    return evicted, freed


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def _topic_priority(topic: str, status: QueueStatus | None) -> tuple[int, int, int, int]:
    if status is None:
        return (0, 0, 0, 0)
    topic_status = status.per_topic.get(topic)
    if topic_status is None:
        return (0, 0, 0, 0)
    pushable = topic_status.pushable_inventory
    blocked = topic_status.blocked_inventory
    if pushable == 0 and topic_status.refill_gap > 0:
        tier = 3
    elif topic_status.refill_gap > 0:
        tier = 2
    elif blocked > 0:
        tier = 1
    else:
        tier = 0
    return (tier, topic_status.refill_gap, -pushable, blocked)


def _ordered_topics(topics: list[str], status: QueueStatus | None) -> list[str]:
    def sort_key(topic: str) -> tuple[int, int, int, int, str]:
        tier, gap, neg_pushable, blocked = _topic_priority(topic, status)
        return (-tier, -gap, -neg_pushable, -blocked, topic)

    return sorted(
        topics,
        key=sort_key,
    )


def _video_url(qi, platform: str) -> str:
    if platform == "youtube" and qi.content.youtube is not None:
        return qi.content.youtube.url
    if platform == "tiktok" and qi.content.tiktok is not None:
        return qi.content.tiktok.url
    return ""


def _video_items_for_platform(items: list, platform: str) -> list:
    out = []
    for qi in items:
        if qi.content.platform != platform:
            continue
        if platform == "tiktok" and (
            qi.content.tiktok is None or qi.content.tiktok.media_kind != "video"
        ):
            continue
        if not _video_url(qi, platform):
            continue
        out.append(qi)
    return out


def _video_working_set(
    queue: Queue,
    cfg: YouTubeDownloadConfig | TikTokDownloadConfig,
    *,
    platform: str,
    topic_filter: str | None = None,
) -> list:
    out = []
    for topic, items in queue.topics.items():
        if topic_filter is not None and topic != topic_filter:
            continue
        media_items = _video_items_for_platform(items, platform)
        out.extend(media_items[:cfg.ready_target_per_topic])
    return out


def _video_working_set_ids(
    queue: Queue,
    cfg: YouTubeDownloadConfig | TikTokDownloadConfig,
    *,
    platform: str,
    topic_filter: str | None = None,
) -> set[str]:
    return {
        qi.content.content_id
        for qi in _video_working_set(queue, cfg, platform=platform, topic_filter=topic_filter)
    }


def _candidates(
    queue: Queue,
    index: VideoCacheIndex,
    cfg: YouTubeDownloadConfig | TikTokDownloadConfig,
    *,
    platform: str,
    topic_filter: str | None = None,
) -> list[tuple[str, str, float]]:
    """Return list of (content_id, url, rank_score) eligible this tick.

    Queue order is already source-diverse inside each topic. prepare keeps
    that order, then interleaves topics by queue_status demand so topics with
    zero pushable media get download budget first.
    """
    backoff = timedelta(minutes=cfg.failure_backoff_minutes)
    now = _utc_now()
    status = _load_queue_status()
    by_topic: dict[str, dict[str, tuple[str, float]]] = {}
    for topic, items in queue.topics.items():
        if topic_filter is not None and topic != topic_filter:
            continue
        eligible_items = []
        for qi in _video_items_for_platform(items, platform):
            content_id = qi.content.content_id
            url = _video_url(qi, platform)
            if not url:
                continue
            entry = index.videos.get(content_id)
            if entry is not None:
                if entry.state == "ready" and entry.local_path and Path(entry.local_path).exists():
                    continue
                if entry.state == "permanently_failed":
                    continue
                if entry.state == "failed" and entry.last_failed_at:
                    try:
                        last = datetime.fromisoformat(entry.last_failed_at)
                    except ValueError:
                        last = None
                    if last is not None and (now - last) < backoff:
                        continue
            eligible_items.append(qi)
        slot = by_topic.setdefault(topic, {})
        for qi in eligible_items[:cfg.ready_target_per_topic]:
            content_id = qi.content.content_id
            url = _video_url(qi, platform)
            existing = slot.get(content_id)
            if existing is None or qi.rank_score > existing[1]:
                slot[content_id] = (url, qi.rank_score)
    by_topic = {topic: slot for topic, slot in by_topic.items() if slot}
    per_topic_sorted = {
        topic: [
            (content_id, url, score)
            for content_id, (url, score) in vids.items()
        ]
        for topic, vids in by_topic.items() if vids
    }
    # Interleave: round 0 takes #1 from each topic, round 1 takes #2, etc.
    # Within a round, topics are visited by demand priority from queue_status.
    out: list[tuple[str, str, float]] = []
    if not per_topic_sorted:
        return out
    max_depth = max(len(lst) for lst in per_topic_sorted.values())
    topics = _ordered_topics(list(per_topic_sorted.keys()), status)
    for depth in range(max_depth):
        for topic in topics:
            lst = per_topic_sorted[topic]
            if depth < len(lst):
                out.append(lst[depth])
    return out


def _image_candidates(
    queue: Queue,
    index: ImageCacheIndex,
    cfg: TikTokImageDownloadConfig,
    *,
    topic_filter: str | None = None,
) -> list[tuple[str, list[str], str, float]]:
    """Return (content_id, image_urls, referer, rank_score) for TikTok photos."""
    backoff = timedelta(minutes=cfg.failure_backoff_minutes)
    now = _utc_now()
    status = _load_queue_status()
    by_topic: dict[str, dict[str, tuple[list[str], str, float]]] = {}
    for topic, items in queue.topics.items():
        if topic_filter is not None and topic != topic_filter:
            continue
        slot: dict[str, tuple[list[str], str, float]] = {}
        media_items = [
            qi for qi in items
            if (
                qi.content.platform == "tiktok"
                and qi.content.tiktok is not None
                and qi.content.tiktok.media_kind == "photo"
            )
        ]
        for qi in media_items[:cfg.ready_target_per_topic]:
            content_id = qi.content.content_id
            urls = [u for u in qi.content.tiktok.photo_image_urls if u]
            if not urls:
                continue
            entry = index.images.get(content_id)
            if entry is not None:
                if entry.state == "ready" and all(Path(p).exists() for p in entry.image_paths):
                    continue
                if entry.state == "permanently_failed":
                    continue
                if entry.state == "failed" and entry.last_failed_at:
                    try:
                        last = datetime.fromisoformat(entry.last_failed_at)
                    except ValueError:
                        last = None
                    if last is not None and (now - last) < backoff:
                        continue
            existing = slot.get(content_id)
            if existing is None or qi.rank_score > existing[2]:
                slot[content_id] = (urls, qi.content.tiktok.url, qi.rank_score)
        if slot:
            by_topic[topic] = slot
    per_topic_sorted = {
        topic: [
            (content_id, urls, referer, score)
            for content_id, (urls, referer, score) in items.items()
        ]
        for topic, items in by_topic.items() if items
    }
    out: list[tuple[str, list[str], str, float]] = []
    if not per_topic_sorted:
        return out
    max_depth = max(len(lst) for lst in per_topic_sorted.values())
    topics = _ordered_topics(list(per_topic_sorted.keys()), status)
    for depth in range(max_depth):
        for topic in topics:
            lst = per_topic_sorted[topic]
            if depth < len(lst):
                out.append(lst[depth])
    return out


# ---------------------------------------------------------------------------
# Download phase
# ---------------------------------------------------------------------------


def _download_one(
    platform: str,
    content_id: str,
    url: str,
    cfg: YouTubeDownloadConfig | TikTokDownloadConfig,
) -> tuple[str, Path | None, str | None, bool]:
    """Returns (content_id, local_path | None, error_message | None, permanent)."""
    target = _CACHE_DIR / f"{content_id}.mp4"
    try:
        if platform == "youtube":
            assert isinstance(cfg, YouTubeDownloadConfig)
            yt_download(
                content_id,
                target,
                max_height=cfg.target_height,
                max_filesize_mb=cfg.max_filesize_mb,
                timeout_seconds=cfg.tick_budget_seconds,
            )
        elif platform == "tiktok":
            assert isinstance(cfg, TikTokDownloadConfig)
            tiktok_download(
                url,
                target,
                max_filesize_mb=cfg.max_filesize_mb,
                timeout=cfg.tick_budget_seconds,
            )
        else:
            raise RuntimeError(f"unsupported video platform: {platform}")
        return content_id, target, None, False
    except YouTubeDownloadPermanentError as exc:
        return content_id, None, str(exc), True
    except (YouTubeDownloadError, TikTokYtDlpError) as exc:
        return content_id, None, str(exc), False
    except Exception as exc:  # noqa: BLE001 — defensive
        return content_id, None, f"unexpected: {exc!r}", False


def _image_ext(url: str, content_type: str | None) -> str:
    if content_type:
        ctype = content_type.split(";", 1)[0].strip().lower()
        if ctype == "image/png":
            return ".png"
        if ctype == "image/webp":
            return ".webp"
        if ctype in {"image/jpeg", "image/jpg"}:
            return ".jpg"
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def _download_image_set(
    content_id: str,
    image_urls: list[str],
    referer: str,
    cfg: TikTokImageDownloadConfig,
) -> tuple[str, list[Path] | None, str | None]:
    target_dir = _IMAGE_CACHE_DIR / "tiktok" / content_id
    tmp_dir = target_dir.with_name(f"{target_dir.name}.tmp")
    try:
        if tmp_dir.exists():
            for old in tmp_dir.glob("*"):
                old.unlink(missing_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        deadline = time.monotonic() + cfg.tick_budget_seconds
        for idx, url in enumerate(image_urls):
            remaining = max(1, int(deadline - time.monotonic()))
            request = Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Referer": referer,
                },
            )
            with urlopen(request, timeout=remaining) as response:
                data = response.read()
                ext = _image_ext(url, response.headers.get("Content-Type"))
            if not data:
                raise RuntimeError(f"empty image response for {url[:120]}")
            path = tmp_dir / f"image_{idx:02d}{ext}"
            path.write_bytes(data)
            paths.append(path)
        if target_dir.exists():
            for old in target_dir.glob("*"):
                old.unlink(missing_ok=True)
        else:
            target_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir.replace(target_dir)
        final_paths = sorted(target_dir.glob("image_*"))
        logger.info(
            "[%s] images ready (%d files, %.1f MB)",
            content_id,
            len(final_paths),
            sum(p.stat().st_size for p in final_paths) / 1e6,
        )
        return content_id, final_paths, None
    except Exception as exc:  # noqa: BLE001
        if tmp_dir.exists():
            for old in tmp_dir.glob("*"):
                old.unlink(missing_ok=True)
            tmp_dir.rmdir()
        return content_id, None, str(exc)


def _run_tick(
    platform: str,
    cfg: YouTubeDownloadConfig | TikTokDownloadConfig,
    *,
    cache_max_gb: float,
    protected_ids: set[str],
    topic_filter: str | None = None,
) -> tuple[int, int, int, int]:
    """One prepare_video tick. Returns (eligible, attempted, ready, failed)."""
    lane = (
        backpressure.YOUTUBE_DOWNLOAD
        if platform == "youtube"
        else backpressure.TIKTOK_DOWNLOAD
    )
    block = backpressure.active_block(lane)
    if block is not None:
        logger.warning(
            "%s download backpressure active (%s): %s",
            platform,
            block.get("reason"), block.get("detail", ""),
        )
        return 0, 0, 0, 0

    queue = _load_queue()
    index = _load_index()
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cap_bytes = int(cache_max_gb * 1024 * 1024 * 1024)
    cache_bytes = _cache_size_bytes()
    if cap_bytes > 0 and cache_bytes >= cap_bytes:
        _evict_ready_videos_outside_working_set(
            index,
            protected_ids,
            cap_bytes=cap_bytes,
        )
        cache_bytes = _cache_size_bytes()
    if cap_bytes > 0 and cache_bytes >= cap_bytes:
        detail = f"video cache at {cache_bytes / 1024 / 1024 / 1024:.2f} GB >= {cache_max_gb:.2f} GB"
        backpressure.block_lane(
            lane,
            reason="local_cache_cap",
            detail=detail,
            cooldown_seconds=_CACHE_CAP_COOLDOWN_SECONDS,
        )
        logger.warning(
            "video cache at cap: %.2f GB >= %.2f GB; skipping downloads",
            cache_bytes / 1024 / 1024 / 1024, cache_max_gb,
        )
        return 0, 0, 0, 0

    eligible = _candidates(queue, index, cfg, platform=platform, topic_filter=topic_filter)
    if not eligible:
        logger.info("no eligible %s videos this tick", platform)
        return 0, 0, 0, 0
    todo = eligible[:cfg.max_per_tick]
    logger.info(
        "%s: %d eligible, downloading top %d (max_concurrent=%d, budget=%ds)",
        platform, len(eligible), len(todo), cfg.max_concurrent, cfg.tick_budget_seconds,
    )

    deadline = time.monotonic() + cfg.tick_budget_seconds
    pool = ThreadPoolExecutor(max_workers=cfg.max_concurrent)
    try:
        futures = {
            pool.submit(_download_one, platform, content_id, url, cfg): content_id
            for content_id, url, _ in todo
        }
        remaining = max(0.0, deadline - time.monotonic())
        done, not_done = wait(futures, timeout=remaining, return_when=ALL_COMPLETED)
        for fut in not_done:
            fut.cancel()

        ready = failed = 0
        for fut, content_id in futures.items():
            if fut not in done:
                logger.info("[%s] tick budget exhausted, will retry next tick", content_id)
                continue
            try:
                video_id, local_path, err, permanent = fut.result()
            except Exception as exc:  # noqa: BLE001
                video_id, local_path, err, permanent = content_id, None, f"future error: {exc!r}", False

            entry = index.videos.get(video_id) or VideoCacheEntry(
                video_id=video_id, state="failed",
            )
            if local_path is not None:
                size = local_path.stat().st_size
                entry.state = "ready"
                entry.local_path = str(local_path)
                entry.size_bytes = size
                entry.downloaded_at = _utc_now_iso()
                entry.failure_count = 0
                entry.last_failed_at = None
                entry.last_error = None
                ready += 1
                logger.info("[%s] ready (%.1f MB)", video_id, size / 1e6)
            else:
                entry.failure_count += 1
                entry.last_failed_at = _utc_now_iso()
                entry.last_error = (err or "unknown")[:300]
                if permanent:
                    entry.state = "permanently_failed"
                    logger.warning(
                        "[%s] permanently_failed by download policy: %s",
                        video_id, entry.last_error,
                    )
                elif entry.failure_count >= cfg.max_failures_before_permanent:
                    entry.state = "permanently_failed"
                    logger.warning(
                        "[%s] permanently_failed after %d attempts: %s",
                        video_id, entry.failure_count, entry.last_error,
                    )
                else:
                    entry.state = "failed"
                    logger.warning(
                        "[%s] failed (count=%d): %s",
                        video_id, entry.failure_count, entry.last_error,
                    )
                failed += 1
            index.videos[video_id] = entry

        _save_index(index)
        return len(eligible), len(todo), ready, failed
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _run_image_tick(
    cfg: TikTokImageDownloadConfig,
    *,
    topic_filter: str | None = None,
) -> tuple[int, int, int, int]:
    """One TikTok image prepare tick. Returns (eligible, attempted, ready, failed)."""
    queue = _load_queue()
    index = _load_image_index()
    _IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    eligible = _image_candidates(queue, index, cfg, topic_filter=topic_filter)
    if not eligible:
        logger.info("no eligible tiktok photo items this tick")
        return 0, 0, 0, 0
    todo = eligible[:cfg.max_per_tick]
    logger.info(
        "tiktok_image: %d eligible, downloading top %d (max_concurrent=%d, budget=%ds)",
        len(eligible), len(todo), cfg.max_concurrent, cfg.tick_budget_seconds,
    )
    deadline = time.monotonic() + cfg.tick_budget_seconds
    pool = ThreadPoolExecutor(max_workers=cfg.max_concurrent)
    try:
        futures = {
            pool.submit(_download_image_set, content_id, urls, referer, cfg): content_id
            for content_id, urls, referer, _ in todo
        }
        remaining = max(0.0, deadline - time.monotonic())
        done, not_done = wait(futures, timeout=remaining, return_when=ALL_COMPLETED)
        for fut in not_done:
            fut.cancel()

        ready = failed = 0
        for fut, content_id in futures.items():
            if fut not in done:
                logger.info("[%s] image tick budget exhausted, will retry next tick", content_id)
                continue
            try:
                item_id, paths, err = fut.result()
            except Exception as exc:  # noqa: BLE001
                item_id, paths, err = content_id, None, f"future error: {exc!r}"
            entry = index.images.get(item_id) or ImageCacheEntry(
                content_id=item_id,
                platform="tiktok",
                state="failed",
            )
            if paths:
                entry.state = "ready"
                entry.image_paths = [str(path) for path in paths]
                entry.image_count = len(paths)
                entry.downloaded_at = _utc_now_iso()
                entry.failure_count = 0
                entry.last_failed_at = None
                entry.last_error = None
                ready += 1
            else:
                entry.failure_count += 1
                entry.last_failed_at = _utc_now_iso()
                entry.last_error = (err or "unknown")[:300]
                if entry.failure_count >= cfg.max_failures_before_permanent:
                    entry.state = "permanently_failed"
                    logger.warning(
                        "[%s] images permanently_failed after %d attempts: %s",
                        item_id, entry.failure_count, entry.last_error,
                    )
                else:
                    entry.state = "failed"
                    logger.warning(
                        "[%s] images failed (count=%d): %s",
                        item_id, entry.failure_count, entry.last_error,
                    )
                failed += 1
            index.images[item_id] = entry
        _save_image_index(index)
        return len(eligible), len(todo), ready, failed
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openfeed-prepare-video")
    ap.add_argument("--topic", help="Only prepare videos for one topic")
    ap.add_argument(
        "--platform",
        choices=["youtube", "tiktok"],
        help="Only prepare videos for one platform",
    )
    ap.add_argument(
        "--media-kind",
        choices=["all", "video", "image"],
        default="all",
        help="For TikTok, prepare video mp4s, photo images, or both",
    )
    args = ap.parse_args(argv)

    _configure_logging()
    workdir = Path.cwd()
    load_env(workdir)

    runtime = load_runtime(workdir)
    platforms = [args.platform] if args.platform else ["youtube", "tiktok"]
    queue = _load_queue()
    protected_ids = (
        _video_working_set_ids(
            queue,
            runtime.youtube_download,
            platform="youtube",
            topic_filter=args.topic,
        )
        | _video_working_set_ids(
            queue,
            runtime.tiktok_download,
            platform="tiktok",
            topic_filter=args.topic,
        )
    )

    totals = {"eligible": 0, "attempted": 0, "ready": 0, "failed": 0}
    for platform in platforms:
        if args.media_kind in {"all", "video"}:
            cfg = runtime.youtube_download if platform == "youtube" else runtime.tiktok_download
            eligible, attempted, ready, failed = _run_tick(
                platform,
                cfg,
                cache_max_gb=runtime.video_cleanup.cache_max_gb,
                protected_ids=protected_ids,
                topic_filter=args.topic,
            )
            logger.info(
                "prepare_video[%s] tick: eligible=%d attempted=%d ready=%d failed=%d",
                platform, eligible, attempted, ready, failed,
            )
            totals["eligible"] += eligible
            totals["attempted"] += attempted
            totals["ready"] += ready
            totals["failed"] += failed
        if platform == "tiktok" and args.media_kind in {"all", "image"}:
            eligible, attempted, ready, failed = _run_image_tick(
                runtime.tiktok_image_download,
                topic_filter=args.topic,
            )
            logger.info(
                "prepare_image[tiktok] tick: eligible=%d attempted=%d ready=%d failed=%d",
                eligible, attempted, ready, failed,
            )
            totals["eligible"] += eligible
            totals["attempted"] += attempted
            totals["ready"] += ready
            totals["failed"] += failed
    logger.info("prepare_video tick total: %s", totals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
