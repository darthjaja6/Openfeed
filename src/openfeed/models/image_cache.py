"""state/image_cache_index.json — downloaded images for image cards."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ImageCacheState = Literal["ready", "failed", "permanently_failed"]


class ImageCacheEntry(BaseModel):
    """One row of the image cache index."""
    model_config = ConfigDict(extra="ignore")
    content_id: str
    platform: str
    state: ImageCacheState
    image_paths: list[str] = Field(default_factory=list)
    image_count: int = 0
    downloaded_at: str | None = None
    failure_count: int = 0
    last_failed_at: str | None = None
    last_error: str | None = None


class ImageCacheIndex(BaseModel):
    model_config = ConfigDict(extra="ignore")
    generated_at: str
    images: dict[str, ImageCacheEntry] = Field(default_factory=dict)
