"""Discovery and profile regression tests; browser traffic is fulfilled locally."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import async_playwright

from facebook_marketplace_scraper.collector import FacebookMarketplaceCollector, FacebookRestrictionError
from facebook_marketplace_scraper.feed import FeedCapture, candidate_from_node, json_documents
from facebook_marketplace_scraper.config import Settings
from facebook_marketplace_scraper.profile import profile_lock


def listing(identifier, amount=10000, city="Pattaya"):
    return {
        "id": identifier, "marketplace_listing_title": "Condo",
        "listing_price": {"amount": str(amount), "currency": "THB", "formatted_amount": f"฿{amount}"},
        "location": {"reverse_geocode": {"city": city}},
    }


def batch(*nodes, more=True):
    return {"data": {"marketplace_search": {"feed_units": {
        "edges": [{"node": {"listing": node}} for node in nodes],
        "page_info": {"has_next_page": more},
    }}}}


def test_json_stream_ignores_recommendations_and_invalid_nodes():
    capture = FeedCapture(None, 10)
    body = 'for (;;);\n' + json.dumps(batch(listing("1"))) + '\ninvalid\n' + json.dumps({
        **batch(listing("2"), listing("1"), {"id": "3"}, more=False),
        "recommendations": {"listing": listing("999")},
    })
    for doc in json_documents(body):
        capture.absorb(doc)
    assert set(capture.candidates) == {"1", "2"}
    assert capture.candidates["1"]["price_amount"] == 10000
    assert capture.finished
    assert candidate_from_node(listing("invalid")) is None
    assert candidate_from_node(listing("4", amount="NaN"))["price_amount"] is None
    assert json_documents(json.dumps(batch(listing("5")), indent=2))


async def test_price_ranges_are_bounded_disjoint_and_deduplicated(monkeypatch):
    settings = Settings(_env_file=None, facebook_price_split_threshold=2,
                        facebook_price_split_max_searches=2)
    collector = FacebookMarketplaceCollector(settings)
    items = [candidate_from_node(listing(str(i), i * 10000)) for i in (1, 2, 3)]
    search = AsyncMock(side_effect=[items[:2], items[:1], items[1:]])
    monkeypatch.setattr(collector, "_collect_candidates", search)
    result = await collector._search_category(AsyncMock(), "propertyrentals")
    assert [item["id"] for item in result] == ["1", "2", "3"]
    assert search.call_args_list[1].kwargs == {"min_price": 0, "max_price": 10000}
    assert search.call_args_list[2].kwargs == {"min_price": 10001, "max_price": None}
    assert search.await_count == 3


async def test_price_split_requires_reliable_prices(monkeypatch):
    collector = FacebookMarketplaceCollector(Settings(_env_file=None, facebook_price_split_threshold=2))
    items = [{"id": "1"}, {"id": "2"}]
    search = AsyncMock(return_value=items)
    monkeypatch.setattr(collector, "_collect_candidates", search)
    assert await collector._search_category(AsyncMock(), "propertyrentals") == items
    search.assert_awaited_once()


@pytest.mark.parametrize("detail_limit, expected", [(1, ["1"]), (5, ["1", "2"])])
@pytest.mark.parametrize("with_browsing", [False, True])
async def test_seller_discovery_uses_shared_deduplication_and_due_checks(
    monkeypatch, detail_limit, expected, with_browsing,
):
    collector = FacebookMarketplaceCollector(Settings(_env_file=None, facebook_detail_limit=detail_limit,
                                                               facebook_photo_browsing_enabled=with_browsing))
    from facebook_marketplace_scraper import collector as facebook
    browse = AsyncMock()
    monkeypatch.setattr(facebook, "browse_listing_photos", browse)
    monkeypatch.setattr(facebook.random, "random", lambda: 0.0)
    feed = MagicMock(close=AsyncMock(), is_closed=lambda: True)
    detail = MagicMock(close=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(side_effect=[feed, detail]))
    monkeypatch.setattr(collector, "_search_category", AsyncMock(side_effect=[
        [{"id": "1"}, {"id": "9"}], [{"id": "1"}],
    ]))
    fetch = AsyncMock(return_value=SimpleNamespace(raw_data={
        "seller_profile_url": "https://www.facebook.com/marketplace/profile/10/",
    }))
    monkeypatch.setattr(collector, "_fetch_detail", fetch)
    seller = AsyncMock(return_value=[{"id": "1"}, {"id": "2"}, {"id": "9"}, {"id": "8"}])
    monkeypatch.setattr(collector, "_collect_seller_candidates", seller)
    check = AsyncMock()
    await collector._fetch_from_context(context, {"8", "9"}, check_known=check)
    assert [call.args[1]["id"] for call in fetch.call_args_list] == expected
    seller.assert_awaited_once()
    assert browse.await_count == int(with_browsing and detail_limit > 1)
    check.assert_awaited_once_with(context, {"8", "9"})
    if detail_limit > 1:
        assert fetch.call_args_list[1].args[1]["feed_kinds"] == set()


def test_profile_lock_rejects_simultaneous_login_or_worker(tmp_path):
    with (profile_lock(tmp_path), pytest.raises(RuntimeError, match="already in use"),
          profile_lock(tmp_path)):
        pass
    with profile_lock(tmp_path):
        pass


@pytest.fixture
async def offline_page():
    executable = os.environ.get("TEST_BROWSER_EXECUTABLE")
    if not executable:
        pytest.skip("Set TEST_BROWSER_EXECUTABLE for offline browser tests")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=executable, headless=True)
        context = await browser.new_context(service_workers="block")
        await context.route("**/*", lambda route: route.fulfill(body="", content_type="text/html"))
        page = await context.new_page()
        yield page
        await browser.close()


async def test_embedded_network_json_and_html_fallback(offline_page):
    page = offline_page
    html = '''<body>Within 10 km<div style="height:5000px">
    <a href="/marketplace/item/2/?category_feed=1" aria-label="Pattaya condo">Condo</a>
    <a href="/marketplace/item/1/?category_feed=1" aria-label="Pattaya condo">Condo</a>
    <script type="application/json">PAYLOAD</script>
    <script>fetch('/api/graphql/', {method:'POST'});</script></div></body>'''.replace(
        "PAYLOAD", json.dumps(batch(listing("1"))),
    )
    async def respond(route):
        if "/api/graphql" in route.request.url:
            await route.fulfill(content_type="application/json", body=json.dumps(batch(
                listing("3"), listing("4", city="Bangkok"), more=False,
            )))
        else:
            await route.fulfill(content_type="text/html", body=html)
    await page.route("**/*", respond)
    collector = FacebookMarketplaceCollector(Settings(_env_file=None))
    found = await collector._collect_candidates(page, "propertyrentals")
    assert {item["id"] for item in found} == {"1", "2", "3"}
    assert next(item for item in found if item["id"] == "1")["price_amount"] == 10000


async def test_html_only_fallback_and_seller_local_filter(offline_page):
    await offline_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='''
      <main role="main"><a href="/marketplace/item/2/" aria-label="Pattaya condo">Condo</a>
      <a href="/marketplace/item/3/" aria-label="Bangkok condo">Condo</a></main>'''))
    collector = FacebookMarketplaceCollector(Settings(_env_file=None))
    result = await collector._collect_seller_candidates(
        offline_page, "https://www.facebook.com/marketplace/profile/10/",
    )
    assert [item["id"] for item in result] == ["2"]


async def test_warning_during_seller_enrichment_is_not_swallowed(offline_page):
    await offline_page.route("**/*", lambda route: route.fulfill(
        body="We suspect automated behavior on your account", content_type="text/html",
    ))
    collector = FacebookMarketplaceCollector(Settings(_env_file=None))
    with pytest.raises(FacebookRestrictionError):
        await collector._collect_seller_candidates(
            offline_page, "https://www.facebook.com/marketplace/profile/10/",
        )


async def test_profile_bootstrap_once_and_reuse_without_storage_state(tmp_path):
    executable = os.environ.get("TEST_BROWSER_EXECUTABLE")
    if not executable:
        pytest.skip("Set TEST_BROWSER_EXECUTABLE for offline browser tests")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies": [], "origins": [{
        "origin": "https://example.invalid", "localStorage": [{"name": "test", "value": "seed"}],
    }]}))
    profile = tmp_path / "profile"
    settings = Settings(_env_file=None, facebook_profile_dir=str(profile),
                        facebook_storage_state=str(state), facebook_browser_executable=executable)
    collector = FacebookMarketplaceCollector(settings)
    for expected in ("seed", "refreshed"):
        async with collector.browser_context(persist_storage_state=False) as context:
            await context.route("**/*", lambda route: route.fulfill(body="<html></html>", content_type="text/html"))
            page = await context.new_page()
            await page.goto("https://example.invalid")
            assert await page.evaluate("localStorage.getItem('test')") == expected
            await page.evaluate("localStorage.setItem('test', 'refreshed')")
        state.unlink(missing_ok=True)
    assert (profile / ".initialized").is_file()


async def test_capture_detaches_listener_and_cancels_pending_body_on_error():
    import asyncio

    page = MagicMock()
    capture = FeedCapture(page, 10)
    pending_read = asyncio.Event()
    response = SimpleNamespace(url="https://www.facebook.com/api/graphql/", text=pending_read.wait)
    with pytest.raises(RuntimeError, match="stop"):
        async with capture:
            capture.on_response(response)
            raise RuntimeError("stop")
    page.remove_listener.assert_called_once_with("response", capture.on_response)
    assert all(task.cancelled() for task in capture.tasks)


async def test_disabled_expansions_do_not_visit_extra_pages(monkeypatch):
    collector = FacebookMarketplaceCollector(Settings(
        _env_file=None, facebook_price_split_enabled=False, facebook_seller_listings_enabled=False,
    ))
    search = AsyncMock(return_value=[candidate_from_node(listing("1"))])
    monkeypatch.setattr(collector, "_collect_candidates", search)
    assert len(await collector._search_category(AsyncMock(), "propertyrentals")) == 1
    search.assert_awaited_once()
    feed = MagicMock(close=AsyncMock(), is_closed=lambda: True)
    detail = MagicMock(close=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(side_effect=[feed, detail]))
    monkeypatch.setattr(collector, "_search_category", AsyncMock(return_value=[{"id": "1"}]))
    monkeypatch.setattr(collector, "_fetch_detail", AsyncMock(return_value=SimpleNamespace(raw_data={
        "seller_profile_url": "https://www.facebook.com/marketplace/profile/10/",
    })))
    seller = AsyncMock()
    monkeypatch.setattr(collector, "_collect_seller_candidates", seller)
    await collector._fetch_from_context(context, set())
    seller.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
async def test_additional_seller_preview_reuses_queue_and_limits(monkeypatch, enabled):
    from facebook_marketplace_scraper import collector as facebook

    collector = FacebookMarketplaceCollector(Settings(
        _env_file=None, facebook_detail_limit=3, facebook_photo_browsing_enabled=False,
        facebook_seller_browsing_enabled=enabled, facebook_seller_browsing_max_cards=2,
    ))
    feed = MagicMock(close=AsyncMock(), is_closed=lambda: True)
    detail = MagicMock(close=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(side_effect=[feed, detail]))
    monkeypatch.setattr(collector, "_search_category", AsyncMock(side_effect=[
        [{"id": "1", "additional_visit": True}], [],
    ]))
    fetch = AsyncMock(return_value=SimpleNamespace(raw_data={
        "seller_profile_url": "https://www.facebook.com/marketplace/profile/10/",
    }))
    monkeypatch.setattr(collector, "_fetch_detail", fetch)
    seller = AsyncMock(return_value=[
        {"id": "1"}, {"id": "2"}, {"id": "2"}, {"id": "9"}, {"id": "3"}, {"id": "4"},
    ])
    monkeypatch.setattr(collector, "_collect_seller_candidates", seller)
    monkeypatch.setattr(facebook.random, "randint", lambda lo, hi: hi)
    monkeypatch.setattr(facebook.random, "sample", lambda items, count: items[-count:])
    check = AsyncMock()
    result = await collector._fetch_from_context(context, {"9"}, check_known=check)
    assert len(result) == 3
    assert [call.args[1]["id"] for call in fetch.call_args_list] == (
        ["1", "3", "4"] if enabled else ["1", "2", "3"]
    )
    seller.assert_awaited_once_with(
        detail, "https://www.facebook.com/marketplace/profile/10/", browse=enabled,
    )
    check.assert_awaited_once_with(context, {"9"})
