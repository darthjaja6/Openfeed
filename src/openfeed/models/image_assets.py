"""state/image_assets.json — image assets uploaded to ticlawk for gallery cards."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ImageAssetEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content_id: str
    asset_ids: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    size_bytes: int = 0
    image_count: int = 0
    uploaded_at: str


class ImageAssetIndex(BaseModel):
    model_config = ConfigDict(extra="ignore")
    generated_at: str
    assets: dict[str, ImageAssetEntry] = Field(default_factory=dict)
