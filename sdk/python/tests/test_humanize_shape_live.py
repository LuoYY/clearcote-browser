"""LIVE path shape and engine routing against a real Clearcote build (SC 210 lane S).

Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary (and a licence is available for a PRO
build). What a PAGE sees: trusted pointer events, where a press lands, how far a held drag strays from
its line, and whether the pointer ever jumps.
"""
import math
import os

import pytest

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")

PAGE = """<!doctype html><html><head><style>
body { margin: 0; height: 900px; font: 14px sans-serif; }
#track { position: absolute; left: 60px; top: 300px; width: 420px; height: 44px; background: #ddd; }
#knob { position: absolute; left: 2px; top: 2px; width: 40px; height: 40px; border-radius: 20px;
        background: #16a34a; touch-action: none; }
.b { position: absolute; width: 90px; height: 34px; }
</style></head><body>
<div id="track"><div id="knob"></div></div>
<button class="b" id="t1" style="left:600px;top:120px">one</button>
<button class="b" id="t2" style="left:180px;top:520px">two</button>
<button class="b" id="t3" style="left:820px;top:560px">three</button>
<script>
window.__ev = [];
addEventListener('pointermove', e => {
  const cs = (e.getCoalescedEvents && e.getCoalescedEvents().length) ? e.getCoalescedEvents() : [e];
  for (const c of cs) window.__ev.push({t: 'move', ts: c.timeStamp, x: c.clientX, y: c.clientY, b: c.buttons, tr: e.isTrusted});
}, true);
for (const ty of ['pointerdown', 'pointerup', 'click'])
  addEventListener(ty, e => window.__ev.push({t: ty, ts: e.timeStamp, x: e.clientX, y: e.clientY, tr: e.isTrusted,
                                             id: e.target.id || e.target.tagName}), true);
(() => { const k = document.getElementById('knob'); let sx = null, x0 = 2;
  k.addEventListener('pointerdown', e => { sx = e.clientX; x0 = k.offsetLeft; k.setPointerCapture(e.pointerId); });
  k.addEventListener('pointermove', e => { if (sx === null) return;
    k.style.left = Math.max(2, Math.min(378, x0 + e.clientX - sx)) + 'px'; });
  k.addEventListener('pointerup', () => { sx = null; }); })();
</script></body></html>"""


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
    b = clearcote.launch(executable_path=LIVE, headless=True, humanize=True, fingerprint="shape-live-1", quiet=True)
    yield b
    b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    p.set_content(PAGE)
    p.wait_for_timeout(200)
    yield p
    p.close()


def take(page):
    return page.evaluate("window.__ev.splice(0)")


def centre(page, sel):
    b = page.locator(sel).bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, b


class _Spy:
    """Records the pointer-path commands the engine accepted on the browser CDP session the humanizer
    opens (r38 answers "wasn't found" to the new name once, then walks under the old one)."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def send(self, method, params=None):
        out = self._inner.send(method, params) if params is not None else self._inner.send(method)
        if method in ("Browser.dispatchPointerPath", "Browser.humanizedClick"):
            self._log.append((method, dict(params or {})))   # only walks the engine accepted
        return out

    def __getattr__(self, name):
        return getattr(self._inner, name)


def spy_engine(page):
    log = []
    br = page.context.browser
    orig = br.new_browser_cdp_session
    br.new_browser_cdp_session = lambda: _Spy(orig(), log)
    return log


def test_slider_drag_stays_on_the_track_and_lands(page):
    x, y, _ = centre(page, "#knob")
    page.mouse.move(x, y)
    take(page)
    page.mouse.down()
    page.mouse.move(x + 330, y + 2)
    page.mouse.up()
    ev = take(page)
    held = [e for e in ev if e["t"] == "move" and e["b"]]
    # Enough held samples to measure the leg. Not a rate check: the planned glide is now paced against a
    # wall clock (lane P), so a loaded box sends fewer, later samples of the same ~0.7-1.2 s leg (20 at
    # load 23 on the merged SDK) where the unpaced loop sent 3x more spread over 3x the time.
    assert len(held) >= 10, len(held)
    assert all(e["tr"] for e in ev), "untrusted pointer event during the drag"
    # line from the grab point to the drop point; how far does the held leg stray from it?
    x0, y0, x1, y1 = x, y, x + 330, y + 2
    d = math.hypot(x1 - x0, y1 - y0)
    side = max(abs((e["x"] - x0) * (y1 - y0) / d - (e["y"] - y0) * (x1 - x0) / d) for e in held)
    # planner bound 6 px; the engine's own coalesced sub-samples add up to ~1.5 px and whole-pixel rounding
    assert side <= 9, f"held leg strayed {side:.1f} px from its line (a 40 px track leaves 20)"
    left = page.evaluate("document.getElementById('knob').offsetLeft")
    assert abs(left - (2 + 330)) <= 3, left
    print(f"\nlive slider drag: {len(held)} held samples, max sideways {side:.1f} px, "
          f"{held[-1]['ts'] - held[0]['ts']:.0f} ms, knob left {left}")


def test_coordinate_click_after_an_sdk_glide_never_teleports(page):
    log = spy_engine(page)
    tx, ty, _ = centre(page, "#t1")
    page.mouse.click(tx, ty)                       # engine walk (a fresh page: nothing moved yet)
    take(page)
    page.mouse.move(850, 600)                      # SDK glide: the engine does not see it
    take(page)
    t2x, t2y, box = centre(page, "#t2")
    page.mouse.click(t2x, t2y)
    ev = take(page)
    moves = [e for e in ev if e["t"] == "move"]
    pts = [(850, 600)] + [(e["x"], e["y"]) for e in moves]
    worst = max(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) for i in range(1, len(pts)))
    assert worst < 60, f"pointer jumped {worst:.0f} px (the engine walked from its stale position)"
    downs = [e for e in ev if e["t"] == "pointerdown"]
    assert len(downs) == 1 and downs[0]["tr"] and downs[0]["id"] == "t2"
    print(f"\nengine walks: {len(log)} (the click after the glide went through the planner)")


def test_routed_selector_click_is_trusted_and_lands_inside(page, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_ENGINE_CLICKS", "all")
    log = spy_engine(page)
    for sel in ("#t1", "#t3", "#t2"):
        page.click(sel)
    ev = take(page)
    assert len(log) == 3, f"engine walks: {len(log)}"   # every click walked in the engine (in sync)
    for sel, (_, params) in zip(("#t1", "#t3", "#t2"), log):
        _, _, b = centre(page, sel)
        assert b["x"] <= params["x"] <= b["x"] + b["width"] and b["y"] <= params["y"] <= b["y"] + b["height"]
        assert params["noClick"] is False
    downs = [e for e in ev if e["t"] == "pointerdown"]
    clicks = [e for e in ev if e["t"] == "click"]
    assert [e["id"] for e in downs] == ["t1", "t3", "t2"]
    assert [e["id"] for e in clicks] == ["t1", "t3", "t2"]
    assert all(e["tr"] for e in ev)
    # a selector click after an SDK glide goes back to the planner (no stale-position walk)
    page.mouse.move(300, 200)
    take(page)
    page.click("#t1")
    ev = take(page)
    assert len(log) == 3
    assert [e["id"] for e in ev if e["t"] == "click"] == ["t1"]


def test_default_selector_click_stays_on_the_planner(page):
    log = spy_engine(page)
    page.click("#t3")
    ev = take(page)
    assert log == []
    assert [e["id"] for e in ev if e["t"] == "click"] == ["t3"]
    assert all(e["tr"] for e in ev)
