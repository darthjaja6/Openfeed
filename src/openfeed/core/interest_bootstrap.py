"""Bootstrap canonical state from `openfeed.yaml`.

Per the new architecture (PRD §2 「选择 source」+ 「生成 keywords」):

Different platforms take different paths through bootstrap because LLM
knowledge about sources varies wildly by platform:

  web / x — LLM reliably names canonical sources (Hacker News, Simon Willison,
            Karpathy, Paul Graham). Flow:
              LLM proposes seed sources → validate → sample content
              → extract per-platform keywords from samples
  youtube — LLM hallucinates niche creator handles ~50% of the time. YouTube's
            own search index is reliable though. Flow:
              LLM proposes channel-search keywords → YouTube search.list?type=channel
              → enrich candidates → LLM source review → admit passing channels
              → those channel-search keywords become the topic's youtube keywords

Outputs:
  - (persona is read directly from openfeed.yaml; no state file)
  - state/source_catalog.json all admitted sources (from both paths)
  - state/search_terms.json   keywords per (topic, platform), nested
"""
from __future__ import annotations

import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlparse

from openfeed.utils.config_files import config_path, load_env

from openfeed.clients.content import opencli
from openfeed.clients.content.browser import get_html
from openfeed.clients.llm import GeminiRunner
from openfeed.clients.content.opencli import OpenCLIError, OpenCLITransientError
from openfeed.core.bootstrap_io import (
    empty_keyword_slots,
    load_seed_sources,
    load_validated_from_catalog,
    load_youtube_candidates,
    load_youtube_channel_keywords,
    merge_search_terms,
    write_interests_yaml,
    write_search_terms,
    write_seed_sources,
    write_source_catalog,
    write_youtube_candidates,
    write_youtube_channel_keywords,
)
from openfeed.core.judgment_ledger import attach_file as _attach_ledger, emit_judgment
from openfeed.core.youtube_source_review import (
    YouTubeDiscoverParams,
    discover_youtube_candidates,
    extract_youtube_frames,
    review_youtube_candidates,
)
from openfeed.models.interests import InterestEntry, InterestsConfig, load_interests
from openfeed.models.persona import PersonaOutput
from openfeed.models.seed_source import SeedSource, TopicPlatformSources
from openfeed.models.validated_source import ValidatedSource
from openfeed.prompts.interest_bootstrap import (
    KeywordsOutput,
    TopicTemporalOutput,
    TopicYouTubeChannelKeywords,
    build_keywords_from_topic_prompt,
    build_keywords_from_samples_prompt,
    build_seed_sources_prompt,
    build_topic_temporal_prompt,
    build_youtube_channel_keywords_prompt,
)
from openfeed.utils.state_io import atomic_write_json
from openfeed.utils.logging_setup import configure_task_logging
from openfeed.utils.html_parse import (
    estimate_body_text_length,
    extract_body_text,
    extract_same_domain_links,
    extract_title,
)


_SYSTEM_MESSAGE = "Return only one compact JSON object. No reasoning, no markdown."
_BOOTSTRAP_REASON_CODE = "bootstrap_seed_validated"
_YT_BOOTSTRAP_REASON_CODE = "bootstrap_youtube_search_admitted"
_LEDGER_PATH = Path("ledgers/source_judgments.jsonl")  # legacy ledger; frozen

# YouTube retrieval budget for bootstrap (kept conservative for quota); the
# discover path reads its own values from runtime config. `admit_reason_code`
# tags every admitted channel's provenance in the catalog + ledger.
_YT_BOOTSTRAP_PARAMS = YouTubeDiscoverParams(
    keywords_per_topic=5,
    results_per_keyword=5,
    oversample_multiplier=2,      # bootstrap's legacy budget — pull 10 per keyword
    min_subscribers=1000,
    admit_reason_code=_YT_BOOTSTRAP_REASON_CODE,
)

logger = logging.getLogger("interest_bootstrap")


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
    run_log = configure_task_logging("interest_bootstrap")
    _attach_ledger(_LEDGER_PATH)
    logger.info("run log → %s", run_log)
    logger.info("ledger → %s", _LEDGER_PATH)


