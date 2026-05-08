"""Helpers for reading/mutating state/queue.json.

`state/queue.json` is shared by supply/filter/queue_manage and refill/push.
Every write must happen through the same transaction boundary:

    lock → read latest queue → mutate → write queue → write queue_status

That keeps queue_status a derived view of queue.json and prevents a long-running
task from writing back a stale queue snapshot over a newer one.
"""
from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

from openfeed.models.image_cache import ImageCacheIndex
from openfeed.models.interests import InterestEntry
from openfeed.models.queue import Queue, QueueStatus, TopicStatus
from openfeed.models.runtime import RuntimeConfig
from openfeed.models.video_cache import VideoCacheIndex
from openfeed.utils.media_readiness import queue_item_media_readiness
from openfeed.utils.state_io import atomic_write_json


QUEUE_PATH = Path("state/queue.json")
QUEUE_STATUS_PATH = Path("state/queue_status.json")
QUEUE_LOCK_PATH = Path("state/queue.lock")
VIDEO_CACHE_INDEX_PATH = Path("state/video_cache_index.json")
IMAGE_CACHE_INDEX_PATH = Path("state/image_cache_index.json")
T = TypeVar("T")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def queue_lock():
    QUEUE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE_LOCK_PATH.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_queue() -> Queue:
    """Read state/queue.json. Returns an empty Queue if the file is missing."""
    if not QUEUE_PATH.exists():
        return Queue(generated_at=_utc_now_iso(), topics={})
    raw = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    return Queue.model_validate(raw)


def _save_queue_unlocked(queue: Queue) -> None:
    queue.generated_at = _utc_now_iso()
    atomic_write_json(QUEUE_PATH, queue.model_dump())


def _load_video_cache_index() -> VideoCacheIndex:
    if not VIDEO_CACHE_INDEX_PATH.exists():
        return VideoCacheIndex(generated_at=_utc_now_iso())
    return VideoCacheIndex.model_validate(
        json.loads(VIDEO_CACHE_INDEX_PATH.read_text(encoding="utf-8"))
    )


def _load_image_cache_index() -> ImageCacheIndex:
    if not IMAGE_CACHE_INDEX_PATH.exists():
        return ImageCacheIndex(generated_at=_utc_now_iso())
    return ImageCacheIndex.model_validate(
        json.loads(IMAGE_CACHE_INDEX_PATH.read_text(encoding="utf-8"))
    )


def compute_queue_status(
    queue: Queue,
    topic_by_name: dict[str, InterestEntry],
    runtime: RuntimeConfig,
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
            refill_gap=gap,
            floor=qm.topic_floor,
        )

    refill_topics = sorted(
        [t for t, s in per_topic.items() if s.refill_gap > 0],
        key=lambda t: per_topic[t].refill_gap,
        reverse=True,
    )
    return QueueStatus(
        generated_at=_utc_now_iso(),
        total_inventory=total_inventory,
        total_pushable_inventory=total_pushable_inventory,
        topic_capacity=qm.topic_capacity,
        per_topic=per_topic,
        refill_topics=refill_topics,
    )


def _save_queue_status_unlocked(
    queue: Queue,
    topic_by_name: dict[str, InterestEntry],
    runtime: RuntimeConfig,
) -> QueueStatus:
    status = compute_queue_status(queue, topic_by_name, runtime)
    atomic_write_json(QUEUE_STATUS_PATH, status.model_dump())
    return status


def mutate_queue(
    mutator: Callable[[Queue], T],
    *,
    topic_by_name: dict[str, InterestEntry] | None = None,
    runtime: RuntimeConfig | None = None,
) -> T:
    """Mutate the latest queue under the queue lock.

    When `topic_by_name` and `runtime` are supplied, queue_status is refreshed
    in the same transaction.
    """
    if (topic_by_name is None) != (runtime is None):
        raise ValueError("topic_by_name and runtime must be supplied together")
    with queue_lock():
        queue = load_queue()
        result = mutator(queue)
        _save_queue_unlocked(queue)
        if topic_by_name is not None and runtime is not None:
            _save_queue_status_unlocked(queue, topic_by_name, runtime)
        return result


def prune_for_retired_sources(
    retired_source_keys: list[str],
    *,
    topic_by_name: dict[str, InterestEntry],
    runtime: RuntimeConfig,
) -> int:
    """Drop any queue items whose source matches a retired source. Loads,
    prunes, atomically writes back. Returns count of items removed.

    `retired_source_keys` use the per-topic catalog_key shape
    `<platform>:<source_id>:<topic>`. Queue items carry the three pieces
    separately on the embedded ContentItem; we recombine to compare.
    """
    if not retired_source_keys:
        return 0
    retired = set(retired_source_keys)
    def prune(queue: Queue) -> int:
        removed = 0
        for topic, items in list(queue.topics.items()):
            kept = []
            for it in items:
                key = f"{it.content.platform}:{it.content.source_id}:{it.content.topic}"
                if key in retired:
                    removed += 1
                    continue
                kept.append(it)
            queue.topics[topic] = kept
        return removed

    return mutate_queue(prune, topic_by_name=topic_by_name, runtime=runtime)
