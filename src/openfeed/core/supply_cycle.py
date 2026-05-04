"""Supply-cycle orchestrator (PRD §3.5).

Thin loop: topic_reconcile → bootstrap_missing → patrol → filter → queue_manage
→ sleep → repeat.

PRD §3.5 makes the loop itself minimal ("只负责顺序与节拍") — no business
logic lives here. Each sub-task owns its own state-file semantics, so this
orchestrator just calls their `main()` in sequence and honours the outer
sleep cadence. If a step returns non-zero, we stop the current cycle (per
§3.7 "依赖挂了不试图往下走"); next cycle's tick will retry.

The `bootstrap_missing` step picks ONE topic per tick that's in
`openfeed.yaml` but has no `state/source_catalog/<topic>.json`,
runs scoped LLM keyword seed for it, then runs `discover --topic <name>`
to populate its catalog. After all topics have catalogs the step is a
no-op. New topics are picked alphabetically — one per tick so the daemon
keeps serving existing topics on its normal cadence even while a fresh
topic is being onboarded (full discover takes 15-35 min).
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from openfeed.clients.llm import GeminiRunner
from openfeed.core import cleanup_assets as cleanup_assets_mod
from openfeed.core import discover as discover_mod
from openfeed.core import filter as filter_mod
from openfeed.core import patrol as patrol_mod
from openfeed.core import queue_manage as queue_manage_mod
from openfeed.core import topic_reconcile as topic_reconcile_mod
from openfeed.core.bootstrap_io import merge_search_terms
from openfeed.core.interest_bootstrap import generate_keywords_per_platform
from openfeed.models.interests import InterestsConfig, load_interests
from openfeed.models.persona import PersonaOutput
from openfeed.utils import cycle_summary
from openfeed.utils.logging_setup import configure_task_logging
from openfeed.utils.state_io import atomic_write_json


logger = logging.getLogger("supply_cycle")


def _bootstrap_missing(_argv: list[str] | None = None) -> int:
    """If any yaml topic has no per-topic catalog file, scope-bootstrap one
    of them this tick (alphabetical order). Step:
      1. LLM-generate search_terms for the missing topic's platforms
      2. Merge into state/search_terms.json (other topics untouched)
      3. Run `discover --topic <name>` synchronously to populate its catalog

    Returns 0 always (no work needed = success). When work is needed and
    discover fails, logs and returns 0 anyway — we don't want a transient
    discover failure to abort the rest of the supply tick.
    """
    workdir = Path.cwd()
    config = load_interests(workdir)
    catalog_dir = workdir / "state" / "source_catalog"
    have_catalog = (
        {fp.stem for fp in catalog_dir.glob("*.json")} if catalog_dir.exists() else set()
    )
    missing = sorted(
        t.topic for t in config.interests if t.topic not in have_catalog
    )
    if not missing:
        return 0
    target_name = missing[0]
    target_entry = next(t for t in config.interests if t.topic == target_name)
    logger.info(
        "bootstrap_missing: %d topic(s) without catalog; doing %r this tick (rest: %s)",
        len(missing), target_name, missing[1:] or "—",
    )

    # ----- step 1: scoped LLM keyword seed (mirrors tmp/scoped_seed_*.py) -----
    runner = GeminiRunner(workdir)
    persona = PersonaOutput.model_validate(config.persona)
    sliced = InterestsConfig(persona=config.persona, interests=[target_entry])
    new_keywords = generate_keywords_per_platform(
        sliced, persona, [], runner,
        only_slots={(target_name, p) for p in target_entry.platforms},
    )
    keywords_path = workdir / "state" / "search_terms.json"
    existing = json.loads(keywords_path.read_text(encoding="utf-8")) if keywords_path.exists() else {}
    merged = merge_search_terms(config, existing, new_keywords)
    atomic_write_json(keywords_path, merged)
    n_kw = sum(
        len((merged.get("topics") or {}).get(target_name, {}).get(p, {}).get("keywords") or [])
        for p in target_entry.platforms
    )
    logger.info("bootstrap_missing: seeded %d keywords for %r", n_kw, target_name)

    # ----- step 2: full discover for that topic (slow — 15-35 min) -----
    logger.info("bootstrap_missing: invoking discover --topic %r (slow path)", target_name)
    try:
        rc = discover_mod.main(["--topic", target_name])
    except SystemExit as exc:
        rc = _system_exit_rc(exc, task=f"discover --topic {target_name!r}")
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap_missing: discover --topic %r crashed", target_name)
        return 0  # don't abort the rest of the supply tick
    logger.info("bootstrap_missing: discover --topic %r → rc=%d", target_name, rc)
    return 0


_TASKS: list[tuple[str, Callable[[list[str] | None], int]]] = [
    ("topic_reconcile", topic_reconcile_mod.main),
    ("bootstrap_missing", _bootstrap_missing),
    ("patrol", patrol_mod.main),
    ("filter", filter_mod.main),
    ("queue_manage", queue_manage_mod.main),
    ("cleanup_assets", cleanup_assets_mod.main),
]


def _configure_logging() -> None:
    # StreamHandler so daemon stdout still gets the live log; the rotating
    # file handler is attached by `configure_task_logging`.
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
    run_log = configure_task_logging("supply_cycle")
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
    """One full supply_cycle pass. Returns 0 on full success; non-zero if
    any task fails (current cycle aborts, next tick retries)."""
    for name, fn in _TASKS:
        logger.info("─── %s ───", name)
        try:
            rc = fn([])
        except SystemExit as exc:
            rc = _system_exit_rc(exc, task=name)
        except Exception:  # noqa: BLE001 — unexpected task crash; surface & stop cycle
            logger.exception("%s crashed; aborting this cycle", name)
            return 1
        if rc != 0:
            logger.warning("%s returned rc=%d; aborting this cycle", name, rc)
            return rc
    return 0


_stop_requested = False


def _install_stop_handler() -> None:
    def _handler(signum, _frame):  # type: ignore[no-untyped-def]
        global _stop_requested
        _stop_requested = True
        logger.info("signal %d received — finishing current cycle then exiting", signum)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openfeed-supply-cycle")
    ap.add_argument(
        "--loop", action="store_true",
        help="run repeatedly, sleeping between cycles (Ctrl-C to stop cleanly)",
    )
    ap.add_argument(
        "--interval", type=int, default=900,
        help="seconds to sleep between cycles in --loop mode (default 900 = 15min)",
    )
    args = ap.parse_args(argv)

    _configure_logging()

    if not args.loop:
        return _run_once()

    _install_stop_handler()
    cycle_num = 0
    while not _stop_requested:
        cycle_num += 1
        started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        logger.info("════ cycle %d starting ════", cycle_num)
        rc = _run_once()
        cycle_summary.flush(cycle="supply", tick_num=cycle_num, started_at=started_at, rc=rc)
        logger.info("════ cycle %d done (rc=%d) ════", cycle_num, rc)
        if _stop_requested:
            break
        logger.info("sleeping %ds before next cycle", args.interval)
        # Responsive sleep — break early if SIGINT arrives.
        slept = 0
        while slept < args.interval and not _stop_requested:
            time.sleep(min(5, args.interval - slept))
            slept += 5
    logger.info("supply_cycle stopped after %d cycles", cycle_num)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
