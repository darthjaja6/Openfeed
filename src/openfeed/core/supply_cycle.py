"""Supply-cycle orchestrator (PRD §3.5).

Thin loop: topic_reconcile → bootstrap_missing → patrol → filter → queue_manage
→ sleep → repeat.

PRD §3.5 makes the loop itself minimal ("只负责顺序与节拍") — no business
logic lives here. Each sub-task owns its own state-file semantics, so this
orchestrator just calls their `main()` in sequence and honours the outer
sleep cadence. If a step returns non-zero, we stop the current cycle (per
§3.7 "依赖挂了不试图往下走"); next cycle's tick will retry.

The `bootstrap_missing` step picks ONE source-missing (topic, platform) slot
per tick, seeds keywords if that slot has none, then runs scoped discover.
This lets a new platform be added to an existing topic without rebuilding the
rest of the topic. `starvation_discover` is a bounded escalation for topics
whose active sources are under floor but temporarily exhausted; it runs at
most one scoped topic discover per tick and backs off per topic.
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
from openfeed.models.queue import QueueStatus, SourceInventoryStatus
from openfeed.models.runtime import load_runtime
from openfeed.utils import backpressure, cycle_summary
from openfeed.utils import catalog_io
from openfeed.utils.logging_setup import configure_task_logging
from openfeed.utils.state_io import atomic_write_json


logger = logging.getLogger("supply_cycle")
_DISCOVER_DID_WORK = False
_ESCALATION_PATH = Path("state/supply_escalation.json")
_STARVED_DISCOVER_BACKOFF_SECONDS = 6 * 60 * 60
_LIVE_SOURCE_DISCOVER_BACKOFF_SECONDS = 6 * 60 * 60


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _slot_key(topic: str, platform: str) -> str:
    return f"{topic}:{platform}"


def _search_terms_for_slot(search_terms: dict, topic: str, platform: str) -> list:
    return (
        ((search_terms.get("topics") or {}).get(topic) or {})
        .get(platform, {})
        .get("keywords")
        or []
    )


def _ensure_keywords_for_slot(
    *,
    config: InterestsConfig,
    existing: dict,
    keywords_path: Path,
    topic: str,
    platform: str,
) -> tuple[dict, int]:
    keywords = _search_terms_for_slot(existing, topic, platform)
    if keywords:
        return existing, len(keywords)
    target_entry = next(t for t in config.interests if t.topic == topic)
    runner = GeminiRunner(Path.cwd())
    persona = PersonaOutput.model_validate(config.persona)
    sliced = InterestsConfig(persona=config.persona, interests=[target_entry])
    new_keywords = generate_keywords_per_platform(
        sliced, persona, [], runner,
        only_slots={(topic, platform)},
    )
    merged = merge_search_terms(config, existing, new_keywords)
    atomic_write_json(keywords_path, merged)
    keyword_count = len(_search_terms_for_slot(merged, topic, platform))
    logger.info(
        "seeded %d keywords for %s/%s",
        keyword_count, topic, platform,
    )
    return merged, keyword_count


def _active_source_counts(workdir: Path) -> dict[tuple[str, str], int]:
    catalog = catalog_io.load_catalog(workdir / "state")
    counts: dict[tuple[str, str], int] = {}
    for entry in catalog.sources.values():
        if entry.status != "active":
            continue
        key = (entry.topic, entry.platform)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _topic_demand_priority(status: QueueStatus | None, topic: str) -> tuple[int, int]:
    if status is None:
        return (2, 0)
    topic_status = status.per_topic.get(topic)
    if topic_status is None:
        return (2, 0)
    if topic_status.active_source_count == 0:
        return (0, 0)
    if topic_status.refill_source_count > 0 and topic_status.pushable_count == 0:
        return (0, -topic_status.refill_source_count)
    if topic_status.refill_source_count > 0:
        return (1, -topic_status.refill_source_count)
    return (2, 0)


def _bootstrap_missing(_argv: list[str] | None = None) -> int:
    """Bootstrap one source-missing (topic, platform) slot per tick.

    A slot needs bootstrap when it has no active sources. Missing keywords for
    a slot that already has active sources is bookkeeping, not a reason to run
    full discover ahead of a topic that cannot currently produce content.

    Returns 0 always (no work needed = success). When work is needed and
    discover fails, logs and returns 0 anyway — we don't want a transient
    discover failure to abort the rest of the supply tick.
    """
    global _DISCOVER_DID_WORK
    if _DISCOVER_DID_WORK:
        logger.info("bootstrap_missing: skipped; discover already ran this tick")
        return 0
    workdir = Path.cwd()
    config = load_interests(workdir)
    keywords_path = workdir / "state" / "search_terms.json"
    existing = _load_json(keywords_path)
    active_counts = _active_source_counts(workdir)
    queue_status = _load_queue_status()
    source_missing: list[tuple[tuple[int, int], int, str, str, int]] = []
    keyword_only: list[str] = []
    slot_index = 0
    for entry in config.interests:
        for platform in entry.platforms:
            keywords = _search_terms_for_slot(existing, entry.topic, platform)
            active_count = active_counts.get((entry.topic, platform), 0)
            if active_count == 0:
                source_missing.append((
                    _topic_demand_priority(queue_status, entry.topic),
                    slot_index,
                    entry.topic,
                    platform,
                    len(keywords),
                ))
            elif not keywords:
                keyword_only.append(_slot_key(entry.topic, platform))
            slot_index += 1

    if not source_missing:
        if keyword_only:
            logger.info(
                "bootstrap_missing: ignoring %d keyword-only slot(s) with active sources: %s",
                len(keyword_only),
                keyword_only,
            )
        return 0
    source_missing.sort()
    _, _, target_name, target_platform, keyword_count = source_missing[0]
    target_entry = next(t for t in config.interests if t.topic == target_name)
    logger.info(
        "bootstrap_missing: %d source-missing slot(s); doing %s/%s this tick (rest: %s; keyword-only skipped: %s)",
        len(source_missing),
        target_name,
        target_platform,
        [_slot_key(t, p) for _, _, t, p, _ in source_missing[1:]] or "—",
        keyword_only or "—",
    )

    # ----- step 1: scoped LLM keyword seed (mirrors tmp/scoped_seed_*.py) -----
    if keyword_count == 0:
        existing, keyword_count = _ensure_keywords_for_slot(
            config=config,
            existing=existing,
            keywords_path=keywords_path,
            topic=target_name,
            platform=target_platform,
        )
        logger.info(
            "bootstrap_missing: seeded %d keywords for %s/%s",
            keyword_count, target_name, target_platform,
        )
    else:
        logger.info(
            "bootstrap_missing: %s/%s already has %d keywords",
            target_name, target_platform, keyword_count,
        )

    # ----- step 2: scoped discover for that slot -----
    logger.info(
        "bootstrap_missing: invoking discover --topic %r --platform %s",
        target_name, target_platform,
    )
    try:
        rc = discover_mod.main(["--topic", target_name, "--platform", target_platform])
    except SystemExit as exc:
        rc = _system_exit_rc(
            exc,
            task=f"discover --topic {target_name!r} --platform {target_platform}",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "bootstrap_missing: discover --topic %r --platform %s crashed",
            target_name, target_platform,
        )
        return 0  # don't abort the rest of the supply tick
    _DISCOVER_DID_WORK = True
    logger.info(
        "bootstrap_missing: discover --topic %r --platform %s → rc=%d",
        target_name, target_platform, rc,
    )
    return 0


def _load_queue_status() -> QueueStatus | None:
    path = Path("state/queue_status.json")
    if not path.exists():
        return None
    return QueueStatus.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _seconds_since(iso_value: str | None) -> float | None:
    if not iso_value:
        return None
    try:
        dt = datetime.fromisoformat(iso_value)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _starvation_discover(_argv: list[str] | None = None) -> int:
    """Bounded refill escalation for source-floor starvation.

    Runs at most one scoped topic discover per tick, with a per-topic backoff.
    It is deliberately not a loop-until-success path.
    """
    global _DISCOVER_DID_WORK
    if _DISCOVER_DID_WORK:
        logger.info("starvation_discover: skipped; discover already ran this tick")
        return 0

    status = _load_queue_status()
    if status is None:
        return 0
    config = load_interests(Path.cwd())
    configured_topics = {entry.topic for entry in config.interests}
    starved = sorted(
        topic
        for topic, topic_status in status.per_topic.items()
        if topic in configured_topics
        and topic_status.pushable_count == 0
        and topic_status.under_floor_source_count > 0
        and topic_status.refill_source_count == 0
    )
    if not starved:
        return 0

    state = _load_json(_ESCALATION_PATH)
    by_topic = state.setdefault("starved_discover_last_at", {})
    if not isinstance(by_topic, dict):
        by_topic = {}
        state["starved_discover_last_at"] = by_topic

    target = None
    for topic in starved:
        elapsed = _seconds_since(by_topic.get(topic))
        if elapsed is None or elapsed >= _STARVED_DISCOVER_BACKOFF_SECONDS:
            target = topic
            break
    if target is None:
        logger.info("starvation_discover: all starved topics are in backoff: %s", starved)
        return 0

    logger.info(
        "starvation_discover: %s has no refillable source inventory; invoking discover --topic",
        target,
    )
    try:
        rc = discover_mod.main(["--topic", target])
    except SystemExit as exc:
        rc = _system_exit_rc(exc, task=f"discover --topic {target!r}")
    except Exception:  # noqa: BLE001
        logger.exception("starvation_discover: discover --topic %r crashed", target)
        return 0
    by_topic[target] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    atomic_write_json(_ESCALATION_PATH, state)
    _DISCOVER_DID_WORK = True
    logger.info("starvation_discover: discover --topic %r → rc=%d", target, rc)
    return 0


def _source_is_live(source_status: SourceInventoryStatus) -> bool:
    return (
        source_status.queued_count > 0
        or source_status.pushable_count > 0
        or (source_status.needs_refill and source_status.exhausted_until is None)
    )


def _live_source_discover(_argv: list[str] | None = None) -> int:
    """Discover replacement sources when active sources are mostly depleted.

    `active` means catalog-approved. It does not mean the source still has
    content in the queue. This step focuses on slots that are actually feeding
    the queue now but have too few stocked sources, then runs one scoped
    discover per supply cycle. The per-platform quota is static runtime
    config: slow saturated platforms can stay conservative while idle
    platforms can catch up.
    """
    global _DISCOVER_DID_WORK
    if _DISCOVER_DID_WORK:
        logger.info("live_source_discover: skipped; discover already ran this tick")
        return 0

    status = _load_queue_status()
    if status is None:
        return 0
    workdir = Path.cwd()
    config = load_interests(workdir)
    runtime = load_runtime(Path.cwd())
    min_publishable_sources = runtime.queue_manage.min_publishable_sources_per_slot
    active_counts = _active_source_counts(workdir)
    by_slot: dict[tuple[str, str], list[SourceInventoryStatus]] = {}
    for source_status in status.per_source.values():
        by_slot.setdefault(
            (source_status.topic, source_status.platform),
            [],
        ).append(source_status)

    candidates: list[tuple[int, int, int, tuple[int, int], int, str, str]] = []
    slot_index = 0
    for entry in config.interests:
        for platform in entry.platforms:
            active_count = active_counts.get((entry.topic, platform), 0)
            if active_count == 0:
                slot_index += 1
                continue
            sources = by_slot.get((entry.topic, platform), [])
            queued_total = sum(source.queued_count for source in sources)
            if queued_total == 0:
                slot_index += 1
                continue
            publishable_source_count = sum(1 for source in sources if source.pushable_count > 0)
            if publishable_source_count < min_publishable_sources:
                live_count = sum(1 for source in sources if _source_is_live(source))
                candidates.append((
                    publishable_source_count,
                    live_count,
                    -queued_total,
                    _topic_demand_priority(status, entry.topic),
                    slot_index,
                    entry.topic,
                    platform,
                ))
            slot_index += 1

    if not candidates:
        return 0

    state = _load_json(_ESCALATION_PATH)
    by_slot_state = state.setdefault("live_source_discover_last_at", {})
    if not isinstance(by_slot_state, dict):
        by_slot_state = {}
        state["live_source_discover_last_at"] = by_slot_state

    candidates.sort()
    quotas = {
        platform: max(0, int(limit))
        for platform, limit in runtime.queue_manage.live_source_discover_per_cycle_by_platform.items()
    }
    used_by_platform: dict[str, int] = {}
    targets: list[tuple[str, str, int, int]] = []
    blocked_by_backoff: list[str] = []
    blocked_by_quota: list[str] = []
    for publishable_source_count, live_count, _neg_queued_total, _, _, topic, platform in candidates:
        key = _slot_key(topic, platform)
        quota = quotas.get(platform, 1)
        if used_by_platform.get(platform, 0) >= quota:
            blocked_by_quota.append(key)
            continue
        elapsed = _seconds_since(by_slot_state.get(key))
        if elapsed is None or elapsed >= _LIVE_SOURCE_DISCOVER_BACKOFF_SECONDS:
            targets.append((topic, platform, publishable_source_count, live_count))
            used_by_platform[platform] = used_by_platform.get(platform, 0) + 1
        else:
            blocked_by_backoff.append(key)
    if not targets:
        logger.info(
            "live_source_discover: no low-live-source slot selected "
            "(backoff=%s quota=%s)",
            blocked_by_backoff or "—",
            blocked_by_quota or "—",
        )
        return 0

    opencli_block = backpressure.active_block(backpressure.OPENCLI)
    if opencli_block is not None:
        logger.info(
            "live_source_discover: skipped; opencli backpressure active (%s): %s",
            opencli_block.get("reason"),
            opencli_block.get("detail"),
        )
        return 0

    any_work = False
    keywords_path = workdir / "state" / "search_terms.json"
    existing_keywords = _load_json(keywords_path)
    for topic, platform, publishable_source_count, live_count in targets:
        key = _slot_key(topic, platform)
        logger.info(
            "live_source_discover: %s has publishable_sources=%d live_sources=%d (<%d publishable); invoking discover --topic --platform",
            key,
            publishable_source_count,
            live_count,
            min_publishable_sources,
        )
        if not _search_terms_for_slot(existing_keywords, topic, platform):
            existing_keywords, keyword_count = _ensure_keywords_for_slot(
                config=config,
                existing=existing_keywords,
                keywords_path=keywords_path,
                topic=topic,
                platform=platform,
            )
            if keyword_count == 0:
                logger.warning(
                    "live_source_discover: %s still has no keywords after seeding; skip",
                    key,
                )
                continue
        try:
            rc = discover_mod.main(["--topic", topic, "--platform", platform])
        except SystemExit as exc:
            rc = _system_exit_rc(exc, task=f"discover --topic {topic!r} --platform {platform}")
        except Exception:  # noqa: BLE001
            logger.exception(
                "live_source_discover: discover --topic %r --platform %s crashed",
                topic,
                platform,
            )
            continue
        opencli_block = backpressure.active_block(backpressure.OPENCLI)
        if opencli_block is not None:
            logger.info(
                "live_source_discover: discover stopped by opencli backpressure "
                "(%s): %s",
                opencli_block.get("reason"),
                opencli_block.get("detail"),
            )
            break
        by_slot_state[key] = (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        )
        any_work = True
        logger.info(
            "live_source_discover: discover --topic %r --platform %s → rc=%d",
            topic,
            platform,
            rc,
        )
    if any_work:
        atomic_write_json(_ESCALATION_PATH, state)
        _DISCOVER_DID_WORK = True
        if blocked_by_backoff or blocked_by_quota:
            logger.info(
                "live_source_discover: deferred low-live-source slots "
                "(backoff=%s quota=%s)",
                blocked_by_backoff or "—",
                blocked_by_quota or "—",
            )
    return 0


_TASKS: list[tuple[str, Callable[[list[str] | None], int]]] = [
    ("topic_reconcile", topic_reconcile_mod.main),
    ("live_source_discover", _live_source_discover),
    ("bootstrap_missing", _bootstrap_missing),
    ("starvation_discover", _starvation_discover),
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
    global _DISCOVER_DID_WORK
    _DISCOVER_DID_WORK = False
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
