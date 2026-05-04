"""State I/O for the bootstrap pipeline.

All writer / loader helpers that serialise or deserialise bootstrap outputs
to disk, extracted from `core/interest_bootstrap.py` so the main pipeline
file stays focused on LLM inference + validation logic.

Written state files, in order of production:
  - state/seed_sources.json         ← write_seed_sources
  - state/youtube_channel_keywords.json ← write_youtube_channel_keywords
  - state/youtube_candidates.json   ← write_youtube_candidates
  - state/source_catalog.json       ← write_source_catalog
  - state/search_terms.json         ← write_search_terms
  - openfeed.yaml                   ← write_interests_yaml (temporal fill)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from openfeed.models.interests import InterestsConfig
from openfeed.models.seed_source import TopicPlatformSources
from openfeed.models.source import (
    BayesianPosterior,
    SourceAttribution,
    SourceCatalog,
    SourceEntry,
    YouTubeCandidate,
)
from openfeed.models.validated_source import ValidatedSource
from openfeed.prompts.interest_bootstrap import TopicYouTubeChannelKeywords
from openfeed.utils import catalog_io
from openfeed.utils.config_files import config_path, load_openfeed_config
from openfeed.utils.state_io import atomic_write_json


_BOOTSTRAP_REASON_CODE_FALLBACK = "bootstrap_seed_validated"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def write_seed_sources(seeds: list[TopicPlatformSources], state_dir: Path) -> None:
    payload = {
        "generated_at": _utc_now(),
        "items": [s.model_dump() for s in seeds],
    }
    atomic_write_json(state_dir / "seed_sources.json", payload)


def write_youtube_channel_keywords(
    yt: list[TopicYouTubeChannelKeywords], state_dir: Path
) -> None:
    payload = {"generated_at": _utc_now(), "items": [t.model_dump() for t in yt]}
    atomic_write_json(state_dir / "youtube_channel_keywords.json", payload)


def write_youtube_candidates(candidates: list[YouTubeCandidate], state_dir: Path) -> None:
    payload = {"generated_at": _utc_now(), "items": [c.model_dump() for c in candidates]}
    atomic_write_json(state_dir / "youtube_candidates.json", payload)


def write_source_catalog(validated: list[ValidatedSource], state_dir: Path) -> None:
    decided_at = _utc_now()
    sources: dict[str, SourceEntry] = {}
    for vs in validated:
        attribution_meta: dict[str, Any] = {
            "bootstrap_seed_name": vs.seed.name if vs.seed else None,
            "bootstrap_seed_why": vs.seed.reason if vs.seed else None,
            "matched_keywords": vs.matched_keywords,
            "llm_reasoning": vs.llm_reasoning,
            # Persist samples so keyword regeneration can reuse them without
            # re-running validation.
            "sample_titles": list(vs.sample_titles),
            "sample_snippets": list(vs.sample_snippets),
            **vs.metadata,
        }
        entry = SourceEntry(
            source_id=vs.canonical_id,
            platform=vs.platform,  # type: ignore[arg-type]
            topic=vs.topic,
            status=vs.status,
            name=vs.canonical_name,
            url=vs.url,
            decision_reason_code=vs.reason_code,
            decided_at=decided_at,
            attribution=SourceAttribution(
                introduced_by_seed_term=(vs.matched_keywords[0] if vs.matched_keywords else None),
                introduced_at=decided_at,
                matched_terms=list(vs.matched_keywords),
            ),
            posterior=BayesianPosterior(),
            metadata={k: v for k, v in attribution_meta.items() if v is not None},
        )
        sources[entry.catalog_key] = entry
    catalog = SourceCatalog(generated_at=decided_at, sources=sources)
    catalog_io.save_catalog(state_dir, catalog)


def write_search_terms(
    config: InterestsConfig,
    keywords_by_topic_platform: dict[str, dict[str, list[str]]],
    state_dir: Path,
) -> None:
    payload = {
        "generated_at": _utc_now(),
        "topics": {
            i.topic: {
                platform: {
                    "keywords": keywords_by_topic_platform.get(i.topic, {}).get(platform, []),
                }
                for platform in i.platforms
            }
            for i in config.interests
        },
    }
    atomic_write_json(state_dir / "search_terms.json", payload)


def write_interests_yaml(config: InterestsConfig, workdir: Path) -> None:
    """Persist enriched interests back to the configured openfeed YAML.

    PyYAML strips comments and reformats; openfeed.yaml has no structural
    comments today and pulling in ruamel just for round-tripping would be
    over-engineering. Atomic via temp-file rename so a crash never leaves a
    partial file."""
    del workdir
    target = config_path()
    payload = load_openfeed_config()
    payload["persona"] = config.persona
    payload["interests"] = [
        item.model_dump(exclude_none=True)
        for item in config.interests
    ]
    yaml_text = yaml.safe_dump(
        payload, default_flow_style=False, allow_unicode=True,
        sort_keys=False, indent=2,
    )
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(yaml_text, encoding="utf-8")
    tmp.replace(target)


# ---------------------------------------------------------------------------
# Loaders (incremental-rerun helpers)
# ---------------------------------------------------------------------------


def load_seed_sources(path: Path) -> list[TopicPlatformSources]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [TopicPlatformSources.model_validate(item) for item in raw.get("items", [])]


def load_youtube_channel_keywords(path: Path) -> list[TopicYouTubeChannelKeywords]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [TopicYouTubeChannelKeywords.model_validate(item) for item in raw.get("items", [])]


def load_youtube_candidates(path: Path) -> list[YouTubeCandidate]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [YouTubeCandidate.model_validate(item) for item in raw.get("items", [])]


def load_validated_from_catalog(
    state_dir: Path, *, reason_code_fallback: str = _BOOTSTRAP_REASON_CODE_FALLBACK,
) -> list[ValidatedSource]:
    """Reconstruct ValidatedSource list from the per-topic catalog dir."""
    catalog = catalog_io.load_catalog(state_dir)
    out: list[ValidatedSource] = []
    for entry in catalog.sources.values():
        metadata = dict(entry.metadata or {})
        sample_titles = list(metadata.pop("sample_titles", []) or [])
        sample_snippets = list(metadata.pop("sample_snippets", []) or [])
        metadata.pop("llm_reasoning", None)
        out.append(
            ValidatedSource(
                seed=None,
                topic=entry.topic,
                platform=entry.platform,
                canonical_id=entry.source_id,
                canonical_name=entry.name,
                url=entry.url,
                reason_code=entry.decision_reason_code or reason_code_fallback,
                status=entry.status,
                llm_reasoning=None,
                matched_keywords=list((entry.attribution.matched_terms if entry.attribution else []) or []),
                sample_titles=sample_titles,
                sample_snippets=sample_snippets,
                metadata=metadata,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Search-term merge helpers
# ---------------------------------------------------------------------------


def empty_keyword_slots(
    config: InterestsConfig, existing: dict[str, Any] | None,
) -> set[tuple[str, str]]:
    """Return the (topic, platform) slots whose keywords list is missing or empty."""
    empty: set[tuple[str, str]] = set()
    topics_block = (existing or {}).get("topics") or {}
    for topic_entry in config.interests:
        topic_block = topics_block.get(topic_entry.topic) or {}
        for platform in topic_entry.platforms:
            slot = (topic_block.get(platform) or {})
            if not slot.get("keywords"):
                empty.add((topic_entry.topic, platform))
    return empty


def merge_search_terms(
    config: InterestsConfig,
    existing: dict[str, Any] | None,
    new_by_topic_platform: dict[str, dict[str, list[str]]],
) -> dict[str, Any]:
    """Preserve existing non-empty slots; overwrite empty ones with new keywords."""
    merged_topics: dict[str, Any] = {}
    existing_topics = (existing or {}).get("topics") or {}
    for topic_entry in config.interests:
        topic_block: dict[str, Any] = {}
        existing_topic = existing_topics.get(topic_entry.topic) or {}
        for platform in topic_entry.platforms:
            existing_slot = existing_topic.get(platform) or {}
            existing_terms = existing_slot.get("keywords") or []
            if existing_terms:
                topic_block[platform] = {"keywords": existing_terms}
            else:
                new_terms = new_by_topic_platform.get(topic_entry.topic, {}).get(platform, [])
                topic_block[platform] = {"keywords": new_terms}
        merged_topics[topic_entry.topic] = topic_block
    return {"generated_at": _utc_now(), "topics": merged_topics}
