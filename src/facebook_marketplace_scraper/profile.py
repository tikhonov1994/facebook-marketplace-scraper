"""Persistent browser profile bootstrap and exclusive local access."""

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def profile_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    with (directory / ".scraper.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Facebook profile is already in use") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def seed_profile(context, state_path: Path):
    state = json.loads(state_path.read_text())
    await context.add_cookies(state.get("cookies", []))
    origins = state.get("origins", [])
    if not origins:
        return
    page = await context.new_page()
    try:
        # Bootstrap each origin locally: no HTTP request or authentication action.
        await page.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body="<html></html>",
        ))
        for item in origins:
            origin = item["origin"]
            if not origin.startswith(("https://", "http://")):
                continue
            await page.goto(origin, wait_until="domcontentloaded")
            await page.evaluate(
                "entries => { for (const e of entries) localStorage.setItem(e.name, e.value); }",
                item.get("localStorage", []),
            )
    finally:
        await page.close()
