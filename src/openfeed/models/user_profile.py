"""UserProfile shape — derived view over the configured `openfeed.yaml`.

Earlier this module read from `state/user_profile.json`, a redundant copy
written by bootstrap. That made config edits invisible at
runtime until bootstrap was re-run. Persona is user-immutable config,
not derived state, so it has no business living under `state/`.

`get_user_profile(workdir)` now reads `OPENFEED_CONFIG_FILE` directly
and returns a `UserProfile` view (persona + language_preferences). No
state file is touched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from openfeed.models.interests import load_interests
from openfeed.models.persona import PersonaOutput


class UserProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    generated_at: str
    persona: PersonaOutput


def get_user_profile(workdir: Path) -> UserProfile:
    """Build a UserProfile view from the configured `openfeed.yaml`.

    `generated_at` is set to "now" — the field is preserved for prompt
    callers that may want to render a freshness hint, but it no longer
    represents a persisted snapshot timestamp.

    `language_preferences` is no longer here — it lives per-topic on
    `InterestEntry` now. Callers that need it must look it up by topic."""
    cfg = load_interests(workdir)
    return UserProfile(
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        persona=PersonaOutput.model_validate(cfg.persona),
    )
