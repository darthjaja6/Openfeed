"""Append-only judgment ledger (source admit / reject / retire events).

Business code calls `emit_judgment(...)` without caring which file it goes to.
The top-level entrypoint (bootstrap, discover) calls `attach_file(path)` once
to wire a named FileHandler onto the module logger — so bootstrap writes to
`ledgers/source_judgments.jsonl` (frozen, legacy) and discover writes to the
PRD-standard `ledgers/decisions.jsonl`.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openfeed.models.judgment import JudgmentEvent


_logger = logging.getLogger("judgment_ledger")
_logger.setLevel(logging.INFO)
_logger.propagate = False


def attach_file(path: Path) -> None:
    """Attach a FileHandler writing JSONL to `path`. Idempotent for the same path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    for h in _logger.handlers:
        if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == resolved:
            return
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)


def emit_judgment(
    *,
    event_type: str,
    platform: str,
    topic: str,
    source_id: str,
    source_name: str,
    reason_code: str,
    reasoning: str | None = None,
    matched_keywords: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    event = JudgmentEvent(
        ts=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        event_type=event_type,  # type: ignore[arg-type]
        platform=platform,  # type: ignore[arg-type]
        topic=topic,
        source_id=source_id,
        source_name=source_name,
        reason_code=reason_code,
        reasoning=reasoning,
        matched_keywords=list(matched_keywords or []),
        evidence=evidence or {},
    )
    _logger.info(json.dumps(event.model_dump(), ensure_ascii=False))
