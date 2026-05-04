from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from openfeed.models.interests import InterestEntry, InterestsConfig
from openfeed.models.persona import PersonaOutput
from openfeed.models.seed_source import SeedSource, TopicPlatformSources

BACKGROUND = """
We are working on social media content curation and search planning for a user
on a specific topic.
"""


# Calibration anchors for the three per-topic temporal fields bootstrap emits
# on each InterestEntry. Keyed by CONCRETE TOPIC EXAMPLES — not archetype
# names — so the LLM matches by analogy to a real topic rather than bucketing
# into a taxonomy. Each anchor carries a short reasoning line showing WHY
# these numbers fit that topic; the LLM is asked to produce its own reasoning
# for the user's topic the same way.
#
# Units: days throughout. Invariants (approx): patrol ≤ half_life/2 ≤ max_age/8.
#
# Spread is intentional — pet clips and celebrity gossip are both nominally
# "entertainment" but sit on opposite ends of the freshness spectrum; the
# table has to teach that distinction by example.
FRESHNESS_ANCHORS: dict[str, dict[str, int | str]] = {
    "Global Affairs/military news": {
        "max_content_age_days": 3,
        "freshness_half_life_days": 2,
        "reasoning": """This topic evolves extremely rapidly;
        news from 3 days ago is already stale, and we want to
        catch breaking news within hours.""",
    },
    "cars": {
        "max_content_age_days": 60,
        "freshness_half_life_days": 30,
        "reasoning": """Car models and trends evolve on a monthly cadence"""
    },
    "history": {
        "max_content_age_days": 1825,
        "freshness_half_life_days": 365,
        "reasoning": "history content don't age — a history content from 5"
        "years ago is just as good as one from yesterday.",
    },
}

# --- Pass 1.5: per-topic temporal shape (patrol / max_age / half_life) -------
#
# Runs once per topic. Output lands on the topic's InterestEntry in
# openfeed.yaml. Bootstrap preserves any value the user already set
# and asks the LLM only to fill in the rest. See FRESHNESS_ANCHORS (above)
# for the calibration table the prompt hands to the model.


class TopicTemporalOutput(BaseModel):
    """Three temporal knobs in days + a reasoning line that justifies them.

    Bounds mirror FRESHNESS_ANCHORS. `reasoning` is a plain-text explanation
    of WHY these three numbers fit this topic — the same shape as the
    `reasoning` strings in the anchor table. We don't persist it, but forcing
    the LLM to articulate the logic keeps the three numbers internally
    consistent and makes bad outputs easy to spot in the log."""
    model_config = ConfigDict(extra="forbid")
    reasoning: str = Field(
        description="one sentence explaining why these three numbers fit "
                    "this topic — following the style of the `reasoning` "
                    "lines in the anchor table."
    )
    max_content_age_days: int = Field(ge=3, le=1825)
    freshness_half_life_days: int = Field(ge=1, le=1800)


def build_topic_temporal_prompt(
    *,
    topic: str,
    topic_description: str,
    persona: PersonaOutput,
) -> str:
    return f"""{BACKGROUND}
You will now determine the temporal dynamics of the topic for content curation
and search purposes. This includes:
- Max content age: how old content can be before it's no longer relevant.
- Freshness half-life: how quickly content in this topic becomes stale.

The topic and user persona information
Topic: {topic}
Description: {topic_description}
Persona: {persona.demographics}

When determining these values, consider the nature of the topic and how quickly it evolves.
Generally in fast-moving topics like news or trends,
max content age should be low and freshness half-life should be short.
In topics where value of content don't decay much over time and high quality content stays relevant
even after a long time, these values should be higher.
You can refer to this calibration table (archetype → (low, high) range per field, in days):
{json.dumps(FRESHNESS_ANCHORS, indent=2)}

Your output will be in this format: {TopicTemporalOutput.model_json_schema()}.
"""


