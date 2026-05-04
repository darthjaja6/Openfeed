"""Extract 2-3 key takeaways from an admitted content item (web/X only).

Runs in filter right after `admit_content`, before the item lands in queue.
YouTube doesn't go through this — Ticlawk renders video cards natively.

Prompt content is deliberately a stub — the real instructions / rubric /
examples are owned by the user and routed through codex (see CLAUDE.md's
code style rule "Don't write LLM prompts").
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openfeed.models.content_item import ContentItem
from openfeed.models.interests import InterestEntry
from openfeed.models.persona import PersonaOutput


class Takeaway(BaseModel):
    """One carousel page's worth of editorial content."""
    model_config = ConfigDict(extra="forbid")
    heading: str = Field(description="a short sub-heading (8-20 chars) for this page")
    body: str = Field(
        description=(
            "the body text for this page. Use '\\n\\n' between paragraphs "
            "if you want separate paragraphs."
        ),
    )


class KeyTakeaways(BaseModel):
    """Editorial card output: a cover subtitle + several takeaway pages.

    Rendered downstream as:
      - Cover page: article title (from the source) + `subtitle` as hero
      - Middle pages: one page per `Takeaway`, showing `heading` + `body`
    """
    model_config = ConfigDict(extra="forbid")
    subtitle: str = Field(
        description=(
            "one punchy sentence for the cover page, sits under the article "
            "title as a hero statement"
        ),
    )
    takeaways: list[Takeaway] = Field(
        description="the ordered list of takeaway pages that tell the story",
    )


def build_key_takeaway_prompt(
    *,
    content_text: str,
) -> list[dict[str, Any]]:
    """Build messages for the takeaway extraction call.

    `content_text` is the already-fetched clean body (trafilatura output)
    for web items, or the raw post text for X. Caller supplies it so this
    module doesn't re-derive from the digest.
    """
    system_prompt = f"""You will be given the raw text of a web page and
    your job is to reorganize it into several takeaways that can be presented
    to the user just like turning a long article into a slide deck.
    The takeways could be important insights, key numbers, or incisive quotes
    but not limited to them - imagine a eyecatching social media post that conveys
    simple and powerful messages.
    Each takeaway should be a paragraph that is concise but attractive enough to
    catch the user's attention. All takeaways should make a good story.
    )"""
    user_prompt = f"""
Content:
{content_text}"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
        {"role": "user",
         "content": (
             f"""Your output format should be like: {KeyTakeaways.model_json_schema()}
             Now your response is:"""
         )},
    ]
