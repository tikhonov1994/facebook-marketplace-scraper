from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from facebook_marketplace_scraper.collector import (
    FacebookSnapshotCollector,
    has_radius_confirmation,
    is_local_marketplace_label,
    parse_detail_text,
    relative_age_minutes,
    save_refreshed_storage_state,
)
from facebook_marketplace_scraper.models import RawAdvertisement


def _advertisement(
    *,
    published_at: datetime,
    location: str = "Pattaya",
    radius_km: int = 10,
) -> RawAdvertisement:
    return RawAdvertisement(
        source="facebook_marketplace",
        external_id="123",
        url="https://www.facebook.com/marketplace/item/123/",
        title="Condo for sale",
        text="Condo for sale in Pattaya",
        published_at=published_at,
        raw_data={"search_location": location, "search_radius_km": radius_km},
    )


def _write_snapshot(path: Path, advertisements: list[RawAdvertisement]) -> None:
    path.write_text("\n".join(item.model_dump_json() for item in advertisements))


@pytest.mark.asyncio
async def test_collector_accepts_only_today_in_pattaya_at_ten_km(tmp_path: Path) -> None:
    snapshot = tmp_path / "facebook.ndjson"
    today = _advertisement(published_at=datetime(2026, 8, 26, 3, tzinfo=UTC))
    yesterday = _advertisement(published_at=datetime(2026, 8, 25, 3, tzinfo=UTC))
    wrong_radius = _advertisement(
        published_at=datetime(2026, 8, 26, 4, tzinfo=UTC),
        radius_km=20,
    )
    wrong_location = _advertisement(
        published_at=datetime(2026, 8, 26, 5, tzinfo=UTC),
        location="Bangkok",
    )
    _write_snapshot(snapshot, [today, yesterday, wrong_radius, wrong_location])

    result = await FacebookSnapshotCollector(snapshot, today=date(2026, 8, 26)).fetch()

    assert result == [today]


@pytest.mark.asyncio
async def test_collector_reports_invalid_row_number(tmp_path: Path) -> None:
    snapshot = tmp_path / "facebook.ndjson"
    snapshot.write_text("{}\nnot-json")

    with pytest.raises(ValueError, match="line 1"):
        await FacebookSnapshotCollector(snapshot, today=date(2026, 8, 26)).fetch()


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Опубликовано 13 минут назад", 13),
        ("Опубликовано около часа назад", 60),
        ("Listed 3 hours ago", 180),
        ("Listed yesterday", 1440),
    ],
)
def test_relative_age_minutes(label: str, expected: int | None) -> None:
    assert relative_age_minutes(label) == expected


def test_local_marketplace_label_accepts_pattaya_area_only() -> None:
    assert is_local_marketplace_label("Condo, 2,000,000 THB, Pattaya")
    assert is_local_marketplace_label("House, 50,000 THB, Bang Lamung")
    assert not is_local_marketplace_label("Condo, 30,000 THB, Bangkok")


@pytest.mark.parametrize(
    "label",
    ["Паттайя · В радиусе 10 км", "Pattaya · Within 10 km", "พัทยา · 10 กม."],
)
def test_radius_confirmation_handles_marketplace_locales(label: str) -> None:
    assert has_radius_confirmation(label, 10)


@pytest.mark.parametrize(
    ("label", "radius_km"),
    [
        ("Паттайя\n\u00a0\n · В радиусе 6 миль", 10),
        ("Pattaya · Within 6 miles", 10),
        ("Pattaya · Within 6 mi", 10),
        ("พัทยา · 6 ไมล์", 10),
        ("Pattaya · Within 1 mile", 2),
        ("Паттайя · В радиусе 3 мили", 5),
        ("Pattaya · Within 12 miles", 20),
    ],
)
def test_radius_confirmation_accepts_rounded_miles(label: str, radius_km: int) -> None:
    assert has_radius_confirmation(label, radius_km)


