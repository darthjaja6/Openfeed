from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openfeed.utils.config_files import config_path, load_openfeed_config


SUPPORTED_PLATFORMS = ("youtube", "x", "web", "tiktok")
Platform = Literal["youtube", "x", "web", "tiktok"]


class PlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    html_template: str | None = None


class InterestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str
    description: str
    platforms: dict[Platform, PlatformConfig]

    # Per-topic consumer routing. `consumer_type` selects which adapter in
    # `clients/consumer/` handles push + feedback for this topic; the raw
    # `consumer_config` dict is validated against that adapter's pydantic
    # config model in `_validate_consumer` below. The previous `weight`
    # field (cross-topic push-time random + queue_manage refill proportion)
    # was removed once each topic got its own channel — buffer-driven
    # refill makes static priorities unnecessary.
    consumer_type: str
    consumer_config: dict[str, Any]

    # Per-topic content-language preference (was previously a top-level
    # union list — content/source review LLMs accepted any language in the
    # global set for every topic, which polluted niche topics with off-
    # language content). Now each topic declares its own preferred set;
    # callers thread `topic_data.language_preferences` into the review
    # prompt instead of the global list.
    language_preferences: list[str]

    # Per-topic temporal shape (days). Optional: user may omit and bootstrap
    # fills any unset field via LLM using FRESHNESS_ANCHORS as a rubric.
    # Bounds mirror the outer envelope of FRESHNESS_ANCHORS (see
    # openfeed.prompts.interest_bootstrap). User-set values are preserved
    # across bootstrap reruns.
    max_content_age_days: int | None = Field(default=None, ge=3, le=1825)
    freshness_half_life_days: int | None = Field(default=None, ge=1, le=1800)

    # Per-topic YouTube duration cap (seconds). Filter falls back to
    # `runtime.filter.youtube.duration_max_seconds` if unset. Useful for
    # swipe-feed UX tuning: Shorts-dominant topics (beauty, pets) cap at 60s,
    # tutorial / recap topics tolerate longer (600s). PRD §2 rationale.
    youtube_duration_max_seconds: int | None = Field(default=None, ge=15, le=3600)

    @field_validator("platforms")
    @classmethod
    def _platforms_non_empty(cls, value: dict[Platform, PlatformConfig]) -> dict[Platform, PlatformConfig]:
        if not value:
            raise ValueError("platforms must be non-empty")
        return value

    @model_validator(mode="after")
    def _validate_consumer(self) -> "InterestEntry":
        # Dispatch raw consumer_config through the registered consumer's
        # config model so adapter-specific keys (channel_id for ticlawk,
        # webhook_url for hypothetical-discord, …) get strict validation
        # at config load time, not at first push.
        from openfeed.clients.consumer import get_consumer
        spec = get_consumer(self.consumer_type)
        spec.config_model.model_validate(self.consumer_config)
        for platform in ("web", "x"):
            platform_config = self.platforms.get(platform)  # type: ignore[arg-type]
            if platform_config is not None and not platform_config.html_template:
                raise ValueError(f"{platform} requires platforms.{platform}.html_template")
        return self


class InterestsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # User-written persona — feeds the source/content review LLM as the
    # "who is this user being judged for" context. Free-form text;
    # demographics + behavioural traits the LLM should treat as ground truth
    # when deciding fit. See `models/persona.PersonaOutput` for the field
    # shape (currently a single `demographics` string).
    persona: dict[str, str]
    interests: list[InterestEntry]
    # NOTE: top-level `language_preferences` was removed in favour of
    # per-topic preference (see InterestEntry.language_preferences).


def load_interests(workdir: Path) -> InterestsConfig:
    """Read the interests section from the configured openfeed YAML."""
    del workdir
    raw = load_openfeed_config()
    try:
        payload = {"persona": raw["persona"], "interests": raw["interests"]}
    except KeyError as exc:
        raise ValueError(f"missing required config field {exc.args[0]!r} in {config_path()}") from exc
    return InterestsConfig.model_validate(payload)
