"""Patrol — pull new content from every active source in the catalog.

Per PRD §5.2: iterate `source_catalog.json` active entries, call the
platform-specific puller, dedup by content_id against the existing
`queues/patrol/` directory, write one JSON file per new item.

  - YouTube: `opencli youtube channel <id>` recent_videos
  - X:       `opencli twitter search from:<handle>` (twitter_user_timeline)
  - Web:     `feedparser.parse(feed_url)` (all web sources are RSS/Atom URLs
             after the bootstrap→feed upgrade)

Current MVP shape:
  - Every active source is patrolled every run (no per-source cadence tiers
    yet — runtime config has the hook, we'll layer cadence on later)
  - Dedup = filename-based; content_id baked into filename
  - Failures emit a `patrol_failed` event to `ledgers/decisions.jsonl`;
    no watchlist escalation yet (PRD §5.2 defers that to learn + retire)
  - `source.last_patrolled_at` is updated whether or not new items surfaced

File naming: `{YYYYMMDDTHHMMSS}_{platform}_{content_id_sanitised}.json`
Web content_ids can be full URLs; we SHA-256 them for the filesystem and
keep the original content_id inside the file payload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser
from openfeed.utils.config_files import load_env
from tqdm import tqdm

from openfeed.clients.content import opencli
from openfeed.clients.content import tiktok as tiktok_client
from openfeed.clients.content.opencli import OpenCLIError, OpenCLIInfraError
from openfeed.core.judgment_ledger import attach_file as _attach_ledger, emit_judgment
from openfeed.models.content_item import (
    ContentItem,
    TikTokDigest,
    WebDigest,
    XDigest,
    YouTubeDigest,
)
from openfeed.models.interests import InterestEntry, load_interests
from openfeed.utils.content_meta import (
    content_age_days,
    load_content_blocklist,
    resolve_topic_temporal,
)
from openfeed.models.queue import QueueStatus
from openfeed.models.runtime import RuntimeConfig, load_runtime
from openfeed.models.source import SourceCatalog, SourceEntry
from openfeed.utils import backpressure, catalog_io, cycle_summary
from openfeed.utils.logging_setup import configure_task_logging
from openfeed.utils.state_io import atomic_write_json


logger = logging.getLogger("patrol")

_QUEUE_DIR = Path("queues/patrol")
_LEDGER_PATH = Path("ledgers/decisions.jsonl")
_CATALOG_PATH = Path("state/source_catalog.json")
_QUEUE_STATUS_PATH = Path("state/queue_status.json")


def _load_queue_status() -> QueueStatus | None:
    """Read queue_manage's signal file. None = cold start (no file yet)."""
    if not _QUEUE_STATUS_PATH.exists():
        return None
    try:
        return QueueStatus.model_validate(
            json.loads(_QUEUE_STATUS_PATH.read_text(encoding="utf-8"))
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("queue_status.json malformed; run queue_manage") from exc

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_FILENAME_ID_PAT = re.compile(r"[^A-Za-z0-9_-]")
_YT_VIDEO_ID_PAT = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")


def _configure_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler)
               and getattr(h, "stream", None) is sys.stdout
               for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(sh)
    configure_task_logging("patrol")
    _attach_ledger(_LEDGER_PATH)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fetched_at_stamp() -> str:
    """Compact timestamp for filenames: YYYYMMDDTHHMMSS (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _filesafe_id(content_id: str) -> str:
    """Sanitise content_id for use in a filename.

    YouTube video_ids and X post ids are already filesafe. Web content_ids
    are URLs / GUIDs which aren't — for those we fall back to a stable hash."""
    if _FILENAME_ID_PAT.search(content_id):
        return hashlib.sha256(content_id.encode("utf-8")).hexdigest()[:16]
    return content_id


def _queue_filename(fetched_at: str, platform: str, content_id: str) -> str:
    return f"{fetched_at}_{platform}_{_filesafe_id(content_id)}.json"


