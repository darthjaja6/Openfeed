"""Shared content-metadata helpers: age parsing, blocklist loading,
per-topic temporal resolution.

These were historically private to `core/filter.py` but are needed by
multiple core modules (filter, patrol, queue_manage). Splitting them out
removes cross-module `_private` imports and keeps `core/` modules
independent of each other.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import NamedTuple

from openfeed.models.content_item import ContentItem
from openfeed.models.interests import InterestEntry
from openfeed.models.queue import Queue
from openfeed.models.runtime import FilterConfig


_QUEUE_JSON = Path("state/queue.json")
_LEDGER_PATH = Path("ledgers/decisions.jsonl")


# ---------------------------------------------------------------------------
# Age parsing — platform time formats → days
# ---------------------------------------------------------------------------


# opencli emits compact forms ("2y ago", "5mo ago", "3w ago", "12d ago").
# Longer tokens first so `mo` beats `m`, `hr` beats `h`, etc.
_YT_AGO_PAT = re.compile(
    r"(\d+)\s*(year|yr|y|month|mon|mo|week|wk|w|day|d|hour|hr|h|minute|min|m|second|sec|s)s?\s+ago",
    re.IGNORECASE,
)
_YT_AGO_MULT_DAYS = {
    "year": 365.0, "yr": 365.0, "y": 365.0,
    "month": 30.0, "mon": 30.0, "mo": 30.0,
    "week": 7.0, "wk": 7.0, "w": 7.0,
    "day": 1.0, "d": 1.0,
    "hour": 1 / 24, "hr": 1 / 24, "h": 1 / 24,
    "minute": 1 / 1440, "min": 1 / 1440, "m": 1 / 1440,
    "second": 1 / 86400, "sec": 1 / 86400, "s": 1 / 86400,
}


def yt_ago_to_days(s: str) -> float | None:
    """Parse YouTube's "X units ago" → days. None on failure."""
    m = _YT_AGO_PAT.search((s or "").lower())
    if not m:
        return None
    n = int(m.group(1))
    mult = _YT_AGO_MULT_DAYS.get(m.group(2))
    if mult is None:
        return None
    return n * mult


def content_age_days(item: ContentItem) -> float | None:
    """Age in days regardless of platform time format. None if unparseable."""
    now = datetime.now(timezone.utc)
    if item.platform == "youtube" and item.youtube is not None:
        return yt_ago_to_days(item.youtube.published)
    if item.platform == "x" and item.x is not None:
        if not item.x.created_at:
            return None
        try:
            dt = parsedate_to_datetime(item.x.created_at)
            if dt is None:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (now - dt).total_seconds() / 86400.0
        except Exception:  # noqa: BLE001
            return None
    if item.platform == "web" and item.web is not None:
        if not item.web.published_at:
            return None
        try:
            dt = datetime.fromisoformat(item.web.published_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (now - dt).total_seconds() / 86400.0
        except ValueError:
            return None
    if item.platform == "tiktok" and item.tiktok is not None:
        if item.tiktok.timestamp is not None:
            try:
                dt = datetime.fromtimestamp(item.tiktok.timestamp, tz=timezone.utc)
                return (now - dt).total_seconds() / 86400.0
            except Exception:  # noqa: BLE001
                return None
        if item.tiktok.upload_date:
            try:
                dt = datetime.strptime(item.tiktok.upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
                return (now - dt).total_seconds() / 86400.0
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Content dedup blocklist — state/queue.json + ledgers/decisions.jsonl
# ---------------------------------------------------------------------------


def load_content_blocklist(queue: Queue | None = None) -> set[str]:
    """content_ids already decided — never re-admit / re-review.

    Source:
      - `state/queue.json` admitted (pending push): can't double-admit
      - `ledgers/decisions.jsonl` past admit_content / reject_content events

    Used by filter (dedup against pre-decided content) and by patrol (skip
    writing items already processed). Pass `queue` in if you have it in
    hand to skip the disk read.
    """
    blocked: set[str] = set()
    if queue is None and _QUEUE_JSON.exists():
        try:
            queue = Queue.model_validate(json.loads(_QUEUE_JSON.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError):
            queue = None
    if queue is not None:
        for topic_items in queue.topics.values():
            for qi in topic_items:
                blocked.add(qi.content.content_id)
    if _LEDGER_PATH.exists():
        for line in _LEDGER_PATH.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event_type") in ("admit_content", "reject_content"):
                cid = ev.get("source_id")  # content events reuse the source_id slot for content_id
                if isinstance(cid, str) and cid:
                    blocked.add(cid)
    return blocked


# ---------------------------------------------------------------------------
# Per-topic temporal resolution — max_age + half_life
# ---------------------------------------------------------------------------


class TopicTemporal(NamedTuple):
    """Resolved per-(topic, platform) temporal values.

    - `max_age_days`: hard-gate cutoff in days. None = no age gate for this
      (topic, platform) — e.g. topic has no max_content_age_days and platform
      has no runtime fallback (x / web).
    - `half_life_days`: freshness exponential-decay half-life in days.
    """
    max_age_days: int | None
    half_life_days: float


def resolve_topic_temporal(
    topic: str, entry: InterestEntry | None,
    filter_cfg: FilterConfig, platform: str,
) -> TopicTemporal:
    """Resolve temporal values with fallback chain.

    half_life: InterestEntry.freshness_half_life_days
               → runtime.filter.freshness_half_life_days_per_topic[topic]
               → runtime.filter.freshness_half_life_days_default
    max_age:   InterestEntry.max_content_age_days
               → runtime.filter.youtube.max_age_days (YouTube only)
               → runtime.filter.tiktok.max_age_days (TikTok only)
               → None (no age gate for x / web without topic override)
    """
    if entry is not None and entry.freshness_half_life_days is not None:
        half_life = float(entry.freshness_half_life_days)
    else:
        half_life = float(
            filter_cfg.freshness_half_life_days_per_topic.get(
                topic, filter_cfg.freshness_half_life_days_default,
            )
        )

    if entry is not None and entry.max_content_age_days is not None:
        max_age: int | None = entry.max_content_age_days
    elif platform == "youtube":
        max_age = filter_cfg.youtube.max_age_days
    elif platform == "tiktok":
        max_age = filter_cfg.tiktok.max_age_days
    else:
        max_age = None

    return TopicTemporal(max_age_days=max_age, half_life_days=half_life)
