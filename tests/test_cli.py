import json
import stat
from unittest.mock import AsyncMock

import pytest

from facebook_marketplace_scraper import cli
from facebook_marketplace_scraper.models import RawAdvertisement
from facebook_marketplace_scraper.photos import browser_photos


async def test_export_json_and_skip_known_ids(tmp_path, monkeypatch):
    known = tmp_path / "known.json"
    known.write_text('["123"]')
    fetch = AsyncMock(return_value=[RawAdvertisement(
        source="facebook_marketplace", external_id="456", text="Квартира ฿10000",
        raw_data={"photo_urls": ["https://example.invalid/photo.jpg"]},
    )])
    monkeypatch.setattr(cli.FacebookMarketplaceCollector, "fetch", fetch)
    destination = tmp_path / "output" / "listings.json"
    assert await cli.collect(destination, known) == 1
    fetch.assert_awaited_once_with(known_external_ids={"123"})
    result = json.loads(destination.read_text())
    assert result[0]["external_id"] == "456"
    assert result[0]["text"] == "Квартира ฿10000"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


async def test_existing_output_rejected_before_collection(tmp_path, monkeypatch):
    destination = tmp_path / "listings.json"
    destination.write_text("keep")
    fetch = AsyncMock()
    monkeypatch.setattr(cli.FacebookMarketplaceCollector, "fetch", fetch)
    with pytest.raises(FileExistsError):
        await cli.collect(destination)
    fetch.assert_not_awaited()
    assert destination.read_text() == "keep"


@pytest.mark.parametrize("value", [{"id": "123"}, [123], "123"])
async def test_invalid_known_ids_rejected_before_collection(tmp_path, monkeypatch, value):
    known = tmp_path / "known.json"
    known.write_text(json.dumps(value))
    fetch = AsyncMock()
    monkeypatch.setattr(cli.FacebookMarketplaceCollector, "fetch", fetch)
    with pytest.raises(ValueError, match="JSON array of strings"):
        await cli.collect(tmp_path / "out.json", known)
    fetch.assert_not_awaited()


async def test_failed_collection_does_not_write_output(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.FacebookMarketplaceCollector, "fetch", AsyncMock(side_effect=RuntimeError("stop")))
    destination = tmp_path / "out.json"
    with pytest.raises(RuntimeError, match="stop"):
        await cli.collect(destination)
    assert not destination.exists()


async def test_photo_urls_are_returned_without_downloads():
    page = AsyncMock()
    page.evaluate.return_value = {"urls": ["https://example.invalid/1.jpg"], "complete": True}
    assert await browser_photos(page) == {
        "photo_urls": ["https://example.invalid/1.jpg"], "photos_complete": True,
    }
    page.request.get.assert_not_called()


async def test_unreadable_gallery_is_incomplete():
    page = AsyncMock()
    page.evaluate.side_effect = RuntimeError("gone")
    assert await browser_photos(page) == {"photo_urls": [], "photos_complete": False}
