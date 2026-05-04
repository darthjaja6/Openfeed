"""Web renderer — structured data for user-owned HTML templates.

For web content items, the fetched article body (trafilatura) is fed to the
`key_takeaway` LLM which returns semantic card data. Pagination, page
construction, dots, and visual layout belong to the user-owned template.
"""
from __future__ import annotations

import logging

from openfeed.card_producers.ticlawk.html_template import (
    build_html_card_render_input,
    html_template_fingerprint,
    render_html_template,
    resolve_html_template,
)
from openfeed.card_producers.base import CardPayload, RenderContext
from openfeed.models.content_item import ContentItem
from openfeed.prompts.key_takeaway import (
    KeyTakeaways,
    build_key_takeaway_prompt,
)


_logger = logging.getLogger("producer.ticlawk.web")

class WebRenderer:
    def render(self, item: ContentItem, ctx: RenderContext) -> CardPayload | None:
        if item.web is None:
            return None
        title = item.web.title or item.content_id
        content = self._extract_card_content(item, ctx)
        if content is not None:
            subtitle = content.subtitle
            takeaways = content.takeaways
        else:
            # Fallback: LLM failed → first chunk of body as one takeaway, no
            # subtitle (cover will just show the title).
            body_text = (item.web.full_body or item.web.summary or "").strip()
            if not body_text:
                return None
            subtitle = ""
            takeaways = [Takeaway(heading="摘要", body=body_text[:320])]
        template_path = resolve_html_template(item, ctx)
        html = self._compose_html(
            item, ctx, template_path=template_path, title=title,
            article_url=item.web.link, subtitle=subtitle, takeaways=takeaways,
        )
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
            renderer_name="ticlawk.web.html-card-render-input",
        )

    def _extract_card_content(
        self, item: ContentItem, ctx: RenderContext,
    ) -> KeyTakeaways | None:
        if item.web is None:
            return None
        text = item.web.full_body or item.web.summary
        if not text:
            return None
        try:
            raw = ctx.runner.run_json(
                build_key_takeaway_prompt(content_text=text),
                schema=KeyTakeaways.model_json_schema(),
                schema_name=KeyTakeaways.__name__,
            )
            return KeyTakeaways.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("key_takeaway failed for %s: %s", item.content_id, exc)
            return None

    def _compose_html(
        self, item: ContentItem, ctx: RenderContext, *, template_path,
        title: str, article_url: str,
        subtitle: str, takeaways,
    ) -> str:
        render_input = build_html_card_render_input(
            item,
            ctx,
            title=title,
            url=article_url,
            normalized={
                "title": title,
                "url": article_url,
                "summary": item.web.summary if item.web else "",
                "full_body": item.web.full_body if item.web else None,
                "published_at": item.web.published_at if item.web else "",
            },
            derived={
                "subtitle": subtitle,
                "takeaways": [t.model_dump(mode="json") for t in takeaways],
            },
        )
        return render_html_template(template_path, render_input)
