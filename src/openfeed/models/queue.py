"""state/queue.json schema — filter's output, push's input.

PRD §3.2: "待推送 content 列表，按 topic 分桶。每条含 source_id、content_id、
平台字段、rank_score". We carry the full ContentItem plus the composite
rank_score from filter, so downstream (push) can rank / cap without re-computing.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from openfeed.card_producers.base import CardPayload
from openfeed.models.content_item import ContentItem, ContentScore


class QueueItem(BaseModel):
    """A single admitted piece of content awaiting push."""
    model_config = ConfigDict(extra="ignore")
    content: ContentItem
    score: ContentScore
    rank_score: float               # equal to score.composite at admit time; may be overwritten by queue_manage
    admitted_at: str                # ISO8601
    # Opportunistic cache of the producer-rendered card. Filter admits with
    # None (renderer no longer runs at admit time). Push lazily renders when
    # selecting an item, then writes the result back here as an intermediate
    # checkpoint before calling ticlawk — so a ticlawk failure doesn't burn
    # the LLM tokens twice on next pickup.
    rendered_card: CardPayload | None = None


class Queue(BaseModel):
    """Full queue, bucketed by topic."""
    model_config = ConfigDict(extra="forbid")
    generated_at: str
    topics: dict[str, list[QueueItem]] = Field(default_factory=dict)


class SourceInventoryStatus(BaseModel):
    """Inventory snapshot for one active source, produced by queue_manage."""
    model_config = ConfigDict(extra="forbid")
    topic: str
    platform: str
    source_id: str
    queued_count: int
    pushable_count: int
    blocked_count: int
    source_floor: int
    refill_gap: int
    needs_refill: bool
    exhausted_until: str | None = None
    exhausted_reason: str | None = None
    last_patrolled_at: str | None = None


class TopicStatus(BaseModel):
    """Informational rollup for one topic.

    Supply decisions are source-level; these topic fields are for status,
    prioritisation, and discover escalation only.
    """
    model_config = ConfigDict(extra="forbid")
    queued_count: int
    pushable_count: int
    blocked_count: int
    active_source_count: int
    under_floor_source_count: int
    refill_source_count: int
    exhausted_source_count: int


class QueueStatus(BaseModel):
    """state/queue_status.json — queue_manage's source-level signal file."""
    model_config = ConfigDict(extra="forbid")
    generated_at: str
    source_floor: int
    total_queued_count: int
    total_pushable_count: int
    per_topic: dict[str, TopicStatus]
    per_source: dict[str, SourceInventoryStatus]
    # Active, non-exhausted sources below source_floor, ordered by gap desc.
    refill_sources: list[str]