# ---------------------------------------------------------------------------
# Step 1.5: per-topic temporal knobs (patrol / max_age / half_life)
# ---------------------------------------------------------------------------


def _topic_needs_temporal(entry: InterestEntry) -> bool:
    """True iff at least one of the temporal fields is unset."""
    return (
        entry.max_content_age_days is None
        or entry.freshness_half_life_days is None
    )


def infer_topic_temporal(
    config: InterestsConfig, persona: PersonaOutput, runner: GeminiRunner
) -> dict[str, TopicTemporalOutput]:
    """One parallel LLM call per topic with at least one unset temporal field.

    Returns {topic_name: TopicTemporalOutput}. Topics whose three fields are
    already set in openfeed.yaml are skipped entirely — no call.
    """
    pending = [t for t in config.interests if _topic_needs_temporal(t)]
    if not pending:
        logger.info("topic_temporal: all topics already populated — skipping LLM")
        return {}

    def _call(topic: str, topic_description: str) -> tuple[str, TopicTemporalOutput]:
        raw = runner.run_json(
            [
                {"role": "system", "content": _SYSTEM_MESSAGE},
                {
                    "role": "user",
                    "content": build_topic_temporal_prompt(
                        topic=topic,
                        topic_description=topic_description,
                        persona=persona,
                    ),
                },
            ],
            schema=TopicTemporalOutput.model_json_schema(),
            schema_name=TopicTemporalOutput.__name__,
        )
        parsed = TopicTemporalOutput.model_validate(raw)
        logger.info(
            "topic_temporal[%s]: max_age=%dd half_life=%dd | %s",
            topic, parsed.max_content_age_days, parsed.freshness_half_life_days,
            parsed.reasoning,
        )
        return topic, parsed

    out: dict[str, TopicTemporalOutput] = {}
    failures: list[tuple[str, Exception]] = []
    with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
        futures = {pool.submit(_call, t.topic, t.description): t.topic for t in pending}
        for future in as_completed(futures):
            topic = futures[future]
            try:
                topic_name, result = future.result()
                out[topic_name] = result
            except Exception as exc:  # noqa: BLE001 — one topic failure shouldn't sink rest
                logger.warning("topic_temporal LLM failed for %s: %s", topic, exc)
                failures.append((topic, exc))
    if failures and not out:
        raise RuntimeError(f"All topic_temporal LLM calls failed: {failures}")
    return out


def _merge_topic_temporal(
    config: InterestsConfig, llm_out: dict[str, TopicTemporalOutput]
) -> InterestsConfig:
    """Fill unset fields from llm_out; user-set values always win."""
    for entry in config.interests:
        proposal = llm_out.get(entry.topic)
        if proposal is None:
            continue
        if entry.max_content_age_days is None:
            entry.max_content_age_days = proposal.max_content_age_days
        if entry.freshness_half_life_days is None:
            entry.freshness_half_life_days = proposal.freshness_half_life_days
    return config


# ---------------------------------------------------------------------------
# Step 2a: seed sources for web + x
# ---------------------------------------------------------------------------


