from __future__ import annotations

import asyncio
from pathlib import Path

from facebook_marketplace_scraper.collector import FacebookMarketplaceCollector, check_facebook_page
from facebook_marketplace_scraper.config import get_settings


async def main() -> None:
    settings = get_settings()
    state_path = Path(settings.facebook_storage_state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    executable_candidates = (
        settings.facebook_browser_executable,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    )
    executable = next(
        (path for path in executable_candidates if path and Path(path).is_file()),
        None,
    )

    settings = settings.model_copy(update={"facebook_browser_executable": executable})
    collector = FacebookMarketplaceCollector(settings)
    async with collector.browser_context(headless=False, require_auth=False) as context:
        page = await context.new_page()
        await page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded")
        print("Log in to Facebook in the opened browser, then return here.")
        await asyncio.to_thread(input, "Press Enter after Marketplace is visible: ")
        await check_facebook_page(page)
    print(f"Facebook session saved to {state_path}")


if __name__ == "__main__":
    asyncio.run(main())
