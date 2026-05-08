"""Filter — content review pipeline per PRD §5.3.

Consumes `queues/patrol/*.json`, produces:
  - `state/queue.json` — admitted content, bucketed by topic (rank_score from
    composite filter score). push reads this.
  - `ledgers/decisions.jsonl` — admit_content / reject_content events.
  - `state/source_catalog.json` — `admission_rate` EMA-updated per source.

Pipeline stages, each can reject:
  1. `duplicate`             — content_id already admitted or previously rejected
  2. per-platform hard gates — `low_views` / `too_old` / `duration_out_of_range` /
                               `low_interactions` / `post_too_short` /
                               `summary_too_short` / `title_missing`
  3. continuous composite score threshold — `low_composite_score`
  4. multimodal LLM review (single-pass, 4 gates: topic / user_taste /
     language / quality) — admit or reject with the first-failed dim as
     `reason_code`

After a content item is processed (admit or reject), its patrol queue file
is deleted. LLM-infrastructure failures leave the file in place for next
run, per PRD §3.7 "don't advance on partial failure".
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from openfeed.utils.config_files import load_env
from tqdm import tqdm

from openfeed.clients.content.article_fetch import fetch_article
from openfeed.clients.llm import GeminiRunner
from openfeed.core.judgment_ledger import attach_file as _attach_ledger, emit_judgment
from openfeed.models.content_item import ContentItem, ContentScore
from openfeed.models.interests import InterestEntry, InterestsConfig, load_interests
from openfeed.models.persona import PersonaOutput
from openfeed.models.user_profile import get_user_profile
from openfeed.models.queue import Queue, QueueItem
from openfeed.models.runtime import FilterConfig, load_runtime
from openfeed.models.source import SourceCatalog, SourceEntry
from openfeed.prompts.content_review import (
    ContentReviewDecision,
    build_content_review_prompt,
    content_text_block,
    flatten_content_reasoning,
)
from openfeed.prompts.content_understanding import (
    ContentUnderstanding,
    build_content_understanding_prompt,
)
from openfeed.utils.content_meta import (
    TopicTemporal,
    content_age_days,
    load_content_blocklist,
    resolve_topic_temporal,
)
from openfeed.utils import catalog_io, cycle_summary
from openfeed.utils.logging_setup import configure_task_logging
from openfeed.utils.queue_io import load_queue, mutate_queue
from openfeed.utils.video_frames import (
    extract_youtube_frames,
    parse_duration_seconds,
    parse_views,
)


logger = logging.getLogger("filter")

_QUEUE_PATROL_DIR = Path("queues/patrol")
_CATALOG_PATH = Path("state/source_catalog.json")
_LEDGER_PATH = Path("ledgers/decisions.jsonl")

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
    configure_task_logging("filter")
    _attach_ledger(_LEDGER_PATH)
    logger.info("ledger → %s", _LEDGER_PATH)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _popularity_score(item: ContentItem) -> float:
    """[0,1] — normalised log of the platform's own popularity signal."""
    if item.platform == "youtube" and item.youtube is not None:
        views = parse_views(item.youtube.views)
        return min(1.0, math.log10(max(views, 1) + 1) / 6.0)  # log10(1M)=6 → 1.0
    if item.platform == "x" and item.x is not None:
        if item.x.views > 0:
            return min(1.0, math.log10(item.x.views + 1) / 6.0)
        ints = item.x.likes + item.x.retweets
        return min(1.0, math.log10(max(ints, 1) + 1) / 4.0)  # fallback on interactions
    if item.platform == "tiktok" and item.tiktok is not None:
        views = item.tiktok.view_count or 0
        return min(1.0, math.log10(max(views, 1) + 1) / 6.0)
    # Web has no reliable platform popularity signal → neutral
    return 0.5


