"""Human-readable OpenFeed runtime status.

Reads only local state / ledgers. It does not call LLMs, OpenCLI, Chrome, or
consumer APIs.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from openfeed.models.history import HistoryEntry
from openfeed.models.interests import load_interests
from openfeed.models.queue import Queue, QueueStatus
from openfeed.models.video_cache import VideoCacheIndex
from openfeed.utils import backpressure, catalog_io
from openfeed.utils.config_files import config_path, load_env


_QUEUE_PATH = Path("state/queue.json")
_QUEUE_STATUS_PATH = Path("state/queue_status.json")
_VIDEO_CACHE_PATH = Path("state/video_cache_index.json")
_HISTORY_PATH = Path("ledgers/history.jsonl")
_CYCLE_SUMMARY_PATH = Path("ledgers/cycle_summary.jsonl")
_PATROL_DIR = Path("queues/patrol")
_PROCESS_MARKERS = {
    "supply": "openfeed-supply-cycle",
    "refill": "openfeed-refill-cycle",
    "prepare": "openfeed-prepare-video",
    "local_server": "openfeed-local-server",
}


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _load_queue() -> Queue | None:
    raw = _read_json(_QUEUE_PATH)
    if raw is None:
        return None
    try:
        return Queue.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None


def _load_queue_status() -> QueueStatus | None:
    raw = _read_json(_QUEUE_STATUS_PATH)
    if raw is None:
        return None
    try:
        return QueueStatus.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None


def _load_video_cache() -> VideoCacheIndex | None:
    raw = _read_json(_VIDEO_CACHE_PATH)
    if raw is None:
        return None
    try:
        return VideoCacheIndex.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None


def _last_history() -> HistoryEntry | None:
    if not _HISTORY_PATH.exists():
        return None
    lines = [line for line in _HISTORY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            return HistoryEntry.model_validate_json(line)
        except Exception:  # noqa: BLE001
            continue
    return None


def _last_cycle(cycle: str) -> dict[str, Any] | None:
    if not _CYCLE_SUMMARY_PATH.exists():
        return None
    lines = [line for line in _CYCLE_SUMMARY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("cycle") == cycle:
            return row
    return None


def _lock_state(label: str) -> str:
    path = Path(f"/tmp/openfeed-{label}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        locked = False
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            return "running"
        finally:
            if locked:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return "idle"


def _print_cycle(label: str, row: dict[str, Any] | None) -> None:
    if row is None:
        print(f"- last {label}: none")
        return
    print(
        f"- last {label}: rc={row.get('rc')} "
        f"started={row.get('started_at')} ended={row.get('ended_at')}"
    )
    phases = row.get("phases")
    if isinstance(phases, dict) and phases:
        print(f"  phases: {json.dumps(phases, ensure_ascii=False)}")


def _process_state() -> dict[str, list[str]]:
    try:
        result = subprocess.run(
            ["ps", "ax", "-o", "pid=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return {}
    found: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or "openfeed-status" in stripped:
            continue
        for label, marker in _PROCESS_MARKERS.items():
            if marker in stripped:
                pid = stripped.split(maxsplit=1)[0]
                found.setdefault(label, []).append(pid)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openfeed-status")
    parser.add_argument(
        "--workdir",
        help="runtime output directory; defaults to OPENFEED_WORKDIR or ./output",
    )
    parser.add_argument(
        "--config",
        help="path to openfeed.yaml; defaults to OPENFEED_CONFIG_FILE",
    )
    args = parser.parse_args(argv)

    if args.config:
        os.environ["OPENFEED_CONFIG_FILE"] = str(Path(args.config).expanduser().resolve())
    workdir = Path(args.workdir or os.environ.get("OPENFEED_WORKDIR") or "output").expanduser().resolve()
    load_env(workdir)
    old_cwd = Path.cwd()
    workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(workdir)
    try:
        print("OpenFeed status")
        print(f"- workdir: {workdir}")
        try:
            print(f"- config: {config_path()}")
        except Exception:
            print("- config: not set")

        try:
            config = load_interests(workdir)
            print(f"- topics: {', '.join(t.topic for t in config.interests)}")
        except Exception as exc:  # noqa: BLE001
            print(f"- topics: unavailable ({exc})")

        processes = _process_state()
        if processes:
            parts = ", ".join(
                f"{label}={','.join(pids)}" for label, pids in sorted(processes.items())
            )
            print(f"- processes: {parts}")
        else:
            print("- processes: none detected")

        print(
            "- locks: "
            f"supply={_lock_state('supply')} "
            f"refill={_lock_state('refill')} "
            f"prepare={_lock_state('prepare-video')} "
            f"discover={_lock_state('discover')}"
        )

        blocks = backpressure.all_blocks()
        if blocks:
            print(f"- backpressure: {json.dumps(blocks, ensure_ascii=False)}")
        else:
            print("- backpressure: none")

        queue = _load_queue()
        if queue is None:
            print("- queue: missing")
        else:
            total = sum(len(items) for items in queue.topics.values())
            print(f"- queue: {total} item(s)")
            for topic, items in sorted(queue.topics.items()):
                platforms = Counter(item.content.platform for item in items)
                parts = ", ".join(f"{k}={v}" for k, v in sorted(platforms.items()))
                print(f"  {topic}: {len(items)} ({parts or 'empty'})")

        status = _load_queue_status()
        if status is not None:
            print(f"- refill topics: {status.refill_topics}")

        patrol_files = len(list(_PATROL_DIR.glob("*.json"))) if _PATROL_DIR.exists() else 0
        print(f"- patrol queue: {patrol_files} file(s)")

        catalog = catalog_io.load_catalog(Path("state"))
        active = Counter((entry.topic, entry.platform) for entry in catalog.sources.values() if entry.status == "active")
        if active:
            print("- active sources:")
            for (topic, platform), count in sorted(active.items()):
                print(f"  {topic}/{platform}: {count}")
        else:
            print("- active sources: none")

        video_cache = _load_video_cache()
        if video_cache is None:
            print("- video cache: missing")
        else:
            states = Counter(entry.state for entry in video_cache.videos.values())
            print(
                "- video cache: "
                f"ready={states.get('ready', 0)} "
                f"failed={states.get('failed', 0)} "
                f"permanently_failed={states.get('permanently_failed', 0)}"
            )
            failed = [
                entry for entry in video_cache.videos.values()
                if entry.state in {"failed", "permanently_failed"}
            ]
            for entry in sorted(failed, key=lambda e: e.last_failed_at or "", reverse=True)[:3]:
                print(f"  {entry.video_id}: {entry.state} {entry.last_error or ''}".strip())

        last = _last_history()
        if last is None:
            print("- last push: none")
        else:
            print(
                f"- last push: {last.pushed_at} topic={last.topic} "
                f"platform={last.platform} card_id={last.card_id}"
            )

        _print_cycle("supply", _last_cycle("supply"))
        _print_cycle("refill", _last_cycle("refill"))
        return 0
    finally:
        try:
            os.chdir(old_cwd)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
