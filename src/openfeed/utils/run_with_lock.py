"""Run one command while holding a cross-platform fcntl file lock.

Used by shell wrappers instead of the external `flock` binary, which is not
present on a default macOS install.
"""
from __future__ import annotations

import argparse
import fcntl
import subprocess
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m openfeed.utils.run_with_lock")
    parser.add_argument("lock_path")
    parser.add_argument("label")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("missing command", file=sys.stderr)
        return 2

    lock_path = Path(args.lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"previous {args.label} run still active; skipping")
            return 0

        try:
            return subprocess.run(command, check=False).returncode
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