def infer_web_x_seed_sources(
    config: InterestsConfig, persona: PersonaOutput, runner: GeminiRunner
) -> list[TopicPlatformSources]:
    """One LLM call per (topic, web|x) combo, run in parallel. Each call's
    LLM schema is `TopicPlatformSources` directly — LLM echoes back the
    topic+platform we asked for; we override with our values to ignore drift."""
    # combos: (topic, topic_description, platform, language_preferences)
    combos: list[tuple[str, str, str, list[str]]] = []
    for entry in config.interests:
        for platform in entry.platforms:
            if platform == "youtube":
                continue  # youtube has its own keyword-search path
            combos.append((entry.topic, entry.description, platform, entry.language_preferences))
    if not combos:
        return []

    def _call(topic: str, topic_description: str, platform: str,
              language_preferences: list[str]) -> TopicPlatformSources:
        raw = runner.run_json(
            [
                {"role": "system", "content": _SYSTEM_MESSAGE},
                {
                    "role": "user",
                    "content": build_seed_sources_prompt(
                        topic=topic,
                        topic_description=topic_description,
                        platform=platform,
                        persona=persona,
                        language_preferences=language_preferences,
                    ),
                },
            ],
            schema=TopicPlatformSources.model_json_schema(),
            schema_name=TopicPlatformSources.__name__,
        )
        parsed = TopicPlatformSources.model_validate(raw)
        # ignore LLM-supplied topic/platform; trust what we passed in
        result = TopicPlatformSources(topic=topic, platform=platform, sources=parsed.sources)  # type: ignore[arg-type]
        logger.info("seed_sources[%s × %s]: %d sources", topic, platform, len(result.sources))
        return result

    items: list[TopicPlatformSources] = []
    failures: list[tuple[str, str, Exception]] = []
    with ThreadPoolExecutor(max_workers=min(8, len(combos))) as pool:
        futures = {pool.submit(_call, t, d, p, lp): (t, p) for t, d, p, lp in combos}
        for future in as_completed(futures):
            topic, platform = futures[future]
            try:
                items.append(future.result())
            except Exception as exc:  # noqa: BLE001 — single combo failure shouldn't sink whole step
                logger.warning("seed_sources LLM failed for %s × %s: %s", topic, platform, exc)
                failures.append((topic, platform, exc))
    if failures and not items:
        raise RuntimeError(f"All seed_sources LLM calls failed: {failures}")
    return items


# ---------------------------------------------------------------------------
# Step 2b: YouTube channel-search keywords
# ---------------------------------------------------------------------------


def infer_youtube_channel_keywords(
    config: InterestsConfig, persona: PersonaOutput, runner: GeminiRunner
) -> list[TopicYouTubeChannelKeywords]:
    """One LLM call per youtube topic, run in parallel. Mirrors `infer_web_x_seed_sources`."""
    yt_topics = [i for i in config.interests if "youtube" in i.platforms]
    if not yt_topics:
        return []

    def _call(topic: str, topic_description: str,
              language_preferences: list[str]) -> TopicYouTubeChannelKeywords:
        raw = runner.run_json(
            [
                {"role": "system", "content": _SYSTEM_MESSAGE},
                {
                    "role": "user",
                    "content": build_youtube_channel_keywords_prompt(
                        topic=topic,
                        topic_description=topic_description,
                        persona=persona,
                        language_preferences=language_preferences,
                    ),
                },
            ],
            schema=TopicYouTubeChannelKeywords.model_json_schema(),
            schema_name=TopicYouTubeChannelKeywords.__name__,
        )
        parsed = TopicYouTubeChannelKeywords.model_validate(raw)
        # ignore LLM-supplied topic; trust what we passed in
        result = TopicYouTubeChannelKeywords(topic=topic, keywords=parsed.keywords)
        logger.info("yt_keywords[%s]: %d keywords", topic, len(result.keywords))
        return result

    items: list[TopicYouTubeChannelKeywords] = []
    failures: list[tuple[str, Exception]] = []
    with ThreadPoolExecutor(max_workers=min(8, len(yt_topics))) as pool:
        futures = {
            pool.submit(_call, t.topic, t.description, t.language_preferences): t.topic
            for t in yt_topics
        }
        for future in as_completed(futures):
            topic = futures[future]
            try:
                items.append(future.result())
            except Exception as exc:  # noqa: BLE001
                logger.warning("yt_keywords LLM failed for %s: %s", topic, exc)
                failures.append((topic, exc))
    if failures and not items:
        raise RuntimeError(f"All yt_keywords LLM calls failed: {failures}")
    return items


# ---------------------------------------------------------------------------
# Step 3a: validate web + x seed sources in parallel
# ---------------------------------------------------------------------------


