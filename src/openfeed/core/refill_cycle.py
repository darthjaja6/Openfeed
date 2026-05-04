"""Refill-cycle orchestrator — consumer-side tick loop (PRD §3.5).

Mirrors `supply_cycle.py`: a thin `push → collect_feedback → learn` tick,
sleep one interval, repeat. Each sub-task owns its own state — the loop only
schedules.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from datetime import timezone

from openfeed.core import collect_feedback as collect_feedback_mod
from openfeed.core import learn as learn_mod
from openfeed.core import push as push_mod
from openfeed.models.runtime import load_runtime
from openfeed.utils import cycle_summary
from openfeed.utils.logging_setup import configure_task_logging


logger = logging.getLogger("refill_cycle")


# Name → callable; ordering matters. Each callable returns an int rc.
_TASKS: list[tuple[str, Callable[[list[str] | None], int]]] = [
    ("push", push_mod.main),
    ("collect_feedback", collect_feedback_mod.main),
    ("learn", learn_mod.main),
]


def _configure_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler)
               and getattr(h, "stream", None) is sys.stdout
               for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(sh)
    run_log = configure_task_logging("refill_cycle")
    logger.info("run log → %s", run_log)


def _system_exit_rc(exc: SystemExit, *, task: str) -> int:
    code = getattr(exc, "code", 0)
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    logger.error("%s exited: %s", task, code)
    return 1


def _run_once() -> int:
    """One full refill_cycle pass. Individual task failures log but don't
    abort the cycle — push failing shouldn't stop a future collect_feedback
    from advancing, unlike supply_cycle where the chain is strictly ordered."""
    worst_rc = 0
    for name, fn in _TASKS:
        if name == "push" and os.environ.get("OPENFEED_DISABLE_PUSH") == "1":
            logger.warning("push skipped: OPENFEED_DISABLE_PUSH=1")
            continue
        if name == "collect_feedback" and os.environ.get("OPENFEED_DISABLE_FEEDBACK") == "1":
            logger.warning("collect_feedback skipped: OPENFEED_DISABLE_FEEDBACK=1")
            continue
        if name == "learn" and os.environ.get("OPENFEED_DISABLE_LEARN") == "1":
            logger.warning("learn skipped: OPENFEED_DISABLE_LEARN=1")
            continue
        logger.info("─── %s ───", name)
        try:
            rc = fn([])
        except SystemExit as exc:
            rc = _system_exit_rc(exc, task=name)
        except Exception:  # noqa: BLE001
            logger.exception("%s crashed", name)
            rc = 1
        if rc != 0:
            logger.warning("%s returned rc=%d", name, rc)
            worst_rc = max(worst_rc, rc)
    return worst_rc


_stop_requested = False


def _install_stop_handler() -> None:
    def _handler(signum, _frame):  # type: ignore[no-untyped-def]
        global _stop_requested
        _stop_requested = True
        logger.info("signal %d received — finishing current tick then exiting", signum)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openfeed-refill-cycle")
    ap.add_argument(
        "--loop", action="store_true",
        help="run repeatedly, sleeping between ticks (Ctrl-C to stop cleanly)",
    )
    ap.add_argument(
        "--interval", type=int, default=None,
        help="override runtime.refill_cycle.interval_seconds for --loop",
    )
    args = ap.parse_args(argv)

    _configure_logging()
    runtime = load_runtime(Path.cwd())
    interval = args.interval if args.interval is not None else runtime.refill_cycle.interval_seconds

    if not args.loop:
        return _run_once()

    _install_stop_handler()
    tick_num = 0
    while not _stop_requested:
        tick_num += 1
        started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        logger.info("════ tick %d ════", tick_num)
        rc = _run_once()
        cycle_summary.flush(cycle="refill", tick_num=tick_num, started_at=started_at, rc=rc)
        logger.info("════ tick %d done (rc=%d) ════", tick_num, rc)
        if _stop_requested:
            break
        logger.info("sleeping %ds", interval)
        slept = 0
        while slept < interval and not _stop_requested:
            time.sleep(min(2, interval - slept))
            slept += 2
    logger.info("refill_cycle stopped after %d ticks", tick_num)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
