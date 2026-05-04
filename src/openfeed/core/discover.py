"""Ongoing source discovery — per-topic keyword search → unique source → review.

Bootstrap produced an initial catalog + per-(topic, platform) keyword pools.
Discover uses those keywords to expand the catalog:

  - YouTube: reuses the bootstrap-era pipeline (keyword search → enrich → frames
    → two-pass multimodal review) via `core.youtube_source_review`.
  - X: opencli keyword search → unique authors → profile + timeline enrich →
    two-pass review (pass 1 can embed post images; text-only in v1).
  - Web: opencli google search → unique hosts → feed-first discovery
    (autodiscovery + walk-up + Medium platform rule) → text-only review,
    source_id = feed URL.

All admitted sources are incrementally persisted into
`state/source_catalog.json`. Admit / reject events go to
`ledgers/decisions.jsonl`.

Policy knobs live in `openfeed.yaml` under `runtime.discover.*`.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openfeed.utils.config_files import config_path, load_env
from tqdm import tqdm

from openfeed.clients.content import feed, opencli, tiktok as tiktok_client
from openfeed.clients.llm import GeminiRunner
from openfeed.clients.content.opencli import OpenCLIError, OpenCLIInfraError
from openfeed.core.judgment_ledger import attach_file as _attach_ledger, emit_judgment
from openfeed.core.youtube_source_review import (
    YouTubeDiscoverParams,
    discover_youtube_candidates,
    extract_youtube_frames,
    review_youtube_candidates,
)
from openfeed.models.interests import InterestEntry, InterestsConfig, load_interests
from openfeed.models.persona import PersonaOutput
from openfeed.models.user_profile import get_user_profile
from openfeed.models.runtime import RuntimeConfig, load_runtime
from openfeed.models.source import (
    BayesianPosterior,
    SourceAttribution,
    SourceCatalog,
    SourceEntry,
)
from openfeed.models.validated_source import ValidatedSource
from openfeed.utils import backpressure, catalog_io
from openfeed.prompts.interest_bootstrap import (
    TopicYouTubeChannelKeywords,
)
from openfeed.prompts.content_understanding import (
    ContentUnderstanding,
    build_content_understanding_prompt,
)
from openfeed.prompts.source_review import (
    SourceReviewDecision,
    build_source_review_prompt,
    flatten_decision_reasoning,
)
from openfeed.utils.logging_setup import configure_task_logging
from openfeed.utils.state_io import atomic_write_json

logger = logging.getLogger("discover")

_LEDGER_PATH = Path("ledgers/decisions.jsonl")

_DISCOVER_ADMIT_REASON_CODE = {
    "youtube": "discover_youtube_search_admitted",
    "x": "discover_x_search_admitted",
    "web": "discover_web_feed_admitted",
    "tiktok": "discover_tiktok_search_admitted",
}

# Hard-gate reason codes — mechanical thresholds whose underlying condition
# can change over time (subs grow, followers climb, dead blogs revive). These
# rejects are stickied to catalog so within one retry window we don't waste
# opencli/LLM on them again, but expire after the window so channels that
# crossed the threshold get a fresh chance. LLM-level rejects are NOT here —
# those are fundamental judgments that don't decay.
_HARD_GATE_REASONS: frozenset[str] = frozenset({
    "low_subscribers",
    "low_followers",
    "low_videos",
    "min_entries_not_met",
    "stale",
    "no_entry_date",
    "no_tiktok_video_samples",
    "no_tiktok_media_samples",
    # System-level retire reasons that don't reflect the source's content
    # quality — let them re-enter the pool after the retry window so the
    # current rules get a fresh shot at them.
    "replaced_by_shorts_pivot",
    "filter_consistent_reject",
})

# How many recent posts to enrich each X author with, fed into the pass-1
# understanding LLM. Kept separate from runtime config because this is a review
# input shape decision, not a retrieval budget.
_X_ENRICH_POSTS = 10
_TIKTOK_REVIEW_VIDEOS = 5
_TIKTOK_REVIEW_SAMPLES = 3
_TIKTOK_REVIEW_PHOTOS = 5


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


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
    run_log = configure_task_logging("discover")
    _attach_ledger(_LEDGER_PATH)
    logger.info("run log → %s", run_log)
    logger.info("ledger → %s", _LEDGER_PATH)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def _int_value(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(",", "")
    if not text:
        return 0
    multiplier = 1
    suffix = text[-1:].lower()
    if suffix == "k":
        multiplier = 1_000
        text = text[:-1]
    elif suffix == "m":
        multiplier = 1_000_000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


def _load_search_terms(state_dir: Path) -> dict[str, dict[str, dict[str, list[str]]]]:
    raw = json.loads((state_dir / "search_terms.json").read_text(encoding="utf-8"))
    return raw.get("topics") or {}


def _load_catalog(state_dir: Path) -> SourceCatalog:
    return catalog_io.load_catalog(state_dir)


def _write_catalog(catalog: SourceCatalog, state_dir: Path, *, only_topic: str | None = None) -> None:
    """Persist the catalog. Default: split by topic, write every per-topic
    file under `state/source_catalog/`. Pass `only_topic` to write just one
    topic's file (used by per-topic discover so it doesn't trample sibling
    topics' concurrent edits)."""
    if only_topic is None:
        catalog_io.save_catalog(state_dir, catalog)
        return
    scoped = {k: v for k, v in catalog.sources.items() if v.topic == only_topic}
    catalog_io.save_catalog_topic(state_dir, only_topic, scoped)


