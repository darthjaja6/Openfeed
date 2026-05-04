"""Ticlawk adapter — HTTPS API client (PRD §4.3 feed_client).

Endpoints map 1:1 to the Ticlawk Publisher API:
  - `push_card(...)`        -> POST /api/cards
  - `get_channel_metrics()` -> GET  /api/channels/:id/metrics
  - `get_channel_changes()` -> GET  /api/channels/:id/changes?since=<cursor>

Auth: publisher bearer token in `TICLAWK_PUBLISHER_API_KEY` (env). Base URL in
`TICLAWK_API_URL` env (default `https://ticlawk.com`).

`channel_id` is **per-topic, NOT env-level**: a single Ticlawk account can
host multiple channels, so the channel id rides on the per-topic
`consumer_config` block in `openfeed.yaml`. The functions that
need it take it as a required arg — there is no env / global default.

Registered with `clients.consumer.CONSUMERS` at module load via
`TiclawkConsumerConfig`; callers reach this through the registry rather
than importing this module directly.

Transport is plain `urllib.request` to avoid pulling in `requests`.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict


_logger = logging.getLogger("ticlawk")

_DEFAULT_TIMEOUT_SECONDS = 15


class TiclawkConsumerConfig(BaseModel):
    """Per-topic consumer config for Ticlawk: just a channel id.

    Validated against the raw `consumer_config` dict in `openfeed.yaml` at
    interests-load time."""
    model_config = ConfigDict(extra="forbid")
    channel_id: str


class TiclawkError(RuntimeError):
    """Any non-2xx from the Ticlawk API, or transport error."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after


class TiclawkRateLimited(TiclawkError):
    """HTTP 429. `retry_after` is an ISO timestamp when the header is usable."""


class TiclawkAuthError(TiclawkError):
    """Authentication/permission failure that needs operator intervention."""


def _api_base() -> str:
    return os.environ.get("TICLAWK_API_URL", "https://ticlawk.com").rstrip("/")


def _api_key() -> str:
    key = os.environ.get("TICLAWK_PUBLISHER_API_KEY", "").strip()
    if not key:
        raise TiclawkAuthError("TICLAWK_PUBLISHER_API_KEY not set")
    return key


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    url = f"{_api_base()}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url=url, data=data, headers=headers, method=method)
    return _do_request(req, method, path, timeout)


def _multipart_request(
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
        filename = file_path.name
        file_bytes = file_path.read_bytes()
        total_size += len(file_bytes)
        chunks.extend([
            f"--{boundary}".encode("ascii"),
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"'
            ).encode("utf-8"),
            f"Content-Type: {content_type}".encode("ascii"),
            b"",
            file_bytes,
        ])

    chunks.append(f"--{boundary}--".encode("ascii"))
    chunks.append(b"")
    data = crlf.join(chunks)
    req = Request(
        url=f"{_api_base()}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(data)),
        },
        method="POST",
    )
    _logger.info(
        "multipart POST %s: %d file(s), %.1f MB",
        path, len(files), total_size / 1e6,
    )
    return _do_request(req, "POST", path, timeout)


def _do_request(
    req: "Request", method: str, path: str, timeout: int,
) -> dict[str, Any]:
    """Shared HTTP execution + error mapping. Split out so the binary-body
    upload path can reuse it without going through the JSON-body builder."""
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            msg = json.loads(raw).get("error") or raw
        except json.JSONDecodeError:
            msg = raw
        retry_after = _parse_retry_after(exc.headers.get("Retry-After"))
        message = f"{method} {path} -> {exc.code}: {msg}"
        if exc.code == 429:
            raise TiclawkRateLimited(
                message, status=exc.code, body=raw, retry_after=retry_after,
            ) from exc
        if exc.code == 401:
            raise TiclawkAuthError(message, status=exc.code, body=raw) from exc
        raise TiclawkError(
            message, status=exc.code, body=raw, retry_after=retry_after,
        ) from exc
    except URLError as exc:
        raise TiclawkError(f"{method} {path} transport error: {exc.reason}") from exc


