"""Intermediate result type shared by bootstrap + discover.

A ValidatedSource is a source that completed the review pipeline — either
admitted (`status="active"`) or rejected by the LLM (`status="rejected"`).
Both get persisted to source_catalog.json so subsequent discover runs can
dedup against either verdict without paying the LLM-review cost again.

Hard-gate rejections (low subscribers, low followers, stale feed, etc.) do
NOT flow through this type — they stay ledger-only and remain re-eligible on
later runs, since the underlying condition may change.

Callers set `reason_code` explicitly — its value signals the provenance path
(bootstrap seed vs discover keyword search vs specific LLM-reject reason),
which is audited downstream.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from openfeed.models.seed_source import SeedSource


@dataclass
class ValidatedSource:
    seed: SeedSource | None  # None when the source came from keyword search rather than an LLM-proposed seed
    topic: str
    platform: str
    canonical_id: str
    canonical_name: str
    url: str
    reason_code: str
    status: Literal["active", "rejected"] = "active"
    llm_reasoning: str | None = None
    matched_keywords: list[str] = field(default_factory=list)
    sample_titles: list[str] = field(default_factory=list)
    sample_snippets: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