def _expire_hard_gate_rejects(catalog: SourceCatalog, window_days: int) -> int:
    """Remove hard-gate rejections from the catalog once they exceed the retry
    window. Channel subs grow, dead blogs revive — give them another shot. The
    next discover run will re-enrich and re-gate; if they still fail the same
    hard gate, they get re-stickied with a fresh timestamp. Returns the number
    of entries removed."""
    if window_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    to_remove: list[str] = []
    for key, entry in catalog.sources.items():
        if entry.status != "rejected":
            continue
        if entry.decision_reason_code not in _HARD_GATE_REASONS:
            continue
        try:
            decided = datetime.fromisoformat(entry.decided_at)
        except ValueError:
            continue
        if decided < cutoff:
            to_remove.append(key)
    for key in to_remove:
        del catalog.sources[key]
    return len(to_remove)


def _known_web_hosts(catalog: SourceCatalog) -> set[str]:
    """Lower-cased host portion of every existing web source URL, so feed-URL
    candidates can dedupe against bootstrap-era HTML-URL sources without the
    source_id formats needing to match."""
    hosts: set[str] = set()
    for entry in catalog.sources.values():
        if entry.platform != "web":
            continue
        try:
            p = urllib.parse.urlparse(entry.url)
        except Exception:  # noqa: BLE001
            continue
        if p.netloc:
            hosts.add(p.netloc.lower())
    return hosts


