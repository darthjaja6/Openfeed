"""cleanup_assets — drop local media caches + ticlawk-hosted assets that have aged out.

Three reasons we delete:
  1. **Aged out**: card was pushed more than `keep_days` ago and isn't
     currently in queue. Frees both local disk and ticlawk storage.
  2. **Local cache overflow**: total local mp4 cache exceeds `cache_max_gb`.
     LRU-evict (oldest by `downloaded_at`) until under cap. Safety net.
  3. **Ticlawk quota approaching**: sum of asset sizes in video_assets.json
     plus image_assets.json exceeds `ticlawk_quota_max_gb`. LRU-evict
     ticlawk-side assets and matching local cache files until under. This
     threshold is account/deployment config, below the real Ticlawk creator
     asset quota.

Plus the **stranded asset reaper**: state/stranded_assets.json holds
asset_ids whose original DELETE failed during upload error recovery.
Each tick we retry DELETE; success → drop the entry, failure → leave for
next tick.

Active queue items are NEVER deleted, regardless of age — those are
about to be pushed. Once a card leaves the queue, it becomes a candidate.

Failures (ticlawk DELETE 5xx, file unlink) are logged and skipped — next
tick retries.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openfeed.utils.config_files import load_env

from openfeed.clients.consumer import ticlawk
from openfeed.models.history import HistoryEntry
from openfeed.models.image_assets import ImageAssetIndex
from openfeed.models.image_cache import ImageCacheIndex
from openfeed.models.queue import Queue
from openfeed.models.runtime import VideoCleanupConfig, load_runtime
from openfeed.models.video_assets import VideoAssetIndex
from openfeed.models.video_cache import VideoCacheIndex
from openfeed.utils.logging_setup import configure_task_logging
from openfeed.utils.state_io import atomic_write_json


logger = logging.getLogger("cleanup_assets")

_QUEUE_PATH = Path("state/queue.json")
_HISTORY_PATH = Path("ledgers/history.jsonl")
_CACHE_INDEX_PATH = Path("state/video_cache_index.json")
_ASSET_INDEX_PATH = Path("state/video_assets.json")
_IMAGE_CACHE_INDEX_PATH = Path("state/image_cache_index.json")
_IMAGE_ASSET_INDEX_PATH = Path("state/image_assets.json")
_STRANDED_PATH = Path("state/stranded_assets.json")
_CACHE_DIR = Path("state/video_cache")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    configure_task_logging("cleanup_assets")


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_queue_video_ids() -> set[str]:
    if not _QUEUE_PATH.exists():
        return set()
    q = Queue.model_validate(json.loads(_QUEUE_PATH.read_text(encoding="utf-8")))
    out: set[str] = set()
    for items in q.topics.values():
        for qi in items:
            if qi.content.platform == "youtube":
                out.add(qi.content.content_id)
            elif (
                qi.content.platform == "tiktok"
                and qi.content.tiktok is not None
                and qi.content.tiktok.media_kind == "video"
            ):
                out.add(qi.content.content_id)
    return out


def _load_queue_image_ids() -> set[str]:
    if not _QUEUE_PATH.exists():
        return set()
    q = Queue.model_validate(json.loads(_QUEUE_PATH.read_text(encoding="utf-8")))
    out: set[str] = set()
    for items in q.topics.values():
        for qi in items:
            if (
                qi.content.platform == "tiktok"
                and qi.content.tiktok is not None
                and qi.content.tiktok.media_kind == "photo"
            ):
                out.add(qi.content.content_id)
    return out


def _load_last_push_per_video() -> dict[str, datetime]:
    """video_id → most-recent pushed_at across history.jsonl. (One video may
    be pushed more than once via re-render; we keep the latest.)"""
    if not _HISTORY_PATH.exists():
        return {}
    out: dict[str, datetime] = {}
    for line in _HISTORY_PATH.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            h = HistoryEntry.model_validate_json(line)
        except Exception:  # noqa: BLE001
            continue
        if h.content_subtype != "video":
            continue
        try:
            ts = datetime.fromisoformat(h.pushed_at)
        except ValueError:
            continue
        prev = out.get(h.content_id)
        if prev is None or ts > prev:
            out[h.content_id] = ts
    return out


def _load_last_push_per_image() -> dict[str, datetime]:
    if not _HISTORY_PATH.exists():
        return {}
    out: dict[str, datetime] = {}
    for line in _HISTORY_PATH.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            h = HistoryEntry.model_validate_json(line)
        except Exception:  # noqa: BLE001
            continue
        if h.content_subtype != "gallery":
            continue
        try:
            ts = datetime.fromisoformat(h.pushed_at)
        except ValueError:
            continue
        prev = out.get(h.content_id)
        if prev is None or ts > prev:
            out[h.content_id] = ts
    return out


def _load_card_ids_by_content() -> dict[str, list[str]]:
    if not _HISTORY_PATH.exists():
        return {}
    out: dict[str, list[str]] = {}
    for line in _HISTORY_PATH.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            h = HistoryEntry.model_validate_json(line)
        except Exception:  # noqa: BLE001
            continue
        if h.card_id:
            out.setdefault(h.content_id, []).append(h.card_id)
    return out


def _load_cache_index() -> VideoCacheIndex:
    if not _CACHE_INDEX_PATH.exists():
        return VideoCacheIndex(generated_at=_utc_now_iso(), videos={})
    return VideoCacheIndex.model_validate(
        json.loads(_CACHE_INDEX_PATH.read_text(encoding="utf-8"))
    )


def _save_cache_index(idx: VideoCacheIndex) -> None:
    idx.generated_at = _utc_now_iso()
    atomic_write_json(_CACHE_INDEX_PATH, idx.model_dump())


def _load_asset_index() -> VideoAssetIndex:
    if not _ASSET_INDEX_PATH.exists():
        return VideoAssetIndex(generated_at=_utc_now_iso(), assets={})
    return VideoAssetIndex.model_validate(
        json.loads(_ASSET_INDEX_PATH.read_text(encoding="utf-8"))
    )


def _save_asset_index(idx: VideoAssetIndex) -> None:
    idx.generated_at = _utc_now_iso()
    atomic_write_json(_ASSET_INDEX_PATH, idx.model_dump())


def _load_image_cache_index() -> ImageCacheIndex:
    if not _IMAGE_CACHE_INDEX_PATH.exists():
        return ImageCacheIndex(generated_at=_utc_now_iso(), images={})
    return ImageCacheIndex.model_validate(
        json.loads(_IMAGE_CACHE_INDEX_PATH.read_text(encoding="utf-8"))
    )


def _save_image_cache_index(idx: ImageCacheIndex) -> None:
    idx.generated_at = _utc_now_iso()
    atomic_write_json(_IMAGE_CACHE_INDEX_PATH, idx.model_dump())


def _load_image_asset_index() -> ImageAssetIndex:
    if not _IMAGE_ASSET_INDEX_PATH.exists():
        return ImageAssetIndex(generated_at=_utc_now_iso(), assets={})
    return ImageAssetIndex.model_validate(
        json.loads(_IMAGE_ASSET_INDEX_PATH.read_text(encoding="utf-8"))
    )


def _save_image_asset_index(idx: ImageAssetIndex) -> None:
    idx.generated_at = _utc_now_iso()
    atomic_write_json(_IMAGE_ASSET_INDEX_PATH, idx.model_dump())


# ---------------------------------------------------------------------------
# Per-video delete (idempotent)
# ---------------------------------------------------------------------------


def _delete_cards(content_id: str, card_ids: dict[str, list[str]]) -> bool:
    ok = True
    for card_id in card_ids.get(content_id, []):
        try:
            ticlawk.delete_card(card_id)
            logger.info("[%s] ticlawk card %s deleted", content_id, card_id)
        except ticlawk.TiclawkError as exc:
            logger.warning("[%s] ticlawk card delete failed for %s: %s",
                           content_id, card_id, exc)
            ok = False
    return ok


def _delete_video(
    video_id: str, cache_idx: VideoCacheIndex, asset_idx: VideoAssetIndex,
    card_ids: dict[str, list[str]],
) -> tuple[bool, int]:
    """Delete every trace of this video. Returns (success, freed_bytes).
    Freed-bytes is best-effort; covers local file size."""
    freed = 0
    ok = True

    # Local file
    cache_entry = cache_idx.videos.get(video_id)
    if cache_entry and cache_entry.local_path:
        fp = Path(cache_entry.local_path)
        if fp.exists():
            try:
                freed = fp.stat().st_size
                fp.unlink()
                logger.info("[%s] removed local %s (%.1f MB)",
                            video_id, fp, freed / 1e6)
            except OSError as exc:
                logger.warning("[%s] local unlink failed: %s", video_id, exc)
                ok = False

    # Ticlawk asset
    asset_entry = asset_idx.assets.get(video_id)
    if asset_entry:
        if not _delete_cards(video_id, card_ids):
            ok = False
        try:
            ticlawk.delete_video(asset_entry.asset_id)
            logger.info("[%s] ticlawk asset %s deleted", video_id, asset_entry.asset_id)
        except ticlawk.TiclawkError as exc:
            logger.warning("[%s] ticlawk delete failed: %s", video_id, exc)
            ok = False
        else:
            asset_idx.assets.pop(video_id, None)

    if ok and video_id in cache_idx.videos:
        cache_idx.videos.pop(video_id, None)
    return ok, freed


def _delete_image_set(
    content_id: str, cache_idx: ImageCacheIndex, asset_idx: ImageAssetIndex,
    card_ids: dict[str, list[str]],
) -> tuple[bool, int]:
    """Delete local cached images and Ticlawk assets for one gallery card."""
    freed = 0
    ok = True

    cache_entry = cache_idx.images.get(content_id)
    if cache_entry:
        parent_dirs: set[Path] = set()
        for raw in cache_entry.image_paths:
            fp = Path(raw)
            parent_dirs.add(fp.parent)
            if fp.exists():
                try:
                    freed += fp.stat().st_size
                    fp.unlink()
                    logger.info("[%s] removed local image %s", content_id, fp)
                except OSError as exc:
                    logger.warning("[%s] local image unlink failed: %s", content_id, exc)
                    ok = False
        for directory in parent_dirs:
            try:
                directory.rmdir()
            except OSError:
                pass

    asset_entry = asset_idx.assets.get(content_id)
    if asset_entry:
        if not _delete_cards(content_id, card_ids):
            ok = False
        for asset_id in asset_entry.asset_ids:
            try:
                ticlawk.delete_asset(asset_id)
                logger.info("[%s] ticlawk image asset %s deleted", content_id, asset_id)
            except ticlawk.TiclawkError as exc:
                logger.warning("[%s] ticlawk image delete failed: %s", content_id, exc)
                ok = False
        if ok:
            asset_idx.assets.pop(content_id, None)

    if ok and content_id in cache_idx.images:
        cache_idx.images.pop(content_id, None)
    return ok, freed


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _reap_stranded() -> tuple[int, int]:
    """Retry DELETE for asset_ids recorded as stranded (DELETE failed earlier).
    Returns (reaped, remaining)."""
    if not _STRANDED_PATH.exists():
        return 0, 0
    try:
        data = json.loads(_STRANDED_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0, 0
    rows = data.get("stranded") or []
    if not rows:
        return 0, 0
    reaped = 0
    remaining: list[dict] = []
    for row in rows:
        asset_id = row.get("asset_id")
        if not asset_id:
            continue
        try:
            ticlawk.delete_video(asset_id)
            reaped += 1
            logger.info("[stranded] reaped asset %s", asset_id)
        except ticlawk.TiclawkError as exc:
            logger.warning("[stranded] reap failed for %s: %s", asset_id, exc)
            remaining.append(row)
    if reaped:
        atomic_write_json(_STRANDED_PATH, {"stranded": remaining})
    return reaped, len(remaining)


def _run_tick(cfg: VideoCleanupConfig) -> tuple[int, int, int, float]:
    """Returns (aged_out_deleted, evicted_for_local_cap, evicted_for_ticlawk_quota, freed_mb)."""
    queue_vids = _load_queue_video_ids()
    queue_imgs = _load_queue_image_ids()
    last_push = _load_last_push_per_video()
    last_image_push = _load_last_push_per_image()
    card_ids = _load_card_ids_by_content()
    cache_idx = _load_cache_index()
    asset_idx = _load_asset_index()
    image_cache_idx = _load_image_cache_index()
    image_asset_idx = _load_image_asset_index()

    # Pass 0: stranded asset reap (recover ticlawk quota leaked by failed PUTs).
    reaped, remaining = _reap_stranded()
    if reaped or remaining:
        logger.info("stranded assets: reaped=%d still_pending=%d", reaped, remaining)

    now = datetime.now(timezone.utc)
    keep_window = timedelta(days=cfg.keep_days)

    # Pass 1: aged-out deletion.
    aged_out: list[str] = []
    candidates = set(cache_idx.videos.keys()) | set(asset_idx.assets.keys())
    for vid in candidates:
        if vid in queue_vids:
            continue
        last = last_push.get(vid)
        if last is None:
            entry = cache_idx.videos.get(vid)
            if entry and entry.downloaded_at:
                try:
                    last = datetime.fromisoformat(entry.downloaded_at)
                except ValueError:
                    last = None
        if last is None or (now - last) > keep_window:
            aged_out.append(vid)

    aged_out_images: list[str] = []
    image_candidates = set(image_cache_idx.images.keys()) | set(image_asset_idx.assets.keys())
    for content_id in image_candidates:
        if content_id in queue_imgs:
            continue
        last = last_image_push.get(content_id)
        if last is None:
            entry = image_cache_idx.images.get(content_id)
            if entry and entry.downloaded_at:
                try:
                    last = datetime.fromisoformat(entry.downloaded_at)
                except ValueError:
                    last = None
        if last is None or (now - last) > keep_window:
            aged_out_images.append(content_id)

    freed_total = 0
    deleted_aged = 0
    for vid in aged_out:
        ok, freed = _delete_video(vid, cache_idx, asset_idx, card_ids)
        if ok:
            deleted_aged += 1
            freed_total += freed
    for content_id in aged_out_images:
        ok, freed = _delete_image_set(content_id, image_cache_idx, image_asset_idx, card_ids)
        if ok:
            deleted_aged += 1
            freed_total += freed

    # Pass 2: local cache cap (LRU on downloaded_at, oldest first).
    cap_bytes = int(cfg.cache_max_gb * 1024 * 1024 * 1024)
    total_cached = sum((e.size_bytes or 0) for e in cache_idx.videos.values())
    evicted_local = 0
    if total_cached > cap_bytes:
        ranked = sorted(
            cache_idx.videos.items(),
            key=lambda kv: kv[1].downloaded_at or "",
        )
        for vid, _entry in ranked:
            if total_cached <= cap_bytes:
                break
            if vid in queue_vids:
                continue
            ok, freed = _delete_video(vid, cache_idx, asset_idx, card_ids)
            if ok:
                evicted_local += 1
                freed_total += freed
                total_cached -= freed
        if total_cached > cap_bytes:
            logger.warning(
                "local cache still over cap (%.1f GB > %.1f GB) after evicting %d "
                "non-active videos; remaining are queue-active",
                total_cached / 1e9, cfg.cache_max_gb, evicted_local,
            )

    # Pass 3: ticlawk quota cap (LRU on uploaded_at, oldest first). Drops
    # ticlawk-side assets first; the local mp4 file goes with them via
    # _delete_video. This protects against the configured creator quota.
    quota_bytes = int(cfg.ticlawk_quota_max_gb * 1024 * 1024 * 1024)
    total_assets = (
        sum(a.size_bytes for a in asset_idx.assets.values())
        + sum(a.size_bytes for a in image_asset_idx.assets.values())
    )
    evicted_quota = 0
    if total_assets > quota_bytes:
        ranked_assets = sorted(
            [(entry.uploaded_at or "", "video", key, entry.size_bytes)
             for key, entry in asset_idx.assets.items()]
            + [(entry.uploaded_at or "", "image", key, entry.size_bytes)
               for key, entry in image_asset_idx.assets.items()]
        )
        for _uploaded_at, kind, key, size_bytes in ranked_assets:
            if total_assets <= quota_bytes:
                break
            if kind == "video" and key in queue_vids:
                continue
            if kind == "image" and key in queue_imgs:
                continue
            if kind == "video":
                ok, freed = _delete_video(key, cache_idx, asset_idx, card_ids)
            else:
                ok, freed = _delete_image_set(key, image_cache_idx, image_asset_idx, card_ids)
            if ok:
                evicted_quota += 1
                freed_total += freed
                total_assets -= size_bytes
        if total_assets > quota_bytes:
            logger.warning(
                "ticlawk asset usage still over quota (%.1f GB > %.1f GB) — "
                "remaining %d video assets / %d image sets include queue-active assets",
                total_assets / 1e9, cfg.ticlawk_quota_max_gb,
                len(asset_idx.assets), len(image_asset_idx.assets),
            )

    _save_cache_index(cache_idx)
    _save_asset_index(asset_idx)
    _save_image_cache_index(image_cache_idx)
    _save_image_asset_index(image_asset_idx)
    return deleted_aged, evicted_local, evicted_quota, freed_total / 1e6


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openfeed-cleanup-assets")
    ap.parse_args(argv)

    _configure_logging()
    workdir = Path.cwd()
    load_env(workdir)

    runtime = load_runtime(workdir)
    cfg = runtime.video_cleanup

    aged_out, evicted_local, evicted_quota, freed_mb = _run_tick(cfg)
    logger.info(
        "cleanup_assets tick: aged_out=%d evicted_local_cap=%d "
        "evicted_ticlawk_quota=%d freed=%.1f MB",
        aged_out, evicted_local, evicted_quota, freed_mb,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
