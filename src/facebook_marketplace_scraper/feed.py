"""Read Marketplace search batches already delivered to the browser.

The response shapes were studied in danyk20/facebook-marketplace-scraper;
this implementation uses our asynchronous browser and bounded collection policy.
"""

import asyncio
import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError


def json_documents(body):
    body = body.strip().removeprefix("for (;;);").strip()
    try:
        return [json.loads(body)]
    except (ValueError, RecursionError):
        result = []
        for line in body.splitlines():
            try:
                result.append(json.loads(line))
            except (ValueError, RecursionError):
                continue
        return result


def search_feeds(value):
    # Only search results: recommendations and seller/user objects are not candidates.
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            search = item.get("marketplace_search")
            if isinstance(search, dict) and isinstance(search.get("feed_units"), dict):
                yield search["feed_units"]
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def candidate_from_node(node):
    if not isinstance(node, dict):
        return None
    identifier = str(node.get("id", ""))
    price = node.get("listing_price")
    if not identifier.isdecimal() or not isinstance(price, dict):
        return None
    location = node.get("location")
    location = location if isinstance(location, dict) else {}
    geo = location.get("reverse_geocode")
    geo = geo if isinstance(geo, dict) else {}
    city_page = geo.get("city_page")
    city_page = city_page if isinstance(city_page, dict) else {}
    label = " ".join(str(part) for part in (
        node.get("marketplace_listing_title"), price.get("formatted_amount"),
        geo.get("city"), geo.get("state"), city_page.get("display_name"),
    ) if part)
    amount = None
    try:
        number = Decimal(str(price.get("amount")))
        if number.is_finite() and 0 <= number <= 1_000_000_000_000 and number == number.to_integral_value():
            amount = int(number)
    except InvalidOperation:
        pass
    return {
        "id": identifier,
        "url": f"https://www.facebook.com/marketplace/item/{identifier}/",
        "feed_label": label,
        "feed_location": " ".join(str(v) for v in (
            geo.get("city"), geo.get("state"), city_page.get("display_name"),
        ) if v),
        "price_amount": amount,
        "price_currency": price.get("currency"),
    }


class FeedCapture:
    def __init__(self, page, limit):
        self.page = page
        self.limit = limit
        self.candidates = {}
        self.tasks = set()
        self.batches = 0
        self.finished = False

    def absorb(self, document):
        for feed in search_feeds(document):
            self.batches += 1
            info = feed.get("page_info")
            self.finished = self.finished or (
                isinstance(info, dict) and info.get("has_next_page") is False
            )
            edges = feed.get("edges")
            for edge in edges if isinstance(edges, list) else []:
                node = edge.get("node") if isinstance(edge, dict) else None
                candidate = candidate_from_node(node.get("listing")) if isinstance(node, dict) else None
                if candidate and len(self.candidates) < self.limit:
                    self.candidates.setdefault(candidate["id"], candidate)

    async def read(self, response):
        try:
            body = await response.text()
            if len(body) <= 5_000_000:
                for document in json_documents(body):
                    self.absorb(document)
        except PlaywrightError:
            # A navigation can discard a body; rendered tiles remain the fallback.
            pass

    def on_response(self, response):
        url = urlsplit(response.url)
        if (url.hostname in {"www.facebook.com", "facebook.com"}
                and url.path.rstrip("/") == "/api/graphql" and len(self.tasks) < 20):
            task = asyncio.create_task(self.read(response))
            self.tasks.add(task)

    async def drain(self):
        if self.tasks:
            pending = list(self.tasks)
            try:
                await asyncio.wait_for(asyncio.gather(*pending), timeout=10)
            except TimeoutError:
                pass
            finally:
                self.tasks.difference_update(pending)

    async def embedded(self):
        for body in await self.page.locator('script[type="application/json"]').all_text_contents():
            if len(body) <= 5_000_000 and "marketplace_search" in body:
                for document in json_documents(body):
                    self.absorb(document)

    async def __aenter__(self):
        self.page.on("response", self.on_response)
        return self

    async def __aexit__(self, *_):
        self.page.remove_listener("response", self.on_response)
        # Do not let response readers from an old page leak into the next search.
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
