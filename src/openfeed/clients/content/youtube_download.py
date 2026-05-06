"""yt-dlp subprocess wrapper for YouTube video download.

Empirical baseline (re-probed 2026-05-06 against production failures):
- cold-cache `ejs:npm` + node can leave yt-dlp with storyboard-only formats,
  producing "Requested format is not available"
- cold-cache `ejs:github` + node solves the challenge and exposes the full
  DASH ladder
- `tv` exposes 720p H.264/AAC for Shorts; `tv_embedded` is currently reported
  by yt-dlp as unsupported and should not be used as a fallback

Required deps (caller's environment):
- node.js installed and on PATH (we use the `node` JS runtime)
- Chrome with logged-in YouTube account (cookies are read from its profile)

Failure mode: if the exact 720p strategy fails, raises `YouTubeDownloadError`.
If the 720p file exceeds the configured consumer file-size cap, raises
`YouTubeDownloadPermanentError` so the caller can stop retrying. We do not
downshift to 480p/360p because low-resolution video cards are not useful for
this feed.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path


_logger = logging.getLogger("youtube_download")

_TV_CLIENT_ARGS: tuple[str, ...] = (
    "--cookies-from-browser", "chrome",
    "--extractor-args", "youtube:player_client=tv",
)

class YouTubeDownloadError(RuntimeError):
    """The 720p download failed. `tier_errors` lists stderr summaries."""

    def __init__(self, message: str, tier_errors: list[tuple[str, str]]) -> None:
        super().__init__(message)
        self.tier_errors = tier_errors


class YouTubeDownloadPermanentError(YouTubeDownloadError):
    """The video is not suitable for this consumer policy, e.g. still too big."""


def _h264_aac_720_selector(target_height: int) -> str:
    """Return a strict H.264/AAC selector for exactly the target resolution.

    Do not use `height<=720`: vertical 720p Shorts are 720x1280, so height
    filtering would discard the desired format. Exact 720p means either
    landscape height=720 or vertical width=720.
    """
    target = max(1, target_height)
    video = f"bv*[vcodec^=avc1][height={target}]+ba[ext=m4a]"
    vertical_video = f"bv*[vcodec^=avc1][width={target}]+ba[ext=m4a]"
    bundled = f"b[vcodec^=avc1][height={target}]"
    vertical_bundled = f"b[vcodec^=avc1][width={target}]"
    return "/".join((video, vertical_video, bundled, vertical_bundled))


def _too_large(path: Path, max_filesize_mb: int | None) -> bool:
    if max_filesize_mb is None or max_filesize_mb <= 0:
        return False
    return path.stat().st_size > max_filesize_mb * 1024 * 1024


def download(
    video_id: str,
    target_path: Path,
    *,
    max_height: int = 720,
    max_filesize_mb: int | None = None,
    timeout_seconds: int = 180,
) -> Path:
    """Download `video_id` to `target_path` (mp4). Returns the path on success.

    Format strategy:
      `-f` hard-filters to H.264 video (avc1.*) + AAC audio (m4a) — both are
      universally supported (QuickTime, Safari, every browser). VP9/AV1 might
      be smaller but break QuickTime + older mobile players.
      The selector requires exact target resolution in either landscape or
      vertical orientation. We intentionally do not fall back to lower
      resolutions to fit upload limits.
      `--merge-output-format mp4` forces the muxed container to be mp4 even
      when one source stream came in as e.g. webm.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.unlink(missing_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    common = [
        "yt-dlp", "--no-warnings", "--no-progress",
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        "--merge-output-format", "mp4",
        "--socket-timeout", "30",
        "-o", str(target_path),
    ]
    selector = _h264_aac_720_selector(max_height)
    strategy_name = f"h264_aac_exact_{max_height}"
    cmd = (
        common
        + list(_TV_CLIENT_ARGS)
        + ["-f", selector, "-S", f"res:{max_height}", url]
    )
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        err = f"timeout after {elapsed:.0f}s"
        _logger.warning("[%s] strategy=%s %s", video_id, strategy_name, err)
        raise YouTubeDownloadError(
            f"720p download timed out for {video_id}: {err}",
            [(strategy_name, err)],
        )
    elapsed = time.monotonic() - t0
    if r.returncode == 0 and target_path.exists() and target_path.stat().st_size > 0:
        if _too_large(target_path, max_filesize_mb):
            size_mb = target_path.stat().st_size / 1024 / 1024
            err = f"{size_mb:.1f} MB exceeds max_filesize_mb={max_filesize_mb}"
            _logger.warning(
                "[%s] strategy=%s too large in %.1fs: %s",
                video_id, strategy_name, elapsed, err,
            )
            target_path.unlink(missing_ok=True)
            raise YouTubeDownloadPermanentError(
                f"720p file too large for {video_id}: {err}",
                [(strategy_name, err)],
            )
        _logger.info(
            "[%s] strategy=%s ok in %.1fs (%.1f MB)",
            video_id, strategy_name, elapsed, target_path.stat().st_size / 1e6,
        )
        return target_path

    err_line = ""
    if r.stderr:
        for line in reversed(r.stderr.strip().splitlines()):
            if line.strip():
                err_line = line.strip()[:200]
                break
    if not err_line:
        err_line = f"rc={r.returncode}"
    _logger.warning(
        "[%s] strategy=%s failed in %.1fs: %s",
        video_id, strategy_name, elapsed, err_line,
    )
    target_path.unlink(missing_ok=True)
    if "Requested format is not available" in err_line:
        raise YouTubeDownloadPermanentError(
            f"720p H.264/AAC is not available for {video_id}: {err_line}",
            [(strategy_name, err_line)],
        )
    raise YouTubeDownloadError(
        f"720p download failed for {video_id}: {err_line}",
        [(strategy_name, err_line)],
    )
