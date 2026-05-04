"""Feed-first web source discovery.

Given an article URL that came out of a google search, decide whether this
site is a viable "patrol target" — i.e. has a stable RSS/Atom feed we can
dumb-diff later. The resolution order:

  1. Platform rule (e.g. medium.com/@user/... → medium.com/feed/@user)
  2. Autodiscovery via `<link rel="alternate" type="application/rss+xml|...">`
     at the article URL, then at each ancestor path (/a/b/ → /a/ → /).
  3. Common-path probe: `/feed.xml`, `/rss.xml`, `/atom.xml`, `/index.xml`,
     `/feed`, `/rss`, at each ancestor scope.

A feed is accepted only if feedparser parses it AND it has ≥ min_entries AND
the most recent entry is within max_age_days.

No LLM calls. No platform-specific scrapers beyond the Medium rule.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import feedparser

_logger = logging.getLogger("feed")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = 10
_FEED_FILES = ("feed.xml", "rss.xml", "atom.xml", "index.xml", "feed", "rss")


@dataclass
class FeedResolution:
    """Result of resolve_and_validate. feed_url non-None ⇔ success."""

    feed_url: str | None = None
    method: str | None = None  # e.g. "autodisc@article", "autodisc@ancestor:/blog/", "common:/feed.xml", "platform:medium"
    feed_title: str | None = None
    feed_description: str | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)  # each: {title, summary, link, published_at}
    reject_reason_code: str | None = None  # "no_autodiscovery_no_common" / "min_entries_not_met" / "stale" / "html_fetch_fail" ...
    reject_detail: str | None = None  # short free-form for the ledger evidence


_LINK_TAG = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_HREF = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
_TYPE = re.compile(
    r"""type=["']?(application/(?:rss|atom)\+xml|application/xml|text/xml)["']?""",
    re.IGNORECASE,
)
_REL = re.compile(r"""rel=["']?alternate["']?""", re.IGNORECASE)


def _http_get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:  # noqa: S310
        return r.status, r.read()


def _autodiscover_feed(html_bytes: bytes, base_url: str) -> str | None:
    try:
        text = html_bytes.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    head_end = text.lower().find("</head>")
    scan = text[: head_end if head_end != -1 else len(text)]
    for m in _LINK_TAG.finditer(scan):
        tag = m.group(0)
        if not _REL.search(tag) or not _TYPE.search(tag):
            continue
        h = _HREF.search(tag)
        if h:
            return urllib.parse.urljoin(base_url, h.group(1))
    return None


def _walk_up_paths(article_url: str) -> list[str]:
    """Return URLs to try autodiscovery on, deepest first.

    https://a.com/x/y/post → [article, https://a.com/x/y/, https://a.com/x/, https://a.com/]
    """
    p = urllib.parse.urlparse(article_url)
    host = f"{p.scheme}://{p.netloc}"
    parts = [seg for seg in p.path.split("/") if seg]
    out = [article_url]
    for i in range(len(parts) - 1, -1, -1):
        ancestor_path = "/" + "/".join(parts[:i])
        if not ancestor_path.endswith("/"):
            ancestor_path += "/"
        candidate = host + ancestor_path
        if candidate not in out:
            out.append(candidate)
    return out


def _medium_platform_rule(article_url: str) -> str | None:
    p = urllib.parse.urlparse(article_url)
    if not p.netloc.endswith("medium.com"):
        return None
    segs = [seg for seg in p.path.split("/") if seg]
    if not segs:
        return None
    first = segs[0]
    # medium.com/@user/... → /feed/@user ; medium.com/<publication>/... → /feed/<pub>
    return f"{p.scheme}://{p.netloc}/feed/{first}"


