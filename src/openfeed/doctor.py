from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openfeed.clients.consumer import get_consumer
from openfeed.clients.consumer.http_consumer import HttpConsumerConfig
from openfeed.clients.consumer.ticlawk import TiclawkConsumerConfig
from openfeed.clients.llm import GeminiRunner
from openfeed.models.interests import InterestsConfig, load_interests
from openfeed.models.runtime import load_runtime
from openfeed.opencli_service import client as opencli_service
from openfeed.utils.config_files import load_env, load_openfeed_config


@dataclass
class Check:
    status: str
    name: str
    detail: str


class Doctor:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def ok(self, name: str, detail: str) -> None:
        self.checks.append(Check("OK", name, detail))

    def warn(self, name: str, detail: str) -> None:
        self.checks.append(Check("WARN", name, detail))

    def fail(self, name: str, detail: str) -> None:
        self.checks.append(Check("FAIL", name, detail))

    def print(self) -> None:
        width = max((len(check.name) for check in self.checks), default=0)
        for check in self.checks:
            print(f"[{check.status}] {check.name.ljust(width)}  {check.detail}")
        failed = sum(1 for check in self.checks if check.status == "FAIL")
        warned = sum(1 for check in self.checks if check.status == "WARN")
        passed = sum(1 for check in self.checks if check.status == "OK")
        print(f"\nSummary: {passed} ok, {warned} warning, {failed} failed")

    @property
    def has_failure(self) -> bool:
        return any(check.status == "FAIL" for check in self.checks)


def _resolve_config(arg_value: str | None) -> Path:
    value = arg_value or os.environ.get("OPENFEED_CONFIG_FILE") or "openfeed.yaml"
    return Path(value).expanduser().resolve()


def _resolve_workdir(arg_value: str | None) -> Path:
    value = arg_value or os.environ.get("OPENFEED_WORKDIR") or "output"
    return Path(value).expanduser().resolve()