def _engagement_score(item: ContentItem) -> float:
    """[0,1] — platform-interaction rate relative to reach."""
    if item.platform == "youtube":
        # Current patrol digest doesn't carry likes_count (opencli
        # recent_videos list omits it). Default to neutral; filter's
        # popularity + freshness + LLM quality carry the load for YouTube.
        return 0.5
    if item.platform == "x" and item.x is not None:
        if item.x.views > 0:
            rate = (item.x.likes + item.x.retweets) / item.x.views
            return min(1.0, rate * 20.0)  # 5% engagement → saturate at 1.0
        return 0.5
    if item.platform == "tiktok" and item.tiktok is not None:
        views = item.tiktok.view_count or 0
        if views <= 0:
            return 0.5
        interactions = (
            (item.tiktok.like_count or 0)
            + (item.tiktok.comment_count or 0)
            + (item.tiktok.repost_count or 0)
        )
        return min(1.0, (interactions / views) * 20.0)
    # Web CTR comes from Ticlawk history; not yet wired → neutral
    return 0.5


def _freshness_score(age_days: float | None, half_life_days: float) -> float:
    """Exponential decay on content age. Unknown age → neutral 0.5."""
    if age_days is None:
        return 0.5
    if age_days < 0:
        age_days = 0
    return math.exp(-math.log(2) * age_days / max(half_life_days, 0.001))


def _preference_score(source: SourceEntry) -> float:
    """Source's Bayesian posterior mean from learn-phase feedback.
    Defaults to 0.5 for fresh sources (Jeffreys prior α=β=0.5)."""
    post = source.posterior
    denom = post.alpha + post.beta
    return (post.alpha / denom) if denom > 0 else 0.5


def _compute_score(
    item: ContentItem,
    source: SourceEntry,
    filter_cfg: FilterConfig,
    half_life_days: float,
) -> ContentScore:
    pop = _popularity_score(item)
    eng = _engagement_score(item)
    fresh = _freshness_score(content_age_days(item), half_life_days)
    pref = _preference_score(source)
    w = filter_cfg.score_weights
    composite = (
        w.popularity * pop + w.engagement * eng + w.freshness * fresh + w.preference * pref
    )
    return ContentScore(
        popularity=round(pop, 4), engagement=round(eng, 4),
        freshness=round(fresh, 4), preference=round(pref, 4),
        composite=round(composite, 4),
    )


# ---------------------------------------------------------------------------
# Hard gates
# ---------------------------------------------------------------------------


def _check_hard_gates(
    item: ContentItem, cfg: FilterConfig, max_age_days: int | None,
    youtube_duration_max_seconds: int | None = None,
) -> str | None:
    """Return a reject reason_code if hard-gate fails, else None.

    Both `max_age_days` and `youtube_duration_max_seconds` are resolved by
    the caller — per-topic InterestEntry override with runtime config fallback.
    """
    if max_age_days is not None:
        age = content_age_days(item)
        if age is not None and age > max_age_days:
            return "too_old"

    if item.platform == "youtube" and item.youtube is not None:
        yt_cfg = cfg.youtube
        views = parse_views(item.youtube.views)
        if views < yt_cfg.min_views:
            return "low_views"
        dur = parse_duration_seconds(item.youtube.duration)
        if dur == 0:
            return None  # unparseable duration — be lenient, let LLM judge
        # Per-topic override falls back to runtime platform default.
        dur_cap = (youtube_duration_max_seconds
                   if youtube_duration_max_seconds is not None
                   else yt_cfg.duration_max_seconds)
        if dur < yt_cfg.duration_min_seconds or dur > dur_cap:
            return "duration_out_of_range"
        return None
    if item.platform == "x" and item.x is not None:
        x_cfg = cfg.x
        interactions = item.x.likes + item.x.retweets
        if interactions < x_cfg.min_interactions:
            return "low_interactions"
        if len(item.x.text or "") < x_cfg.min_text_length:
            return "post_too_short"
        return None
    if item.platform == "web" and item.web is not None:
        w_cfg = cfg.web
        if not (item.web.title or "").strip():
            return "title_missing"
        # Post-fetch: prefer trafilatura body over thin RSS summary. An item
        # only fails if BOTH sources are too thin — RSS-only "Comments" (HN)
        # no longer gets auto-rejected when fetched body is substantive.
        body = item.web.full_body or item.web.summary or ""
        if len(body) < w_cfg.min_summary_length:
            return "summary_too_short"
        return None
    if item.platform == "tiktok" and item.tiktok is not None:
        tt_cfg = cfg.tiktok
        if item.tiktok.media_kind == "audio_or_cover_only":
            return "non_video_tiktok"
        if item.tiktok.media_kind == "video":
            if tt_cfg.require_video_stream and not item.tiktok.has_video_stream:
                return "non_video_tiktok"
            duration = item.tiktok.duration_seconds
            if duration is not None and (
                duration < tt_cfg.duration_min_seconds
                or duration > tt_cfg.duration_max_seconds
            ):
                return "duration_out_of_range"
        elif item.tiktok.media_kind == "photo":
            if not tt_cfg.allow_photo:
                return "non_video_tiktok"
            if item.tiktok.photo_count < tt_cfg.min_photo_count:
                return "photo_count_too_low"
        if (item.tiktok.view_count or 0) < tt_cfg.min_views:
            return "low_views"
        return None
    return None


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_catalog() -> SourceCatalog:
    state_dir = Path("state")
    if not catalog_io.catalog_dir(state_dir).exists():
        raise SystemExit(
            f"{catalog_io.catalog_dir(state_dir)} missing — run interest-bootstrap first"
        )
    return catalog_io.load_catalog(state_dir)


