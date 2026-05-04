"""Article fetch + extract via trafilatura.

Web RSS feeds often expose only meta-description summaries (TechCrunch) or
literal placeholders like "Comments" (Hacker News). For filter's LLM review
to see real content, we re-fetch the article URL and run trafilatura to
produce clean markdown with YAML frontmatter (title/author/date/tags).

No JS-render fallback by design — trafilatura's plain HTTP fetch covers the
sites we care about. If fetch or extraction fails, we return None and the
caller falls back to whatever RSS summary we already have (graceful
degradation — fetch failure never rejects content on its own).

Output is truncated to `_MAX_CHARS` to keep LLM prompts bounded. Result is
cached on disk keyed by SHA-256 of the URL so a filter re-run / rerun after
restart doesn't re-fetch.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import trafilatura


_logger = logging.getLogger("article_fetch")

_CACHE_DIR = Path("state/article_cache")
_MAX_CHARS = 5000  # cap on extracted output (~1100 EN tokens / ~2200 CN tokens)


def _cache_path(url: str) -> Path:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return _CACHE_DIR / f"{key}.md"


def fetch_article(url: str, *, use_cache: bool = True) -> str | None:
    """Fetch + extract article body. Returns clean markdown truncated to
    `_MAX_CHARS`, or None if fetch/extraction failed at any step."""
    if not url:
        return None
    cache = _cache_path(url)
    if use_cache and cache.exists():
        try:
            return cache.read_text(encoding="utf-8")
        except OSError:
            pass  # re-fetch on cache read error

    try:
        html = trafilatura.fetch_url(url)
    except Exception as exc:  # noqa: BLE001 — trafilatura can raise on DNS, ssl, redirects
        _logger.debug("fetch_url raised for %s: %s", url, exc)
        return None
    if not html:
        return None

    md = trafilatura.extract(
        html,
        output_format="markdown",
        with_metadata=True,
        include_comments=False,
        include_tables=True,
        include_links=False,
        favor_recall=False,
    )
    if not md:
        return None

    truncated = md[:_MAX_CHARS]
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(truncated, encoding="utf-8")
    except OSError as exc:
        _logger.warning("article cache write failed for %s: %s", url, exc)
    return truncated
