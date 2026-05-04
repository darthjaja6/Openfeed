"""state/video_cache_index.json — what YouTube videos we have on disk.

Tracks per-video lifecycle: never-tried / ready / failed-with-backoff /
permanently-failed. Lives under `state/` (canonical state, regenerable
from `state/video_cache/` content + decisions ledger if lost).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


VideoCacheState = Literal["ready", "failed", "permanently_failed"]


class VideoCacheEntry(BaseModel):
    """One row of the index — current best-known state of `video_id`."""
    model_config = ConfigDict(extra="ignore")
    video_id: str
    state: VideoCacheState
    # Set when state=="ready". local_path is repo-root-relative.
    local_path: str | None = None
    size_bytes: int | None = None
    downloaded_at: str | None = None
    # Failure bookkeeping (counted across tries since last success).
    failure_count: int = 0
    last_failed_at: str | None = None
    last_error: str | None = None


class VideoCacheIndex(BaseModel):
    model_config = ConfigDict(extra="ignore")
    generated_at: str
    videos: dict[str, VideoCacheEntry] = Field(default_factory=dict)
