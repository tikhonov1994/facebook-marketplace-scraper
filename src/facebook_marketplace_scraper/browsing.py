"""Optional, bounded photo viewing on newly discovered additional listings."""

import random
import re

from playwright.async_api import Error as PlaywrightError

PHOTO_OPEN = re.compile(r"^(?:Open|View) (?:photo|image)(?: \d+)?$|^Открыть (?:фото|фотографию)(?: \d+)?$", re.IGNORECASE)
PHOTO_DIALOG = re.compile(r"^(?:Photos?|Images?|Photo viewer|Фотографии|Фото|Просмотр фото)$", re.IGNORECASE)
NEXT_PHOTO = re.compile(r"^(?:Next photo|Next image|Следующее фото|Следующая фотография)$", re.IGNORECASE)
PREVIOUS_PHOTO = re.compile(r"^(?:Previous photo|Previous image|Предыдущее фото|Предыдущая фотография)$", re.IGNORECASE)


async def browse_listing_photos(page, check_page, *, max_steps=2, rng=None):
    """Use explicit photo controls only; unknown layouts are left alone.

    The caller supplies the account-warning check to avoid a collector import cycle.
    No writes to the Facebook account (messages, reactions, saves) are performed.
    """
    rng = rng or random.SystemRandom()
    try:
        await check_page(page)
        if rng.choice((False, True)):
            await page.mouse.wheel(0, rng.randint(150, 450))
            await page.wait_for_timeout(rng.randint(300, 700))
            await check_page(page)
        opener = page.get_by_role("button", name=PHOTO_OPEN).first
        if not await opener.is_visible():
            return
        await opener.click(timeout=2000)
        await page.wait_for_timeout(rng.randint(400, 900))
        await check_page(page)
        viewer = page.get_by_role("dialog", name=PHOTO_DIALOG).last
        if not await viewer.is_visible():
            return
        for _ in range(rng.randint(0, max_steps)):
            await check_page(page)
            button = viewer.get_by_role(
                "button", name=rng.choice((NEXT_PHOTO, PREVIOUS_PHOTO)),
            ).first
            if not await button.is_visible() or not await button.is_enabled():
                break
            await button.click(timeout=2000)
            await page.wait_for_timeout(rng.randint(400, 900))
            await check_page(page)
        await check_page(page)
        await page.keyboard.press("Escape")
        await check_page(page)
    except PlaywrightError:
        # Optional browsing must not discard extracted data, but account warnings
        # must still propagate to the shared persistent pause mechanism.
        try:
            await check_page(page)
        except PlaywrightError:
            pass
