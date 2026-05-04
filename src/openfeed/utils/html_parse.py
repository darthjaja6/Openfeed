"""Lightweight HTML parsing helpers (regex-based, no extra deps).

Just enough for web discover to extract titles, dates, and same-domain links.
"""
from __future__ import annotations

import re
from datetime import datetime
from html import unescape
from urllib.parse import urldefrag, urljoin, urlparse


_TITLE_PAT = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_PUBLISHED_PATS = [
    re.compile(
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+name=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+name=["\']pubdate["\'][^>]+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<time[^>]+datetime=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
]
_LINK_PAT = re.compile(
    r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_STRIP_PAT = re.compile(r"<[^>]+>")
_WS_PAT = re.compile(r"\s+")

_DEFAULT_EXCLUDED_PATH_FRAGMENTS = (
    "/tag/",
    "/tags/",
    "/category/",
    "/categories/",
    "/author/",
    "/authors/",
    "/about",
    "/contact",
    "/careers",
    "/jobs",
    "/pricing",
    "/login",
    "/signin",
    "/feed",
    "/rss",
    ".rss",
    ".xml",
)


def extract_title(html: str) -> str | None:
    match = _TITLE_PAT.search(html)
    if not match:
        return None
    text = unescape(match.group(1)).strip()
    text = _WS_PAT.sub(" ", text)
    return text or None


def extract_published_at(html: str) -> datetime | None:
    for pat in _META_PUBLISHED_PATS:
        match = pat.search(html)
        if match:
            raw = unescape(match.group(1)).strip()
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def estimate_body_text_length(html: str) -> int:
    text = _TAG_STRIP_PAT.sub(" ", html)
    text = _WS_PAT.sub(" ", text).strip()
    return len(text)


_SCRIPT_STYLE_PAT = re.compile(
    r"<(?:script|style)[^>]*>.*?</(?:script|style)>",
    re.IGNORECASE | re.DOTALL,
)


def extract_body_text(html: str, *, max_chars: int = 1000) -> str:
    """Strip scripts/styles + all tags, return the first `max_chars` of body text."""
    stripped = _SCRIPT_STYLE_PAT.sub(" ", html)
    stripped = _TAG_STRIP_PAT.sub(" ", stripped)
    stripped = unescape(stripped)
    stripped = _WS_PAT.sub(" ", stripped).strip()
    return stripped[:max_chars]


def extract_same_domain_links(
    html: str,
    base_url: str,
    *,
    excluded_path_fragments: tuple[str, ...] = _DEFAULT_EXCLUDED_PATH_FRAGMENTS,
    max_links: int | None = None,
) -> list[dict[str, str]]:
    """Return [{url, anchor_text}] for same-domain links, filtered by excludes."""
    base_host = urlparse(base_url).netloc.lower()
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for match in _LINK_PAT.finditer(html):
        raw_href = unescape(match.group(1)).strip()
        anchor_html = match.group(2)
        anchor_text = _WS_PAT.sub(" ", _TAG_STRIP_PAT.sub(" ", unescape(anchor_html))).strip()
        if not raw_href or raw_href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = urldefrag(urljoin(base_url, raw_href)).url
        parsed = urlparse(absolute)
        if not parsed.scheme.startswith("http") or parsed.netloc.lower() != base_host:
            continue
        path_lower = parsed.path.lower()
        if any(fragment in path_lower for fragment in excluded_path_fragments):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append({"url": absolute, "anchor_text": anchor_text[:120]})
        if max_links is not None and len(out) >= max_links:
            break
    return out


def root_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"
