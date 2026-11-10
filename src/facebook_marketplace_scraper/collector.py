from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from facebook_marketplace_scraper.feed import FeedCapture
from facebook_marketplace_scraper.config import Settings
from facebook_marketplace_scraper.models import RawAdvertisement
from facebook_marketplace_scraper.browser_lifecycle import close_browser_resource, managed_playwright
from facebook_marketplace_scraper.photos import browser_photos
from facebook_marketplace_scraper.browsing import browse_listing_photos
from facebook_marketplace_scraper.profile import profile_lock, seed_profile

logger = logging.getLogger(__name__)


def marketplace_price_label(body: str, title: str) -> str | None:
    """Read only the price block after the exact heading, never description prices."""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if title not in lines:
        return None
    candidates = []
    for line in lines[lines.index(title) + 1:][:6]:
        if line.casefold().startswith(("описание", "description", "รายละเอียด", "listed", "опубликовано", "ลงประกาศ")):
            break
        if re.search(r"(?:฿|THB|บาท|бат|Baht|\$|€|USD|EUR)", line, re.IGNORECASE) and re.search(r"\d", line):
            candidates.append(line)
    # Two prices may include a crossed-out value; do not guess which is current.
    return candidates[0] if len(candidates) == 1 else None


def listing_text(title: str, description: str | None, price_label: str | None) -> str:
    return "\n\n".join(part for part in (
        title, f"Facebook Marketplace displayed price: {price_label}" if price_label else None,
        description,
    ) if part)

FACEBOOK_CATEGORIES = {"rent": "propertyrentals", "sale": "propertyforsale"}
LOCAL_LOCATION_MARKERS = (
    "pattaya",
    "พัทยา",
    "паттайя",
    "jomtien",
    "จอมเทียน",
    "na kluea",
    "naklua",
    "บางละมุง",
    "bang lamung",
    "nong prue",
    "หนองปรือ",
    "pratumnak",
    "พระตำหนัก",
)


class FacebookSessionExpiredError(PermissionError):
    """Raised when Facebook redirects the collector to the login page."""


class FacebookRestrictionError(RuntimeError):
    """Facebook requires human review; collection must remain paused."""


def check_facebook_restriction(url: str, text: str = "") -> None:
    path = urlsplit(url).path.lower()
    if any(part in path for part in ("/checkpoint", "/challenge", "/captcha")):
        raise FacebookRestrictionError("security_check")
    normalized = " ".join(text.casefold().split())
    markers = (
        "we suspect automated behavior", "we suspect automated behaviour",
        "suspected automated behavior", "suspected automated behaviour",
        "your account has been temporarily blocked", "you're temporarily blocked",
        "your account has been suspended", "your account has been disabled",
        "confirm you're human", "confirm you’re human", "verify you're human",
        "verify you’re human", "complete the security check",
        "enter the text you see", "введите текст с картинки",
        "подозреваем автоматизирован", "подозрение на автоматизирован",
        "мы заподозрили автоматизированное поведение в вашем аккаунте",
        "подозрении в автоматизации", "подозрительная автоматизированная",
        "ваш аккаунт временно заблокирован", "ваш аккаунт заблокирован",
        "действие временно заблокировано", "подтвердите, что вы человек",
        "подтвердите, что вы не робот", "введите символы с картинки",
        "you can't use this feature right now", "you can’t use this feature right now",
        "вы временно не можете использовать эту функцию",
    )
    if any(marker in normalized for marker in markers):
        raise FacebookRestrictionError("restriction_notice")


async def check_facebook_page(page: Page) -> str:
    check_facebook_restriction(page.url)
    text = await page.locator("body").inner_text(timeout=7000)
    check_facebook_restriction(page.url, text)
    if "/login" in urlsplit(page.url).path:
        raise FacebookSessionExpiredError("Facebook session expired; run facebook_login again")
    return text