# --- Pass 2a: per-(topic, platform) seed sources (web + x only) ---------------
#
# A seed source is "the source on this platform that publishes the highest
# frequency, freshest, highest-quality content for this topic and persona".
# Defined STRUCTURALLY — by the source's nature as a fresh-content publisher —
# not by "what posted today".


def build_seed_sources_prompt(
    *,
    topic: str,
    topic_description: str,
    platform: str,
    persona: PersonaOutput,
    language_preferences: list[str],
) -> str:
    return f"""{BACKGROUND}
Your job is to come up with a list of 5-10 sources for topic {topic} on platform {platform}.
Detail about the topic: {topic_description}
Detailed demographics of the user: {persona.demographics}
User prefers langauges in: {language_preferences}

A seed source is "the source on this platform that publishes the highest-frequency, freshest,
highest-quality content for this topic and persona". This is a STRUCTURAL property —
pick sources that, by their nature, are reliable fresh-content publishers for this topic.
NOT "the source that posted something today". Bias toward concrete, well-known sources you
actually know exist; prefer omitting over inventing a plausible-sounding URL/handle that may not be real.


Your output will be in this format: {TopicPlatformSources.model_json_schema()}.
"""


# --- Pass 2b: per-topic YouTube channel-search keywords ------------------------
#
# Different approach for YouTube specifically: LLM is unreliable at knowing
# niche channel handles, but YouTube itself indexes channels by topic. So we
# ask the LLM to produce video-title-style search queries, then run them
# through `search.list?type=channel` at bootstrap to find real channels.


