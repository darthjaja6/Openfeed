"""Local web consumer.

This adapter is a zero-account local feed target. `push` writes rendered cards
to `state/local_web/channels/<channel_id>/cards.json`; `openfeed-local-server`
serves that inbox and appends browser interaction events to `events.jsonl`.

The adapter returns the same feedback page shape as Ticlawk's
channel-changes endpoint, so `collect_feedback` and `learn` do not branch.
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import sys
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict

from openfeed.utils.config_files import load_env
from openfeed.utils.state_io import atomic_write_json


_logger = logging.getLogger("local_web")

_STATE_ROOT = Path("state/local_web")
_UPLOAD_ROOT = _STATE_ROOT / "uploads"
_COUNTER_EVENTS = {
    "view": "views",
    "like": "like_count",
    "save": "save_count",
    "share": "share_count",
}
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class LocalWebConsumerConfig(BaseModel):
    """Per-topic local web inbox config."""

    model_config = ConfigDict(extra="forbid")
    channel_id: str = "default"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_channel_id(channel_id: str) -> str:
    cleaned = _SAFE_ID_RE.sub("_", channel_id.strip())
    return cleaned or "default"


def _channel_dir(channel_id: str) -> Path:
    return _STATE_ROOT / "channels" / _safe_channel_id(channel_id)


def _cards_path(channel_id: str) -> Path:
    return _channel_dir(channel_id) / "cards.json"


def _events_path(channel_id: str) -> Path:
    return _channel_dir(channel_id) / "events.jsonl"


def _load_cards(channel_id: str) -> list[dict[str, Any]]:
    path = _cards_path(channel_id)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    cards = raw.get("cards") if isinstance(raw, dict) else None
    return list(cards) if isinstance(cards, list) else []


def _save_cards(channel_id: str, cards: list[dict[str, Any]]) -> None:
    path = _cards_path(channel_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"generated_at": _utc_now_iso(), "cards": cards})


def _store_card(channel_id: str, card: dict[str, Any]) -> dict[str, Any]:
    cards = _load_cards(channel_id)
    cards.insert(0, card)
    _save_cards(channel_id, cards)
    return card


def _read_events(channel_id: str) -> list[dict[str, Any]]:
    path = _events_path(channel_id)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _append_event(channel_id: str, event: dict[str, Any]) -> dict[str, Any]:
    path = _events_path(channel_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = _read_events(channel_id)
    row = {
        "seq": len(prior) + 1,
        "observed_at": _utc_now_iso(),
        **event,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _viewed_card_ids(channel_id: str) -> set[str]:
    return {
        str(e.get("card_id"))
        for e in _read_events(channel_id)
        if e.get("event_type") == "view" and e.get("card_id")
    }


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = (len(ordered) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return float(ordered[lo] * (1 - frac) + ordered[hi] * frac)


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
    del video_asset_id, image_asset_ids
    if content_subtype == "html" and not html:
        raise ValueError("html required for local_web content_subtype=html")
    if content_subtype == "video" and not video_path:
        raise ValueError("video_path required for local_web content_subtype=video")
    if content_subtype == "gallery" and not image_paths:
        raise ValueError("image_paths required for local_web content_subtype=gallery")
    if content_subtype == "youtube_video" and not video_id:
        raise ValueError("video_id required for local_web content_subtype=youtube_video")

    card_id = f"local_{uuid.uuid4().hex}"
    card = {
        "id": card_id,
        "channel_id": channel_id,
        "title": title,
        "content_subtype": content_subtype,
        "html": html,
        "video_id": video_id,
        "video_path": video_path,
        "image_paths": image_paths or [],
        "thumbnail_path": thumbnail_path,
        "pushed_at": _utc_now_iso(),
    }
    return _store_card(channel_id, card)


def get_channel_metrics(channel_id: str) -> dict[str, Any]:
    cards = _load_cards(channel_id)
    viewed = _viewed_card_ids(channel_id)
    unconsumed_total = sum(1 for c in cards if c.get("id") not in viewed)
    return {
        "unconsumed_total": unconsumed_total,
        "card_count": len(cards),
    }


def get_channel_changes(*, channel_id: str, since: str) -> dict[str, Any]:
    try:
        cursor = int(since)
    except (TypeError, ValueError):
        cursor = 0
    events = [e for e in _read_events(channel_id) if int(e.get("seq") or 0) > cursor]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        card_id = event.get("card_id")
        if card_id:
            grouped.setdefault(str(card_id), []).append(event)

    changes: list[dict[str, Any]] = []
    for card_id, card_events in grouped.items():
        deltas = {k: 0 for k in ("like_count", "save_count", "share_count", "views")}
        dwell: list[float] = []
        watch: list[float] = []
        consumed_at: str | None = None
        for event in card_events:
            event_type = str(event.get("event_type") or "")
            counter = _COUNTER_EVENTS.get(event_type)
            if counter:
                deltas[counter] += 1
            if event_type == "dwell":
                dwell.append(float(event.get("dwell_seconds") or 0))
            if event_type == "watch":
                watch.append(float(event.get("watch_progress") or 0))
            if event_type in {"view", "dwell", "watch"}:
                observed_at = str(event.get("observed_at") or "")
                if observed_at and (consumed_at is None or observed_at > consumed_at):
                    consumed_at = observed_at
        changes.append({
            "card_id": card_id,
            "deltas": deltas,
            "current_distribution": {
                "p50_dwell_seconds": _percentile(dwell, 0.5),
                "p90_dwell_seconds": _percentile(dwell, 0.9),
                "p50_watch_progress": _percentile(watch, 0.5),
                "p90_watch_progress": _percentile(watch, 0.9),
            },
            "last_consumed_at": consumed_at,
        })

    next_cursor = max([cursor, *[int(e.get("seq") or 0) for e in events]])
    return {"cursor": str(next_cursor), "has_more": False, "changes": changes}


def _adapter_push_card(config: LocalWebConsumerConfig, **card_kwargs: Any) -> dict[str, Any]:
    return push_card(channel_id=config.channel_id, **card_kwargs)


def _adapter_get_metrics(config: LocalWebConsumerConfig) -> dict[str, Any]:
    return get_channel_metrics(config.channel_id)


def _adapter_fetch_changes(config: LocalWebConsumerConfig, *, since: str) -> dict[str, Any]:
    return get_channel_changes(channel_id=config.channel_id, since=since)


@dataclass(frozen=True)
class _CardRef:
    channel_id: str
    card: dict[str, Any]


def _all_card_refs() -> list[_CardRef]:
    out: list[_CardRef] = []
    channels_root = _STATE_ROOT / "channels"
    if not channels_root.exists():
        return out
    for channel_dir in sorted(channels_root.iterdir()):
        if not channel_dir.is_dir():
            continue
        path = channel_dir / "cards.json"
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        for card in raw.get("cards") or []:
            if isinstance(card, dict):
                out.append(_CardRef(channel_id=str(card.get("channel_id") or channel_dir.name), card=card))
    out.sort(key=lambda ref: str(ref.card.get("pushed_at") or ""), reverse=True)
    return out


def _find_card(card_id: str) -> _CardRef | None:
    for ref in _all_card_refs():
        if ref.card.get("id") == card_id:
            return ref
    return None


def _media_path(card: dict[str, Any], kind: str, index: int) -> Path | None:
    if kind == "video":
        value = card.get("video_path")
        return Path(value) if value else None
    if kind == "thumbnail":
        value = card.get("thumbnail_path")
        return Path(value) if value else None
    if kind == "image":
        paths = card.get("image_paths") or []
        if 0 <= index < len(paths):
            return Path(paths[index])
    return None


def _public_cards() -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for ref in _all_card_refs():
        card = ref.card
        card_id = str(card.get("id") or "")
        image_paths = card.get("image_paths") or []
        cards.append({
            "id": card_id,
            "channel_id": ref.channel_id,
            "title": card.get("title") or "",
            "content_subtype": card.get("content_subtype") or "",
            "html": card.get("html"),
            "video_url": f"/media/{card_id}/video/0" if card.get("video_path") else None,
            "thumbnail_url": f"/media/{card_id}/thumbnail/0" if card.get("thumbnail_path") else None,
            "image_urls": [
                f"/media/{card_id}/image/{i}" for i, _ in enumerate(image_paths)
            ],
            "pushed_at": card.get("pushed_at") or "",
        })
    return cards


class _Handler(BaseHTTPRequestHandler):
    server_version = "OpenFeedLocalWeb/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        _logger.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(HTTPStatus.OK, _page_html(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/cards":
            self._send_json(HTTPStatus.OK, {"cards": _public_cards()})
            return
        metrics_match = re.fullmatch(r"/openfeed/v1/channels/([^/]+)/metrics", parsed.path)
        if metrics_match:
            self._send_json(HTTPStatus.OK, {"data": get_channel_metrics(metrics_match.group(1))})
            return
        changes_match = re.fullmatch(r"/openfeed/v1/channels/([^/]+)/changes", parsed.path)
        if changes_match:
            since = parse_qs(parsed.query).get("since", ["0"])[0]
            self._send_json(
                HTTPStatus.OK,
                {"data": get_channel_changes(channel_id=changes_match.group(1), since=since)},
            )
            return
        if parsed.path.startswith("/media/"):
            self._serve_media(parsed.path)
            return
        self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/openfeed/v1/cards":
            self._handle_protocol_push()
            return
        match = re.fullmatch(r"/api/cards/([^/]+)/events", parsed.path)
        if not match:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        card_id = match.group(1)
        ref = _find_card(card_id)
        if ref is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown card"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return
        event_type = str(payload.get("event_type") or "")
        if event_type not in {"view", "dwell", "watch", "like", "save", "share"}:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid event_type"})
            return
        event = {"card_id": card_id, "event_type": event_type}
        if event_type == "dwell":
            event["dwell_seconds"] = max(0.0, float(payload.get("dwell_seconds") or 0))
        if event_type == "watch":
            progress = float(payload.get("watch_progress") or 0)
            event["watch_progress"] = min(1.0, max(0.0, progress))
        row = _append_event(ref.channel_id, event)
        self._send_json(HTTPStatus.OK, {"ok": True, "seq": row["seq"]})

    def _handle_protocol_push(self) -> None:
        try:
            fields, files = self._read_card_request()
            card = self._card_from_protocol_request(fields, files)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, {"data": card})

    def _read_card_request(self) -> tuple[dict[str, str], dict[str, list[tuple[str, bytes, str]]]]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        content_type = self.headers.get("Content-Type") or ""
        if content_type.startswith("application/json"):
            try:
                payload = json.loads(raw.decode("utf-8") if raw else "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("invalid json") from exc
            return {str(k): str(v) for k, v in payload.items() if v is not None}, {}
        if content_type.startswith("multipart/form-data"):
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
                + raw
            )
            fields: dict[str, str] = {}
            files: dict[str, list[tuple[str, bytes, str]]] = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name:
                    continue
                filename = part.get_filename()
                content = part.get_payload(decode=True) or b""
                if filename:
                    files.setdefault(name, []).append((
                        filename,
                        content,
                        part.get_content_type() or "application/octet-stream",
                    ))
                else:
                    fields[name] = content.decode(part.get_content_charset() or "utf-8")
            return fields, files
        raise ValueError("Content-Type must be application/json or multipart/form-data")

    def _card_from_protocol_request(
        self,
        fields: dict[str, str],
        files: dict[str, list[tuple[str, bytes, str]]],
    ) -> dict[str, Any]:
        channel_id = fields.get("channel_id") or "default"
        title = fields.get("title") or "Untitled"
        content_subtype = fields.get("content_subtype") or ""
        card_id = f"local_{uuid.uuid4().hex}"
        upload_dir = _UPLOAD_ROOT / card_id
        upload_dir.mkdir(parents=True, exist_ok=True)

        def save_file(field_name: str, fallback_name: str) -> str | None:
            values = files.get(field_name) or []
            if not values:
                return None
            filename, data, _ = values[0]
            suffix = Path(filename).suffix or Path(fallback_name).suffix
            path = upload_dir / f"{field_name}{suffix}"
            path.write_bytes(data)
            return str(path)

        def save_many(field_name: str) -> list[str]:
            out: list[str] = []
            for idx, (filename, data, _) in enumerate(files.get(field_name) or []):
                suffix = Path(filename).suffix or ".bin"
                path = upload_dir / f"{field_name}_{idx}{suffix}"
                path.write_bytes(data)
                out.append(str(path))
            return out

        image_paths: list[str] = []
        video_path: str | None = None
        if content_subtype == "html" and not fields.get("html"):
            raise ValueError("html required for content_subtype=html")
        if content_subtype == "video":
            video_path = save_file("video", "video.mp4")
            if not video_path:
                raise ValueError("video file required for content_subtype=video")
        if content_subtype == "gallery":
            image_paths = save_many("images")
            if not image_paths:
                raise ValueError("images files required for content_subtype=gallery")
        if content_subtype == "youtube_video" and not fields.get("video_id"):
            raise ValueError("video_id required for content_subtype=youtube_video")
        thumbnail_path = save_file("thumbnail", "thumbnail.jpg")

        card = {
            "id": card_id,
            "channel_id": channel_id,
            "title": title,
            "content_subtype": content_subtype,
            "html": fields.get("html"),
            "video_id": fields.get("video_id"),
            "video_path": video_path,
            "image_paths": image_paths,
            "thumbnail_path": thumbnail_path,
            "pushed_at": _utc_now_iso(),
        }
        return _store_card(channel_id, card)

    def _serve_media(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        _, card_id, kind, raw_index = parts
        ref = _find_card(card_id)
        if ref is None:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        try:
            index = int(raw_index)
        except ValueError:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        media_path = _media_path(ref.card, kind, index)
        if media_path is None or not media_path.exists() or not media_path.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        content_type, _ = mimetypes.guess_type(str(media_path))
        self._send(HTTPStatus.OK, media_path.read_bytes(), content_type or "application/octet-stream")


def _page_html() -> bytes:
    cards_json = json.dumps(_public_cards(), ensure_ascii=False).replace("</", "<\\/")
    html_body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Openfeed Demo</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #050505;
  --fg: #ffffff;
  --muted: rgba(255, 255, 255, 0.72);
  --shadow: 0 2px 12px rgba(0, 0, 0, 0.55);
  --accent: #fe2c55;
}}
* {{ box-sizing: border-box; }}
html, body {{ width: 100%; height: 100%; overflow: hidden; }}
body {{ margin: 0; font: 15px/1.35 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--fg); }}
header {{ position: fixed; top: 0; left: 50%; z-index: 5; width: min(100vw, 480px); transform: translateX(-50%); padding: 14px 18px; pointer-events: none; }}
h1 {{ margin: 0; color: rgba(255, 255, 255, 0.9); font-size: 16px; font-weight: 760; text-shadow: var(--shadow); }}
main {{ width: min(100vw, 480px); height: 100svh; margin: 0 auto; overflow-y: auto; overflow-x: hidden; scroll-snap-type: y mandatory; scrollbar-width: none; -ms-overflow-style: none; background: #000; overscroll-behavior-y: contain; }}
main::-webkit-scrollbar {{ display: none; }}
.empty {{ min-height: 100svh; display: grid; place-items: center; padding: 28px; color: var(--muted); text-align: center; }}
.card {{ position: relative; height: 100svh; scroll-snap-align: start; scroll-snap-stop: always; overflow: hidden; background: #000; isolation: isolate; }}
.body {{ position: absolute; inset: 0; display: grid; place-items: center; background: #000; }}
.body::after {{ content: ""; position: absolute; inset: auto 0 0; height: 42%; pointer-events: none; background: linear-gradient(to top, rgba(0, 0, 0, 0.72), rgba(0, 0, 0, 0)); }}
.html-frame {{ display: block; width: 100%; height: 100%; border: 0; background: white; }}
.video-wrap {{ position: absolute; inset: 0; display: grid; place-items: center; background: #000; }}
video {{ display: block; width: 100%; height: 100%; object-fit: contain; background: #000; }}
.play-overlay {{ position: absolute; inset: 0; z-index: 2; display: grid; place-items: center; border: 0; padding: 0; background: transparent; color: rgba(255, 255, 255, 0.72); cursor: pointer; transition: opacity 120ms ease; }}
.play-overlay.hidden {{ opacity: 0; pointer-events: none; }}
.play-triangle {{ display: block; width: 0; height: 0; margin-left: 8px; border-top: 34px solid transparent; border-bottom: 34px solid transparent; border-left: 54px solid currentColor; filter: drop-shadow(0 3px 12px rgba(0, 0, 0, 0.72)); }}
.gallery {{ position: absolute; inset: 0; display: grid; grid-template-columns: 46px minmax(0, 1fr) 46px; grid-template-rows: 1fr; place-items: stretch; background: #000; touch-action: pan-y; }}
.gallery img {{ display: block; grid-column: 2; grid-row: 1; width: 100%; height: 100%; object-fit: contain; user-select: none; -webkit-user-drag: none; }}
.gallery-nav {{ position: relative; z-index: 2; width: 100%; height: 100%; border: 0; padding: 0; background: transparent; color: #fff; cursor: pointer; display: grid; place-items: center; opacity: 0.96; }}
.gallery-nav:hover {{ opacity: 1; }}
.gallery-nav:disabled {{ opacity: 0.22; cursor: default; }}
.gallery-prev {{ grid-column: 1; grid-row: 1; }}
.gallery-next {{ grid-column: 3; grid-row: 1; }}
.gallery-nav svg {{ width: 42px; height: 42px; fill: none; stroke: currentColor; stroke-width: 4.2; stroke-linecap: round; stroke-linejoin: round; filter: drop-shadow(0 2px 2px rgba(0, 0, 0, 0.95)) drop-shadow(0 8px 18px rgba(0, 0, 0, 0.85)); }}
.gallery-dots {{ position: absolute; left: 50%; bottom: 114px; z-index: 3; display: flex; max-width: calc(100% - 128px); transform: translateX(-50%); gap: 5px; }}
.gallery-dot {{ width: 6px; height: 6px; border-radius: 999px; background: rgba(255, 255, 255, 0.38); box-shadow: var(--shadow); }}
.gallery-dot.active {{ background: #fff; }}
.caption {{ position: absolute; left: 14px; right: 82px; bottom: max(18px, env(safe-area-inset-bottom)); z-index: 3; color: #fff; text-shadow: var(--shadow); }}
.title {{ display: -webkit-box; margin: 0 0 7px; overflow: hidden; -webkit-line-clamp: 4; -webkit-box-orient: vertical; font-size: 15px; font-weight: 650; line-height: 1.28; }}
.sub {{ color: var(--muted); font-size: 12px; font-weight: 580; }}
.actions {{ position: absolute; right: 12px; bottom: max(24px, env(safe-area-inset-bottom)); z-index: 4; display: grid; gap: 18px; justify-items: center; }}
.action-button {{ appearance: none; width: 54px; height: 58px; border: 0; padding: 0; background: transparent; color: white; cursor: pointer; display: grid; place-items: center; }}
.action-icon {{ display: grid; place-items: center; width: 46px; height: 46px; border-radius: 999px; filter: drop-shadow(0 2px 8px rgba(0, 0, 0, 0.7)); transition: transform 120ms ease; }}
.action-icon svg {{ display: block; width: 38px; height: 38px; stroke: currentColor; stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round; fill: rgba(0, 0, 0, 0.08); }}
.action-button:hover .action-icon {{ transform: scale(1.08); }}
.action-button.active[data-event="like"] .action-icon svg {{ fill: var(--accent); stroke: var(--accent); }}
.action-button.active[data-event="save"] .action-icon svg {{ fill: #f5c518; stroke: #f5c518; }}
@media (min-width: 760px) {{
  body {{ background: radial-gradient(circle at 50% 0%, #202124 0, #050505 42%); }}
  main {{ box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.08), 0 24px 80px rgba(0, 0, 0, 0.55); }}
}}
@media (max-width: 520px) {{
  header {{ width: 100vw; padding-left: 14px; }}
  main {{ width: 100vw; }}
  .gallery {{ grid-template-columns: 42px minmax(0, 1fr) 42px; }}
  .gallery-nav {{ width: 100%; height: 100%; }}
  .gallery-nav svg {{ width: 38px; height: 38px; }}
  .caption {{ left: 12px; right: 78px; }}
  .actions {{ right: 8px; }}
}}
</style>
</head>
<body>
<header><h1>Openfeed Demo</h1></header>
<main id="app"></main>
<script>
const cards = {cards_json};
const app = document.getElementById("app");
const active = new Map();
const viewed = new Set();
const sentActions = new Set();

function send(id, event_type, data = {{}}) {{
  const body = JSON.stringify({{event_type, ...data}});
  if (navigator.sendBeacon && event_type === "dwell") {{
    navigator.sendBeacon(`/api/cards/${{id}}/events`, new Blob([body], {{type: "application/json"}}));
    return;
  }}
  fetch(`/api/cards/${{id}}/events`, {{method: "POST", headers: {{"Content-Type": "application/json"}}, body, keepalive: true}}).catch(() => {{}});
}}

function leave(id) {{
  const start = active.get(id);
  if (!start) return;
  active.delete(id);
  const dwell = (Date.now() - start) / 1000;
  if (dwell >= 1) send(id, "dwell", {{dwell_seconds: dwell}});
}}

function pauseVideos(root) {{
  root.querySelectorAll("video").forEach(video => {{
    if (!video.paused) video.pause();
  }});
}}

function renderCard(card) {{
  const el = document.createElement("article");
  el.className = "card";
  el.dataset.cardId = card.id;
  const body = document.createElement("section");
  body.className = "body";
  if (card.content_subtype === "html") {{
    const frame = document.createElement("iframe");
    frame.className = "html-frame";
    frame.sandbox = "allow-popups allow-popups-to-escape-sandbox allow-same-origin";
    frame.srcdoc = card.html || "";
    body.appendChild(frame);
  }} else if (card.content_subtype === "video") {{
    const wrap = document.createElement("div");
    wrap.className = "video-wrap";
    const video = document.createElement("video");
    video.controls = true;
    video.playsInline = true;
    video.src = card.video_url;
    const play = document.createElement("button");
    play.type = "button";
    play.className = "play-overlay";
    play.setAttribute("aria-label", "Play");
    play.innerHTML = '<span class="play-triangle"></span>';
    const syncPlayState = () => play.classList.toggle("hidden", !video.paused && !video.ended);
    const togglePlay = () => {{
      if (video.paused || video.ended) {{
        video.play().catch(() => {{}});
      }} else {{
        video.pause();
      }}
    }};
    play.addEventListener("click", togglePlay);
    video.addEventListener("click", togglePlay);
    video.addEventListener("play", syncPlayState);
    video.addEventListener("pause", () => trackWatch(card.id, video));
    video.addEventListener("pause", syncPlayState);
    video.addEventListener("ended", () => trackWatch(card.id, video));
    video.addEventListener("ended", syncPlayState);
    wrap.append(video, play);
    body.appendChild(wrap);
  }} else if (card.content_subtype === "gallery") {{
    body.appendChild(renderGallery(card.image_urls || []));
  }} else {{
    body.textContent = "Unsupported card subtype.";
  }}
  const caption = document.createElement("section");
  caption.className = "caption";
  caption.innerHTML = `<h2 class="title"></h2><div class="sub"></div>`;
  caption.querySelector(".title").textContent = card.title || "Untitled";
  caption.querySelector(".sub").textContent = `@${{card.channel_id}}`;
  const actions = document.createElement("section");
  actions.className = "actions";
  for (const [label, event] of [["Like", "like"], ["Save", "save"]]) {{
    const button = document.createElement("button");
    button.type = "button";
    button.className = "action-button";
    button.dataset.event = event;
    button.setAttribute("aria-label", label);
    button.innerHTML = `<span class="action-icon">${{iconSvg(event)}}</span>`;
    button.addEventListener("click", () => {{
      const key = `${{card.id}}:${{event}}`;
      if (sentActions.has(key)) return;
      sentActions.add(key);
      button.classList.add("active");
      send(card.id, event);
    }});
    actions.appendChild(button);
  }}
  el.append(body, caption, actions);
  return el;
}}

function renderGallery(urls) {{
  const gallery = document.createElement("div");
  gallery.className = "gallery";
  const img = document.createElement("img");
  img.loading = "eager";
  img.alt = "";
  gallery.appendChild(img);

  const prev = document.createElement("button");
  prev.type = "button";
  prev.className = "gallery-nav gallery-prev";
  prev.setAttribute("aria-label", "Previous image");
  prev.innerHTML = arrowSvg("left");

  const next = document.createElement("button");
  next.type = "button";
  next.className = "gallery-nav gallery-next";
  next.setAttribute("aria-label", "Next image");
  next.innerHTML = arrowSvg("right");

  const dots = document.createElement("div");
  dots.className = "gallery-dots";
  const dotEls = urls.map(() => {{
    const dot = document.createElement("span");
    dot.className = "gallery-dot";
    dots.appendChild(dot);
    return dot;
  }});

  let index = 0;
  function show(nextIndex) {{
    if (!urls.length) return;
    index = Math.max(0, Math.min(urls.length - 1, nextIndex));
    img.src = urls[index];
    prev.disabled = index === 0;
    next.disabled = index === urls.length - 1;
    dotEls.forEach((dot, i) => dot.classList.toggle("active", i === index));
  }}

  prev.addEventListener("click", event => {{
    event.stopPropagation();
    show(index - 1);
  }});
  next.addEventListener("click", event => {{
    event.stopPropagation();
    show(index + 1);
  }});

  let startX = null;
  gallery.addEventListener("pointerdown", event => {{ startX = event.clientX; }});
  gallery.addEventListener("pointerup", event => {{
    if (startX === null) return;
    const dx = event.clientX - startX;
    startX = null;
    if (Math.abs(dx) < 42) return;
    show(index + (dx < 0 ? 1 : -1));
  }});

  if (urls.length > 1) {{
    gallery.append(prev, next, dots);
  }}
  show(0);
  return gallery;
}}

function arrowSvg(direction) {{
  const d = direction === "left" ? "M15 5l-7 7 7 7" : "M9 5l7 7-7 7";
  return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${{d}}"/></svg>`;
}}

function iconSvg(event) {{
  if (event === "like") {{
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.8 4.6c-2-2-5.2-1.9-7.1.2L12 6.6l-1.7-1.8C8.4 2.7 5.2 2.6 3.2 4.6 1 6.8 1 10.4 3.3 12.7L12 21l8.7-8.3c2.3-2.3 2.3-5.9.1-8.1z"/></svg>';
  }}
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 3.5h11A1.5 1.5 0 0 1 19 5v16l-7-4-7 4V5a1.5 1.5 0 0 1 1.5-1.5z"/></svg>';
}}

function trackWatch(id, video) {{
  if (!video.duration || !Number.isFinite(video.duration)) return;
  send(id, "watch", {{watch_progress: Math.min(1, video.currentTime / video.duration)}});
}}

if (!cards.length) {{
  app.innerHTML = '<div class="empty">No local cards yet. Run supply, prepare if needed, then refill.</div>';
}} else {{
  for (const card of cards) app.appendChild(renderCard(card));
}}

const observer = new IntersectionObserver(entries => {{
  for (const entry of entries) {{
    const id = entry.target.dataset.cardId;
    if (!id) continue;
    if (entry.isIntersecting) {{
      active.set(id, Date.now());
      if (!viewed.has(id)) {{
        viewed.add(id);
        send(id, "view");
      }}
    }} else {{
      pauseVideos(entry.target);
      leave(id);
    }}
  }}
}}, {{threshold: 0.6}});

document.querySelectorAll(".card").forEach(el => observer.observe(el));
window.addEventListener("beforeunload", () => {{
  for (const id of Array.from(active.keys())) leave(id);
}});
</script>
</body>
</html>"""
    return html_body.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openfeed-local-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="open the browser after starting")
    args = parser.parse_args(argv)

    load_env(Path.cwd())
    workdir = Path(os.environ.get("OPENFEED_WORKDIR") or ("output" if Path("output").exists() else "."))
    workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(workdir)
    try:
        server = ThreadingHTTPServer((args.host, args.port), _Handler)
    except OSError as exc:
        print(
            f"openfeed local web server could not bind {args.host}:{args.port}: {exc}. "
            "Fix: stop the process already using that port, then rerun `./run-openfeed start`.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    url = f"http://{args.host}:{args.port}/"
    print(f"openfeed local web server: {url} (workdir: {Path.cwd()})", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


from openfeed.clients.consumer import CONSUMERS, ConsumerSpec  # noqa: E402

CONSUMERS["local_web"] = ConsumerSpec(
    config_model=LocalWebConsumerConfig,
    push_card=_adapter_push_card,
    get_metrics=_adapter_get_metrics,
    fetch_changes=_adapter_fetch_changes,
)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
