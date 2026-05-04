"""Feedback ledger + state schemas (PRD §3.2 / §5.6).

Two file shapes:

  * `ledgers/feedback.jsonl` (append-only) — one `FeedbackEntry` per row.
    Cumulative-counter deltas live in `delta`; distribution percentiles
    live in `snapshot` (they describe the population, not a counter, so
    no diff is meaningful). `last_consumed_at` carries the most recent
    consumption time within this delta window when ticlawk reports it.

  * `state/feedback_state.json` — collect_feedback's per-topic cursor
    into the consumer's channel-changes endpoint. Each topic has its own
    consumer/channel under per-topic routing, so cursors are tracked
    independently per topic. Server keeps the per-card snapshots on its
    side; we just persist the opaque cursor it returns.

Migration: old single-cursor schema (`{cursor: "..."}`) is auto-converted
on load — the old cursor seeds every topic's starting position so no
backlog gets re-emitted.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FeedbackState(BaseModel):
    """state/feedback_state.json — per-topic opaque consumer cursors.

    `cursors[topic] = "0"` means "first call ever" for that topic
    (ticlawk convention). `extra="ignore"` lets us silently drop any
    legacy fields (pre-channel-changes per-card snapshots, the old
    single-channel `cursor` string)."""
    model_config = ConfigDict(extra="ignore")
    cursors: dict[str, str] = Field(default_factory=dict)
    channel_ids: dict[str, str] = Field(default_factory=dict)
    last_polled_at: str = ""


class FeedbackEntry(BaseModel):
    """One row of `ledgers/feedback.jsonl`.

    `delta` carries cumulative-counter increments since the last cursor for
    this card. `snapshot` carries the distribution percentiles at observation
    time. `last_consumed_at` is the most recent consume event in this window
    if ticlawk reports one (None if the card got engagement deltas without a
    fresh consume — e.g. delayed like)."""
    model_config = ConfigDict(extra="forbid")
    card_id: str
    content_id: str                 # for join-back when learn runs
    source_id: str
    topic: str
    platform: str                   # "youtube" | "x" | "web" | "tiktok"
    observed_at: str                # ISO8601 UTC; when collect_feedback wrote this row
    last_consumed_at: str | None = None
    delta: dict[str, int]           # like_count / save_count / share_count / views
    # p50/p90_dwell_seconds (int seconds), p50/p90_watch_progress (float 0..1).
    # `float` since pydantic coerces ints up; declaring float keeps both endpoints valid.
    snapshot: dict[str, float]
