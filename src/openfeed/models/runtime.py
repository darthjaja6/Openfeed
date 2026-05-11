"""Runtime config — thresholds / rate limits / cycle tempo.

Bootstrap is frozen and does not consult this file; only discover / patrol /
filter / etc. read it. Keep fields minimal; add new ones as tasks come online.
"""
from __future__ import annotations

from importlib import resources
from pathlib import Path

from pydantic import BaseModel, ConfigDict
import yaml

from openfeed.utils.config_files import config_path, load_openfeed_config


class DiscoverYouTube(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords_per_topic: int
    results_per_keyword: int
    oversample_multiplier: int
    min_subscribers: int


class DiscoverX(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords_per_topic: int
    results_per_keyword: int
    min_followers: int


class DiscoverWeb(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords_per_topic: int
    results_per_keyword: int
    min_feed_entries: int
    max_age_days: int


class DiscoverTikTok(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords_per_topic: int
    results_per_keyword: int
    max_candidates_per_tick: int | None = None
    min_followers: int
    min_videos: int


class DiscoverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Cross-platform: how long a hard-gate (low_subscribers / low_followers /
    # stale-feed etc.) rejected source stays in catalog before auto-expiring
    # back into the candidate pool. LLM-level rejects never expire.
    hard_gate_retry_window_days: int
    youtube: DiscoverYouTube
    x: DiscoverX
    web: DiscoverWeb
    tiktok: DiscoverTikTok


class PatrolYouTube(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_items_per_source: int


class PatrolX(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_items_per_source: int


class PatrolWeb(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_items_per_source: int


class PatrolTikTok(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_items_per_source: int
    backfill_max_pages: int


class PatrolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    youtube: PatrolYouTube
    x: PatrolX
    web: PatrolWeb
    tiktok: PatrolTikTok


class FilterScoreWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    popularity: float
    engagement: float
    freshness: float
    preference: float


class FilterYouTube(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_views: int
    max_age_days: int
    duration_min_seconds: int
    duration_max_seconds: int


class FilterX(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_interactions: int   # likes + retweets + replies
    min_text_length: int


class FilterWeb(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_summary_length: int


class FilterTikTok(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_views: int
    max_age_days: int
    duration_min_seconds: int
    duration_max_seconds: int
    require_video_stream: bool
    allow_photo: bool
    min_photo_count: int


class FilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score_weights: FilterScoreWeights
    composite_score_threshold: float
    # freshness exp-decay half-life (days). `default` applies when a topic has
    # no explicit override in `per_topic`.
    freshness_half_life_days_default: int
    freshness_half_life_days_per_topic: dict[str, int]
    # EMA smoothing alpha when updating source.admission_rate
    admission_rate_ema_alpha: float
    # If a source patrols >0 items but admits 0 for this many CONSECUTIVE
    # filter passes, retire it (status=rejected, reason=filter_consistent_reject).
    # Eligible for hard_gate_retry expiry so re-discovery can revive it later.
    zero_admit_retire_threshold: int
    youtube: FilterYouTube
    x: FilterX
    web: FilterWeb
    tiktok: FilterTikTok


class QueueManageConfig(BaseModel):
    """Per-source inventory signalling for supply.

    `source_floor` — every active source should keep at least this many queued
    metadata items. Supply refills sources below this floor.
    `min_publishable_sources_per_slot` — minimum distinct sources with at
    least one media-ready/publishable queue item for each (topic, platform)
    slot. Supply runs scoped discover when a slot falls below this count.
    `live_source_discover_per_cycle_by_platform` — static per-platform quota
    for how many low-publishable (topic, platform) slots live_source_discover
    may run in one supply cycle.
    `source_exhausted_retry_seconds` — temporary skip window after patrol finds
    no new content for a source.
    """
    model_config = ConfigDict(extra="forbid")
    source_floor: int
    min_publishable_sources_per_slot: int
    live_source_discover_per_cycle_by_platform: dict[str, int]
    source_exhausted_retry_seconds: int


class PushConfig(BaseModel):
    """Consumer-side push: target buffer + card selection constraints.

    `target_buffer` — per PRD §5.5, each refill tick pushes until Ticlawk's
    unconsumed_total reaches this. No hysteresis — simplicity beats batching
    at single-user scale.
    `max_per_tick` — safety cap; refuse to push more than this per tick even
    if the gap is larger (protects against a sudden buffer wipe).
    Source diversity belongs to queue_manage's canonical queue order.
    `producer` — name of the card producer under `card_producers/<name>/`.
    `tick_budget_seconds` — wall-clock cap covering render + ticlawk push for
    the whole tick. Anything not finished stays in queue for the next tick.
    `render_workers` — parallel render threadpool size for the lazy render
    pass within a tick.
    """
    model_config = ConfigDict(extra="forbid")
    target_buffer: int
    max_per_tick: int
    producer: str
    tick_budget_seconds: int
    render_workers: int


class RefillCycleConfig(BaseModel):
    """Consumer-side main loop."""
    model_config = ConfigDict(extra="forbid")
    interval_seconds: int


class LearnConfig(BaseModel):
    """How learn turns feedback rows into Beta posterior evidence and decides
    which sources to retire. See PRD §5.7.

    Each feedback row gets a single signed score that is the sum of its
    independent active and passive signals. Positive net score adds Δα,
    negative net score adds Δβ. No bucketing — signals stack additively so
    `like + long dwell` is strictly stronger than just `like`.
    """
    model_config = ConfigDict(extra="forbid")

    # ---- Active signal weights (one component per delta type) ----
    score_share: float                       # share is the scarcest active signal
    score_save: float                        # save = strong intent to revisit
    score_like: float                        # like = lightest active acknowledgement

    # ---- Passive signal scoring (cognitive thresholds in absolute time) ----
    # Reflexive swipe: dwell below this is "I didn't even register the content"
    dwell_reflex_seconds: float              # e.g. 3 → score = score_strong_negative
    # Active dismiss: short dwell paired with low watch_progress = "saw it, bailed"
    dwell_dismiss_seconds: float             # e.g. 10
    watch_dismiss_max: float                 # paired with above; only triggers when watch < this
    # Engagement floor: long dwell or high watch = strong positive on its own
    dwell_strong_positive_seconds: float     # e.g. 60
    watch_strong_positive: float             # e.g. 0.8
    # Engagement floor (lighter)
    dwell_positive_seconds: float            # e.g. 30
    watch_positive: float                    # e.g. 0.5

    score_strong_positive: float             # passive engagement scoring tiers
    score_positive: float
    score_weak_negative: float               # negative scores stored as positive numbers
    score_strong_negative: float             # (we apply sign in the scorer)

    # Outlier guard: raw dwell above this is "tab abandoned" (webview stuck,
    # YouTube bot challenge, user wandered off) — passive signal is dropped
    # entirely. Active triggers still count.
    dwell_outlier_cap_seconds: int

    # ---- Preference drift (signal decay) ----
    # Daily multiplier on the evidence portion of α/β. 1.0 = no decay.
    # 0.977 ≈ 30-day half-life. Applied lazily per-source when new feedback
    # arrives, scaled by (now - last_evidence_at) in days.
    feedback_signal_decay_rate: float

    # ---- Retire path ----
    retire_posterior_threshold: float        # posterior_mean below this counts as "low"
    retire_evidence_min: int                 # require this much accumulated evidence to retire
    retire_bottom_k: int                     # retire candidates: bottom-K per topic by posterior_mean

    # ---- Global mood damping (multiplier on negative scores when recent
    # window has high neg-ratio across all sources). ----
    mood_window_hours: int
    mood_damp_threshold: float               # neg_ratio above this → light damp
    mood_heavy_damp_threshold: float         # neg_ratio above this → heavy damp
    mood_damp_multiplier: float
    mood_heavy_damp_multiplier: float

    # ---- search-term keyword expansion (LLM proposes new keywords from positive feedback) ----
    keyword_proposal_min_positive_examples: int
    keyword_proposal_max_examples: int
    keyword_proposal_max_new_terms: int
    keyword_proposal_workers: int

    # ---- Stage-1 deep-dive (per-positive multimodal perception) ----
    deep_dive_workers: int
    deep_dive_max_height: int
    deep_dive_frame_count: int

    # NOTE: search_term retire used to take min_evidence + min_llm_rejects
    # config knobs. The new rule (see learn_search_terms.py) is parameterless
    # — retire when ≥ 1 judgment-evaluated source exists and 0 are active.


class VideoCleanupConfig(BaseModel):
    """cleanup_assets task — drop local mp4 + ticlawk asset for cards that
    have aged out of history (and are no longer in queue).

    `keep_days` — keep an asset for this long after the most recent push of
    the underlying card; lets users replay recent cards without
    re-downloading + re-uploading.
    `cache_max_gb` — local mp4 cache size hard cap (state/video_cache/);
    LRU-evict beyond cap as a safety net for runaway disk use.
    `ticlawk_quota_max_gb` — guard against the creator/account's Ticlawk asset quota.
    Set below the real account quota to leave headroom for in-flight uploads.
    Cleanup tracks our `state/video_assets.json` total and LRU-evicts when over.
    """
    model_config = ConfigDict(extra="forbid")
    keep_days: int
    cache_max_gb: float
    ticlawk_quota_max_gb: float


class YouTubeDownloadConfig(BaseModel):
    """prepare_video task — local mp4 cache builder for YouTube cards.

    Keeps a bounded ready set near the front of each YouTube topic queue.
    Failures are backed off; videos that fail too many times in a row get
    marked permanently_failed and are no longer attempted.
    """
    model_config = ConfigDict(extra="forbid")
    ready_target_per_topic: int                 # stop once each topic has this many ready queued videos
    max_per_tick: int                          # how many videos to try per tick
    max_concurrent: int                        # parallel yt-dlp processes
    tick_budget_seconds: int                   # wall-clock cap per tick
    target_height: int                         # target resolution for yt-dlp `-S res:N` (e.g. 720)
    max_filesize_mb: int                       # skip/downshift files that are too large for the consumer
    failure_backoff_minutes: int               # don't retry a failed video sooner than this
    max_failures_before_permanent: int         # mark permanently_failed after N consecutive fails


class TikTokDownloadConfig(BaseModel):
    """prepare_video task config for TikTok native video cards."""
    model_config = ConfigDict(extra="forbid")
    ready_target_per_topic: int
    max_per_tick: int
    max_concurrent: int
    tick_budget_seconds: int
    max_filesize_mb: int
    failure_backoff_minutes: int
    max_failures_before_permanent: int


class TikTokImageDownloadConfig(BaseModel):
    """prepare_video task config for TikTok photo-mode image cards."""
    model_config = ConfigDict(extra="forbid")
    ready_target_per_topic: int
    max_per_tick: int
    max_concurrent: int
    tick_budget_seconds: int
    failure_backoff_minutes: int
    max_failures_before_permanent: int


class CollectFeedbackConfig(BaseModel):
    """collect_feedback config — bounds the channel-changes pagination loop.

    Server now does the per-card diff (PRD §5.6 update — ticlawk shipped
    `GET /api/channels/:id/changes`); we only need a wall-clock cap for the
    pagination loop. Anything not fetched this tick stays behind the cursor
    and gets pulled next tick.
    """
    model_config = ConfigDict(extra="forbid")
    tick_budget_seconds: int


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discover: DiscoverConfig
    patrol: PatrolConfig
    filter: FilterConfig
    queue_manage: QueueManageConfig
    push: PushConfig
    refill_cycle: RefillCycleConfig
    collect_feedback: CollectFeedbackConfig
    learn: LearnConfig
    youtube_download: YouTubeDownloadConfig
    tiktok_download: TikTokDownloadConfig
    tiktok_image_download: TikTokImageDownloadConfig
    video_cleanup: VideoCleanupConfig


def load_default_runtime() -> RuntimeConfig:
    default_runtime = resources.files("openfeed").joinpath("default_runtime.yaml")
    parsed = yaml.safe_load(default_runtime.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"default runtime config must be a mapping: {default_runtime}")
    return RuntimeConfig.model_validate(parsed)


def load_runtime(workdir: Path) -> RuntimeConfig:
    del workdir
    raw = load_openfeed_config()
    if "runtime" in raw:
        raise ValueError(
            "runtime is no longer configured in openfeed.yaml. "
            f"Remove the top-level 'runtime' section from {config_path()}; "
            "OpenFeed uses source-code runtime defaults."
        )
    return load_default_runtime()
