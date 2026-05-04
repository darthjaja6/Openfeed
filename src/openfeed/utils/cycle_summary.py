"""Per-cycle aggregated summary event.

Each supply_cycle / refill_cycle tick produces one JSON line in
`ledgers/cycle_summary.jsonl` recording what each phase did. Phases
contribute counts via `add()` during execution; the cycle wrapper calls
`flush()` at end-of-tick to write the merged record.

Per PRD §6.3:
  > 每个 cycle 跑完写一条 cycle_summary.jsonl 事件，记这轮发生了什么：
  > - 供给侧: admit 了几个 source / patrol 覆盖几个 / filter 过了几条 / 入队几条
  > - 消费侧: 推了几张卡 / 收到多少反馈 / learn 更新了哪几类状态
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger("cycle_summary")

LEDGER_PATH = Path("ledgers/cycle_summary.jsonl")

# In-process per-cycle state. Phases inside one Python process accumulate into
# this dict; flush() writes it out + resets. Using thread-local would let
# multiple concurrent cycles coexist, but supply / refill are single-threaded
# at the tick level so a module-global is enough here.
_lock = threading.Lock()
_buffer: dict[str, Any] = {}


def add(phase: str, **metrics: Any) -> None:
    """Record metrics for a phase under `phase` key. Repeat calls for the same
    phase merge (later overrides on collision)."""
    if not metrics:
        return
    with _lock:
        existing = _buffer.setdefault(phase, {})
        existing.update(metrics)


def flush(*, cycle: str, tick_num: int, started_at: str, rc: int) -> None:
    """Write one aggregated line to cycle_summary.jsonl, then reset state.

    `cycle` is "supply" or "refill"; `rc` is the wrapper's return code so
    failed-cycle records still go out. Always emits a record even when no
    phases reported metrics — gives operators a "tick happened" pulse."""
    with _lock:
        phases = dict(_buffer)
        _buffer.clear()
    record = {
        "cycle": cycle,
        "tick_num": tick_num,
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "rc": rc,
        "phases": phases,
    }
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("cycle_summary write failed: %s", exc)