class TopicYouTubeChannelKeywords(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(description="the topic, exactly matching one of the user's configured topics")
    keywords: list[str] = Field(description="8-12 YouTube channel-search keywords, ≤6 words each")


def build_youtube_channel_keywords_prompt(
    *,
    topic: str,
    topic_description: str,
    persona: PersonaOutput,
    language_preferences: list[str],
) -> str:
    return f"""{BACKGROUND}
Your job is to come up with 8-12 YouTube channel search keywords for topic {topic}.
Detail about the topic: {topic_description}
Detailed demographics of the user: {persona.demographics}
User prefers languages in: {language_preferences}. The language you choose for the keywords
should be consistent with these preferences.

These keywords will be fed into youtube search api to surface
channels that, by their nature, are reliable and popular fresh-content publishers for this topic.

You are writing search queries optimised for YouTube's own channel index. Good
keywords match how creators in this topic actually title their videos and how
YouTube clusters channels by topic. Think: what would a savvy user type into
YouTube's search box to find a new creator in this topic?

What works:
- Title-style phrases creators use in their videos
- Genre / format names
- Platform-native style terms
- Named formats / franchises / recurring segments.

What doesn't:
- Generic SEO phrases ("best ... 2026", "top creators", "guide to").
- Pure theory / article-style phrasing — creators don't title videos
  "An examination of X".

Your output will be in this format: {TopicYouTubeChannelKeywords.model_json_schema()}.
"""


def build_keywords_from_topic_prompt(
    *,
    topic: str,
    topic_description: str,
    platform: str,
    persona: PersonaOutput,
    language_preferences: list[str],
) -> str:
    """Fallback when the topic has zero validated sources."""
    payload = {
        "topic": topic,
        "topic_description": topic_description,
        "platform": platform,
        "persona": persona.model_dump(),
        "language_preferences": language_preferences,
    }
    platform_rules = _keyword_platform_rules(platform)
    return f"""{BACKGROUND}
Generate execution-level keywords for one topic. These keywords will later be used
to (a) search {platform} for new candidate sources and (b) score whether incoming
content fits the topic.

This topic has NO validated sources yet (bootstrap couldn't confirm any), so you
must work from the persona + topic description alone. That means keywords are
necessarily speculative — bias toward concrete named entities (creators, products,
tools, sub-genres) that this persona would plausibly consume in this topic,
rather than abstract category words.

Platform-specific search guidance:
{platform_rules}

Input:
{json.dumps(payload, ensure_ascii=False, indent=2)}

Rules:
- Produce 10-20 keywords total.
- Keywords should be specific enough to act as real search queries (≤6 words each).
- Mix concrete named entities you confidently know exist with broader thematic
  phrases for the topic.
- Avoid generic SEO listicle vocabulary ("best ... 2026", "top frameworks", etc.).
- Use language consistent with `language_preferences`.
- Return JSON only.
"""


def _keyword_platform_rules(platform: str) -> str:
    if platform == "tiktok":
        return """
- These keywords will be fed directly into TikTok's native search.
- Write short TikTok-native queries that surface creator/video candidates, not
  article/web concepts.
- Prefer phrases that match TikTok/Douyin/Xiaohongshu short-video formats,
  captions, aesthetics, and creator niches.
- Avoid tutorial/review/how-to/product-shopping terms unless the topic
  description explicitly asks for instructional or cosmetics content.
- If an English topic word is ambiguous on TikTok, follow the concrete topic
  description instead of the generic English category meaning.
"""
    if platform == "youtube":
        return """
- These keywords will be fed into YouTube search.
- Prefer video-title-style phrases, creator niches, formats, and recurring
  series names.
"""
    if platform == "x":
        return """
- These keywords will be fed into X search.
- Prefer handle/community terms, recurring phrases, and short topic labels that
  creators actually use in posts.
"""
    if platform == "web":
        return """
- These keywords will be used for web/source discovery.
- Prefer publication/domain/topic phrases and recurring coverage areas.
"""
    return "- Use platform-native search phrases for this platform."



class KeywordsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(
        description="""
        10-20 search keywords that meett the requirements on the specified platform.""")

def build_keywords_from_samples_prompt(
    *,
    topic_data: InterestEntry,
    platform: str,
    persona: PersonaOutput,
    language_preferences: list[str],
    source_samples: list[dict[str, str]],
) -> str:
    """`source_samples` is a list of {source, platform, title|body}."""
    platform_rules = _keyword_platform_rules(platform)
    return f"""{BACKGROUND}
Your will be given the demographics of the user, a topic they are interested
in, and a selected set of content samples under this topic.
Your job: produce keywords that capture **what's currently
important or trending in this topic** — the topic's zeitgeist as revealed by
the recent content its validated sources are publishing.

This is NOT a summarisation task. You are not asked "what does this content
talk about". You are asked "what does this body of samples suggest the topic
audience actually cares about right now — what entities, topics, debates,
people keep showing up?" - that can be used to search for more
content in the same vein.

How to derive keywords (in this order):
1. Scan the samples for "what matters now".
2. Identify **topic clusters** — themes that several pieces circle around even
   if they use different words. Name those clusters as short search phrases.
3. Pick up on **timely signals** — new releases, ongoing debates, momentum
around specific topics.
4. ONLY after the above, fall back to slightly broader thematic phrases if you
   need to round out coverage.

What NOT to do:
- DO NOT include a keyword that is literally one sample's title/headline. A
  single content title is a one-off, not a trend.
- DO NOT produce generic SEO vocabulary ("best X 2026", "top xxx",
  "guide to Y", "trends").
- DO NOT invent named entities that aren't grounded in either the samples or
  persona/topic context.

Topic: {topic_data.topic}
Details about this topic: {topic_data.description}
Target platform: {platform}
Detailed demographics of the user: {persona.demographics}
User prefers languages in: {language_preferences}
Samples of recent content from validated sources in this topic:
{json.dumps(source_samples, ensure_ascii=False, indent=2)}

Platform-specific search guidance:
{platform_rules}

Rules:
- Produce 10-20 keywords total.
- Each keyword ≤6 words, usable verbatim as a search query on {platform}.
- Use language consistent with `language_preferences`; bilingual topics should
  produce keywords in both languages where natural.

Your output will be in this format: {KeywordsOutput.model_json_schema()}.
"""
