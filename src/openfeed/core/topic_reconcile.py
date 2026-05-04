"""Reconcile topic config changes with derived production state.

`openfeed.yaml` is the user's source of truth. Most runtime state is
derived from each topic's semantic config, so a topic description/platform/etc.
change must invalidate the old per-topic source/search/queue state before the
supply cycle tries to refill it again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openfeed.models.interests import InterestEntry, load_interests
from openfeed.models.queue import Queue
from openfeed.utils import catalog_io
from openfeed.utils.state_io import atomic_write_json


logger = logging.getLogger("topic_reconcile")

_MANIFEST_PATH = Path("state/topic_manifest.json")
_ARCHIVE_DIR = Path("state/topic_archive")
_QUEUE_PATH = Path("state/queue.json")
_QUEUE_STATUS_PATH = Path("state/queue_status.json")
_LEARN_STATE_PATH = Path("state/learn_state.json")
_SEARCH_TERMS_PATH = Path("state/search_terms.json")
_PATROL_DIR = Path("queues/patrol")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _topic_semantic_payload(entry: InterestEntry) -> dict[str, Any]:
    """Fields whose change makes old derived content decisions stale.

    Consumer routing is intentionally excluded: changing a Ticlawk channel does
    not change which sources/content belong in the topic.
    """
    return {
        "topic": entry.topic,
        "description": entry.description,
        "platforms": list(entry.platforms),
        "language_preferences": list(entry.language_preferences),
        "max_content_age_days": entry.max_content_age_days,
        "freshness_half_life_days": entry.freshness_half_life_days,
        "youtube_duration_max_seconds": entry.youtube_duration_max_seconds,
    }


def topic_fingerprint(entry: InterestEntry) -> str:
    data = json.dumps(_topic_semantic_payload(entry), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _manifest_for(config_topics: dict[str, InterestEntry], now: str) -> dict[str, Any]:
    return {
        "generated_at": now,
        "topics": {
            topic: {
                "fingerprint": topic_fingerprint(entry),
                "semantic_config": _topic_semantic_payload(entry),
            }
            for topic, entry in sorted(config_topics.items())
        },
    }


def _load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("topic_reconcile: ignoring unreadable %s: %s", path, exc)
        return None


def _archive_write(archive_dir: Path, relative_name: str, payload: Any) -> None:
    target = archive_dir / relative_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _remove_topic_from_items_file(path: Path, topic: str, archive_dir: Path) -> int:
    raw = _load_json(path)
    if not isinstance(raw, dict):
        return 0
    items = raw.get("items")
    if not isinstance(items, list):
        return 0
    removed = [item for item in items if isinstance(item, dict) and item.get("topic") == topic]
    if not removed:
        return 0
    raw["items"] = [item for item in items if not (isinstance(item, dict) and item.get("topic") == topic)]
    _archive_write(archive_dir, path.name, {"items": removed})
    atomic_write_json(path, raw)
    return len(removed)


def _remove_search_terms(topic: str, archive_dir: Path) -> bool:
    raw = _load_json(_SEARCH_TERMS_PATH)
    if not isinstance(raw, dict):
        return False
    topics = raw.get("topics")
    if not isinstance(topics, dict) or topic not in topics:
        return False
    _archive_write(archive_dir, "search_terms.json", {topic: topics[topic]})
    del topics[topic]
    raw["generated_at"] = _utc_now_iso()
    atomic_write_json(_SEARCH_TERMS_PATH, raw)
    return True


def _remove_queue_topic(topic: str, archive_dir: Path) -> int:
    raw = _load_json(_QUEUE_PATH)
    if not isinstance(raw, dict):
        return 0
    queue = Queue.model_validate(raw)
    if topic not in queue.topics:
        return 0
    removed = queue.topics.pop(topic)
    _archive_write(
        archive_dir,
        "queue_items.json",
        [item.model_dump() for item in removed],
    )
    queue.generated_at = _utc_now_iso()
    atomic_write_json(_QUEUE_PATH, queue.model_dump())
    return len(removed)


def _remove_queue_status_topic(topic: str, archive_dir: Path) -> bool:
    raw = _load_json(_QUEUE_STATUS_PATH)
    if not isinstance(raw, dict):
        return False
    changed = False
    per_topic = raw.get("per_topic")
    if isinstance(per_topic, dict) and topic in per_topic:
        _archive_write(archive_dir, "queue_status.json", {topic: per_topic[topic]})
        del per_topic[topic]
        changed = True
    refill_topics = raw.get("refill_topics")
    if isinstance(refill_topics, list) and topic in refill_topics:
        raw["refill_topics"] = [item for item in refill_topics if item != topic]
        changed = True
    if changed:
        raw["generated_at"] = _utc_now_iso()
        atomic_write_json(_QUEUE_STATUS_PATH, raw)
    return changed


def _remove_learn_topic(topic: str, *, keep_boundary: bool, changed_at: str) -> bool:
    raw = _load_json(_LEARN_STATE_PATH)
    if not isinstance(raw, dict):
        return False
    block = raw.get("keyword_proposal_last_at")
    if not isinstance(block, dict):
        return False
    if keep_boundary:
        block[topic] = changed_at
        atomic_write_json(_LEARN_STATE_PATH, raw)
        return True
    if topic in block:
        del block[topic]
        atomic_write_json(_LEARN_STATE_PATH, raw)
        return True
    return False


def _remove_patrol_items(topic: str, archive_dir: Path) -> int:
    if not _PATROL_DIR.exists():
        return 0
    removed = 0
    patrol_archive = archive_dir / "patrol"
    patrol_archive.mkdir(parents=True, exist_ok=True)
    for path in sorted(_PATROL_DIR.glob("*.json")):
        raw = _load_json(path)
        if not isinstance(raw, dict) or raw.get("topic") != topic:
            continue
        path.replace(patrol_archive / path.name)
        removed += 1
    return removed


@dataclass
class TopicResetSummary:
    topic: str
    reason: str
    archived_to: str
    catalog_removed: bool = False
    search_terms_removed: bool = False
    queue_items_removed: int = 0
    queue_status_removed: bool = False
    patrol_items_removed: int = 0
    seed_sources_removed: int = 0
    youtube_keywords_removed: int = 0
    youtube_candidates_removed: int = 0
    learn_boundary_updated: bool = False


@dataclass
class ReconcileResult:
    initialized_manifest: bool = False
    added_topics: list[str] = field(default_factory=list)
    changed_topics: list[str] = field(default_factory=list)
    removed_topics: list[str] = field(default_factory=list)
    forced_topics: list[str] = field(default_factory=list)
    resets: list[TopicResetSummary] = field(default_factory=list)


def _reset_topic_state(workdir: Path, topic: str, *, reason: str, keep_learn_boundary: bool, now: str) -> TopicResetSummary:
    state_dir = workdir / "state"
    archive_dir = workdir / _ARCHIVE_DIR / now.replace(":", "").replace("+", "Z") / topic
    archive_dir.mkdir(parents=True, exist_ok=True)
    summary = TopicResetSummary(topic=topic, reason=reason, archived_to=str(archive_dir))

    summary.catalog_removed = catalog_io.archive_topic(
        state_dir, topic, archive_dir / "source_catalog.json",
    )
    summary.search_terms_removed = _remove_search_terms(topic, archive_dir)
    summary.queue_items_removed = _remove_queue_topic(topic, archive_dir)
    summary.queue_status_removed = _remove_queue_status_topic(topic, archive_dir)
    summary.patrol_items_removed = _remove_patrol_items(topic, archive_dir)
    summary.seed_sources_removed = _remove_topic_from_items_file(state_dir / "seed_sources.json", topic, archive_dir)
    summary.youtube_keywords_removed = _remove_topic_from_items_file(
        state_dir / "youtube_channel_keywords.json", topic, archive_dir,
    )
    summary.youtube_candidates_removed = _remove_topic_from_items_file(
        state_dir / "youtube_candidates.json", topic, archive_dir,
    )
    summary.learn_boundary_updated = _remove_learn_topic(
        topic, keep_boundary=keep_learn_boundary, changed_at=now,
    )
    return summary


def reconcile_topics(workdir: Path, *, force_topics: set[str] | None = None) -> ReconcileResult:
    now = _utc_now_iso()
    config = load_interests(workdir)
    config_topics = {entry.topic: entry for entry in config.interests}
    current_manifest = _manifest_for(config_topics, now)
    manifest_path = workdir / _MANIFEST_PATH
    force_topics = set(force_topics or set())

    previous = _load_json(manifest_path)
    if not isinstance(previous, dict) or not isinstance(previous.get("topics"), dict):
        if not force_topics:
            atomic_write_json(manifest_path, current_manifest)
            logger.info("topic_reconcile: initialized manifest for %d topic(s)", len(config_topics))
            return ReconcileResult(initialized_manifest=True)
        previous = {"topics": {}}

    previous_topics = previous.get("topics") or {}
    previous_names = set(previous_topics)
    current_names = set(config_topics)
    added = sorted(current_names - previous_names)
    removed = sorted(previous_names - current_names)
    changed = sorted(
        topic
        for topic in current_names & previous_names
        if (previous_topics.get(topic) or {}).get("fingerprint")
        != current_manifest["topics"][topic]["fingerprint"]
    )
    forced = sorted(topic for topic in force_topics if topic in current_names and topic not in changed)

    result = ReconcileResult(
        added_topics=added,
        changed_topics=changed,
        removed_topics=removed,
        forced_topics=forced,
    )

    for topic in removed:
        result.resets.append(
            _reset_topic_state(workdir, topic, reason="removed_from_config", keep_learn_boundary=False, now=now)
        )
    for topic in changed:
        result.resets.append(
            _reset_topic_state(workdir, topic, reason="semantic_config_changed", keep_learn_boundary=True, now=now)
        )
    for topic in forced:
        result.resets.append(
            _reset_topic_state(workdir, topic, reason="forced", keep_learn_boundary=True, now=now)
        )

    atomic_write_json(manifest_path, current_manifest)
    if added or changed or removed or forced:
        logger.info(
            "topic_reconcile: added=%s changed=%s removed=%s forced=%s resets=%d",
            added or "—", changed or "—", removed or "—", forced or "—", len(result.resets),
        )
        for reset in result.resets:
            logger.info("topic_reconcile: reset %s (%s) → %s", reset.topic, reset.reason, reset.archived_to)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openfeed-topic-reconcile")
    parser.add_argument(
        "--force-topic",
        action="append",
        default=[],
        help="Force-reset one configured topic even if its manifest fingerprint did not change.",
    )
    args = parser.parse_args(argv)
    reconcile_topics(Path.cwd(), force_topics=set(args.force_topic))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
