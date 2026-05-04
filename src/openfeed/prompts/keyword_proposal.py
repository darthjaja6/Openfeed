"""LLM prompt: propose new search keywords for one topic from positive
feedback examples.

Triggered by `learn` once enough positive examples have accumulated for a
topic since its last expansion. Output is a short list of new search-term
strings to extend `search_terms.json`'s active keyword pool — discover
uses these on the next run to surface fresh source candidates.

Negative examples are NOT fed to this LLM. Per project decision: keep this
loop simple and one-directional — positives in, new keywords out. Negative
signal stays in `learn`'s Bayesian source posterior + the search_term
retire path.

Prompt content here is a stub — final wording is owned by codex (CLAUDE.md
"Don't write LLM prompts" rule). The schema and message structure ARE
load-bearing and finalised here.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openfeed.models.persona import PersonaOutput
from openfeed.prompts.content_deep_diving import ContentDeepDiving


class KeywordProposalUpdate(BaseModel):
    """LLM output: short list of new search keywords to add to the active pool."""
    model_config = ConfigDict(extra="forbid")
    new_keywords: list[str] = Field(
        description="""List of new search-term strings to add to the topic's active keyword
pool. Each term should be a concrete search query (the kind a user
might type into YouTube/X search), grounded in the positive examples.
Keep them short (1-5 words) and platform-agnostic. Empty list if no
new keyword is meaningfully better than what's already there.""",
    )


class TopicExample(BaseModel):
    """One content example fed into the keyword-proposal prompt.

    `content_id` is the platform-native id (yt video id / x post id / web
    url) used by the deep-dive frame-source resolver. `discovered_by_keyword`
    is the search term that originally surfaced the source this content
    came from — gives Stage 2 the lineage trail (which keyword has been
    pulling positives) so it can propose adjacent terms. `perception` is the
    Stage-1 deep-dive output (frames + LLM); when present we render its
    structured fields in the prompt instead of title-only. `None` means
    deep-dive failed or wasn't run — caller falls back gracefully."""
    model_config = ConfigDict(extra="forbid")
    content_id: str
    title: str
    platform: str
    discovered_by_keyword: str | None = None
    perception: ContentDeepDiving | None = None


def _format_one_example(i: int, e: "TopicExample") -> str:
    """One numbered example block. Header line always; deep-dive body line
    only when perception is present."""
    lineage = (
        f"  (discovered via search term: {e.discovered_by_keyword!r})"
        if e.discovered_by_keyword else ""
    )
    header = f"{i}. [{e.platform}] {e.title}{lineage}"
    if e.perception is None:
        return header
    body = e.perception.deep_dive.replace("\n", "\n     ")
    return f"""{header}
   deep_dive : {body}"""


def _format_examples(examples: list["TopicExample"]) -> str:
    """Render a list of TopicExample as a numbered block joined by newlines."""
    if not examples:
        return "(none)"
    return "\n".join(
        _format_one_example(i, e) for i, e in enumerate(examples, start=1)
    )


def build_keyword_proposal_prompt(
    *,
    topic: str,
    topic_description: str,
    persona: PersonaOutput,
    positive_examples: list[TopicExample],
    active_keywords: list[str],
    retired_keywords: list[str],
    max_new_keywords: int,
) -> list[dict[str, Any]]:
    """Build messages. Prompt body is intentionally minimal — codex owns rewrite."""
    # TODO: codex — full prompt body. For now the schema + structured input
    # carry most of the contract.
    system_prompt = """We are building a personalized content feed for a user
and have now collected a set of content examples this user likes very much.
You will be given this set of content examples which are inside one topic,
a deep dive of the particular features that may have attracted the user,
plus the topic's currently-active and previously-retired
search keywords. Propose a small number of NEW search-term strings (queries the
user might type into YouTube/X search) that are likely to surface more content
of the same kind. Avoid keywords that overlap with active or retired terms.
Keep terms short, concrete, and platform-agnostic."""

    pos_block = _format_examples(positive_examples)
    active_block = ", ".join(f'"{k}"' for k in active_keywords) if active_keywords else "(none)"
    retired_block = ", ".join(f'"{k}"' for k in retired_keywords) if retired_keywords else "(none)"

    user = f"""Topic: {topic}
Topic description: {topic_description}
User persona: {persona.demographics}

Positive examples (user engaged):
{pos_block}

Currently-active search keywords for this topic:
{active_block}

Previously-retired keywords (avoid suggesting any term overlapping these):
{retired_block}

Propose at most {max_new_keywords} new search keywords."""

    schema_msg = f"""Output JSON exactly matching this schema: {KeywordProposalUpdate.model_json_schema()}
Now your response is:"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user},
        {"role": "user", "content": schema_msg},
    ]