def _save_catalog(catalog: SourceCatalog, topics: set[str]) -> None:
    if not topics:
        return
    state_dir = Path("state")
    for topic in sorted(topics):
        scoped = {k: v for k, v in catalog.sources.items() if v.topic == topic}
        catalog_io.save_catalog_topic(state_dir, topic, scoped)


def _load_patrol_items() -> list[tuple[Path, ContentItem]]:
    if not _QUEUE_PATROL_DIR.exists():
        return []
    out: list[tuple[Path, ContentItem]] = []
    for fp in sorted(_QUEUE_PATROL_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            out.append((fp, ContentItem.model_validate(data)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("skip malformed patrol file %s: %s", fp.name, exc)
    return out


def _youtube_frame_bytes(item: ContentItem) -> list[bytes]:
    """Download the video + extract 5 evenly-spaced frames, return the
    JPG bytes. Empty list if we can't parse a duration or extraction
    fails. Cached on disk under `logs/video_frames/<video_id>/` so
    repeat calls for the same video skip both yt-dlp and ffmpeg."""
    if item.youtube is None:
        return []
    duration_s = parse_duration_seconds(item.youtube.duration)
    if duration_s <= 0:
        return []
    paths = extract_youtube_frames(item.youtube.url, duration_s)
    return [p.read_bytes() for p in paths if p.exists()]


def _fetch_tiktok_image_bytes(url: str, *, referer: str, timeout: int = 10) -> bytes | None:
    try:
        request = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Referer": referer,
            },
        )
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except Exception as exc:  # noqa: BLE001
        logger.info("tiktok image fetch failed for %s: %s", url[:80], exc)
        return None


def _tiktok_media_bytes(item: ContentItem) -> list[bytes]:
    """Fetch TikTok visual bytes for LLM review.

    Videos get the thumbnail as a lightweight cue. Photo-mode posts get up to
    five original CDN images from the digest.
    """
    if item.tiktok is None:
        return []
    if item.tiktok.media_kind == "photo":
        images: list[bytes] = []
        for url in item.tiktok.photo_image_urls[:5]:
            data = _fetch_tiktok_image_bytes(url, referer=item.tiktok.url)
            if data:
                images.append(data)
        return images
    if item.tiktok.thumbnail_url:
        data = _fetch_tiktok_image_bytes(item.tiktok.thumbnail_url, referer=item.tiktok.url)
        return [data] if data else []
    return []


# ---------------------------------------------------------------------------
# Ledger wrappers
# ---------------------------------------------------------------------------


def _content_display_name(item: ContentItem) -> str:
    if item.youtube is not None:
        return item.youtube.title
    if item.x is not None:
        return f"@{item.x.author}"
    if item.web is not None:
        return item.web.title or item.content_id
    if item.tiktok is not None:
        return item.tiktok.title or item.content_id
    return item.content_id


def _emit_content_decision(
    event_type: str,
    item: ContentItem,
    source: SourceEntry | None,
    reason_code: str,
    *,
    reasoning: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    ev = dict(evidence or {})
    if source is not None:
        ev.setdefault("source_id", source.source_id)
        ev.setdefault("source_name", source.name)
    emit_judgment(
        event_type=event_type,
        platform=item.platform,
        topic=item.topic,
        source_id=item.content_id,
        source_name=_content_display_name(item),
        reason_code=reason_code,
        reasoning=reasoning,
        evidence=ev,
    )


# ---------------------------------------------------------------------------
# LLM review
# ---------------------------------------------------------------------------


def _review_one_content(
    item: ContentItem,
    topic_data: InterestEntry,
    persona: PersonaOutput,
    config: InterestsConfig,
    runner: GeminiRunner,
    images: list[bytes] | None,
) -> ContentReviewDecision | None:
    """content_understanding → content_review, two LLM calls.

    `images` is caller-supplied (YouTube: 5 extracted frames; X/Web:
    empty). Either call failing returns None so the patrol file stays
    around for next-cycle retry per PRD §3.7.
    """
    try:
        u_raw = runner.run_json(
            build_content_understanding_prompt(
                text=content_text_block(item),
                images=images,
            ),
            schema=ContentUnderstanding.model_json_schema(),
            schema_name=ContentUnderstanding.__name__,
        )
        understanding = ContentUnderstanding.model_validate(u_raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("content_understanding failed for %s: %s",
                       item.content_id, exc)
        return None

    try:
        d_raw = runner.run_json(
            build_content_review_prompt(
                content_item=item,
                content_understanding=understanding,
                topic_data=topic_data,
                persona=persona,
                language_preferences=topic_data.language_preferences,
            ),
            schema=ContentReviewDecision.model_json_schema(),
            schema_name=ContentReviewDecision.__name__,
        )
        return ContentReviewDecision.model_validate(d_raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("content_review failed for %s: %s",
                       item.content_id, exc)
        return None


def _derive_reject_reason(d: ContentReviewDecision) -> str:
    """Pick a slug that names the first failing gate (order: topic > taste > language)."""
    if not d.is_the_topic:
        return "off_topic"
    if not d.matches_user_taste:
        return "user_taste_mismatch"
    if not d.language_match:
        return "language_mismatch"
    return "unknown"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Filter patrol queue items into admitted content")
    parser.add_argument("--topic", help="Only process patrol items for one topic")
    parser.add_argument(
        "--platform",
        choices=["youtube", "x", "web", "tiktok"],
        help="Only process patrol items for one platform",
    )
    args = parser.parse_args(argv)
    _configure_logging()
    workdir = Path.cwd()
    load_env(workdir)

    config = load_interests(workdir)
    runtime = load_runtime(workdir)
    profile = get_user_profile(workdir)
    persona = profile.persona
    runner = GeminiRunner(workdir)

    catalog = _load_catalog()
    queue = load_queue()
    blocklist = load_content_blocklist(queue)

    patrol_items = _load_patrol_items()
    if args.topic:
        patrol_items = [(fp, item) for fp, item in patrol_items if item.topic == args.topic]
    if args.platform:
        patrol_items = [(fp, item) for fp, item in patrol_items if item.platform == args.platform]
    logger.info(
        "filter start: %d patrol items, %d already-decided in blocklist, queue has %d admitted",
        len(patrol_items), len(blocklist),
        sum(len(v) for v in queue.topics.values()),
    )

    topic_by_name = {i.topic: i for i in config.interests}

    # Pre-resolve per-(topic, platform) temporal values so hard_gate and
    # scoring don't re-run the fallback chain for every item.
    temporal_map: dict[tuple[str, str], TopicTemporal] = {}
    for entry in config.interests:
        for platform in entry.platforms:
            temporal_map[(entry.topic, platform)] = resolve_topic_temporal(
                entry.topic, entry, runtime.filter, platform,
            )

    def _temporal_for(item: ContentItem) -> TopicTemporal:
        key = (item.topic, item.platform)
        if key not in temporal_map:
            # unknown topic (orphan patrol file) — fall back with no entry
            temporal_map[key] = resolve_topic_temporal(
                item.topic, topic_by_name.get(item.topic),
                runtime.filter, item.platform,
            )
        return temporal_map[key]

    stats: Counter = Counter()
    source_reviewed: Counter = Counter()
    source_admitted: Counter = Counter()

    # --------------------------------------------------------------
    # Stage 0: re-fetch article bodies for web items (trafilatura)
    # RSS summaries are often too thin (HN "Comments", TechCrunch meta);
    # pulling the real article here means hard_gate + LLM review see real
    # content. Fetch failures leave full_body=None — caller falls back to
    # RSS summary automatically.
    # --------------------------------------------------------------
    web_to_fetch = [
        item for _, item in patrol_items
        if item.platform == "web" and item.web is not None and item.web.full_body is None
    ]
    if web_to_fetch:
        def _hydrate(item: ContentItem) -> None:
            try:
                body = fetch_article(item.web.link) if item.web else None
            except Exception as exc:  # noqa: BLE001
                logger.warning("article fetch crashed for %s: %s", item.web.link if item.web else "?", exc)
                return
            if body and item.web is not None:
                item.web.full_body = body
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(tqdm(
                pool.map(_hydrate, web_to_fetch),
                total=len(web_to_fetch), desc="web_fetch", unit="c",
            ))
        hydrated = sum(1 for it in web_to_fetch if it.web and it.web.full_body)
        logger.info("article bodies: %d/%d hydrated", hydrated, len(web_to_fetch))

    # --------------------------------------------------------------
    # Stages 1–3: dedup + hard gates + scoring (no LLM, no HTTP yet)
    # --------------------------------------------------------------
    to_review: list[tuple[Path, ContentItem, SourceEntry, ContentScore]] = []
    for fp, item in patrol_items:
        key = f"{item.platform}:{item.source_id}:{item.topic}"
        source = catalog.sources.get(key)
        source_reviewed[key] += 1

        if source is None:
            _emit_content_decision(
                "reject_content", item, None, "source_not_in_catalog",
                evidence={"patrol_file": fp.name, "orphan_source_key": key},
            )
            fp.unlink(missing_ok=True)
            stats["source_not_in_catalog"] += 1
            continue

        if item.content_id in blocklist:
            _emit_content_decision(
                "reject_content", item, source, "duplicate",
                evidence={"patrol_file": fp.name},
            )
            fp.unlink(missing_ok=True)
            stats["duplicate"] += 1
            continue

        temporal = _temporal_for(item)
        topic_entry = topic_by_name.get(item.topic)
        per_topic_dur = (topic_entry.youtube_duration_max_seconds
                        if topic_entry is not None else None)
        gate = _check_hard_gates(
            item, runtime.filter, temporal.max_age_days,
            youtube_duration_max_seconds=per_topic_dur,
        )
        if gate:
            _emit_content_decision(
                "reject_content", item, source, gate,
                evidence={"patrol_file": fp.name,
                          "digest": item.model_dump(exclude_none=True),
                          "max_age_days_applied": temporal.max_age_days},
            )
            fp.unlink(missing_ok=True)
            stats[gate] += 1
            continue

        score = _compute_score(item, source, runtime.filter, temporal.half_life_days)
        if score.composite < runtime.filter.composite_score_threshold:
            _emit_content_decision(
                "reject_content", item, source, "low_composite_score",
                evidence={"score": score.model_dump(),
                          "threshold": runtime.filter.composite_score_threshold},
            )
            fp.unlink(missing_ok=True)
            stats["low_composite_score"] += 1
            continue

        to_review.append((fp, item, source, score))

    logger.info(
        "pre-LLM: %d survivors, %d rejected so far (%s)",
        len(to_review), sum(stats.values()), dict(stats),
    )

    # --------------------------------------------------------------
    # Extract visual cues for video platforms. YouTube uses its CDN scene
    # thumbnails; TikTok gets the creator thumbnail for this module. X/web
    # have no extracted media today — pass empty images list.
    # --------------------------------------------------------------
    yt_survivors = [t for t in to_review if t[1].platform == "youtube"]
    yt_frames: dict[str, list[bytes]] = {}
    if yt_survivors:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(_youtube_frame_bytes, t[1]): t[1].content_id
                for t in yt_survivors
            }
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="yt_frames", unit="vid"):
                cid = futures[future]
                try:
                    yt_frames[cid] = future.result()
                except Exception:  # noqa: BLE001
                    yt_frames[cid] = []
        ok = sum(1 for v in yt_frames.values() if v)
        logger.info("yt frames extracted: %d/%d videos (5 frames each)",
                    ok, len(yt_survivors))

    tiktok_survivors = [t for t in to_review if t[1].platform == "tiktok"]
    tiktok_images: dict[str, list[bytes]] = {}
    if tiktok_survivors:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(_tiktok_media_bytes, t[1]): t[1].content_id
                for t in tiktok_survivors
            }
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="tiktok_thumbs", unit="vid"):
                cid = futures[future]
                try:
                    tiktok_images[cid] = future.result()
                except Exception:  # noqa: BLE001
                    tiktok_images[cid] = []
        ok = sum(1 for v in tiktok_images.values() if v)
        logger.info("tiktok visual batches fetched: %d/%d items", ok, len(tiktok_survivors))

    # --------------------------------------------------------------
    # Stage 4: multimodal LLM review in parallel
    # --------------------------------------------------------------
    # Collect admits rather than enqueue them directly — Stage 5 below runs
    # the takeaway LLM on web/X admits before they land in queue, so push
    # never has to deal with "admitted but no takeaways yet" state.
    admitted_items: list[tuple[Path, ContentItem, SourceEntry, ContentScore]] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {
            pool.submit(
                _review_one_content, item,
                topic_by_name.get(item.topic) or InterestEntry(
                    topic=item.topic,
                    description="",
                    platforms={
                        item.platform: (
                            {"html_template": "missing.html"}
                            if item.platform in ("web", "x")
                            else {}
                        ),
                    },
                    consumer_type="ticlawk",
                    consumer_config={"channel_id": "orphan"},
                    language_preferences=[],
                ),
                persona, config, runner,
                (
                    yt_frames.get(item.content_id)
                    if item.platform == "youtube"
                    else tiktok_images.get(item.content_id)
                    if item.platform == "tiktok"
                    else None
                ),
            ): (fp, item, source, score)
            for fp, item, source, score in to_review
        }
        for future in tqdm(as_completed(futures), total=len(futures),
                           desc="llm_review", unit="c"):
            fp, item, source, score = futures[future]
            try:
                decision = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("content review crashed for %s: %s", item.content_id, exc)
                stats["llm_crashed"] += 1
                continue
            if decision is None:
                stats["llm_failed"] += 1
                continue  # leave patrol file for retry

            admitted = (
                decision.is_the_topic and decision.matches_user_taste
                and decision.language_match
            )
            reasoning = flatten_content_reasoning(decision)
            evidence = {"score": score.model_dump()}
            if admitted:
                _emit_content_decision(
                    "admit_content", item, source, "admit",
                    reasoning=reasoning, evidence=evidence,
                )
                admitted_items.append((fp, item, source, score))
                source_admitted[source.catalog_key] += 1
                stats["admit"] += 1
            else:
                reason = _derive_reject_reason(decision)
                _emit_content_decision(
                    "reject_content", item, source, reason,
                    reasoning=reasoning, evidence=evidence,
                )
                stats[reason] += 1
                fp.unlink(missing_ok=True)

    # --------------------------------------------------------------
    # Stage 5: enqueue admitted items. Rendering is push's job — push
    # lazily renders the cards it actually selects each tick and caches
    # the result back onto QueueItem.rendered_card. Failed pushes leave
    # the cache for the next pickup so LLM tokens are spent at most once.
    # --------------------------------------------------------------
    # --------------------------------------------------------------
    # EMA-update admission_rate per source + filter-retire bookkeeping.
    # Source retire here covers the "patrol kept returning items but filter
    # admitted 0" case — different from learn-side retire (which fires on
    # real user feedback). Keeps patrol budget off sources whose pipeline
    # output is consistently filtered out.
    # --------------------------------------------------------------
    alpha = runtime.filter.admission_rate_ema_alpha
    retire_threshold = runtime.filter.zero_admit_retire_threshold
    now_iso = _utc_now_iso()
    retired_keys: list[str] = []
    for key, total in source_reviewed.items():
        entry = catalog.sources.get(key)
        if entry is None or total <= 0:
            continue
        admitted = source_admitted.get(key, 0)
        batch_rate = admitted / total
        entry.admission_rate = alpha * batch_rate + (1 - alpha) * entry.admission_rate

        if admitted > 0:
            entry.consecutive_zero_admit_patrols = 0
            continue
        # All items from this source rejected this pass — extend the streak.
        entry.consecutive_zero_admit_patrols += 1
        if (entry.status == "active"
                and entry.consecutive_zero_admit_patrols >= retire_threshold):
            entry.status = "rejected"
            entry.decision_reason_code = "filter_consistent_reject"
            entry.decided_at = now_iso
            retired_keys.append(entry.catalog_key)
            emit_judgment(
                event_type="reject_source",
                platform=entry.platform,
                topic=entry.topic,
                source_id=entry.source_id,
                source_name=entry.name,
                reason_code="filter_consistent_reject",
                evidence={
                    "consecutive_zero_admit_patrols": entry.consecutive_zero_admit_patrols,
                    "items_reviewed_this_pass": total,
                    "admission_rate": round(entry.admission_rate, 4),
                },
            )
            logger.info(
                "→ filter-retire: %s [%s] streak=%d items=%d",
                entry.catalog_key, entry.topic,
                entry.consecutive_zero_admit_patrols, total,
            )
    # Drop any in-flight queue items belonging to a freshly-retired source so
    # push doesn't keep flushing them after we've decided the source is bad.
    if retired_keys:
        logger.info(
            "filter-retire pass: %d sources flipped to rejected; queued items will be pruned in the queue transaction",
            len(retired_keys),
        )

    touched_topics = {
        entry.topic
        for key in source_reviewed
        if (entry := catalog.sources.get(key)) is not None
    }
    _save_catalog(catalog, touched_topics)

    admitted_by_id = {
        item.content_id: (fp, item, score)
        for fp, item, _source, score in admitted_items
    }
    retired = set(retired_keys)

    def apply_queue_updates(latest: Queue) -> tuple[int, int, int]:
        existing_ids = {
            qi.content.content_id
            for topic_items in latest.topics.values()
            for qi in topic_items
        }
        enqueued = 0
        duplicates = 0
        for content_id, (_fp, item, score) in admitted_by_id.items():
            if content_id in existing_ids:
                duplicates += 1
                continue
            latest.topics.setdefault(item.topic, []).append(
                QueueItem(
                    content=item,
                    score=score,
                    rank_score=score.composite,
                    admitted_at=_utc_now_iso(),
                )
            )
            existing_ids.add(content_id)
            enqueued += 1

        pruned = 0
        if retired:
            for topic, topic_items in latest.topics.items():
                kept = [
                    it for it in topic_items
                    if f"{it.content.platform}:{it.content.source_id}:{it.content.topic}"
                    not in retired
                ]
                pruned += len(topic_items) - len(kept)
                latest.topics[topic] = kept
        return enqueued, duplicates, pruned

    enqueued, duplicate_admits, queue_pruned = mutate_queue(
        apply_queue_updates,
        topic_by_name=topic_by_name,
        runtime=runtime,
    )
    for fp, _item, _source, _score in admitted_items:
        fp.unlink(missing_ok=True)
    if duplicate_admits or queue_pruned:
        logger.info(
            "queue transaction: enqueued=%d duplicate_admits=%d pruned_retired_items=%d",
            enqueued, duplicate_admits, queue_pruned,
        )
    final_queue = load_queue()

    logger.info("filter_ok: %s", dict(stats))
    logger.info(
        "queue now: %d topics, %d total items",
        len(final_queue.topics), sum(len(v) for v in final_queue.topics.values()),
    )
    cycle_summary.add(
        "filter",
        admitted=stats.get("admit", 0),
        rejected=sum(v for k, v in stats.items() if k != "admit"),
        sources_filter_retired=len(retired_keys),
        queue_size=sum(len(v) for v in final_queue.topics.values()),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
