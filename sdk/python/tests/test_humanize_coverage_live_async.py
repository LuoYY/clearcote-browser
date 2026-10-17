"""Lane F (SC 232) live, async API: frame select, option dicts, hidden checkbox through its label.

The fixture and server are test_humanize_coverage_live's. Skipped unless CLEARCOTE_LIVE_ENGINE is set.
"""
import asyncio
import os

import pytest

from test_humanize_coverage_live import LIVE, server  # noqa: F401  (the module-scoped fixture)

pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")


def ev(rows, t, id):
    return [e for e in rows if e["t"] == t and e["id"] == id]


def test_async_frame_select_dicts_and_label_checkbox(server):  # noqa: F811
    from clearcote.async_api import launch

    async def go():
        b = await launch(executable_path=LIVE, headless=True, humanize=True, fingerprint="coverage-live-async",
                         quiet=True)
        try:
            p = await b.new_page()
            await p.goto(server + "/main.html")
            f = p.frame(name="same")
            for i, name in enumerate(("Peru", "Ghana", "Spain")):
                await f.evaluate("window.__ev.length = 0")
                got = await p.frame_locator("#same").locator("#cs").select_option(label=name)
                assert got and got[0].startswith("v")
                ch = ev(await f.evaluate("window.__ev"), "change", "cs")
                assert len(ch) == 1 and ch[0]["tr"], (i, ch)
            await p.evaluate("window.__ev.length = 0")
            assert await p.select_option("#s", {"label": "Norway"}) == ["v6"]
            assert [e["tr"] for e in ev(await p.evaluate("window.__ev"), "change", "s")] == [True]
            for sel in ("#hcb", "#hcb2"):
                await p.locator(sel).check(timeout=3000)
                assert await p.locator(sel).is_checked()
            assert getattr(p, "_clearcote_last_fallback", None) is None, p._clearcote_last_fallback
        finally:
            await b.close()

    asyncio.run(go())