def _scan_existing_content_ids() -> set[str]:
    """Pre-load all content_ids already present in queues/patrol/ so we don't
    re-enqueue the same item twice. Loads each file since filename uses a
    sanitised id that isn't directly comparable for web URLs."""
    out: set[str] = set()
    if not _QUEUE_DIR.exists():
        return out
    for fp in _QUEUE_DIR.glob("*.json"):
        try:
            rec = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — corrupt file doesn't stop startup
            continue
        cid = rec.get("content_id")
        if isinstance(cid, str):
            out.add(cid)
    return out


def _load_catalog() -> SourceCatalog:
    state_dir = Path("state")
    if not catalog_io.catalog_dir(state_dir).exists():
        raise SystemExit(
            f"{catalog_io.catalog_dir(state_dir)} missing — run interest-bootstrap first"
        )
    return catalog_io.load_catalog(state_dir)


def _save_catalog(catalog: SourceCatalog, topics: set[str]) -> None:
    if not topics:
        return
    state_dir = Path("state")
    for topic in sorted(topics):
        scoped = {k: v for k, v in catalog.sources.items() if v.topic == topic}
        catalog_io.save_catalog_topic(state_dir, topic, scoped)


def _write_queue_item(item: ContentItem) -> Path:
    _QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    fname = _queue_filename(item.fetched_at, item.platform, item.content_id)
    path = _QUEUE_DIR / fname
    atomic_write_json(path, item.model_dump(exclude_none=True))
    return path


def _extract_video_id(url: str) -> str | None:
    match = _YT_VIDEO_ID_PAT.search(url or "")
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Per-platform patrollers — return list[ContentItem]
# ---------------------------------------------------------------------------


def _filter_by_age(item: ContentItem, max_age_days: int | None) -> bool:
    """True iff the item is within the topic's max age (or age unknown).

    Unparseable dates are kept (lenient — filter will deal); `None` max
    means no age gate at all."""
    if max_age_days is None:
        return True
    age = content_age_days(item)
    if age is None:
        return True
    return age <= max_age_days


