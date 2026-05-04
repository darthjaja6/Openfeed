"""state/video_assets.json — what we've already uploaded to ticlawk.

Lookup before re-uploading the same `video_id`: avoids burning ticlawk
storage on duplicates. Cleared by the cleanup task once a video is no
longer in queue / has aged out of history.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VideoAssetEntry(BaseModel):
    """One asset that lives on ticlawk. `url` is what we splice into the
    `<video src=...>` HTML card."""
    model_config = ConfigDict(extra="ignore")
    video_id: str
    asset_id: str
    url: str
    size_bytes: int
    uploaded_at: str


class VideoAssetIndex(BaseModel):
    model_config = ConfigDict(extra="ignore")
    generated_at: str
    assets: dict[str, VideoAssetEntry] = Field(default_factory=dict)
