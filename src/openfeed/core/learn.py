"""learn — fold feedback observations into source-side Bayesian posteriors,
retire weak sources, expand the per-topic search-keyword pool from positive
feedback (PRD §5.7).

Each tick (in order):
  1. Read `state/learn_state.json` to know how far we've consumed feedback.jsonl.
  2. Parse new feedback rows past that offset.
  3. **Posterior phase**: each row → one signed score (active + passive
     signals are independent additive components). Positive net score adds
     Δα, negative adds Δβ. Negative scores are damped by the global mood
     multiplier. Per-source α/β are lazily decayed by
     `feedback_signal_decay_rate` to model preference drift.
  4. **Retire phase**: per topic, take the bottom-K sources by posterior
     mean; any whose mean < threshold AND evidence ≥ min flip directly
     `active` → `rejected` (no watchlist intermediate state).
  5. **Keyword-proposal phase**: per topic, gather positive content examples
     (dedupe by content_id, take strongest score per content) observed
     AFTER `keyword_proposal_last_at[topic]`. If pool size ≥
     `keyword_proposal_min_positive_examples`, fire one LLM call per topic
     (parallel) to propose new search keywords; new terms are appended to
     each platform's `keywords[]` in `state/search_terms.json`. Negative
     feedback is NOT fed into this phase — negatives drive Bayesian source
     retire and search-term retire instead.
  6. Atomic-write source_catalog.json + search_terms.json + learn_state.json.

Failure modes:
  - Uncaught error before writes → all files unchanged, next tick replays.
  - Per-topic LLM failure → that topic's keywords + last_at left untouched,
    other topics still commit. Next tick re-evaluates the same pool (since
    timestamp didn't advance).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openfeed.utils.config_files import load_env

from openfeed.clients.llm import GeminiRunner, LLMClientError
from openfeed.core import learn_search_terms
from openfeed.core.judgment_ledger import attach_file as _attach_ledger, emit_judgment
from openfeed.models.feedback import FeedbackEntry
from openfeed.models.history import HistoryEntry
from openfeed.models.interests import load_interests
from openfeed.models.learn_state import LearnState
from openfeed.models.runtime import LearnConfig, load_runtime
from openfeed.models.source import SourceCatalog, SourceEntry
from openfeed.models.user_profile import UserProfile, get_user_profile
from openfeed.core.deep_dive import deep_dive_one
from openfeed.prompts.keyword_proposal import (
    KeywordProposalUpdate, TopicExample, build_keyword_proposal_prompt,
)
from openfeed.utils import catalog_io, cycle_summary, queue_io
from openfeed.utils.logging_setup import configure_task_logging
from openfeed.utils.state_io import atomic_write_json


logger = logging.getLogger("learn")

_FEEDBACK_PATH = Path("ledgers/feedback.jsonl")
_HISTORY_PATH = Path("ledgers/history.jsonl")
_LEARN_STATE_PATH = Path("state/learn_state.json")
_CATALOG_PATH = Path("state/source_catalog.json")
_SEARCH_TERMS_PATH = Path("state/search_terms.json")
_DECISIONS_PATH = Path("ledgers/decisions.jsonl")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    configure_task_logging("learn")


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _load_state() -> LearnState:
    if not _LEARN_STATE_PATH.exists():
        return LearnState()
    return LearnState.model_validate(
        json.loads(_LEARN_STATE_PATH.read_text(encoding="utf-8"))
    )


def _save_state(state: LearnState) -> None:
    atomic_write_json(_LEARN_STATE_PATH, state.model_dump())


def _load_catalog() -> SourceCatalog:
    return catalog_io.load_catalog(Path("state"))


def _save_catalog(catalog: SourceCatalog, topics: set[str]) -> None:
    if not topics:
        return
    state_dir = Path("state")
    for topic in sorted(topics):
        scoped = {k: v for k, v in catalog.sources.items() if v.topic == topic}
        catalog_io.save_catalog_topic(state_dir, topic, scoped)


def _read_feedback_lines() -> list[str]:
    if not _FEEDBACK_PATH.exists():
        return []
    return [line for line in _FEEDBACK_PATH.read_text(encoding="utf-8").split("\n") if line.strip()]


# ---------------------------------------------------------------------------
# Scoring — additive model. Each feedback row produces a single signed score:
#   score > 0 → contributes Δα = score
#   score < 0 → contributes Δβ = abs(score)
# Active and passive components stack independently (e.g. like + long dwell
# is strictly stronger than just like). Active signals weighted heaviest
# because they're the scarcest evidence of intent.
# ---------------------------------------------------------------------------


def score(row: FeedbackEntry, cfg: LearnConfig) -> float:
    """Compute a single signed score for one feedback row.

    Positive net score is supportive of the source; negative net score is
    against. NEUTRAL outcomes return 0.0 — the row is "observed but no clear
    signal" and won't move posterior, but still counts toward evidence_count.
    """
    d = row.delta or {}
    s = row.snapshot or {}
    total = 0.0

    # ---- Active signals (each independent; user explicitly acted) ----
    if d.get("share_count", 0) > 0:
        total += cfg.score_share
    if d.get("save_count", 0) > 0:
        total += cfg.score_save
    if d.get("like_count", 0) > 0:
        total += cfg.score_like

    # ---- Passive signals (only if card was actually consumed) ----
    if int(d.get("views", 0)) <= 0:
        return total

    raw_dwell = float(s.get("p50_dwell_seconds", 0))
    # Outlier guard — abandoned tab. Drop passive, keep active.
    if raw_dwell > cfg.dwell_outlier_cap_seconds:
        return total

    watch = float(s.get("p50_watch_progress", 0))

    # Reflexive swipe: short absolute dwell — user didn't even register
    # the content. Trumps any other passive consideration.
    if raw_dwell < cfg.dwell_reflex_seconds:
        total -= cfg.score_strong_negative
        return total

    # Strong engagement: long dwell or high watch ratio. Either is enough.
    if (raw_dwell >= cfg.dwell_strong_positive_seconds
            or watch >= cfg.watch_strong_positive):
        total += cfg.score_strong_positive
        return total

    # Moderate engagement.
    if (raw_dwell >= cfg.dwell_positive_seconds
            or watch >= cfg.watch_positive):
        total += cfg.score_positive
        return total

    # Active dismiss: looked briefly, low watch ratio. Both must hold —
    # a short Shorts video watched fully shouldn't count as dismiss.
    if raw_dwell < cfg.dwell_dismiss_seconds and watch < cfg.watch_dismiss_max:
        total -= cfg.score_weak_negative
        return total

    # Middle ground — passive contributes nothing.
    return total


# ---------------------------------------------------------------------------
# Global mood damping — see PRD §5.7. Multiplier applied to ALL negative
# scores when the last `mood_window_hours` window shows high neg-ratio.
# Protects against bad-day cascades pummelling the catalog.
# ---------------------------------------------------------------------------


def compute_mood_dampener(
    all_rows: list[FeedbackEntry], cfg: LearnConfig,
) -> float:
    """Multiplier applied to negative scores when recent window has high
    negative ratio. 1.0 = no damping. Counts rows in the last
    `mood_window_hours` window by sign of `score()`."""
    cutoff = _utc_now() - timedelta(hours=cfg.mood_window_hours)
    pos = neg = 0
    for row in all_rows:
        try:
            ts = datetime.fromisoformat(row.observed_at)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        sc = score(row, cfg)
        if sc > 0:
            pos += 1
        elif sc < 0:
            neg += 1
    total = pos + neg
    if total == 0:
        return 1.0
    neg_ratio = neg / total
    if neg_ratio >= cfg.mood_heavy_damp_threshold:
        return cfg.mood_heavy_damp_multiplier
    if neg_ratio >= cfg.mood_damp_threshold:
        return cfg.mood_damp_multiplier
    return 1.0


# ---------------------------------------------------------------------------
# Posterior application
# ---------------------------------------------------------------------------


def _resolve_source(
    catalog: SourceCatalog, platform: str, source_id: str, topic: str,
) -> SourceEntry | None:
    """Catalog is keyed by `platform:source_id:topic` (per-topic source
    scoping). Return None if no record (e.g. source manually deleted, or
    feedback came from a topic that's since been removed)."""
    return catalog.sources.get(f"{platform}:{source_id}:{topic}")


def _decay_evidence(source: SourceEntry, cfg: LearnConfig, now: datetime) -> None:
    """Lazy preference-drift decay: shrink the evidence portion of α/β toward
    the prior (0.5 each) based on days elapsed since last_evidence_at.

    Models user preference change over time, NOT content freshness. The mean
    is mostly preserved (proportional shrinkage), but effective evidence count
    drops — making this source MORE responsive to subsequent new feedback.

    No-op if decay_rate >= 1.0 or last_evidence_at is missing/unparseable.
    """
    if cfg.feedback_signal_decay_rate >= 1.0:
        return
    if not source.last_evidence_at:
        return
    try:
        last = datetime.fromisoformat(source.last_evidence_at)
    except ValueError:
        return
    days = (now - last).total_seconds() / 86400.0
    if days <= 0:
        return
    factor = cfg.feedback_signal_decay_rate ** days
    # Decay only the "beyond-prior" portion; prior (0.5) remains.
    source.posterior.alpha = 0.5 + (source.posterior.alpha - 0.5) * factor
    source.posterior.beta = 0.5 + (source.posterior.beta - 0.5) * factor


def apply_evidence(
    rows: list[FeedbackEntry], catalog: SourceCatalog, cfg: LearnConfig,
    *, mood_dampener: float, observed_at: str,
) -> tuple[int, int, dict[str, int]]:
    """Mutates catalog.sources in place. Returns
    (applied, skipped, sign_hist) where sign_hist counts positive / negative /
    neutral score outcomes."""
    now = _utc_now()
    applied = 0
    skipped = 0
    hist: dict[str, int] = defaultdict(int)
    for row in rows:
        source = _resolve_source(catalog, row.platform, row.source_id, row.topic)
        if source is None:
            skipped += 1
            continue
        # rejected / blocked sources don't receive new evidence — they're out
        # of the active pool and shouldn't be resurrected or further penalised.
        if source.status != "active":
            skipped += 1
            continue

        sc = score(row, cfg)
        bucket = "positive" if sc > 0 else "negative" if sc < 0 else "neutral"
        hist[bucket] += 1

        # Lazy preference-drift decay before adding new evidence.
        _decay_evidence(source, cfg, now)

        if sc > 0:
            source.posterior.alpha += sc
        elif sc < 0:
            source.posterior.beta += abs(sc) * mood_dampener

        # evidence_count + last_evidence_at always advance per row, even when
        # score is 0 (we did observe a row from this source).
        source.evidence_count += 1
        source.last_evidence_at = observed_at
        applied += 1
    return applied, skipped, dict(hist)


# ---------------------------------------------------------------------------
# Retire state machine — per PRD §5.7 two-layer
# ---------------------------------------------------------------------------


def evaluate_retire(
    catalog: SourceCatalog, cfg: LearnConfig, *, decided_at: str,
) -> list[str]:
    """Per-topic, take the bottom-K sources (by posterior mean ascending);
    any of them satisfying mean < threshold AND evidence ≥ min are retired
    immediately to `rejected` status. No watchlist intermediate state.

    Returns the catalog_key list of sources retired this pass — caller uses
    it to prune queue items belonging to the retired sources.
    """
    by_topic: dict[str, list[SourceEntry]] = defaultdict(list)
    for s in catalog.sources.values():
        if s.status == "active":
            by_topic[s.topic].append(s)

    retired_keys: list[str] = []
    for topic, sources in by_topic.items():
        ranked = sorted(sources, key=lambda s: s.posterior.mean)
        candidates = ranked[:cfg.retire_bottom_k]
        for s in candidates:
            if s.posterior.mean >= cfg.retire_posterior_threshold:
                continue
            if s.evidence_count < cfg.retire_evidence_min:
                continue
            s.status = "rejected"
            s.decision_reason_code = "posterior_below_threshold"
            s.decided_at = decided_at
            retired_keys.append(s.catalog_key)
            emit_judgment(
                event_type="retire_source",
                platform=s.platform,
                topic=s.topic,
                source_id=s.source_id,
                source_name=s.name,
                reason_code="posterior_below_threshold",
                evidence={
                    "posterior_alpha": round(s.posterior.alpha, 3),
                    "posterior_beta": round(s.posterior.beta, 3),
                    "posterior_mean": round(s.posterior.mean, 4),
                    "evidence_count": s.evidence_count,
                },
            )
            logger.info("→ retire: %s [%s] mean=%.3f ev=%d",
                        s.catalog_key, topic, s.posterior.mean, s.evidence_count)
    return retired_keys


# ---------------------------------------------------------------------------
# Profile-update phase — refresh per-topic likes/dislikes via LLM
# ---------------------------------------------------------------------------




def _load_history_map() -> dict[str, HistoryEntry]:
    """card_id → HistoryEntry. Skips rows we can't parse."""
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


def _build_topic_positive_examples(
    rows: list[FeedbackEntry],
    history: dict[str, HistoryEntry],
    last_at: dict[str, str],
    catalog: SourceCatalog,
    cfg: LearnConfig,
) -> dict[str, list[TopicExample]]:
    """Group feedback by topic → list of positive content examples.

    Per-topic, dedupes by content_id and keeps the strongest positive score
    ever observed for that content after the topic's last keyword-proposal
    timestamp. Negative and neutral (score ≤ 0) rows are dropped — keyword
    proposal is positive-only by design.

    `catalog` is used to look up each source's `attribution.introduced_by_seed_term`
    so the Stage-2 prompt can show the LLM which keyword originally surfaced
    each positive (lineage trail).
    """
    # (topic, content_id) → (best_pos_score, latest_observed_at, hist)
    best: dict[tuple[str, str], tuple[float, str, HistoryEntry]] = {}
    for row in rows:
        cutoff = last_at.get(row.topic, "")
        if cutoff and row.observed_at <= cutoff:
            continue
        hist = history.get(row.card_id)
        if hist is None:
            continue
        sc = score(row, cfg)
        if sc <= 0:
            continue
        key = (row.topic, row.content_id)
        prev = best.get(key)
        if prev is None or sc > prev[0] or (
            sc == prev[0] and row.observed_at > prev[1]
        ):
            best[key] = (sc, row.observed_at, hist)

    by_topic: dict[str, list[tuple[str, TopicExample]]] = {}
    for (topic, cid), (_sc, observed_at, hist) in best.items():
        catalog_entry = catalog.sources.get(f"{hist.platform}:{hist.source_id}:{topic}")
        seed_term = (
            catalog_entry.attribution.introduced_by_seed_term
            if catalog_entry else None
        )
        ex = TopicExample(
            content_id=cid,
            title=hist.title,
            platform=hist.platform,
            discovered_by_keyword=seed_term,
        )
        by_topic.setdefault(topic, []).append((observed_at, ex))

    out: dict[str, list[TopicExample]] = {}
    for topic, items in by_topic.items():
        sorted_examples = [e for _, e in sorted(items, key=lambda x: x[0], reverse=True)]
        out[topic] = sorted_examples[:cfg.keyword_proposal_max_examples]
    return out


def _propose_keywords_one_topic(
    topic: str,
    topic_description: str,
    persona,
    pos: list[TopicExample],
    active_kws: list[str],
    retired_kws: list[str],
    max_new: int,
    runner: GeminiRunner,
) -> KeywordProposalUpdate | None:
    """Single-topic LLM call. Returns None on failure (caller logs)."""
    messages = build_keyword_proposal_prompt(
        topic=topic, topic_description=topic_description, persona=persona,
        positive_examples=pos,
        active_keywords=active_kws,
        retired_keywords=retired_kws,
        max_new_keywords=max_new,
    )
    try:
        raw = runner.run_json(
            messages,
            schema=KeywordProposalUpdate.model_json_schema(),
            schema_name="KeywordProposalUpdate",
        )
        return KeywordProposalUpdate.model_validate(raw)
    except (LLMClientError, Exception) as exc:  # noqa: BLE001
        logger.warning("keyword_proposal failed for [%s]: %s", topic, exc)
        return None


def _topic_keyword_pool(
    search_terms: dict, topic: str,
) -> tuple[list[str], list[str]]:
    """Pull (active, retired) keyword lists for a topic, unioned across platforms."""
    topic_node = (search_terms.get("topics") or {}).get(topic) or {}
    active: list[str] = []
    retired: list[str] = []
    for _platform, plat_node in topic_node.items():
        if not isinstance(plat_node, dict):
            continue
        for kw in plat_node.get("keywords") or []:
            if kw not in active:
                active.append(kw)
        for entry in plat_node.get("retired_keywords") or []:
            term = entry.get("term") if isinstance(entry, dict) else None
            if term and term not in retired:
                retired.append(term)
    return active, retired


def _append_new_keywords(
    search_terms: dict, topic: str, new_kws: list[str],
) -> int:
    """Append new keywords to every platform pool already configured for this
    topic. Skip terms already active or retired anywhere in the topic. Returns
    count of (platform, keyword) appends made."""
    if not new_kws:
        return 0
    topic_node = (search_terms.setdefault("topics", {})).setdefault(topic, {})
    if not topic_node:
        # Topic has no platform pool yet — nothing to extend. Caller logs.
        return 0
    active_union, retired_union = _topic_keyword_pool(search_terms, topic)
    fresh = [k for k in new_kws if k not in active_union and k not in retired_union]
    if not fresh:
        return 0
    appended = 0
    for _platform, plat_node in topic_node.items():
        if not isinstance(plat_node, dict):
            continue
        kws = plat_node.setdefault("keywords", [])
        for k in fresh:
            if k not in kws:
                kws.append(k)
                appended += 1
    return appended


def _run_deep_dive_stage(
    triggers: list[tuple[str, list[TopicExample], list[str], list[str]]],
    persona,
    topic_descriptions: dict[str, str],
    cfg: LearnConfig,
    runner: GeminiRunner,
) -> tuple[int, int]:
    """Run Stage-1 deep-dive in parallel across every positive in every
    triggered topic. Mutates each `TopicExample.perception` in place
    (None on failure). Returns (attempted, succeeded)."""
    jobs: list[tuple[str, TopicExample]] = [
        (topic, ex) for topic, pos, _a, _r in triggers for ex in pos
    ]
    if not jobs:
        return 0, 0
    logger.info("deep-dive: starting %d items across %d topic(s)",
                len(jobs), len(triggers))
    succeeded = 0
    pool = ThreadPoolExecutor(max_workers=cfg.deep_dive_workers)
    try:
        future_to_ex = {
            pool.submit(
                deep_dive_one,
                content_id=ex.content_id,
                platform=ex.platform,
                title=ex.title,
                topic=topic,
                topic_description=topic_descriptions.get(topic, ""),
                persona=persona,
                runner=runner,
                max_height=cfg.deep_dive_max_height,
                frame_count=cfg.deep_dive_frame_count,
            ): ex
            for topic, ex in jobs
        }
        for fut in as_completed(future_to_ex):
            ex = future_to_ex[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("deep-dive crashed for %s: %s", ex.content_id, exc)
                result = None
            ex.perception = result
            if result is not None:
                succeeded += 1
    finally:
        pool.shutdown(wait=True)
    logger.info("deep-dive done: %d/%d succeeded", succeeded, len(jobs))
    return len(jobs), succeeded


def expand_search_terms(
    profile: UserProfile,
    state: LearnState,
    search_terms: dict,
    examples_by_topic: dict[str, list[TopicExample]],
    topic_descriptions: dict[str, str],
    cfg: LearnConfig,
    runner: GeminiRunner,
    *, observed_at: str,
) -> tuple[int, int, int, int]:
    """Run LLM in parallel per-topic that meets the positive-examples threshold.
    Mutates `search_terms` + `state` in place. Returns
    (triggered, updated, failed, total_keywords_added)."""
    triggers: list[tuple[str, list[TopicExample], list[str], list[str]]] = []
    for topic, pos in examples_by_topic.items():
        if len(pos) < cfg.keyword_proposal_min_positive_examples:
            continue
        active_kws, retired_kws = _topic_keyword_pool(search_terms, topic)
        triggers.append((topic, pos, active_kws, retired_kws))
    if not triggers:
        return 0, 0, 0, 0
    logger.info("keyword-proposal triggers: %s",
                [f"{t}({len(p)} pos)" for t, p, _, _ in triggers])

    # Stage 1 — fetch frames + run deep-dive on every positive in every
    # triggered topic. Mutates each TopicExample.perception in place; Stage 2
    # picks it up via _format_examples.
    _run_deep_dive_stage(triggers, profile.persona, topic_descriptions, cfg, runner)

    updated = failed = total_added = 0
    pool = ThreadPoolExecutor(max_workers=cfg.keyword_proposal_workers)
    try:
        future_to_topic = {
            pool.submit(
                _propose_keywords_one_topic, topic,
                topic_descriptions.get(topic, ""), profile.persona,
                pos, active_kws, retired_kws,
                cfg.keyword_proposal_max_new_terms, runner,
            ): topic
            for topic, pos, active_kws, retired_kws in triggers
        }
        for fut in as_completed(future_to_topic):
            topic = future_to_topic[fut]
            result = fut.result()
            if result is None:
                failed += 1
                continue
            new_kws = result.new_keywords[:cfg.keyword_proposal_max_new_terms]
            n_added = _append_new_keywords(search_terms, topic, new_kws)
            state.keyword_proposal_last_at[topic] = observed_at
            updated += 1
            total_added += n_added
            logger.info(
                "keyword-proposal: [%s] proposed=%d, appended=%d",
                topic, len(new_kws), n_added,
            )
    finally:
        pool.shutdown(wait=True)
    return len(triggers), updated, failed, total_added


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openfeed-learn")
    ap.parse_args(argv or [])

    _configure_logging()
    workdir = Path.cwd()
    load_env(workdir)
    _attach_ledger(_DECISIONS_PATH)

    runtime = load_runtime(workdir)
    cfg = runtime.learn

    state = _load_state()
    catalog = _load_catalog()

    # Search-term retire — purely catalog-derived, runs every tick regardless
    # of feedback. Cheap (~ms) and keeps the discover keyword pool clean.
    st_retire_map = learn_search_terms.evaluate_search_terms(catalog)
    n_search_term_retired = 0
    if st_retire_map:
        n_search_term_retired = learn_search_terms.apply_retirements(st_retire_map)
        logger.info("search_term retire: %d terms moved to retired_keywords", n_search_term_retired)

    all_lines = _read_feedback_lines()
    new_lines = all_lines[state.feedback_offset:]
    if not new_lines:
        logger.info("learn: no new feedback rows since offset=%d", state.feedback_offset)
        cycle_summary.add(
            "learn",
            new_feedback_rows=0,
            search_terms_retired=n_search_term_retired,
        )
        return 0

    # Parse new rows
    new_rows: list[FeedbackEntry] = []
    for line in new_lines:
        try:
            new_rows.append(FeedbackEntry.model_validate_json(line))
        except Exception as exc:  # noqa: BLE001 — bad row shouldn't block the batch
            logger.warning("skip bad feedback row: %s", exc)
    logger.info("learn: %d new rows past offset=%d", len(new_rows), state.feedback_offset)
    touched_topics = {row.topic for row in new_rows}

    # For mood damping, also parse all rows (cheap; bounded by recent_window × cycles).
    # We compute mood ONCE per tick and apply it uniformly to weak-neg β in
    # this batch — close enough; longer windows could refresh per-row but that's
    # noise sensitivity not worth the code path.
    all_rows: list[FeedbackEntry] = []
    for line in all_lines:
        try:
            all_rows.append(FeedbackEntry.model_validate_json(line))
        except Exception:  # noqa: BLE001
            continue
    mood_mult = compute_mood_dampener(all_rows, cfg)
    logger.info("global mood dampener for negative scores: %.2f", mood_mult)

    # `catalog` was loaded at top of main() for the search-term retire pass.
    observed_at = _utc_now_iso()
    applied, skipped, hist = apply_evidence(
        new_rows, catalog, cfg,
        mood_dampener=mood_mult, observed_at=observed_at,
    )
    logger.info(
        "evidence applied: %d (skipped %d). signs=%s",
        applied, skipped, hist,
    )

    state.cycle_num += 1
    retired_keys = evaluate_retire(catalog, cfg, decided_at=observed_at)
    touched_topics.update(
        entry.topic
        for key in retired_keys
        if (entry := catalog.sources.get(key)) is not None
    )
    pruned = queue_io.prune_for_retired_sources(retired_keys) if retired_keys else 0
    logger.info(
        "retire pass (cycle %d): %d → rejected, %d queue items pruned",
        state.cycle_num, len(retired_keys), pruned,
    )

    # Keyword-proposal phase — positive-only examples scanned across ALL
    # feedback so far, per-topic gated by `keyword_proposal_last_at[topic]`.
    # Lazy-load GeminiRunner only if any topic triggers (keeps test runs
    # without OPENROUTER_API_KEY working when nothing fires).
    history = _load_history_map()
    examples_by_topic = _build_topic_positive_examples(
        all_rows, history, state.keyword_proposal_last_at, catalog, cfg,
    )
    runnable = any(
        len(p) >= cfg.keyword_proposal_min_positive_examples
        for p in examples_by_topic.values()
    )
    if runnable:
        profile = get_user_profile(workdir)
        search_terms = json.loads(_SEARCH_TERMS_PATH.read_text(encoding="utf-8"))
        topic_descriptions = {
            i.topic: i.description for i in load_interests(workdir).interests
        }
        runner = GeminiRunner(workdir)
        triggered, updated, failed, kws_added = expand_search_terms(
            profile, state, search_terms, examples_by_topic,
            topic_descriptions, cfg, runner,
            observed_at=observed_at,
        )
        logger.info(
            "keyword-proposal: triggered=%d updated=%d failed=%d kws_added=%d",
            triggered, updated, failed, kws_added,
        )
        if kws_added > 0:
            search_terms["generated_at"] = observed_at
            atomic_write_json(_SEARCH_TERMS_PATH, search_terms)
    else:
        triggered = updated = failed = kws_added = 0
        logger.info(
            "keyword-proposal: no topic met min_positive_examples=%d; skipping",
            cfg.keyword_proposal_min_positive_examples,
        )

    # Commit catalog + cursor. Order: catalog first; if it fails, cursor
    # stays put and next tick replays the same feedback rows.
    _save_catalog(catalog, touched_topics)
    state.feedback_offset = len(all_lines)
    state.last_run_at = observed_at
    _save_state(state)

    logger.info(
        "learn tick done: offset %d → %d, cycle_num=%d",
        state.feedback_offset - len(new_lines), state.feedback_offset, state.cycle_num,
    )
    cycle_summary.add(
        "learn",
        new_feedback_rows=len(new_rows),
        evidence_applied=applied,
        sources_retired=len(retired_keys),
        queue_items_pruned=pruned,
        search_terms_retired=n_search_term_retired,
        keyword_proposals_triggered=triggered,
        keyword_proposals_succeeded=updated,
        new_keywords_added=kws_added,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