@pytest.mark.parametrize(
    "label",
    [
        "Паттайя · В радиусе 6 км",
        "Pattaya · Within 10 miles",
        "Pattaya · Within 5 miles",
        "Pattaya · Within 7 miles",
        "Pattaya · Within 16 miles",
        "Pattaya · Within 6.5 miles",
        "Паттайя · В радиусе 6,5 миль",
        "Pattaya · Within 110 km",
        "Pattaya · Within 10.5 km",
        "Pattaya · Within 1.10 km",
        "Pattaya · Within 6 minutes",
        "Паттайя",
    ],
)
def test_radius_confirmation_rejects_other_distances(label: str) -> None:
    assert not has_radius_confirmation(label, 10)


def test_parse_detail_text_extracts_listing_fields() -> None:
    body = """Marketplace
Condo for sale
2 000 000 THB
Опубликовано 2 часа назад
Описание
Sea-view condo in Jomtien
Ещё
Информация о продавце
Информация о продавце
Jane Agent
На Facebook с 2020 года
"""

    result = parse_detail_text(body, "Condo for sale")

    assert result == {
        "published_label": "Опубликовано 2 часа назад",
        "description": "Sea-view condo in Jomtien",
        "seller_name": "Jane Agent",
    }


class _StorageStateContext:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def storage_state(self, *, path: str) -> None:
        Path(path).write_text('{"cookies":[{"name":"refreshed"}]}')
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_refreshed_storage_state_replaces_session_atomically(tmp_path: Path) -> None:
    destination = tmp_path / "storage_state.json"
    destination.write_text('{"cookies":[{"name":"old"}]}')

    await save_refreshed_storage_state(_StorageStateContext(), destination)  # type: ignore[arg-type]

    assert destination.read_text() == '{"cookies":[{"name":"refreshed"}]}'
    assert destination.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / ".storage_state.json.tmp").exists()


@pytest.mark.asyncio
async def test_failed_session_refresh_preserves_previous_session(tmp_path: Path) -> None:
    destination = tmp_path / "storage_state.json"
    destination.write_text('{"cookies":[{"name":"old"}]}')

    with pytest.raises(OSError, match="write failed"):
        await save_refreshed_storage_state(  # type: ignore[arg-type]
            _StorageStateContext(error=OSError("write failed")),
            destination,
        )

    assert destination.read_text() == '{"cookies":[{"name":"old"}]}'
    assert not (tmp_path / ".storage_state.json.tmp").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_new", [False, True])
