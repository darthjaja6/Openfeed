from __future__ import annotations

import sys


def _usage() -> None:
    print("usage: openfeed doctor [--config PATH] [--workdir PATH] [--no-network]")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        _usage()
        return 0
    command = args.pop(0)
    if command == "doctor":
        from openfeed.doctor import main as doctor_main

        return doctor_main(args)
    print(f"unknown command: {command}", file=sys.stderr)
    _usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
