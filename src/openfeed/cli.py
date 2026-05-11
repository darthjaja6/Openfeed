from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from openfeed.opencli_service import client as opencli_service


@dataclass(frozen=True)
class Instance:
    root: Path
    config: Path
    workdir: Path


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[bytes]
    log_path: Path
    log_handle: object


CORE_COMMANDS: dict[str, str] = {
    "cleanup-assets": "openfeed.core.cleanup_assets",
    "collect-feedback": "openfeed.core.collect_feedback",
    "discover": "openfeed.core.discover",
    "filter": "openfeed.core.filter",
    "learn": "openfeed.core.learn",
    "local-server": "openfeed.clients.consumer.local_web",
    "opencli-service": "openfeed.opencli_service.server",
    "patrol": "openfeed.core.patrol",
    "prepare": "openfeed.core.prepare_video",
    "push": "openfeed.core.push",
    "queue-manage": "openfeed.core.queue_manage",
    "refill": "openfeed.core.refill_cycle",
    "supply": "openfeed.core.supply_cycle",
    "topic-reconcile": "openfeed.core.topic_reconcile",
}

EXPLICIT_PATH_COMMANDS: dict[str, str] = {
    "doctor": "openfeed.doctor",
    "smoke": "openfeed.smoke_publish",
    "smoke-publish": "openfeed.smoke_publish",
    "status": "openfeed.status",
}

LOCKED_COMMANDS: dict[str, str] = {
    "discover": "discover",
    "prepare": "prepare-video",
    "refill": "refill",
    "supply": "supply",
}


def _usage() -> None:
    commands = sorted([*CORE_COMMANDS, *EXPLICIT_PATH_COMMANDS, "start"])
    print(
        "usage: openfeed [--instance DIR] [--config PATH] [--workdir DIR] "
        "COMMAND [args]\n\n"
        "Common commands:\n"
        "  start            run doctor, then foreground supply/refill/prepare loops\n"
        "  supply           run one supply cycle; pass --loop for daemon mode\n"
        "  refill           run one refill cycle; pass --loop for daemon mode\n"
        "  prepare          download/prepare local media for queued items\n"
        "  discover         run source discovery\n"
        "  doctor           validate config, credentials, templates, and tools\n"
        "  status           show instance state\n"
        "  smoke            push one smoke-test card\n\n"
        "All commands run against an instance directory containing openfeed.yaml.\n"
        f"Available commands: {', '.join(commands)}"
    )


def _start_usage() -> None:
    print(
        "usage: openfeed [global options] start "
        "[--local-server] [--open] [--supply-interval SECONDS] "
        "[--prepare-interval SECONDS] [--refill-interval SECONDS]\n\n"
        "Global options:\n"
        "  --instance DIR   instance directory; default: current directory\n"
        "  --config PATH    config file; default: <instance>/openfeed.yaml\n"
        "  --workdir DIR    runtime directory; default: <instance>/output"
    )


def _parse_global_args(argv: Sequence[str]) -> tuple[dict[str, str], str | None, list[str]]:
    options: dict[str, str] = {}
    args = list(argv)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"-h", "--help"}:
            return options, None, [arg]
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-"):
            command = arg
            return options, command, args[i + 1 :]
        if arg in {"--instance", "--config", "--workdir", "--output"}:
            if i + 1 >= len(args):
                raise ValueError(f"{arg} requires a value")
            key = "workdir" if arg == "--output" else arg.removeprefix("--")
            options[key] = args[i + 1]
            i += 2
            continue
        raise ValueError(f"unknown global option: {arg}")
    if i < len(args):
        return options, args[i], args[i + 1 :]
    return options, None, []


def _resolve_instance(options: dict[str, str]) -> Instance:
    root = Path(options.get("instance") or ".").expanduser().resolve()
    config = (
        Path(options["config"]).expanduser().resolve()
        if "config" in options
        else root / "openfeed.yaml"
    )
    workdir = (
        Path(options["workdir"]).expanduser().resolve()
        if "workdir" in options
        else root / "output"
    )
    if not config.is_file():
        raise FileNotFoundError(f"missing OpenFeed config file: {config}")
    workdir.mkdir(parents=True, exist_ok=True)
    return Instance(root=root, config=config, workdir=workdir)


def _env(instance: Instance) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENFEED_CONFIG_FILE"] = str(instance.config)
    env["OPENFEED_WORKDIR"] = str(instance.workdir)
    return env


def _module_command(module: str, args: Sequence[str]) -> list[str]:
    return [sys.executable, "-m", module, *args]


