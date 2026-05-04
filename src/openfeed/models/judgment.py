from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class JudgmentEvent(BaseModel):
    """One admit/reject decision, appended to ledgers/source_judgments.jsonl."""

    model_config = ConfigDict(extra="forbid")

    ts: str
    event_type: Literal[
        "admit_source", "reject_source",
        "admit_content", "reject_content",
        "retire_source",
    ]
    platform: Literal["youtube", "x", "web", "tiktok"]
    topic: str
    # For source events: canonical source id. For content events: content_id
    # (video_id / post_id / web canonical URL). `source_id` is reused as
    # `subject_id` conceptually — see decisions.jsonl audit conventions.
    source_id: str
    source_name: str
    reason_code: str
    reasoning: str | None = None
    matched_keywords: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
