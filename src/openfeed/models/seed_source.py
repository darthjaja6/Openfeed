"""Seed source schemas — LLM-proposed source candidates during bootstrap.

`SeedSource` describes a single candidate publisher (handle or URL) that the
bootstrap LLM has suggested as a high-quality source for a topic × platform.
`TopicPlatformSources` is the LLM's output container: one topic × platform
combo → list of SeedSources.

Kept in `models/` because these are pure data schemas (the prompt builder
uses them as output schemas, and `ValidatedSource` keeps a reference to
the originating seed on disk via `seed: SeedSource | None`).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SeedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Per-platform format (enforced in prompt, not at schema):
    #   web     → full URL of the content list page
    #   x       → user handle starting with @
    identifier: str = Field(description="""
the platform-specific identifier for this source.
for web: full URL of the LIST PAGE where new content appears (e.g. "https://www.xxx.com/news",
NOT the bare domain "xxx.com", NOT a single article URL).
If a site only has one obvious blog index at root, use that ("https://xxx.com/").
for X: user handle starting with @ (e.g. @xxx)
""")
    name: str = Field(description="the name of the source")
    reason: str = Field(description="ONE sentence (≤25 words) on why this source is structurally a fresh high-quality publisher for this topic × platform.")


class TopicPlatformSources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(description="the topic this source covers")
    platform: Literal["youtube", "x", "web", "tiktok"] = Field(description="the platform this source is on")
    sources: list[SeedSource] = Field(description="the list of seed sources for this topic and platform")
