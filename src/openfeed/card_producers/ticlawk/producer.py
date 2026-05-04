"""Ticlawk card producer — thin per-platform dispatcher.

Each platform owns its own renderer under a subdirectory (`youtube/`,
`web/`, `x/`). HTML templates are user-owned files configured per
topic/platform in `openfeed.yaml`. This
`TiclawkProducer` class is intentionally lean — it just routes to the
right renderer. Adding a new platform = new subdirectory + one line here.

Contract (see `card_producers/base.py`):
  `render(item, ctx) -> CardPayload | None`
"""
from __future__ import annotations

import logging

from openfeed.card_producers.base import CardPayload, RenderContext
from openfeed.card_producers.ticlawk.web import WebRenderer
from openfeed.card_producers.ticlawk.tiktok import TikTokRenderer
from openfeed.card_producers.ticlawk.thumbnails import ensure_thumbnail
from openfeed.card_producers.ticlawk.x import XRenderer
from openfeed.card_producers.ticlawk.youtube import YouTubeRenderer
from openfeed.models.content_item import ContentItem


_logger = logging.getLogger("producer.ticlawk")


class TiclawkProducer:
    name = "ticlawk"

    def __init__(self) -> None:
        self._renderers = {
            "youtube": YouTubeRenderer(),
            "web": WebRenderer(),
            "x": XRenderer(),
            "tiktok": TikTokRenderer(),
        }

    def render(self, item: ContentItem, ctx: RenderContext) -> CardPayload | None:
        renderer = self._renderers.get(item.platform)
        if renderer is None:
            _logger.warning("no renderer for platform %s (%s)",
                            item.platform, item.content_id)
            return None
        payload = renderer.render(item, ctx)
        if payload is None:
            return None
        return ensure_thumbnail(item, payload)

    def render_fingerprint(self, item: ContentItem, ctx: RenderContext) -> str | None:
        renderer = self._renderers.get(item.platform)
        if renderer is None:
            return None
        render_fingerprint = getattr(renderer, "render_fingerprint", None)
        if render_fingerprint is None:
            return None
        return render_fingerprint(item, ctx)
