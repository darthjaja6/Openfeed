from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from openfeed.utils.logging_setup import configure_task_logging

from .config import load_service_config
from .models import JobRequest, PoolConfig
from .scheduler import OpenCLIService
from .store import JobStore

logger = logging.getLogger("opencli_service")


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(_json_safe(payload), ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    data = handler.rfile.read(length)
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("request body must be a JSON object")
    return parsed


def _opencli_version() -> str | None:
    try:
        proc = subprocess.run(
            ["opencli", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def _opencli_capabilities(*, site: str | None = None, command: str | None = None) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["opencli", "list", "-f", "json"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "opencli list failed")[:500])
    parsed = json.loads(proc.stdout or "[]")
    if not isinstance(parsed, list):
        raise RuntimeError("opencli list returned non-list JSON")
    out: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        item_site = str(item.get("site") or "")
        item_name = str(item.get("name") or "")
        item_command = str(item.get("command") or "")
        if site and item_site != site:
            continue
        if command and command not in {item_name, item_command, f"{item_site}/{item_name}"}:
            continue
        out.append(item)
    return out


class OpenCLIHTTPServer(ThreadingHTTPServer):
    service: OpenCLIService
    store: JobStore
    default_profile: str
    default_timeout_seconds: int
    opencli_version: str | None


class Handler(BaseHTTPRequestHandler):
    server: OpenCLIHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/v1/health":
            _json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "opencli_version": self.server.opencli_version,
                    "jobs": self.server.store.counts(),
                    "pools": self.server.service.known_pools(),
                },
            )
            return
        if parsed.path == "/v1/pools":
            _json_response(self, HTTPStatus.OK, {"pools": self.server.service.known_pools()})
            return
        if parsed.path == "/v1/capabilities":
            query = parse_qs(parsed.query)
            site = (query.get("site") or [None])[0]
            command = (query.get("command") or [None])[0]
            try:
                commands = _opencli_capabilities(site=site, command=command)
            except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            _json_response(self, HTTPStatus.OK, {"commands": commands})
            return
        if parsed.path.startswith("/v1/jobs/"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) not in {3, 4}:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            job = self.server.store.get(parts[2])
            if job is None:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "job_not_found"})
                return
            if len(parts) == 4 and parts[3] == "result":
                result = None
                if job.get("result_json"):
                    try:
                        result = json.loads(str(job["result_json"]))
                    except json.JSONDecodeError:
                        result = None
                _json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "id": job["id"],
                        "project": job["project"],
                        "profile": job["profile"],
                        "site": job["site"],
                        "command": job["command"],
                        "args": json.loads(str(job["args_json"])),
                        "lane_id": job["lane_id"],
                        "attempts": job["attempts"],
                        "status": job["status"],
                        "returncode": job["returncode"],
                        "result": result,
                        "stdout": job["stdout"],
                        "stderr": job["stderr"],
                        "error": job["error"],
                    },
                )
                return
            _json_response(self, HTTPStatus.OK, {"job": job})
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = _read_json(self)
        except (json.JSONDecodeError, ValueError) as exc:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if parsed.path == "/v1/jobs":
            try:
                request = JobRequest.from_json(
                    body,
                    default_profile=self.server.default_profile,
                    default_timeout=self.server.default_timeout_seconds,
                )
            except (TypeError, ValueError) as exc:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self.server.service.wake_for(request.profile, request.site)
            job = self.server.store.enqueue(request)
            _json_response(self, HTTPStatus.ACCEPTED, {"job": job})
            return
        if parsed.path.startswith("/v1/jobs/") and parsed.path.endswith("/cancel"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) != 4:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            cancelled = self.server.store.cancel(parts[2])
            _json_response(self, HTTPStatus.OK, {"cancelled": cancelled})
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})


def _pool_configs(raw: Any, *, default_timeout: int) -> dict[str, PoolConfig]:
    out: dict[str, PoolConfig] = {}
    if not isinstance(raw, dict):
        return out
    for site, data in raw.items():
        if isinstance(data, dict):
            lanes = int(data.get("lanes") or 1)
            timeout = int(data.get("timeout_seconds") or default_timeout)
        else:
            lanes = int(getattr(data, "lanes", 1) or 1)
            timeout = int(getattr(data, "timeout_seconds", default_timeout) or default_timeout)
        out[str(site)] = PoolConfig(lanes=max(1, lanes), timeout_seconds=max(1, timeout))
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openfeed-opencli-service",
        description="Run OpenFeed's local OpenCLI job service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "HTTP API:\n"
            "  GET  /v1/health\n"
            "  GET  /v1/pools\n"
            "  GET  /v1/capabilities[?site=twitter&command=search]\n"
            "  POST /v1/jobs\n"
            "  GET  /v1/jobs/{job_id}\n"
            "  GET  /v1/jobs/{job_id}/result\n\n"
            "POST /v1/jobs fields:\n"
            "  project, site, command, args, profile, priority,\n"
            "  timeout_seconds, idempotency_key\n\n"
            "Example:\n"
            "  curl -X POST http://127.0.0.1:19826/v1/jobs \\\n"
            "    -H 'Content-Type: application/json' \\\n"
            "    -d '{\"project\":\"my-agent\",\"site\":\"twitter\","
            "\"command\":\"search\",\"args\":[\"AI agents\",\"--limit\",\"3\"],"
            "\"timeout_seconds\":180}'"
        ),
    )
    config_env = os.environ.get("OPENFEED_OPENCLI_SERVICE_CONFIG")
    parser.add_argument("--config", type=Path, default=Path(config_env) if config_env else None)
    parser.add_argument("--host", default=os.environ.get("OPENFEED_OPENCLI_SERVICE_HOST"))
    port_env = os.environ.get("OPENFEED_OPENCLI_SERVICE_PORT")
    parser.add_argument("--port", type=int, default=int(port_env) if port_env else None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--workdir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workdir = (args.workdir or Path(os.environ.get("OPENFEED_WORKDIR") or ".")).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    configure_task_logging("opencli_service", log_dir=workdir / "logs")
    config = load_service_config(args.config)
    host = args.host or config.host
    port = args.port or config.port
    db_path = (args.db or (workdir / "state" / "opencli_service.sqlite")).resolve()

    store = JobStore(db_path)
    recovered = store.recover_interrupted_jobs()
    if recovered:
        logger.warning("marked %d interrupted opencli job(s) as failed", recovered)
    default_pool = PoolConfig(
        lanes=config.default_lanes,
        timeout_seconds=config.default_timeout_seconds,
    )
    service = OpenCLIService(
        store=store,
        default_profile=config.default_profile,
        default_pool=default_pool,
        pools=_pool_configs(config.pools, default_timeout=config.default_timeout_seconds),
        poll_seconds=config.poll_seconds,
    )
    service.start()

    server = OpenCLIHTTPServer((host, port), Handler)
    server.store = store
    server.service = service
    server.default_profile = config.default_profile
    server.default_timeout_seconds = config.default_timeout_seconds
    server.opencli_version = _opencli_version()

    stop_requested = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop_requested.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous_sigint = signal.signal(signal.SIGINT, _stop)
    previous_sigterm = signal.signal(signal.SIGTERM, _stop)
    logger.info("opencli service listening on http://%s:%d db=%s", host, port, db_path)
    try:
        server.serve_forever(poll_interval=0.5)
        return 0
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        service.stop()
        store.close()
        server.server_close()
        if stop_requested.is_set():
            logger.info("opencli service stopped")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
