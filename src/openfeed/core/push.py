"""Push — pull cards from `state/queue.json`, render lazily, ship to Ticlawk.

Per PRD §5.5 (revised): rendering happens at push time, not at filter admit
time. Each tick:

  1. Read channel metrics → `to_push = max(0, target_buffer - unconsumed_total)`
     (capped by `max_per_tick`). Zero means user's feed is full; sleep.
  2. For each topic with a gap, scan the canonical queue order and pick the
     first media-ready items.
  3. **Render phase** (parallel, `render_workers` threads): for items with
     `rendered_card is None`, call `producer.render`. Items already cached
     skip this. Mutates `QueueItem.rendered_card` in place.
  4. **Checkpoint A**: update rendered cards through the queue transaction so
     any newly cached `rendered_card` is durable without overwriting newer
     supply-side queue additions.
  5. **Push phase** (sequential): for items that have a `rendered_card`,
     call `ticlawk.push_card`. On success, append `HistoryEntry` to
     `ledgers/history.jsonl` and remove the item from queue. On failure,
     the item stays (with cache).
  6. **Checkpoint B**: remove successful pushes through the queue transaction.
  7. Remove local mp4 cache files for successfully pushed YouTube/TikTok
     videos that no longer remain active in queue. Ticlawk remote cleanup
     stays in `cleanup_assets`.

The whole render+push phase is bounded by `tick_budget_seconds` (default
30s). Unfinished work stays in queue and will be picked again next tick
since rank_score hasn't changed.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from openfeed.utils.config_files import load_env

from openfeed.card_producers.base import CardPayload, RenderContext, get_producer
from openfeed.card_producers.ticlawk.thumbnails import ensure_thumbnail
from openfeed.clients.consumer import get_consumer, ticlawk
from openfeed.clients.llm import GeminiRunner
from openfeed.models.history import HistoryEntry
from openfeed.models.image_assets import ImageAssetIndex
from openfeed.models.image_cache import ImageCacheIndex
from openfeed.models.interests import load_interests
from openfeed.models.persona import load_persona
from openfeed.models.queue import Queue, QueueItem
from openfeed.models.runtime import PushConfig, load_runtime
from openfeed.models.video_assets import VideoAssetIndex
from openfeed.models.video_cache import VideoCacheIndex
from openfeed.utils import backpressure
from openfeed.utils import cycle_summary
from openfeed.utils.logging_setup import configure_task_logging
from openfeed.utils.media_readiness import queue_item_media_readiness
from openfeed.utils.queue_io import load_queue, mutate_queue
from openfeed.utils.state_io import atomic_write_json


logger = logging.getLogger("push")

_HISTORY_PATH = Path("ledgers/history.jsonl")
_ASSET_INDEX_PATH = Path("state/video_assets.json")
_IMAGE_ASSET_INDEX_PATH = Path("state/image_assets.json")
_CACHE_INDEX_PATH = Path("state/video_cache_index.json")
_IMAGE_CACHE_INDEX_PATH = Path("state/image_cache_index.json")
_DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 15 * 60


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    configure_task_logging("push")


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_queue() -> Queue:
    return load_queue()


def _queue_item_by_id(queue: Queue, content_id: str) -> QueueItem | None:
    for items in queue.topics.values():
        for item in items:
            if item.content.content_id == content_id:
                return item
    return None


def _apply_rendered_cards(
    picks: list[QueueItem],
    *,
    cleared_ids: set[str],
    topic_by,
    runtime,
) -> int:
    rendered = {
        pick.content.content_id: pick.rendered_card
        for pick in picks
    }

    def apply(latest: Queue) -> int:
        updated = 0
        for content_id, payload in rendered.items():
            item = _queue_item_by_id(latest, content_id)
            if item is None:
                continue
            if item.rendered_card != payload:
                item.rendered_card = payload
                updated += 1
        for content_id in cleared_ids:
            item = _queue_item_by_id(latest, content_id)
            if item is None:
                continue
            if item.rendered_card is not None:
                item.rendered_card = None
                updated += 1
        return updated

    return mutate_queue(apply, topic_by_name=topic_by, runtime=runtime)


def _finalize_pushed_queue(
    picks: list[QueueItem],
    *,
    pushed_ids: set[str],
    topic_by,
    runtime,
) -> Queue:
    rendered = {
        pick.content.content_id: pick.rendered_card
        for pick in picks
        if pick.content.content_id not in pushed_ids
    }

    def apply(latest: Queue) -> Queue:
        for content_id, payload in rendered.items():
            item = _queue_item_by_id(latest, content_id)
            if item is not None:
                item.rendered_card = payload
        if pushed_ids:
            for topic, items in latest.topics.items():
                latest.topics[topic] = [
                    item for item in items
                    if item.content.content_id not in pushed_ids
                ]
        return latest.model_copy(deep=True)

    return mutate_queue(apply, topic_by_name=topic_by, runtime=runtime)


def _load_video_cache_index() -> VideoCacheIndex:
    if not _CACHE_INDEX_PATH.exists():
        return VideoCacheIndex(generated_at=_utc_now_iso(), videos={})
    return VideoCacheIndex.model_validate(
        json.loads(_CACHE_INDEX_PATH.read_text(encoding="utf-8"))
    )


def _load_image_cache_index() -> ImageCacheIndex:
    if not _IMAGE_CACHE_INDEX_PATH.exists():
        return ImageCacheIndex(generated_at=_utc_now_iso(), images={})
    return ImageCacheIndex.model_validate(
        json.loads(_IMAGE_CACHE_INDEX_PATH.read_text(encoding="utf-8"))
    )


def _append_history(entry: HistoryEntry) -> None:
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(entry.model_dump_json() + "\n")


def _unconsumed_from_metrics(metrics: dict) -> int | None:
    """Consumer metrics → unread buffer count.

    `unconsumed_total` is OpenFeed's canonical field. Ticlawk's public API has
    also exposed `cards_unread`; accept that alias, but fail closed when neither
    exists so a schema drift cannot be interpreted as "0 unread" and spam a
    channel.
    """
    for name in ("unconsumed_total", "cards_unread"):
        if name not in metrics or metrics.get(name) is None:
            continue
        try:
            return max(0, int(metrics[name]))
        except (TypeError, ValueError):
            return None
    return None


def _pick_for_topic(
    items: list[QueueItem],
    n: int,
) -> list[QueueItem]:
    """Pick up to `n` items from the canonical queue order."""
    if n <= 0:
        return []
    return items[:n]


def _queue_video_cache_ids(queue: Queue) -> set[str]:
    ids: set[str] = set()
    for items in queue.topics.values():
        for qi in items:
            if qi.content.platform == "youtube":
                ids.add(qi.content.content_id)
            elif (
                qi.content.platform == "tiktok"
                and qi.content.tiktok is not None
                and qi.content.tiktok.media_kind == "video"
            ):
                ids.add(qi.content.content_id)
    return ids


def _pushed_video_cache_id(item: QueueItem) -> str | None:
    payload = item.rendered_card
    if payload is None or payload.content_subtype != "video":
        return None
    if item.content.platform == "youtube":
        return item.content.content_id
    if (
        item.content.platform == "tiktok"
        and item.content.tiktok is not None
        and item.content.tiktok.media_kind == "video"
    ):
        return item.content.content_id
    return None


def _drop_pushed_local_video_cache(video_ids: list[str], queue: Queue) -> None:
    """Delete local mp4 cache for pushed videos no longer active in queue.

    This intentionally leaves Ticlawk asset/card indexes alone; remote
    lifecycle cleanup remains owned by cleanup_assets.
    """
    if not video_ids or not _CACHE_INDEX_PATH.exists():
        return
    active_ids = _queue_video_cache_ids(queue)
    targets = sorted({vid for vid in video_ids if vid not in active_ids})
    if not targets:
        return
    try:
        idx = VideoCacheIndex.model_validate(
            json.loads(_CACHE_INDEX_PATH.read_text(encoding="utf-8"))
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("video_cache_index malformed; skip pushed local cleanup: %s", exc)
        return

    changed = False
    freed = 0
    dropped = 0
    for video_id in targets:
        entry = idx.videos.get(video_id)
        if entry is None:
            continue
        if entry.local_path:
            fp = Path(entry.local_path)
            if fp.exists():
                try:
                    size = fp.stat().st_size
                    fp.unlink()
                    freed += size
                except OSError as exc:
                    logger.warning("local video cache unlink failed for %s: %s", video_id, exc)
                    continue
        idx.videos.pop(video_id, None)
        changed = True
        dropped += 1

    if changed:
        idx.generated_at = _utc_now_iso()
        atomic_write_json(_CACHE_INDEX_PATH, idx.model_dump())
        logger.info(
            "dropped %d pushed local video cache entries (%.1f MB)",
            dropped,
            freed / 1e6,
        )


# ---------------------------------------------------------------------------
# Render phase
# ---------------------------------------------------------------------------


def _render_one(item: QueueItem, ctx: RenderContext, producer) -> CardPayload | None:
    """Single-item render. Returns CardPayload on success, None on any failure
    (renderer returned None, or raised). Failures are logged at warning."""
    try:
        return producer.render(item.content, ctx)
    except ticlawk.TiclawkQuotaExceeded as exc:
        backpressure.block_lane(
            backpressure.TICLAWK_VIDEO_UPLOAD,
            reason="quota_exceeded",
            detail=str(exc),
        )
        logger.warning("render blocked video upload lane for %s: %s",
                       item.content.content_id, exc)
        return None
    except ticlawk.TiclawkRateLimited as exc:
        backpressure.block_lane(
            backpressure.TICLAWK_VIDEO_UPLOAD,
            reason="rate_limited",
            detail=str(exc),
            retry_after=exc.retry_after,
            cooldown_seconds=None if exc.retry_after else _DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
        )
        logger.warning("render rate-limited video upload lane for %s: %s",
                       item.content.content_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — producer may raise anything
        logger.warning("render failed for %s: %s", item.content.content_id, exc)
        return None


def _render_phase(
    picks: list[QueueItem], producer, runner: GeminiRunner, persona,
    topic_by, *, workers: int, deadline: float,
) -> int:
    """Render any picks with `rendered_card is None`. Mutates the QueueItem
    list in place. Returns number of successful renders."""
    def render_context(pick: QueueItem) -> RenderContext:
        return RenderContext(
            runner=runner,
            persona=persona,
            topic_data=topic_by.get(pick.content.topic),
        )

    def needs_render(pick: QueueItem) -> bool:
        if pick.rendered_card is None:
            return True
        render_fingerprint = getattr(producer, "render_fingerprint", None)
        if render_fingerprint is None:
            return False
        expected = render_fingerprint(pick.content, render_context(pick))
        return bool(expected and pick.rendered_card.render_fingerprint != expected)

    need_render = [p for p in picks if needs_render(p)]
    if not need_render:
        return 0
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(
                _render_one, p,
                render_context(p),
                producer,
            ): p
            for p in need_render
        }
        remaining = max(0.0, deadline - time.monotonic())
        done, not_done = wait(futures, timeout=remaining, return_when=ALL_COMPLETED)
        for fut in not_done:
            fut.cancel()
        ok = 0
        for fut, pick in futures.items():
            if fut not in done:
                logger.info("render timed out: %s", pick.content.content_id)
                continue
            try:
                payload = fut.result()
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning("render result error %s: %s", pick.content.content_id, exc)
                continue
            if payload is not None:
                pick.rendered_card = payload
                ok += 1
        return ok
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# Push phase
# ---------------------------------------------------------------------------


def _invalidate_video_asset(video_id: str) -> None:
    """Drop the asset_index entry for `video_id` so the next push tick
    re-uploads. Used when ticlawk reports the asset is gone (404) or never
    finished its byte upload (409)."""
    if not _ASSET_INDEX_PATH.exists():
        return
    try:
        idx = VideoAssetIndex.model_validate(
            json.loads(_ASSET_INDEX_PATH.read_text(encoding="utf-8"))
        )
    except Exception:  # noqa: BLE001
        return
    if video_id in idx.assets:
        idx.assets.pop(video_id, None)
        idx.generated_at = _utc_now_iso()
        atomic_write_json(_ASSET_INDEX_PATH, idx.model_dump())
        logger.info("invalidated stale video_asset for %s — will re-upload", video_id)


def _invalidate_image_assets(content_id: str) -> None:
    """Drop uploaded image asset refs for a gallery card so render can re-upload."""
    if not _IMAGE_ASSET_INDEX_PATH.exists():
        return
    try:
        idx = ImageAssetIndex.model_validate(
            json.loads(_IMAGE_ASSET_INDEX_PATH.read_text(encoding="utf-8"))
        )
    except Exception:  # noqa: BLE001
        return
    if content_id in idx.assets:
        idx.assets.pop(content_id, None)
        idx.generated_at = _utc_now_iso()
        atomic_write_json(_IMAGE_ASSET_INDEX_PATH, idx.model_dump())
        logger.info("invalidated stale image_assets for %s — will re-upload", content_id)


def _mark_video_permanently_failed(video_id: str, reason: str) -> None:
    """Mark a video as `permanently_failed` in the cache index so prepare_video
    stops trying it. Used for hard validation errors (file too big, wrong
    content-type) that won't fix themselves."""
    if not _CACHE_INDEX_PATH.exists():
        return
    try:
        idx = VideoCacheIndex.model_validate(
            json.loads(_CACHE_INDEX_PATH.read_text(encoding="utf-8"))
        )
    except Exception:  # noqa: BLE001
        return
    entry = idx.videos.get(video_id)
    if entry is None:
        return
    entry.state = "permanently_failed"
    entry.last_error = f"ticlawk_rejected: {reason}"
    idx.generated_at = _utc_now_iso()
    atomic_write_json(_CACHE_INDEX_PATH, idx.model_dump())
    logger.warning("marked %s permanently_failed: %s", video_id, reason)


