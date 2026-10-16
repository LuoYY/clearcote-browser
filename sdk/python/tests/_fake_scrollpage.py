"""A fake page for the drag + scroll humanize tests (SC 232 lane E): a document that scrolls (or not),
a nested scrolling container, a slider thumb that follows a held pointer, and a virtual clock, so the
humanizer's wheel / drag paths run with no browser.

What the fake records (``page.log``): ("move", x, y, t), ("down", t), ("up", t), ("wheel", dx, dy, x, y, t)
and ("prog", how, t) for every PROGRAMMATIC scroll (isolated scrollIntoViewIfNeeded, scrollBy,
Playwright's scroll_into_view_if_needed), ("native_dnd", ...) for Playwright's own drag_and_drop."""
import clearcote._humanize as H
import clearcote._pacing as P
try:   # absent before 0.52: the tests then run against the old humanizer and fail on behaviour
    import clearcote._scroll as S
except ImportError:  # pragma: no cover
    S = None
from clearcote._isolated import (
    CSS_BOX, DESCRIBE, FRAMES, HIT_IN_BOX, PLATFORM, SCROLL, SCROLL_BY, VIEWPORT,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, s):
        self.now += max(0.0, s)

    def monotonic(self):
        return self.now

    time = perf_counter = monotonic


class Doc:
    """Document coordinates; the viewport is (sx, sy, vw, vh)."""

    def __init__(self, scrollable=True, height=4000, platform="Windows"):
        self.vw, self.vh = 1280.0, 800.0
        self.sx = self.sy = 0.0
        self.height = height
        self.scrollable = scrollable
        self.platform = platform
        self.els = {}          # sel -> dict(rect=[x, y, w, h] doc or container coords, parent=sel|None)
        self.containers = {}   # sel -> dict(rect=[...] doc, st=0, inner=h)
        self.ptr = (0.0, 0.0)
        self.held = False
        self.grab = None       # (sel, pointer x at press, element x at press)
        self.follow = {}       # sel -> (gain, quantum): a thumb that follows a held pointer on x
        self.covers = []       # [x, y, w, h] viewport rects that own the hit test
        self.clock = None
        self.log = []

    def t(self):
        return self.clock.now if self.clock else 0.0

    # geometry ------------------------------------------------------------------------------------
    def vrect(self, sel):
        e = self.els[sel]
        x, y, w, h = e["rect"]
        if e.get("parent"):
            c = self.containers[e["parent"]]
            cx, cy, _, _ = c["rect"]
            x, y = cx + x, cy + y - c["st"]
        return [x - self.sx, y - self.sy, w, h]

    def crect(self, csel):
        x, y, w, h = self.containers[csel]["rect"]
        return [x - self.sx, y - self.sy, w, h]

    def at(self, x, y):
        for c in self.covers:
            if c[0] <= x < c[0] + c[2] and c[1] <= y < c[1] + c[3]:
                return "#cover"
        for sel in reversed(list(self.els)):
            r = self.vrect(sel)
            p = self.els[sel].get("parent")
            if p:
                cr = self.crect(p)
                if not (cr[0] <= x < cr[0] + cr[2] and cr[1] <= y < cr[1] + cr[3]):
                    continue
            if r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]:
                return sel
        return "body"

    def center_on(self, sel, how):
        self.log.append(("prog", how, self.t()))
        r = self.vrect(sel)
        if self.scrollable:
            self.sy = max(0.0, min(self.height - self.vh, self.sy + r[1] + r[3] / 2 - self.vh / 2))
        p = self.els[sel].get("parent")
        if p:
            c = self.containers[p]
            x, y, w, h = self.els[sel]["rect"]
            c["st"] = max(0.0, min(c["inner"] - c["rect"][3], y + h / 2 - c["rect"][3] / 2))

    # input ---------------------------------------------------------------------------------------
    def move(self, x, y):
        self.ptr = (x, y)
        self.log.append(("move", x, y, self.t()))
        if self.held and self.grab and self.grab[0] in self.follow:
            sel, px0, ex0 = self.grab
            gain, q = self.follow[sel]
            off = round((x - px0) * gain / q) * q
            self.els[sel]["rect"][0] = ex0 + off

    def down(self):
        self.held = True
        self.log.append(("down", self.t()))
        hit = self.at(*self.ptr)
        if hit in self.follow:
            self.grab = (hit, self.ptr[0], self.els[hit]["rect"][0])

    def up(self):
        self.held = False
        self.grab = None
        self.log.append(("up", self.t()))

    def wheel(self, dx, dy):
        self.log.append(("wheel", dx, dy, self.ptr[0], self.ptr[1], self.t()))
        for csel, c in self.containers.items():
            cr = self.crect(csel)
            if cr[0] <= self.ptr[0] < cr[0] + cr[2] and cr[1] <= self.ptr[1] < cr[1] + cr[3]:
                st = max(0.0, min(c["inner"] - c["rect"][3], c["st"] + dy))
                if st != c["st"]:
                    c["st"] = st
                    return
        if self.scrollable:
            self.sy = max(0.0, min(self.height - self.vh, self.sy + dy))


