"""collect_feedback — pull ticlawk channel-changes delta, write to ledger.

Per PRD §5.6, this is the consumer-side observation loop. Since ticlawk
shipped the `GET /api/channels/:id/changes?since=<cursor>` endpoint, the
client side simplified dramatically:

  - server keeps per-card snapshots and computes deltas
  - we pass an opaque cursor; ticlawk returns only cards that moved
  - one HTTP call per page (was N calls per tick under per-card polling)
  - no client-side snapshot file beyond the cursor itself

Each tick:
  1. Read `state/feedback_state.json` for the prior cursor (default "0").
  2. Call `get_channel_changes(since=cursor)` repeatedly until `has_more`
     is False, bounded by `tick_budget_seconds`.
  3. For each change record: look up the matching `HistoryEntry` (for
     source/topic/platform context) and append a `FeedbackEntry` row.
  4. Save the cursor returned by the most recent successful page.

Failure / partial-pagination: cursor only advances after a page's rows are
appended AND the new cursor is saved. If a write fails mid-page, next tick
re-pulls from the prior cursor (ticlawk is idempotent, same deltas come
back, slight chance of duplicate rows in feedback.jsonl — acceptable;
learn classifies deterministically so a dup just over-weights one event).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from openfeed.utils.config_files import load_env

from openfeed.clients.consumer import get_consumer, ticlawk
from openfeed.models.feedback import FeedbackEntry, FeedbackState
from openfeed.models.history import HistoryEntry
from openfeed.models.interests import load_interests
from openfeed.models.runtime import CollectFeedbackConfig, load_runtime
from openfeed.utils import backpressure, cycle_summary
from openfeed.utils.logging_setup import configure_task_logging
from openfeed.utils.state_io import atomic_write_json


logger = logging.getLogger("collect_feedback")

_DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 15 * 60

_HISTORY_PATH = Path("ledgers/history.jsonl")
_FEEDBACK_PATH = Path("ledgers/feedback.jsonl")
_STATE_PATH = Path("state/feedback_state.json")


_COUNTER_FIELDS = ("like_count", "save_count", "share_count", "views")
_PERCENTILE_FIELDS = (
    "p50_dwell_seconds", "p90_dwell_seconds",
    "p50_watch_progress", "p90_watch_progress",
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    configure_task_logging("collect_feedback")


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_history_map() -> dict[str, HistoryEntry]:
    """card_id → HistoryEntry. Drives source/topic/platform enrichment of
    raw ticlawk deltas. A change for a card_id we've never pushed is
    skipped (shouldn't happen; ticlawk only knows about cards we sent it)."""
    if not _HISTORY_PATH.exists():
        return {}
    out: dict[str, HistoryEntry] = {}
    for line in _HISTORY_PATH.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            h = HistoryEntry.model_validate_json(line)
        except Exception:  # noqa: BLE001
            continue
        out[h.card_id] = h
    return out


def _load_state(known_topics: list[str]) -> FeedbackState:
    """Load feedback state, migrating legacy single-cursor shape if found.

    Old shape: `{cursor: "<iso>", last_polled_at: "..."}` — the lone
    cursor seeds every known topic's starting position so we don't
    re-emit backlogged feedback at first per-topic poll.

    New shape: `{cursors: {topic: "<iso>"}, last_polled_at: "..."}`.

    Topics declared in openfeed.yaml that have no cursor yet (e.g.
    brand-new topic, or first run after migration) default to "0" via
    `_run_tick`'s lookup.
    """
    if not _STATE_PATH.exists():
        return FeedbackState()
    raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    legacy_cursor = raw.get("cursor")
    state = FeedbackState.model_validate(raw)
    if legacy_cursor and not state.cursors:
        # Migrate: seed every known topic with the legacy single cursor.
        state.cursors = {t: str(legacy_cursor) for t in known_topics}
        logger.info(
            "feedback_state migrated: legacy cursor=%s seeded into %d topic(s)",
            legacy_cursor, len(known_topics),
        )
    return state


def _save_state(state: FeedbackState) -> None:
    state.last_polled_at = _utc_now_iso()
    atomic_write_json(_STATE_PATH, state.model_dump())


def _append_feedback(rows: list[FeedbackEntry]) -> None:
    if not rows:
        return
    _FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _FEEDBACK_PATH.open("a", encoding="utf-8") as f:
        for entry in rows:
            f.write(entry.model_dump_json() + "\n")


# ---------------------------------------------------------------------------
# Change → FeedbackEntry projection
# ---------------------------------------------------------------------------


def _change_to_entry(
    change: dict, history: dict[str, HistoryEntry], observed_at: str,
) -> FeedbackEntry | None:
    """Project one ticlawk change record into our ledger row. Returns None
    if we can't resolve the card to a history entry (rare; means our local
    history.jsonl is missing a card ticlawk knows about — log and skip)."""
    card_id = change.get("card_id")
    if not card_id:
        return None
    hist = history.get(card_id)
    if hist is None:
        logger.warning("change for unknown card_id %s — skipping", card_id)
        return None
    deltas_raw = change.get("deltas") or {}
    dist_raw = change.get("current_distribution") or {}
    delta = {f: int(deltas_raw.get(f) or 0) for f in _COUNTER_FIELDS}
    snapshot = {f: float(dist_raw.get(f) or 0) for f in _PERCENTILE_FIELDS}
    last_consumed_at = change.get("last_consumed_at") or None
    return FeedbackEntry(
        card_id=card_id,
        content_id=hist.content_id,
        source_id=hist.source_id,
        topic=hist.topic,
        platform=hist.platform,
        observed_at=observed_at,
        last_consumed_at=last_consumed_at,
        delta=delta,
        snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# Pagination loop
# ---------------------------------------------------------------------------


def _run_tick(
    cfg: CollectFeedbackConfig,
    spec_by_topic: dict,
    consumer_config_by_topic: dict,
    consumer_type_by_topic: dict[str, str],
) -> tuple[int, int]:
    """One collect_feedback tick — sequential per-topic pagination loop.
    Each topic has its own consumer/channel and independent cursor; we
    walk them one at a time, sharing the global tick_budget_seconds.
    Returns (pages_pulled_total, rows_written_total)."""
    has_ticlawk_topic = any(v == "ticlawk" for v in consumer_type_by_topic.values())
    block = backpressure.active_block(backpressure.TICLAWK_API) if has_ticlawk_topic else None
    if block is not None:
        logger.warning(
            "ticlawk api backpressure active (%s): %s",
            block.get("reason"), block.get("detail", ""),
        )
        return 0, 0

    known_topics = sorted(spec_by_topic.keys())
    state = _load_state(known_topics)
    history = _load_history_map()
    deadline = time.monotonic() + cfg.tick_budget_seconds

    pages_total = 0
    rows_total = 0
    api_blocked = False

    for topic in known_topics:
        if api_blocked:
            break
        if time.monotonic() > deadline:
            logger.info("tick budget exhausted; %d topics deferred",
                        len(known_topics) - known_topics.index(topic))
            break
        spec = spec_by_topic[topic]
        cc = consumer_config_by_topic[topic]
        channel_id = getattr(cc, "channel_id", None)
        if channel_id:
            previous_channel_id = state.channel_ids.get(topic)
            if previous_channel_id and previous_channel_id != channel_id:
                logger.info(
                    "[%s] channel changed %s -> %s; reset feedback cursor",
                    topic, previous_channel_id, channel_id,
                )
                state.cursors[topic] = "0"
            state.channel_ids[topic] = channel_id
        cursor = state.cursors.get(topic) or "0"
        topic_pages = 0
        topic_rows = 0
        while True:
            if time.monotonic() > deadline:
                logger.info("[%s] tick budget exhausted at cursor=%s pages=%d",
                            topic, cursor, topic_pages)
                break
            try:
                page = spec.fetch_changes(cc, since=cursor)
            except Exception as exc:  # noqa: BLE001 — any consumer error
                if isinstance(exc, ticlawk.TiclawkRateLimited):
                    backpressure.block_lane(
                        backpressure.TICLAWK_API,
                        reason="rate_limited",
                        detail=f"fetch_changes[{topic}]: {exc}",
                        retry_after=exc.retry_after,
                        cooldown_seconds=None if exc.retry_after else _DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
                    )
                    api_blocked = True
                elif isinstance(exc, ticlawk.TiclawkAuthError):
                    backpressure.block_lane(
                        backpressure.TICLAWK_API,
                        reason="auth_failed",
                        detail=f"fetch_changes[{topic}]: {exc}",
                    )
                    api_blocked = True
                elif isinstance(exc, ticlawk.TiclawkError) and exc.status in (401, 403):
                    backpressure.block_lane(
                        backpressure.TICLAWK_API,
                        reason="auth_or_forbidden",
                        detail=f"fetch_changes[{topic}]: {exc}",
                    )
                    api_blocked = True
                logger.warning("[%s] fetch_changes failed at cursor=%s: %s",
                               topic, cursor, exc)
                break
            topic_pages += 1
            changes = page.get("changes") or []
            next_cursor = page.get("cursor") or cursor
            has_more = bool(page.get("has_more"))
            observed_at = _utc_now_iso()
            rows: list[FeedbackEntry] = []
            for ch in changes:
                entry = _change_to_entry(ch, history, observed_at)
                if entry is not None:
                    rows.append(entry)
            _append_feedback(rows)
            topic_rows += len(rows)
            # Advance cursor only after rows committed to ledger. If state
            # save fails we'll refetch this page next tick — server is
            # idempotent so we'd re-emit (acceptable dup).
            cursor = next_cursor
            state.cursors[topic] = cursor
            _save_state(state)
            logger.info(
                "[%s] page %d: %d changes → %d rows; cursor=%s has_more=%s",
                topic, topic_pages, len(changes), len(rows), cursor, has_more,
            )
            if not has_more:
                break
        pages_total += topic_pages
        rows_total += topic_rows
        if topic_pages or topic_rows:
            logger.info("[%s] tick done: pages=%d rows=%d", topic, topic_pages, topic_rows)
    return pages_total, rows_total


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openfeed-collect-feedback")
    ap.parse_args(argv)

    _configure_logging()
    workdir = Path.cwd()
    load_env(workdir)

    runtime = load_runtime(workdir)
    cfg = runtime.collect_feedback

    interests = load_interests(workdir)
    spec_by_topic = {}
    consumer_config_by_topic = {}
    consumer_type_by_topic = {}
    for t in interests.interests:
        spec = get_consumer(t.consumer_type)
        spec_by_topic[t.topic] = spec
        consumer_config_by_topic[t.topic] = spec.config_model.model_validate(t.consumer_config)
        consumer_type_by_topic[t.topic] = t.consumer_type

    pages, rows = _run_tick(cfg, spec_by_topic, consumer_config_by_topic, consumer_type_by_topic)
    logger.info("collect_feedback tick: pages=%d rows=%d", pages, rows)
    cycle_summary.add("collect_feedback", new_rows=rows, pages=pages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
