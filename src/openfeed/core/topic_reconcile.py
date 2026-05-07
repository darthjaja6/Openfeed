"""Reconcile topic config changes with derived production state.

`openfeed.yaml` is the user's source of truth. Topic-level semantic changes
invalidate the whole topic. Platform-list changes are reconciled at the
(topic, platform) slot level so adding TikTok to an existing YouTube topic
does not wipe the YouTube catalog, queue, or history.
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
    """Topic-level fields whose change makes all platform slots stale.

    Consumer routing is intentionally excluded: changing a Ticlawk channel does
    not change which sources/content belong in the topic.
    """
    return {
        "topic": entry.topic,
        "description": entry.description,
        "language_preferences": list(entry.language_preferences),
        "max_content_age_days": entry.max_content_age_days,
        "freshness_half_life_days": entry.freshness_half_life_days,
    }


def _platform_semantic_payload(entry: InterestEntry, platform: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "topic": entry.topic,
        "platform": platform,
    }
    if platform == "youtube":
        payload["youtube_duration_max_seconds"] = entry.youtube_duration_max_seconds
    return payload


def _topic_semantic_payload_from_raw(topic: str, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "topic": topic,
        "description": raw.get("description"),
        "language_preferences": list(raw.get("language_preferences") or []),
        "max_content_age_days": raw.get("max_content_age_days"),
        "freshness_half_life_days": raw.get("freshness_half_life_days"),
    }


def _platform_semantic_payload_from_raw(
    topic: str,
    platform: str,
    raw: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "topic": topic,
        "platform": platform,
    }
    if platform == "youtube":
        payload["youtube_duration_max_seconds"] = raw.get("youtube_duration_max_seconds")
    return payload


def topic_fingerprint(entry: InterestEntry) -> str:
    data = json.dumps(_topic_semantic_payload(entry), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def platform_fingerprint(entry: InterestEntry, platform: str) -> str:
    data = json.dumps(
        _platform_semantic_payload(entry, platform),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _manifest_for(config_topics: dict[str, InterestEntry], now: str) -> dict[str, Any]:
    return {
        "generated_at": now,
        "topics": {
            topic: {
                "topic_fingerprint": topic_fingerprint(entry),
                "topic_config": _topic_semantic_payload(entry),
                "platforms": {
                    platform: {
                        "fingerprint": platform_fingerprint(entry, platform),
                        "semantic_config": _platform_semantic_payload(entry, platform),
                    }
                    for platform in sorted(entry.platforms)
                },
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


def _fingerprint_payload(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _previous_topic_fingerprint(topic: str, entry: dict[str, Any]) -> str | None:
    value = entry.get("topic_fingerprint")
    if isinstance(value, str):
        return value
    raw = entry.get("semantic_config")
    if isinstance(raw, dict):
        return _fingerprint_payload(_topic_semantic_payload_from_raw(topic, raw))
    return None


def _previous_platform_fingerprints(topic: str, entry: dict[str, Any]) -> dict[str, str]:
    platforms = entry.get("platforms")
    if isinstance(platforms, dict):
        out: dict[str, str] = {}
        for platform, slot in platforms.items():
            if isinstance(platform, str) and isinstance(slot, dict):
                fp = slot.get("fingerprint")
                if isinstance(fp, str):
                    out[platform] = fp
        return out

    raw = entry.get("semantic_config")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for platform in raw.get("platforms") or []:
        if not isinstance(platform, str):
            continue
        out[platform] = _fingerprint_payload(
            _platform_semantic_payload_from_raw(topic, platform, raw)
        )
    return out


def _archive_write(archive_dir: Path, relative_name: str, payload: Any) -> None:
    target = archive_dir / relative_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _remove_topic_from_items_file(
    path: Path,
    topic: str,
    archive_dir: Path,
    *,
    platform: str | None = None,
) -> int:
    raw = _load_json(path)
    if not isinstance(raw, dict):
        return 0
    items = raw.get("items")
    if not isinstance(items, list):
        return 0
    def _matches(item: Any) -> bool:
        if not isinstance(item, dict) or item.get("topic") != topic:
            return False
        return platform is None or item.get("platform") == platform

    removed = [item for item in items if _matches(item)]
    if not removed:
        return 0
    raw["items"] = [item for item in items if not _matches(item)]
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


def _remove_search_terms_platform(topic: str, platform: str, archive_dir: Path) -> bool:
    raw = _load_json(_SEARCH_TERMS_PATH)
    if not isinstance(raw, dict):
        return False
    topics = raw.get("topics")
    if not isinstance(topics, dict):
        return False
    topic_terms = topics.get(topic)
    if not isinstance(topic_terms, dict) or platform not in topic_terms:
        return False
    _archive_write(
        archive_dir,
        "search_terms.json",
        {topic: {platform: topic_terms[platform]}},
    )
    del topic_terms[platform]
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


def _remove_queue_platform(topic: str, platform: str, archive_dir: Path) -> int:
    raw = _load_json(_QUEUE_PATH)
    if not isinstance(raw, dict):
        return 0
    queue = Queue.model_validate(raw)
    items = queue.topics.get(topic)
    if not items:
        return 0
    removed = [item for item in items if item.content.platform == platform]
    if not removed:
        return 0
    queue.topics[topic] = [item for item in items if item.content.platform != platform]
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


def _remove_patrol_items_platform(topic: str, platform: str, archive_dir: Path) -> int:
    if not _PATROL_DIR.exists():
        return 0
    removed = 0
    patrol_archive = archive_dir / "patrol"
    patrol_archive.mkdir(parents=True, exist_ok=True)
    for path in sorted(_PATROL_DIR.glob("*.json")):
        raw = _load_json(path)
        if (
            not isinstance(raw, dict)
            or raw.get("topic") != topic
            or raw.get("platform") != platform
        ):
            continue
        path.replace(patrol_archive / path.name)
        removed += 1
    return removed


@dataclass
class TopicResetSummary:
    topic: str
    reason: str
    archived_to: str
    platform: str | None = None
    catalog_removed: bool = False
    catalog_sources_removed: int = 0
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
    added_platforms: list[str] = field(default_factory=list)
    changed_platforms: list[str] = field(default_factory=list)
    removed_platforms: list[str] = field(default_factory=list)
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


def _reset_platform_state(
    workdir: Path,
    topic: str,
    platform: str,
    *,
    reason: str,
    now: str,
) -> TopicResetSummary:
    state_dir = workdir / "state"
    archive_dir = (
        workdir
        / _ARCHIVE_DIR
        / now.replace(":", "").replace("+", "Z")
        / topic
        / platform
    )
    archive_dir.mkdir(parents=True, exist_ok=True)
    summary = TopicResetSummary(
        topic=topic,
        platform=platform,
        reason=reason,
        archived_to=str(archive_dir),
    )

    summary.catalog_sources_removed = catalog_io.archive_topic_platform(
        state_dir, topic, platform, archive_dir / "source_catalog.json",
    )
    summary.catalog_removed = summary.catalog_sources_removed > 0
    summary.search_terms_removed = _remove_search_terms_platform(topic, platform, archive_dir)
    summary.queue_items_removed = _remove_queue_platform(topic, platform, archive_dir)
    summary.patrol_items_removed = _remove_patrol_items_platform(topic, platform, archive_dir)
    summary.seed_sources_removed = _remove_topic_from_items_file(
        state_dir / "seed_sources.json", topic, archive_dir, platform=platform,
    )
    summary.youtube_keywords_removed = _remove_topic_from_items_file(
        state_dir / "youtube_channel_keywords.json", topic, archive_dir, platform=platform,
    )
    summary.youtube_candidates_removed = _remove_topic_from_items_file(
        state_dir / "youtube_candidates.json", topic, archive_dir, platform=platform,
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
        if _previous_topic_fingerprint(topic, previous_topics.get(topic) or {})
        != current_manifest["topics"][topic]["topic_fingerprint"]
    )
    forced = sorted(topic for topic in force_topics if topic in current_names and topic not in changed)
    topic_level_reset = set(changed) | set(forced)

    added_platforms: list[str] = []
    removed_platforms: list[str] = []
    changed_platforms: list[str] = []
    for topic in sorted((current_names & previous_names) - topic_level_reset):
        previous_platforms = _previous_platform_fingerprints(topic, previous_topics.get(topic) or {})
        current_platforms = {
            platform: slot["fingerprint"]
            for platform, slot in current_manifest["topics"][topic]["platforms"].items()
        }
        previous_set = set(previous_platforms)
        current_set = set(current_platforms)
        added_platforms.extend(f"{topic}:{platform}" for platform in sorted(current_set - previous_set))
        removed_platforms.extend(f"{topic}:{platform}" for platform in sorted(previous_set - current_set))
        changed_platforms.extend(
            f"{topic}:{platform}"
            for platform in sorted(current_set & previous_set)
            if previous_platforms[platform] != current_platforms[platform]
        )

    result = ReconcileResult(
        added_topics=added,
        changed_topics=changed,
        removed_topics=removed,
        forced_topics=forced,
        added_platforms=added_platforms,
        changed_platforms=changed_platforms,
        removed_platforms=removed_platforms,
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
    for slot in removed_platforms:
        topic, platform = slot.rsplit(":", 1)
        result.resets.append(
            _reset_platform_state(
                workdir, topic, platform, reason="platform_removed", now=now,
            )
        )
    for slot in changed_platforms:
        topic, platform = slot.rsplit(":", 1)
        result.resets.append(
            _reset_platform_state(
                workdir, topic, platform, reason="platform_config_changed", now=now,
            )
        )

    atomic_write_json(manifest_path, current_manifest)
    if added or changed or removed or forced or added_platforms or removed_platforms or changed_platforms:
        logger.info(
            "topic_reconcile: added=%s changed=%s removed=%s forced=%s "
            "platform_added=%s platform_changed=%s platform_removed=%s resets=%d",
            added or "—",
            changed or "—",
            removed or "—",
            forced or "—",
            added_platforms or "—",
            changed_platforms or "—",
            removed_platforms or "—",
            len(result.resets),
        )
        for reset in result.resets:
            target = f"{reset.topic}:{reset.platform}" if reset.platform else reset.topic
            logger.info("topic_reconcile: reset %s (%s) → %s", target, reset.reason, reset.archived_to)
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
