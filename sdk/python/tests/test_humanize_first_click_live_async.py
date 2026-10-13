"""LIVE: the async humanizer's first click on a fresh headless page walks and presses (see
test_humanize_first_click_live.py for why). Its own module: the sync live tests keep a sync Playwright
loop running.

Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary.
"""
import asyncio
import os
from urllib.parse import quote

import pytest

from test_humanize_first_click_live import (  # noqa: F401
    AT_B, AT_C, PAGE, PAGES, TAKE, WAIT_UP, _no_font_cache_left_behind, check, summary,
)

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")


async def click_and_read(page, at=AT_B):
    await page.mouse.click(*at)
    try:
        await page.wait_for_function(WAIT_UP, timeout=3000)
    except Exception:  # noqa: BLE001 - no mouseup is what the summary reports
        pass
    return await page.evaluate(TAKE)


def test_async_first_clicks(_no_font_cache_left_behind):
    async def run():
        import clearcote.async_api as ca
        b = await ca.launch(executable_path=LIVE, headless=True, humanize=True, fingerprint="first-click-live-pya",
                            quiet=True)
        try:
            fresh = []
            for _ in range(PAGES):
                page = await b.new_page()
                await page.set_content(PAGE)
                await page.wait_for_timeout(200)
                fresh.append(summary(await click_and_read(page)))
                await page.close()
            page = await b.new_page()
            await page.set_content(PAGE)
            await page.wait_for_timeout(200)
            await click_and_read(page)
            nav = []
            for i in range(4):
                await page.goto("data:text/html," + quote(PAGE + f"<!-- {i} -->"))
                await page.wait_for_timeout(200)
                nav.append(summary(await click_and_read(page, AT_B if i % 2 else AT_C)))
            await page.close()
            return fresh, nav
        finally:
            await b.close()
    fresh, nav = asyncio.run(run())
    check(fresh)
    check(nav)
