"""Thin subprocess wrapper around the `opencli` CLI.

opencli drives a user-logged-in Chrome session via a browser extension; calling
it from here gives us real-login-state fetches that bypass most bot detection
and paywalls, plus purpose-built adapters for Twitter/X, YouTube, Reddit,
Hacker News, Substack, Bilibili, etc.

opencli backs all calls onto a single Chrome instance, so concurrent requests
to the same platform race on tab reuse + risk anti-bot detection. We serialize
per platform via `_platform_lock` (threading.Lock for in-process + fcntl.flock
for cross-process). Different platforms run in parallel with their own locks.

Three output shapes:

  - `youtube search`, `twitter search`, `hackernews top`, `reddit search`:
      list[dict] with real columns (rank, title, url, …)
  - `youtube channel`, `youtube video`, `twitter profile`:
      list[{"field": str, "value": str}] — a flattened key/value display
      form. Channel responses also embed a `---` separator before an
      inline "recent videos" block.
  - error envelopes: `{"ok": false, "error": {...}}`
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger("opencli")

# Per-platform locks coordinate access to opencli's single Chrome instance.
# In-process: threading.Lock per platform → at most one of this process's
# worker threads runs an opencli call for that platform at a time.
# Cross-process: fcntl.flock on a per-platform lock file → at most one PROCESS
# holds the lock for that platform. Both layers are needed: threading.Lock
# alone doesn't span processes; fcntl.flock alone is per-fd on Linux so
# different threads of one process could each open the file and double up.
_PLATFORM_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_LOCK_DIR = Path(os.environ.get("TMPDIR") or "/tmp")


def _get_thread_lock(platform: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lock = _PLATFORM_THREAD_LOCKS.get(platform)
        if lock is None:
            lock = threading.Lock()
            _PLATFORM_THREAD_LOCKS[platform] = lock
    return lock


@contextmanager
def _platform_lock(platform: str):
    """Hold both the in-process thread lock AND the cross-process file lock
    for `platform`. Different platforms hold independent locks → run in
    parallel. Same-platform callers (any thread, any process) serialize."""
    thread_lock = _get_thread_lock(platform)
    lock_path = _LOCK_DIR / f"openfeed-opencli-{platform}.lock"
    with thread_lock:
        # Open afresh per acquire so closing the fd in the finally clause is
        # the unambiguous release path.
        fd = open(lock_path, "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                fd.close()


class OpenCLIError(RuntimeError):
    """Any opencli failure — per-source errors (handle doesn't exist, 404, etc)
    and infrastructure errors (browser bridge down, daemon crashed) share this
    base. Callers that want to distinguish check for `OpenCLIInfraError`."""
    pass


class OpenCLIInfraError(OpenCLIError):
    """Infrastructure failure — the opencli toolchain itself is unavailable
    (Browser Bridge extension not connected, Chrome not running, daemon
    timeout, etc). Caller should abort-and-retry-later rather than skip and
    move on, because every subsequent call will fail the same way."""
    pass


# Exit 69 is BSD EX_UNAVAILABLE — opencli uses it for infra failures.
# Text-level markers catch the case when exit code gets normalised.
_INFRA_EXIT_CODES = {69}
_INFRA_CODE_MARKERS = (
    "code: BROWSER_CONNECT",
    "code: BROWSER_TIMEOUT",
    "code: BRIDGE_",
    "Browser Bridge extension not connected",
    "Make sure Chrome",
)


def _is_infra_failure(returncode: int, payload: str) -> bool:
    if returncode in _INFRA_EXIT_CODES:
        return True
    return any(marker in payload for marker in _INFRA_CODE_MARKERS)


def run(args: list[str], *, timeout: int = 120, retries: int = 1) -> Any:
    """Run `opencli <args...> --format json`, return parsed JSON stdout.

    Concurrency-capped via a module-level semaphore; retries once after a short
    backoff on transient per-source errors. Infra failures short-circuit
    (no retry — if Chrome is down, retrying in 2s won't help).
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return _run_once(args, timeout=timeout)
        except OpenCLIInfraError:
            raise  # don't burn retries on infra failures
        except OpenCLIError as exc:
            last_exc = exc
            if attempt < retries:
                logger.debug("opencli retry after %s: %s", 2 * (attempt + 1), exc)
                time.sleep(2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _run_once(args: list[str], *, timeout: int) -> Any:
    """Run one opencli call under the per-platform lock. The lock spans both
    threads (in-process) and processes (cross-process via flock), so all
    callers serialize per-platform. Different platforms run independently."""
    full_args = ["opencli", *args, "--format", "json"]
    platform = args[0] if args else "_default"
    with _platform_lock(platform):
        try:
            result = subprocess.run(
                full_args, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise OpenCLIInfraError(
                f"opencli {' '.join(args)} timed out after {timeout}s"
            ) from exc
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        payload = stderr[:300] or stdout[:300]
        msg = f"opencli {' '.join(args)} exit={result.returncode}: {payload}"
        if _is_infra_failure(result.returncode, stderr + stdout):
            raise OpenCLIInfraError(msg)
        raise OpenCLIError(msg)
    if not stdout:
        raise OpenCLIError(f"opencli {' '.join(args)} returned empty stdout")
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise OpenCLIError(f"opencli {' '.join(args)} non-JSON: {stdout[:300]!r}") from exc
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        err = parsed.get("error") or {}
        err_code = str(err.get("code", ""))
        err_msg = str(err.get("message") or err)
        msg = f"opencli {' '.join(args)} error: {err_msg}"
        if err_code.startswith("BROWSER_") or err_code.startswith("BRIDGE_"):
            raise OpenCLIInfraError(msg)
        raise OpenCLIError(msg)
    return parsed


def ping() -> None:
    """Preflight health check — raises `OpenCLIInfraError` if the browser
    bridge / daemon isn't reachable. Cheap: just asks for opencli's version."""
    try:
        subprocess.run(
            ["opencli", "--version"],
            capture_output=True, text=True, timeout=10,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpenCLIInfraError("opencli --version timed out — daemon hung?") from exc
    except subprocess.CalledProcessError as exc:
        raise OpenCLIInfraError(
            f"opencli --version exit={exc.returncode}: {(exc.stderr or exc.stdout or '')[:200]}"
        ) from exc
    except FileNotFoundError as exc:
        raise OpenCLIInfraError("opencli binary not found in PATH") from exc
    # Now test the actual Chrome bridge with a trivial query. In remote desktop
    # VPS sessions X/Twitter can take >20s to wake a cold tab, so keep this
    # timeout configurable and conservative.
    ping_timeout = int(
        os.environ.get("OPENFEED_OPENCLI_PING_TIMEOUT")
        or os.environ.get("OPENCLI_BROWSER_COMMAND_TIMEOUT")
        or "60"
    )
    try:
        run(["twitter", "profile", "jack"], timeout=ping_timeout, retries=0)
    except OpenCLIInfraError:
        raise
    except OpenCLIError:
        # Per-source error on jack's profile is still OK — it means the bridge
        # is reachable even if the payload was unusual.
        logger.debug("opencli ping: profile lookup non-infra error; treating as OK")


def flatten_kv(result: Any) -> dict[str, str]:
    """Flatten a list[{field,value}] response into a plain dict (metadata part only)."""
    if not isinstance(result, list):
        raise OpenCLIError(f"expected list-of-kv, got {type(result).__name__}")
    out: dict[str, str] = {}
    for item in result:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field", ""))
        value = item.get("value", "")
        if field == "---" or field.startswith("---"):
            # Rest of the payload is a secondary section (e.g. recent videos).
            break
        out[field] = value if isinstance(value, str) else str(value)
    return out


def split_after_separator(result: Any) -> list[dict[str, str]]:
    """Return the rows that appear after the `---` separator as [{field,value}]."""
    if not isinstance(result, list):
        return []
    rows: list[dict[str, str]] = []
    seen_sep = False
    for item in result:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field", ""))
        value = item.get("value", "")
        if not seen_sep:
            if field == "---" or field.startswith("---"):
                seen_sep = True
            continue
        rows.append({"field": field, "value": value if isinstance(value, str) else str(value)})
    return rows


# --- Twitter / X ------------------------------------------------------------


def twitter_profile(username: str, *, timeout: int = 60) -> dict[str, Any]:
    """Profile shape: {bio, name, screen_name, followers, following, tweets,
    likes, location, url, verified, created_at}."""
    result = run(["twitter", "profile", username.lstrip("@")], timeout=timeout)
    if isinstance(result, list) and result:
        return result[0] if isinstance(result[0], dict) else {}
    return {}


def twitter_user_timeline(username: str, *, limit: int = 10, timeout: int = 60) -> list[dict[str, Any]]:
    """Get a user's recent tweets via `search from:username`. Returns list of
    {id, author, text, created_at, likes, views, url}."""
    result = run(
        ["twitter", "search", f"from:{username.lstrip('@')}", "--limit", str(limit)],
        timeout=timeout,
    )
    return result if isinstance(result, list) else []


def twitter_search(query: str, *, limit: int = 10, timeout: int = 60) -> list[dict[str, Any]]:
    """Free-form keyword search across X. Each result: {id, author, text,
    created_at, likes, retweets, views, url}. Used by discover to seed
    candidate authors."""
    result = run(["twitter", "search", query, "--limit", str(limit)], timeout=timeout)
    return result if isinstance(result, list) else []


# --- YouTube ----------------------------------------------------------------


def youtube_search(
    query: str, *, limit: int = 10, timeout: int = 60, type: str | None = None,
) -> list[dict[str, Any]]:
    """Search YouTube videos. Each result has {rank, title, channel, duration,
    views, published, url}.

    `type`: opencli `--type` filter — one of `shorts`, `video`, `channel`,
    `playlist`. None (default) leaves it empty so YouTube returns its native
    mixed ranking.
    """
    args = ["youtube", "search", query, "--limit", str(limit)]
    if type:
        args.extend(["--type", type])
    result = run(args, timeout=timeout)
    return result if isinstance(result, list) else []


def youtube_video(url: str, *, timeout: int = 60) -> dict[str, str]:
    """Fetch video metadata — includes channelId, channel, description,
    duration, keywords, likes, publishDate, subscribers, title, views, etc."""
    result = run(["youtube", "video", url], timeout=timeout)
    return flatten_kv(result)


# --- Google search ----------------------------------------------------------


def google_search(query: str, *, limit: int = 10, lang: str = "en", timeout: int = 60) -> list[dict[str, Any]]:
    """Google search via opencli (browser strategy, no API key). Each result:
    {type, title, url, snippet}. `lang` accepts short codes like "en" / "zh"."""
    result = run(
        ["google", "search", query, "--limit", str(limit), "--lang", lang],
        timeout=timeout,
    )
    return result if isinstance(result, list) else []


def youtube_channel(
    channel_id: str, *, limit: int = 10, timeout: int = 60, type: str | None = None,
) -> dict[str, Any]:
    """Fetch channel info + recent uploads. Returns
    {metadata: {name, handle, subscribers, description, keywords, ...},
     recent_videos: [{title, duration, views, published, url}]}.

    `type`: opencli `--type` filter. Default (None) reads Home tab + Videos
    fallback. `"shorts"` reads the Shorts tab and emits items shaped like
    {title, duration: 'SHORT', views, url: '.../shorts/<id>'} (no `published`
    since the Shorts shelf doesn't surface upload dates).
    """
    args = ["youtube", "channel", channel_id, "--limit", str(limit)]
    if type:
        args.extend(["--type", type])
    result = run(args, timeout=timeout)
    metadata = flatten_kv(result)
    recent = []
    for row in split_after_separator(result):
        title = row["field"].strip()
        value = row["value"].strip()
        parts = [p.strip() for p in value.split("|")]
        # Default channel pull emits 4-part rows (duration | views | published | url).
        # `--type shorts` emits 3-part rows (duration | views | url) since the
        # Shorts shelf doesn't carry upload dates. Identify the URL by prefix
        # and only treat the rest as duration/views/published in order.
        url = ""
        non_url = []
        for p in parts:
            if not url and p.startswith("http"):
                url = p
            else:
                non_url.append(p)
        video: dict[str, str] = {"title": title, "url": url}
        if len(non_url) >= 1:
            video["duration"] = non_url[0]
        if len(non_url) >= 2:
            video["views"] = non_url[1]
        if len(non_url) >= 3:
            video["published"] = non_url[2]
        recent.append(video)
    return {"metadata": metadata, "recent_videos": recent}


# --- TikTok -----------------------------------------------------------------


def tiktok_search(query: str, *, limit: int = 10, timeout: int = 90) -> list[dict[str, Any]]:
    """Search TikTok videos. Each result currently includes fields like
    {rank, desc, author, url, plays, likes, comments, shares}.

    Production uses this only for creator discovery. Creator patrol and video
    metadata intentionally go through yt-dlp, not `opencli tiktok user`.
    """
    result = run(["tiktok", "search", query, "--limit", str(limit)], timeout=timeout)
    return result if isinstance(result, list) else []


def tiktok_profile(username: str, *, timeout: int = 90) -> dict[str, Any]:
    """Fetch TikTok profile metadata via opencli.

    Expected fields include {username, name, followers, following, likes,
    videos, verified, bio}. Shape is normalized to one dict.
    """
    result = run(["tiktok", "profile", username.lstrip("@")], timeout=timeout)
    if isinstance(result, list) and result:
        return result[0] if isinstance(result[0], dict) else {}
    return result if isinstance(result, dict) else {}
