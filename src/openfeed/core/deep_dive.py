"""Stage-1 perception for keyword-proposal: get real video frames for a
positive content item, then run `content_deep_diving` on them.

Frame source priority:
  1. `state/video_cache/{content_id}.mp4` if push has already downloaded it
     (free — just ffmpeg-extract).
  2. Else `yt-dlp` low-res mp4 to a tmp file, ffmpeg-extract, delete tmp.

Frame extraction: N evenly-spaced timestamps via `ffmpeg -vf fps=N/dur`.

Extracted frames are cached under `logs/positive_frames/{content_id}/frame_*.jpg`
so a re-run of the same learn tick (or a re-fire after a crash) skips the
yt-dlp + ffmpeg work entirely.

Today this module is YouTube-only — non-YouTube platforms return empty
frames and the caller falls back to title-only deep-dive.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from openfeed.clients.llm import GeminiRunner, LLMClientError
from openfeed.models.persona import PersonaOutput
from openfeed.prompts.content_deep_diving import (
    ContentDeepDiving, build_content_deep_diving_prompt,
)


_logger = logging.getLogger("deep_dive")

_VIDEO_CACHE = Path("state/video_cache")
_FRAME_CACHE = Path("logs/positive_frames")


def _ffmpeg_extract_frames(
    src_mp4: Path, out_dir: Path, frame_count: int,
) -> list[Path]:
    """Extract `frame_count` evenly-spaced frames from `src_mp4` into
    `out_dir/frame_NN.jpg`. Returns the list of written paths (sorted)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # ffprobe duration
    r = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration", "-of", "csv=p=0", str(src_mp4)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        duration = float(r.stdout.strip())
    except ValueError:
        _logger.warning("ffprobe failed for %s: %s", src_mp4, r.stderr.strip()[:200])
        return []
    if duration <= 0:
        return []
    fps = f"{frame_count}/{duration}"
    out_pattern = str(out_dir / "frame_%02d.jpg")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(src_mp4), "-vf", f"fps={fps}",
         "-frames:v", str(frame_count), out_pattern],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        _logger.warning("ffmpeg failed for %s: %s", src_mp4, r.stderr.strip()[:200])
        return []
    return sorted(out_dir.glob("frame_*.jpg"))


_YT_DLP_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # `tv` client unlocks the full DASH ladder (matches youtube_download.py
    # tier choice). mweb/web/android are silently capped by YouTube at
    # format 18 (360p only) as of 2026-04-26.
    ("tv+cookies", (
        "--cookies-from-browser", "chrome",
        "--extractor-args", "youtube:player_client=tv",
    )),
    ("tv_embedded+cookies", (
        "--cookies-from-browser", "chrome",
        "--extractor-args", "youtube:player_client=tv_embedded",
    )),
)


def _yt_dlp_lowres(video_id: str, target_path: Path, max_height: int) -> bool:
    """Pull video-only stream (≤max_height) to target_path. No audio — frames
    only. Returns True on success."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.unlink(missing_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    common = [
        "yt-dlp", "--no-warnings", "--no-progress",
        "--js-runtimes", "node",
        "--remote-components", "ejs:npm",
        "-f", f"wv*[height<={max_height}]/wv*",
        "--socket-timeout", "30",
        "-o", str(target_path),
    ]
    for tier_name, tier_args in _YT_DLP_TIERS:
        cmd = common + list(tier_args) + [url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            _logger.warning("[%s] yt-dlp tier=%s timeout", video_id, tier_name)
            continue
        if r.returncode == 0 and target_path.exists() and target_path.stat().st_size > 0:
            _logger.info(
                "[%s] yt-dlp tier=%s ok (%.1f KB)",
                video_id, tier_name, target_path.stat().st_size / 1024,
            )
            return True
        last_err = ""
        if r.stderr:
            for line in reversed(r.stderr.strip().splitlines()):
                if line.strip():
                    last_err = line.strip()[:200]
                    break
        _logger.warning("[%s] yt-dlp tier=%s rc=%d %s",
                        video_id, tier_name, r.returncode, last_err)
    return False


def get_frames_for_youtube(
    content_id: str, *, max_height: int, frame_count: int,
) -> list[bytes]:
    """Resolve frames for a YouTube `content_id`. Cached after first call.
    Returns [] on any failure; caller should fall back to title-only."""
    out_dir = _FRAME_CACHE / content_id
    if out_dir.exists():
        cached = sorted(out_dir.glob("frame_*.jpg"))
        if cached:
            return [p.read_bytes() for p in cached]

    # Path 1: push already downloaded the mp4 — reuse.
    cached_mp4 = _VIDEO_CACHE / f"{content_id}.mp4"
    if cached_mp4.exists() and cached_mp4.stat().st_size > 0:
        frames = _ffmpeg_extract_frames(cached_mp4, out_dir, frame_count)
        if frames:
            return [p.read_bytes() for p in frames]

    # Path 2: yt-dlp low-res to tmp, extract, drop tmp.
    with tempfile.TemporaryDirectory(prefix="deep_dive_") as td:
        tmp_path = Path(td) / f"{content_id}.mp4"
        if not _yt_dlp_lowres(content_id, tmp_path, max_height):
            return []
        # yt-dlp may have written .webm or .mkv depending on stream; find it.
        produced = next((p for p in Path(td).iterdir() if p.is_file()), None)
        if produced is None or produced.stat().st_size == 0:
            return []
        frames = _ffmpeg_extract_frames(produced, out_dir, frame_count)
        if not frames:
            shutil.rmtree(out_dir, ignore_errors=True)
            return []
        return [p.read_bytes() for p in frames]


def deep_dive_one(
    *,
    content_id: str,
    platform: str,
    title: str,
    topic: str,
    topic_description: str,
    persona: PersonaOutput,
    runner: GeminiRunner,
    max_height: int,
    frame_count: int,
) -> ContentDeepDiving | None:
    """Run Stage-1 deep-dive on one positive item. Returns None on any
    failure (caller logs and degrades to title-only at Stage 2)."""
    if platform == "youtube":
        frame_bytes = get_frames_for_youtube(
            content_id, max_height=max_height, frame_count=frame_count,
        )
    else:
        # Non-youtube: no frame source today; deep-dive runs text-only.
        frame_bytes = []

    text = f"Title: {title}"
    try:
        raw = runner.run_json(
            build_content_deep_diving_prompt(
                text=text, topic=topic, topic_description=topic_description,
                persona=persona, images=frame_bytes or None,
            ),
            schema=ContentDeepDiving.model_json_schema(),
            schema_name=ContentDeepDiving.__name__,
        )
        return ContentDeepDiving.model_validate(raw)
    except (LLMClientError, Exception) as exc:  # noqa: BLE001
        _logger.warning("deep_dive_one failed for %s: %s", content_id, exc)
        return None
