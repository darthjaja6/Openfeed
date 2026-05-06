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

Failure mode: any strategy returning non-zero is logged; the chain continues
to the next strategy. If all strategies fail, raises `YouTubeDownloadError`.
If every available strategy exceeds the configured consumer file-size cap,
raises `YouTubeDownloadPermanentError` so the caller can stop retrying.
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

_H264_AAC_SELECTOR = "bv*[vcodec^=avc1]+ba[ext=m4a]/b[vcodec^=avc1]"
_PROGRESSIVE_360_SELECTOR = "18/b[height<=360][ext=mp4]"


class YouTubeDownloadError(RuntimeError):
    """All download strategies failed. `tier_errors` lists per-strategy stderr."""

    def __init__(self, message: str, tier_errors: list[tuple[str, str]]) -> None:
        super().__init__(message)
        self.tier_errors = tier_errors


class YouTubeDownloadPermanentError(YouTubeDownloadError):
    """The video is not suitable for this consumer policy, e.g. still too big."""


def _format_strategies(target_height: int) -> tuple[tuple[str, str, int], ...]:
    """Return (strategy_name, yt-dlp format selector, target_res) attempts.

    Do not encode `height<=720` in the selector: vertical 720p Shorts are
    720x1280, so height filtering would discard the desired format. `-S res:N`
    correctly picks the representation closest to N for both landscape and
    vertical videos.
    """
    primary = max(1, target_height)
    out: list[tuple[str, str, int]] = [
        (f"h264_aac_res{primary}", _H264_AAC_SELECTOR, primary),
    ]
    if primary > 480:
        out.append(("h264_aac_res480", _H264_AAC_SELECTOR, 480))
    out.append(("progressive_360", _PROGRESSIVE_360_SELECTOR, 360))
    return tuple(out)


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
      `-S "res:N"` then sorts the matching pool by resolution-closest-to-N.
      For vertical videos yt-dlp's `res` accounts for both dimensions, so a
      vertical "720p" (720x1280) is picked correctly even though height=1280.
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
    tier_errors: list[tuple[str, str]] = []
    too_large_errors: list[tuple[str, str]] = []
    for strategy_name, selector, sort_res in _format_strategies(max_height):
        target_path.unlink(missing_ok=True)
        cmd = (
            common
            + list(_TV_CLIENT_ARGS)
            + ["-f", selector, "-S", f"res:{sort_res}", url]
        )
        t0 = time.monotonic()
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            err = f"timeout after {elapsed:.0f}s"
            _logger.warning("[%s] tier=%s %s", video_id, tier_name, err)
            tier_errors.append((tier_name, err))
            continue
        elapsed = time.monotonic() - t0
        if r.returncode == 0 and target_path.exists() and target_path.stat().st_size > 0:
            if _too_large(target_path, max_filesize_mb):
                size_mb = target_path.stat().st_size / 1024 / 1024
                err = f"{size_mb:.1f} MB exceeds max_filesize_mb={max_filesize_mb}"
                _logger.warning(
                    "[%s] strategy=%s too large in %.1fs: %s",
                    video_id, strategy_name, elapsed, err,
                )
                too_large_errors.append((strategy_name, err))
                target_path.unlink(missing_ok=True)
                continue
            _logger.info(
                "[%s] strategy=%s ok in %.1fs (%.1f MB)",
                video_id, strategy_name, elapsed, target_path.stat().st_size / 1e6,
            )
            return target_path
        # Capture last meaningful stderr line.
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
        tier_errors.append((strategy_name, err_line))
        # Clean up partial file before next tier attempt.
        target_path.unlink(missing_ok=True)

    if too_large_errors and not tier_errors:
        msg = "; ".join(f"{n}: {e}" for n, e in too_large_errors)
        raise YouTubeDownloadPermanentError(
            f"all size fallbacks too large for {video_id}: {msg}",
            too_large_errors,
        )
    msg = "; ".join(f"{n}: {e}" for n, e in tier_errors)
    if too_large_errors:
        size_msg = "; ".join(f"{n}: {e}" for n, e in too_large_errors)
        msg = f"{msg}; size rejects: {size_msg}" if msg else f"size rejects: {size_msg}"
    raise YouTubeDownloadError(
        f"all strategies failed for {video_id}: {msg}", tier_errors + too_large_errors,
    )
