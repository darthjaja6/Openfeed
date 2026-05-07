"""Queue management — per-topic inventory signalling (PRD §5.4).

Idempotent post-pass over `state/queue.json`:

  1. Expire items whose age in queue has passed the topic's
     `max_content_age_days` (they slipped by filter but have aged out).
     Emit a `reject_content / expired_in_queue` judgment so the filter
     blocklist catches them if they ever re-appear in a patrol batch.
  2. Sort each topic bucket by `rank_score` descending (push reads top-K).
  3. Compute per-topic inventory + `refill_gap = max(0, target - inventory)`
     where `target = max(topic_floor, topic_capacity)`.
  4. Emit `state/queue_status.json`:
       - `refill_topics`: positive-gap topics, ordered gap-desc — patrol
         consults this to prioritise which (topic, platform) to refresh

This module does **not** do selection / diversity / per-user capping — that
is push's job (§5.5).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from openfeed.core.judgment_ledger import attach_file as _attach_ledger, emit_judgment
from openfeed.models.image_cache import ImageCacheIndex
from openfeed.models.interests import InterestEntry, load_interests
from openfeed.models.queue import Queue, QueueItem, QueueStatus, TopicStatus
from openfeed.models.runtime import RuntimeConfig, load_runtime
from openfeed.models.video_cache import VideoCacheIndex
from openfeed.utils.content_meta import yt_ago_to_days
from openfeed.utils.logging_setup import configure_task_logging
from openfeed.utils.media_readiness import queue_item_media_readiness
from openfeed.utils.state_io import atomic_write_json


logger = logging.getLogger("queue_manage")

_QUEUE_JSON = Path("state/queue.json")
_QUEUE_STATUS_JSON = Path("state/queue_status.json")
_VIDEO_CACHE_INDEX_JSON = Path("state/video_cache_index.json")
_IMAGE_CACHE_INDEX_JSON = Path("state/image_cache_index.json")
_LEDGER_PATH = Path("ledgers/decisions.jsonl")


def _configure_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler)
               and getattr(h, "stream", None) is sys.stdout
               for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(sh)
    configure_task_logging("queue_manage")
    _attach_ledger(_LEDGER_PATH)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _queue_item_age_days(qi: QueueItem) -> float | None:
    """Recompute current age of the content (not of the admit timestamp).

    Uses the source's published timestamp, since that's what filter's
    freshness gate cares about. Falls back to `admitted_at` only when the
    platform-published timestamp is unparseable."""
    item = qi.content
    now = datetime.now(timezone.utc)
    if item.platform == "youtube" and item.youtube is not None:
        age = yt_ago_to_days(item.youtube.published)
        if age is not None:
            # Add the wall-clock delta since admission so "2w ago" at admit
            # becomes "2w ago + days since admit" today.
            try:
                admitted = datetime.fromisoformat(qi.admitted_at)
                if admitted.tzinfo is None:
                    admitted = admitted.replace(tzinfo=timezone.utc)
                age += (now - admitted).total_seconds() / 86400.0
            except ValueError:
                pass
            return age
    if item.platform == "x" and item.x is not None and item.x.created_at:
        try:
            dt = parsedate_to_datetime(item.x.created_at)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return (now - dt).total_seconds() / 86400.0
        except Exception:  # noqa: BLE001
            pass
    if item.platform == "web" and item.web is not None and item.web.published_at:
        try:
            dt = datetime.fromisoformat(item.web.published_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (now - dt).total_seconds() / 86400.0
        except ValueError:
            pass
    # Fallback: days since admission
    try:
        admitted = datetime.fromisoformat(qi.admitted_at)
        if admitted.tzinfo is None:
            admitted = admitted.replace(tzinfo=timezone.utc)
        return (now - admitted).total_seconds() / 86400.0
    except ValueError:
        return None


def _effective_max_age(entry: InterestEntry | None, runtime: RuntimeConfig) -> int | None:
    """Max content age to apply. Topic field wins; else YouTube's platform
    fallback only applies per-platform inside filter — queue_manage uses
    topic-level semantics, so no platform fallback here. None = no gate."""
    if entry is not None and entry.max_content_age_days is not None:
        return entry.max_content_age_days
    return None


def _expire_stale(
    queue: Queue, topic_by_name: dict[str, InterestEntry], runtime: RuntimeConfig,
) -> int:
    """Drop queue items whose age exceeds topic's `max_content_age_days`.

    Emits `reject_content` events with `expired_in_queue` so they are
    permanently blocked from re-admission (filter consults the ledger)."""
    expired = 0
    for topic, items in list(queue.topics.items()):
        max_age = _effective_max_age(topic_by_name.get(topic), runtime)
        if max_age is None:
            continue
        kept: list[QueueItem] = []
        for qi in items:
            age = _queue_item_age_days(qi)
            if age is not None and age > max_age:
                _emit_expired(qi, age, max_age)
                expired += 1
            else:
                kept.append(qi)
        queue.topics[topic] = kept
    return expired


def _emit_expired(qi: QueueItem, age: float, max_age: int) -> None:
    item = qi.content
    name = (
        item.youtube.title if item.youtube
        else f"@{item.x.author}" if item.x
        else (item.web.title if item.web else item.content_id)
    )
    emit_judgment(
        event_type="reject_content",
        platform=item.platform,
        topic=item.topic,
        source_id=item.content_id,
        source_name=name,
        reason_code="expired_in_queue",
        evidence={
            "age_days": round(age, 2),
            "max_age_days": max_age,
            "admitted_at": qi.admitted_at,
        },
    )


def _sort_topics(queue: Queue) -> None:
    """In-place sort of each topic bucket by rank_score descending."""
    for topic, items in queue.topics.items():
        items.sort(key=lambda qi: qi.rank_score, reverse=True)
        queue.topics[topic] = items


def _load_video_cache_index() -> VideoCacheIndex:
    if not _VIDEO_CACHE_INDEX_JSON.exists():
        return VideoCacheIndex(generated_at=_utc_now_iso())
    try:
        return VideoCacheIndex.model_validate(
            json.loads(_VIDEO_CACHE_INDEX_JSON.read_text(encoding="utf-8"))
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("video_cache_index malformed; treating videos as not ready: %s", exc)
        return VideoCacheIndex(generated_at=_utc_now_iso())


def _load_image_cache_index() -> ImageCacheIndex:
    if not _IMAGE_CACHE_INDEX_JSON.exists():
        return ImageCacheIndex(generated_at=_utc_now_iso())
    try:
        return ImageCacheIndex.model_validate(
            json.loads(_IMAGE_CACHE_INDEX_JSON.read_text(encoding="utf-8"))
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_cache_index malformed; treating images as not ready: %s", exc)
        return ImageCacheIndex(generated_at=_utc_now_iso())


def _drop_permanently_failed_media(
    queue: Queue,
    video_idx: VideoCacheIndex,
    image_idx: ImageCacheIndex,
) -> int:
    removed = 0
    for topic, items in list(queue.topics.items()):
        kept: list[QueueItem] = []
        for qi in items:
            state = queue_item_media_readiness(qi, video_idx, image_idx)
            if state.permanent_failure:
                removed += 1
                logger.info(
                    "[%s] dropping permanently failed media item %s (%s)",
                    topic,
                    qi.content.content_id,
                    state.reason,
                )
                continue
            if state.stale_rendered_card:
                qi.rendered_card = None
            kept.append(qi)
        queue.topics[topic] = kept
    return removed


def _compute_status(
    queue: Queue, topic_by_name: dict[str, InterestEntry], runtime: RuntimeConfig,
) -> QueueStatus:
    qm = runtime.queue_manage
    target = max(qm.topic_floor, qm.topic_capacity)
    video_idx = _load_video_cache_index()
    image_idx = _load_image_cache_index()

    total_inventory = sum(len(v) for v in queue.topics.values())
    total_pushable_inventory = 0
    per_topic: dict[str, TopicStatus] = {}
    for topic in topic_by_name:
        items = queue.topics.get(topic, [])
        inv = len(items)
        pushable = sum(
            1 for qi in items
            if queue_item_media_readiness(qi, video_idx, image_idx).ready
        )
        total_pushable_inventory += pushable
        blocked = inv - pushable
        gap = max(0, target - pushable)
        per_topic[topic] = TopicStatus(
            inventory=inv,
            pushable_inventory=pushable,
            blocked_inventory=blocked,
            target=target,
            refill_gap=gap, floor=qm.topic_floor,
        )

    refill_topics = sorted(
        [t for t, s in per_topic.items() if s.refill_gap > 0],
        key=lambda t: per_topic[t].refill_gap, reverse=True,
    )

    return QueueStatus(
        generated_at=_utc_now_iso(),
        total_inventory=total_inventory,
        total_pushable_inventory=total_pushable_inventory,
        topic_capacity=qm.topic_capacity,
        per_topic=per_topic,
        refill_topics=refill_topics,
    )


def main(argv: list[str] | None = None) -> int:
    del argv
    _configure_logging()
    workdir = Path.cwd()

    if not _QUEUE_JSON.exists():
        logger.warning("no queue.json — nothing to manage; emitting empty status")
        config = load_interests(workdir)
        runtime = load_runtime(workdir)
        status = _compute_status(
            Queue(generated_at=_utc_now_iso(), topics={}),
            {t.topic: t for t in config.interests}, runtime,
        )
        atomic_write_json(_QUEUE_STATUS_JSON, status.model_dump())
        logger.info("queue_status emitted (empty queue)")
        return 0

    queue = Queue.model_validate(json.loads(_QUEUE_JSON.read_text(encoding="utf-8")))
    config = load_interests(workdir)
    runtime = load_runtime(workdir)
    topic_by_name = {t.topic: t for t in config.interests}

    before = sum(len(v) for v in queue.topics.values())
    logger.info("queue_manage start: %d items across %d topics", before, len(queue.topics))

    expired = _expire_stale(queue, topic_by_name, runtime)
    logger.info("expired %d stale items", expired)

    video_idx = _load_video_cache_index()
    image_idx = _load_image_cache_index()
    permanent_removed = _drop_permanently_failed_media(queue, video_idx, image_idx)
    logger.info("removed %d permanently failed media items", permanent_removed)

    _sort_topics(queue)
    queue.generated_at = _utc_now_iso()

    status = _compute_status(queue, topic_by_name, runtime)

    atomic_write_json(_QUEUE_JSON, queue.model_dump())
    atomic_write_json(_QUEUE_STATUS_JSON, status.model_dump())

    after = status.total_inventory
    logger.info(
        "queue_manage_ok: %d → %d items; refill_topics=%s",
        before, after, status.refill_topics,
    )
    for topic, s in sorted(status.per_topic.items(), key=lambda kv: -kv[1].refill_gap):
        logger.info(
            "  %-12s inv=%-3d pushable=%-3d blocked=%-3d target=%-3d gap=%-3d",
            topic,
            s.inventory,
            s.pushable_inventory,
            s.blocked_inventory,
            s.target,
            s.refill_gap,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