def relative_age_minutes(label: str) -> int | None:
    normalized = label.replace("\u00a0", " ").casefold()
    day_match = re.search(r"(\d+)\s*(?:дней|дня|день|days?|วัน)", normalized)
    if day_match:
        return int(day_match.group(1)) * 1440
    if any(marker in normalized for marker in (
        "вчера", "yesterday", "около дня", "about a day", "เมื่อวาน", "ประมาณหนึ่งวัน",
    )):
        return 1440
    minute_match = re.search(r"(\d+)\s*(?:мин|minute|min|นาที)", normalized)
    if minute_match:
        return int(minute_match.group(1))
    hour_match = re.search(r"(\d+)\s*(?:час|hour|hr|ชั่วโมง)", normalized)
    if hour_match:
        return int(hour_match.group(1)) * 60
    if any(
        marker in normalized
        for marker in ("около часа", "about an hour", "ประมาณหนึ่งชั่วโมง")
    ):
        return 60
    if any(
        marker in normalized
        for marker in ("только что", "секунд", "just now", "second", "เมื่อสักครู่")
    ):
        return 0
    return None


def is_local_marketplace_label(label: str) -> bool:
    normalized = label.casefold()
    return any(marker in normalized for marker in LOCAL_LOCATION_MARKERS)


def has_radius_confirmation(text: str, radius_km: int) -> bool:
    normalized = text.replace("\u00a0", " ").casefold()
    # Marketplace can display whole miles even with a Russian locale and a
    # kilometre radius in the URL (10 km is displayed as 6 miles).
    displayed_miles = int(radius_km / 1.609344 + 0.5)
    for match in re.finditer(
        r"(?<![\w.,])(?P<distance>\d+(?:[.,]\d+)?)\s*"
        r"(?P<unit>км|km|กม|миль|мили|миля|miles?|mi|ไมล์)(?!\w)",
        normalized,
    ):
        distance = float(match["distance"].replace(",", "."))
        if match["unit"] in {"км", "km", "กม"}:
            if distance == radius_km:
                return True
        elif distance == displayed_miles:
            return True
    return False


def parse_detail_text(body: str, title: str) -> dict[str, str | None]:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    start = lines.index(title) if title in lines else 0
    detail = lines[start:]
    published_label = next(
        (
            line
            for line in detail
            if line.casefold().startswith(("опубликовано", "listed", "ลงประกาศ"))
        ),
        None,
    )

    description = None
    description_markers = ("Описание", "Description", "รายละเอียด")
    description_index = next(
        (detail.index(marker) for marker in description_markers if marker in detail),
        None,
    )
    if description_index is not None:
        end = len(detail)
        for marker in (
            "Ещё",
            "See more",
            "ดูเพิ่มเติม",
            "Показать перевод",
            "See translation",
            "Информация о продавце",
            "Seller information",
        ):
            if marker in detail[description_index + 1 :]:
                end = min(end, detail.index(marker, description_index + 1))
        description = "\n".join(detail[description_index + 1 : end]).strip() or None

    seller = None
    seller_markers = ("Информация о продавце", "Seller information")
    seller_index = next((detail.index(marker) for marker in seller_markers if marker in detail), None)
    if seller_index is not None:
        ignored = {*seller_markers}
        seller = next(
            (
                line
                for line in detail[seller_index + 1 :]
                if line not in ignored
                and not line.startswith(("На Facebook", "Joined Facebook", "Если вы считаете"))
            ),
            None,
        )
    return {
        "published_label": published_label,
        "description": description,
        "seller_name": seller,
    }


