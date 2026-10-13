"""LIVE: the async press_and_hold (clearcote.async_api): `until` may be async, and a cancelled hold still
lets the button up. Its own module: the sync live tests keep a sync Playwright loop running.

Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary.
"""
import asyncio
import os

import pytest

from test_press_and_hold_live import PAGE, _held, _no_font_cache_left_behind  # noqa: F401

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")


def test_async(_no_font_cache_left_behind, tmp_path):
    f = tmp_path / "async.html"
    f.write_text(PAGE, encoding="utf-8")

    async def run():
        import clearcote.async_api as ca
        b = await ca.launch(executable_path=LIVE, headless=True, humanize=True, fingerprint="hold-live-2", quiet=True)
        try:
            p = await b.new_page(viewport={"width": 1180, "height": 760})
            await p.goto(f.as_uri())

            async def done():
                return await p.locator("#done").is_visible()
            assert await p.locator("#hold").press_and_hold(6000, until=done) is True
            ev = await p.evaluate("window.__ev")
            d, u, during = _held(ev)
            assert 1600 <= u["ts"] - d["ts"] <= 2050
            assert all(m["buttons"] == 1 for m in during)

            # cancelled mid-hold: the button still comes up
            await p.evaluate("window.__ev = []")
            task = asyncio.ensure_future(p.press_and_hold("#hold", 5000))
            await asyncio.sleep(2.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            ev = await p.evaluate("window.__ev")
            assert [e["t"] for e in ev if e["t"] in ("pointerdown", "pointerup")] == ["pointerdown", "pointerup"]
        finally:
            await b.close()
    asyncio.run(run())