def _upsert_validated(
    catalog: SourceCatalog,
    vs: ValidatedSource,
    known_keys: set[str],
    known_hosts: set[str],
) -> None:
    decided_at = _utc_now()
    attribution_meta: dict[str, Any] = {
        "matched_keywords": vs.matched_keywords,
        "llm_reasoning": vs.llm_reasoning,
        "sample_titles": list(vs.sample_titles),
        "sample_snippets": list(vs.sample_snippets),
        **vs.metadata,
    }
    entry = SourceEntry(
        source_id=vs.canonical_id,
        platform=vs.platform,  # type: ignore[arg-type]
        topic=vs.topic,
        status=vs.status,  # "active" on admit, "rejected" on LLM reject
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
    catalog.sources[entry.catalog_key] = entry
    known_keys.add(entry.catalog_key)
    if entry.platform == "web":
        try:
            host = urllib.parse.urlparse(entry.url).netloc.lower()
        except Exception:  # noqa: BLE001
            host = ""
        if host:
            known_hosts.add(host)


# ---------------------------------------------------------------------------
# YouTube (thin wrapper around the shared pipeline)
# ---------------------------------------------------------------------------


def _discover_youtube_for_topic(
    topic_entry: InterestEntry,
    keywords: list[str],
    config: InterestsConfig,
    persona: PersonaOutput,
    runner: GeminiRunner,
    runtime: RuntimeConfig,
    *,
    catalog_channel_ids: set[str],
) -> list[ValidatedSource]:
    topic = topic_entry.topic
    rt = runtime.discover.youtube
    params = YouTubeDiscoverParams(
        keywords_per_topic=rt.keywords_per_topic,
        results_per_keyword=rt.results_per_keyword,
        oversample_multiplier=rt.oversample_multiplier,
        min_subscribers=rt.min_subscribers,
        admit_reason_code=_DISCOVER_ADMIT_REASON_CODE["youtube"],
    )
    topic_kw = TopicYouTubeChannelKeywords(topic=topic, keywords=keywords)
    candidates, hard_gate_rejects = discover_youtube_candidates(
        [topic_kw], config, params=params, catalog_channel_ids=catalog_channel_ids,
    )
    # hard_gate_rejects (low_subscribers) must be persisted to catalog so
    # next discover run skips them at stage 2A; they expire after the retry
    # window and become re-eligible automatically.
    if not candidates:
        return list(hard_gate_rejects)
    # All survivors of stage-1 + stage-2A (resolve, catalog dedup, subs gate)
    # go downstream — `results_per_keyword` only sizes the opencli pull, it is
    # not a cap on yield.
    extract_youtube_frames(candidates)
    reviewed = review_youtube_candidates(
        candidates, config, persona, runner,
        params=params,
    )
    return hard_gate_rejects + reviewed


# ---------------------------------------------------------------------------
# X
# ---------------------------------------------------------------------------


def _discover_x_for_topic(
    topic_entry: InterestEntry,
    keywords: list[str],
    config: InterestsConfig,
    persona: PersonaOutput,
    runner: GeminiRunner,
    runtime: RuntimeConfig,
    known_keys: set[str],
) -> list[ValidatedSource]:
    topic = topic_entry.topic
    rt = runtime.discover.x
    kws = keywords[: rt.keywords_per_topic]
    if not kws:
        return []
    logger.info(
        "x_discover[%s]: running %d keywords × %d posts each", topic, len(kws), rt.results_per_keyword,
    )

    # Stage 1: keyword search → unique authors
    by_author: dict[str, dict[str, Any]] = {}
    for kw in kws:
        try:
            posts = opencli.twitter_search(kw, limit=rt.results_per_keyword)
        except OpenCLIInfraError:
            raise
        except OpenCLIError as exc:
            logger.warning("x_discover[%s]: twitter_search %r failed: %s", topic, kw, exc)
            continue
        for post in posts:
            author = (post.get("author") or "").strip().lstrip("@")
            if not author:
                continue
            if f"x:{author}:{topic}" in known_keys:
                continue
            meta = by_author.setdefault(author, {"matched_keywords": []})
            if kw not in meta["matched_keywords"]:
                meta["matched_keywords"].append(kw)
    logger.info("x_discover[%s]: %d unique candidate authors", topic, len(by_author))
    if not by_author:
        return []

    # Stage 2: enrich each author (opencli calls; global serialisation)
    enriched: list[dict[str, Any]] = []
    for handle, meta in tqdm(by_author.items(), desc=f"x_enrich[{topic}]", unit="acct"):
        try:
            profile = opencli.twitter_profile(handle)
        except OpenCLIInfraError:
            raise
        except OpenCLIError as exc:
            emit_judgment(
                event_type="reject_source", platform="x", topic=topic,
                source_id=handle, source_name=handle,
                reason_code="profile_lookup_failed",
                matched_keywords=meta["matched_keywords"],
                evidence={"error": str(exc)[:200]},
            )
            continue
        screen_name = str(profile.get("screen_name") or "").strip()
        if not screen_name:
            emit_judgment(
                event_type="reject_source", platform="x", topic=topic,
                source_id=handle, source_name=handle,
                reason_code="profile_not_found",
                matched_keywords=meta["matched_keywords"],
                evidence={"handle": handle},
            )
            continue
        if f"x:{screen_name}:{topic}" in known_keys:
            continue
        followers = int(profile.get("followers") or 0)
        if followers < rt.min_followers:
            emit_judgment(
                event_type="reject_source", platform="x", topic=topic,
                source_id=screen_name, source_name=str(profile.get("name") or handle),
                reason_code="low_followers",
                matched_keywords=meta["matched_keywords"],
                evidence={"followers": followers, "min_followers": rt.min_followers},
            )
            continue
        try:
            tweets = opencli.twitter_user_timeline(handle, limit=_X_ENRICH_POSTS)
        except OpenCLIInfraError:
            raise
        except OpenCLIError as exc:
            logger.warning("x_discover[%s]: timeline failed for %s: %s", topic, handle, exc)
            tweets = []
        enriched.append({
            "handle": handle,
            "screen_name": screen_name,
            "display_name": str(profile.get("name") or handle),
            "bio": str(profile.get("bio") or ""),
            "followers": followers,
            "tweets": tweets,
            "matched_keywords": meta["matched_keywords"],
        })
    logger.info(
        "x_discover[%s]: %d passed hard gate (followers >= %d)",
        topic, len(enriched), rt.min_followers,
    )
    if not enriched:
        return []

    # Stage 3: parallel per-author LLM review
    validated: list[ValidatedSource] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {
            pool.submit(_review_x_author, enr, topic_entry, persona, config, runner): enr
            for enr in enriched
        }
        for fut in tqdm(as_completed(futures), total=len(futures), desc=f"x_review[{topic}]", unit="acct"):
            enr = futures[fut]
            try:
                vs = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("x_review crashed for %s/%s: %s", topic, enr["handle"], exc)
                continue
            if vs is not None:
                validated.append(vs)
    logger.info("x_discover[%s]: admitted=%d", topic, len(validated))
    return validated


def _review_x_author(
    enr: dict[str, Any],
    topic_entry: InterestEntry,
    persona: PersonaOutput,
    config: InterestsConfig,
    runner: GeminiRunner,
) -> ValidatedSource | None:
    sample_posts = [
        {
            "text": (t.get("text") or "")[:800],
            "likes": t.get("likes"),
            "retweets": t.get("retweets"),
        }
        for t in (enr.get("tweets") or [])[:_X_ENRICH_POSTS]
    ]

    sample_understandings: list[ContentUnderstanding] = []
    for p in sample_posts:
        text = p.get("text") or ""
        if not text:
            continue
        try:
            u_raw = runner.run_json(
                build_content_understanding_prompt(
                    text=f"Post text:\n{text}",
                    images=None,
                ),
                schema=ContentUnderstanding.model_json_schema(),
                schema_name=ContentUnderstanding.__name__,
            )
            sample_understandings.append(ContentUnderstanding.model_validate(u_raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("x content_understanding failed for %s: %s",
                           enr.get("handle"), exc)

    if not sample_understandings:
        logger.warning("x_review: no usable understandings for %s", enr.get("handle"))
        return None

    source_info = (
        f"handle: @{enr['screen_name']}\n"
        f"display name: {enr['display_name']}\n"
        f"bio: {enr['bio']}\n"
        f"followers: {enr['followers']}"
    )
    try:
        d_raw = runner.run_json(
            build_source_review_prompt(
                source_info=source_info,
                sample_understandings=sample_understandings,
                topic_data=topic_entry,
                persona=persona,
                language_preferences=topic_entry.language_preferences,
            ),
            schema=SourceReviewDecision.model_json_schema(),
            schema_name=SourceReviewDecision.__name__,
        )
        decision = SourceReviewDecision.model_validate(d_raw)
        reasoning_text = flatten_decision_reasoning(decision)
    except Exception as exc:  # noqa: BLE001
        logger.warning("x_review LLM failed for %s: %s", enr["handle"], exc)
        return None

    sample_texts = [p["text"] for p in sample_posts if p.get("text")]
    ledger_evidence = {
        "bio": enr["bio"][:300],
        "followers": enr["followers"],
        "sample_post_count": len(sample_texts),
        "sample_understandings": [u.model_dump() for u in sample_understandings],
    }
    if decision.decision != "admit":
        emit_judgment(
            event_type="reject_source", platform="x", topic=topic_entry.topic,
            source_id=enr["screen_name"], source_name=enr["display_name"],
            reason_code=decision.reason_code,
            reasoning=reasoning_text,
            matched_keywords=list(enr["matched_keywords"]),
            evidence=ledger_evidence,
        )
        return ValidatedSource(
            seed=None,
            topic=topic_entry.topic,
            platform="x",
            canonical_id=enr["screen_name"],
            canonical_name=enr["display_name"],
            url=f"https://x.com/{enr['screen_name']}",
            reason_code=decision.reason_code,
            status="rejected",
            llm_reasoning=reasoning_text,
            matched_keywords=list(enr["matched_keywords"]),
            metadata={"bio": enr["bio"], "followers": enr["followers"]},
        )

    emit_judgment(
        event_type="admit_source", platform="x", topic=topic_entry.topic,
        source_id=enr["screen_name"], source_name=enr["display_name"],
        reason_code=decision.reason_code,
        reasoning=reasoning_text,
        matched_keywords=list(enr["matched_keywords"]),
        evidence=ledger_evidence,
    )
    return ValidatedSource(
        seed=None,
        topic=topic_entry.topic,
        platform="x",
        canonical_id=enr["screen_name"],
        canonical_name=enr["display_name"],
        url=f"https://x.com/{enr['screen_name']}",
        reason_code=_DISCOVER_ADMIT_REASON_CODE["x"],
        llm_reasoning=reasoning_text,
        matched_keywords=list(enr["matched_keywords"]),
        sample_titles=[],
        sample_snippets=sample_texts,
        metadata={"bio": enr["bio"], "followers": enr["followers"]},
    )


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------


def _discover_tiktok_for_topic(
    topic_entry: InterestEntry,
    keywords: list[str],
    config: InterestsConfig,
    persona: PersonaOutput,
    runner: GeminiRunner,
    runtime: RuntimeConfig,
    known_keys: set[str],
) -> list[ValidatedSource]:
    topic = topic_entry.topic
    rt = runtime.discover.tiktok
    kws = keywords[: rt.keywords_per_topic]
    if not kws:
        return []
    logger.info(
        "tiktok_discover[%s]: running %d keywords × %d videos each",
        topic, len(kws), rt.results_per_keyword,
    )

    # Stage 1: keyword search → unique creators.
    by_author: dict[str, dict[str, Any]] = {}
    for kw in kws:
        try:
            rows = opencli.tiktok_search(kw, limit=rt.results_per_keyword)
        except OpenCLIInfraError:
            raise
        except OpenCLIError as exc:
            logger.warning("tiktok_discover[%s]: tiktok_search %r failed: %s", topic, kw, exc)
            continue
        for row in rows:
            author = str(row.get("author") or "").strip().lstrip("@")
            if not author:
                continue
            if f"tiktok:{author}:{topic}" in known_keys:
                continue
            meta = by_author.setdefault(author, {"matched_keywords": [], "search_rows": []})
            if kw not in meta["matched_keywords"]:
                meta["matched_keywords"].append(kw)
            if len(meta["search_rows"]) < 3:
                meta["search_rows"].append(row)
    logger.info("tiktok_discover[%s]: %d unique candidate creators", topic, len(by_author))
    if not by_author:
        return []
    if rt.max_candidates_per_tick is not None and len(by_author) > rt.max_candidates_per_tick:
        by_author = dict(list(by_author.items())[: rt.max_candidates_per_tick])
        logger.info(
            "tiktok_discover[%s]: capped to %d candidate creators for this tick",
            topic,
            len(by_author),
        )

    validated: list[ValidatedSource] = []
    for handle, meta in tqdm(by_author.items(), desc=f"tiktok_enrich[{topic}]", unit="acct"):
        try:
            profile = opencli.tiktok_profile(handle)
        except OpenCLIInfraError:
            raise
        except OpenCLIError as exc:
            emit_judgment(
                event_type="reject_source", platform="tiktok", topic=topic,
                source_id=handle, source_name=handle,
                reason_code="profile_lookup_failed",
                matched_keywords=meta["matched_keywords"],
                evidence={"error": str(exc)[:200]},
            )
            continue
        username = str(profile.get("username") or handle).strip().lstrip("@")
        if not username:
            emit_judgment(
                event_type="reject_source", platform="tiktok", topic=topic,
                source_id=handle, source_name=handle,
                reason_code="profile_not_found",
                matched_keywords=meta["matched_keywords"],
                evidence={"handle": handle},
            )
            continue
        if f"tiktok:{username}:{topic}" in known_keys:
            continue

        display_name = str(profile.get("name") or username)
        followers = _int_value(profile.get("followers"))
        videos_count = _int_value(profile.get("videos"))
        if followers < rt.min_followers:
            vs = _tiktok_hard_reject(
                topic_entry, username, display_name, "low_followers",
                meta["matched_keywords"],
                {
                    "followers": followers,
                    "min_followers": rt.min_followers,
                    "profile": profile,
                    "search_rows": meta["search_rows"],
                },
            )
            validated.append(vs)
            continue
        if videos_count < rt.min_videos:
            vs = _tiktok_hard_reject(
                topic_entry, username, display_name, "low_videos",
                meta["matched_keywords"],
                {
                    "videos": videos_count,
                    "min_videos": rt.min_videos,
                    "profile": profile,
                    "search_rows": meta["search_rows"],
                },
            )
            validated.append(vs)
            continue

        try:
            recent = tiktok_client.tiktok_list_user_videos(
                username, limit=_TIKTOK_REVIEW_VIDEOS,
            )
        except tiktok_client.TikTokYtDlpError as exc:
            emit_judgment(
                event_type="reject_source", platform="tiktok", topic=topic,
                source_id=username, source_name=display_name,
                reason_code="recent_videos_lookup_failed",
                matched_keywords=meta["matched_keywords"],
                evidence={"error": str(exc)[:200], "profile": profile},
            )
            continue
        sample_media: list[tiktok_client.TikTokVideoMetadata] = []
        for item in recent:
            if len(sample_media) >= _TIKTOK_REVIEW_SAMPLES:
                break
            if item.has_video_stream and (
                item.duration is None
                or item.duration <= runtime.filter.tiktok.duration_max_seconds
            ):
                sample_media.append(item)
                continue
            try:
                probed = tiktok_client.tiktok_probe_media(item.url)
            except tiktok_client.TikTokYtDlpError as exc:
                logger.info("tiktok media probe failed for %s/%s: %s", username, item.id, exc)
                continue
            if probed.media_kind == "photo" and probed.photo_count > 0:
                sample_media.append(probed)
        if not sample_media:
            vs = _tiktok_hard_reject(
                topic_entry, username, display_name, "no_tiktok_media_samples",
                meta["matched_keywords"],
                {
                    "profile": profile,
                    "recent": [item.model_dump(exclude={"raw"}) for item in recent],
                },
            )
            validated.append(vs)
            continue

        reviewed = _review_tiktok_creator(
            username=username,
            display_name=display_name,
            profile=profile,
            sample_media=sample_media,
            matched_keywords=meta["matched_keywords"],
            topic_entry=topic_entry,
            persona=persona,
            config=config,
            runner=runner,
        )
        if reviewed is not None:
            validated.append(reviewed)
    logger.info(
        "tiktok_discover[%s]: admitted=%d rejected=%d",
        topic,
        sum(1 for vs in validated if vs.status == "active"),
        sum(1 for vs in validated if vs.status == "rejected"),
    )
    return validated


def _tiktok_hard_reject(
    topic_entry: InterestEntry,
    username: str,
    display_name: str,
    reason_code: str,
    matched_keywords: list[str],
    evidence: dict[str, Any],
) -> ValidatedSource:
    emit_judgment(
        event_type="reject_source", platform="tiktok", topic=topic_entry.topic,
        source_id=username, source_name=display_name,
        reason_code=reason_code,
        matched_keywords=matched_keywords,
        evidence=evidence,
    )
    return ValidatedSource(
        seed=None,
        topic=topic_entry.topic,
        platform="tiktok",
        canonical_id=username,
        canonical_name=display_name,
        url=f"https://www.tiktok.com/@{username}",
        reason_code=reason_code,
        status="rejected",
        matched_keywords=list(matched_keywords),
        metadata=evidence,
    )


def _review_tiktok_creator(
    *,
    username: str,
    display_name: str,
    profile: dict[str, Any],
    sample_media: list[tiktok_client.TikTokVideoMetadata],
    matched_keywords: list[str],
    topic_entry: InterestEntry,
    persona: PersonaOutput,
    config: InterestsConfig,
    runner: GeminiRunner,
) -> ValidatedSource | None:
    del config
    sample_understandings: list[ContentUnderstanding] = []
    for item in sample_media:
        images: list[bytes] | None = None
        if item.media_kind == "photo":
            try:
                images = tiktok_client.fetch_tiktok_photo_images(
                    item, max_images=_TIKTOK_REVIEW_PHOTOS,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("tiktok photo fetch failed for %s/%s: %s", username, item.id, exc)
                images = None
        text = (
            f"TikTok {item.media_kind} title/description:\n{item.title[:1200]}\n\n"
            f"photo_count: {item.photo_count}\n"
            f"duration_seconds: {item.duration}\n"
            f"views: {item.view_count}\n"
            f"likes: {item.like_count}\n"
            f"comments: {item.comment_count}\n"
            f"reposts: {item.repost_count}"
        )
        try:
            u_raw = runner.run_json(
                build_content_understanding_prompt(text=text, images=images),
                schema=ContentUnderstanding.model_json_schema(),
                schema_name=ContentUnderstanding.__name__,
            )
            sample_understandings.append(ContentUnderstanding.model_validate(u_raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("tiktok content_understanding failed for %s/%s: %s", username, item.id, exc)

    if not sample_understandings:
        logger.warning("tiktok_review: no usable understandings for %s", username)
        return None

    source_info = (
        f"handle: @{username}\n"
        f"display name: {display_name}\n"
        f"bio: {profile.get('bio') or ''}\n"
        f"followers: {profile.get('followers')}\n"
        f"videos: {profile.get('videos')}\n"
        f"likes: {profile.get('likes')}\n"
        f"verified: {profile.get('verified')}"
    )
    try:
        d_raw = runner.run_json(
            build_source_review_prompt(
                source_info=source_info,
                sample_understandings=sample_understandings,
                topic_data=topic_entry,
                persona=persona,
                language_preferences=topic_entry.language_preferences,
            ),
            schema=SourceReviewDecision.model_json_schema(),
            schema_name=SourceReviewDecision.__name__,
        )
        decision = SourceReviewDecision.model_validate(d_raw)
        reasoning_text = flatten_decision_reasoning(decision)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tiktok_review LLM failed for %s: %s", username, exc)
        return None

    ledger_evidence = {
        "profile": profile,
        "sample_media": [item.model_dump(exclude={"raw"}) for item in sample_media],
        "sample_understandings": [u.model_dump() for u in sample_understandings],
    }
    sample_titles = [item.title for item in sample_media if item.title]
    metadata = {
        "profile": profile,
        "sample_media": [item.model_dump(exclude={"raw"}) for item in sample_media],
    }
    if decision.decision != "admit":
        emit_judgment(
            event_type="reject_source", platform="tiktok", topic=topic_entry.topic,
            source_id=username, source_name=display_name,
            reason_code=decision.reason_code,
            reasoning=reasoning_text,
            matched_keywords=matched_keywords,
            evidence=ledger_evidence,
        )
        return ValidatedSource(
            seed=None,
            topic=topic_entry.topic,
            platform="tiktok",
            canonical_id=username,
            canonical_name=display_name,
            url=f"https://www.tiktok.com/@{username}",
            reason_code=decision.reason_code,
            status="rejected",
            llm_reasoning=reasoning_text,
            matched_keywords=list(matched_keywords),
            sample_titles=sample_titles,
            metadata=metadata,
        )

    emit_judgment(
        event_type="admit_source", platform="tiktok", topic=topic_entry.topic,
        source_id=username, source_name=display_name,
        reason_code=decision.reason_code,
        reasoning=reasoning_text,
        matched_keywords=matched_keywords,
        evidence=ledger_evidence,
    )
    return ValidatedSource(
        seed=None,
        topic=topic_entry.topic,
        platform="tiktok",
        canonical_id=username,
        canonical_name=display_name,
        url=f"https://www.tiktok.com/@{username}",
        reason_code=_DISCOVER_ADMIT_REASON_CODE["tiktok"],
        llm_reasoning=reasoning_text,
        matched_keywords=list(matched_keywords),
        sample_titles=sample_titles,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------


def _discover_web_for_topic(
    topic_entry: InterestEntry,
    keywords: list[str],
    config: InterestsConfig,
    persona: PersonaOutput,
    runner: GeminiRunner,
    runtime: RuntimeConfig,
    known_keys: set[str],
    known_hosts: set[str],
) -> list[ValidatedSource]:
    topic = topic_entry.topic
    rt = runtime.discover.web
    kws = keywords[: rt.keywords_per_topic]
    if not kws:
        return []
    logger.info(
        "web_discover[%s]: running %d keywords × %d urls each", topic, len(kws), rt.results_per_keyword,
    )

    # Stage 1: google_search → unique hosts
    by_host: dict[str, dict[str, Any]] = {}
    for kw in kws:
        lang = "zh" if _has_cjk(kw) else "en"
        try:
            results = opencli.google_search(kw, limit=rt.results_per_keyword, lang=lang)
        except OpenCLIInfraError:
            raise
        except OpenCLIError as exc:
            logger.warning("web_discover[%s]: google_search %r failed: %s", topic, kw, exc)
            continue
        for r in results:
            url = (r.get("url") or "").strip()
            if not url:
                continue
            try:
                p = urllib.parse.urlparse(url)
            except Exception:  # noqa: BLE001
                continue
            if p.scheme not in ("http", "https") or not p.netloc:
                continue
            host = p.netloc.lower()
            if host in known_hosts:
                continue
            entry = by_host.setdefault(host, {"article_url": url, "matched_keywords": []})
            if kw not in entry["matched_keywords"]:
                entry["matched_keywords"].append(kw)
    logger.info("web_discover[%s]: %d unique candidate hosts", topic, len(by_host))
    if not by_host:
        return []

    # Stage 2: parallel feed resolution (pure HTTP, no opencli)
    resolved: list[dict[str, Any]] = []

    def _resolve_one(host: str, meta: dict[str, Any]) -> dict[str, Any] | None:
        res = feed.resolve_and_validate(
            meta["article_url"],
            min_entries=rt.min_feed_entries,
            max_age_days=rt.max_age_days,
        )
        if not res.feed_url:
            emit_judgment(
                event_type="reject_source", platform="web", topic=topic,
                source_id=host, source_name=host,
                reason_code=res.reject_reason_code or "no_feed_found",
                matched_keywords=meta["matched_keywords"],
                evidence={"article_url": meta["article_url"], "detail": res.reject_detail or ""},
            )
            return None
        return {
            "host": host,
            "feed_url": res.feed_url,
            "method": res.method,
            "article_url": meta["article_url"],
            "matched_keywords": meta["matched_keywords"],
            "feed_title": res.feed_title or "",
            "feed_description": res.feed_description or "",
            "entries": res.entries,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_resolve_one, h, m): h for h, m in by_host.items()}
        for fut in tqdm(as_completed(futures), total=len(futures), desc=f"web_resolve[{topic}]", unit="site"):
            r = fut.result()
            if r is not None:
                resolved.append(r)
    logger.info("web_discover[%s]: %d hosts have a valid feed", topic, len(resolved))
    if not resolved:
        return []

    # Stage 3: parallel LLM review (text only)
    validated: list[ValidatedSource] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {
            pool.submit(_review_web_feed, res, topic_entry, persona, config, runner): res
            for res in resolved
        }
        for fut in tqdm(as_completed(futures), total=len(futures), desc=f"web_review[{topic}]", unit="site"):
            res = futures[fut]
            try:
                vs = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("web_review crashed for %s/%s: %s", topic, res["host"], exc)
                continue
            if vs is not None:
                validated.append(vs)
    logger.info("web_discover[%s]: admitted=%d", topic, len(validated))
    return validated


def _review_web_feed(
    res: dict[str, Any],
    topic_entry: InterestEntry,
    persona: PersonaOutput,
    config: InterestsConfig,
    runner: GeminiRunner,
) -> ValidatedSource | None:
    sample_understandings: list[ContentUnderstanding] = []
    for entry in res.get("entries", []):
        title = (entry.get("title") or "").strip()
        summary = (entry.get("summary") or "").strip()[:800]
        if not (title or summary):
            continue
        text = f"Title: {title}\n\nSummary:\n{summary}"
        try:
            u_raw = runner.run_json(
                build_content_understanding_prompt(text=text, images=None),
                schema=ContentUnderstanding.model_json_schema(),
                schema_name=ContentUnderstanding.__name__,
            )
            sample_understandings.append(ContentUnderstanding.model_validate(u_raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("web content_understanding failed for %s/%s: %s",
                           res["host"], title[:40], exc)

    if not sample_understandings:
        logger.warning("web_review: no usable understandings for %s", res["host"])
        return None

    source_info = (
        f"feed url: {res['feed_url']}\n"
        f"feed title: {res['feed_title']}\n"
        f"feed description: {res['feed_description']}"
    )
    try:
        d_raw = runner.run_json(
            build_source_review_prompt(
                source_info=source_info,
                sample_understandings=sample_understandings,
                topic_data=topic_entry,
                persona=persona,
                language_preferences=topic_entry.language_preferences,
            ),
            schema=SourceReviewDecision.model_json_schema(),
            schema_name=SourceReviewDecision.__name__,
        )
        decision = SourceReviewDecision.model_validate(d_raw)
        reasoning_text = flatten_decision_reasoning(decision)
    except Exception as exc:  # noqa: BLE001
        logger.warning("web_review LLM failed for %s: %s", res["host"], exc)
        return None

    source_name = res["feed_title"] or res["host"]
    ledger_evidence = {
        "feed_url": res["feed_url"],
        "discovery_method": res["method"],
        "feed_title": res["feed_title"],
        "sample_entry_count": len(res["entries"]),
        "sample_understandings": [u.model_dump() for u in sample_understandings],
    }
    if decision.decision != "admit":
        emit_judgment(
            event_type="reject_source", platform="web", topic=topic_entry.topic,
            source_id=res["feed_url"], source_name=source_name,
            reason_code=decision.reason_code,
            reasoning=reasoning_text,
            matched_keywords=list(res["matched_keywords"]),
            evidence=ledger_evidence,
        )
        return ValidatedSource(
            seed=None,
            topic=topic_entry.topic,
            platform="web",
            canonical_id=res["feed_url"],
            canonical_name=source_name,
            url=res["feed_url"],
            reason_code=decision.reason_code,
            status="rejected",
            llm_reasoning=reasoning_text,
            matched_keywords=list(res["matched_keywords"]),
            metadata={
                "discovery_method": res["method"],
                "feed_description": res["feed_description"],
                "article_url": res["article_url"],
            },
        )

    emit_judgment(
        event_type="admit_source", platform="web", topic=topic_entry.topic,
        source_id=res["feed_url"], source_name=source_name,
        reason_code=decision.reason_code,
        reasoning=reasoning_text,
        matched_keywords=list(res["matched_keywords"]),
        evidence=ledger_evidence,
    )
    sample_titles = [(e.get("title") or "").strip() for e in (res["entries"] or [])[:10] if e.get("title")]
    sample_snippets = [
        (e.get("summary") or "").strip()[:400]
        for e in (res["entries"] or [])[:5]
        if e.get("summary")
    ]
    return ValidatedSource(
        seed=None,
        topic=topic_entry.topic,
        platform="web",
        canonical_id=res["feed_url"],
        canonical_name=source_name,
        url=res["feed_url"],
        reason_code=_DISCOVER_ADMIT_REASON_CODE["web"],
        llm_reasoning=reasoning_text,
        matched_keywords=list(res["matched_keywords"]),
        sample_titles=sample_titles,
        sample_snippets=sample_snippets,
        metadata={
            "discovery_method": res["method"],
            "feed_description": res["feed_description"],
            "article_url": res["article_url"],
        },
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openfeed-discover")
    parser.add_argument(
        "--topic", default=None,
        help=(
            "Run discover for a single topic only (matched against openfeed.yaml). "
            "Default: iterate every topic sequentially. Multiple --topic processes can run "
            "in parallel — each writes only its own state/source_catalog/<topic>.json file."
        ),
    )
    parser.add_argument(
        "--platform", choices=["youtube", "x", "web", "tiktok"], default=None,
        help="Run discover for one platform only. Intended for scoped rollout validation.",
    )
    args = parser.parse_args(argv)

    _configure_logging()
    workdir = Path.cwd()
    if not config_path().exists():
        raise SystemExit(
            f"Run discover with a readable config file: {config_path()}"
        )
    state_dir = workdir / "state"

    load_env(workdir)
    config = load_interests(workdir)
    runtime = load_runtime(workdir)
    profile = get_user_profile(workdir)
    persona = profile.persona
    search_terms = _load_search_terms(state_dir)

    runner = GeminiRunner(workdir)
    catalog = _load_catalog(state_dir)
    # Expire hard-gate rejects past the retry window so channels that grew /
    # blogs that revived get another shot at review this run.
    n_expired = _expire_hard_gate_rejects(
        catalog, window_days=runtime.discover.hard_gate_retry_window_days,
    )
    if n_expired:
        _write_catalog(catalog, state_dir)
        logger.info(
            "expired %d hard-gate rejects older than %d days — re-eligible this run",
            n_expired, runtime.discover.hard_gate_retry_window_days,
        )
    # Per-topic dedup sets: each topic only checks against its own existing
    # entries, so the same physical source (Marques Brownlee channel etc.)
    # can be admitted independently into multiple topics with independent
    # posteriors and retire decisions.
    known_keys_by_topic: dict[str, set[str]] = {}
    known_hosts_by_topic: dict[str, set[str]] = {}
    catalog_youtube_ids_by_topic: dict[str, set[str]] = {}
    for entry in catalog.sources.values():
        known_keys_by_topic.setdefault(entry.topic, set()).add(entry.catalog_key)
        if entry.platform == "youtube":
            catalog_youtube_ids_by_topic.setdefault(entry.topic, set()).add(entry.source_id)
        if entry.platform == "web":
            try:
                host = urllib.parse.urlparse(entry.url).netloc.lower()
            except Exception:  # noqa: BLE001
                host = ""
            if host:
                known_hosts_by_topic.setdefault(entry.topic, set()).add(host)
    # Filter the topic list early so logs / counts reflect what we'll actually run.
    interests_to_run = list(config.interests)
    if args.topic:
        matches = [t for t in interests_to_run if t.topic == args.topic]
        if not matches:
            available = [t.topic for t in interests_to_run]
            raise SystemExit(
                f"--topic {args.topic!r} not in openfeed.yaml (available: {available})"
            )
        interests_to_run = matches
        logger.info("filtered to single topic: %s", args.topic)
    logger.info(
        "discover starting: %d topics, %d existing sources",
        len(interests_to_run), len(catalog.sources),
    )

    total_admitted = 0
    for topic_entry in interests_to_run:
        topic = topic_entry.topic
        topic_kws = search_terms.get(topic) or {}
        platforms_to_run = [
            p for p in topic_entry.platforms
            if args.platform is None or p == args.platform
        ]
        if args.platform and not platforms_to_run:
            logger.info("--- topic: %s  platform=%s not configured, skip ---", topic, args.platform)
            continue
        logger.info("--- topic: %s  platforms=%s ---", topic, platforms_to_run)
        for platform in platforms_to_run:
            if platform in {"youtube", "x", "web", "tiktok"}:
                block = backpressure.active_block(backpressure.OPENCLI)
                if block is not None:
                    logger.warning(
                        "  %s: opencli backpressure active (%s): %s",
                        platform, block.get("reason"), block.get("detail", ""),
                    )
                    continue
            kw_list = ((topic_kws.get(platform) or {}).get("keywords") or [])
            if not kw_list:
                logger.info("  %s: no keywords, skip", platform)
                continue
            topic_known_keys = known_keys_by_topic.setdefault(topic, set())
            topic_known_hosts = known_hosts_by_topic.setdefault(topic, set())
            topic_youtube_ids = catalog_youtube_ids_by_topic.setdefault(topic, set())
            try:
                if platform == "youtube":
                    validated = _discover_youtube_for_topic(
                        topic_entry, kw_list, config, persona, runner, runtime,
                        catalog_channel_ids=topic_youtube_ids,
                    )
                elif platform == "x":
                    validated = _discover_x_for_topic(
                        topic_entry, kw_list, config, persona, runner, runtime, topic_known_keys,
                    )
                elif platform == "web":
                    validated = _discover_web_for_topic(
                        topic_entry, kw_list, config, persona, runner, runtime,
                        topic_known_keys, topic_known_hosts,
                    )
                elif platform == "tiktok":
                    validated = _discover_tiktok_for_topic(
                        topic_entry, kw_list, config, persona, runner, runtime, topic_known_keys,
                    )
                else:
                    logger.warning("  %s: unsupported platform, skip", platform)
                    continue
            except OpenCLIInfraError as exc:
                backpressure.block_lane(
                    backpressure.OPENCLI,
                    reason="infra_unavailable",
                    detail=str(exc),
                )
                logger.exception("  %s opencli infra failed for topic %s: %s", platform, topic, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("  %s discover crashed for topic %s: %s", platform, topic, exc)
                continue

            if validated:
                for vs in validated:
                    _upsert_validated(catalog, vs, topic_known_keys, topic_known_hosts)
                # Per-topic write: avoid trampling sibling topics' files
                # if another `openfeed-discover --topic <X>` is running.
                _write_catalog(catalog, state_dir, only_topic=topic)
                n_admit = sum(1 for vs in validated if vs.status == "active")
                n_reject = len(validated) - n_admit
                total_admitted += n_admit
                logger.info(
                    "  %s: %d admitted, %d LLM-rejected (persisted) — catalog size: %d",
                    platform, n_admit, n_reject, len(catalog.sources),
                )

    logger.info(
        "discover_ok: admitted=%d total, catalog size=%d",
        total_admitted, len(catalog.sources),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