def _tiktok_age_days(media: tiktok_client.TikTokVideoMetadata) -> float | None:
    now = datetime.now(timezone.utc)
    if media.timestamp is not None:
        try:
            dt = datetime.fromtimestamp(media.timestamp, tz=timezone.utc)
            return (now - dt).total_seconds() / 86400.0
        except Exception:  # noqa: BLE001
            return None
    if media.upload_date:
        try:
            dt = datetime.strptime(media.upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
            return (now - dt).total_seconds() / 86400.0
        except ValueError:
            return None
    return None


def _tiktok_is_stale(
    media: tiktok_client.TikTokVideoMetadata,
    max_age_days: int | None,
) -> bool:
    if max_age_days is None:
        return False
    age = _tiktok_age_days(media)
    return age is not None and age > max_age_days


def _tiktok_page_past_freshness(
    page: list[tiktok_client.TikTokVideoMetadata],
    max_age_days: int | None,
) -> bool:
    if not page or max_age_days is None:
        return False
    ages = [_tiktok_age_days(item) for item in page]
    return all(age is not None and age > max_age_days for age in ages)


def _patrol_youtube(
    source: SourceEntry, max_items: int,
    existing_ids: set[str], max_age_days: int | None,
) -> list[ContentItem]:
    """Pull recent uploads from BOTH the channel's Home/Videos tab and its
    Shorts tab, dedupe by videoId, return everything for downstream filter.

    Dual-fetch rationale: long-form-only and Shorts-only creators both exist;
    a third group posts both. The Home tab + Shorts tab are independent
    surfaces on YouTube — neither is a superset. Filter applies the duration
    gate to drop > 10min items so this stays compatible with the swipe-feed
    target."""
    home_recent: list[dict] = []
    try:
        ch = opencli.youtube_channel(source.source_id, limit=max_items)
        home_recent = ch.get("recent_videos") or []
    except Exception as exc:  # noqa: BLE001 — patrol must not abort the source
        logger.warning("yt home pull failed for %s: %s", source.source_id, exc)
    # Note: opencli ≥ 1.7 dropped the `--type shorts` flag; the home/Videos
    # tab now returns a mix of long-form + shorts naturally. Filter side
    # (duration_max_seconds per topic) trims anything outside the wanted
    # length window.

    stamp = _fetched_at_stamp()
    items: list[ContentItem] = []
    seen: set[str] = set()
    for v in home_recent:
        url = str(v.get("url", "")).strip()
        vid = _extract_video_id(url)
        if not vid or vid in existing_ids or vid in seen:
            continue
        seen.add(vid)
        item = ContentItem(
            content_id=vid,
            source_id=source.source_id,
            topic=source.topic,
            platform="youtube",
            fetched_at=stamp,
            source_of="patrol",
            youtube=YouTubeDigest(
                title=str(v.get("title", "")),
                duration=str(v.get("duration", "")),
                views=str(v.get("views", "")),
                published=str(v.get("published", "")),
                url=url,
                thumbnail_url=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            ),
        )
        if _filter_by_age(item, max_age_days):
            items.append(item)
    return items


def _patrol_x(
    source: SourceEntry, max_items: int,
    existing_ids: set[str], max_age_days: int | None,
) -> list[ContentItem]:
    posts = opencli.twitter_user_timeline(source.source_id, limit=max_items)
    stamp = _fetched_at_stamp()
    items: list[ContentItem] = []
    for p in posts:
        pid = str(p.get("id", "")).strip()
        if not pid or pid in existing_ids:
            continue
        item = ContentItem(
            content_id=pid,
            source_id=source.source_id,
            topic=source.topic,
            platform="x",
            fetched_at=stamp,
            source_of="patrol",
            x=XDigest(
                text=str(p.get("text", ""))[:2000],
                author=str(p.get("author", "")),
                likes=int(p.get("likes") or 0),
                retweets=int(p.get("retweets") or 0),
                views=int(p.get("views") or 0),
                created_at=str(p.get("created_at", "")),
                url=str(p.get("url", "")),
                has_media=bool(p.get("has_media", False)),
                media_urls=list(p.get("media_urls") or []),
            ),
        )
        if _filter_by_age(item, max_age_days):
            items.append(item)
    return items


def _parse_entry_published(entry: Any) -> str:
    """Extract ISO8601 date from a feedparser entry; empty string if missing."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if struct:
            try:
                return datetime(*struct[:6], tzinfo=timezone.utc).isoformat()
            except Exception:  # noqa: BLE001
                continue
    return ""


def _patrol_web(
    source: SourceEntry, max_items: int,
    existing_ids: set[str], max_age_days: int | None,
) -> list[ContentItem]:
    parsed = feedparser.parse(source.source_id, request_headers={"User-Agent": _UA})
    entries = parsed.get("entries") or []
    stamp = _fetched_at_stamp()
    items: list[ContentItem] = []
    for e in entries[:max_items]:
        link = str(e.get("link") or "").strip()
        guid = str(e.get("id") or link).strip()
        if not guid or guid in existing_ids:
            continue
        summary = str(e.get("summary") or e.get("description") or "")[:1000].strip()
        item = ContentItem(
            content_id=guid,
            source_id=source.source_id,
            topic=source.topic,
            platform="web",
            fetched_at=stamp,
            source_of="patrol",
            web=WebDigest(
                title=str(e.get("title") or "").strip(),
                summary=summary,
                link=link,
                published_at=_parse_entry_published(e),
            ),
        )
        if _filter_by_age(item, max_age_days):
            items.append(item)
    return items


def _tiktok_digest(item: tiktok_client.TikTokVideoMetadata) -> TikTokDigest:
    return TikTokDigest(
        media_kind=item.media_kind,
        title=item.title,
        url=item.url,
        uploader=item.uploader,
        duration_seconds=item.duration,
        timestamp=item.timestamp,
        upload_date=item.upload_date,
        view_count=item.view_count,
        like_count=item.like_count,
        comment_count=item.comment_count,
        repost_count=item.repost_count,
        thumbnail_url=item.thumbnail_url,
        has_video_stream=item.has_video_stream,
        photo_count=item.photo_count,
        photo_image_urls=[image.url for image in item.photo_images],
        audio_url=item.audio_url,
    )


def _patrol_tiktok(
    source: SourceEntry,
    max_items: int,
    existing_ids: set[str],
    max_age_days: int | None,
    *,
    backfill_max_pages: int,
    require_video_stream: bool,
    allow_photo: bool,
) -> list[ContentItem]:
    page_size = max(1, max_items)
    backfill = source.metadata.get("tiktok_backfilled_at") is None and max_age_days is not None
    stamp = _fetched_at_stamp()
    items: list[ContentItem] = []
    seen: set[str] = set()

    max_pages = max(1, backfill_max_pages) if backfill else 1
    if backfill:
        logger.info(
            "tiktok backfill @%s: page_size=%d max_pages=%d max_age_days=%s",
            source.source_id,
            page_size,
            max_pages,
            max_age_days,
        )

    for page_idx in range(max_pages):
        start_index = page_idx * page_size + 1
        recent = tiktok_client.tiktok_list_user_videos(
            source.source_id,
            limit=page_size,
            start_index=start_index,
            allow_empty=page_idx > 0,
        )
        if not recent:
            break

        for raw in recent:
            media = raw
            if not media.id or media.id in existing_ids or media.id in seen:
                continue
            seen.add(media.id)
            if _tiktok_is_stale(media, max_age_days):
                continue

            if media.media_kind != "video" or not media.has_video_stream:
                try:
                    media = tiktok_client.tiktok_probe_media(media.url)
                except tiktok_client.TikTokYtDlpError as exc:
                    logger.info(
                        "tiktok patrol probe failed for %s/%s: %s",
                        source.source_id,
                        raw.id,
                        exc,
                    )
                    continue
                if _tiktok_is_stale(media, max_age_days):
                    continue

            if media.media_kind == "video":
                if require_video_stream and not media.has_video_stream:
                    continue
            elif media.media_kind == "photo":
                if not allow_photo or media.photo_count <= 0:
                    continue
            else:
                continue
            item = ContentItem(
                content_id=media.id,
                source_id=source.source_id,
                topic=source.topic,
                platform="tiktok",
                fetched_at=stamp,
                source_of="patrol",
                tiktok=_tiktok_digest(media),
            )
            if _filter_by_age(item, max_age_days):
                items.append(item)

        if not backfill or len(recent) < page_size:
            break
        if _tiktok_page_past_freshness(recent, max_age_days):
            logger.info(
                "tiktok backfill @%s stopped at page %d: page is outside freshness",
                source.source_id,
                page_idx + 1,
            )
            break
    return items


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _patrol_one(
    source: SourceEntry, runtime: RuntimeConfig,
    existing_ids: set[str], max_age_days: int | None,
) -> list[ContentItem]:
    if source.platform == "youtube":
        return _patrol_youtube(
            source, runtime.patrol.youtube.max_items_per_source, existing_ids, max_age_days,
        )
    if source.platform == "x":
        return _patrol_x(
            source, runtime.patrol.x.max_items_per_source, existing_ids, max_age_days,
        )
    if source.platform == "web":
        return _patrol_web(
            source, runtime.patrol.web.max_items_per_source, existing_ids, max_age_days,
        )
    if source.platform == "tiktok":
        return _patrol_tiktok(
            source,
            runtime.patrol.tiktok.max_items_per_source,
            existing_ids,
            max_age_days,
            backfill_max_pages=runtime.patrol.tiktok.backfill_max_pages,
            require_video_stream=runtime.filter.tiktok.require_video_stream,
            allow_photo=runtime.filter.tiktok.allow_photo,
        )
    return []


def _should_mark_tiktok_backfilled(source: SourceEntry, max_age_days: int | None) -> bool:
    return (
        source.platform == "tiktok"
        and max_age_days is not None
        and source.metadata.get("tiktok_backfilled_at") is None
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Patrol active sources for new content")
    parser.add_argument("--topic", help="Only patrol one topic")
    parser.add_argument(
        "--platform",
        choices=["youtube", "x", "web", "tiktok"],
        help="Only patrol one platform",
    )
    args = parser.parse_args(argv)
    _configure_logging()
    workdir = Path.cwd()
    load_env(workdir)

    runtime = load_runtime(workdir)
    catalog = _load_catalog()
    active = [e for e in catalog.sources.values() if e.status == "active"]
    if args.topic:
        active = [s for s in active if s.topic == args.topic]
    if args.platform:
        active = [s for s in active if s.platform == args.platform]

    # Consult queue_manage's signals. Cold start (no file) falls through to
    # "patrol everything"; otherwise restrict to refill_topics so API quota
    # is spent on topics that actually need stock.
    status = None if (args.topic or args.platform) else _load_queue_status()
    if status is not None:
        refill_set = set(status.refill_topics)
        if not refill_set:
            logger.info(
                "all topic queues at target (total=%d) — skipping patrol cycle",
                status.total_inventory,
            )
            return 0
        active = [s for s in active if s.topic in refill_set]
        logger.info(
            "refill cycle: %d sources across topics %s",
            len(active), sorted(refill_set),
        )
    else:
        if args.topic or args.platform:
            logger.info(
                "scoped patrol: topic=%s platform=%s",
                args.topic or "*",
                args.platform or "*",
            )
        else:
            logger.info("cold start (no queue_status.json) — patrolling all active sources")

    by_platform: dict[str, int] = {}
    for s in active:
        by_platform[s.platform] = by_platform.get(s.platform, 0) + 1
    logger.info("patrol: %d active sources %s", len(active), by_platform)
    touched_topics = {s.topic for s in active}

    # Preflight: if the opencli Browser Bridge isn't up, every per-source call
    # will fail the same way. Bail before burning ~60s/source on doomed calls.
    # Skip preflight only if this cycle has no opencli-dependent platforms.
    if any(p in by_platform for p in ("youtube", "x")):
        block = backpressure.active_block(backpressure.OPENCLI)
        if block is not None:
            logger.error(
                "opencli backpressure active (%s): %s",
                block.get("reason"), block.get("detail", ""),
            )
            return 2
        try:
            opencli.ping()
            logger.info("opencli preflight: OK")
        except OpenCLIInfraError as exc:
            backpressure.block_lane(
                backpressure.OPENCLI,
                reason="infra_unavailable",
                detail=str(exc),
            )
            logger.error(
                "opencli infra unavailable — aborting patrol. fix: %s\ncause: %s",
                "open Chrome and ensure the Browser Bridge extension is connected",
                exc,
            )
            return 2

    # Two-layered dedup:
    #   - scan queues/patrol/ for in-flight files (same cycle races)
    #   - load blocklist (queue.json + decisions.jsonl) for cross-cycle dedup
    # Union catches both "we already wrote this to queue earlier in this run"
    # AND "filter already admitted/rejected this content_id some run ago".
    existing_ids = _scan_existing_content_ids() | load_content_blocklist()
    logger.info(
        "pre-existing content_ids: %d (queues/patrol + blocklist)",
        len(existing_ids),
    )

    # Per-(topic, platform) max_age for the age filter. Reuses filter's
    # fallback chain: topic field → runtime.filter youtube fallback → None.
    config = load_interests(workdir)
    interests_by_topic: dict[str, InterestEntry] = {t.topic: t for t in config.interests}
    max_age_map: dict[tuple[str, str], int | None] = {}
    for s in active:
        key = (s.topic, s.platform)
        if key not in max_age_map:
            temporal = resolve_topic_temporal(
                s.topic, interests_by_topic.get(s.topic), runtime.filter, s.platform,
            )
            max_age_map[key] = temporal.max_age_days

    # opencli calls (youtube + x) share a Chrome semaphore globally, so
    # parallelism above 1 is capped there anyway; web uses feedparser over
    # plain HTTP which parallelises freely. Run them through a shared pool
    # for uniform progress reporting.
    n_new = 0
    n_fail = 0
    n_infra_fail = 0
    # If the Chrome bridge dies mid-run, every remaining source will fail the
    # same way — break early after 3 consecutive infra errors.
    _INFRA_CIRCUIT_BREAKER = 3
    aborted = False
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(
                _patrol_one, s, runtime, existing_ids,
                max_age_map.get((s.topic, s.platform)),
            ): s
            for s in active
        }
        for fut in tqdm(as_completed(futures), total=len(futures), desc="patrol", unit="src"):
            source = futures[fut]
            try:
                items = fut.result()
            except OpenCLIInfraError as exc:
                # Don't write patrol_failed event — this isn't the source's
                # fault, it's our infrastructure. Count toward circuit breaker.
                n_infra_fail += 1
                logger.error(
                    "opencli infra failure on %s (%d/%d): %s",
                    source.catalog_key, n_infra_fail, _INFRA_CIRCUIT_BREAKER, exc,
                )
                if n_infra_fail >= _INFRA_CIRCUIT_BREAKER:
                    backpressure.block_lane(
                        backpressure.OPENCLI,
                        reason="infra_unavailable",
                        detail=str(exc),
                    )
                    logger.error(
                        "circuit breaker tripped — aborting patrol run "
                        "(remaining sources will fail the same way)",
                    )
                    aborted = True
                    # Cancel futures not yet started; in-flight worker finishes.
                    for f in futures:
                        f.cancel()
                    break
                continue
            except (OpenCLIError, Exception) as exc:  # noqa: BLE001
                emit_judgment(
                    event_type="reject_source", platform=source.platform, topic=source.topic,
                    source_id=source.source_id, source_name=source.name,
                    reason_code="patrol_failed",
                    evidence={"error": f"{type(exc).__name__}: {str(exc)[:200]}"},
                )
                n_fail += 1
                continue
            for item in items:
                if item.content_id in existing_ids:
                    continue  # belt-and-suspenders vs races across same-run sources
                _write_queue_item(item)
                existing_ids.add(item.content_id)
                n_new += 1
            row = catalog.sources[source.catalog_key]
            patrolled_at = _utc_now_iso()
            row.last_patrolled_at = patrolled_at
            if _should_mark_tiktok_backfilled(
                source,
                max_age_map.get((source.topic, source.platform)),
            ):
                row.metadata = dict(row.metadata)
                row.metadata["tiktok_backfilled_at"] = patrolled_at
                row.metadata["tiktok_backfill_page_size"] = (
                    runtime.patrol.tiktok.max_items_per_source
                )

    _save_catalog(catalog, touched_topics)
    if aborted:
        logger.warning(
            "patrol aborted after %d infra failures; %d items written from %d sources "
            "processed before abort (%d per-source failures)",
            n_infra_fail, n_new,
            sum(1 for f in futures if f.done() and not f.cancelled()), n_fail,
        )
        return 2
    logger.info(
        "patrol_ok: %d new queue items, %d source failures, catalog patrolled_at updated",
        n_new, n_fail,
    )
    cycle_summary.add(
        "patrol",
        sources_covered=len(active),
        new_items=n_new,
        source_failures=n_fail,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
