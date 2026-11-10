from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from facebook_marketplace_scraper.collector import FacebookRestrictionError
from facebook_marketplace_scraper.browsing import browse_listing_photos


def controls():
    button = MagicMock()
    button.is_visible = AsyncMock(return_value=True)
    button.is_enabled = AsyncMock(return_value=True)
    button.click = AsyncMock()
    viewer = MagicMock()
    viewer.is_visible = AsyncMock(return_value=True)
    viewer.get_by_role.return_value.first = button
    page = MagicMock()
    page.get_by_role.side_effect = lambda role, **_: SimpleNamespace(
        first=button, last=viewer,
    )
    page.mouse.wheel = AsyncMock()
    page.keyboard.press = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    rng = SimpleNamespace(choice=lambda values: values[0], randint=lambda lo, hi: hi)
    return page, button, rng


async def test_photo_steps_are_bounded_and_viewer_is_closed():
    page, button, rng = controls()
    check = AsyncMock()
    await browse_listing_photos(page, check, max_steps=2, rng=rng)
    assert button.click.await_count == 3  # Open, next, next.
    page.keyboard.press.assert_awaited_once_with("Escape")
    assert check.await_count >= 6


async def test_unknown_layout_does_not_click_anything():
    page, button, rng = controls()
    button.is_visible.return_value = False
    await browse_listing_photos(page, AsyncMock(), rng=rng)
    button.click.assert_not_awaited()
    page.keyboard.press.assert_not_awaited()


async def test_warning_after_open_stops_without_further_clicks():
    page, button, rng = controls()
    check = AsyncMock(side_effect=[None, FacebookRestrictionError("warning")])
    with pytest.raises(FacebookRestrictionError):
        await browse_listing_photos(page, check, rng=rng)
    button.click.assert_awaited_once()
    page.keyboard.press.assert_not_awaited()
