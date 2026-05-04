"""Generic HTTP consumer adapter.

This is the no-code-extension path for custom feed clients. A user can set
`consumer_type: http` and point OpenFeed at any service that implements the
OpenFeed consumer protocol, or at an existing service with compatible endpoint
paths.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field


_logger = logging.getLogger("http_consumer")


class HttpConsumerEndpoints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    push_card: str = "/openfeed/v1/cards"
    get_metrics: str = "/openfeed/v1/channels/{channel_id}/metrics"
    fetch_changes: str = "/openfeed/v1/channels/{channel_id}/changes"


class HttpConsumerConfig(BaseModel):
    """Per-topic config for any OpenFeed-compatible HTTP consumer."""

    model_config = ConfigDict(extra="forbid")
    base_url: str
    channel_id: str
    api_key_env: str | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    timeout_seconds: int = Field(default=30, ge=1)
    endpoints: HttpConsumerEndpoints = Field(default_factory=HttpConsumerEndpoints)


class HttpConsumerError(RuntimeError):
    """Any non-2xx response, malformed response, or transport failure."""


def _base_url(config: HttpConsumerConfig) -> str:
    return config.base_url.rstrip("/")


def _headers(config: HttpConsumerConfig, *, content_type: str | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    if config.api_key_env:
        key = os.environ.get(config.api_key_env, "").strip()
        if not key:
            raise HttpConsumerError(f"{config.api_key_env} not set")
        headers[config.auth_header] = f"{config.auth_scheme} {key}".strip()
    return headers


def _format_endpoint(template: str, *, channel_id: str, since: str | None = None) -> str:
    values = {"channel_id": channel_id, "since": since or ""}
    path = template.format(**values)
    if since is not None and "{since}" not in template:
        separator = "&" if "?" in path else "?"
        path = f"{path}{separator}{urlencode({'since': since})}"
    return path


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _request_json(
    config: HttpConsumerConfig,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(
        url=f"{_base_url(config)}{path}",
        data=data,
        headers=_headers(config, content_type="application/json" if data is not None else None),
        method=method,
    )
    return _do_request(req, method, path, timeout or config.timeout_seconds)


def _multipart_request(
    config: HttpConsumerConfig,
    path: str,
    *,
    fields: dict[str, str],
    files: list[tuple[str, Path, str]],
    timeout: int,
) -> dict[str, Any]:
    boundary = f"----openfeed-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    crlf = b"\r\n"
    total_size = 0

    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}".encode("ascii"),
            f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"),
            b"",
            value.encode("utf-8"),
        ])

    for field_name, file_path, content_type in files:
        file_bytes = file_path.read_bytes()
        total_size += len(file_bytes)
        chunks.extend([
            f"--{boundary}".encode("ascii"),
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{file_path.name}"'
            ).encode("utf-8"),
            f"Content-Type: {content_type}".encode("ascii"),
            b"",
            file_bytes,
        ])

    chunks.append(f"--{boundary}--".encode("ascii"))
    chunks.append(b"")
    data = crlf.join(chunks)
    req = Request(
        url=f"{_base_url(config)}{path}",
        data=data,
        headers={
            **_headers(config),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(data)),
        },
        method="POST",
    )
    _logger.info("multipart POST %s: %d file(s), %.1f MB", path, len(files), total_size / 1e6)
    return _do_request(req, "POST", path, timeout)


def _do_request(req: Request, method: str, path: str, timeout: int) -> dict[str, Any]:
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise HttpConsumerError(f"{method} {path} -> {exc.code}: {raw}") from exc
    except URLError as exc:
        raise HttpConsumerError(f"{method} {path} transport error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise HttpConsumerError(f"{method} {path} returned non-json response") from exc


def _image_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed:
        return guessed
    return "application/octet-stream"


def _card_body(channel_id: str, title: str, content_subtype: str) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "card_type": "content",
        "content_subtype": content_subtype,
        "title": title,
    }


def push_card(
    config: HttpConsumerConfig,
    *,
    title: str,
    content_subtype: str,
    html: str | None = None,
    video_id: str | None = None,
    video_asset_id: str | None = None,
    image_asset_ids: list[str] | None = None,
    video_path: str | None = None,
    image_paths: list[str] | None = None,
    thumbnail_path: str | None = None,
) -> dict[str, Any]:
    path = _format_endpoint(config.endpoints.push_card, channel_id=config.channel_id)
    body = _card_body(config.channel_id, title, content_subtype)
    files: list[tuple[str, Path, str]] = []

    if content_subtype == "html":
        if not html:
            raise HttpConsumerError("html required for content_subtype=html")
        body["html"] = html
    elif content_subtype == "video":
        if video_asset_id:
            body["video_asset_id"] = video_asset_id
        elif video_path:
            path_obj = Path(video_path)
            if not path_obj.exists():
                raise HttpConsumerError(f"video_path does not exist: {video_path}")
            files.append(("video", path_obj, "video/mp4"))
        else:
            raise HttpConsumerError("video_path or video_asset_id required for content_subtype=video")
    elif content_subtype == "gallery":
        if image_asset_ids:
            body["image_asset_ids"] = json.dumps(image_asset_ids)
        elif image_paths:
            for image_path in image_paths:
                path_obj = Path(image_path)
                if not path_obj.exists():
                    raise HttpConsumerError(f"image_path does not exist: {image_path}")
                files.append(("images", path_obj, _image_content_type(path_obj)))
        else:
            raise HttpConsumerError("image_paths or image_asset_ids required for content_subtype=gallery")
    elif content_subtype == "youtube_video":
        if not video_id:
            raise HttpConsumerError("video_id required for content_subtype=youtube_video")
        body["video_id"] = video_id
    else:
        raise HttpConsumerError(f"unknown content_subtype: {content_subtype!r}")

    if thumbnail_path:
        path_obj = Path(thumbnail_path)
        if not path_obj.exists():
            raise HttpConsumerError(f"thumbnail_path does not exist: {thumbnail_path}")
        files.append(("thumbnail", path_obj, _image_content_type(path_obj)))

    if files:
        total_size = sum(path.stat().st_size for _, path, _ in files)
        timeout = max(config.timeout_seconds, 60, int(total_size / (1024 * 1024)) + 45)
        response = _multipart_request(
            config,
            path,
            fields={k: str(v) for k, v in body.items()},
            files=files,
            timeout=timeout,
        )
    else:
        response = _request_json(config, "POST", path, body=body)
    data = _unwrap(response)
    if "id" not in data:
        raise HttpConsumerError(f"POST {path} response missing id: {response!r}")
    return data


def get_metrics(config: HttpConsumerConfig) -> dict[str, Any]:
    path = _format_endpoint(config.endpoints.get_metrics, channel_id=config.channel_id)
    return _unwrap(_request_json(config, "GET", path))


def fetch_changes(config: HttpConsumerConfig, *, since: str) -> dict[str, Any]:
    path = _format_endpoint(config.endpoints.fetch_changes, channel_id=config.channel_id, since=since)
    data = _unwrap(_request_json(config, "GET", path))
    if "cursor" not in data or "changes" not in data:
        raise HttpConsumerError(f"GET {path} response missing cursor/changes: {data!r}")
    data.setdefault("has_more", False)
    return data


from openfeed.clients.consumer import CONSUMERS, ConsumerSpec  # noqa: E402

CONSUMERS["http"] = ConsumerSpec(
    config_model=HttpConsumerConfig,
    push_card=push_card,
    get_metrics=get_metrics,
    fetch_changes=fetch_changes,
)