def validate_web_x_seed_sources(seed_sources: list[TopicPlatformSources]) -> list[ValidatedSource]:
    jobs: list[tuple[str, str, SeedSource]] = []
    for item in seed_sources:
        for source in item.sources:
            jobs.append((item.topic, item.platform, source))
    if not jobs:
        return []
    logger.info("validating %d web/x seed sources in parallel...", len(jobs))

    def _do_one(topic: str, platform: str, seed: SeedSource) -> ValidatedSource | None:
        try:
            if platform == "web":
                return _validate_web(topic, seed)
            if platform == "x":
                return _validate_x_via_opencli(topic, seed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("validator crashed for %s/%s: %s", platform, seed.identifier, exc)
            return None
        return None

    validated: list[ValidatedSource] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_do_one, topic, platform, seed): (topic, platform, seed) for topic, platform, seed in jobs}
        for future in as_completed(futures):
            topic, platform, seed = futures[future]
            result = future.result()
            if result is None:
                logger.info("drop  %s × %s → %s (%s)", topic, platform, seed.identifier, seed.name)
            else:
                validated.append(result)
                logger.info(
                    "keep  %s × %s → %s (%d samples)",
                    topic, platform, result.canonical_id, len(result.sample_titles) + len(result.sample_snippets),
                )
    return validated


def _validate_web(topic: str, seed: SeedSource) -> ValidatedSource | None:
    url = seed.identifier.strip()
    seed_meta = {"seed_proposal_url": url, "seed_proposal_name": seed.name, "seed_proposal_why": seed.reason}
    def _reject(reason_code: str, **extra: Any) -> None:
        emit_judgment(event_type="reject_source", platform="web", topic=topic,
                       source_id=url, source_name=seed.name,
                       reason_code=reason_code, evidence={**seed_meta, **extra})

    if not url.startswith(("http://", "https://")):
        logger.warning("web seed identifier not a URL: %s", url)
        _reject("invalid_url")
        return None
    try:
        status, html = get_html(url, timeout=20)
    except Exception as exc:  # noqa: BLE001
        logger.debug("web fetch failed for %s: %s", url, exc)
        _reject("fetch_error", error=str(exc)[:200])
        return None
    if status != 200:
        _reject("non_200", status=status)
        return None
    if not html or estimate_body_text_length(html) < 500:
        _reject("body_too_short", body_len=estimate_body_text_length(html or ""))
        return None
    same_domain = extract_same_domain_links(html, url, max_links=30)
    if len(same_domain) < 5:
        _reject("too_few_same_domain_links", links=len(same_domain))
        return None
    sample_titles = [link["anchor_text"] for link in same_domain if link["anchor_text"]][:15]
    canonical = _canonicalise_url(url)
    page_title = extract_title(html)

    article_candidates = [
        link["url"] for link in same_domain
        if link["url"] != url and link["url"] != canonical
    ][:3]
    sample_bodies: list[str] = []
    for article_url in article_candidates:
        try:
            a_status, a_html = get_html(article_url, timeout=20)
        except Exception as exc:  # noqa: BLE001
            logger.debug("article fetch failed for %s: %s", article_url, exc)
            continue
        if a_status != 200 or not a_html:
            continue
        body = extract_body_text(a_html, max_chars=1200)
        if body and len(body) >= 200:
            sample_bodies.append(body)

    emit_judgment(
        event_type="admit_source", platform="web", topic=topic,
        source_id=canonical, source_name=page_title or seed.name,
        reason_code=_BOOTSTRAP_REASON_CODE,
        evidence={**seed_meta, "sample_title_count": len(sample_titles),
                  "sample_body_count": len(sample_bodies)},
    )
    return ValidatedSource(
        seed=seed,
        topic=topic,
        platform="web",
        canonical_id=canonical,
        canonical_name=page_title or seed.name,
        url=canonical,
        reason_code=_BOOTSTRAP_REASON_CODE,
        sample_titles=sample_titles,
        sample_snippets=sample_bodies,
        metadata={"original_url": url},
    )