@pytest.mark.parametrize("with_checks", [False, True])
async def test_known_listings_are_seen_without_opening_details(monkeypatch, include_new, with_checks):
    from unittest.mock import AsyncMock, MagicMock

    from facebook_marketplace_scraper.collector import FacebookMarketplaceCollector
    from facebook_marketplace_scraper.config import Settings

    collector = FacebookMarketplaceCollector(Settings())
    feed_page = MagicMock()
    feed_page.close = AsyncMock()
    feed_page.is_closed.return_value = True
    detail_page = MagicMock()
    detail_page.close = AsyncMock()
    context = MagicMock()
    context.new_page = AsyncMock(side_effect=[feed_page, detail_page])

    # The same listing can appear in both categories; only new IDs need a detail read.
    rows = [{"id": "known"}]
    if include_new:
        rows.append({"id": "new"})
    monkeypatch.setattr(collector, "_collect_candidates", AsyncMock(side_effect=[
        [dict(row) for row in rows], [dict(row) for row in rows],
    ]))
    advertisement = _advertisement(published_at=datetime(2026, 10, 21, tzinfo=UTC))
    fetch_detail = AsyncMock(return_value=advertisement)
    monkeypatch.setattr(collector, "_fetch_detail", fetch_detail)

    check_known = AsyncMock() if with_checks else None
    result = await collector._fetch_from_context(
        context, {"known", "not_in_feed"}, check_known=check_known
    )
    if with_checks:
        check_known.assert_awaited_once_with(context, {"known"})

    assert collector.seen_external_ids == {"known"}
    if include_new:
        assert result == [advertisement]
        fetch_detail.assert_awaited_once_with(
            detail_page, {"id": "new", "feed_kinds": {"rent", "sale"}},
        )
        assert context.new_page.await_count == 2
    else:
        assert result == []
        fetch_detail.assert_not_awaited()
        context.new_page.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "reason", "accepted"),
    [
        ("", "accepted", True),
        ("Listed yesterday", "accepted", True),
        ("Listed 48 hours ago", "accepted", True),
        ("Опубликовано 18 часов назад", "accepted", True),
        ("Опубликовано около часа назад", "accepted", True),
        ("Опубликовано около дня назад", "accepted", True),
        ("Listed just now", "accepted", True),
    ],
)
async def test_detail_diagnostics_preserve_filtering(label, reason, accepted, caplog):
    from unittest.mock import AsyncMock, MagicMock

    from facebook_marketplace_scraper.collector import FacebookMarketplaceCollector
    from facebook_marketplace_scraper.config import Settings

    collector = FacebookMarketplaceCollector(Settings())
    page = MagicMock()
    page.url = "https://www.facebook.com/marketplace/item/123/"
    page.goto = AsyncMock()
    page.evaluate = AsyncMock(return_value={"urls": [], "complete": False})
    page.wait_for_timeout = AsyncMock()
    title = MagicMock()
    title.last.inner_text = AsyncMock(return_value="Private title")
    body = MagicMock()
    body.inner_text = AsyncMock(
        return_value=f"Private title\n{label}\nDescription\nPrivate description"
    )
    page.locator.side_effect = lambda selector: title if "h1" in selector else body
    candidate = {
        "id": "123", "url": "https://www.facebook.com/marketplace/item/123/",
        "feed_kinds": {"rent"}, "feed_label": "Pattaya",
    }
    with caplog.at_level("INFO"):
        result = await collector._fetch_detail(page, candidate)

    if not label:
        assert result.published_at is None
    assert (result is not None) == accepted
    assert dict(collector._outcomes) == {reason: 1}
    if not accepted:
        assert f"reason={reason}" in caplog.text
        assert "item=123" in caplog.text
    assert "Private title" not in caplog.text
    assert "Private description" not in caplog.text


@pytest.mark.asyncio
async def test_detail_waits_for_late_date_and_accepts_previous_day(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from facebook_marketplace_scraper import collector as facebook
    from facebook_marketplace_scraper.config import Settings

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 11, 1, tzinfo=tz)

    monkeypatch.setattr(facebook, "datetime", Clock)
    collector = facebook.FacebookMarketplaceCollector(Settings())
    page = MagicMock()
    page.url = "https://www.facebook.com/marketplace/item/123/"
    page.goto = AsyncMock()
    page.evaluate = AsyncMock(return_value={"urls": [], "complete": False})
    page.wait_for_timeout = AsyncMock()
    heading = MagicMock()
    heading.last.inner_text = AsyncMock(return_value="Condo")
    body = MagicMock()
    body.inner_text = AsyncMock(side_effect=[
        "Condo", "Condo\nОпубликовано 18 часов назад\nОписание\nSea view",
    ])
    page.locator.side_effect = lambda selector: heading if "h1" in selector else body
    result = await collector._fetch_detail(page, {
        "id": "123", "url": "https://www.facebook.com/marketplace/item/123/",
        "feed_kinds": {"rent"}, "feed_label": "Pattaya",
    })
    assert result is not None
    assert result.published_at.date() == date(2026, 9, 10)
    assert result.text == "Condo\n\nSea view"
    assert body.inner_text.await_count == 2
    assert dict(collector._outcomes) == {"accepted": 1}


@pytest.mark.parametrize("label,minutes", [
    ("Опубликовано 6 дней назад", 8640),
    ("Опубликовано 4 дня назад", 5760),
    ("Опубликовано около дня назад · Доступно сейчас", 1440),
    ("Listed yesterday", 1440),
    ("Listed 2 days ago", 2880),
    ("ลงประกาศ 3 วัน", 4320),
    ("Unknown", None),
])
def test_relative_days(label, minutes):
    from facebook_marketplace_scraper.collector import relative_age_minutes
    assert relative_age_minutes(label) == minutes


