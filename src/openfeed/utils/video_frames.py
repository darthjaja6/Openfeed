"""Video frame helpers — YouTube scene thumbnails via i.ytimg.com.

We use YouTube's public thumbnail CDN (`i.ytimg.com/vi/<vid>/<variant>.jpg`)
instead of `yt-dlp + ffmpeg`. The CDN exposes the creator-designed main
thumbnail plus YouTube's own scene samples at ~1/4, 2/4, 3/4 of the video.
Fixed URLs, no auth, no bot check, no rate limit, no client-side JS.

Variants fetched (ordered most→least distinct):
  - `hqdefault.jpg`     — creator-designed main thumbnail (480×360)
  - `hq1.jpg`           — scene sample at ~1/4
  - `hq2.jpg`           — scene sample at ~2/4
  - `hq3.jpg`           — scene sample at ~3/4
  - `maxresdefault.jpg` — 1280×720+ HD version of main thumbnail

Normal videos yield 5 unique frames. Shorts sometimes reuse the same
thumbnail across hq1/hq2/hq3 and may lack maxresdefault — we dedup by
content hash and tolerate 404s, so Shorts typically yield 3-5 frames.

Cached under `logs/video_frames/<video_id>/frame_{i}.jpg` keyed by
video_id; repeat calls skip the HTTP fetch entirely.
"""
from __future__ import annotations

import hashlib
import logging
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("video_frames")

CACHE_DIR = Path("logs/video_frames")

# Ordered so the most-distinct visual (main thumbnail) comes first; dedup
# below preserves this order.
_THUMBNAIL_VARIANTS = (
    "hqdefault.jpg",
    "hq1.jpg",
    "hq2.jpg",
    "hq3.jpg",
    "maxresdefault.jpg",
)

_YTIMG_URL = "https://i.ytimg.com/vi/{vid}/{variant}"
_UA = "Mozilla/5.0"
_TIMEOUT = 10

_VIDEO_ID_PAT = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")


# ---------------------------------------------------------------------------
# Small parsing helpers used across the pipeline
# ---------------------------------------------------------------------------


def video_id_from_url(url: str) -> str | None:
    m = _VIDEO_ID_PAT.search(url or "")
    return m.group(1) if m else None


def parse_views(s: str) -> int:
    """Parse '845,629 views' or '22.2K subscribers' or '1.3M' to int.
    Returns 0 on failure."""
    s = (s or "").lower().strip()
    for suffix in ("subscribers", "subscriber", "views", "view"):
        s = s.replace(suffix, "")
    s = s.strip().replace(",", "")
    if not s:
        return 0
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("b"):
        mult, s = 1_000_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def parse_duration_seconds(s: str) -> int:
    """Parse a YouTube duration string to seconds. Returns 0 on failure.

    Accepted shapes (opencli is inconsistent across endpoints):
      - 'H:MM:SS' / 'MM:SS' / 'M:SS'  (clock-style)
      - '432s'                         (already-seconds with 's' suffix)
      - '432'                          (already-seconds, no suffix)
    """
    raw = (s or "").strip()
    if not raw:
        return 0
    # 'NNNs' / 'NNN' — already in seconds
    if ":" not in raw:
        body = raw[:-1] if raw.endswith("s") or raw.endswith("S") else raw
        try:
            return max(0, int(body))
        except ValueError:
            return 0
    parts = raw.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        pass
    return 0


# ---------------------------------------------------------------------------
# i.ytimg.com thumbnail fetch
# ---------------------------------------------------------------------------


def _fetch_one(vid: str, variant: str) -> bytes | None:
    """HTTP GET a single thumbnail variant. Returns bytes on 200,
    None on 404 or any network error."""
    url = _YTIMG_URL.format(vid=vid, variant=variant)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310
            return r.read()
    except Exception as exc:  # noqa: BLE001 — 404, timeout, DNS all equivalent here
        logger.debug("ytimg fetch failed %s: %s", url, exc)
        return None


def extract_youtube_frames(video_url: str, duration_seconds: int = 0) -> list[Path]:
    """Return up to 5 thumbnail frame paths for the given YouTube URL.

    Fetches `hqdefault` + 3 scene samples + `maxresdefault` in parallel
    from `i.ytimg.com`, dedups identical images (Shorts sometimes reuse
    the same file across hq1/hq2/hq3), tolerates individual 404s, and
    caches results under `logs/video_frames/<video_id>/frame_N.jpg`.

    Normal videos: typically 5 frames. Shorts: typically 3-5.
    Returns an empty list only if the video_id is unparseable or all
    five variants fail.

    `duration_seconds` is kept in the signature for compatibility but is
    ignored — YouTube's CDN picks the scene positions itself."""
    del duration_seconds  # kept for ABI compatibility; unused
    video_id = video_id_from_url(video_url)
    if not video_id:
        return []

    out_dir = CACHE_DIR / video_id
    if out_dir.exists():
        cached = sorted(out_dir.glob("frame_*.jpg"))
        if cached:
            return cached

    with ThreadPoolExecutor(max_workers=len(_THUMBNAIL_VARIANTS)) as pool:
        raw = list(pool.map(lambda v: _fetch_one(video_id, v), _THUMBNAIL_VARIANTS))

    # Dedup by content hash, preserving variant order (hqdefault first).
    seen: set[str] = set()
    unique: list[bytes] = []
    for data in raw:
        if data is None:
            continue
        h = hashlib.md5(data).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        unique.append(data)
    if not unique:
        logger.warning("no thumbnails available for %s", video_id)
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, data in enumerate(unique):
        fp = out_dir / f"frame_{i}.jpg"
        fp.write_bytes(data)
        paths.append(fp)
    return paths