def _run_subprocess(
    module: str,
    instance: Instance,
    args: Sequence[str],
    *,
    explicit_paths: bool = False,
    lock_label: str | None = None,
) -> int:
    module_args = list(args)
    if explicit_paths:
        module_args = [
            "--config",
            str(instance.config),
            "--workdir",
            str(instance.workdir),
            *module_args,
        ]
    command = _module_command(module, module_args)
    if lock_label:
        command = _module_command(
            "openfeed.utils.run_with_lock",
            [f"/tmp/openfeed-{lock_label}.lock", lock_label, "--", *command],
        )
    return subprocess.run(
        command,
        cwd=instance.workdir,
        env=_env(instance),
        check=False,
    ).returncode


def _run_command_help(command: str, args: Sequence[str]) -> int:
    if command in EXPLICIT_PATH_COMMANDS:
        return subprocess.run(
            _module_command(EXPLICIT_PATH_COMMANDS[command], args),
            check=False,
        ).returncode
    if command in CORE_COMMANDS:
        return subprocess.run(
            _module_command(CORE_COMMANDS[command], args),
            check=False,
        ).returncode
    return 2


def _run_opencli_service_command(options: dict[str, str], args: Sequence[str]) -> int:
    if "config" in options:
        print(
            "opencli-service uses its own config; pass it after the command: "
            "openfeed opencli-service --config PATH",
            file=sys.stderr,
        )
        return 2
    root = Path(options.get("instance") or ".").expanduser().resolve()
    workdir = Path(options.get("workdir") or (root / "output")).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["OPENFEED_WORKDIR"] = str(workdir)
    env.pop("OPENFEED_CONFIG_FILE", None)

    module_args = list(args)
    has_workdir_arg = any(arg == "--workdir" or arg.startswith("--workdir=") for arg in module_args)
    if not has_workdir_arg:
        module_args = ["--workdir", str(workdir), *module_args]

    return subprocess.run(
        _module_command("openfeed.opencli_service.server", module_args),
        cwd=workdir,
        env=env,
        check=False,
    ).returncode


def _parse_start_args(args: Sequence[str]) -> dict[str, object]:
    values: dict[str, object] = {
        "local_server": False,
        "open_browser": False,
        "supply_interval": int(os.environ.get("OPENFEED_SUPPLY_INTERVAL", "900")),
        "prepare_interval": float(os.environ.get("OPENFEED_PREPARE_INTERVAL", "10")),
        "refill_interval": None,
    }
    rest = list(args)
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in {"-h", "--help"}:
            values["help"] = True
            return values
        if arg == "--local-server":
            values["local_server"] = True
            i += 1
            continue
        if arg == "--open":
            values["local_server"] = True
            values["open_browser"] = True
            i += 1
            continue
        if arg in {"--supply-interval", "--prepare-interval", "--refill-interval"}:
            if i + 1 >= len(rest):
                raise ValueError(f"{arg} requires a value")
            if arg == "--supply-interval":
                values["supply_interval"] = int(rest[i + 1])
            elif arg == "--prepare-interval":
                values["prepare_interval"] = float(rest[i + 1])
            else:
                values["refill_interval"] = int(rest[i + 1])
            i += 2
            continue
        raise ValueError(f"unknown start option: {arg}")
    return values


def _write_log_header(log_path: Path, name: str) -> object:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    handle.write(f"\n==== {stamp} openfeed {name} started ====\n".encode("utf-8"))
    handle.flush()
    return handle


