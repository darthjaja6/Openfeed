"""Centralized logging setup for openfeed task entrypoints.

Replaces the per-task `_configure_logging()` helpers that each created a
new `logs/<task>_<timestamp>.log` per invocation. That pattern produced
thousands of files (e.g. learn ticking every 30s → 4000+ files in 14
days, ~8GB), with no rotation or cleanup.

This module exposes one entry point — `configure_task_logging(task_name)` —
that attaches:

  - A `RotatingFileHandler` to the root logger writing
    `logs/<task_name>.log` (10 MB × 5 backups → 60 MB cap per task)
  - A `RotatingFileHandler` on the `llm_trace` named logger writing
    `logs/llm_trace_<task_name>.jsonl` (50 MB × 3 backups → 200 MB cap),
    with `propagate=False` so trace JSONL doesn't pollute the task log

Idempotent: re-calling with the same `task_name` doesn't double-attach.
Tasks with no LLM activity still get the trace handler attached at zero
cost (the file just stays empty).

Total disk ceiling across all 12 tasks: ~3.1 GB, regardless of run time.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_TASK_LOG_MAX_BYTES = 10 * 1024 * 1024     # 10 MB
_TASK_LOG_BACKUPS = 5                       # → 60 MB cap per task

_TRACE_LOG_MAX_BYTES = 50 * 1024 * 1024     # 50 MB (LLM responses can be hefty)
_TRACE_LOG_BACKUPS = 3                      # → 200 MB cap per task

_TASK_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_DATEFMT = "%H:%M:%S"


def _attach_rotating(
    logger: logging.Logger, path: Path, max_bytes: int, backup_count: int,
    formatter: logging.Formatter,
) -> None:
    """Attach a RotatingFileHandler to `logger` writing to `path`. Skip if
    a handler for the same baseFilename is already on this logger."""
    resolved = str(path.resolve())
    for h in logger.handlers:
        if (isinstance(h, RotatingFileHandler)
                and Path(h.baseFilename).resolve() == path.resolve()):
            return
    handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def configure_task_logging(
    task_name: str, *, log_dir: Path = Path("logs"),
) -> Path:
    """Attach rotating handlers for `task_name` to root + the `llm_trace`
    logger. Returns the resolved task-log path so callers can reference it
    (e.g. for printing on startup).

    Caller usually does `configure_task_logging("learn")` from main()."""
    log_dir.mkdir(parents=True, exist_ok=True)

    # Task .log (root logger, formatted for human reading)
    task_path = log_dir / f"{task_name}.log"
    _attach_rotating(
        logging.getLogger(), task_path,
        _TASK_LOG_MAX_BYTES, _TASK_LOG_BACKUPS,
        logging.Formatter(_TASK_FORMAT, datefmt=_DATEFMT),
    )
    if logging.getLogger().level > logging.INFO:
        logging.getLogger().setLevel(logging.INFO)

    # LLM trace (named logger, raw JSONL — formatter drops timestamp prefix)
    trace_path = log_dir / f"llm_trace_{task_name}.jsonl"
    trace_logger = logging.getLogger("llm_trace")
    _attach_rotating(
        trace_logger, trace_path,
        _TRACE_LOG_MAX_BYTES, _TRACE_LOG_BACKUPS,
        logging.Formatter("%(message)s"),
    )
    trace_logger.propagate = False
    if trace_logger.level > logging.INFO:
        trace_logger.setLevel(logging.INFO)

    return task_path.resolve()
