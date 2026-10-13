"""LIVE: the FIRST humanized mouse.click on a fresh headless page, and the first after a navigation,
walks and presses. Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary (and a licence is
available for a PRO build).

WHY. The engine route (Browser.dispatchPointerPath) walks in the browser process and does not wait
for the page. A fresh headless page has drawn one or two frames and then stalls until something asks
for one, and what the engine sent meanwhile was lost: on r38 and r40 (SC 216, 2026-10-13) the first
click of a fresh browser delivered no mousedown at all on some launches (a mouseup without one on
some), a handful of pointer moves for the whole walk on the others. Waiting for two animation frames
before the walk fixed it. Mirrors sdk/node/test/humanize-first-click.live.test.ts.
"""
import json
import os
from urllib.parse import quote

import pytest

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")

PAGES = 6

PAGE = """<!doctype html><html><body style="margin:0;height:1200px">
<button id=b style="position:absolute;left:600px;top:400px;width:120px;height:40px">target</button>
<button id=c style="position:absolute;left:150px;top:120px;width:120px;height:40px">other</button>
<script>window.ev = [];
addEventListener('pointermove', e => {
  const cs = (e.getCoalescedEvents && e.getCoalescedEvents().length) ? e.getCoalescedEvents() : [e];
  for (const c of cs) ev.push({ t: 'move', tr: e.isTrusted });
}, true);
for (const t of ['mousedown', 'mouseup', 'click'])
  addEventListener(t, e => ev.push({ t, tr: e.isTrusted, id: e.target.id }), true);</script></body></html>"""

# b's centre, or c's: consecutive clicks on one page alternate, so each walk covers ~500 px.
AT_B, AT_C = (660, 420), (210, 140)
WAIT_UP = "() => window.ev.some(e => e.t === 'mouseup')"
TAKE = "() => window.ev.splice(0)"


def summary(ev):
    return {
        "moves": sum(e["t"] == "move" for e in ev),
        "down": sum(e["t"] == "mousedown" for e in ev),
        "up": sum(e["t"] == "mouseup" for e in ev),
        "click": sum(e["t"] == "click" and e.get("id") in ("b", "c") for e in ev),
        "trusted": all(e["tr"] for e in ev),
    }


def check(got):
    """A walk of 300+ px over >= 280 ms reaches the page as tens of moves once frames flow (22-50
    measured on Node); the broken first walks delivered 1-9, or no press."""
    for g in got:
        assert {k: g[k] for k in ("down", "up", "click", "trusted")} == \
            {"down": 1, "up": 1, "click": 1, "trusted": True}, json.dumps(got)
        assert g["moves"] >= 10, json.dumps(got)


@pytest.fixture(scope="module")
def _no_font_cache_left_behind():
    import shutil
    import tempfile
    cache = os.path.join(tempfile.gettempdir(), "cc-fc-cache")
    existed = os.path.exists(cache)
    yield
    if not existed:
        shutil.rmtree(cache, ignore_errors=True)


@pytest.fixture(scope="module")
def browser(_no_font_cache_left_behind):
    import clearcote
    b = clearcote.launch(executable_path=LIVE, headless=True, humanize=True, fingerprint="first-click-live-py",
                         quiet=True)
    yield b
    b.close()


def click_and_read(page, at=AT_B):
    page.mouse.click(*at)
    try:
        page.wait_for_function(WAIT_UP, timeout=3000)
    except Exception:  # noqa: BLE001 - no mouseup is what the summary reports
        pass
    return page.evaluate(TAKE)


def test_walks_and_presses_on_each_fresh_page(browser):
    got = []
    for _ in range(PAGES):
        page = browser.new_page()
        page.set_content(PAGE)
        page.wait_for_timeout(200)
        got.append(summary(click_and_read(page)))
        page.close()
    check(got)


def test_walks_and_presses_after_a_navigation(browser):
    page = browser.new_page()
    page.set_content(PAGE)
    page.wait_for_timeout(200)
    click_and_read(page)
    got = []
    for i in range(4):
        page.goto("data:text/html," + quote(PAGE + f"<!-- {i} -->"))
        page.wait_for_timeout(200)
        got.append(summary(click_and_read(page, AT_B if i % 2 else AT_C)))
    page.close()
    check(got)
