"""Content review decision — text-only judgement using a prior ContentUnderstanding.

Per PRD §5.3, filter's LLM review judges on three dimensions:
    1. topic fit
    2. user-taste fit (persona)
    3. language fit

(A fourth `has_quality` gate existed earlier — dropped because it proved
too coarse in practice; quality signal is better captured via per-source
Bayesian posterior from feedback.)

Perception happens in `content_understanding.py`; this module consumes the
understanding + topic + persona + language prefs and emits the 3-gate
decision. Text-only — no images here.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openfeed.models.content_item import ContentItem
from openfeed.models.interests import InterestEntry
from openfeed.prompts.content_understanding import ContentUnderstanding
from openfeed.models.persona import PersonaOutput
from openfeed.prompts.interest_bootstrap import BACKGROUND


class ContentReviewDecision(BaseModel):
    """Three binary gates. admit iff all three are True."""
    model_config = ConfigDict(extra="forbid")
    is_the_topic: bool = Field(description="true or false")
    topic_fit_reason: str = Field(description="reasoning on whether the content is the topic.")
    matches_user_taste: bool = Field(description="true or false")
    user_taste_fit_reason: str = Field(description="reasoning on whether the content matches the user's taste in this topic.")
    language_match: bool = Field(description="true or false")
    language_fit_reason: str = Field(description="reasoning on whether the content is in a language the user reads.")


def flatten_content_reasoning(decision: ContentReviewDecision) -> str:
    """One-blob reasoning for ledger / queue storage."""
    return (
        f"Topic fit: {decision.topic_fit_reason}\n"
        f"User taste: {decision.user_taste_fit_reason}\n"
        f"Language: {decision.language_fit_reason}"
    )


def content_text_block(item: ContentItem) -> str:
    """Render only the fields that carry content-perception signal for a
    ContentItem. Callers use this to build the `text` input to
    `build_content_understanding_prompt` and the "specifics" block in
    `build_content_review_prompt`.

    Dropped intentionally: source_name / platform / content_id / views /
    likes / retweets / published / posted / url — none of those tell the
    LLM WHAT the content is. Popularity is already captured upstream in
    `score`."""
    if item.platform == "youtube" and item.youtube is not None:
        d = item.youtube
        return f"""Title: {d.title}
Duration: {d.duration}"""
    if item.platform == "x" and item.x is not None:
        d = item.x
        return f"""Post text:
{d.text}"""
    if item.platform == "web" and item.web is not None:
        d = item.web
        # Prefer trafilatura-extracted full body (clean markdown w/ YAML frontmatter);
        # fall back to RSS summary when body fetch failed.
        body = d.full_body or d.summary
        return f"""Title: {d.title}

Content:
{body}"""
    if item.platform == "tiktok" and item.tiktok is not None:
        d = item.tiktok
        return f"""Title/description: {d.title}
Media kind: {d.media_kind}
Duration seconds: {d.duration_seconds}
Photo count: {d.photo_count}"""
    return ""


def build_content_review_prompt(
    *,
    content_item: ContentItem,
    content_understanding: ContentUnderstanding,
    topic_data: InterestEntry,
    persona: PersonaOutput,
    language_preferences: list[str],
) -> list[dict[str, Any]]:
    """Text-only decision message. Consumes the upstream ContentUnderstanding
    plus topic + persona + language preferences; emits the three-dimension
    judgement."""
    system_prompt = f"""{BACKGROUND}
        You will be given a concise description of our understanding about a
        content item, based on its media and text. You will also be given the
        topic we're trying to match it to, and a description of the user's
        persona and language preferences.
        Your task is to judge the content on the following dimensions:
1. Topic fit: does the content focus on the topic?
   Topic does not fit if the content just overlaps with the topic but isn't mainly about it.
2. User-taste fit: based on the user's persona and language preferences, would they like this content if they saw it?
3. Language fit: is the content in a language the user reads?"""

    user_prompt = f"""The content you are judging:
Content: {content_understanding.understanding}
Content language: {content_understanding.language}

The user's interest topic and preferences:
Topic: {topic_data.topic} - {topic_data.description}
User persona: {persona.demographics}
User language preferences: {language_preferences}

Content specifics:
{content_text_block(content_item)}"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
        {"role": "user",
         "content": (
             f"Your output format should be like: {ContentReviewDecision.model_json_schema()}\n"
             "Now your response is:"
         )},
    ]
