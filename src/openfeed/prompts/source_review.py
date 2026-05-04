"""Source review decision — text-only judgement using per-sample ContentUnderstandings.

Per the new architecture, source review reuses `content_understanding` as
a per-sample perception primitive instead of having its own. The caller
(youtube_source_review / discover):
  1. Picks N sample items from the candidate source (3 videos / 10 tweets /
     10 feed entries depending on platform)
  2. For each sample, runs content_understanding (with platform-appropriate
     media: YouTube frames, X/Web text-only)
  3. Calls this module with the list of ContentUnderstandings + a caller-
     formatted `source_info` block → SourceReviewDecision

No LLM call lives in this module — it just builds the decision prompt.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from openfeed.models.interests import InterestEntry
from openfeed.prompts.content_understanding import ContentUnderstanding
from openfeed.models.persona import PersonaOutput
from openfeed.prompts.interest_bootstrap import BACKGROUND


class SourceReviewDecision(BaseModel):
    """Three reason strings + final admit/reject + a short reason_code slug."""
    model_config = ConfigDict(extra="forbid")
    topic_fit_reason: str = Field(description="reasoning on whether the content source meets the topic fit criteria.")
    persona_fit_reason: str = Field(description="reasoning on whether the content source meets the persona fit criteria.")
    language_fit_reason: str = Field(description="reasoning on whether the content source meets the language fit criteria.")
    decision: Literal["admit", "reject"] = Field(description="admit or reject. Only admit if the content source meets ALL the criteria.")
    reason_code: str = Field(description="short slug like topic_fit / off_topic / persona_mismatch / generic_filler / clickbait / low_signal / spammy / language_mismatch")


def flatten_decision_reasoning(decision: SourceReviewDecision) -> str:
    """Join the per-criterion reasons into a single blob for ledger /
    catalog storage."""
    return (
        f"Topic fit: {decision.topic_fit_reason}\n"
        f"Persona fit: {decision.persona_fit_reason}\n"
        f"Language fit: {decision.language_fit_reason}"
    )


def build_source_review_prompt(
    *,
    source_info: str,
    sample_understandings: list[ContentUnderstanding],
    topic_data: InterestEntry,
    persona: PersonaOutput,
    language_preferences: list[str],
) -> list[dict[str, Any]]:
    """Build the source-review decision message — text only.

    `source_info` is caller-formatted source metadata (channel name / subs /
    bio / feed title / etc.). `sample_understandings` is a list of
    ContentUnderstandings produced upstream, one per sample item shown
    to the LLM when forming its mental model of the source."""
    system_prompt = f"""{BACKGROUND}
You will be given metadata about a candidate content source plus factual
descriptions of several sample items from that source. You will also be
given the user's topic of interest, persona, and language preferences.

Judge whether this source is a good source for the topic and user, on three
criteria:

1. **Topic fit** — the source's body of work FOCUSES on the topic. Topic fit
   is true only when the source's content exactly matches or is a subset of
   the topic. If the source covers a broad range of topics that merely
   include this one, it's NOT a fit.
2. **Persona fit** — the source's likely audience meaningfully overlaps
   with the user's persona with no fundamental mismatch (e.g. teenager-
   oriented channel ≠ retiree persona).
3. **Language fit** — the source publishes in a language the user reads.

First produce reasoning for each criterion, citing evidence from the source
metadata and sample descriptions. Then give a final admit/reject decision.
A source can be admitted only if it meets ALL three criteria.
"""

    samples_block_lines = []
    for i, u in enumerate(sample_understandings, start=1):
        samples_block_lines.append(
            f"Sample {i}:\n  what it is: {u.understanding}\n  language: {u.language}"
        )
    samples_block = "\n\n".join(samples_block_lines) if samples_block_lines else "(no samples)"

    user_text = f"""Topic: {topic_data.topic}
Topic description: {topic_data.description}
User persona: {persona.demographics}
User language preferences: {language_preferences}

Candidate source information:
{source_info}

Sample items from this source (factual descriptions):
{samples_block}

Your output format should be like: {SourceReviewDecision.model_json_schema()}
Now your response is:"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]