def _push_one(pick: QueueItem, spec, consumer_config) -> dict | None:
    """Send one card via the topic's consumer. Returns the API record on
    success, None on failure (logged). Handles ticlawk's video-asset error
    codes by invalidating or permanently-failing the underlying asset
    where appropriate.

    `spec` and `consumer_config` come from the registry — see
    `clients/consumer/__init__.py`. Ticlawk has typed recoverable errors;
    other consumers fall through to the generic exception handler and leave
    the queue item in place."""
    payload = pick.rendered_card
    assert payload is not None
    payload = ensure_thumbnail(pick.content, payload)
    if payload is None:
        logger.warning("skip push without thumbnail: %s", pick.content.content_id)
        return None
    pick.rendered_card = payload
    try:
        return spec.push_card(
            consumer_config,
            title=payload.title,
            content_subtype=payload.content_subtype,
            html=payload.html,
            video_id=payload.video_id,
            video_asset_id=payload.video_asset_id,
            image_asset_ids=payload.image_asset_ids,
            video_path=payload.video_path,
            image_paths=payload.image_paths,
            thumbnail_path=payload.thumbnail_path,
        )
    except (ticlawk.TiclawkAssetNotFound, ticlawk.TiclawkUploadIncomplete) as exc:
        # Asset is gone or never received bytes — drop our local record so
        # next render does a fresh upload. Card stays in queue.
        if payload.content_subtype == "gallery":
            _invalidate_image_assets(pick.content.content_id)
            logger.warning("ticlawk image asset stale for %s (%s) — re-upload next tick",
                           pick.content.content_id, exc)
        else:
            _invalidate_video_asset(pick.content.content_id)
            logger.warning("ticlawk video asset stale for %s (%s) — re-upload next tick",
                           pick.content.content_id, exc)
        return None
    except (
        ticlawk.TiclawkAssetTooBig,
        ticlawk.TiclawkBadAssetType,
        ticlawk.TiclawkWorkerResourceLimit,
    ) as exc:
        # Hard rejection — fix-yourself client bug. Drop the asset entry so
        # we don't ship the bad ref again; video cache can be marked permanent.
        if payload.content_subtype == "gallery":
            _invalidate_image_assets(pick.content.content_id)
            logger.warning("ticlawk rejected gallery asset for %s: %s",
                           pick.content.content_id, exc)
        else:
            _mark_video_permanently_failed(pick.content.content_id, str(exc))
            _invalidate_video_asset(pick.content.content_id)
        return None
    except ticlawk.TiclawkQuotaExceeded as exc:
        backpressure.block_lane(
            backpressure.TICLAWK_VIDEO_UPLOAD,
            reason="quota_exceeded",
            detail=str(exc),
        )
        logger.warning("ticlawk quota exceeded for %s — needs cleanup",
                       pick.content.content_id)
        return None
    except ticlawk.TiclawkRateLimited as exc:
        backpressure.block_lane(
            backpressure.TICLAWK_VIDEO_UPLOAD,
            reason="rate_limited",
            detail=str(exc),
            retry_after=exc.retry_after,
            cooldown_seconds=None if exc.retry_after else _DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
        )
        logger.warning("ticlawk video upload rate-limited for %s: %s",
                       pick.content.content_id, exc)
        return None
    except ticlawk.TiclawkError as exc:
        logger.warning("ticlawk push failed for %s: %s", pick.content.content_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — consumer adapters may raise anything
        logger.warning("consumer push failed for %s: %s", pick.content.content_id, exc)
        return None


def _record_ticlawk_api_backpressure(exc: Exception, *, operation: str) -> bool:
    if isinstance(exc, ticlawk.TiclawkRateLimited):
        backpressure.block_lane(
            backpressure.TICLAWK_API,
            reason="rate_limited",
            detail=f"{operation}: {exc}",
            retry_after=exc.retry_after,
            cooldown_seconds=None if exc.retry_after else _DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
        )
        return True
    if isinstance(exc, ticlawk.TiclawkAuthError):
        backpressure.block_lane(
            backpressure.TICLAWK_API,
            reason="auth_failed",
            detail=f"{operation}: {exc}",
        )
        return True
    if isinstance(exc, ticlawk.TiclawkError) and exc.status in (401, 403):
        backpressure.block_lane(
            backpressure.TICLAWK_API,
            reason="auth_or_forbidden",
            detail=f"{operation}: {exc}",
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openfeed-push")
    ap.add_argument("--max", type=int, default=None,
                    help="override max_per_tick for one-off runs")
    args = ap.parse_args(argv)

    _configure_logging()
    workdir = Path.cwd()
    load_env(workdir)

    runtime = load_runtime(workdir)
    cfg = runtime.push
    interests = load_interests(workdir)
    topic_by = {t.topic: t for t in interests.interests}

    # Resolve per-topic consumer + config from openfeed.yaml. Validation
    # already happened at load time (InterestEntry._validate_consumer);
    # here we just look up the registered spec and reify the typed config.
    spec_by_topic = {}
    consumer_config_by_topic = {}
    consumer_type_by_topic = {}
    for t in interests.interests:
        spec = get_consumer(t.consumer_type)
        spec_by_topic[t.topic] = spec
        consumer_config_by_topic[t.topic] = spec.config_model.model_validate(t.consumer_config)
        consumer_type_by_topic[t.topic] = t.consumer_type

    queue = _load_queue()
    if not any(queue.topics.values()):
        logger.warning("queue empty — nothing to push")
        return 0

    has_ticlawk_topic = any(v == "ticlawk" for v in consumer_type_by_topic.values())
    api_block = backpressure.active_block(backpressure.TICLAWK_API) if has_ticlawk_topic else None
    if api_block is not None:
        logger.warning(
            "ticlawk api backpressure active (%s): %s",
            api_block.get("reason"), api_block.get("detail", ""),
        )
        cycle_summary.add("push", pushed=0, blocked="ticlawk_api")
        return 0

    video_upload_block = (
        backpressure.active_block(backpressure.TICLAWK_VIDEO_UPLOAD)
        if has_ticlawk_topic else None
    )
    skip_youtube = video_upload_block is not None
    if skip_youtube:
        logger.warning(
            "ticlawk video upload backpressure active (%s): %s",
            video_upload_block.get("reason"), video_upload_block.get("detail", ""),
        )

    # ----- Phase 1: per-topic to_push budget by querying each channel -----
    # Iterate topics in a deterministic order; share the global max-per-tick
    # budget first-come-first-serve so a single chatty topic can't crowd
    # everyone else out, but neither can N topics multiply the budget.
    remaining_global = args.max if args.max is not None else cfg.max_per_tick
    to_push_by_topic: dict[str, int] = {}
    for topic in sorted(queue.topics.keys()):
        if remaining_global <= 0:
            break
        if not queue.topics[topic]:
            continue
        spec = spec_by_topic.get(topic)
        cc = consumer_config_by_topic.get(topic)
        if spec is None or cc is None:
            logger.warning("queue has topic %r but openfeed.yaml does not — skipping", topic)
            continue
        try:
            metrics = spec.get_metrics(cc)
        except Exception as exc:  # noqa: BLE001 — any consumer error
            blocked = _record_ticlawk_api_backpressure(exc, operation=f"get_metrics[{topic}]")
            logger.warning("get_metrics failed for [%s]: %s — skip topic this tick", topic, exc)
            if blocked:
                break
            continue
        unconsumed = _unconsumed_from_metrics(metrics)
        if unconsumed is None:
            logger.warning(
                "get_metrics for [%s] missing unread count "
                "(expected unconsumed_total or cards_unread): %s — skip topic",
                topic, metrics,
            )
            cycle_summary.add("push", skipped_metrics_missing=topic)
            continue
        gap = max(0, cfg.target_buffer - unconsumed)
        n = min(gap, remaining_global)
        if n <= 0:
            continue
        to_push_by_topic[topic] = n
        remaining_global -= n
        logger.info(
            "[%s] unconsumed=%d target=%d gap=%d → to_push=%d",
            topic, unconsumed, cfg.target_buffer, gap, n,
        )

    if not to_push_by_topic:
        cycle_summary.add("push", pushed=0)
        logger.info("no topic needs pushing this tick")
        return 0

    # ----- Phase 2: per-topic pick selection from canonical queue order -----
    video_cache_idx = _load_video_cache_index()
    image_cache_idx = _load_image_cache_index()
    picks_by_topic: dict[str, list[QueueItem]] = {}
    all_picks: list[QueueItem] = []
    media_state_dirty = False
    cleared_rendered_ids: set[str] = set()
    for topic, n in to_push_by_topic.items():
        topic_items: list[QueueItem] = []
        skipped_reasons: Counter[str] = Counter()
        stale_rendered = 0
        for qi in queue.topics[topic]:
            state = queue_item_media_readiness(qi, video_cache_idx, image_cache_idx)
            if state.stale_rendered_card:
                qi.rendered_card = None
                cleared_rendered_ids.add(qi.content.content_id)
                stale_rendered += 1
                media_state_dirty = True
            if state.ready:
                topic_items.append(qi)
            else:
                skipped_reasons[state.reason] += 1
        if skip_youtube and consumer_type_by_topic.get(topic) == "ticlawk":
            topic_items = [qi for qi in topic_items if qi.content.platform != "youtube"]
        if skipped_reasons or stale_rendered:
            logger.info(
                "[%s] media-ready candidates=%d/%d; skipped=%s; cleared_stale_rendered=%d",
                topic,
                len(topic_items),
                len(queue.topics[topic]),
                dict(skipped_reasons),
                stale_rendered,
            )
        topic_picks = _pick_for_topic(topic_items, n)
        if topic_picks:
            picks_by_topic[topic] = topic_picks
            all_picks.extend(topic_picks)

    if not all_picks:
        if media_state_dirty:
            _apply_rendered_cards(
                [],
                cleared_ids=cleared_rendered_ids,
                topic_by=topic_by,
                runtime=runtime,
            )
        logger.info("no eligible items across topics — stopping")
        return 0
    logger.info(
        "selected %d picks across %d topic(s)",
        len(all_picks), len(picks_by_topic),
    )

    # Lazy producer / runner / persona setup — only pay this cost when we
    # actually have something to render. (Cached items skip the LLM but
    # producer.render is still the call site.)
    persona = load_persona(workdir)
    runner = GeminiRunner(workdir)
    producer = get_producer(cfg.producer)

    deadline = time.monotonic() + cfg.tick_budget_seconds
    cached = sum(1 for p in all_picks if p.rendered_card is not None)
    logger.info("render phase: %d picks, %d cached, %d need render",
                len(all_picks), cached, len(all_picks) - cached)
    rendered_now = _render_phase(
        all_picks, producer, runner, persona, topic_by,
        workers=cfg.render_workers, deadline=deadline,
    )
    logger.info("render phase done: %d newly rendered", rendered_now)

    # ----- Checkpoint A: persist any newly cached rendered_card -----
    _apply_rendered_cards(
        all_picks,
        cleared_ids=cleared_rendered_ids,
        topic_by=topic_by,
        runtime=runtime,
    )

    # ----- Push phase: sequential per topic, each batch to its own channel -----
    pushed_ok = 0
    pushed_fail = 0
    skipped_unrendered = 0
    skipped_backpressure = 0
    aborted_remaining = 0
    pushed_video_cache_ids: list[str] = []
    pushed_ids: set[str] = set()
    for topic, topic_picks in picks_by_topic.items():
        spec = spec_by_topic[topic]
        cc = consumer_config_by_topic[topic]
        for pick in topic_picks:
            if time.monotonic() > deadline:
                aborted_remaining = sum(
                    1 for picks in picks_by_topic.values() for p in picks
                ) - (pushed_ok + pushed_fail + skipped_unrendered + skipped_backpressure)
                logger.info("tick budget exhausted; %d picks deferred", aborted_remaining)
                break
            if pick.rendered_card is None:
                skipped_unrendered += 1
                continue
            if (
                pick.content.platform == "youtube"
                and consumer_type_by_topic.get(topic) == "ticlawk"
                and backpressure.active_block(backpressure.TICLAWK_VIDEO_UPLOAD) is not None
            ):
                skipped_backpressure += 1
                continue
            record = _push_one(pick, spec, cc)
            if record is None:
                pushed_fail += 1
                continue
            payload = pick.rendered_card
            entry = HistoryEntry(
                card_id=str(record.get("id", "")),
                content_id=pick.content.content_id,
                source_id=pick.content.source_id,
                topic=pick.content.topic,
                platform=pick.content.platform,
                content_subtype=payload.content_subtype,
                title=payload.title,
                pushed_at=_utc_now_iso(),
                rank_score=pick.rank_score,
            )
            _append_history(entry)
            pushed_ids.add(pick.content.content_id)
            video_cache_id = _pushed_video_cache_id(pick)
            if video_cache_id is not None:
                pushed_video_cache_ids.append(video_cache_id)
            pushed_ok += 1
            logger.info("pushed [%s] %s / %s", pick.content.platform,
                        pick.content.topic, payload.title[:60])
        if time.monotonic() > deadline:
            break

    # ----- Checkpoint B: persist queue with successful pushes removed -----
    final_queue = _finalize_pushed_queue(
        all_picks,
        pushed_ids=pushed_ids,
        topic_by=topic_by,
        runtime=runtime,
    )
    _drop_pushed_local_video_cache(pushed_video_cache_ids, final_queue)
    queue_size = sum(len(v) for v in final_queue.topics.values())
    logger.info(
        "push tick: %d ok / %d push-failed / %d unrendered / %d backpressure-skipped (queue now %d)",
        pushed_ok, pushed_fail, skipped_unrendered, skipped_backpressure, queue_size,
    )
    cycle_summary.add(
        "push",
        pushed=pushed_ok,
        push_failed=pushed_fail,
        unrendered=skipped_unrendered,
        skipped_backpressure=skipped_backpressure,
        queue_size=queue_size,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