@pytest.mark.parametrize(("url", "body"), [
    ("https://www.facebook.com/checkpoint/123", ""),
    ("https://www.facebook.com/challenge/", ""),
    ("https://www.facebook.com/marketplace/", "We suspect automated behavior on your account"),
    ("https://www.facebook.com/marketplace/", "Подтвердите, что вы не робот"),
    ("https://www.facebook.com/marketplace/", "Ваш аккаунт временно заблокирован"),
])
def test_restriction_signals_raise(url, body):
    from facebook_marketplace_scraper.collector import FacebookRestrictionError, check_facebook_restriction

    with pytest.raises(FacebookRestrictionError):
        check_facebook_restriction(url, body)


def test_ordinary_listing_is_not_a_restriction():
    from facebook_marketplace_scraper.collector import check_facebook_restriction

    check_facebook_restriction(
        "https://www.facebook.com/marketplace/item/123/", "Condo with automated lighting",
    )


def test_russian_automation_warning_from_screenshot():
    from facebook_marketplace_scraper.collector import FacebookRestrictionError, check_facebook_restriction

    with pytest.raises(FacebookRestrictionError, match="restriction_notice"):
        check_facebook_restriction(
            "https://www.facebook.com/marketplace/",
            "Мы заподозрили автоматизированное\nповедение в вашем аккаунте\n"
            "To prevent your account from being temporarily restricted or permanently "
            "disabled, ensure that no other users or tools have access to your account "
            "and that you're following our Terms of Use. Also consider changing your "
            "password to a stronger one to prevent unauthorized access to your "
            "account by third parties.\nОтклонить",
        )


@pytest.mark.asyncio
async def test_detail_restriction_aborts_before_reading_heading():
    from unittest.mock import AsyncMock, MagicMock

    from facebook_marketplace_scraper.collector import FacebookMarketplaceCollector, FacebookRestrictionError
    from facebook_marketplace_scraper.config import Settings

    page = MagicMock()
    page.url = "https://www.facebook.com/marketplace/item/123/"
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.locator.return_value.inner_text = AsyncMock(
        return_value="We suspect automated behavior on your account",
    )
    collector = FacebookMarketplaceCollector(Settings())
    with pytest.raises(FacebookRestrictionError):
        await collector._fetch_detail(page, {"id": "123", "url": page.url})
    assert not collector._outcomes["read_error"]
    page.locator.assert_called_once_with("body")


@pytest.mark.asyncio
async def test_feed_navigation_timeout_still_detects_checkpoint():
    from unittest.mock import AsyncMock, MagicMock

    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    from facebook_marketplace_scraper.collector import FacebookMarketplaceCollector, FacebookRestrictionError
    from facebook_marketplace_scraper.config import Settings

    page = MagicMock()
    page.url = "https://www.facebook.com/checkpoint/123"
    page.goto = AsyncMock(side_effect=PlaywrightTimeoutError("navigation timed out"))
    collector = FacebookMarketplaceCollector(Settings())
    with pytest.raises(FacebookRestrictionError):
        await collector._collect_candidates(page, "propertyrentals")
    page.locator.assert_not_called()