def _start_process(
    instance: Instance,
    name: str,
    command: Sequence[str],
) -> ManagedProcess:
    log_path = instance.workdir / "logs" / f"{name}.out"
    handle = _write_log_header(log_path, name)
    process = subprocess.Popen(
        list(command),
        cwd=instance.workdir,
        env=_env(instance),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return ManagedProcess(
        name=name,
        process=process,
        log_path=log_path,
        log_handle=handle,
    )


def _stop_processes(processes: Sequence[ManagedProcess]) -> None:
    for managed in processes:
        if managed.process.poll() is None:
            managed.process.terminate()
    deadline = time.monotonic() + 10
    for managed in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            managed.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            managed.process.kill()
            managed.process.wait()
        close = getattr(managed.log_handle, "close", None)
        if close is not None:
            close()


def _tail(path: Path, lines: int = 40) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(data[-lines:])


def _wait_opencli_service(timeout_seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            opencli_service.health()
            return True
        except opencli_service.OpenCLIServiceError:
            time.sleep(0.25)
    return False


def _run_prepare_loop(instance: Instance, args: Sequence[str]) -> int:
    interval = float(args[0]) if args else float(os.environ.get("OPENFEED_PREPARE_INTERVAL", "10"))
    while True:
        _run_subprocess("openfeed.core.prepare_video", instance, [])
        time.sleep(interval)


def _run_start(instance: Instance, args: Sequence[str]) -> int:
    options = _parse_start_args(args)
    if options.get("help"):
        _start_usage()
        return 0

    processes: list[ManagedProcess] = []
    stop_requested = False

    processes.append(
        _start_process(
            instance,
            "opencli-service",
            _module_command(
                "openfeed.opencli_service.server",
                ["--workdir", str(instance.workdir)],
            ),
        )
    )
    if not _wait_opencli_service():
        tail = _tail(processes[0].log_path)
        print(
            f"opencli service did not become healthy; last log lines from {processes[0].log_path}:\n{tail}",
            file=sys.stderr,
        )
        _stop_processes(processes)
        return 1

    print("Running openfeed doctor...")
    doctor_rc = _run_subprocess(
        "openfeed.doctor",
        instance,
        [],
        explicit_paths=True,
    )
    if doctor_rc != 0:
        _stop_processes(processes)
        return doctor_rc

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)

    try:
        supply_args = ["--loop", "--interval", str(options["supply_interval"])]
        processes.append(
            _start_process(
                instance,
                "supply",
                _module_command("openfeed.core.supply_cycle", supply_args),
            )
        )

        refill_args = ["--loop"]
        if options["refill_interval"] is not None:
            refill_args.extend(["--interval", str(options["refill_interval"])])
        processes.append(
            _start_process(
                instance,
                "refill",
                _module_command("openfeed.core.refill_cycle", refill_args),
            )
        )

        processes.append(
            _start_process(
                instance,
                "prepare",
                _module_command(
                    "openfeed.cli",
                    [
                        "--instance",
                        str(instance.root),
                        "--config",
                        str(instance.config),
                        "--workdir",
                        str(instance.workdir),
                        "_prepare-loop",
                        str(options["prepare_interval"]),
                    ],
                ),
            )
        )

        if options["local_server"]:
            local_args = ["--open"] if options["open_browser"] else []
            processes.append(
                _start_process(
                    instance,
                    "local-server",
                    _module_command("openfeed.clients.consumer.local_web", local_args),
                )
            )

        print("OpenFeed is running.")
        print(f"Instance: {instance.root}")
        print(f"Config: {instance.config}")
        print(f"Workdir: {instance.workdir}")
        print(f"Logs: {instance.workdir / 'logs'}")
        if options["local_server"]:
            print("Local feed: http://127.0.0.1:8765/")
        print("Press Ctrl-C to stop.")

        while not stop_requested:
            for managed in processes:
                rc = managed.process.poll()
                if rc is None:
                    continue
                print(
                    f"openfeed {managed.name} exited with rc={rc}; stopping the rest",
                    file=sys.stderr,
                )
                tail = _tail(managed.log_path)
                if tail:
                    print(
                        f"last log lines from {managed.log_path}:\n{tail}",
                        file=sys.stderr,
                    )
                return rc or 1
            time.sleep(2)
        return 0
    finally:
        _stop_processes(processes)
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    try:
        options, command, args = _parse_global_args(raw_args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        _usage()
        return 2

    if command is None:
        _usage()
        return 0 if args and args[0] in {"-h", "--help"} else 2

    if args in (["-h"], ["--help"]) and command != "start":
        return _run_command_help(command, args)

    if command == "start" and args in (["-h"], ["--help"]):
        _start_usage()
        return 0

    if command == "opencli-service":
        return _run_opencli_service_command(options, args)

    try:
        instance = _resolve_instance(options)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if command == "_prepare-loop":
        return _run_prepare_loop(instance, args)

    if command == "start":
        try:
            return _run_start(instance, args)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            _start_usage()
            return 2

    if command in EXPLICIT_PATH_COMMANDS:
        return _run_subprocess(
            EXPLICIT_PATH_COMMANDS[command],
            instance,
            args,
            explicit_paths=True,
        )

    if command in CORE_COMMANDS:
        return _run_subprocess(
            CORE_COMMANDS[command],
            instance,
            args,
            lock_label=LOCKED_COMMANDS.get(command),
        )

    print(f"unknown command: {command}", file=sys.stderr)
    _usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