def _parse_retry_after(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return (
            datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(seconds=int(value))
        ).isoformat()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class TiclawkAssetNotFound(TiclawkError):
    """`POST /api/cards` 404: video_asset_id unknown / not ours / deleted.
    Caller should invalidate the local asset_index entry and re-upload."""


class TiclawkUploadIncomplete(TiclawkError):
    """`POST /api/cards` 409: asset row exists but bytes never arrived
    (PUT failed / signed URL expired). Caller should re-upload."""


class TiclawkAssetTooBig(TiclawkError):
    """`POST /api/cards` 413: actual uploaded bytes exceeded 200 MB cap."""


class TiclawkBadAssetType(TiclawkError):
    """`POST /api/cards` 415: uploaded content-type wasn't video/mp4."""


class TiclawkQuotaExceeded(TiclawkError):
    """`POST /api/upload` or `POST /api/cards` 403: creator asset quota
    exceeded. Caller should run cleanup (DELETE old assets) and retry."""


def push_card(
    *,
    channel_id: str,
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
    """Publish a content card. Returns the created card record (contains `id`).

    `content_subtype`:
      - "html"          → `html` body (web / X cards)
      - "video"         → multipart upload with local `video_path`
      - "gallery"       → multipart upload with local `image_paths`
      - "youtube_video" → legacy native IFrame embed via `video_id`
      - any subtype may include multipart `thumbnail_path`
    """
    body: dict[str, Any] = {
        "channel_id": channel_id,
        "card_type": "content",
        "content_subtype": content_subtype,
        "title": title,
    }
    files: list[tuple[str, Path, str]] = []
    use_multipart = False
    if content_subtype == "html":
        if not html:
            raise TiclawkError("html required for content_subtype=html")
        body["html"] = html
    elif content_subtype == "video":
        if not video_path:
            raise TiclawkError("video_path required for content_subtype=video")
        path = Path(video_path)
        if not path.exists():
            raise TiclawkError(f"video_path does not exist: {video_path}")
        files.append(("video", path, "video/mp4"))
        use_multipart = True
    elif content_subtype == "gallery":
        if not image_paths:
            raise TiclawkError("image_paths required for content_subtype=gallery")
        for image_path in image_paths:
            path = Path(image_path)
            if not path.exists():
                raise TiclawkError(f"image_path does not exist: {image_path}")
            files.append(("images", path, _image_content_type(path)))
        use_multipart = True
    elif content_subtype == "youtube_video":
        if not video_id:
            raise TiclawkError("video_id required for content_subtype=youtube_video")
        body["video_id"] = video_id
    else:
        raise TiclawkError(f"unknown content_subtype: {content_subtype!r}")

    if thumbnail_path:
        path = Path(thumbnail_path)
        if not path.exists():
            raise TiclawkError(f"thumbnail_path does not exist: {thumbnail_path}")
        files.append(("thumbnail", path, _image_content_type(path)))
        use_multipart = True

    try:
        if use_multipart:
            total_size = sum(path.stat().st_size for _, path, _ in files)
            timeout = max(60, int(total_size / (1 * 1024 * 1024)) + 45)
            envelope = _multipart_request(
                "/api/cards",
                fields={k: str(v) for k, v in body.items()},
                files=files,
                timeout=timeout,
            )
        else:
            envelope = _request("POST", "/api/cards", body=body)
    except TiclawkError as exc:
        if exc.status == 413:
            raise TiclawkAssetTooBig(str(exc), status=413, body=exc.body) from exc
        if exc.status == 415:
            raise TiclawkBadAssetType(str(exc), status=415, body=exc.body) from exc
        if exc.status == 403:
            raise TiclawkQuotaExceeded(str(exc), status=403, body=exc.body) from exc
        raise
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict) or "id" not in data:
        raise TiclawkError(f"POST /api/cards unexpected response shape: {envelope!r}")
    return data


def get_channel_metrics(channel_id: str) -> dict[str, Any]:
    """Return channel-level feed health (subscribers + buffer percentiles)."""
    envelope = _request("GET", f"/api/channels/{channel_id}/metrics")
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        raise TiclawkError(f"GET /api/channels/{channel_id}/metrics unexpected shape: {envelope!r}")
    return data


def get_channel_changes(*, channel_id: str, since: str) -> dict[str, Any]:
    """Pull channel-level feedback delta since a cursor.

    Returns one page of changes (server may paginate via `has_more` / `cursor`).
    Caller advances `since` with the returned `cursor` until `has_more` is False.

    Use `since="0"` for the very first call. The endpoint returns `data` with:
      - `cursor`: opaque string to pass back as `since` on next call
      - `has_more`: bool
      - `changes`: list of per-card change records (deltas + current + last_consumed_at)
    """
    envelope = _request("GET", f"/api/channels/{channel_id}/changes?since={since}")
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        raise TiclawkError(f"GET /api/channels/{channel_id}/changes unexpected shape: {envelope!r}")
    return data


def _image_content_type(path: "Path") -> str:
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
    if guessed in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        return guessed
    raise TiclawkError(f"unsupported image type {path}")


def delete_asset(asset_id: str) -> None:
    """DELETE /api/upload/{asset_id}. Idempotent — 404 is silently ignored."""
    req = Request(
        url=f"{_api_base()}/api/upload/{asset_id}",
        headers={"Authorization": f"Bearer {_api_key()}", "Accept": "application/json"},
        method="DELETE",
    )
    try:
        _do_request(req, "DELETE", f"/api/upload/{asset_id}", 30)
    except TiclawkError as exc:
        if exc.status == 404:
            return  # already gone, success
        raise


def delete_video(asset_id: str) -> None:
    delete_asset(asset_id)


def delete_card(card_id: str) -> None:
    """DELETE /api/cards/{card_id}. Idempotent — 404 is silently ignored."""
    req = Request(
        url=f"{_api_base()}/api/cards/{card_id}",
        headers={"Authorization": f"Bearer {_api_key()}", "Accept": "application/json"},
        method="DELETE",
    )
    try:
        _do_request(req, "DELETE", f"/api/cards/{card_id}", 30)
    except TiclawkError as exc:
        if exc.status == 404:
            return
        raise


def get_card_metrics(card_id: str) -> dict[str, Any]:
    """Return single-card engagement (likes/saves/shares/views + dwell/watch)."""
    envelope = _request("GET", f"/api/cards/{card_id}/metrics")
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        raise TiclawkError(f"GET /api/cards/{card_id}/metrics unexpected shape: {envelope!r}")
    return data


# ---------------------------------------------------------------------------
# Consumer-registry adapters: thin wrappers that take a typed
# `TiclawkConsumerConfig` (validated per-topic) and forward to the
# underlying functions above. callers in core/ never touch raw strings.
# ---------------------------------------------------------------------------


def _adapter_push_card(config: TiclawkConsumerConfig, **card_kwargs: Any) -> dict[str, Any]:
    return push_card(channel_id=config.channel_id, **card_kwargs)


def _adapter_get_metrics(config: TiclawkConsumerConfig) -> dict[str, Any]:
    return get_channel_metrics(config.channel_id)


def _adapter_fetch_changes(config: TiclawkConsumerConfig, *, since: str) -> dict[str, Any]:
    return get_channel_changes(channel_id=config.channel_id, since=since)


# Registry self-registration. Imported by `clients/consumer/__init__.py`
# so a `from openfeed.clients.consumer import CONSUMERS` always sees us.
from openfeed.clients.consumer import CONSUMERS, ConsumerSpec  # noqa: E402

CONSUMERS["ticlawk"] = ConsumerSpec(
    config_model=TiclawkConsumerConfig,
    push_card=_adapter_push_card,
    get_metrics=_adapter_get_metrics,
    fetch_changes=_adapter_fetch_changes,
)