def _canonicalise_url(url: str) -> str:
    cleaned = urldefrag(url).url.rstrip("/")
    parsed = urlparse(cleaned)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _validate_x_via_opencli(topic: str, seed: SeedSource) -> ValidatedSource | None:
    handle = seed.identifier.lstrip("@").strip()
    seed_meta = {"seed_proposal_handle": seed.identifier, "seed_proposal_name": seed.name, "seed_proposal_why": seed.reason}
    def _reject(reason_code: str, **extra: Any) -> None:
        emit_judgment(event_type="reject_source", platform="x", topic=topic,
                       source_id=handle or seed.identifier, source_name=seed.name,
                       reason_code=reason_code, evidence={**seed_meta, **extra})

    if not handle:
        _reject("invalid_handle")
        return None
    try:
        profile = opencli.twitter_profile(handle)
    except OpenCLITransientError as exc:
        logger.warning("transient twitter profile failed for %s; skip seed without rejecting: %s", handle, exc)
        return None
    except OpenCLIError as exc:
        logger.debug("twitter profile failed for %s: %s", handle, exc)
        _reject("profile_lookup_failed", **exc.evidence())
        return None
    screen_name = str(profile.get("screen_name") or "").strip()
    if not screen_name:
        _reject("profile_not_found")
        return None  # not a real account

    try:
        tweets = opencli.twitter_user_timeline(handle, limit=10)
    except OpenCLITransientError as exc:
        logger.warning("transient twitter timeline failed for %s; continuing without tweets: %s", handle, exc)
        tweets = []
    except OpenCLIError as exc:
        logger.warning("twitter timeline failed for %s: %s", handle, exc)
        tweets = []

    sample_snippets = [t.get("text", "")[:800] for t in tweets if t.get("text")]
    emit_judgment(
        event_type="admit_source", platform="x", topic=topic,
        source_id=screen_name, source_name=str(profile.get("name") or seed.name),
        reason_code=_BOOTSTRAP_REASON_CODE,
        evidence={
            **seed_meta,
            "bio": str(profile.get("bio") or "")[:300],
            "followers": int(profile.get("followers") or 0),
            "sample_tweet_count": len(sample_snippets),
        },
    )
    return ValidatedSource(
        seed=seed,
        topic=topic,
        platform="x",
        canonical_id=screen_name,
        canonical_name=str(profile.get("name") or seed.name),
        url=f"https://x.com/{screen_name}",
        reason_code=_BOOTSTRAP_REASON_CODE,
        sample_titles=[],
        sample_snippets=sample_snippets,
        metadata={
            "bio": str(profile.get("bio") or ""),
            "followers": int(profile.get("followers") or 0),
            "tweets_count": int(profile.get("tweets") or 0),
        },
    )


# ---------------------------------------------------------------------------
# Step 4: per-(topic, platform) keyword generation
# ---------------------------------------------------------------------------


