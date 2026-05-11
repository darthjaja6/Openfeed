from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from .models import Lane


@dataclass(frozen=True)
class ExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    parsed: Any | None
    error: str | None


def execute_opencli_job(job: dict[str, Any], lane: Lane) -> ExecutionResult:
    args = json.loads(str(job["args_json"]))
    if not isinstance(args, list):
        args = []
    timeout = int(job["timeout_seconds"] or lane.timeout_seconds)
    command = [
        "opencli",
        str(job["site"]),
        str(job["command"]),
        *[str(value) for value in args],
        "--format",
        "json",
        "--reuse",
        "none",
    ]
    env = os.environ.copy()
    env["OPENCLI_BROWSER_COMMAND_TIMEOUT"] = str(timeout)
    env["OPENCLI_SERVICE_LANE"] = lane.id
    env["OPENCLI_SERVICE_WORKSPACE"] = lane.workspace
    if lane.profile and lane.profile != "default":
        env["OPENCLI_PROFILE"] = lane.profile
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return ExecutionResult(
            returncode=124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            parsed=None,
            error=f"opencli job timed out after {timeout}s",
        )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    parsed = None
    parse_error = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            parse_error = f"opencli returned non-JSON stdout: {stdout[:300]!r}"
    return ExecutionResult(
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        parsed=parsed,
        error=parse_error,
    )
