"""YouTube renderer — returns a native Ticlawk video card from cached mp4.

Background: ticlawk's webview can't reliably play YouTube IFrames (YouTube's
"Sign in to confirm you're not a bot" challenge keeps triggering). Workaround:
publisher downloads the video locally (`prepare_video` task), and `push`
sends that file directly as multipart `video` to Ticlawk's Publisher API.

Render contract:
  - Local mp4 not ready yet → return None. push.py skips this card;
    `prepare_video` will try to download it next supply tick.
  - Local mp4 ready → return a payload with `video_path`.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from openfeed.card_producers.base import CardPayload, RenderContext
from openfeed.models.content_item import ContentItem
from openfeed.models.video_cache import VideoCacheIndex


_logger = logging.getLogger("producer.ticlawk.youtube")

_YT_VIDEO_ID_PAT = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")

_CACHE_INDEX_PATH = Path("state/video_cache_index.json")


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_cache_index() -> VideoCacheIndex:
    if not _CACHE_INDEX_PATH.exists():
        return VideoCacheIndex(generated_at=_utc_now_iso(), videos={})
    return VideoCacheIndex.model_validate(
        json.loads(_CACHE_INDEX_PATH.read_text(encoding="utf-8"))
    )


class YouTubeRenderer:
    def render(self, item: ContentItem, ctx: RenderContext) -> CardPayload | None:
        if item.youtube is None:
            return None
        # The catalog stores the video id as content_id directly (not URL).
        video_id = item.content_id
        if not _YT_VIDEO_ID_PAT.fullmatch(video_id):
            # If patrol stored a URL instead, parse it out.
            m = _YT_VIDEO_ID_PAT.search(item.youtube.url or "")
            if not m:
                _logger.warning("yt content %s missing parseable video_id", item.content_id)
                return None
            video_id = m.group(1)

        # Check that prepare_video has dropped the mp4 on disk.
        cache_idx = _load_cache_index()
        cache_entry = cache_idx.videos.get(video_id)
        if cache_entry is None or cache_entry.state != "ready" or not cache_entry.local_path:
            _logger.info(
                "yt %s not yet ready in video_cache (state=%s) — skipping",
                video_id, cache_entry.state if cache_entry else "missing",
            )
            return None
        local_path = Path(cache_entry.local_path)
        if not local_path.exists():
            _logger.warning(
                "yt %s cache index says ready but file missing at %s",
                video_id, local_path,
            )
            return None

        title = (item.youtube.title or video_id).strip()
        return CardPayload(
            title=title,
            content_subtype="video",
            video_path=str(local_path),
        )
