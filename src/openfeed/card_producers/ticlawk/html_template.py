from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from openfeed.card_producers.base import RenderContext
from openfeed.models.content_item import ContentItem
from openfeed.models.html_card import HTMLCardRenderInput
from openfeed.utils.config_files import config_path


def resolve_html_template(item: ContentItem, ctx: RenderContext) -> Path:
    if ctx.topic_data is None:
        raise ValueError(f"missing topic config for {item.topic!r}")
    platform_config = ctx.topic_data.platforms.get(item.platform)  # type: ignore[arg-type]
    if platform_config is None:
        raise ValueError(f"topic {item.topic!r} has no platform config for {item.platform!r}")
    if not platform_config.html_template:
        raise ValueError(f"topic {item.topic!r} platform {item.platform!r} has no html_template")
    template_ref = Path(platform_config.html_template)
    if template_ref.is_absolute():
        raise ValueError("html_template must be relative to openfeed.yaml")
    path = (config_path().parent / template_ref).resolve()
    if not path.is_file():
        raise ValueError(f"html_template does not exist: {path}")
    return path


def html_template_fingerprint(path: Path, *, renderer_name: str) -> str:
    digest = hashlib.sha256()
    digest.update(renderer_name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(path).encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    return digest.hexdigest()


def build_html_card_render_input(
    item: ContentItem,
    ctx: RenderContext,
    *,
    title: str,
    url: str,
    normalized: dict[str, Any] | None = None,
    derived: dict[str, Any] | None = None,
    assets: dict[str, Any] | None = None,
) -> HTMLCardRenderInput:
    topic_payload = ctx.topic_data.model_dump(mode="json") if ctx.topic_data else None
    consumer_payload = None
    if ctx.topic_data is not None:
        consumer_payload = {
            "consumer_type": ctx.topic_data.consumer_type,
            "consumer_config": ctx.topic_data.consumer_config,
        }
    return HTMLCardRenderInput(
        card={
            "content_id": item.content_id,
            "source_id": item.source_id,
            "topic": item.topic,
            "platform": item.platform,
            "fetched_at": item.fetched_at,
            "source_of": item.source_of,
            "title": title,
            "url": url,
            "raw": item.model_dump(mode="json"),
            "normalized": normalized or {},
            "derived": derived or {},
            "assets": assets or {},
        },
        topic=topic_payload,
        consumer=consumer_payload,
    )


def render_html_template(
    path: Path,
    render_input: HTMLCardRenderInput,
) -> str:
    root = config_path().parent.resolve()
    template_name = str(path.relative_to(root))
    env = Environment(
        loader=FileSystemLoader(str(root)),
        autoescape=select_autoescape(("html", "xml")),
        undefined=StrictUndefined,
    )
    template = env.get_template(template_name)
    context = {
        "HTMLCardRenderInput": render_input.model_dump(mode="json"),
        "card": render_input.card,
        "topic": render_input.topic,
        "consumer": render_input.consumer,
    }
    return template.render(context)
