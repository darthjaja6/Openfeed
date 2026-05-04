"""Per-topic, per-platform search-term retirement based on accumulated catalog
evidence. Runs as a phase of `openfeed-learn` each tick.

**Rule** — a keyword is retired when:
  1. ≥ 1 of its introduced sources has been judgment-evaluated
     (i.e. NOT only platform hard-gate rejects)
  2. None of those judgment-evaluated sources are currently `active`

That is: as soon as we have any real verdict on this keyword's output AND
nothing it brought is still alive, the keyword goes.

**Why no `min_evidence ≥ 3` floor any more**:
discover commonly yields 1-2 sources per keyword (especially long-tail ones).
The old "≥ 3 sources required" rule made every long-tail keyword immortal.

**What counts as "judgment-evaluated"**:
Anything whose `decision_reason_code` is NOT in `KEYWORD_ACQUITTAL_REASONS`.
The acquittal list is just platform-scale / system-side decisions
(low_subscribers, empty feed, channel pivoted to shorts, etc.) — those are
not the keyword's fault. Everything else (LLM source-review rejects,
Bayesian posterior_below_threshold, filter_consistent_reject) IS the
keyword's fault.

Retired keywords get moved out of `keywords[]` into a sibling
`retired_keywords[]` block on `state/search_terms.json`.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openfeed.models.source import SourceCatalog
from openfeed.utils.state_io import atomic_write_json


logger = logging.getLogger("learn_search_terms")

_SEARCH_TERMS_PATH = Path("state/search_terms.json")

# Reject reason codes that ACQUIT the keyword — i.e. these rejects reflect
# platform-scale or system-side decisions, not "the keyword found the wrong
# kind of content". Anything outside this set is treated as a demerit.
KEYWORD_ACQUITTAL_REASONS: frozenset[str] = frozenset({
    "low_subscribers",          # YouTube channel below subscriber threshold
    "low_followers",            # X account below follower threshold
    "min_entries_not_met",      # web feed too short to evaluate
    "stale",                    # web feed last updated too long ago
    "no_entry_date",            # malformed feed entry
    "channel_resolve_failed",   # opencli couldn't resolve channel id
    "replaced_by_shorts_pivot", # creator-side pivot, not keyword's fault
})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def evaluate_search_terms(
    catalog: SourceCatalog,
) -> dict[tuple[str, str], list[str]]:
    """Group sources by their introducing keyword and compute retire decisions.

    Returns: dict keyed by `(topic, platform)` → list of keyword strings to
    retire. Caller persists to search_terms.json.

    Rule (see module docstring): retire a keyword when ≥ 1 of its sources
    has been judgment-evaluated AND none of those judgment-evaluated sources
    are currently active.
    """
    # (topic, platform, term) → list of SourceEntry
    bucket: dict[tuple[str, str, str], list] = defaultdict(list)
    for entry in catalog.sources.values():
        term = entry.attribution.introduced_by_seed_term
        if not term:
            continue
        bucket[(entry.topic, entry.platform, term)].append(entry)

    to_retire: dict[tuple[str, str], list[str]] = defaultdict(list)
    for (topic, platform, term), sources in bucket.items():
        # Keep only sources that carry a real verdict — strip those whose
        # status was decided by a platform-scale gate (the keyword wasn't on
        # trial there, the platform was).
        judged = [
            s for s in sources
            if s.status == "active"
            or s.decision_reason_code not in KEYWORD_ACQUITTAL_REASONS
        ]
        if not judged:
            continue  # no real evidence on this keyword yet
        n_active = sum(1 for s in judged if s.status == "active")
        if n_active > 0:
            continue  # at least one source from this keyword is still alive
        # All judgment-evaluated sources are non-active → keyword retired.
        to_retire[(topic, platform)].append(term)
        n_demerit = len(judged)
        logger.info(
            "→ search_term retire: [%s/%s] %r  introduced=%d judged=%d demerit=%d",
            topic, platform, term, len(sources), len(judged), n_demerit,
        )
    return dict(to_retire)


def apply_retirements(
    retire_map: dict[tuple[str, str], list[str]],
) -> int:
    """Mutate state/search_terms.json: move retired terms from `keywords` →
    `retired_keywords`. Returns count of newly-retired terms."""
    if not retire_map:
        return 0
    if not _SEARCH_TERMS_PATH.exists():
        logger.warning("search_terms.json missing, skip apply")
        return 0
    raw = json.loads(_SEARCH_TERMS_PATH.read_text(encoding="utf-8"))
    topics = raw.setdefault("topics", {})
    now_iso = _utc_now_iso()
    n_retired = 0
    for (topic, platform), terms_to_retire in retire_map.items():
        topic_node = topics.setdefault(topic, {})
        plat_node = topic_node.setdefault(platform, {})
        active = plat_node.get("keywords") or []
        retired = plat_node.setdefault("retired_keywords", [])
        for term in terms_to_retire:
            if term in active:
                active.remove(term)
                retired.append({
                    "term": term,
                    "retired_at": now_iso,
                    "reason": "all_introduced_sources_llm_rejected",
                })
                n_retired += 1
        plat_node["keywords"] = active
        plat_node["retired_keywords"] = retired
    raw["generated_at"] = now_iso
    atomic_write_json(_SEARCH_TERMS_PATH, raw)
    return n_retired