def generate_keywords_per_platform(
    config: InterestsConfig,
    persona: PersonaOutput,
    validated: list[ValidatedSource],
    runner: GeminiRunner,
    *,
    only_slots: set[tuple[str, str]] | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Returns {topic: {platform: [keywords]}}.

    All platforms (web / x / youtube) go through the same path: use the
    admitted sources' content samples if we have any, otherwise fall back to
    topic-description-only generation.

    If `only_slots` is given, only generate for those (topic, platform) pairs;
    useful for incremental regeneration.
    """
    # Keyword generation only samples from ADMITTED sources; rejected entries
    # now show up in the catalog with status="rejected" but shouldn't pollute
    # the keyword-content signal.
    by_topic_platform: dict[tuple[str, str], list[ValidatedSource]] = {}
    for vs in validated:
        if vs.status != "active":
            continue
        by_topic_platform.setdefault((vs.topic, vs.platform), []).append(vs)

    out: dict[str, dict[str, list[str]]] = {i.topic: {} for i in config.interests}
    llm_jobs: list[tuple[str, str, str]] = []  # (topic, platform, prompt)
    for topic_entry in config.interests:
        topic = topic_entry.topic
        for platform in topic_entry.platforms:
            if only_slots is not None and (topic, platform) not in only_slots:
                continue
            topic_sources = by_topic_platform.get((topic, platform), [])
            samples = _build_samples_for_prompt(topic_sources)
            if samples:
                logger.info(
                    "keywords[%s×%s]: from %d sources, %d sample lines",
                    topic, platform, len(topic_sources), len(samples),
                )
                prompt = build_keywords_from_samples_prompt(
                    topic_data=topic_entry,
                    platform=platform,
                    persona=persona,
                    language_preferences=topic_entry.language_preferences,
                    source_samples=samples,
                )
            else:
                logger.info("keywords[%s×%s]: topic-only fallback", topic, platform)
                prompt = build_keywords_from_topic_prompt(
                    topic=topic,
                    topic_description=topic_entry.description,
                    platform=platform,
                    persona=persona,
                    language_preferences=topic_entry.language_preferences,
                )
            llm_jobs.append((topic, platform, prompt))

    if not llm_jobs:
        return out

    def _call(topic: str, platform: str, prompt: str) -> tuple[str, str, list[str]]:
        raw = runner.run_json(
            [
                {"role": "system", "content": _SYSTEM_MESSAGE},
                {"role": "user", "content": prompt},
            ],
            schema=KeywordsOutput.model_json_schema(),
            schema_name=KeywordsOutput.__name__,
        )
        return topic, platform, KeywordsOutput.model_validate(raw).keywords

    with ThreadPoolExecutor(max_workers=min(8, len(llm_jobs))) as pool:
        futures = [pool.submit(_call, topic, platform, prompt) for topic, platform, prompt in llm_jobs]
        for future in as_completed(futures):
            try:
                topic, platform, keywords = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("keyword-gen LLM call failed: %s", exc)
                continue
            out[topic][platform] = keywords
    return out


def _build_samples_for_prompt(topic_sources: list[ValidatedSource]) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    for vs in topic_sources:
        for title in vs.sample_titles[:8]:
            samples.append({"source": vs.canonical_name, "platform": vs.platform, "title": title})
        for snippet in vs.sample_snippets[:5]:
            samples.append({"source": vs.canonical_name, "platform": vs.platform, "body": snippet})
    return samples


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    del argv
    _configure_logging()
    workdir = Path.cwd()
    if not config_path().exists():
        raise SystemExit(
            f"Run interest-bootstrap with a readable config file: {config_path()}"
        )
    state_dir = workdir / "state"
    config = load_interests(workdir)
    load_env(workdir)
    runner = GeminiRunner(workdir)

    seed_sources_path = state_dir / "seed_sources.json"
    yt_keywords_path = state_dir / "youtube_channel_keywords.json"
    yt_candidates_path = state_dir / "youtube_candidates.json"
    catalog_dir = state_dir / "source_catalog"
    keywords_path = state_dir / "search_terms.json"

    # --- Step 1: persona ----------------------------------------------------
    # Persona is user-written in openfeed.yaml; downstream loaders
    # (`get_user_profile`, `load_persona`) read directly from there. Nothing
    # to write here.
    persona = PersonaOutput.model_validate(config.persona)
    logger.info("step 1: persona loaded from openfeed.yaml")

    # --- Step 1.5: per-topic temporal knobs ---------------------------------
    # Only topics with at least one unset field trigger an LLM call.
    # Merge results back into `config` and persist to openfeed.yaml;
    # downstream steps read the enriched object in-memory.
    topics_needing_temporal = [t.topic for t in config.interests if _topic_needs_temporal(t)]
    if topics_needing_temporal:
        logger.info(
            "step 1.5: temporal inference for %d topic(s): %s",
            len(topics_needing_temporal), topics_needing_temporal,
        )
        temporal_out = infer_topic_temporal(config, persona, runner)
        config = _merge_topic_temporal(config, temporal_out)
        write_interests_yaml(config, workdir)
    else:
        logger.info("step 1.5: all topics have temporal fields set, skipping")

    # --- Step 2: seed sources + youtube channel keywords --------------------
    if seed_sources_path.exists():
        logger.info("step 2a: reuse %s (delete to regenerate)", seed_sources_path.name)
        web_x_seeds = load_seed_sources(seed_sources_path)
    else:
        logger.info("step 2a: web/x seed sources (parallel LLM calls)")
        web_x_seeds = infer_web_x_seed_sources(config, persona, runner)
        write_seed_sources(web_x_seeds, state_dir)

    if yt_keywords_path.exists():
        logger.info("step 2b: reuse %s (delete to regenerate)", yt_keywords_path.name)
        yt_keywords = load_youtube_channel_keywords(yt_keywords_path)
    else:
        logger.info("step 2b: youtube channel-search keywords (LLM)")
        yt_keywords = infer_youtube_channel_keywords(config, persona, runner)
        write_youtube_channel_keywords(yt_keywords, state_dir)

    logger.info(
        "  web/x seed entries=%d, youtube topics=%d",
        len(web_x_seeds), len(yt_keywords),
    )

    # --- Step 3: discovery + validation -------------------------------------
    # 3a: youtube candidates via opencli (the slow part — persist before review)
    yt_hard_gate_rejects: list[ValidatedSource] = []
    if yt_candidates_path.exists():
        logger.info("step 3a: reuse %s (delete to regenerate)", yt_candidates_path.name)
        yt_candidates = load_youtube_candidates(yt_candidates_path)
    else:
        logger.info("step 3a: youtube candidate discovery (opencli search → resolve)")
        yt_candidates, yt_hard_gate_rejects = discover_youtube_candidates(
            yt_keywords, config, params=_YT_BOOTSTRAP_PARAMS,
        )
        write_youtube_candidates(yt_candidates, state_dir)
    logger.info("  yt candidates: %d (hard-gate rejects: %d)", len(yt_candidates), len(yt_hard_gate_rejects))

    # 3a-bis: download videos + extract frames per candidate (cached in
    # logs/video_frames/, frame paths persisted back into youtube_candidates.json)
    n_new = extract_youtube_frames(yt_candidates)
    if n_new > 0:
        write_youtube_candidates(yt_candidates, state_dir)

    # 3b: validation + LLM review → catalog
    if catalog_dir.exists() and any(catalog_dir.glob("*.json")):
        logger.info("step 3b: reuse %s (delete to regenerate)", catalog_dir.name)
        all_validated = load_validated_from_catalog(state_dir)
        logger.info("  loaded %d validated sources from catalog", len(all_validated))
    else:
        logger.info("step 3b-i: validate web/x seed sources")
        web_x_validated = validate_web_x_seed_sources(web_x_seeds)
        logger.info("step 3b-ii: LLM review youtube candidates")
        yt_validated = review_youtube_candidates(yt_candidates, config, persona, runner, params=_YT_BOOTSTRAP_PARAMS)
        all_validated = web_x_validated + yt_validated + yt_hard_gate_rejects
        logger.info(
            "validation summary: web/x=%d youtube_admits_plus_llm_rejects=%d youtube_hard_gate_rejects=%d total=%d",
            len(web_x_validated), len(yt_validated), len(yt_hard_gate_rejects), len(all_validated),
        )
        write_source_catalog(all_validated, state_dir)

    # --- Step 4: keywords (incremental per (topic, platform) slot) -----------
    existing_keywords = None
    if keywords_path.exists():
        existing_keywords = json.loads(keywords_path.read_text(encoding="utf-8"))
    empty_slots = empty_keyword_slots(config, existing_keywords)
    if not empty_slots:
        logger.info("step 4: all (topic, platform) slots populated, skipping")
    else:
        logger.info(
            "step 4: generating keywords for %d empty slot(s): %s",
            len(empty_slots), sorted(empty_slots),
        )
        new_keywords = generate_keywords_per_platform(
            config, persona, all_validated, runner, only_slots=empty_slots,
        )
        merged = merge_search_terms(config, existing_keywords, new_keywords)
        atomic_write_json(keywords_path, merged)

    logger.info(
        "interest_bootstrap_ok topics=%d validated_sources=%d → %s",
        len(config.interests), len(all_validated), state_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
