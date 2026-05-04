from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


Platform = Literal["youtube", "x", "web", "tiktok"]
# Source lifecycle states. `active` sources are patrolled; `rejected` were
# never admitted (or admitted then retired by learn); `blocked` is reserved
# for hard rejects from blocklist policies. The earlier `watchlist` state was
# removed (PRD §5.7 simplification: direct active → rejected on retire).
SourceStatus = Literal["active", "rejected", "blocked"]


class BayesianPosterior(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alpha: float = 0.5
    beta: float = 0.5

    @property
    def mean(self) -> float:
        total = self.alpha + self.beta
        return self.alpha / total if total > 0 else 0.5


class SourceAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    introduced_by_seed_term: str | None = None
    introduced_at: str
    matched_terms: list[str] = Field(default_factory=list)


class SourceEntry(BaseModel):
    # `extra="ignore"` so older catalog rows (without learn-added fields) keep
    # loading; new fields default cleanly to neutral starting values.
    model_config = ConfigDict(extra="ignore")
    source_id: str
    platform: Platform
    topic: str
    status: SourceStatus
    name: str
    url: str
    admission_rate: float = 0.0
    posterior: BayesianPosterior = Field(default_factory=BayesianPosterior)
    # learn-side counters (see PRD §5.7). evidence_count tracks total Bayesian
    # evidence increments folded in; last_evidence_at is the timestamp of the
    # most recent feedback row applied (used by signal-decay math).
    evidence_count: int = 0
    last_evidence_at: str | None = None
    # filter-side retire counter: count of consecutive filter passes where
    # patrol returned > 0 items but filter admitted 0. Reset on any admit.
    # When ≥ filter_zero_admit_retire_threshold, source flips active → rejected
    # with reason `filter_consistent_reject` (eligible for hard_gate retry).
    consecutive_zero_admit_patrols: int = 0
    decision_reason_code: str
    decided_at: str
    last_patrolled_at: str | None = None
    attribution: SourceAttribution
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def catalog_key(self) -> str:
        # Per-topic logical scoping: same physical source (`platform`+`source_id`)
        # in two different topics is two independent catalog entries with
        # independent posteriors and retire decisions. Discover dedup,
        # source-side feedback, and patrol rotation all key by this triple.
        return make_catalog_key(self.platform, self.source_id, self.topic)


def make_catalog_key(platform: str, source_id: str, topic: str) -> str:
    """Build the canonical catalog key. Use this everywhere instead of
    f-string-literals so the format stays consistent."""
    return f"{platform}:{source_id}:{topic}"


class SourceCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: str
    sources: dict[str, SourceEntry] = Field(default_factory=dict)


class VideoFrames(BaseModel):
    """5 evenly-spaced frame jpegs extracted from one sample video."""
    model_config = ConfigDict(extra="forbid")
    video_url: str
    video_title: str
    frame_paths: list[str] = Field(default_factory=list)


class YouTubeCandidate(BaseModel):
    """One channel surfaced by opencli search+resolve, pre-LLM-review."""
    model_config = ConfigDict(extra="forbid")
    topic: str
    channel_id: str
    channel_title: str
    channel_description: str = ""
    subscribers: str = ""
    creator_keywords: str = ""
    matched_keywords: list[str] = Field(default_factory=list)
    sample_videos: list[dict[str, Any]] = Field(default_factory=list)
    thumbnail_url: str | None = None
    frames: list[VideoFrames] = Field(default_factory=list)
