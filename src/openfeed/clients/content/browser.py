"""Headless-browser fetch via Playwright.

Some sites (Cloudflare-fronted, Forbes, clutch.co, Anthropic blog, etc.) refuse
plain urllib requests. Playwright drives the system Chrome install so this path
uses the same browser family as the opencli-backed YouTube / X connectors.

Thread-safety: Playwright sync API requires per-thread instances. We keep a
thread-local browser, lazily launched on first use; processes exiting reaps
the browsers automatically.
"""
from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("discover.browser")

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)


_local = threading.local()
_all_playwrights: list[Any] = []
_lock = threading.Lock()
_CHROME_EXECUTABLE = "/usr/bin/google-chrome"


def _default_tmpdir() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent / "tmp" / "playwright-runtime"
    return Path.cwd() / "tmp" / "playwright-runtime"


def _ensure_browser():
    if getattr(_local, "browser", None) is not None:
        return _local.browser
    started = time.monotonic()
    if not os.environ.get("TMPDIR"):
        tmpdir = _default_tmpdir()
        tmpdir.mkdir(parents=True, exist_ok=True)
        os.environ["TMPDIR"] = str(tmpdir)
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        executable_path=_CHROME_EXECUTABLE,
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    _local.pw = pw
    _local.browser = browser
    with _lock:
        _all_playwrights.append(pw)
    logger.info("launched chrome for thread %s in %.1fs", threading.current_thread().name, time.monotonic() - started)
    return browser


def get_html(
    url: str,
    *,
    timeout: int = 20,
    wait_until: str = "domcontentloaded",
) -> tuple[int, str]:
    """Fetch URL via headless Chrome. Returns (status, html). status=0 if no response."""
    browser = _ensure_browser()
    timeout_ms = timeout * 1000
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 720},
        locale="en-US",
    )
    page = context.new_page()
    started = time.monotonic()
    try:
        try:
            response = page.goto(url, timeout=timeout_ms, wait_until=wait_until)
        except PlaywrightTimeout:
            html = page.content() if page else ""
            logger.debug("timeout %s after %.1fs", url, time.monotonic() - started)
            return 0, html
        status = response.status if response else 0
        html = page.content()
        logger.debug("fetched %s status=%d in %.1fs", url, status, time.monotonic() - started)
        return status, html
    except PlaywrightError as exc:
        logger.debug("playwright error %s: %s", url, exc)
        raise RuntimeError(f"Playwright error fetching {url}: {exc}") from exc
    finally:
        page.close()
        context.close()


@atexit.register
def _cleanup() -> None:
    with _lock:
        for pw in _all_playwrights:
            try:
                pw.stop()
            except Exception:
                pass
        _all_playwrights.clear()