class World:
    def __init__(self, doc):
        self.d = doc

    def evaluate(self, expr, arg=None, await_promise=False):
        d = self.d
        if expr == VIEWPORT:
            return [d.vw, d.vh]
        if expr == PLATFORM:
            return d.platform
        if expr == SCROLL:
            return [d.sx, d.sy]
        if expr == FRAMES:
            return True
        if expr == CSS_BOX:
            sel = arg["sel"]
            if sel not in d.els:
                return {"n": 0}
            if arg.get("scroll"):
                d.center_on(sel, "css_box")
            return {"n": 1, "rect": d.vrect(sel)}
        if S is not None and expr == S.SCROLLER:
            p = d.els.get(arg["sel"], {}).get("parent")
            if not p:
                return None
            c = d.containers[p]
            return {"rect": d.crect(p), "x": False, "y": True, "s": [0.0, c["st"], 0.0, c["inner"] - c["rect"][3]]}
        if S is not None and expr == getattr(S, "SCROLL_RANGE", None):
            return [d.sx, d.sy, 0.0, max(0.0, d.height - d.vh)] if d.scrollable else [0.0, 0.0, 0.0, 0.0]
        if expr == SCROLL_BY:
            d.log.append(("prog", "scroll_by", d.t()))
            before = d.sy
            if d.scrollable:
                d.sy = max(0.0, min(d.height - d.vh, d.sy + arg[1]))
            return [0.0, d.sy - before]
        return None

    def scroll(self):
        return (self.d.sx, self.d.sy)

    def node_at(self, x, y, scroll=None):
        return self.d.at(x, y)

    def call_on_node(self, nid, fn, arg=None):
        if fn == HIT_IN_BOX:
            if arg.get("sel"):
                return nid == arg["sel"]
            if nid not in self.d.els:
                return False
            r = self.d.vrect(nid)
            return all(abs(a - b) <= 1.5 for a, b in zip(r, (arg["x"], arg["y"], arg["width"], arg["height"])))
        if fn == DESCRIBE:
            if nid not in self.d.els:
                return None
            return {"rect": self.d.vrect(nid), "bl": 0, "bt": 0, "tag": "div", "type": "", "ce": False}
        return None

    def send(self, method, params=None):
        return None


class _Impl:
    def __init__(self, sel):
        self._selector = sel
        self._frame = type("F", (), {"parent_frame": None})()


class Loc:
    def __init__(self, page, sel):
        self.page = page
        self.sel = sel
        self._impl_obj = _Impl(sel)

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.sel in self.page.doc.els else 0

    def is_visible(self):
        return self.sel in self.page.doc.els

    def is_enabled(self):
        return True

    def wait_for(self, **kw):
        if self.sel not in self.page.doc.els:
            raise TimeoutError("Timeout waiting for " + self.sel)

    def bounding_box(self):
        x, y, w, h = self.page.doc.vrect(self.sel)
        return {"x": x, "y": y, "width": w, "height": h}

    def scroll_into_view_if_needed(self, **kw):
        self.page.doc.center_on(self.sel, "playwright")


class Mouse:
    def __init__(self, doc):
        self.d = doc

    def move(self, x, y, **kw):
        self.d.move(x, y)

    def down(self, **kw):
        self.d.down()

    def up(self, **kw):
        self.d.up()

    def wheel(self, dx, dy):
        self.d.wheel(dx, dy)

    def click(self, x, y, **kw):
        self.d.move(x, y)
        self.d.down()
        self.d.up()

    def dblclick(self, *a, **kw):
        pass


class Keyboard:
    def __getattr__(self, name):
        return lambda *a, **k: None


class Page:
    def __init__(self, doc):
        self.doc = doc
        self.log = doc.log
        self.mouse = Mouse(doc)
        self.keyboard = Keyboard()
        self.main_frame = object()
        self.viewport_size = {"width": 1280, "height": 800}

    def locator(self, sel):
        return Loc(self, sel)

    def drag_and_drop(self, source, target, **kw):
        self.log.append(("native_dnd", source, target, kw))

    def click(self, selector, **kw):
        self.log.append(("native_click", selector))

    def hover(self, selector, **kw):
        self.log.append(("native_hover", selector))

    def __getattr__(self, name):
        return lambda *a, **k: None


def make(monkeypatch, doc, seed="lane-e", device=None):
    """A humanized fake page over ``doc`` on a virtual clock."""
    clock = Clock()
    doc.clock = clock
    for mod in (H, P, S):
        if mod is not None:
            monkeypatch.setattr(mod, "time", clock)
    world = World(doc)
    monkeypatch.setattr(H, "world_for", lambda page: world)
    monkeypatch.delenv("CLEARCOTE_HUMANIZE_ENGINE_CLICKS", raising=False)
    page = Page(doc)
    # the device through lane A's real options object (HumanizeOptions.input_device)
    H.attach_humanize(None, page, humanize={"input_device": device} if device else True, seed=seed)
    page.clock = clock
    return page
