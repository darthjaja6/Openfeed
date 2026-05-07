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


class TopicStatus(BaseModel):
    """Inventory snapshot for one topic, produced by queue_manage."""
    model_config = ConfigDict(extra="forbid")
    inventory: int          # current count after expiration/resort
    pushable_inventory: int | None = None  # currently pushable without waiting for media prep
    blocked_inventory: int | None = None   # queued but not pushable yet
    target: int             # per-topic target (max(floor, topic_capacity))
    refill_gap: int         # max(0, target - pushable_inventory); ≥1 → refill signal
    floor: int              # floor that applied (copied from runtime)


class QueueStatus(BaseModel):
    """state/queue_status.json — queue_manage's output signal file.

    Consumed by patrol (to decide whether to collect and which topics to
    prioritise). Filter doesn't read this — it already processes whatever
    arrives in `queues/patrol/`."""
    model_config = ConfigDict(extra="forbid")
    generated_at: str
    total_inventory: int
    total_pushable_inventory: int | None = None
    topic_capacity: int
    per_topic: dict[str, TopicStatus]
    # Topics with refill_gap > 0, ordered gap-desc. Patrol should prefer
    # these when choosing which (topic, platform) slots to refresh.
    refill_topics: list[str]
