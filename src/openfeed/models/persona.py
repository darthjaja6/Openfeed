"""User persona schema + loader.

`PersonaOutput` holds the demographic / behavioural sketch of the user.
It used to be an LLM output (bootstrap inferred persona from the user's
short description) but is now a pass-through from the configured
`openfeed.yaml` `persona:` block — bootstrap no longer rewrites it.

`load_persona` reads through `OPENFEED_CONFIG_FILE`.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class PersonaOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    demographics: str = Field(description="a concise best-guess of the user including age, range, gender, occupations, behavioral traits.")


def load_persona(workdir: Path) -> PersonaOutput:
    """Read the user's persona block from the configured `openfeed.yaml`."""
    from openfeed.models.interests import load_interests
    cfg = load_interests(workdir)
    return PersonaOutput.model_validate(cfg.persona)