def _tool_version(command: str, *args: str, timeout: int = 10) -> str:
    result = subprocess.run(
        [command, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0] if output else "installed"


def _run_probe(command: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(command)} timed out after {timeout}s"
    combined = result.stdout + "\n" + result.stderr
    output = "\n".join(combined.strip().splitlines()[-6:])
    if _probe_reported_failure(combined):
        return False, output or "probe reported failure"
    if result.returncode == 0:
        return True, output or "passed"
    return False, output or f"exit code {result.returncode}"


def _probe_reported_failure(output: str) -> bool:
    return any(marker in output for marker in ("[FAIL]", "[MISSING]"))


def _missing_tool_detail(command: str, reason: str) -> str:
    if command == "opencli":
        return (
            f"missing; needed for {reason}. "
            "Fix: run `npm install -g @jackwener/opencli`, then start `openfeed opencli-service`."
        )
    if command == "yt-dlp":
        return (
            f"missing; needed for {reason}. "
            "Fix: rerun `./openfeed/scripts/install` from your instance root."
        )
    if command == "ffmpeg":
        return (
            f"missing; needed for {reason}. "
            "Fix: rerun `./openfeed/scripts/install`, or install ffmpeg with your system package manager."
        )
    return f"missing; needed for {reason}"


def _check_tool(doctor: Doctor, command: str, *, needed: bool, reason: str) -> None:
    if not needed:
        return
    path = shutil.which(command)
    if not path:
        doctor.fail(command, _missing_tool_detail(command, reason))
        return
    try:
        version = _tool_version(command, "--version")
    except Exception:
        version = "installed"
    doctor.ok(command, f"{path} ({version})")


def _enabled_platforms(config: InterestsConfig) -> set[str]:
    platforms: set[str] = set()
    for interest in config.interests:
        platforms.update(str(platform) for platform in interest.platforms.keys())
    return platforms


def _check_templates(doctor: Doctor, config_path: Path, config: InterestsConfig) -> None:
    for interest in config.interests:
        for platform in ("web", "x"):
            platform_config = interest.platforms.get(platform)  # type: ignore[arg-type]
            if platform_config is None or not platform_config.html_template:
                continue
            template_ref = Path(platform_config.html_template)
            name = f"template:{interest.topic}:{platform}"
            if template_ref.is_absolute():
                doctor.fail(name, "html_template must be relative to openfeed.yaml")
                continue
            path = (config_path.parent / template_ref).resolve()
            if path.is_file():
                doctor.ok(name, str(path))
            else:
                doctor.fail(name, f"missing {path}")


def _check_llm(doctor: Doctor, raw: dict[str, Any], workdir: Path) -> None:
    llm = raw.get("llm")
    if not isinstance(llm, dict):
        doctor.fail("llm", "missing llm section")
        return
    openrouter = llm.get("openrouter")
    if not isinstance(openrouter, dict):
        doctor.fail("llm.openrouter", "missing openrouter section")
        return
    model = str(openrouter.get("model", "")).strip()
    api_key_env = str(openrouter.get("api_key_env", "OPENROUTER_API_KEY")).strip()
    if model:
        doctor.ok("llm model", model)
    else:
        doctor.fail("llm model", "missing llm.openrouter.model")
    if api_key_env and os.environ.get(api_key_env, "").strip():
        doctor.ok("llm api key", f"{api_key_env} is set")
    else:
        key_name = api_key_env or "OPENROUTER_API_KEY"
        doctor.fail("llm api key", f"{key_name} is not set. Fix: add it to `.env.local`.")
    try:
        GeminiRunner(workdir)
        doctor.ok("llm client", "OpenRouter client can be constructed")
    except Exception as exc:
        doctor.fail("llm client", str(exc))


def _check_consumers(
    doctor: Doctor,
    config: InterestsConfig,
    workdir: Path,
    *,
    network: bool,
) -> None:
    for interest in config.interests:
        name = f"consumer:{interest.topic}"
        try:
            spec = get_consumer(interest.consumer_type)
            consumer_config = spec.config_model.model_validate(interest.consumer_config)
            doctor.ok(name, f"{interest.consumer_type} config is valid")
        except Exception as exc:
            doctor.fail(name, str(exc))
            continue

        if isinstance(consumer_config, TiclawkConsumerConfig):
            if os.environ.get("TICLAWK_PUBLISHER_API_KEY", "").strip():
                doctor.ok(f"{name}:api key", "TICLAWK_PUBLISHER_API_KEY is set")
            else:
                doctor.fail(
                    f"{name}:api key",
                    "TICLAWK_PUBLISHER_API_KEY is not set. Fix: add it to `.env.local`.",
                )
        if isinstance(consumer_config, HttpConsumerConfig) and consumer_config.api_key_env:
            if os.environ.get(consumer_config.api_key_env, "").strip():
                doctor.ok(f"{name}:api key", f"{consumer_config.api_key_env} is set")
            else:
                doctor.fail(
                    f"{name}:api key",
                    f"{consumer_config.api_key_env} is not set. Fix: add it to `.env.local`.",
                )

        if not network:
            continue
        if not workdir.exists():
            doctor.warn(f"{name}:metrics", f"skipped because workdir is missing: {workdir}")
            continue

        old_cwd = Path.cwd()
        try:
            os.chdir(workdir)
            metrics = spec.get_metrics(consumer_config)
            doctor.ok(f"{name}:metrics", str(metrics))
        except Exception as exc:
            doctor.fail(f"{name}:metrics", str(exc))
        finally:
            os.chdir(old_cwd)


def _check_tools(doctor: Doctor, platforms: set[str], *, network: bool) -> None:
    doctor.ok("python", sys.version.split()[0])
    needs_browser = bool(platforms & {"youtube", "x", "tiktok"})
    needs_media = bool(platforms & {"youtube", "tiktok"})
    _check_tool(
        doctor,
        "yt-dlp",
        needed=needs_media,
        reason=", ".join(sorted(platforms & {"youtube", "tiktok"})),
    )
    _check_tool(doctor, "ffmpeg", needed=needs_media, reason="video preparation")
    _check_tool(
        doctor,
        "opencli",
        needed=needs_browser,
        reason=", ".join(sorted(platforms & {"youtube", "x", "tiktok"})),
    )
    if needs_browser and network and shutil.which("opencli"):
        try:
            health = opencli_service.health()
        except opencli_service.OpenCLIServiceError as exc:
            doctor.fail(
                "opencli service",
                "OpenCLI service is not reachable. Start it with `openfeed opencli-service` "
                f"or use `openfeed start`.\n{exc}",
            )
        else:
            version = health.get("opencli_version") or "unknown"
            jobs = health.get("jobs") or {}
            doctor.ok("opencli service", f"opencli={version} jobs={jobs}")


def run_doctor(config_path: Path, workdir: Path, *, network: bool) -> Doctor:
    doctor = Doctor()
    instance_root = config_path.parent
    os.environ["OPENFEED_CONFIG_FILE"] = str(config_path)
    os.environ["OPENFEED_WORKDIR"] = str(workdir)

    load_env(instance_root)
    env_path = instance_root / ".env.local"
    if env_path.is_file():
        doctor.ok(".env.local", str(env_path))
    else:
        doctor.warn(".env.local", f"not found at {env_path}; shell environment will be used")

    if config_path.is_file():
        doctor.ok("openfeed.yaml", str(config_path))
    else:
        doctor.fail("openfeed.yaml", f"missing {config_path}")
        return doctor

    if workdir.exists():
        doctor.ok("workdir", str(workdir))
    else:
        doctor.warn("workdir", f"missing {workdir}; create it before running tasks directly")

    try:
        interests = load_interests(workdir)
        runtime = load_runtime(workdir)
        topics = ", ".join(interest.topic for interest in interests.interests)
        doctor.ok("config", f"{len(interests.interests)} topic(s): {topics}")
        doctor.ok("producer", runtime.push.producer)
    except Exception as exc:
        doctor.fail("config", str(exc))
        return doctor

    try:
        raw = load_openfeed_config()
    except Exception as exc:
        doctor.fail("yaml", str(exc))
        return doctor

    _check_templates(doctor, config_path, interests)
    _check_llm(doctor, raw, workdir)
    _check_consumers(doctor, interests, workdir, network=network)
    _check_tools(doctor, _enabled_platforms(interests), network=network)
    return doctor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openfeed-doctor")
    parser.add_argument(
        "--config",
        help="path to openfeed.yaml; defaults to OPENFEED_CONFIG_FILE or ./openfeed.yaml",
    )
    parser.add_argument(
        "--workdir",
        help="runtime output directory; defaults to OPENFEED_WORKDIR or ./output",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="skip consumer metrics and opencli service probes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    doctor = run_doctor(
        _resolve_config(args.config),
        _resolve_workdir(args.workdir),
        network=not args.no_network,
    )
    doctor.print()
    return 1 if doctor.has_failure else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
