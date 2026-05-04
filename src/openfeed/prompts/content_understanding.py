"""Content understanding — reusable perception primitive.

Generic "text + optional images → factual understanding + language" LLM
call, consumed by:
  - content_review (per-video judgement in filter)
  - source_review (per-sample-video understanding across 3 samples,
    aggregated into a source-level decision)

Input is caller-formatted text plus zero-or-more image bytes. The prompt
deliberately does NOT mention the topic/persona/taste the content will
later be judged against — we want a clean factual description that the
downstream review pass can reason over without topic bias seeping into
perception.
"""
from __future__ import annotations

import base64
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openfeed.prompts.interest_bootstrap import BACKGROUND


class ContentUnderstanding(BaseModel):
    """Factual description of a single piece of content. No topic context."""
    model_config = ConfigDict(extra="forbid")
    understanding: str = Field(
        description="A precise, factual description of what the content "
                    "actually shows/says — the subject, setting, what happens."
    )
    language: str = Field(
        description="Your best-effort inference of the primary language(s) "
                    "the content is in. You can tell from the text or visual data."
    )


def _image_bytes_to_data_url(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def build_content_understanding_prompt(
    *,
    text: str,
    images: list[bytes] | None = None,
) -> list[dict[str, Any]]:
    """Build a multimodal message asking the LLM to describe the content.

    `text` is any caller-formatted block (title + duration for a YouTube
    video, post body for an X tweet, title + summary for a web article, or
    a channel's metadata block for source_review's aggregate input).

    `images` is zero-or-more image byte blobs. Frame extraction / thumbnail
    fetching / media download happen in the caller — this function doesn't
    know or care where the bytes came from.
    """
    system_prompt = f"""{BACKGROUND}
    Your job is generating a one-sentence factual description of what this content
    is about. You will be provided text content and possibly one or more images
    extracted from the content (e.g. video frames or attached images).
    Be mindful of titles that can be clickbait or misleading; focus on describing
    the actual content. The title can be a useful signal but don't take it at face
    value — cross-check against what the images actually show.
    """

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
    ]

    if images:
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for img in images:
            parts.append({
                "type": "image_url",
                "image_url": {"url": _image_bytes_to_data_url(img)},
            })
        messages.append({"role": "user", "content": parts})
    else:
        messages.append({"role": "user", "content": text})

    messages.append({
        "role": "user",
        "content": (
            f"Your output format should be like: {ContentUnderstanding.model_json_schema()}\n"
            "Now your response is:"
        ),
    })
    return messages
