"""Content deep-diving — heavier per-item perception used by learn's
keyword-proposal phase (Stage 1 of the keyword-expansion pipeline).

Same shape as `content_understanding`, but we want richer structured output
than the single-sentence summary used at filter time. Stage 2 (keyword
proposal) aggregates these structured perceptions across positive examples
to find common visual / subject / setting patterns and propose new search
terms — title alone is too thin for that, especially on YouTube Shorts
where titles are often hashtag soup or clickbait.

Frames are caller-supplied; this module doesn't fetch or extract them.

Prompt content is intentionally a stub — final wording owned by codex per
the project rule. The schema and message structure ARE finalised here.
"""
from __future__ import annotations

import base64
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openfeed.models.persona import PersonaOutput
from openfeed.prompts.interest_bootstrap import BACKGROUND


class ContentDeepDiving(BaseModel):
    """Structured factual perception of one piece of content. No topic context."""
    model_config = ConfigDict(extra="forbid")
    deep_dive: str = Field(
        description="""Make a reasoning of how a particular part/aspect of the content
        triggers the user and give a detailed description of the
        particular part/aspect of the content that attracts the user rather than
        abstract, broad generalization.
        """,
    )

def _image_bytes_to_data_url(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def build_content_deep_diving_prompt(
    *,
    text: str,
    topic: str,
    topic_description: str,
    persona: PersonaOutput,
    images: list[bytes] | None = None,
) -> list[dict[str, Any]]:
    """Build a multimodal message asking the LLM for a detailed structured
    description of the content. Mirrors `build_content_understanding_prompt`
    in shape; richer schema + stronger emphasis on grounding the answer in
    what the frames actually show (Shorts thumbnails / titles are often
    misleading — the description must come from the visual evidence).
    """
    # TODO: codex — full prompt body. The rubric needs to (a) anchor the
    # LLM to what frames actually show vs. what title claims, and (b) push
    # for concrete / search-friendly noun phrases rather than vague vibes.
    system_prompt = f"""{BACKGROUND}
    We have collected user feedback on contents and want to dive deep into
    the reason why the user likes this contnet.
    Your job is to produce a detailed, factual description of one
    piece of content that user likes very much. You will be provided the topic
    that thte user wants, the user's persona, the content context and possibly
    one or more images extracted from the content (e.g. video frames). Describe
    ONLY what is actually visible in the frames. The title is a weak hint and
    is sometimes clickbait — do not assert anything that the frames don't show.
    The user sees a lot of content like this but please find out why the user
    particularly likes this one and give concrete details supporting your reasoning.
    Usually there is only one key reason so do not include multiple reasons, because
    the others might just be noise. To make the reasoning correct, you have to
    think deep about what the user wants from the topic and what this content
    gives, then find that one sweet spot that triggers the user's interest."""

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
    ]

    context_block = f"""This content is collected under this topic that user requests: {topic}
        User's description of this topic: {topic_description}
        User persona: {persona.demographics}
        {text}"""

    if images:
        parts: list[dict[str, Any]] = [{"type": "text", "text": context_block}]
        for img in images:
            parts.append({
                "type": "image_url",
                "image_url": {"url": _image_bytes_to_data_url(img)},
            })
        messages.append({"role": "user", "content": parts})
    else:
        messages.append({"role": "user", "content": context_block})

    messages.append({
        "role": "user",
        "content": f"""Your output format should be like: {ContentDeepDiving.model_json_schema()}
Now your response is:""",
    })
    return messages
