"""Content item schema — one `queues/patrol/*.json` entry.

Shape follows PRD §3.6 queues/patrol: identity fields + per-platform digest
+ fetch metadata. Filter reads these files and scores + LLM-reviews without
querying external services — the digest must be self-contained.

Platform-specific digests are typed separately so filter can pattern-match
by `platform` and trust the right field is populated.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class YouTubeDigest(BaseModel):
    """YouTube video digest pulled from `opencli youtube channel` recent_videos."""
    model_config = ConfigDict(extra="forbid")
    title: str
    duration: str
    views: str
    published: str          # e.g. "3 days ago" (opencli relative string)
    url: str                # full watch URL
    thumbnail_url: str      # i.ytimg.com/vi/{id}/hqdefault.jpg derived from video_id


class XDigest(BaseModel):
    """X (Twitter) post digest pulled from `opencli twitter search from:<user>`."""
    model_config = ConfigDict(extra="forbid")
    text: str
    author: str
    likes: int
    retweets: int
    views: int
    created_at: str
    url: str
    # opencli ≥ PR #1115 exposes media info; default empty so pre-PR items
    # validate fine.
    has_media: bool = False
    media_urls: list[str] = Field(default_factory=list)


class WebDigest(BaseModel):
    """Web RSS/Atom feed entry digest pulled from feedparser.

    `summary` is whatever the RSS feed chose to expose (often a meta description
    or the literal string "Comments" for HN). `full_body` is the result of an
    article re-fetch via trafilatura — clean markdown of the actual body, with
    YAML metadata header. Filter prefers full_body when present, falls back to
    summary when fetch failed (site down, paywalled, etc).
    """
    model_config = ConfigDict(extra="forbid")
    title: str
    summary: str            # entry.summary or entry.description, truncated
    link: str               # article canonical URL
    published_at: str       # ISO8601, empty if entry lacks a parseable date
    full_body: str | None = None  # trafilatura extract; may be None on fetch failure


class TikTokDigest(BaseModel):
    """TikTok digest pulled from yt-dlp + TikTok page metadata."""
    model_config = ConfigDict(extra="forbid")
    media_kind: Literal["video", "photo", "audio_or_cover_only"]
    title: str
    url: str
    uploader: str
    duration_seconds: float | None = None
    timestamp: int | None = None
    upload_date: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    repost_count: int | None = None
    thumbnail_url: str | None = None
    has_video_stream: bool
    photo_count: int = 0
    photo_image_urls: list[str] = Field(default_factory=list)
    audio_url: str | None = None


class ContentItem(BaseModel):
    """One queue item. Filename convention: `{fetched_at}_{platform}_{content_id}.json`
    where fetched_at is compact YYYYMMDDTHHMMSS and content_id is sanitised."""
    model_config = ConfigDict(extra="forbid")
    content_id: str
    source_id: str
    topic: str
    platform: Literal["youtube", "x", "web", "tiktok"]
    fetched_at: str
    source_of: Literal["patrol", "discovery"]
    # Exactly one of the digest fields is populated per item,
    # chosen by `platform`.
    youtube: YouTubeDigest | None = None
    x: XDigest | None = None
    web: WebDigest | None = None
    tiktok: TikTokDigest | None = None


class ContentScore(BaseModel):
    """Filter-phase continuous scoring — audit trail of why content passed/failed.

    Each sub-score is normalised to [0,1]; `composite` is the weighted sum
    using weights from `runtime.filter.score_weights`."""
    model_config = ConfigDict(extra="forbid")
    popularity: float
    engagement: float
    freshness: float
    preference: float
    composite: float
