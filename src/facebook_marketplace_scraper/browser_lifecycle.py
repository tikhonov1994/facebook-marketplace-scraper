"""Bound browser shutdown independently of Playwright's navigation timeouts."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from contextlib import asynccontextmanager

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)
CLOSE_TIMEOUT_SECONDS = 10


async def close_browser_resource(operation: Awaitable[object], label: str) -> None:
    try:
        await asyncio.wait_for(operation, timeout=CLOSE_TIMEOUT_SECONDS)
    except (TimeoutError, PlaywrightError):
        # A failed cleanup must not discard collected rows or prevent the next
        # cleanup layer (context, browser, driver) from running.
        logger.warning("Browser cleanup failed: %s", label, exc_info=True)


@asynccontextmanager
async def managed_playwright():
    playwright = await async_playwright().start()
    try:
        yield playwright
    finally:
        await close_browser_resource(playwright.stop(), "playwright driver")
