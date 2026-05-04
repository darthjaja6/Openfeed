"""state/learn_state.json — learn task's progress cursor.

Lean by design: just enough to answer "where did we leave off?" Per-source
posterior + evidence counters live on `SourceEntry` itself (PRD's split-state
principle), so this file only carries the consumption pointer into
`feedback.jsonl` and run bookkeeping.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LearnState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    feedback_offset: int = 0     # number of feedback.jsonl rows already consumed
    last_run_at: str = ""        # ISO8601 of last successful run end
    cycle_num: int = 0           # incremented each successful run; used by retire-cycle counting
    # Per-topic last-expansion timestamp for the search-terms keyword-proposal
    # phase. Positive-feedback rows observed at-or-before this timestamp don't
    # count toward the next trigger — prevents the same batch firing repeated
    # re-expansions. (Old field name `topic_guidance_last_updated` is silently
    # dropped on load via extra="ignore".)
    keyword_proposal_last_at: dict[str, str] = Field(default_factory=dict)