async def save_refreshed_storage_state(
    context: BrowserContext,
    destination: Path,
) -> None:
    """Atomically persist cookies refreshed during a successful collection."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        await context.storage_state(path=str(temporary))
        temporary.chmod(0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


class FacebookMarketplaceCollector:
    """Collect Pattaya listings with publication ages in seconds, minutes or hours."""

    name = "facebook_marketplace"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.seen_external_ids: set[str] = set()
        self.run_id = uuid.uuid4().hex
        self._outcomes: Counter[str] = Counter()
        self._timezone = ZoneInfo("Asia/Bangkok")

    async def fetch(
        self,
        known_external_ids: set[str] | None = None,
        *,
        check_known: Callable[[BrowserContext, set[str]], Awaitable[None]] | None = None,
    ) -> list[RawAdvertisement]:
        async with self.browser_context() as context:
            return await self._fetch_from_context(
                context, known_external_ids or set(), check_known=check_known
            )

    @asynccontextmanager
    async def browser_context(
        self, *, persist_storage_state: bool = True, headless: bool = True,
        require_auth: bool = True,
    ):
        storage_state = Path(self._settings.facebook_storage_state)
        profile = Path(self._settings.facebook_profile_dir) if self._settings.facebook_profile_dir else None
        initialized = profile is not None and (profile / ".initialized").is_file()
        if require_auth and not initialized and not storage_state.is_file():
            raise ValueError(
                f"Facebook storage state not found: {storage_state}. "
                "Run facebook-scraper login first."
            )

        async with managed_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "headless": headless,
                "args": ["--disable-dev-shm-usage", "--no-sandbox"],
            }
            executable = self._settings.facebook_browser_executable
            if executable and Path(executable).is_file():
                launch_options["executable_path"] = executable
            context_options = {"locale": "ru-RU", "timezone_id": "Asia/Bangkok"}
            if profile:
                with profile_lock(profile):
                    context = await playwright.chromium.launch_persistent_context(
                        user_data_dir=str(profile), **launch_options, **context_options,
                    )
                    try:
                        if not initialized and storage_state.is_file():
                            await seed_profile(context, storage_state)
                        context.set_default_timeout(self._settings.facebook_page_timeout_seconds * 1000)
                        yield context
                        if persist_storage_state:
                            await save_refreshed_storage_state(context, storage_state)
                        (profile / ".initialized").touch(mode=0o600)
                    finally:
                        await close_browser_resource(context.close(), "facebook persistent context")
            else:
                browser = await playwright.chromium.launch(**launch_options)
                try:
                    if storage_state.is_file():
                        context_options["storage_state"] = str(storage_state)
                    context = await browser.new_context(**context_options)
                    context.set_default_timeout(self._settings.facebook_page_timeout_seconds * 1000)
                    try:
                        yield context
                        if persist_storage_state:
                            await save_refreshed_storage_state(context, storage_state)
                    finally:
                        await close_browser_resource(context.close(), "facebook context")
                finally:
                    await close_browser_resource(browser.close(), "facebook browser")

    async def _fetch_from_context(
        self,
        context: BrowserContext,
        known_external_ids: set[str],
        *,
        check_known: Callable[[BrowserContext, set[str]], Awaitable[None]] | None = None,
    ) -> list[RawAdvertisement]:
        self.run_id = uuid.uuid4().hex
        self._outcomes.clear()
        attempted = 0
        completed = False
        feed_page = await context.new_page()
        detail_page: Page | None = None
        candidates: dict[str, dict[str, Any]] = {}
        visited_sellers: set[str] = set()
        browsing_visits = 0
        try:
            for listing_type, category in FACEBOOK_CATEGORIES.items():
                for candidate in await self._search_category(feed_page, category):
                    existing = candidates.get(candidate["id"])
                    if existing:
                        existing["feed_kinds"].add(listing_type)
                    else:
                        candidate["feed_kinds"] = {listing_type}
                        candidates[candidate["id"]] = candidate

            # The feed is no longer needed; stop its scripts/media during detail collection.
            await close_browser_resource(feed_page.close(), "facebook feed_page")
            self.seen_external_ids = set(candidates) & known_external_ids
            new_candidates = [
                candidate for item_id, candidate in candidates.items()
                if item_id not in known_external_ids
            ]
            logger.info(
                "Facebook found %d candidates; %d require detail collection",
                len(candidates),
                len(new_candidates),
            )
            advertisements: list[RawAdvertisement] = []
            for index, candidate in enumerate(new_candidates, start=1):
                if attempted >= self._settings.facebook_detail_limit:
                    break
                if index == 1 or index % 10 == 0:
                    logger.info(
                        "Collecting Facebook details %d/%d",
                        index,
                        len(new_candidates),
                    )
                if detail_page is None:
                    detail_page = await context.new_page()
                attempted += 1
                read_errors = self._outcomes["read_error"]
                advertisement = await self._fetch_detail(detail_page, candidate)
                if advertisement is not None:
                    advertisements.append(advertisement)
                    if (candidate.get("additional_visit")
                            and self._settings.facebook_photo_browsing_enabled
                            and browsing_visits < self._settings.facebook_photo_browsing_visit_limit
                            and random.random() < 0.5):
                        browsing_visits += 1
                        await browse_listing_photos(
                            detail_page, check_facebook_page,
                            max_steps=self._settings.facebook_photo_browsing_max_steps,
                        )
                    seller_url = (advertisement.raw_data or {}).get("seller_profile_url")
                    if (self._settings.facebook_seller_listings_enabled and seller_url
                            and seller_url not in visited_sellers
                            and len(visited_sellers) < self._settings.facebook_seller_page_limit):
                        visited_sellers.add(seller_url)
                        browse_seller = bool(
                            candidate.get("additional_visit")
                            and self._settings.facebook_seller_browsing_enabled
                        )
                        seller_items = await self._collect_seller_candidates(
                            detail_page, seller_url, browse=browse_seller,
                        )
                        discovered = []
                        for item in seller_items:
                            if item["id"] in known_external_ids:
                                self.seen_external_ids.add(item["id"])
                            elif item["id"] not in candidates:
                                # A seller can offer both rent and sale: do not inherit the
                                # originating card's category as evidence for these cards.
                                item["additional_visit"] = True
                                item["feed_kinds"] = set()
                                candidates[item["id"]] = item
                                discovered.append(item)
                        # Preview a few new seller cards next, using the same queue
                        # and global detail limit rather than a second detail reader.
                        preview = []
                        if browse_seller and discovered:
                            count = random.randint(1, min(
                                len(discovered), self._settings.facebook_seller_browsing_max_cards,
                            ))
                            preview = random.sample(discovered, count)
                        preview_ids = {item["id"] for item in preview}
                        new_candidates[index:index] = preview
                        new_candidates.extend(
                            item for item in discovered if item["id"] not in preview_ids
                        )
                if self._outcomes["read_error"] > read_errors:
                    # A timed-out navigation can still be running. Reusing this
                    # tab makes subsequent goto calls interrupt one another.
                    await close_browser_resource(detail_page.close(), "facebook failed detail")
                    detail_page = None
            if check_known and self.seen_external_ids:
                await check_known(context, self.seen_external_ids)
            completed = True
            return advertisements
        finally:
            logger.info(
                "Facebook collection summary run=%s completed=%s candidates=%d known=%d "
                "attempted=%d outcomes=%s",
                self.run_id, completed, len(candidates),
                len(set(candidates) & known_external_ids), attempted,
                json.dumps(dict(self._outcomes), sort_keys=True),
            )
            try:
                if not feed_page.is_closed():
                    await close_browser_resource(feed_page.close(), "facebook feed_page")
            finally:
                if detail_page is not None:
                    await close_browser_resource(detail_page.close(), "facebook detail_page")

    async def _search_category(self, page: Page, category: str):
        initial = await self._collect_candidates(page, category)
        found = {item["id"]: item for item in initial}
        if not self._settings.facebook_price_split_enabled:
            return initial
        pending = [(initial, 0, None)]
        searches = 0
        threshold = self._settings.facebook_price_split_threshold
        budget = self._settings.facebook_price_split_max_searches
        while pending and searches + 2 <= budget:
            items, lower, upper = pending.pop(0)
            if len(items) < threshold:
                continue
            prices = sorted({item.get("price_amount") for item in items
                             if item.get("price_currency") == "THB"
                             and isinstance(item.get("price_amount"), int)
                             and item["price_amount"] >= lower
                             and (upper is None or item["price_amount"] <= upper)})
            if len(prices) < 2:
                continue
            midpoint = prices[(len(prices) - 1) // 2]
            if midpoint < lower or (upper is not None and midpoint >= upper):
                continue
            for minimum, maximum in ((lower, midpoint), (midpoint + 1, upper)):
                await page.wait_for_timeout(self._settings.facebook_check_pause_seconds * 1000)
                batch = await self._collect_candidates(
                    page, category, min_price=minimum, max_price=maximum,
                )
                searches += 1
                for item in batch:
                    item["additional_visit"] = True
                    found.setdefault(item["id"], item)
                pending.append((batch, minimum, maximum))
        return list(found.values())

    async def _collect_candidates(
        self, page: Page, category: str, *, min_price=None, max_price=None,
    ) -> list[dict[str, Any]]:
        location_id = self._settings.facebook_location_id
        url = (
            f"https://www.facebook.com/marketplace/{location_id}/{category}/"
            f"?daysSinceListed=1&sortBy=creation_time_descend&exact=false"
            f"&radius={self._settings.facebook_radius_km}"
        )
        if min_price is not None:
            url += f"&minPrice={min_price}"
        if max_price is not None:
            url += f"&maxPrice={max_price}"
        return await self._read_candidate_page(page, url, require_radius=True)

    async def _collect_seller_candidates(self, page: Page, url: str, *, browse=False):
        try:
            return await self._read_candidate_page(page, url, require_radius=False, browse=browse)
        except PlaywrightError:
            # Do not hide challenges behind an optional enrichment failure.
            await check_facebook_page(page)
            logger.warning("Unable to read Facebook seller listings")
            return []

    async def _save_radius_diagnostics(self, page: Page, requested_url: str, text: str) -> None:
        captured_at = datetime.now(self._timezone)
        directory = (
            Path(self._settings.facebook_storage_state).parent / "diagnostics"
            / f"radius-{captured_at:%Y%m%dT%H%M%S}-{self.run_id}-{uuid.uuid4().hex[:8]}"
        )
        try:
            directory.mkdir(parents=True, mode=0o700)
            metadata = {
                "captured_at": captured_at.isoformat(),
                "run_id": self.run_id,
                "requested_url": requested_url,
                "final_url": page.url,
                "radius_km": self._settings.facebook_radius_km,
            }
            (directory / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            # Preserve the exact text used by the failing check, without another DOM read.
            (directory / "page.txt").write_text(text, encoding="utf-8")
        except Exception:
            logger.exception("Unable to save Facebook radius diagnostics run=%s", self.run_id)
            return
        logger.warning("Facebook radius diagnostics run=%s saved to %s", self.run_id, directory)
        try:
            await page.screenshot(path=str(directory / "page.png"), timeout=5000)
        except Exception:
            logger.exception("Unable to save Facebook radius screenshot run=%s", self.run_id)

    async def _wait_for_search_radius(self, page: Page, url: str) -> None:
        timeout = self._settings.facebook_page_timeout_seconds
        radius = self._settings.facebook_radius_km
        text = ""
        try:
            async with asyncio.timeout(timeout):
                for _ in range(timeout * 2):
                    # Check login redirects and restrictions on every poll, not only after waiting.
                    text = await check_facebook_page(page)
                    if has_radius_confirmation(text, radius):
                        return
                    await page.wait_for_timeout(500)
        except (TimeoutError, PlaywrightTimeoutError):
            pass
        await self._save_radius_diagnostics(page, url, text)
        if not text.strip():
            raise PlaywrightTimeoutError(
                f"Facebook search page did not load within {timeout} seconds"
            )
        raise ValueError(f"Facebook did not confirm the configured {radius} km search radius")

    async def _read_candidate_page(
        self, page: Page, url: str, *, require_radius: bool, browse: bool = False,
    ):
        limit = self._settings.facebook_history_limit
        candidates = {}
        async with FeedCapture(page, limit * 2) as capture:
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except PlaywrightError:
                try:
                    await check_facebook_page(page)
                except PlaywrightError:
                    pass
                raise
            if require_radius:
                await self._wait_for_search_radius(page, url)
            else:
                await page.wait_for_timeout(1500)
                await check_facebook_page(page)
            await capture.embedded()
            unchanged = 0
            previous_progress = (0, 0)
            scroll_limit = (min(self._settings.facebook_max_scrolls, random.randint(2, 4))
                            if browse else self._settings.facebook_max_scrolls)
            for _ in range(scroll_limit):
                await check_facebook_page(page)
                await capture.drain()
                for item in capture.candidates.values():
                    if is_local_marketplace_label(item["feed_location"]):
                        candidates[item["id"]] = item
                # DOM fills only missing IDs; network data keeps its structured price.
                selector = ('a[href*="/marketplace/item/"][href*="category_feed"]'
                            if require_radius else '[role="main"] a[href*="/marketplace/item/"]')
                rows = await page.locator(selector).evaluate_all(
                    """elements => elements.map(element => ({
                        href: element.href,
                        label: element.getAttribute('aria-label') || element.innerText || ''
                    }))"""
                )
                for row in rows:
                    match = re.search(r"/marketplace/item/(\d+)/", row["href"])
                    if match and is_local_marketplace_label(row["label"]):
                        item_id = match.group(1)
                        candidates.setdefault(item_id, {
                            "id": item_id,
                            "url": f"https://www.facebook.com/marketplace/item/{item_id}/",
                            "feed_label": row["label"],
                        })
                progress = (len(candidates), capture.batches)
                unchanged = unchanged + 1 if progress == previous_progress else 0
                previous_progress = progress
                if len(candidates) >= limit or capture.finished or unchanged >= 2:
                    break
                await page.mouse.wheel(0, random.randint(350, 900) if browse else 4000)
                await page.wait_for_timeout(random.randint(500, 1200) if browse else 1000)
            logger.info("Facebook feed run=%s candidates=%d json=%d", self.run_id,
                        len(candidates), len(capture.candidates))
            return list(candidates.values())[:limit]

    async def _fetch_detail(
        self,
        page: Page,
        candidate: dict[str, Any],
    ) -> RawAdvertisement | None:
        try:
            navigation_timeout = min(
                self._settings.facebook_page_timeout_seconds,
                10,
            ) * 1000
            await page.goto(
                candidate["url"],
                wait_until="commit",
                timeout=navigation_timeout,
            )
            await page.wait_for_timeout(1200)
            body = await check_facebook_page(page)
            title_locator = page.locator('[role="main"] h1, [role="dialog"] h1').last
            title = (await title_locator.inner_text(timeout=15000)).strip()
            detail = parse_detail_text(body, title)
            # The heading can render before the publication metadata.
            for _ in range(2):
                if detail["published_label"]:
                    break
                await page.wait_for_timeout(1500)
                body = await check_facebook_page(page)
                detail = parse_detail_text(body, title)
        except PlaywrightError as error:
            # Navigation or heading timeouts can conceal a challenge page.
            try:
                await check_facebook_page(page)
            except PlaywrightError:
                pass
            self._record_outcome(candidate, "read_error")
            logger.warning("Unable to read Facebook item %s: %s", candidate["id"], error)
            return None

        published_label = detail["published_label"]
        age_minutes = relative_age_minutes(published_label or "")
        collected_at = datetime.now(self._timezone)
        # Unknown publication time is not a rejection: freshness falls back to detection.
        published_at = (collected_at - timedelta(minutes=age_minutes)
                        if age_minutes is not None else None)
        description = detail["description"]
        price_label = marketplace_price_label(body, title)
        text = listing_text(title, description, price_label)
        if not text:
            self._record_outcome(candidate, "empty_text", published_label)
            return None
        seller_profile_url = None
        if self._settings.facebook_seller_listings_enabled:
            href = await page.evaluate("""() => {
                const root = document.querySelector('[role="dialog"]') ||
                             document.querySelector('[role="main"]');
                return root?.querySelector('a[href*="/marketplace/profile/"]')?.href || null;
            }""")
            if isinstance(href, str):
                parsed = urlsplit(href)
                match = re.fullmatch(r"/marketplace/profile/(\d+)/?", parsed.path)
                if parsed.hostname in {"www.facebook.com", "facebook.com"} and match:
                    seller_profile_url = f"https://www.facebook.com/marketplace/profile/{match[1]}/"
        advertisement = RawAdvertisement(
            source=self.name,
            external_id=candidate["id"],
            url=candidate["url"],
            title=title,
            text=text,
            author_name=detail["seller_name"],
            published_at=published_at,
            collected_at=collected_at,
            raw_data={
                **await browser_photos(page),
                "seller_profile_url": seller_profile_url,
                "feed_kinds": sorted(candidate["feed_kinds"]),
                "feed_label": candidate["feed_label"],
                "marketplace_price_label": price_label,
                "published_label": published_label,
                "published_at_is_approximate": True,
                "search_location": self._settings.facebook_location_name,
                "search_location_id": self._settings.facebook_location_id,
                "search_radius_km": self._settings.facebook_radius_km,
                "search_days_since_listed": 1,
                "search_sort": "creation_time_descend",
                "collected_via": "playwright",
            },
        )

        self._record_outcome(candidate, "accepted")
        return advertisement

    def _record_outcome(
        self, candidate: dict[str, Any], reason: str, published_label: str | None = None,
    ) -> None:
        self._outcomes[reason] += 1
        if reason != "accepted":
            # Only the bounded date label is needed; never log page bodies or seller details.
            logger.info(
                "Facebook item skipped run=%s item=%s reason=%s published_label=%s",
                self.run_id, candidate["id"], reason,
                json.dumps(published_label[:200] if published_label else None, ensure_ascii=True),
            )


class FacebookSnapshotCollector:
    """Read a browser-exported Facebook Marketplace NDJSON snapshot."""

    name = "facebook_marketplace"

    def __init__(
        self,
        path: Path,
        *,
        location: str = "Pattaya",
        radius_km: int = 10,
        timezone: str = "Asia/Bangkok",
        today: date | None = None,
    ) -> None:
        self._path = path
        self._location = location.casefold()
        self._radius_km = radius_km
        self._timezone = ZoneInfo(timezone)
        self._today = today

    async def fetch(self) -> list[RawAdvertisement]:
        target_date = self._today or datetime.now(self._timezone).date()
        advertisements: list[RawAdvertisement] = []
        for line_number, line in enumerate(self._path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                advertisement = RawAdvertisement.model_validate(payload)
            except (json.JSONDecodeError, ValueError) as error:
                raise ValueError(
                    f"Invalid Facebook snapshot row at line {line_number}: {error}"
                ) from error
            if self._matches_scope(advertisement, target_date):
                advertisements.append(advertisement)
        return advertisements

    def _matches_scope(self, advertisement: RawAdvertisement, target_date: date) -> bool:
        if advertisement.source != self.name or advertisement.published_at is None:
            return False
        published_date = advertisement.published_at.astimezone(self._timezone).date()
        if published_date != target_date:
            return False
        metadata = advertisement.raw_data or {}
        location = str(metadata.get("search_location", "")).casefold()
        return (
            location == self._location
            and metadata.get("search_radius_km") == self._radius_km
        )
