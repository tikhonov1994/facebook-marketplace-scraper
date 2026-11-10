# Facebook Marketplace Scraper

An independent Python library and CLI for collecting Facebook Marketplace property
listings with Playwright. Includes manual login, a dedicated persistent browser
profile, bounded discovery, and JSON export. Licensed under MIT.

## Scope

The current implementation targets **property rentals and sales in Pattaya,
Thailand**. It requests listings from the last day, sorted newest first. It keeps
Pattaya-area location markers (English, Russian, Thai), uses the Asia/Bangkok time
zone and a Russian browser locale, and splits large searches using THB prices.
Changing only the location ID does **not** make it a general-purpose city or
category scraper; the location filters and price handling would also need changes.

Discovery combines embedded JSON, observed GraphQL search responses, and visible
cards. Optional seller discovery and price-range searches share one detail budget.
Known IDs can be skipped. Results include title, description, displayed price,
seller name, approximate publication time when recognized, and gallery image URLs.
No database, queue, AI extraction, availability checks, or image downloads are
required. A snapshot reader is also available as `FacebookSnapshotCollector` in
`facebook_marketplace_scraper.collector` for existing NDJSON records.

## Acknowledgements

The approach of reading JSON responses delivered to the browser during scrolling,
with HTML as a fallback, was inspired by
[danyk20/facebook-marketplace-scraper](https://github.com/danyk20/facebook-marketplace-scraper).
That project was also used to study the Marketplace search response structure.
Our asynchronous response parsing and capture implementation is in
[`feed.py`](src/facebook_marketplace_scraper/feed.py), with discovery limits and
HTML fallback handled by [`collector.py`](src/facebook_marketplace_scraper/collector.py).

## Installation

Requires Python 3.13+ and macOS or Linux (profile locking uses `fcntl`).

```sh
git clone https://github.com/tikhonov1994/facebook-marketplace-scraper.git
cd facebook-marketplace-scraper
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
cp .env.example .env
```

On Linux, `playwright install --with-deps chromium` can install browser system
dependencies as well. Login requires a graphical desktop.

## Login and collect

```sh
facebook-scraper login
facebook-scraper collect --output output/listings.json
```

Complete login manually in the opened browser, open Marketplace, then press Enter
in the terminal. A dedicated profile and refreshed storage state are saved under
`data/facebook/`. Use the same working directory and configuration for subsequent
runs. Never point this tool at your everyday browser profile.

The output is a UTF-8 JSON array. An existing output file is never overwritten.
To skip previously collected IDs, create a JSON array such as `["123", "456"]`:

```sh
facebook-scraper collect --known-ids output/known-ids.json --output output/new-listings.json
```

`python -m facebook_marketplace_scraper` supports the same commands. Each collection
runs once; scheduling belongs to the calling application. Failed runs exit nonzero.
Account restrictions, security challenges and expired sessions stop collection;
the tool does not solve challenges or automatically renew a login.

## Python API

```python
import asyncio
from facebook_marketplace_scraper import FacebookMarketplaceCollector, Settings

async def main():
    collector = FacebookMarketplaceCollector(Settings())
    listings = await collector.fetch(known_external_ids={"123"})
    for listing in listings:
        print(listing.model_dump_json())

asyncio.run(main())
```

Each record contains `source`, `external_id`, `url`, `title`, `text`,
`author_name`, `author_id`, `published_at`, `collected_at` and `raw_data`.
`raw_data` includes `photo_urls`, `photos_complete`, `marketplace_price_label`,
`published_label`, `feed_kinds`, and search metadata. Publication timestamps are
approximate; unrecognized ages produce `null`. Gallery detection is best-effort.

## Configuration

Settings are read from environment variables or `.env` in the current directory.
All fields are defined in `src/facebook_marketplace_scraper/config.py`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `FACEBOOK_STORAGE_STATE` | `./data/facebook/storage_state.json` | Private session export |
| `FACEBOOK_PROFILE_DIR` | `./data/facebook/browser_profile` | Dedicated Chromium profile; empty disables persistence |
| `FACEBOOK_BROWSER_EXECUTABLE` | unset | Optional Chromium executable; otherwise Playwright's installed browser |
| `FACEBOOK_LOCATION_ID` | `112306758786227` | Pattaya Marketplace location |
| `FACEBOOK_LOCATION_NAME` | `Pattaya` | Output metadata label |
| `FACEBOOK_RADIUS_KM` | `10` | Requested radius; must be confirmed on the page |
| `FACEBOOK_HISTORY_LIMIT` | `100` | Candidates per search page |
| `FACEBOOK_DETAIL_LIMIT` | `200` | Maximum detail attempts per run |
| `FACEBOOK_MAX_SCROLLS` | `20` | Search scrolling bound |
| `FACEBOOK_PAGE_TIMEOUT_SECONDS` | `30` | Page timeout |
| `FACEBOOK_CHECK_PAUSE_SECONDS` | `5` | Pause between additional searches (legacy setting name) |
| `FACEBOOK_SELLER_LISTINGS_ENABLED` | `true` | Discover listings from sellers |
| `FACEBOOK_SELLER_PAGE_LIMIT` | `5` | Maximum seller pages |
| `FACEBOOK_PRICE_SPLIT_ENABLED` | `true` | Split large searches into price ranges |
| `FACEBOOK_PRICE_SPLIT_THRESHOLD` | `100` | Candidate threshold for splitting |
| `FACEBOOK_PRICE_SPLIT_MAX_SEARCHES` | `4` | Extra searches per category |
| `FACEBOOK_PHOTO_BROWSING_ENABLED` | `true` | Optional gallery browsing for additional cards |
| `FACEBOOK_PHOTO_BROWSING_VISIT_LIMIT` | `3` | Maximum selected gallery visits |
| `FACEBOOK_PHOTO_BROWSING_MAX_STEPS` | `2` | Maximum gallery steps |
| `FACEBOOK_SELLER_BROWSING_ENABLED` | `true` | Preview extra seller cards |
| `FACEBOOK_SELLER_BROWSING_MAX_CARDS` | `3` | Maximum preview cards |

Sessions, output and diagnostics are private local data and are excluded by
`.gitignore`. A failed radius confirmation saves page text and a screenshot under
`data/facebook/diagnostics/`; these may contain personal information. There is no
automatic cleanup. Never publish browser profiles, cookies or collected records.

## Development

```sh
pip install -e '.[dev]'
pytest
python -m build
```

Tests use synthetic records and mocked pages. To include the offline browser
profile test, set `TEST_BROWSER_EXECUTABLE` to a Chromium executable. It intercepts
requests locally and does not contact Facebook. Live Facebook compatibility is
not verified by these tests; markup and access restrictions can change, and
collection completeness is not guaranteed.
