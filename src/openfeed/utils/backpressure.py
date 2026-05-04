"""Small lane-level pause state for systemic dependency failures.

This is intentionally file-backed and boring: tasks check a lane before doing
work, and dependency errors mark that lane blocked. Queue items stay in place.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openfeed.utils.state_io import atomic_write_json


STATE_PATH = Path("state/backpressure.json")
LOCK_PATH = Path("state/backpressure.lock")

TICLAWK_API = "ticlawk_api"
TICLAWK_VIDEO_UPLOAD = "ticlawk_video_upload"
YOUTUBE_DOWNLOAD = "youtube_download"
TIKTOK_DOWNLOAD = "tiktok_download"
OPENCLI = "opencli"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@contextmanager
def _locked():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_unlocked() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"generated_at": _now_iso(), "lanes": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"generated_at": _now_iso(), "lanes": {}}
    if not isinstance(data, dict):
        return {"generated_at": _now_iso(), "lanes": {}}
    lanes = data.get("lanes")
    if not isinstance(lanes, dict):
        data["lanes"] = {}
    return data


def _save_unlocked(data: dict[str, Any]) -> None:
    data["generated_at"] = _now_iso()
    atomic_write_json(STATE_PATH, data)


def block_lane(
    lane: str,
    *,
    reason: str,
    detail: str = "",
    retry_after: str | None = None,
    cooldown_seconds: int | None = None,
) -> None:
    """Mark a lane blocked.

    If neither `retry_after` nor `cooldown_seconds` is supplied, the lane stays
    blocked until `clear_lane` is called.
    """
    if retry_after is None and cooldown_seconds is not None:
        retry_after = (_now() + timedelta(seconds=cooldown_seconds)).isoformat()
    with _locked():
        data = _load_unlocked()
        data["lanes"][lane] = {
            "reason": reason,
            "detail": detail[:1000],
            "blocked_at": _now_iso(),
            "retry_after": retry_after,
        }
        _save_unlocked(data)


def clear_lane(lane: str) -> bool:
    with _locked():
        data = _load_unlocked()
        existed = lane in data["lanes"]
        data["lanes"].pop(lane, None)
        _save_unlocked(data)
        return existed


def clear_all() -> int:
    with _locked():
        data = _load_unlocked()
        count = len(data["lanes"])
        data["lanes"] = {}
        _save_unlocked(data)
        return count


def active_block(lane: str) -> dict[str, Any] | None:
    """Return the active block record, auto-clearing expired cooldowns."""
    with _locked():
        data = _load_unlocked()
        record = data["lanes"].get(lane)
        if not isinstance(record, dict):
            return None
        retry_after = _parse_time(record.get("retry_after"))
        if retry_after is not None and _now() >= retry_after:
            data["lanes"].pop(lane, None)
            _save_unlocked(data)
            return None
        return dict(record)


def all_blocks() -> dict[str, Any]:
    """Return current blocks after clearing expired cooldowns."""
    with _locked():
        data = _load_unlocked()
        changed = False
        out: dict[str, Any] = {}
        for lane, record in list(data["lanes"].items()):
            if not isinstance(record, dict):
                data["lanes"].pop(lane, None)
                changed = True
                continue
            retry_after = _parse_time(record.get("retry_after"))
            if retry_after is not None and _now() >= retry_after:
                data["lanes"].pop(lane, None)
                changed = True
                continue
            out[lane] = dict(record)
        if changed:
            _save_unlocked(data)
        return out


def _main_status() -> int:
    blocks = all_blocks()
    if not blocks:
        print("no active backpressure")
        return 0
    print(json.dumps({"lanes": blocks}, ensure_ascii=False, indent=2))
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openfeed-backpressure")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    clear = sub.add_parser("clear")
    clear.add_argument("lane", nargs="?", help="lane to clear; omit with --all")
    clear.add_argument("--all", action="store_true")
    pause = sub.add_parser("pause")
    pause.add_argument("lane")
    pause.add_argument("reason")
    pause.add_argument("--detail", default="")
    pause.add_argument("--cooldown-seconds", type=int, default=None)
    pause.add_argument("--retry-after", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "status":
        return _main_status()
    if args.cmd == "clear":
        if args.all:
            count = clear_all()
            print(f"cleared {count} lane(s)")
            return 0
        if not args.lane:
            ap.error("clear requires a lane or --all")
        existed = clear_lane(args.lane)
        print(f"{'cleared' if existed else 'not active'}: {args.lane}")
        return 0
    if args.cmd == "pause":
        block_lane(
            args.lane,
            reason=args.reason,
            detail=args.detail,
            retry_after=args.retry_after,
            cooldown_seconds=args.cooldown_seconds,
        )
        print(f"blocked: {args.lane}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
