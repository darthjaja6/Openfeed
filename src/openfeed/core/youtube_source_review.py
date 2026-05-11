"""YouTube source discovery + multimodal review pipeline.

Extracted from interest_bootstrap.py so both bootstrap and ongoing discover
can share it. Behaviour is unchanged; callers pass policy knobs via
`YouTubeDiscoverParams`:

  - `keywords_per_topic` / `results_per_keyword` — retrieval budget
  - `min_subscribers` — hard gate before the LLM review
  - `admit_reason_code` — the string recorded on ValidatedSource and catalog
    entries when a channel is admitted (bootstrap uses
    "bootstrap_youtube_search_admitted"; discover uses a different value)

The three public entry points mirror the original flow:

  1. `discover_youtube_candidates(yt_keywords, config, *, params)` — opencli
     keyword search → unique channel set → enrich → subscriber hard gate.
  2. `extract_youtube_frames(candidates)` — yt-dlp + ffmpeg frame extraction
     for the multimodal review.
  3. `review_youtube_candidates(candidates, config, persona, runner, *, params)`
     — per-candidate two-pass LLM review (understanding → decision).
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tqdm import tqdm

from openfeed.clients.content import opencli
from openfeed.clients.llm import GeminiRunner
from openfeed.clients.content.opencli import OpenCLIError, OpenCLITransientError
from openfeed.utils.video_frames import (
    extract_youtube_frames as extract_youtube_frames_for_url,
    parse_duration_seconds,
    parse_views,
)
from openfeed.core.judgment_ledger import emit_judgment
from openfeed.models.interests import InterestEntry, InterestsConfig
from openfeed.models.source import VideoFrames, YouTubeCandidate
from openfeed.models.persona import PersonaOutput
from openfeed.models.validated_source import ValidatedSource
from openfeed.prompts.content_understanding import (
    ContentUnderstanding,
    build_content_understanding_prompt,
)
from openfeed.prompts.interest_bootstrap import TopicYouTubeChannelKeywords
from openfeed.prompts.source_review import (
    SourceReviewDecision,
    build_source_review_prompt,
    flatten_decision_reasoning,
)


logger = logging.getLogger("youtube_source_review")

_YT_VIDEO_ID_PAT = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")
_YT_DISCOVERY_VIDEOS_PER_CHANNEL = 3
_TOP_VIDEOS_PER_CANDIDATE = 3


@dataclass(frozen=True)
class _TransientOpenCLIResult:
    error: OpenCLITransientError


@dataclass(frozen=True)
class YouTubeDiscoverParams:
    """Policy knobs for a YouTube discover / bootstrap run.

    `oversample_multiplier` is how many raw search hits we pull per keyword
    relative to `results_per_keyword` — e.g. with 2 / 10 we ask opencli for 20
    per keyword, dedup, and expect roughly `results_per_keyword` to survive.
    Bootstrap keeps the legacy value of 2 to preserve its original budget.
    """

    keywords_per_topic: int
    results_per_keyword: int
    min_subscribers: int
    admit_reason_code: str
    oversample_multiplier: int = 2


# ---------------------------------------------------------------------------
# Stage 1 + 2: keyword search → unique channel set → enrich → hard gate
# ---------------------------------------------------------------------------


def discover_youtube_candidates(
    yt_keywords: list[TopicYouTubeChannelKeywords],
    config: InterestsConfig,
    *,
    params: YouTubeDiscoverParams,
    catalog_channel_ids: set[str] | None = None,
) -> tuple[list[YouTubeCandidate], list[ValidatedSource]]:
    """Keyword search → resolve → dedup + subs gate → full detail. No LLM.

    Returns (survivors, hard_gate_rejects):
      - survivors: channels that passed all gates; pass to frames + LLM review
      - hard_gate_rejects: channels rejected by the subs hard gate, materialised
        as ValidatedSource(status="rejected", reason_code="low_subscribers").
        Caller upserts them to the catalog so future runs dedup them until the
        retry window expires (see `_expire_hard_gate_rejects`).

    Other reject paths (`channel_resolve_failed`, `channel_detail_failed`) are
    infrastructure failures — they have no stable channel_id (resolve) or are
    transient (detail), so they stay ledger-only.

    Pipeline:
      Stage 1  — `opencli youtube search` per keyword, dedup within-search by
                 channel name (a single channel often contributes multiple
                 videos to the top-N; don't resolve it more than once).
      Stage 2A — for each unique name, call `opencli youtube_video(bait_url)`
                 through the local OpenCLI service. This is
                 where channel_id + subscribers first become available, so:
                   - catalog_channel_ids dedup (precise, no name-collision risk)
                   - subs hard gate (`min_subscribers`)
      Stage 2B — for each survivor, `opencli youtube_channel(channel_id)` to
                 pull recent uploads for the LLM-review sample. Only runs for
                 channels that passed the earlier gates, so we never pay call 2
                 for known-bad sources.
    """
    if not yt_keywords:
        return [], []
    topic_by_topic = {i.topic: i for i in config.interests}
    jobs = [lk for lk in yt_keywords if lk.topic in topic_by_topic]

    # ---- Stage 1: keyword search + within-search name dedup -------------
    enrich_jobs: list[tuple[str, str, str, list[str]]] = []
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(jobs)))) as pool:
        stage1_fn = _make_stage1(params)
        for topic_kw, stage1 in zip(jobs, pool.map(stage1_fn, jobs)):
            enrich_jobs.extend(stage1)
    logger.info(
        "yt_discover: %d unique names across %d topics → resolving",
        len(enrich_jobs), len(jobs),
    )
    if not enrich_jobs:
        return [], []

    # ---- Stage 2A: cheap resolve → channel_id + subs; gate on both ------
    known_ids = set(catalog_channel_ids or ())
    resolved: list[dict[str, Any]] = []
    hard_gate_rejects: list[ValidatedSource] = []
    n_skipped_catalog = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_resolve_channel, bait): (topic, name, bait, matched)
            for topic, name, bait, matched in enrich_jobs
        }
        for future in tqdm(as_completed(futures), total=len(futures), desc="yt_resolve", unit="ch"):
            topic, name, bait, matched = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.debug("yt_resolve crashed for %s/%s: %s", topic, name, exc)
                result = None
            if isinstance(result, _TransientOpenCLIResult):
                logger.warning(
                    "yt_resolve transient opencli failure for %s/%s; skip without rejecting source: %s",
                    topic,
                    name,
                    result.error,
                )
                continue
            if result is None:
                emit_judgment(
                    event_type="reject_source", platform="youtube", topic=topic,
                    source_id=name, source_name=name,
                    reason_code="channel_resolve_failed",
                    matched_keywords=matched, evidence={"bait_url": bait},
                )
                continue
            channel_id, video_meta = result
            if channel_id in known_ids:
                n_skipped_catalog += 1
                continue  # already judged; ledger has the original decision
            known_ids.add(channel_id)  # protect against same channel resolved twice in-run
            subs_str = str(video_meta.get("subscribers") or "")
            if parse_views(subs_str) < params.min_subscribers:
                emit_judgment(
                    event_type="reject_source", platform="youtube", topic=topic,
                    source_id=channel_id, source_name=name,
                    reason_code="low_subscribers",
                    matched_keywords=matched,
                    evidence={"subscribers": subs_str},
                )
                hard_gate_rejects.append(ValidatedSource(
                    seed=None,
                    topic=topic,
                    platform="youtube",
                    canonical_id=channel_id,
                    canonical_name=name,
                    url=f"https://www.youtube.com/channel/{channel_id}",
                    reason_code="low_subscribers",
                    status="rejected",
                    matched_keywords=list(matched),
                    metadata={"subscribers": subs_str},
                ))
                continue
            resolved.append({
                "topic": topic, "name": name, "channel_id": channel_id,
                "video_meta": video_meta, "matched_keywords": matched,
            })
    logger.info(
        "yt_discover: %d passed dedup + subs gate (>= %d); %d skipped as already in catalog; %d hard-gate rejects",
        len(resolved), params.min_subscribers, n_skipped_catalog, len(hard_gate_rejects),
    )
    if not resolved:
        return [], hard_gate_rejects

    # ---- Stage 2B: full detail for survivors ----------------------------
    candidates: list[YouTubeCandidate] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_channel_detail, r): r for r in resolved}
        for future in tqdm(as_completed(futures), total=len(futures), desc="yt_detail", unit="ch"):
            r = futures[future]
            try:
                cand = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.debug("yt_detail crashed for %s/%s: %s", r["topic"], r["name"], exc)
                cand = None
            if isinstance(cand, _TransientOpenCLIResult):
                logger.warning(
                    "yt_detail transient opencli failure for %s/%s; skip without rejecting source: %s",
                    r["topic"],
                    r["name"],
                    cand.error,
                )
                continue
            if cand is None:
                emit_judgment(
                    event_type="reject_source", platform="youtube", topic=r["topic"],
                    source_id=r["channel_id"], source_name=r["name"],
                    reason_code="channel_detail_failed",
                    matched_keywords=r["matched_keywords"], evidence={},
                )
                continue
            candidates.append(cand)
    return candidates, hard_gate_rejects


def _make_stage1(params: YouTubeDiscoverParams):
    """Bind `params` into the stage-1 closure so pool.map can send the plain
    TopicYouTubeChannelKeywords argument per job."""

    def _run(topic_kw: TopicYouTubeChannelKeywords) -> list[tuple[str, str, str, list[str]]]:
        return _discover_stage1(topic_kw, params)

    return _run


def _discover_stage1(
    topic_kw: TopicYouTubeChannelKeywords,
    params: YouTubeDiscoverParams,
) -> list[tuple[str, str, str, list[str]]]:
    """Per-topic stage 1: keyword search → unique names (within-search dedup).

    Catalog dedup is deferred to stage 2A (channel_id level) because channel
    NAMES aren't unique YouTube identifiers and opencli search doesn't surface
    channel_id here.

    Returns list of (topic, channel_name, bait_video_url, matched_keywords).
    """
    topic = topic_kw.topic
    keywords_to_run = topic_kw.keywords[: params.keywords_per_topic]
    pull_limit = params.results_per_keyword * params.oversample_multiplier
    logger.info(
        "yt_discover[%s]: %d keywords via opencli (pull %d each)",
        topic, len(keywords_to_run), pull_limit,
    )

    bait_url: dict[str, str] = {}
    matched: dict[str, list[str]] = {}
    for keyword in keywords_to_run:
        try:
            # No --type filter: opencli's `--type shorts` hits search results
            # ranked for the regular feed (sp=EgIQCQ), which under-recalls and
            # mixes noise. Wide search returns relevant CHANNELS regardless of
            # whether they post long-form or Shorts; per-channel Shorts pull
            # happens at patrol time (see core/patrol.py).
            results = opencli.youtube_search(keyword, limit=pull_limit)
        except OpenCLITransientError as exc:
            logger.warning("youtube search %r transient opencli failure; skip keyword: %s", keyword, exc)
            continue
        except OpenCLIError as exc:
            logger.warning("youtube search %r failed: %s", keyword, exc)
            continue
        for r in results:
            name = (r.get("channel") or "").strip()
            url = (r.get("url") or "").strip()
            if not name or not url:
                continue
            if name in bait_url:  # same channel in multiple ranks → resolve once
                kws = matched[name]
                if keyword not in kws:
                    kws.append(keyword)
                continue
            bait_url[name] = url
            matched[name] = [keyword]
    logger.info("  yt_discover[%s]: %d unique names from search", topic, len(bait_url))
    return [(topic, name, bait_url[name], list(matched[name])) for name in bait_url]


def _resolve_channel(bait_url: str) -> tuple[str, dict[str, Any]] | None:
    """Stage 2A worker: one opencli `youtube_video` call. Returns
    (channel_id, video_meta) or None if the video can't be resolved / has no
    channelId. video_meta carries subscribers + description + keywords which
    we reuse in stage 2B as fallbacks."""
    try:
        video_meta = opencli.youtube_video(bait_url)
    except OpenCLITransientError as exc:
        logger.warning("youtube_video transient opencli failure for %s: %s", bait_url, exc)
        return _TransientOpenCLIResult(error=exc)
    except OpenCLIError as exc:
        logger.debug("youtube_video failed for %s: %s", bait_url, exc)
        return None
    channel_id = str(video_meta.get("channelId") or "").strip()
    if not channel_id:
        return None
    return channel_id, video_meta


def _fetch_channel_detail(resolved: dict[str, Any]) -> YouTubeCandidate | None:
    """Stage 2B worker: one opencli `youtube_channel` call for recent uploads
    + full metadata. Only invoked for channels that cleared dedup + subs gate,
    so we never pay this cost for known or too-small channels."""
    channel_id = resolved["channel_id"]
    try:
        ch = opencli.youtube_channel(channel_id, limit=_YT_DISCOVERY_VIDEOS_PER_CHANNEL)
    except OpenCLITransientError as exc:
        logger.warning("youtube_channel transient opencli failure for %s: %s", channel_id, exc)
        return _TransientOpenCLIResult(error=exc)
    except OpenCLIError as exc:
        logger.debug("youtube_channel failed for %s: %s", channel_id, exc)
        return None
    meta = ch.get("metadata") or {}
    recent = ch.get("recent_videos") or []
    video_meta = resolved["video_meta"]

    sample_videos = [
        {k: v.get(k, "") for k in ("title", "duration", "views", "published", "url")}
        for v in recent[:_YT_DISCOVERY_VIDEOS_PER_CHANNEL]
    ]
    return YouTubeCandidate(
        topic=resolved["topic"],
        channel_id=channel_id,
        channel_title=str(meta.get("name") or resolved["name"]),
        channel_description=str(meta.get("description") or video_meta.get("description") or "")[:800],
        subscribers=str(meta.get("subscribers") or video_meta.get("subscribers") or ""),
        creator_keywords=str(meta.get("keywords") or video_meta.get("keywords") or ""),
        matched_keywords=resolved["matched_keywords"],
        sample_videos=sample_videos,
        thumbnail_url=_first_thumbnail_url(sample_videos),
    )


def _first_thumbnail_url(videos: list[dict[str, Any]]) -> str | None:
    """Extract a YouTube thumbnail URL from the first video whose URL contains a
    parseable video id. Uses the high-res CDN path (`maxresdefault.jpg`),
    falling back to the standard quality path is a per-video concern we skip."""
    for video in videos:
        url = str(video.get("url") or "")
        match = _YT_VIDEO_ID_PAT.search(url)
        if match:
            return f"https://i.ytimg.com/vi/{match.group(1)}/hqdefault.jpg"
    return None


# ---------------------------------------------------------------------------
# Stage 2b: frame extraction
# ---------------------------------------------------------------------------


def extract_youtube_frames(candidates: list[YouTubeCandidate]) -> int:
    """For each candidate, pick top-3 sample videos by views, download via
    yt-dlp, extract 5 frames via ffmpeg. Mutates `candidates` in place — populates
    `frames` field. Skips candidates that already have frames. Returns count of
    candidates newly extracted."""
    todo = [c for c in candidates if not c.frames]
    if not todo:
        return 0
    logger.info("yt_frames: extracting for %d/%d candidates", len(todo), len(candidates))

    def _extract_for_one(c: YouTubeCandidate) -> int:
        top_videos = sorted(
            c.sample_videos,
            key=lambda v: parse_views(str(v.get("views", ""))),
            reverse=True,
        )[:_TOP_VIDEOS_PER_CANDIDATE]
        for video in top_videos:
            url = str(video.get("url", ""))
            duration_s = parse_duration_seconds(str(video.get("duration", "")))
            paths = extract_youtube_frames_for_url(url, duration_s)
            if paths:
                c.frames.append(VideoFrames(
                    video_url=url,
                    video_title=str(video.get("title", "")),
                    frame_paths=[str(p) for p in paths],
                ))
        return 1 if c.frames else 0

    n_new = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for result in tqdm(pool.map(_extract_for_one, todo), total=len(todo), desc="yt_frames", unit="ch"):
            n_new += result
    logger.info("yt_frames: extracted frames for %d candidates", n_new)
    return n_new


# ---------------------------------------------------------------------------
# Stage 3: two-pass multimodal LLM review
# ---------------------------------------------------------------------------


def review_youtube_candidates(
    candidates: list[YouTubeCandidate],
    config: InterestsConfig,
    persona: PersonaOutput,
    runner: GeminiRunner,
    *,
    params: YouTubeDiscoverParams,
) -> list[ValidatedSource]:
    """Per-candidate parallel LLM review. Each candidate gets its own focused
    LLM call (no batching across candidates). One thread pool spans all topics."""
    if not candidates:
        return []
    topic_by_topic = {i.topic: i for i in config.interests}
    jobs = [c for c in candidates if c.topic in topic_by_topic]
    logger.info(
        "yt_review: %d candidates across %d topics, parallel per-candidate",
        len(jobs), len({c.topic for c in jobs}),
    )

    all_validated: list[ValidatedSource] = []
    per_topic_counts: dict[str, list[int]] = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {
            pool.submit(
                _review_one_youtube_candidate,
                c, topic_by_topic[c.topic], persona, config, runner, params,
            ): c
            for c in jobs
        }
        for future in tqdm(as_completed(futures), total=len(futures), desc="yt_review", unit="ch"):
            c = futures[future]
            counts = per_topic_counts.setdefault(c.topic, [0, 0])
            try:
                vs = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("yt_review crashed for %s/%s: %s", c.topic, c.channel_title, exc)
                continue
            if vs is None:
                continue  # LLM infrastructure failure; no decision, no catalog entry
            if vs.status == "active":
                counts[0] += 1
            else:
                counts[1] += 1
            all_validated.append(vs)
    for topic, (a, r) in sorted(per_topic_counts.items()):
        logger.info("  yt_review[%s]: %d admitted, %d rejected", topic, a, r)
    return all_validated


def _review_one_youtube_candidate(
    channel_data: YouTubeCandidate,
    topic_data: InterestEntry,
    persona: PersonaOutput,
    config: InterestsConfig,
    runner: GeminiRunner,
    params: YouTubeDiscoverParams,
) -> ValidatedSource | None:
    """Run content_understanding on each of up to 3 sample videos
    (title + 5 frames each), then feed the list of understandings + channel
    metadata to source_review.

    Returns:
      - ValidatedSource(status="active") on LLM admit
      - ValidatedSource(status="rejected") on LLM reject
      - None only on LLM infrastructure failure (no decision made)
    Emits a ledger event in every decision case."""
    sample_understandings: list[ContentUnderstanding] = []
    for vf in channel_data.frames:
        frame_bytes = [Path(p).read_bytes() for p in vf.frame_paths if Path(p).exists()]
        if not frame_bytes:
            continue
        text = f"Title: {vf.video_title}"
        try:
            u_raw = runner.run_json(
                build_content_understanding_prompt(text=text, images=frame_bytes),
                schema=ContentUnderstanding.model_json_schema(),
                schema_name=ContentUnderstanding.__name__,
            )
            sample_understandings.append(ContentUnderstanding.model_validate(u_raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "yt content_understanding failed for %s/%s/%s: %s",
                channel_data.topic, channel_data.channel_title, vf.video_title, exc,
            )

    if not sample_understandings:
        logger.warning(
            "yt_review: no usable sample understandings for %s/%s",
            channel_data.topic, channel_data.channel_title,
        )
        return None

    source_info = (
        f"channel title: {channel_data.channel_title}\n"
        f"channel description: {channel_data.channel_description}\n"
        f"subscribers: {channel_data.subscribers}\n"
        f"creator keywords: {channel_data.creator_keywords}"
    )
    try:
        d_raw = runner.run_json(
            build_source_review_prompt(
                source_info=source_info,
                sample_understandings=sample_understandings,
                topic_data=topic_data,
                persona=persona,
                language_preferences=topic_data.language_preferences,
            ),
            schema=SourceReviewDecision.model_json_schema(),
            schema_name=SourceReviewDecision.__name__,
        )
        decision = SourceReviewDecision.model_validate(d_raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "yt_review LLM failed for %s/%s: %s",
            channel_data.topic, channel_data.channel_title, exc,
        )
        return None

    sample_titles = [v.get("title", "") for v in channel_data.sample_videos if v.get("title")]
    ledger_evidence = {
        "subscribers": channel_data.subscribers,
        "creator_keywords": channel_data.creator_keywords[:240],
        "sample_titles": sample_titles[:5],
        "sample_understandings": [u.model_dump() for u in sample_understandings],
    }
    reasoning_text = flatten_decision_reasoning(decision)
    if decision.decision != "admit":
        emit_judgment(
            event_type="reject_source", platform="youtube", topic=channel_data.topic,
            source_id=channel_data.channel_id, source_name=channel_data.channel_title,
            reason_code=decision.reason_code,
            reasoning=reasoning_text,
            matched_keywords=list(channel_data.matched_keywords),
            evidence=ledger_evidence,
        )
        return ValidatedSource(
            seed=None,
            topic=channel_data.topic,
            platform="youtube",
            canonical_id=channel_data.channel_id,
            canonical_name=channel_data.channel_title,
            url=f"https://www.youtube.com/channel/{channel_data.channel_id}",
            reason_code=decision.reason_code,
            status="rejected",
            llm_reasoning=reasoning_text,
            matched_keywords=list(channel_data.matched_keywords),
            metadata={
                "subscribers": channel_data.subscribers,
                "creator_keywords": channel_data.creator_keywords,
            },
        )

    emit_judgment(
        event_type="admit_source", platform="youtube", topic=channel_data.topic,
        source_id=channel_data.channel_id, source_name=channel_data.channel_title,
        reason_code=decision.reason_code,
        reasoning=reasoning_text,
        matched_keywords=list(channel_data.matched_keywords),
        evidence=ledger_evidence,
    )
    return ValidatedSource(
        seed=None,
        topic=channel_data.topic,
        platform="youtube",
        canonical_id=channel_data.channel_id,
        canonical_name=channel_data.channel_title,
        url=f"https://www.youtube.com/channel/{channel_data.channel_id}",
        reason_code=params.admit_reason_code,
        llm_reasoning=reasoning_text,
        matched_keywords=list(channel_data.matched_keywords),
        sample_titles=sample_titles,
        sample_snippets=[channel_data.channel_description] if channel_data.channel_description else [],
        metadata={
            "subscribers": channel_data.subscribers,
            "creator_keywords": channel_data.creator_keywords,
        },
    )
