"""Helpers for reading/mutating state/queue.json.

Today this exists mainly for the post-retire queue prune: when learn or
filter retires a source, any queue items still referencing it should be
dropped so push doesn't keep flushing them to the user.

Filter's own admit-write path still uses its inline _save_queue (it
holds the queue in memory across a tick); these helpers are for the
"load → prune → save" pattern that learn + filter's retire path need.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from openfeed.models.queue import Queue
from openfeed.utils.state_io import atomic_write_json


QUEUE_PATH = Path("state/queue.json")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_queue() -> Queue:
    """Read state/queue.json. Returns an empty Queue if the file is missing."""
    if not QUEUE_PATH.exists():
        return Queue(generated_at=_utc_now_iso(), topics={})
    raw = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    return Queue.model_validate(raw)


def save_queue(queue: Queue) -> None:
    queue.generated_at = _utc_now_iso()
    atomic_write_json(QUEUE_PATH, queue.model_dump())


def prune_for_retired_sources(retired_source_keys: list[str]) -> int:
    """Drop any queue items whose source matches a retired source. Loads,
    prunes, atomically writes back. Returns count of items removed.

    `retired_source_keys` use the per-topic catalog_key shape
    `<platform>:<source_id>:<topic>`. Queue items carry the three pieces
    separately on the embedded ContentItem; we recombine to compare.
    """
    if not retired_source_keys:
        return 0
    retired = set(retired_source_keys)
    queue = load_queue()
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
    if removed:
        save_queue(queue)
    return removed
