"""Lane E (SC 232), async mirror of test_humanize_dragscroll on the same fake document."""
import asyncio
import math

import pytest

import clearcote._humanize_async as HA
import clearcote._pacing as P
from tests._fake_scrollpage import Clock, Doc, Page, World

try:
    import clearcote._scroll as S
except ImportError:  # pragma: no cover - before 0.52
    S = None


class AWorld:
    def __init__(self, doc):
        self.w = World(doc)

    async def evaluate(self, expr, arg=None, await_promise=False):
        return self.w.evaluate(expr, arg)

    async def scroll(self):
        return self.w.scroll()

    async def node_at(self, x, y, scroll=None):
        return self.w.node_at(x, y)

    async def call_on_node(self, nid, fn, arg=None):
        return self.w.call_on_node(nid, fn, arg)

    async def send(self, method, params=None):
        return None


class ALoc:
    def __init__(self, page, sel):
        self.page = page
        self.sel = sel
        from tests._fake_scrollpage import _Impl
        self._impl_obj = _Impl(sel)

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.sel in self.page.doc.els else 0

    async def is_visible(self):
        return self.sel in self.page.doc.els

    async def is_enabled(self):
        return True

    async def wait_for(self, **kw):
        pass

    async def bounding_box(self):
        x, y, w, h = self.page.doc.vrect(self.sel)
        return {"x": x, "y": y, "width": w, "height": h}

    async def scroll_into_view_if_needed(self, **kw):
        self.page.doc.center_on(self.sel, "playwright")


class AMouse:
    def __init__(self, doc):
        self.d = doc

    async def move(self, x, y, **kw):
        self.d.move(x, y)

    async def down(self, **kw):
        self.d.down()

    async def up(self, **kw):
        self.d.up()

    async def wheel(self, dx, dy):
        self.d.wheel(dx, dy)

    async def click(self, x, y, **kw):
        self.d.move(x, y)
        self.d.down()
        self.d.up()

    async def dblclick(self, *a, **kw):
        pass


class AKeyboard:
    def __getattr__(self, name):
        async def f(*a, **k):
            return None
        return f


class APage(Page):
    def __init__(self, doc):
        super().__init__(doc)
        self.mouse = AMouse(doc)
        self.keyboard = AKeyboard()

    def locator(self, sel):
        return ALoc(self, sel)

    async def drag_and_drop(self, source, target, **kw):
        self.log.append(("native_dnd", source, target, kw))

    async def click(self, selector, **kw):
        self.log.append(("native_click", selector))

    async def hover(self, selector, **kw):
        self.log.append(("native_hover", selector))

    def on(self, *a, **k):
        pass

    def __getattr__(self, name):
        async def f(*a, **k):
            return None
        return f


def amake(monkeypatch, doc, seed="lane-e", device=None):
    clock = Clock()
    doc.clock = clock
    real_sleep = asyncio.sleep

    async def vsleep(s, *a, **k):
        clock.sleep(s)
        await real_sleep(0)

    monkeypatch.setattr(HA.asyncio, "sleep", vsleep)
    for mod in (HA, P, S):
        if mod is not None:
            monkeypatch.setattr(mod, "time", clock)
    world = AWorld(doc)
    monkeypatch.setattr(HA, "async_world_for", lambda page: world)
    monkeypatch.delenv("CLEARCOTE_HUMANIZE_ENGINE_CLICKS", raising=False)
    page = APage(doc)
    page._clearcote_input_device = device or "auto"   # a str: not the fake's catch-all attribute
    return page, clock


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _ev(page, kind):
    return [e for e in page.log if e[0] == kind]


def _dnd_doc():
    d = Doc()
    d.els["#src"] = {"rect": [100.0, 200.0, 120.0, 60.0]}
    d.els["#dst"] = {"rect": [700.0, 420.0, 200.0, 140.0]}
    return d


async def _attach(page, seed="lane-e"):
    await HA.attach_humanize(None, page, humanize=True, seed=seed)


def test_async_drag_and_drop_positions_and_hesitation(monkeypatch):
    d = _dnd_doc()
    page, _ = amake(monkeypatch, d)

    async def go():
        await _attach(page)
        await page.drag_and_drop("#src", "#dst", source_position={"x": 10, "y": 12},
                                 target_position={"x": 180, "y": 20})
    _run(go())
    assert not _ev(page, "native_dnd")
    i_down = next(i for i, e in enumerate(page.log) if e[0] == "down")
    i_up = next(i for i, e in enumerate(page.log) if e[0] == "up")
    press = [e for e in page.log[:i_down] if e[0] == "move"][-1]
    release = [e for e in page.log[:i_up] if e[0] == "move"][-1]
    assert abs(press[1] - 110) <= 2 and abs(press[2] - 212) <= 2
    assert abs(release[1] - 880) <= 2 and abs(release[2] - 440) <= 2
    held = [e for e in page.log[i_down:i_up] if e[0] == "move"]
    assert len(held) >= 10
    t_down = page.log[i_down][1]
    leg = next(e for e in held if math.hypot(e[1] - press[1], e[2] - press[2]) >= 8)
    assert 0.100 <= leg[3] - t_down <= 0.350


def test_async_drag_by_converges(monkeypatch):
    d = Doc()
    d.els["#thumb"] = {"rect": [200.0, 300.0, 20.0, 20.0]}
    d.follow["#thumb"] = (0.93, 10.0)
    page, _ = amake(monkeypatch, d)

    async def go():
        await _attach(page)
        return await page.drag_by("#thumb", 120, 0)
    got = _run(go())
    assert got is not None and abs(got[0] - 120) <= 2
    assert 1 <= page._clearcote_drag_legs <= 3


def test_async_click_offscreen_uses_the_wheel(monkeypatch):
    d = Doc()
    d.els["#far"] = {"rect": [500.0, 3000.0, 160.0, 40.0]}
    page, _ = amake(monkeypatch, d)

    async def go():
        await _attach(page)
        await page.click("#far")
    _run(go())
    assert _ev(page, "wheel") and not _ev(page, "prog")
    r = d.vrect("#far")
    assert 0.18 <= (r[1] + r[3] / 2) / d.vh <= 0.67


def test_async_non_scrollable_falls_back_and_reports(monkeypatch):
    d = Doc(scrollable=False)
    d.els["#far"] = {"rect": [500.0, 3000.0, 160.0, 40.0]}
    page, _ = amake(monkeypatch, d)

    async def go():
        await _attach(page)
        await page.click("#far")
    _run(go())
    assert _ev(page, "prog")
    assert page._clearcote_last_fallback == {"api": "Page.click", "reason": "scroll into view: not_scrollable",
                                             "selector": None}


def test_async_mac_trackpad_stream(monkeypatch):
    d = Doc(platform="MacIntel")
    page, _ = amake(monkeypatch, d)

    async def go():
        await _attach(page)
        await page.mouse.wheel(0, 500)
    _run(go())
    deltas = [e[2] for e in _ev(page, "wheel")]
    assert abs(sum(deltas) - 500) < 1e-6 and len(deltas) > 10 and all(abs(x) <= 40 for x in deltas)
    assert any(abs(x - round(x)) > 1e-3 for x in deltas)
