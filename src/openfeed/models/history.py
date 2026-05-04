"""`ledgers/history.jsonl` schema — one row per push (PRD §3.2).

Written by push when a card is successfully delivered to Ticlawk. Read by:
  - push itself (to enforce same-topic / same-source spacing against recent
    history)
  - future collect_feedback (picks which card_ids to poll metrics for)
  - future learn (grounds feedback increments back to topic / source)

Deliberately lean: we do NOT carry the rendered HTML or payload here — that
bloats the ledger with kilobytes per card and can always be re-rendered
from the content_id if needed.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str                # uuid returned by POST /api/cards
    content_id: str             # platform-native id (yt video id, x post id, web URL)
    source_id: str              # the source this content came from (channel handle / feed URL)
    topic: str
    platform: str               # "youtube" | "x" | "web" | "tiktok"
    content_subtype: str        # "video" | "gallery" | "html"
    title: str
    pushed_at: str              # ISO8601 UTC
    rank_score: float           # composite at admit time (for debugging why selected)
