"""Card-producer contract.

`CardPayload` is what push hands to `ticlawk.push_card(**payload)`, so its
field names mirror the Ticlawk /api/cards POST body. One producer, one
`render(item, ctx) → CardPayload | None` method — producers encapsulate the
entire "ContentItem → Ticlawk card" transformation (LLM calls, HTML
rendering, title selection, video_id extraction).

`RenderContext` carries resources the producer might need (LLM runner,
persona, topic_data). We pass it explicitly rather than making producers
load globals, so tests and smoke scripts can inject mocks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from openfeed.clients.llm import GeminiRunner
from openfeed.models.content_item import ContentItem
from openfeed.models.interests import InterestEntry
from openfeed.models.persona import PersonaOutput


class CardPayload(BaseModel):
    """Ready-to-ship card body. Stored on `QueueItem.rendered_card` and handed
    to ticlawk.push_card. Fields map to ticlawk's POST /api/cards body.
    `thumbnail_path` is a local image file sent as multipart `thumbnail`.

    Subtypes ticlawk supports:
      - "html"          → raw html in `html` field (web + X)
      - "video"         → local mp4 path in `video_path`; Ticlawk accepts it
                          as multipart `video` on POST /api/cards
      - "gallery"       → local image paths in `image_paths`; Ticlawk accepts
                          them as repeated multipart `images` fields
      - "youtube_video" → legacy native IFrame embed via `video_id`. Kept in
                          schema for compat but no current renderer emits it
                          — ticlawk webview reliably trips YouTube's bot
                          challenge on this path.
    """
    model_config = ConfigDict(extra="forbid")
    title: str
    content_subtype: Literal["html", "video", "gallery", "youtube_video"]
    html: str | None = None
    video_id: str | None = None
    video_asset_id: str | None = None
    image_asset_ids: list[str] | None = None
    video_path: str | None = None
    image_paths: list[str] | None = None
    thumbnail_path: str | None = None
    render_fingerprint: str | None = None


@dataclass
class RenderContext:
    """Resources for producer.render. `topic_data` is the resolved
    `InterestEntry` for this item's topic (None if the topic is orphaned)."""
    runner: GeminiRunner
    persona: PersonaOutput
    topic_data: InterestEntry | None


class CardProducer(Protocol):
    """Structural type. Implementations live under `card_producers/<name>/`."""
    name: str

    def render(self, item: ContentItem, ctx: RenderContext) -> CardPayload | None:
        ...


def get_producer(name: str) -> CardProducer:
    """Registry lookup. Kept as a static dict — adding a producer is 1 line
    here + 1 new directory."""
    from openfeed.card_producers.ticlawk import PRODUCER as _TICLAWK

    registry: dict[str, CardProducer] = {
        "ticlawk": _TICLAWK,
    }
    if name not in registry:
        raise KeyError(f"unknown card producer: {name!r}; available: {sorted(registry)}")
    return registry[name]
