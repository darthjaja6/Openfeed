"""yt-dlp subprocess wrapper for YouTube video download.

Empirical baseline (probed 2026-04-23 across 10 videos):
- 10/10 success with `--js-runtimes node --remote-components ejs:npm
  --cookies-from-browser chrome --extractor-args youtube:player_client=web`
- 5-15s/video typical, file sizes 13-160 MB at 720p
- `tv` client without cookies: 0/10 (bot challenge)
- `android` client: yt-dlp warns it doesn't accept cookies; useless

Why we keep a tier chain: YouTube periodically changes player config, breaking
one extractor for hours. Having `ios` as fallback gives us survivability.

Required deps (caller's environment):
- node.js installed and on PATH (we use the `node` JS runtime)
- pip-installed `yt-dlp-ejs` package (provides JS challenge solver)
- Chrome with logged-in YouTube account (cookies are read from its profile)

Failure mode: any tier raising / returning non-zero is logged; the chain
continues to next tier. If all tiers fail, raises `YouTubeDownloadError`
with concatenated stderr summaries — caller decides whether to fall back
to a thumbnail-only card or skip the item.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path


_logger = logging.getLogger("youtube_download")

# Tier definition: (name, extra args appended to the common base).
# `mweb` (mobile web) gives access to YouTube's full DASH format ladder up to
# 1080p+. `web` only exposes <=360p (YouTube saves CDN bandwidth assuming the
# browser will adaptively stream). `ios` is the survivability fallback —
# YouTube periodically breaks one extractor for hours.
_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # `tv` client unlocks the full DASH ladder (up to 4K). `mweb` and `web`
    # are silently restricted by YouTube to format 18 (360p only) — verified
    # 2026-04-26 with `yt-dlp -F` showing only sb*+18 vs tv showing the
    # full 144p–4K ladder. `tv_embedded` is the same shape; kept as fallback.
    ("tv+cookies", (
        "--cookies-from-browser", "chrome",
        "--extractor-args", "youtube:player_client=tv",
    )),
    ("tv_embedded+cookies", (
        "--cookies-from-browser", "chrome",
        "--extractor-args", "youtube:player_client=tv_embedded",
    )),
)


class YouTubeDownloadError(RuntimeError):
    """All download tiers failed. `tier_errors` lists per-tier last-line stderr."""

    def __init__(self, message: str, tier_errors: list[tuple[str, str]]) -> None:
        super().__init__(message)
        self.tier_errors = tier_errors


def download(
    video_id: str,
    target_path: Path,
    *,
    max_height: int = 720,
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
        "--remote-components", "ejs:npm",
        "-f", "bv*[vcodec^=avc1]+ba[ext=m4a]/b[vcodec^=avc1]",
        "-S", f"res:{max_height}",
        "--merge-output-format", "mp4",
        "--socket-timeout", "30",
        "-o", str(target_path),
    ]
    tier_errors: list[tuple[str, str]] = []
    for tier_name, tier_args in _TIERS:
        cmd = common + list(tier_args) + [url]
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
            _logger.info(
                "[%s] tier=%s ok in %.1fs (%.1f MB)",
                video_id, tier_name, elapsed, target_path.stat().st_size / 1e6,
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
            "[%s] tier=%s failed in %.1fs: %s",
            video_id, tier_name, elapsed, err_line,
        )
        tier_errors.append((tier_name, err_line))
        # Clean up partial file before next tier attempt.
        target_path.unlink(missing_ok=True)

    msg = "; ".join(f"{n}: {e}" for n, e in tier_errors)
    raise YouTubeDownloadError(
        f"all tiers failed for {video_id}: {msg}", tier_errors,
    )
