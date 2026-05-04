from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HTMLCardRenderInput(BaseModel):
    """Stable data contract passed to user-owned HTML card templates."""

    model_config = ConfigDict(extra="forbid")
    card: dict[str, Any] = Field(default_factory=dict)
    topic: dict[str, Any] | None = None
    consumer: dict[str, Any] | None = None