def _parse_entry_date(entry: Any) -> datetime | None:
    """feedparser gives several date fields; take the first parseable one."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if struct:
            try:
                return datetime(*struct[:6], tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                continue
    return None


def _parse_feed_payload(feed_url: str) -> tuple[int, Any]:
    """Fetch the feed bytes with our own timeout, then hand them to feedparser.

    feedparser.parse(URL, ...) does its own HTTP fetch internally and offers
    no way to set a socket timeout — a non-responsive host hangs the call
    indefinitely (we lost a 7.5h discover run to this). Pulling the bytes
    via `_http_get` (10s timeout) and passing them in keeps the feedparser
    contract identical but bounds the wait. Returns (0, None) on any error
    so callers can treat it as "no feed found" without special-casing.
    """
    try:
        _, body = _http_get(feed_url)
        parsed = feedparser.parse(body)
    except Exception:  # noqa: BLE001
        return 0, None
    entries = parsed.get("entries") or []
    return len(entries), parsed


def _build_entries_sample(parsed: Any, limit: int = 15) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in (parsed.get("entries") or [])[:limit]:
        dt = _parse_entry_date(e)
        out.append({
            "title": (e.get("title") or "").strip(),
            "summary": (e.get("summary") or e.get("description") or "").strip(),
            "link": (e.get("link") or "").strip(),
            "published_at": dt.isoformat() if dt else "",
        })
    return out


def _latest_entry_age_days(parsed: Any) -> float | None:
    latest: datetime | None = None
    for e in parsed.get("entries") or []:
        dt = _parse_entry_date(e)
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    if latest is None:
        return None
    return (datetime.now(timezone.utc) - latest).total_seconds() / 86400.0


def _accept(feed_url: str, method: str, parsed: Any, *, min_entries: int, max_age_days: int) -> FeedResolution:
    entries = parsed.get("entries") or []
    if len(entries) < min_entries:
        return FeedResolution(
            reject_reason_code="min_entries_not_met",
            reject_detail=f"{feed_url}: {len(entries)} < {min_entries}",
        )
    age = _latest_entry_age_days(parsed)
    if age is None:
        # No parseable date on any entry — treat as stale rather than accepting blindly.
        return FeedResolution(
            reject_reason_code="no_entry_date",
            reject_detail=feed_url,
        )
    if age > max_age_days:
        return FeedResolution(
            reject_reason_code="stale",
            reject_detail=f"{feed_url}: newest entry {age:.0f}d old > {max_age_days}d",
        )
    feed_meta = parsed.get("feed") or {}
    return FeedResolution(
        feed_url=feed_url,
        method=method,
        feed_title=(feed_meta.get("title") or "").strip() or None,
        feed_description=(feed_meta.get("description") or feed_meta.get("subtitle") or "").strip() or None,
        entries=_build_entries_sample(parsed),
    )


def resolve_and_validate(
    article_url: str,
    *,
    min_entries: int,
    max_age_days: int,
) -> FeedResolution:
    """Try to find a feed for the site an article belongs to, and validate it.

    On success: resolution.feed_url is set; .entries has recent samples.
    On failure: feed_url is None; reject_reason_code + detail explain why.
    """
    p = urllib.parse.urlparse(article_url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return FeedResolution(reject_reason_code="invalid_url", reject_detail=article_url)

    # 1. Platform rule
    platform_feed = _medium_platform_rule(article_url)
    if platform_feed:
        n, parsed = _parse_feed_payload(platform_feed)
        if n >= 1 and parsed is not None:
            result = _accept(platform_feed, "platform:medium", parsed,
                             min_entries=min_entries, max_age_days=max_age_days)
            if result.feed_url:
                return result

    paths_to_try = _walk_up_paths(article_url)
    notes: list[str] = []

    # 2. Autodiscovery at article + each ancestor
    for page_url in paths_to_try:
        try:
            status, body = _http_get(page_url)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"fetch_fail[{page_url}]: {type(exc).__name__}")
            continue
        if status != 200 or not body:
            notes.append(f"status={status}[{page_url}]")
            continue
        cand = _autodiscover_feed(body, page_url)
        if not cand:
            continue
        n, parsed = _parse_feed_payload(cand)
        if n < 1 or parsed is None:
            notes.append(f"autodisc_empty[{cand}]")
            continue
        method = (
            "autodisc@article"
            if page_url == article_url
            else f"autodisc@ancestor:{urllib.parse.urlparse(page_url).path}"
        )
        result = _accept(cand, method, parsed, min_entries=min_entries, max_age_days=max_age_days)
        if result.feed_url:
            return result
        notes.append(f"{result.reject_reason_code}[{cand}]")

    # 3. Common-path probe at each ancestor
    host = f"{p.scheme}://{p.netloc}"
    tried: set[str] = set()
    for scope_url in paths_to_try:
        scope = urllib.parse.urlparse(scope_url)
        scope_path = scope.path if scope.path.endswith("/") else scope.path.rsplit("/", 1)[0] + "/"
        if not scope_path.startswith("/"):
            scope_path = "/" + scope_path
        for ff in _FEED_FILES:
            probe = f"{host}{scope_path}{ff}"
            if probe in tried:
                continue
            tried.add(probe)
            n, parsed = _parse_feed_payload(probe)
            if n < 1 or parsed is None:
                continue
            result = _accept(probe, f"common:{scope_path}{ff}", parsed,
                             min_entries=min_entries, max_age_days=max_age_days)
            if result.feed_url:
                return result
            notes.append(f"{result.reject_reason_code}[{probe}]")

    return FeedResolution(
        reject_reason_code="no_feed_found",
        reject_detail="; ".join(notes[:4]) or None,
    )
