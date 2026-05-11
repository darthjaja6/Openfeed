"""Client wrapper for OpenCLI-backed content adapters.

OpenFeed does not invoke the `opencli` binary directly. Browser-backed work is
submitted to the local OpenCLI service, which owns the job queue, per-site
worker pools, and tab/lane resource coordination for this machine.

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

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from openfeed.opencli_service import client as opencli_service

logger = logging.getLogger("opencli")


@dataclass(frozen=True)
class OpenCLIFailure:
    kind: str
    job_id: str | None = None
    site: str | None = None
    command: str | None = None
    returncode: int | None = None
    status: str | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "failure_kind": self.kind,
            "job_id": self.job_id,
            "site": self.site,
            "command": self.command,
            "returncode": self.returncode,
            "status": self.status,
        }


class OpenCLIError(RuntimeError):
    """Any opencli failure — per-source errors (handle doesn't exist, 404, etc)
    and infrastructure errors (browser bridge down, daemon crashed) share this
    base. Callers that want to distinguish check for `OpenCLIInfraError`."""

    def __init__(self, message: str, *, failure: OpenCLIFailure | None = None) -> None:
        super().__init__(message)
        self.failure = failure or OpenCLIFailure(kind="permanent")

    def evidence(self) -> dict[str, Any]:
        return {"error": str(self)[:500], **self.failure.as_evidence()}


class OpenCLIInfraError(OpenCLIError):
    """Infrastructure failure — the opencli toolchain itself is unavailable
    (Browser Bridge extension not connected, Chrome not running, daemon
    unavailable, etc). Caller should abort-and-retry-later rather than skip
    and move on, because every subsequent call will fail the same way."""
    pass


class OpenCLITransientError(OpenCLIError):
    """Transient OpenCLI/browser/adapter failure for one job.

    These failures should not be attributed to the source or candidate being
    fetched. Typical examples are stale tab leases and malformed adapter output
    under browser concurrency.
    """
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
_TRANSIENT_CODE_MARKERS = (
    "No tab with given id",
    "stale page",
    "stale page identity",
    "Target page, context or browser has been closed",
    "Unexpected end of JSON input",
    "opencli returned non-JSON stdout",
    "Execution context was destroyed",
    "Navigation failed because page was closed",
)


def _is_infra_failure(returncode: int, payload: str) -> bool:
    if returncode in _INFRA_EXIT_CODES:
        return True
    return any(marker in payload for marker in _INFRA_CODE_MARKERS)


def _is_transient_failure(payload: str) -> bool:
    return any(marker in payload for marker in _TRANSIENT_CODE_MARKERS)


def _failure(
    *,
    kind: str,
    job_id: str | None,
    site: str | None,
    command: str | None,
    returncode: Any,
    status: str | None,
) -> OpenCLIFailure:
    code = returncode if isinstance(returncode, int) else None
    return OpenCLIFailure(
        kind=kind,
        job_id=job_id,
        site=site,
        command=command,
        returncode=code,
        status=status,
    )


def run(args: list[str], *, timeout: int = 120, retries: int = 1) -> Any:
    """Run an OpenCLI adapter through the local OpenCLI service."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return _run_once(args, timeout=timeout)
        except OpenCLIInfraError:
            raise  # don't burn retries on infra failures
        except OpenCLIError as exc:
            last_exc = exc
            if attempt < retries:
                logger.warning(
                    "opencli retry kind=%s job_id=%s after %ss: %s",
                    exc.failure.kind,
                    exc.failure.job_id,
                    2 * (attempt + 1),
                    exc,
                )
                time.sleep(2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _run_once(args: list[str], *, timeout: int) -> Any:
    if len(args) < 2:
        raise OpenCLIError("opencli args must include site and command")
    site, command, *command_args = args
    try:
        response = opencli_service.submit_job(
            project="openfeed",
            site=site,
            command=command,
            args=command_args,
            timeout_seconds=timeout,
        )
        job = response.get("job") or {}
        job_id = str(job.get("id") or "")
        if not job_id:
            raise OpenCLIError(
                f"opencli service did not return a job id: {response}",
                failure=_failure(
                    kind="infra",
                    job_id=None,
                    site=site,
                    command=command,
                    returncode=None,
                    status=None,
                ),
            )
        result = opencli_service.wait_for_job(job_id)
    except opencli_service.OpenCLIServiceError as exc:
        raise OpenCLIInfraError(
            str(exc),
            failure=_failure(
                kind="infra",
                job_id=None,
                site=site,
                command=command,
                returncode=None,
                status=None,
            ),
        ) from exc

    job_id = str(result.get("id") or job_id)
    status = str(result.get("status") or "")
    result_site = str(result.get("site") or site)
    result_command = str(result.get("command") or command)
    stdout = str(result.get("stdout") or "").strip()
    stderr = str(result.get("stderr") or "").strip()
    returncode = result.get("returncode")
    if status != "succeeded":
        payload = str(result.get("error") or stderr[:300] or stdout[:300])
        msg = (
            f"opencli {' '.join(args)} job_id={job_id} status={status} "
            f"returncode={returncode}: {payload}"
        )
        code = int(returncode) if isinstance(returncode, int) else 1
        combined = stderr + stdout + payload
        if _is_infra_failure(code, combined):
            raise OpenCLIInfraError(
                msg,
                failure=_failure(
                    kind="infra",
                    job_id=job_id,
                    site=result_site,
                    command=result_command,
                    returncode=returncode,
                    status=status,
                ),
            )
        if _is_transient_failure(combined):
            raise OpenCLITransientError(
                msg,
                failure=_failure(
                    kind="transient",
                    job_id=job_id,
                    site=result_site,
                    command=result_command,
                    returncode=returncode,
                    status=status,
                ),
            )
        raise OpenCLIError(
            msg,
            failure=_failure(
                kind="permanent",
                job_id=job_id,
                site=result_site,
                command=result_command,
                returncode=returncode,
                status=status,
            ),
        )
    parsed = result.get("result")
    if parsed is None:
        if not stdout:
            raise OpenCLITransientError(
                f"opencli {' '.join(args)} job_id={job_id} returned empty stdout",
                failure=_failure(
                    kind="transient",
                    job_id=job_id,
                    site=result_site,
                    command=result_command,
                    returncode=returncode,
                    status=status,
                ),
            )
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise OpenCLITransientError(
                f"opencli {' '.join(args)} job_id={job_id} non-JSON: {stdout[:300]!r}",
                failure=_failure(
                    kind="transient",
                    job_id=job_id,
                    site=result_site,
                    command=result_command,
                    returncode=returncode,
                    status=status,
                ),
            ) from exc
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        err = parsed.get("error") or {}
        err_code = str(err.get("code", ""))
        err_msg = str(err.get("message") or err)
        msg = f"opencli {' '.join(args)} job_id={job_id} error: {err_msg}"
        if err_code.startswith("BROWSER_") or err_code.startswith("BRIDGE_"):
            raise OpenCLIInfraError(
                msg,
                failure=_failure(
                    kind="infra",
                    job_id=job_id,
                    site=result_site,
                    command=result_command,
                    returncode=returncode,
                    status=status,
                ),
            )
        if _is_transient_failure(err_msg):
            raise OpenCLITransientError(
                msg,
                failure=_failure(
                    kind="transient",
                    job_id=job_id,
                    site=result_site,
                    command=result_command,
                    returncode=returncode,
                    status=status,
                ),
            )
        raise OpenCLIError(
            msg,
            failure=_failure(
                kind="permanent",
                job_id=job_id,
                site=result_site,
                command=result_command,
                returncode=returncode,
                status=status,
            ),
        )
    return parsed


def ping() -> None:
    """Preflight health check against the local OpenCLI service."""
    try:
        opencli_service.health()
    except opencli_service.OpenCLIServiceError as exc:
        raise OpenCLIInfraError(str(exc)) from exc
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