@pytest.mark.parametrize("failure", [None, "screenshot", "directory"])
async def test_radius_failure_saves_diagnostics_without_masking_error(tmp_path, failure):
    import json
    from unittest.mock import AsyncMock, MagicMock

    from playwright.async_api import Error as PlaywrightError

    from facebook_marketplace_scraper.collector import FacebookMarketplaceCollector
    from facebook_marketplace_scraper.config import Settings

    collector = FacebookMarketplaceCollector(Settings(
        facebook_storage_state=str(tmp_path / "storage_state.json"),
    ))
    page = MagicMock()
    page.url = "https://www.facebook.com/marketplace/112306758786227/propertyrentals/"
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    text = "Фильтры\nПаттайя\nВ радиусе 20 км"
    page.locator.return_value.inner_text = AsyncMock(return_value=text)

    async def screenshot(**kwargs):
        if failure == "screenshot":
            raise PlaywrightError("Page closed")
        Path(kwargs["path"]).write_bytes(b"test screenshot")

    page.screenshot = AsyncMock(side_effect=screenshot)
    if failure == "directory":
        (tmp_path / "diagnostics").write_text("not a directory")

    with pytest.raises(ValueError, match="configured 10 km search radius"):
        await collector._collect_candidates(page, "propertyrentals")

    if failure == "directory":
        page.screenshot.assert_not_awaited()
        return
    directory, = (tmp_path / "diagnostics").iterdir()
    assert directory.stat().st_mode & 0o777 == 0o700
    metadata = json.loads((directory / "metadata.json").read_text())
    assert metadata["run_id"] == collector.run_id
    assert metadata["final_url"] == page.url
    assert "radius=10" in metadata["requested_url"]
    assert metadata["radius_km"] == 10
    assert datetime.fromisoformat(metadata["captured_at"]).utcoffset().total_seconds() == 25200
    assert (directory / "page.txt").read_text() == text
    assert (directory / "page.png").exists() == (failure is None)
    page.screenshot.assert_awaited_once()


@pytest.mark.parametrize("outcome", ["loaded", "empty", "restriction", "login"])
async def test_search_radius_waits_for_loading_and_checks_access(tmp_path, outcome):
    from unittest.mock import AsyncMock, MagicMock

    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    from facebook_marketplace_scraper.collector import (
        FacebookMarketplaceCollector,
        FacebookRestrictionError,
        FacebookSessionExpiredError,
    )
    from facebook_marketplace_scraper.config import Settings

    collector = FacebookMarketplaceCollector(Settings(
        facebook_storage_state=str(tmp_path / "storage_state.json"),
        facebook_page_timeout_seconds=5,
    ))
    collector._save_radius_diagnostics = AsyncMock()
    page = MagicMock()
    page.url = "https://www.facebook.com/marketplace/"
    page.wait_for_timeout = AsyncMock()
    reads = 0

    async def body(**kwargs):
        nonlocal reads
        reads += 1
        if reads < 4 or outcome == "empty":
            return ""
        if outcome == "restriction":
            return "We suspect automated behavior on your account"
        if outcome == "login":
            page.url = "https://www.facebook.com/login/"
            return "Log in"
        return "Паттайя · В радиусе 10 км"

    page.locator.return_value.inner_text = AsyncMock(side_effect=body)
    expected = {
        "empty": PlaywrightTimeoutError,
        "restriction": FacebookRestrictionError,
        "login": FacebookSessionExpiredError,
    }
    if outcome == "loaded":
        await collector._wait_for_search_radius(page, page.url)
        assert reads == 4
    else:
        with pytest.raises(expected[outcome]):
            await collector._wait_for_search_radius(page, page.url)
    if outcome == "empty":
        collector._save_radius_diagnostics.assert_awaited_once_with(page, page.url, "")
        assert reads == 10
    else:
        collector._save_radius_diagnostics.assert_not_awaited()


@pytest.mark.parametrize("persist", [True, False])
async def test_browser_context_only_collector_saves_session(tmp_path, monkeypatch, persist):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from facebook_marketplace_scraper import collector as module
    from facebook_marketplace_scraper.config import Settings

    state = tmp_path / "state.json"
    state.write_text("{}")
    context = SimpleNamespace(set_default_timeout=lambda _: None, close=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    @asynccontextmanager
    async def managed():
        yield playwright

    save = AsyncMock()
    monkeypatch.setattr(module, "managed_playwright", managed)
    monkeypatch.setattr(module, "save_refreshed_storage_state", save)
    collector = module.FacebookMarketplaceCollector(Settings(
        _env_file=None, facebook_storage_state=str(state), facebook_browser_executable=None,
        facebook_profile_dir=""))
    async with collector.browser_context(persist_storage_state=persist) as opened:
        assert opened is context
    assert save.await_count == int(persist)
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
