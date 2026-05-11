from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib import error, request


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


class OpenCLIServiceError(RuntimeError):
    pass


def service_url() -> str:
    explicit = os.environ.get("OPENFEED_OPENCLI_SERVICE_URL")
    if explicit:
        return explicit.rstrip("/")
    host = os.environ.get("OPENFEED_OPENCLI_SERVICE_HOST", "127.0.0.1")
    port = os.environ.get("OPENFEED_OPENCLI_SERVICE_PORT", "19826")
    return f"http://{host}:{port}".rstrip("/")


def _request_json(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: int = 10) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(service_url() + path, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - localhost service URL
            raw = response.read().decode("utf-8")
    except (OSError, error.URLError) as exc:
        raise OpenCLIServiceError(f"opencli service unavailable at {service_url()}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OpenCLIServiceError(f"opencli service returned non-JSON: {raw[:300]!r}") from exc
    if not isinstance(parsed, dict):
        raise OpenCLIServiceError("opencli service returned non-object JSON")
    if "error" in parsed and len(parsed) == 1:
        raise OpenCLIServiceError(str(parsed["error"]))
    return parsed


def health() -> dict[str, Any]:
    return _request_json("GET", "/v1/health")


def submit_job(
    *,
    project: str,
    site: str,
    command: str,
    args: list[str],
    timeout_seconds: int,
    priority: int = 0,
    profile: str = "default",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project": project,
        "site": site,
        "command": command,
        "args": args,
        "timeout_seconds": timeout_seconds,
        "priority": priority,
        "profile": profile,
    }
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    return _request_json("POST", "/v1/jobs", payload)


def get_job(job_id: str) -> dict[str, Any]:
    return _request_json("GET", f"/v1/jobs/{job_id}")["job"]


def get_result(job_id: str) -> dict[str, Any]:
    return _request_json("GET", f"/v1/jobs/{job_id}/result")


def wait_for_job(job_id: str, *, poll_seconds: float = 1.0) -> dict[str, Any]:
    while True:
        job = get_job(job_id)
        if str(job.get("status")) in TERMINAL_STATUSES:
            return get_result(job_id)
        time.sleep(poll_seconds)
