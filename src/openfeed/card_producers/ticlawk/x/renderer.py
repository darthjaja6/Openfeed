"""X renderer — structured data for user-owned HTML templates.

OpenFeed normalizes the post text, author, URL, metrics, and media URLs. The
outer template owns pagination, page order, dots, media treatment, and visual
style.
"""
from __future__ import annotations

import re

from openfeed.card_producers.base import CardPayload, RenderContext
from openfeed.card_producers.ticlawk.html_template import (
    build_html_card_render_input,
    html_template_fingerprint,
    render_html_template,
    resolve_html_template,
)
from openfeed.models.content_item import ContentItem


# Trailing t.co shorteners that X auto-appends when media is attached —
# redundant on media posts since we already show the media visually.
_TRAILING_TCO_PAT = re.compile(r"(?:\s+https?://t\.co/\S+)+\s*$")


class XRenderer:
    def render(self, item: ContentItem, ctx: RenderContext) -> CardPayload | None:
        if item.x is None:
            return None
        text = (item.x.text or "").strip()
        if not text:
            return None
        # When the post has media, trailing `https://t.co/...` shorteners
        # are just X's auto-appended link to the media itself — redundant
        # because we render the media as its own page.
        if item.x.has_media:
            text = _TRAILING_TCO_PAT.sub("", text).rstrip()

        # Ticlawk feed-preview title = first line, capped.
        first_line = text.split("\n", 1)[0]
        title = (first_line[:60] + "…") if len(first_line) > 60 else first_line
        author = (item.x.author or "").strip() or "unknown"
        author_url = (
            f"https://x.com/{author}" if author != "unknown" else item.x.url
        )

        template_path = resolve_html_template(item, ctx)
        render_input = build_html_card_render_input(
            item,
            ctx,
            title=title,
            url=item.x.url,
            normalized={
                "text": text,
                "raw_text": item.x.text,
                "author": author,
                "author_url": author_url,
                "url": item.x.url,
                "has_media": item.x.has_media,
                "media_urls": item.x.media_urls,
                "metrics": {
                    "likes": item.x.likes,
                    "retweets": item.x.retweets,
                    "views": item.x.views,
                },
                "created_at": item.x.created_at,
            },
            derived={
                "display_text": text,
            },
        )
        html = render_html_template(template_path, render_input)
        return CardPayload(
            title=title,
            content_subtype="html",
            html=html,
            render_fingerprint=self.render_fingerprint(item, ctx),
        )

    # ------------------------------------------------------------------

    def render_fingerprint(self, item: ContentItem, ctx: RenderContext) -> str:
        return html_template_fingerprint(
            resolve_html_template(item, ctx),
            renderer_name="ticlawk.x.html-card-render-input",
        )
