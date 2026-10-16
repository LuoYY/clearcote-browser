"""Humanized form input on the ASYNC surface (clearcote._humanize_async), against a simulated page.

The planning is the shared clearcote._forms (vector-tested in test_forms.py); these tests pin the async
WIRING: click options, field-classifying fill, mask-aware typos, select_option by opening the drop-down,
list boxes, check/uncheck/set_checked, timeout propagation, and the async Locator patch. The live
equivalents are in test_humanize_forms_live_async.py.

The simulated page records every trusted input it receives and keeps a field value the way a browser
would after each key (masks and maxlength swallow keys), so a test can assert on the outcome rather
than on the implementation's call sequence. Its isolated world is injected as page._cc_isolated_world,
the cache async_world_for() reads.
"""
import asyncio
import random

import pytest

from clearcote import _forms
from clearcote import _humanize_async as H
from clearcote._isolated import (
    ACTIVE_FIELD, ACTIVE_VALUE, CSS_BOX, DESCRIBE, HIT_IN_BOX, LISTBOX_ROW, PLATFORM, SCROLL, SCROLL_BY, VIEWPORT,
)
from clearcote import _isolated as _iso

# the label reads (lane F); optional so this file still collects against an SDK without them
LABEL_OF = getattr(_iso, "LABEL_OF", "<no LABEL_OF>")
LABEL_BY_SEL = getattr(_iso, "LABEL_BY_SEL", "<no LABEL_BY_SEL>")

PWTimeout = type("TimeoutError", (Exception,), {})   # what _is_timeout recognises (Playwright's name)
ROW_H = 18.0
OVERLAY = 999      # the node id of an overlay the hit test finds over a covered point
VW, VH = 1280.0, 800.0
_real_sleep = asyncio.sleep   # captured before any test replaces asyncio.sleep with a no-op


# ------------------------------------------------------------------------------------- simulator
class Field:
    def __init__(self, tag="input", type="text", value="", rect=(100.0, 100.0, 200.0, 24.0), mask=None,
                 maxlength=-1, autocomplete=None, inputmode=None, bl=2.0, bt=2.0, options=None,
                 selected_index=0, multiple=False, size=0, no_select_all=False, never_commit=False,
                 stubborn=False, visible=True, covered=False, fmt=None, rmin=None, rmax=None, rstep=None,
                 cover_center=False, in_frame=False, appear_on_wait=False, control=None):
        self.tag, self.type, self.value, self.rect = tag, type, value, rect
        self.control = control                    # a <label>: the checkbox its click is passed on to
        self.label = None                         # a checkbox: its <label> Field (set by the label)
        if control is not None:
            control.label = self
        self.in_frame = in_frame                  # its points lie in a child frame: the hit test sees nothing
        self.appear_on_wait = appear_on_wait      # hidden until Playwright's wait_for (it shows up)
        self.st = 0.0                             # a list box's scrollTop
        self.stubborn, self.visible = stubborn, visible   # a checkbox that cancels its click; visibility
        self.covered = covered    # an overlay owns every point of it: the hit test and clicks miss it
        self.cover_center = cover_center   # a floating label over the middle third (all heights)
        self.fmt = fmt            # a page-side formatter run after every key (a phone mask)
        self.rmin, self.rmax, self.rstep = rmin, rmax, rstep
        self.mask, self.maxlength, self.autocomplete, self.inputmode = mask, maxlength, autocomplete, inputmode
        self.bl, self.bt = bl, bt
        self.checked = False
        self.all = False
        self.no_select_all, self.never_commit = no_select_all, never_commit
        self.options = [{"value": v, "label": l, "disabled": d} for v, l, d in
                        ((o + (False,)) if len(o) == 2 else o for o in (options or []))]
        self.selected_index = selected_index if options else -1
        self.selected = set()
        self.multiple, self.size = multiple, size
        self.open, self.opens, self.changes = False, 0, 0
        self.buf, self.start, self.hl = "", -1, -1

    def inside(self, x, y):
        rx, ry, rw, rh = self.rect
        return rx <= x <= rx + rw and ry <= y <= ry + rh

    def covers(self, x, y):
        """Does an overlay own this point of the field?"""
        rx, ry, rw, rh = self.rect
        return self.covered or (self.cover_center and rx + rw * 0.33 <= x <= rx + rw * 0.67)

    def is_listbox(self):
        return self.tag == "select" and (self.multiple or self.size > 1)

    def client_h(self):
        """A list box's client height: `size` rows (all rows when size is 0)."""
        return (self.size or len(self.options)) * ROW_H

    def max_st(self):
        return max(0.0, len(self.options) * ROW_H - self.client_h())

    def row_state(self, i):
        """What LISTBOX_ROW reads for row ``i``."""
        if not (0 <= i < len(self.options)):
            return None
        x, y, w, h = self.rect
        return {"box": [x, y, w, h], "row": [x + self.bl, y + self.bt + i * ROW_H - self.st, w - 2 * self.bl, ROW_H],
                "top": y + self.bt, "bottom": y + self.bt + self.client_h(), "st": self.st, "max": self.max_st(),
                "sel": [k in self.selected for k in range(len(self.options))]}

    def labels(self):
        return [None if o["disabled"] else o["label"] for o in self.options]

    def describe(self, focused):
        x, y, w, h = self.rect
        o = {"tag": self.tag, "type": self.type if self.tag == "input" else "", "ce": False, "rect": [x, y, w, h],
             "focused": focused, "value": self.value, "min": self.rmin, "max": self.rmax, "step": self.rstep,
             "maxlength": self.maxlength, "inputmode": self.inputmode, "autocomplete": self.autocomplete,
             "readonly": False, "disabled": False, "dir": "ltr", "wm": "horizontal-tb", "bl": self.bl, "bt": self.bt}
        if self.tag == "select":
            sel = self.selected if self.is_listbox() else {self.selected_index}
            o.update(multiple=self.multiple, size=self.size, selectedIndex=self.selected_index,
                     options=[dict(op, selected=i in sel) for i, op in enumerate(self.options)])
            if self.is_listbox():
                o["optRects"] = [[x + self.bl, y + self.bt + i * ROW_H, w - 2 * self.bl, ROW_H]
                                 for i in range(len(self.options))]
        return o

    # what the browser does with one trusted click / key
    def label_of(self):
        """What LABEL_OF reads from a checkbox: hidden when not visible or (nearly) zero-size."""
        x, y, w, h = self.rect
        o = {"tag": "input", "type": self.type, "rect": [x, y, w, h],
             "hidden": (not self.visible) or w <= 1 or h <= 1, "checked": self.checked, "disabled": False,
             "label": None}
        if self.label is not None:
            o["label"] = {"rect": list(self.label.rect), "text": None, "contains": False}
        return o

    def click(self, x, y, mods):
        self.clicks = getattr(self, "clicks", 0) + 1
        if self.tag == "label":
            if self.control is not None:
                self.control.click(x, y, mods)   # the label passes the click on to its control
            return
        if self.type == "checkbox":
            if not self.stubborn:
                self.checked = not self.checked
        elif self.is_listbox():
            if not (self.rect[1] + self.bt <= y < self.rect[1] + self.bt + self.client_h()):
                return            # below the client area (a border or scrollbar): no row
            i = int((y - self.rect[1] - self.bt + self.st) // ROW_H)
            if 0 <= i < len(self.options):
                if mods & {"ControlOrMeta", "Control", "Meta"}:
                    self.selected ^= {i}
                else:
                    self.selected = {i}
                self.changes += 1
        elif self.tag == "select":
            self.open, self.opens = True, self.opens + 1
            self.buf, self.start, self.hl = "", self.selected_index, self.selected_index

    def key(self, k):
        if self.tag == "select":
            if not self.open:
                return
            if k == "Enter":
                self.open = False
                if not self.never_commit and self.hl != self.selected_index:
                    self.selected_index, self.changes = self.hl, self.changes + 1
            elif k in ("ArrowDown", "ArrowUp"):
                step = 1 if k == "ArrowDown" else -1
                j = self.hl + step
                while 0 <= j < len(self.options) and self.options[j]["disabled"]:
                    j += step
                if 0 <= j < len(self.options):
                    self.hl = j
            elif len(k) == 1:
                self.buf += k
                self.hl = _forms.typeahead_simulate(self.labels(), self.start, self.buf)
            return
        if k == "ControlOrMeta+a":
            self.all = not self.no_select_all
        elif k == "Backspace":
            self.value = "" if self.all else self.value[:-1]
            self.all = False
        elif k == "End":
            self.all = False
        elif len(k) == 1:
            if self.mask is not None and not self.mask(k):
                return                       # the page's mask rejected it before it landed
            if self.all:
                self.value, self.all = "", False
            if 0 <= self.maxlength <= len(self.value):
                return
            self.value += k
            if self.fmt is not None:
                self.value = self.fmt(self.value)


class World:
    """The isolated world, answering the reads humanize makes from the simulated DOM."""

    def __init__(self, page):
        self.p = page

    async def evaluate(self, src, arg=None):
        f = self.p.focused
        if src == PLATFORM:
            return "Win32"
        if src == VIEWPORT:
            return [1280, 800]
        if src == SCROLL:
            return [0, 0]
        if src == ACTIVE_VALUE:
            if self.p.frame_focus:
                return None          # the focus is inside an iframe: not readable from here
            return f.value if f is not None and f.tag in ("input", "textarea") else None
        if src == ACTIVE_FIELD:
            if self.p.frame_focus:
                return {"frame": True}
            return ({"type": f.type if f.tag == "input" else "", "inputmode": f.inputmode,
                     "autocomplete": f.autocomplete} if f is not None else None)
        if src == CSS_BOX:
            sel = arg["sel"]
            if not sel.startswith("#"):
                return None          # not CSS the browser accepts (role=, text=, ...)
            f = self.p.by_sel.get(sel)
            if f is None:
                return {"n": 0}
            if arg.get("scroll"):
                self.p.scrolls.append(("css", sel))
                self.p.bring_into_view(f)
            return {"n": 1, "rect": list(f.rect)}
        if src == LABEL_BY_SEL:
            f = self.p.by_sel.get(arg["sel"])
            if f is None:
                return {"n": 0}
            o = f.label_of() if f.tag == "input" and f.type in ("checkbox", "radio") else None
            if o is None:
                return {"n": 1}
            o["n"] = 1
            return o
        if src == SCROLL_BY:
            dx, dy = arg
            self.p.scrolls.append(("window", dx, dy))
            for g in self.p.fields:
                x, y, w, h = g.rect
                g.rect = (x - dx, y - dy, w, h)
            return [dx, dy]
        return None

    async def scroll(self):
        return (0.0, 0.0)

    async def node_at(self, x, y, scroll=None):
        for i, f in enumerate(self.p.fields):
            if f.inside(x, y):
                if f.in_frame:
                    return None      # a child frame's document: not this world's
                if f.covers(x, y) or getattr(f, "clipped", False):
                    return OVERLAY   # an overlay (or the clipping container) owns the point
                return i + 1
        return None

    async def call_on_node(self, nid, src, arg=None):
        if nid == OVERLAY:
            return False if src == HIT_IN_BOX else None
        f = self.p.fields[nid - 1]
        if src == HIT_IN_BOX:
            return all(abs(a - b) <= 1.5 for a, b in zip(f.rect, (arg["x"], arg["y"], arg["width"], arg["height"])))
        if src == LISTBOX_ROW:
            return f.row_state(arg["i"])
        if src == LABEL_OF:
            return f.label_of() if f.tag == "input" and f.type in ("checkbox", "radio") else None
        assert src == DESCRIBE, src
        if (arg or {}).get("want") == "select" and f.tag != "select":
            return None
        return f.describe(f is self.p.focused)

    async def send(self, method, params=None):
        return None


class Mouse:
    def __init__(self, page):
        self.p = page

    async def move(self, x, y, **kw):
        await _real_sleep(0)   # a real suspension point, like the driver round-trip
        self.p.pos = (x, y)
        self.p.log.append(("move", x, y))

    async def down(self, button="left", click_count=1):
        await _real_sleep(0)
        self.p.log.append(("down", button, click_count, frozenset(self.p.mods), self.p.pos))

    async def up(self, button="left", click_count=1):
        await _real_sleep(0)
        self.p.log.append(("up", button, click_count))
        if button == "left" and click_count == 1:
            self.p.activate()

    async def click(self, x, y, delay=None, button="left", click_count=1):
        await _real_sleep(0)
        self.p.pos = (x, y)
        self.p.log.append(("click", x, y, delay, button, click_count))
        if button == "left":
            self.p.activate()

    async def dblclick(self, x, y, **kw):
        self.p.log.append(("native-dblclick", x, y))

    async def wheel(self, dx, dy):
        await _real_sleep(0)
        self.p.log.append(("wheel", dx, dy))
        for f in self.p.fields:   # a list box under the pointer scrolls 60 px per 100 px notch
            if f.is_listbox() and f.inside(*self.p.pos):
                f.st = min(max(f.st + dy * 0.6, 0.0), f.max_st())


class Keyboard:
    def __init__(self, page):
        self.p = page

    async def press(self, key, delay=None):
        await _real_sleep(0)
        self.p.log.append(("press", key, delay))
        self.p.key(key)

    async def type(self, text, **kw):
        self.p.log.append(("type", text))
        for ch in text:
            self.p.key(ch)

    async def insert_text(self, text):
        await _real_sleep(0)
        self.p.log.append(("insert", text))
        f = self.p.focused
        if f is not None:
            if f.all:
                f.value, f.all = "", False
            room = len(text) if f.maxlength < 0 else max(0, f.maxlength - len(f.value))
            f.value += text[:room]

    async def down(self, key):
        await _real_sleep(0)
        self.p.log.append(("kdown", key))
        if len(key) == 1 or key in ("ArrowDown", "ArrowUp"):
            self.p.key(key)   # a key already down sends an auto-repeat keydown: it acts again
        else:
            self.p.mods.add(key)

    async def up(self, key):
        await _real_sleep(0)
        self.p.log.append(("kup", key))
        self.p.mods.discard(key)


class Loc:
    """page.locator(sel).first: Playwright's actionability waits, with a missing element timing out."""

    def __init__(self, page, sel):
        self.p, self.sel = page, sel

    @property
    def first(self):
        return self

    def _f(self):
        f = self.p.by_sel.get(self.sel)
        if f is None:
            raise PWTimeout("Timeout exceeded waiting for %s" % self.sel)
        return f

    async def wait_for(self, state=None, timeout=None):
        self.p.waits.append(timeout)
        self.p.handles.append(("wait_for", self.sel))   # resolves a main-world handle
        f = self._f()
        if f.appear_on_wait:
            f.visible = True
        if not f.visible:
            raise PWTimeout("Timeout exceeded waiting for %s to be visible" % self.sel)

    async def count(self):
        return int(self.sel in self.p.by_sel)

    async def is_visible(self, timeout=None):
        f = self.p.by_sel.get(self.sel)   # no wait, no error: Playwright's is_visible
        return bool(f is not None and f.visible)

    def and_(self, other):
        """loc.and_(page.locator(":focus")): matches when this element holds the focus."""
        outer = self

        class _Count:
            async def count(self):
                f = outer.p.by_sel.get(outer.sel)
                return int(other.sel == ":focus" and f is not None and outer.p.focused is f)
        return _Count()

    async def scroll_into_view_if_needed(self, timeout=None):
        self.p.handles.append(("scroll_into_view_if_needed", self.sel))
        self.p.scrolls.append(("playwright", self.sel))
        self.p.bring_into_view(self._f())

    async def is_enabled(self, timeout=None):
        self._f()
        return True

    async def bounding_box(self, timeout=None):
        self.p.handles.append(("bounding_box", self.sel))
        x, y, w, h = self._f().rect
        return {"x": x, "y": y, "width": w, "height": h}

    async def is_checked(self, timeout=None):
        return self._f().checked

    async def input_value(self, timeout=None):
        f = self._f()
        if f.tag == "select":
            sel = sorted(f.selected) if f.is_listbox() else [f.selected_index]
            return f.options[sel[0]]["value"] if sel and sel[0] >= 0 else ""
        return f.value

    async def get_attribute(self, name, timeout=None):
        return {"type": self._f().type}.get(name)

    def locator(self, sel):
        outer = self

        class _Count:
            async def count(self):
                return int(sel == "xpath=self::%s" % outer._f().tag)
        return _Count()


class Page:
    def __init__(self, *fields):
        self.by_sel = dict(fields)
        self.fields = [f for _, f in fields]
        self.mouse, self.keyboard = Mouse(self), Keyboard(self)
        self.log, self.native, self.mods, self.waits = [], [], set(), []
        self.handles, self.scrolls = [], []   # main-world handle calls; scrolls (css / window / playwright)
        self.pos, self.focused, self.frame_focus = (0.0, 0.0), None, False
        self.main_frame = object()
        self.viewport_size = {"width": 1280, "height": 800}
        self._cc_isolated_world = World(self)

    def bring_into_view(self, f):
        """scrollIntoViewIfNeeded: an off-screen field is scrolled to the middle; a clipping
        container scrolls it out."""
        x, y, w, h = f.rect
        if y < 0 or y + h > VH:
            f.rect = (x, VH / 2 - h / 2, w, h)
        f.clipped = False

    def locator(self, sel):
        return Loc(self, sel)

    def on(self, *a, **k):
        pass

    def activate(self):
        mods = set(self.mods)
        for f in self.fields:
            if f.inside(*self.pos):
                if f.covers(*self.pos):
                    break            # the overlay takes the click: nothing gets the focus
                self.focused = f
                f.click(self.pos[0], self.pos[1], mods)
                return
        self.focused = None

    def key(self, k):
        if k == "a" and self.mods & {"ControlOrMeta", "Control", "Meta"}:
            k = "ControlOrMeta+a"   # an A pressed with the modifier held is the select-all chord
        if self.focused is not None:
            self.focused.key(k)

    # Playwright's own page methods (what humanize falls back to): recorded
    async def _native(self, name, sel, *args, **kw):
        self.native.append((name, sel) + args + (kw,))   # recorded first: a native WAIT counts too
        if sel not in self.by_sel:
            raise PWTimeout("Timeout exceeded waiting for %s" % sel)

    async def click(self, sel, **kw):
        await self._native("click", sel, **kw)

    async def hover(self, sel, **kw):
        await self._native("hover", sel, **kw)

    async def dblclick(self, sel, **kw):
        await self._native("dblclick", sel, **kw)

    async def fill(self, sel, value, **kw):
        await self._native("fill", sel, value, **kw)
        self.by_sel[sel].value = value

    async def type(self, sel, text, **kw):
        await self._native("type", sel, text, **kw)

    async def press(self, sel, key, **kw):
        await self._native("press", sel, key, **kw)

    async def check(self, sel, **kw):
        await self._native("check", sel, **kw)

    async def uncheck(self, sel, **kw):
        await self._native("uncheck", sel, **kw)

    async def set_checked(self, sel, checked, **kw):
        await self._native("set_checked", sel, checked, **kw)

    async def select_option(self, sel, value=None, **kw):
        await self._native("select_option", sel, value, **kw)
        return ["native"]


@pytest.fixture
def fast(monkeypatch):
    async def nosleep(_s=0, *a, **k):
        return None
    monkeypatch.setattr(H.asyncio, "sleep", nosleep)
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0")


async def attached(*fields):
    p = Page(*fields)
    await H.attach_humanize(None, p, humanize=True, seed="forms-unit")
    return p


def run(coro):
    return asyncio.run(coro)


def kinds(p, *names):
    return [e for e in p.log if e[0] in names]


def non_trial(p):
    return [n for n in p.native if not n[-1].get("trial")]


def _btn():
    return ("#b", Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0)))


# ---------------------------------------------------------------------------- shared planning
def test_async_module_plans_with_the_shared_forms_module():
    # Python sync and async share _forms, so test_forms.py's run of the shared vectors
    # (sdk/testdata/humanize_forms_vectors.json) pins this surface too.
    for name in ("classify_field", "typos_allowed", "typo_eligible", "plan_typo", "normalize_click_options",
                 "engine_path_ok", "plan_select", "match_option", "range_attrs", "range_sanitize",
                 "range_fraction", "range_point", "range_nudge", "numeric_equal", "plan_datetime",
                 "datetime_equal", "segment_nodes_from_tree", "range_value_ok", "formatted_equal"):
        assert getattr(H, name) is getattr(_forms, name), name


@pytest.mark.parametrize("raw,want", [(None, 0.02), ("", 0.02), ("nan", 0.02), ("x", 0.02), ("0", 0.0),
                                      ("0.35", 0.35), ("7", 1.0), ("-1", 0.0)])
def test_typo_rate_is_read_at_call_time_and_nan_is_the_default(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("CLEARCOTE_HUMANIZE_TYPO_RATE", raising=False)
    else:
        monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", raw)
    assert H.typo_rate() == want


def test_the_temporary_css_only_reads_are_gone():
    from clearcote import _isolated
    for name in ("IS_FOCUSED", "SELECT_PLAN", "SELECTED_INDEX"):
        assert not hasattr(_isolated, name)
    assert not hasattr(H, "_select_by_keyboard")


# ------------------------------------------------------------------------------------- clicks
def test_click_options_shape_the_press(fast):
    async def go():
        p = await attached(_btn())
        await p.click("#b", button="right")
        assert [(d[1], d[2]) for d in kinds(p, "down")] == [("right", 1)]
        assert not non_trial(p), "no native click: the hit test ran as a trial only"
        p.log.clear()
        await p.click("#b", click_count=2)
        assert [(d[1], d[2]) for d in kinds(p, "down")] == [("left", 1), ("left", 2)]
        p.log.clear()
        await p.dblclick("#b")
        assert [(d[1], d[2]) for d in kinds(p, "down")] == [("left", 1), ("left", 2)]
        assert not non_trial(p)
    run(go())


def test_modifiers_are_held_around_the_whole_press(fast):
    async def go():
        p = await attached(_btn())
        await p.click("#b", modifiers=["Shift"])
        seq = [(e[0], e[1]) for e in kinds(p, "kdown", "kup", "down", "up")]
        assert seq == [("kdown", "Shift"), ("down", "left"), ("up", "left"), ("kup", "Shift")]
        assert kinds(p, "down")[0][3] == {"Shift"}
    run(go())


def test_trial_performs_no_action(fast):
    # The SDK answers trial itself: Playwright's trial moves the real pointer onto the target and
    # dispatches pointerdown/pointerup/click that page listeners registered first see (SC 209).
    async def go():
        p = await attached(_btn())
        await p.click("#b", trial=True)
        await p.hover("#b", trial=True)
        await p.dblclick("#b", trial=True)
        assert not kinds(p, "down", "up", "click", "move")
        assert p.native == [], "no Playwright trial: it moves the pointer and presses"
    run(go())


def test_a_trial_on_a_covered_target_times_out_like_playwright(fast):
    async def go():
        b = Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0), covered=True)
        p = await attached(("#b", b))
        with pytest.raises(Exception) as err:
            await p.click("#b", trial=True, timeout=200)
        assert type(err.value).__name__ == "TimeoutError" and "200ms" in str(err.value)
        assert p.native == [] and not kinds(p, "down", "up", "click", "move")
    run(go())


def test_a_trial_on_a_hidden_target_raises_playwrights_timeout(fast):
    async def go():
        b = Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0))
        b.visible = False
        p = await attached(("#b", b))
        with pytest.raises(PWTimeout):
            await p.click("#b", trial=True, timeout=200)
        assert p.native == [] and not kinds(p, "down", "move")
    run(go())


def test_a_forced_trial_only_needs_the_element(fast):
    async def go():
        b = Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0), covered=True)
        p = await attached(("#b", b))
        await p.click("#b", trial=True, force=True, timeout=200)
        assert p.native == [] and not kinds(p, "down", "move")
    run(go())


def test_position_is_measured_from_the_padding_box(fast):
    async def go():
        p = await attached(_btn())
        await p.click("#b", position={"x": 10, "y": 12})
        d = kinds(p, "down")[0]
        assert d[4] == (100.0 + 2 + 10, 100.0 + 2 + 12)   # bbox + clientLeft/Top + position
    run(go())


def test_force_skips_the_hit_test(fast):
    async def go():
        p = await attached(_btn())
        await p.click("#b", force=True)
        assert p.native == [] and kinds(p, "down")
    run(go())


def test_hover_glides_and_presses_nothing(fast):
    async def go():
        p = await attached(_btn())
        await p.hover("#b")
        assert kinds(p, "move") and not kinds(p, "down", "click")
    run(go())


@pytest.mark.parametrize("act", [
    lambda p: p.click("#nope", timeout=5), lambda p: p.dblclick("#nope", timeout=5),
    lambda p: p.hover("#nope", timeout=5), lambda p: p.fill("#nope", "x", timeout=5),
    lambda p: p.select_option("#nope", "x", timeout=5), lambda p: p.check("#nope", timeout=5)])
def test_a_timeout_propagates_and_never_waits_again_natively(fast, act):
    async def go():
        p = await attached(_btn())
        with pytest.raises(PWTimeout):
            await act(p)
        assert p.native == [], "no second (native) wait after the humanized one timed out"
    run(go())


@pytest.mark.parametrize("act", [lambda p: p.type("#nope", "x", timeout=5),
                                 lambda p: p.press("#nope", "a", timeout=5)])
def test_type_and_press_on_a_missing_target_wait_once_natively(fast, act):
    # Native type/press make no visibility wait, so the humanized route does not make one either: it
    # asks is_visible() (no wait) and hands a target that is not a visible text field to Playwright.
    async def go():
        p = await attached(_btn())
        with pytest.raises(PWTimeout):
            await act(p)
        assert len(p.native) == 1 and p.waits == [], "one wait, Playwright's own"
    run(go())


def test_the_callers_timeout_travels_unchanged(fast):
    # None = Playwright's page default (set_default_timeout); a hard-coded 30 s overrode it both ways.
    # The wait is made only for a target that is not visible yet (each wait makes a main-world handle).
    async def go():
        b = Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0), appear_on_wait=True)
        f = Field(rect=(100.0, 200.0, 200.0, 24.0), appear_on_wait=True)
        p = await attached(("#b", b), ("#f", f))
        for kw, want in (({}, [None]), ({"timeout": 1500}, [1500])):
            b.visible = False
            p.waits.clear()
            await p.click("#b", **kw)
            assert p.waits == want and kinds(p, "down")
        f.visible = False
        p.waits.clear()
        await p.fill("#f", "x", timeout=2500)
        assert p.waits == [2500] and f.value == "x"
        p.waits.clear()
        await p.click("#b")
        assert p.waits == [], "a visible target is not waited for"
    run(go())


def test_force_on_a_hidden_target_goes_native_without_waiting(fast):
    async def go():
        hb = Field(tag="button", type="", visible=False)
        hd = Field(type="date", visible=False, rect=(100.0, 200.0, 160.0, 24.0))
        p = await attached(("#hb", hb), ("#hd", hd))
        await p.click("#hb", force=True)
        await p.fill("#hd", "2026-10-11", force=True)
        assert [n[:2] for n in p.native] == [("click", "#hb"), ("fill", "#hd")]
        assert p.waits == [] and not kinds(p, "down", "click", "move")
    run(go())


def test_mouse_click_xy_takes_the_options(fast):
    async def go():
        p = await attached(_btn())
        await p.mouse.click(120, 120, button="right")
        assert [(d[1], d[2]) for d in kinds(p, "down")] == [("right", 1)]
        p.log.clear()
        await p.mouse.dblclick(120, 120)
        assert [(d[1], d[2]) for d in kinds(p, "down")] == [("left", 1), ("left", 2)]
        assert not kinds(p, "native-dblclick"), "stock dblclick teleports and presses for ~0 ms"
    run(go())


class _Session:
    def __init__(self):
        self.calls = []

    async def send(self, method, params=None):
        self.calls.append(method)
        return {"targetInfo": {"targetId": "T"}} if method == "Target.getTargetInfo" else {}


def test_mouse_click_xy_uses_the_engine_path_only_for_a_plain_left_click(fast):
    async def go():
        p = await attached(_btn())
        s = _Session()

        async def new_cdp_session(page):
            return s
        p.context = type("Ctx", (), {"new_cdp_session": staticmethod(new_cdp_session), "browser": None})()
        await p.mouse.click(300, 200)
        await p.mouse.click(300, 200, button="right")
        await p.mouse.click(300, 200, delay=250)
        await p.mouse.click(300, 200, modifiers=["Shift"])
        assert [c for c in s.calls if c.startswith("Browser.")] == ["Browser.dispatchPointerPath"]
        assert [(d[1]) for d in kinds(p, "down")] == ["right", "left", "left"]
    run(go())


# ------------------------------------------------------------------------------------- text
def test_fill_an_empty_field_types_and_presses_no_clear_keys(fast):
    async def go():
        f = Field()
        p = await attached(("#f", f))
        await p.fill("#f", "hello")
        assert f.value == "hello"
        assert not [e for e in kinds(p, "press") if e[1] in ("ControlOrMeta", "ControlOrMeta+a", "Backspace")]
        assert not [e for e in kinds(p, "kdown") if e[1] == "ControlOrMeta"]
        assert len(kinds(p, "down")) == 1 and len(kinds(p, "up")) == 1, "focused with ONE trusted press"
        assert not kinds(p, "click"), "no mouse.click: it moves the pointer again right before the press"
        assert p.native == []
    run(go())


def test_fill_clears_a_prefilled_field_with_held_keys(fast):
    async def go():
        f = Field(value="old value")
        p = await attached(("#f", f))
        await p.fill("#f", "new")
        assert f.value == "new"
        keys = [(e[0], e[1]) for e in kinds(p, "kdown", "kup", "press")][:4]
        assert keys == [("kdown", "ControlOrMeta"), ("press", "a"), ("kup", "ControlOrMeta"),
                        ("press", "Backspace")], "select-all with the modifier HELD around the A"
        assert all(e[2] is not None and e[2] >= 25 for e in kinds(p, "press")[:2]), "held like a finger"
        assert p.native == []
    run(go())


def test_select_all_leads_and_lags_the_a_with_the_modifier(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0")

    async def go():
        f = Field(value="old")
        p = await attached(("#f", f))

        async def sleep(s=0, *a, **k):   # record the pauses between the keys
            p.log.append(("sleep", s))
        monkeypatch.setattr(H.asyncio, "sleep", sleep)
        await p.fill("#f", "x")
        ev = [e for e in p.log if e[0] in ("kdown", "kup", "press", "sleep")]
        i = next(k for k, e in enumerate(ev) if e[:2] == ("kdown", "ControlOrMeta"))
        a = next(k for k, e in enumerate(ev) if e[:2] == ("press", "a"))
        j = next(k for k, e in enumerate(ev) if e[:2] == ("kup", "ControlOrMeta"))
        lead = sum(e[1] for e in ev[i:a] if e[0] == "sleep")
        lag = sum(e[1] for e in ev[a:j] if e[0] == "sleep")
        assert lead >= 0.040 and lag >= 0.020, (lead, lag)
        assert f.value == "x"
    run(go())


def test_a_reformatting_mask_is_typed_once(fast):
    def phone(v):
        d = "".join(c for c in v if c.isdigit())[:10]
        return "(%s) %s-%s" % (d[:3], d[3:6], d[6:]) if len(d) > 6 else d

    async def go():
        f = Field(type="tel", fmt=phone)
        p = await attached(("#f", f))
        await p.fill("#f", "5551234567")
        assert f.value == "(555) 123-4567" and p.native == []
        digits = [e for e in p.log if e[0] in ("press", "kdown") and e[1].isdigit()]
        assert len(digits) == 10, "a person does not type their number twice"
    run(go())


def _expiry(v):   # MM/YY: the slash comes in with the third digit
    d = "".join(c for c in v if c.isdigit())[:4]
    return d[:2] + "/" + d[2:] if len(d) > 2 else d


def _tel_dashes(v):   # 555-123-4567
    d = "".join(c for c in v if c.isdigit())[:10]
    return "-".join(x for x in (d[:3], d[3:6], d[6:]) if x)


def _postcode(v):   # 1234 AB: upper case, a space after the digits, six places at most
    d = "".join(c for c in v.upper() if c.isalnum())[:6]
    return d[:4] + " " + d[4:] if len(d) > 4 else d


SELECT_ALL_OR_ERASE = ("ControlOrMeta", "ControlOrMeta+a", "Backspace")


@pytest.mark.parametrize("fmt,maxlength,value,want,keys", [
    (_expiry, 5, "123456", "12/34", "12345"),                 # verifier B: typed, Ctrl+A, Backspace, retyped
    (_tel_dashes, 12, "5551234567890", "555-123-4567", "555123456789"),
    (_postcode, -1, "1234abx", "1234 AB", "1234abx"),         # a JS mask with no maxlength cuts it too
], ids=["mmyy-maxlength5", "tel-maxlength12", "postcode-js"])
def test_a_mask_that_stops_taking_keys_is_typed_once(fast, fmt, maxlength, value, want, keys):
    """A mask that re-punctuates what is typed and then stops taking keys leaves what a person typing
    the value gets (and what Playwright's fill leaves): no wipe, no retype, no Playwright fill."""
    async def go():
        f = Field(type="tel" if fmt is _tel_dashes else "text", fmt=fmt, maxlength=maxlength)
        p = await attached(("#f", f))
        await p.fill("#f", value)
        assert f.value == want and p.native == [], (f.value, p.native)
        typed = [e[1] for e in p.log if e[0] in ("press", "kdown") and len(e[1]) == 1]
        assert "".join(typed) == keys, typed
        assert not [e for e in p.log if e[0] in ("press", "kdown") and e[1] in SELECT_ALL_OR_ERASE]
    run(go())


def test_a_value_cut_short_with_room_left_is_still_typed_again(fast):
    """mask_kept is for masks: a plain field that came out short (a lost key), with room left and
    nothing re-punctuated, still gets the one clean retry."""
    async def go():
        f = Field(maxlength=8)
        p = await attached(("#f", f))
        orig = f.key
        lost = {"n": 0}

        def key(k):   # the last key of the first pass never lands: "12345", a prefix of the value
            if k == "6" and not lost["n"]:
                lost["n"] = 1
                return None
            return orig(k)
        f.key = key
        await p.fill("#f", "123456")
        assert f.value == "123456" and p.native == []
        assert [e for e in p.log if e[0] in ("press", "kdown") and e[1] in SELECT_ALL_OR_ERASE[:2]], "retyped once"
    run(go())


def _alnum_only(v):   # a field that strips separators as they are typed
    return "".join(c for c in v if c.isalnum())


@pytest.mark.parametrize("fmt,value,want,lost", [
    (str.lower, "ABC126", "abc126", "6"),                  # a field that lower-cases: "abc12" is not a mask
    (_alnum_only, "555-123-4567", "5551234567", "7"),      # one that strips separators: "555123456" is not either
    (None, "123456", "123456", "6"),                       # a plain field: as before
], ids=["lowercase", "strip-punct", "plain"])
def test_a_lost_key_in_a_field_that_normalizes_is_typed_again(fast, fmt, value, want, lost):
    """Round 3: mask_kept read a change of case or a stripped separator as a mask that cuts, and accepted
    a value missing its last key ("abc12" for "ABC126"). Only punctuation the page ADDED marks such a
    mask; here the lost key is noticed and the value gets its one clean retry."""
    async def go():
        f = Field(type="tel" if fmt is _alnum_only else "text", fmt=fmt)
        p = await attached(("#f", f))
        orig = f.key
        dropped = {"n": 0}

        def key(k):   # the first press of the last key never lands
            if k == lost and not dropped["n"]:
                dropped["n"] = 1
                return None
            return orig(k)
        f.key = key
        await p.fill("#f", value)
        assert dropped["n"] == 1
        assert f.value == want and p.native == [], (f.value, p.native)
        assert [e for e in p.log if e[0] in ("press", "kdown") and e[1] in SELECT_ALL_OR_ERASE[:2]], "retyped once"
    run(go())


def test_a_covered_field_goes_to_playwright_and_no_keys_go_astray(fast):
    async def go():
        f = Field(value="old", covered=True)
        p = await attached(("#f", f))
        await p.fill("#f", "new")
        assert [n[:3] for n in p.native] == [("fill", "#f", "new")] and f.value == "new"
        assert not kinds(p, "press", "kdown"), "the click hit the overlay: no keys may follow it"
    run(go())


def test_a_field_whose_value_cannot_be_read_is_never_a_success(fast):
    async def go():
        f = Field(value="old")
        p = await attached(("#f", f))

        async def unreadable(timeout=None):
            raise RuntimeError("Element is not attached")
        loc = Loc(p, "#f")
        loc.input_value = unreadable
        p.locator = lambda sel, _l=loc: _l if sel == "#f" else Loc(p, sel)
        await p.fill("#f", "new")
        assert [n[:3] for n in p.native] == [("fill", "#f", "new")]
    run(go())


def test_a_field_that_ignores_select_all_is_emptied_from_the_end(fast):
    async def go():
        f = Field(value="old value", no_select_all=True)
        p = await attached(("#f", f))
        await p.fill("#f", "new")
        assert f.value == "new"
        keys = [e[1] for e in kinds(p, "press")]
        assert "End" in keys and keys.count("Backspace") == 1 + len("old valu")
    run(go())


def test_a_wrong_value_is_retyped_once_then_handed_to_playwright(fast):
    async def go():
        f = Field(mask=lambda ch: ch != "x")     # the page rejects one letter of the value
        p = await attached(("#f", f))
        await p.fill("#f", "axb")
        assert [n[:3] for n in p.native] == [("fill", "#f", "axb")]
        assert f.value == "axb"
        typed, mod = 0, False   # an "a" pressed with the modifier held is the select-all, not typing
        for e in p.log:
            if e[0] in ("kdown", "kup") and e[1] == "ControlOrMeta":
                mod = e[0] == "kdown"
            elif e[0] in ("press", "kdown") and e[1] == "a" and not mod:
                typed += 1
        assert typed == 2   # typed twice (the first pass and the clean retry), then Playwright
    run(go())


def test_type_does_not_click_a_field_that_already_has_the_caret(fast):
    async def go():
        f = Field(value="ab")
        p = await attached(("#f", f))
        p.focused = f
        await p.type("#f", "cd")
        assert f.value == "abcd" and not kinds(p, "click", "down")
        p.focused = None
        await p.type("#f", "e")
        assert f.value == "abcde" and len(kinds(p, "down")) == 1
    run(go())


def test_press_holds_the_key_and_honours_delay(fast):
    async def go():
        f = Field(value="x")
        p = await attached(("#f", f))
        p.focused = f
        await p.press("#f", "Backspace")
        assert f.value == "" and not kinds(p, "click")
        assert kinds(p, "press")[-1][2] >= 25
        await p.press("#f", "a", delay=300)
        assert kinds(p, "press")[-1][1:] == ("a", 300)
    run(go())


def test_keyboard_press_holds_for_the_persona_unless_a_real_delay_is_given(fast):
    # Playwright's default delay is 0 ("no hold"): an explicit 0 or a negative one is no hold either.
    async def go():
        p = await attached(_btn())
        for kw in ({}, {"delay": 0}, {"delay": -3}, {"delay": None}):
            await p.keyboard.press("Tab", **kw)
            assert kinds(p, "press")[-1][2] >= 25, kw
        await p.keyboard.press("Tab", delay=300)
        assert kinds(p, "press")[-1][2] == 300
    run(go())


def test_a_bulk_value_is_clicked_into_then_inserted(fast):
    # Over 200 characters: still one insert (typing thousands of keys would crawl), but after a glide
    # and a press into the field, the way a person pastes. Before: Playwright's fill, no pointer at all.
    async def go():
        f = Field(value="old")
        p = await attached(("#f", f))
        await p.fill("#f", "z" * 201)
        assert f.value == "z" * 201 and p.native == []
        seq = [e[0] for e in p.log if e[0] in ("move", "down", "up", "insert", "press")]
        assert seq.index("down") < seq.index("insert") and "move" in seq[:seq.index("down")]
        assert [e[1] for e in kinds(p, "insert")] == ["z" * 201]
        assert not [e for e in kinds(p, "press", "kdown") if e[1] == "z"], "not typed key by key"
    run(go())


def test_a_bulk_value_respects_maxlength_and_never_falls_back(fast):
    async def go():
        f = Field(maxlength=250)
        p = await attached(("#f", f))
        await p.fill("#f", "q" * 300)
        assert f.value == "q" * 250 and p.native == []
        assert [e[1] for e in kinds(p, "insert")] == ["q" * 250]
    run(go())


def test_maxlength_types_only_what_fits(fast):
    # fill('1234567') on maxlength=5 typed all seven, saw 12345, wiped it, typed seven again and then
    # handed the field to Playwright (measured on r38). Playwright's own fill leaves 12345 too.
    async def go():
        f = Field(maxlength=5)
        p = await attached(("#f", f))
        await p.fill("#f", "1234567")
        assert f.value == "12345" and p.native == []
        typed = [e[1] for e in kinds(p, "press", "kdown") if len(e[1]) == 1]
        assert typed == list("12345"), typed
        assert not [e for e in kinds(p, "press") if e[1] in ("Backspace", "ControlOrMeta+a")]
        assert not kinds(p, "insert")
    run(go())


def test_non_string_values_go_native(fast):
    async def go():
        f = Field()
        p = await attached(("#f", f))
        await p.fill("#f", 5)
        assert [n[0] for n in p.native] == ["fill"] and not kinds(p, "press", "kdown", "insert")
    run(go())


def _sleeps(monkeypatch):
    """Record every asyncio.sleep (seconds) humanize makes, in order with the pointer log."""
    async def rec(s=0, *a, **k):
        _sleeps.page.log.append(("sleep", s))
    monkeypatch.setattr(H.asyncio, "sleep", rec)


def _pause_before_down(p):
    """Seconds slept between the last pointer move and each press."""
    out, acc = [], None
    for e in p.log:
        if e[0] == "move":
            acc = 0.0
        elif e[0] == "sleep" and acc is not None:
            acc += e[1]
        elif e[0] == "down":
            out.append(acc)
            acc = None
    return out


@pytest.mark.parametrize("act", ["fill", "type", "click", "range", "drag"])
def test_every_sdk_press_waits_after_the_pointer_arrives(fast, monkeypatch, act):
    # The focus click before typing pressed ~4 ms after the last pointermove and a drag grab ~21 ms
    # (r38). Every SDK-issued press now waits the identity's pre-press pause, 40-250 ms.
    async def go():
        f = Field(type="range" if act == "range" else "text", rmin="0", rmax="100", rstep="1",
                  value="10" if act == "range" else "", rect=(100.0, 300.0, 200.0, 24.0))
        p = await attached(("#f", f), _btn())
        _sleeps.page = p
        _sleeps(monkeypatch)
        if act == "fill":
            await p.fill("#f", "abc")
        elif act == "type":
            await p.type("#f", "abc")
        elif act == "click":
            await p.click("#b")
        elif act == "range":
            random.seed(3)   # 0.65: the thumb is grabbed and dragged
            await p.fill("#f", "60")
        else:
            await p.mouse.move(150, 120)
            await p.mouse.down()
            await p.mouse.up()
        pauses = _pause_before_down(p)
        assert pauses, "a press was made"
        assert all(x is not None and 0.040 <= x for x in pauses), pauses
    run(go())


def test_a_bare_mouse_down_with_no_move_stays_instant(fast, monkeypatch):
    async def go():
        p = await attached(_btn())
        _sleeps.page = p
        _sleeps(monkeypatch)
        await p.mouse.down()
        await p.mouse.up()
        assert not kinds(p, "sleep"), "nothing arrived, so nothing to wait for"
    run(go())


def test_unfillable_and_colour_fields_go_native(fast):
    async def go():
        cb, col = Field(type="checkbox"), Field(type="color", rect=(100.0, 200.0, 60.0, 24.0))
        p = await attached(("#cb", cb), ("#col", col))
        await p.fill("#cb", "x")
        await p.fill("#col", "#ff0000")
        assert [n[:3] for n in p.native] == [("fill", "#cb", "x"), ("fill", "#col", "#ff0000")]
        assert not kinds(p, "press", "kdown", "click")
    run(go())


# ------------------------------------------------------------------------------------- typos
def test_typos_in_a_masked_field_backspace_only_what_landed(fast, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "1")
    random.seed(197)

    async def go():
        # every neighbour of 2/4/6/8/0 is an odd digit, which this mask swallows
        f = Field(mask=lambda ch: ch in "24680")
        p = await attached(("#f", f))
        p.focused = f
        await p.keyboard.type("24680")
        return p, f
    p, f = run(go())
    assert f.value == "24680", "a Backspace for a swallowed slip deletes a real digit"
    assert [e for e in kinds(p, "press") if e[1] in "13579"], "slips were attempted (and swallowed)"


def test_typos_respect_maxlength(fast, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "1")
    random.seed(41)

    async def go():
        f = Field(maxlength=5)
        p = await attached(("#f", f))
        for _ in range(3):
            f.value = ""
            p.focused = f
            await p.keyboard.type("12345")
            assert f.value == "12345"
    run(go())


def test_forced_typos_are_corrected(fast, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0.4")
    random.seed(7)

    async def go():
        f = Field()
        p = await attached(("#f", f))
        text = "the quick brown fox jumps over 2024"
        await p.fill("#f", text)
        assert f.value == text and p.native == []
        assert [e for e in kinds(p, "press") if e[1] == "Backspace"], "slips were made and corrected"
    run(go())


@pytest.mark.parametrize("field", [dict(type="password"), dict(type="email"), dict(type="tel"),
                                   dict(type="text", autocomplete="one-time-code"),
                                   dict(type="text", inputmode="numeric")])
def test_no_typos_where_a_slip_breaks_the_field(fast, monkeypatch, field):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "1")

    async def go():
        f = Field(**field)
        p = await attached(("#f", f))
        await p.fill("#f", "abc123def")
        assert f.value == "abc123def"
        assert not [e for e in kinds(p, "press") if e[1] == "Backspace"]
        f.value = ""
        await p.keyboard.type("abc123")    # keyboard.type reads the focused field's type itself
        assert f.value == "abc123" and not [e for e in kinds(p, "press") if e[1] == "Backspace"]
    run(go())


def test_typo_rate_zero_disables_slips(fast, monkeypatch):
    async def go():
        f = Field()
        p = await attached(("#f", f))
        await p.fill("#f", "the quick brown fox")
        return p
    p = run(go())
    assert not [e for e in kinds(p, "press") if e[1] == "Backspace"]


# ------------------------------------------------------------------------------------ select
COUNTRIES = [("afg0", "Afghanistan"), ("alb1", "Albania"), ("ger2", "Germany"), ("gha3", "Ghana"),
             ("gre4", "Greece"), ("bra5", "Bravo", True), ("uru6", "Uruguay")]


def _select(**kw):
    return ("#s", Field(tag="select", options=COUNTRIES, **kw))


def test_select_opens_the_drop_down_types_and_commits_with_enter(fast):
    async def go():
        sel = _select()
        p = await attached(sel)
        s = sel[1]
        assert await p.select_option("#s", "Germany") == ["ger2"]
        assert s.selected_index == 2 and s.changes == 1 and s.opens == 1
        ev = [e for e in p.log if e[0] in ("down", "press")]
        assert ev[0][0] == "down", "opened with the mouse before any key"
        assert ev[-1][:2] == ("press", "Enter")
        plan = _forms.plan_select(s.labels(), 0, 2)
        typed = "".join(e[1] for e in ev[1:-1] if len(e[1]) == 1)
        assert typed == plan["keys"]
        assert all(e[2] is not None and e[2] >= 25 for e in ev[1:]), "keys held like a finger"
        assert not non_trial(p), "the opening click is the humanized one (its cover check is a trial)"
    run(go())


def test_a_select_under_a_floating_overlay_is_opened_where_the_hit_test_proved_it(fast):
    async def go():
        sel = ("#s", Field(tag="select", options=COUNTRIES, cover_center=True))
        p = await attached(sel)
        assert await p.select_option("#s", "Germany") == ["ger2"]
        s = sel[1]
        assert s.opens == 1 and s.changes == 1 and not non_trial(p)
        x = kinds(p, "down")[0][4][0]
        assert not s.covers(x, s.rect[1] + 1), "pressed beside the overlay, not under it"
    run(go())


@pytest.mark.parametrize("args,kw,want", [(("gre4",), {}, "gre4"), ((None,), {"index": 3}, "gha3"),
                                         ((None,), {"label": "Uruguay"}, "uru6"),
                                         ((["Ghana", "Albania"],), {}, "alb1")])
def test_select_value_index_label_first_match_wins(fast, args, kw, want):
    async def go():
        sel = _select()
        p = await attached(sel)
        assert await p.select_option("#s", *args, **kw) == [want]
        assert sel[1].options[sel[1].selected_index]["value"] == want and sel[1].changes == 1
    run(go())


def test_select_the_current_option_fires_nothing(fast):
    async def go():
        sel = _select(selected_index=2)
        p = await attached(sel)
        assert await p.select_option("#s", "ger2") == ["ger2"]
        assert not kinds(p, "click", "press", "down") and sel[1].changes == 0 and p.native == []
    run(go())


@pytest.mark.parametrize("args,kw", [(("bra5",), {}), (("nowhere",), {}), ((None,), {"element": object()})])
def test_disabled_missing_and_element_options_go_native(fast, args, kw):
    async def go():
        sel = _select()
        p = await attached(sel)
        assert await p.select_option("#s", *args, **kw) == ["native"]
        assert not kinds(p, "click", "down", "press") and sel[1].opens == 0
    run(go())


def test_a_drop_down_that_did_not_commit_gets_one_arrow_correction_then_native(fast):
    async def go():
        sel = _select(never_commit=True)
        p = await attached(sel)
        assert await p.select_option("#s", "Greece") == ["native"]
        assert sel[1].opens == 2, "one correction, then Playwright"
        second = [e for e in p.log if e[0] in ("down", "press")]
        k = max(i for i, e in enumerate(second) if e[0] == "down")
        assert all(e[1] in ("ArrowDown", "ArrowUp", "Enter") for e in second[k + 1:]), "arrows only"
    run(go())


def test_a_list_box_is_clicked_row_by_row_with_ctrl_for_the_rest(fast):
    async def go():
        ms = ("#ms", Field(tag="select", options=[(c, "Item " + c.upper()) for c in "abcdefg"],
                           multiple=True, size=7, rect=(100.0, 100.0, 200.0, 7 * ROW_H + 4)))
        p = await attached(ms)
        got = await p.select_option("#ms", ["b", "d", "f"])
        assert sorted(got) == ["b", "d", "f"] and ms[1].selected == {1, 3, 5}
        presses = kinds(p, "click", "down")
        assert presses[0][0] == "down" and presses[0][3] == frozenset(), "the first row: a plain press"
        assert [d[3] for d in presses[1:]] == [{"ControlOrMeta"}, {"ControlOrMeta"}]
        assert p.native == []
    run(go())


def test_a_single_select_list_box_takes_the_first_match(fast):
    async def go():
        lb = ("#lb", Field(tag="select", options=[("o%d" % i, "Option %d" % i) for i in range(5)], size=5,
                           rect=(100.0, 100.0, 200.0, 5 * ROW_H + 4)))
        p = await attached(lb)
        assert await p.select_option("#lb", ["o3", "o1"]) == ["o1"]
        assert lb[1].selected == {1}
    run(go())


# ------------------------------------------------------------------------------------- check
def test_check_uncheck_set_checked_click_only_when_the_state_must_change(fast):
    async def go():
        cb = Field(type="checkbox", rect=(100.0, 100.0, 16.0, 16.0))
        p = await attached(("#cb", cb))
        await p.check("#cb")
        assert cb.checked and len(kinds(p, "down")) == 1
        await p.check("#cb")
        assert cb.checked and len(kinds(p, "down")) == 1, "already checked: nothing happens"
        await p.set_checked("#cb", False)
        assert not cb.checked and len(kinds(p, "down")) == 2
        await p.uncheck("#cb")
        assert len(kinds(p, "down")) == 2
        assert not non_trial(p), "no native check/uncheck: the humanized click did it"
    run(go())


def test_a_checkbox_that_cancels_its_click_is_clicked_once_and_raises(fast):
    from playwright.async_api import Error

    async def go():
        cb = Field(type="checkbox", rect=(100.0, 100.0, 16.0, 16.0), stubborn=True)
        p = await attached(("#cb", cb))
        with pytest.raises(Error, match="did not change its state"):
            await p.check("#cb")
        assert cb.clicks == 1 and len(kinds(p, "down")) == 1, "one click, no native retry"
        assert not non_trial(p)
    run(go())


def test_check_with_trial_performs_no_action(fast):
    async def go():
        cb = Field(type="checkbox", rect=(100.0, 100.0, 16.0, 16.0))
        p = await attached(("#cb", cb))
        await p.check("#cb", trial=True)
        await p.set_checked("#cb", True, trial=True)
        assert not cb.checked and not kinds(p, "down", "click", "move")
        assert p.native == [], "no Playwright trial: it moves the pointer and presses"
    run(go())


# ------------------------------------------------------------------------------- locator patch
class _Impl:
    def __init__(self, parent=None, selector="#b"):
        self._selector = selector
        self._frame = type("F", (), {"parent_frame": parent})()
        self.calls = []

    def __getattr__(self, name):
        async def native(**kw):
            self.calls.append((name, kw))
        return native


class _HPage:
    _clearcote_humanized = True

    def __init__(self, raise_=None):
        self.calls, self.raise_ = [], raise_

    def __getattr__(self, name):
        async def method(sel, *a, **kw):
            self.calls.append((name, sel) + a + (kw,))
            if self.raise_ is not None:
                raise self.raise_
        return method


class _Self:
    """Just what the patched async Locator methods read from a Locator."""

    def __init__(self, n=1, raise_=None, parent=None, selector="#b"):
        self._impl_obj, self.page, self.n = _Impl(parent, selector), _HPage(raise_), n

    async def count(self):
        return self.n


@pytest.fixture
def Locator():
    from playwright.async_api._generated import Locator as L
    H._patch_locator_class()
    return L


def test_locator_actions_forward_their_options(Locator):
    async def go():
        s = _Self()
        await Locator.click(s, button="right", click_count=2, modifiers=["Shift"], position={"x": 1, "y": 2},
                            timeout=1500, trial=None)
        await Locator.dblclick(s, delay=90)
        await Locator.press(s, "Enter", delay=60)
        await Locator.select_option(s, None, index=2, timeout=900)
        await Locator.set_checked(s, True, force=True)
        await Locator.tap(s, position={"x": 3, "y": 4})
        return s
    s = run(go())
    # a tap on a touch context is reported as a fallback (the stub page answers every attribute, so it
    # stands in for lane A's _report_fallback hook), then routed as the humanized click
    reported = [c for c in s.page.calls if c[0] == "_report_fallback"]
    assert len(reported) == 1 and reported[0][1]["api"] == "Locator.tap"
    assert [c for c in s.page.calls if c[0] != "_report_fallback"] == [
        ("click", "#b", {"button": "right", "click_count": 2, "modifiers": ["Shift"], "position": {"x": 1, "y": 2},
                         "timeout": 1500}),
        ("dblclick", "#b", {"delay": 90}),
        ("press", "#b", "Enter", {"delay": 60}),
        ("select_option", "#b", None, {"index": 2, "timeout": 900}),
        ("set_checked", "#b", True, {"force": True}),
        ("click", "#b", {"position": {"x": 3, "y": 4}}),
    ]
    assert s._impl_obj.calls == []


def test_locator_actions_stay_strict(Locator):
    async def go():
        s = _Self(n=2)
        await Locator.click(s, timeout=10)
        await Locator.fill(s, "x")
        return s
    s = run(go())
    assert s.page.calls == [], "two matches: the humanized page method (first match) is not used"
    assert [c[0] for c in s._impl_obj.calls] == ["click", "fill"], "native raises its strict-mode error"


def test_a_routed_locator_action_lets_every_error_through_and_never_reruns_natively(Locator):
    # The page wrapper owns the native fallback. Re-running the locator's own native method after it
    # raised clicked a checkbox whose click is cancelled three times (verify-final-A, S1).
    async def go():
        s = _Self(raise_=PWTimeout("t"))
        with pytest.raises(PWTimeout):
            await Locator.click(s, timeout=10)
        assert s._impl_obj.calls == []
        s2 = _Self(raise_=RuntimeError("Clicking the checkbox did not change its state"))
        with pytest.raises(RuntimeError):
            await Locator.check(s2, timeout=10)
        assert [c[0] for c in s2.page.calls] == ["check"] and s2._impl_obj.calls == []
    run(go())


@pytest.mark.parametrize("kw", [dict(parent=object()),
                                dict(selector="#fr >> internal:control=enter-frame >> #fi")])
def test_frame_locators_take_the_frame_route_and_go_native_with_a_report_when_it_cannot_run(Locator, kw):
    # A FrameLocator's child keeps the MAIN frame as its _frame; only its selector shows the iframe. It
    # no longer goes through the page-level methods (they act on the main frame); it takes the frame
    # route (_frames), which cannot run on this stub (no element waits, no CDP) -> Playwright's own
    # method, with the fallback reported (lane F, SC 232; was: native, silently).
    async def go():
        s = _Self(**kw)
        await Locator.fill(s, "new")
        return s
    s = run(go())
    assert [c[0] for c in s._impl_obj.calls] == ["fill"], "Playwright's own fill, once"
    assert [c[0] for c in s.page.calls] == ["_report_fallback"], "no page.* method; the fallback reported"
    assert s.page.calls[0][1]["api"] == "Locator.fill"


# ------------------------------------------------------------------- press / type / range targets
def test_press_on_a_button_goes_to_playwright_without_a_click(fast):
    async def go():
        p = await attached(_btn())
        await p.press("#b", "Enter")
        assert [n[:3] for n in p.native] == [("press", "#b", "Enter")]
        assert not kinds(p, "click", "down", "press"), "a click first activated the button a second time"
    run(go())


def test_type_into_a_hidden_helper_textarea_goes_native_at_once(fast):
    async def go():
        h = Field(tag="textarea", type="", visible=False)
        p = await attached(("#h", h))
        await p.type("#h", "abc", timeout=3000)
        assert [n[:3] for n in p.native] == [("type", "#h", "abc")] and p.waits == []
    run(go())


def test_range_values_playwright_rejects_go_to_playwright_untouched(fast):
    async def go():
        r = Field(type="range", value="3", rmin="3", rmax="20", rstep="4")
        p = await attached(("#r", r))
        for bad in ("13", "250", "nan"):
            await p.fill("#r", bad)
        assert [n[:3] for n in p.native] == [("fill", "#r", "13"), ("fill", "#r", "250"), ("fill", "#r", "nan")]
        assert not kinds(p, "move", "down", "click", "press"), "never a silently snapped or clamped value"
    run(go())


def test_the_typo_gate_fails_closed_when_the_focus_is_in_a_frame(fast, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "1")

    async def go():
        f = Field()
        p = await attached(("#f", f))
        p.focused, p.frame_focus = f, True    # ACTIVE_FIELD reads {frame: true}
        await p.keyboard.type("abcdef")
        assert f.value == "abcdef"
        assert not [e for e in kinds(p, "press") if e[1] == "Backspace"], "no slips in a field we cannot see"
    run(go())


def test_delay_zero_or_negative_still_holds_the_button(monkeypatch):
    async def go():
        p = await attached(_btn())

        async def sleep(s=0, *a, **k):
            p.log.append(("sleep", s))
        monkeypatch.setattr(H.asyncio, "sleep", sleep)
        for act in (lambda: p.click("#b", delay=0), lambda: p.click("#b", delay=-5),
                    lambda: p.mouse.click(120, 120, delay=0)):
            p.log.clear()
            await act()
            ev = [e for e in p.log if e[0] in ("down", "up", "sleep")]
            i = next(k for k, e in enumerate(ev) if e[0] == "down")
            j = next(k for k, e in enumerate(ev) if e[0] == "up")
            hold = sum(e[1] for e in ev[i:j] if e[0] == "sleep")
            assert hold >= 0.040, hold   # the persona's hold, not ~1 ms (and never a negative sleep)
    run(go())


# ------------------------------------------------------------- async only: one action at a time
def test_concurrent_fills_on_one_page_are_serialised(fast):
    # Unlocked, the two fills interleave at every await: each clicks its field, then both type into
    # whichever was clicked last (measured on this simulator before the lock).
    async def go():
        a, b = Field(), Field(rect=(100.0, 200.0, 200.0, 24.0))
        p = await attached(("#a", a), ("#b", b))
        await asyncio.gather(p.fill("#a", "the quick brown fox"), p.fill("#b", "jumps over the dog"))
        assert (a.value, b.value) == ("the quick brown fox", "jumps over the dog")
        assert p.native == []
    run(go())


def test_no_entry_point_deadlocks_on_the_page_lock(fast):
    # Every public entry point takes the lock; the helpers inside must never call another one.
    async def go():
        cb = Field(type="checkbox", rect=(100.0, 300.0, 16.0, 16.0))
        sel = Field(tag="select", options=COUNTRIES, rect=(100.0, 400.0, 200.0, 24.0))
        txt = Field(rect=(100.0, 500.0, 200.0, 24.0))
        p = await attached(_btn(), ("#cb", cb), ("#s", sel), ("#t", txt))

        async def everything():
            await p.dblclick("#b")
            await p.check("#cb")
            await p.set_checked("#cb", False)
            await p.select_option("#s", "Ghana")
            await p.mouse.dblclick(120, 120)
            await p.hover("#b")
            await p.press("#b", "Enter")
            await p.fill("#t", "abc")
            await p.type("#t", "d")
            await p.press("#t", "Backspace")
            await p.keyboard.press("Tab")
            await p.keyboard.type("x")
        await asyncio.wait_for(everything(), 10)
        assert txt.value == "abcx" and cb.checked is False and sel.selected_index == 3
    run(go())


# ------------------------------------------------------------- async only: cancellation safety
def _arm_on(monkeypatch):
    """asyncio.sleep that is instant until armed (and for pauses under 1 s), then blocks until
    cancelled: a cancellation lands exactly where the action is mid-press. Returns the arm switch."""
    state = {"armed": False}

    async def sleep(s=0, *a, **k):
        if state["armed"] or s >= 1.0:
            await _real_sleep(3600)
    monkeypatch.setattr(H.asyncio, "sleep", sleep)
    return state


def test_a_click_cancelled_mid_press_releases_the_button_and_the_modifier(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0")
    _arm_on(monkeypatch)   # the 5 s hold blocks

    async def go():
        p = await attached(_btn())
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(p.click("#b", delay=5000, modifiers=["Shift"]), 0.5)
        assert len(kinds(p, "down")) == 1 and len(kinds(p, "up")) == 1, "the button came up"
        assert [e[1] for e in kinds(p, "kdown")] == ["Shift"] == [e[1] for e in kinds(p, "kup")]
        p.log.clear()
        await asyncio.wait_for(p.click("#b"), 5)   # the page lock was released too
        assert [(d[1], d[2]) for d in kinds(p, "down")] == [("left", 1)]
    run(go())


SAME = [("s%d" % i, "Same") for i in range(30)]   # 30 rows type-ahead cannot tell apart


@pytest.mark.parametrize("arm_key,act", [
    ("a", lambda p: p.keyboard.type("asdf")),                 # mid-rollover: "a" is down
    ("ControlOrMeta", lambda p: p.fill("#f", "new")),         # mid select-all chord
    ("ArrowDown", lambda p: p.select_option("#same", "s20")),  # mid held arrow run
])
def test_typing_cancelled_mid_chord_leaves_no_key_down(monkeypatch, arm_key, act):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0")
    monkeypatch.setattr(H, "rollover_rate", lambda persona: 1.0)
    state = _arm_on(monkeypatch)

    async def go():
        f = Field(value="old")
        p = Page(("#f", f), ("#same", Field(tag="select", options=SAME, rect=(100.0, 300.0, 200.0, 24.0))))
        native_down = p.keyboard.down

        async def down(key):        # arm the blocking sleep right after the key goes down
            await native_down(key)
            if key == arm_key:
                state["armed"] = True
        p.keyboard.down = down
        await H.attach_humanize(None, p, humanize=True, seed="forms-unit")
        p.focused = f
        task = asyncio.ensure_future(act(p))
        for _ in range(2000):
            if state["armed"]:
                break
            await _real_sleep(0)
        assert state["armed"], "the action never reached the key"
        await _real_sleep(0.01)
        state["armed"] = False   # disarm first: the release's own short lag must not block too
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        downs = sorted(set(e[1] for e in kinds(p, "kdown")))   # a held key repeats its keydown
        ups = sorted(e[1] for e in kinds(p, "kup"))
        assert downs and downs == ups, "every key that went down came up: %s vs %s" % (downs, ups)
    run(go())


# ------------------------------------------------------ verifier B: trial, handles, lists, scrolling
def test_a_click_makes_no_playwright_trial_and_no_main_world_handle(fast):
    # The covered-point check was a Playwright TRIAL: it moved the real pointer onto the target and
    # back and fired a blocked press. Now the hit test in the isolated world decides; a CSS target needs
    # no Playwright handle at all (each one's preview reads textContent in the page's world).
    async def go():
        p = await attached(_btn())
        await p.click("#b")
        await p.dblclick("#b")
        await p.hover("#b")
        assert p.native == [] and p.handles == []
        assert [(d[1], d[2]) for d in kinds(p, "down")] == [("left", 1), ("left", 1), ("left", 2)]
    run(go())


def test_a_non_css_target_costs_exactly_one_bounding_box(fast):
    async def go():
        p = await attached(("role=button[name=Target]", Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0))))
        await p.click("role=button[name=Target]")
        assert p.handles == [("bounding_box", "role=button[name=Target]")] and p.native == []
        assert kinds(p, "down")
    run(go())


@pytest.mark.parametrize("sel,scrolled", [("#far", ("css", "#far")), ("role=far", ("window", 0, 1920.0))])
def test_an_offscreen_target_is_scrolled_to_without_a_playwright_scroll(fast, sel, scrolled):
    async def go():
        far = Field(tag="button", type="", rect=(100.0, 2300.0, 160.0, 40.0))
        p = await attached((sel, far))
        await p.click(sel)
        assert p.scrolls == [scrolled], p.scrolls
        assert not [h for h in p.handles if h[0] != "bounding_box"], "no scroll_into_view_if_needed handle"
        assert len(p.handles) == (0 if sel.startswith("#") else 1)
        d = kinds(p, "down")
        assert d and far.inside(*d[0][4]), "pressed on the target where it now is"
    run(go())


@pytest.mark.parametrize("sel", ["#inner", "role=inner"])
def test_a_target_clipped_by_a_scrolling_container_is_scrolled_out_then_clicked(fast, sel):
    async def go():
        inner = Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0))
        inner.clipped = True   # in the viewport, but the container's overflow hides it
        p = await attached((sel, inner))
        await p.click(sel)
        assert p.scrolls and p.native == [] and kinds(p, "down")
    run(go())


def test_a_covered_click_point_goes_native_without_a_trial(fast):
    async def go():
        b = Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0), covered=True)
        p = await attached(("#b", b))
        await p.click("#b")
        assert [(n[0], n[-1].get("trial")) for n in p.native] == [("click", None)]
        assert not kinds(p, "down"), "no trusted press at a point an overlay owns"
    run(go())


def test_a_click_point_the_hit_test_cannot_answer_runs_no_playwright_trial(fast):
    # A point inside a child frame is answered by its <iframe> element (IsolatedWorld.node_at); when
    # the hit test cannot answer at all, the click is Playwright's own -- never a trial first (it
    # moved the pointer onto the target and pressed there).
    async def go():
        b = Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0), in_frame=True)
        p = await attached(("#b", b))
        await p.click("#b")
        assert [(n[0], n[-1].get("trial")) for n in p.native] == [("click", None)]
        assert not kinds(p, "down"), "no humanized press at a point nothing proved"
    run(go())


def test_a_long_untypeable_run_is_held_not_pressed_row_by_row(fast):
    async def go():
        s = Field(tag="select", options=SAME)
        p = await attached(("#same", s))
        assert await p.select_option("#same", "s20") == ["s20"]
        assert s.selected_index == 20 and s.changes == 1
        downs = [e for e in kinds(p, "kdown") if e[1] == "ArrowDown"]
        assert len(downs) == 20, "one keydown and 19 auto-repeats"
        assert [e[1] for e in kinds(p, "kup")] == ["ArrowDown"], "released once"
        assert not [e for e in kinds(p, "press") if e[1] == "ArrowDown"]
    run(go())


def test_a_short_arrow_run_is_still_pressed(fast):
    async def go():
        s = Field(tag="select", options=SAME)
        p = await attached(("#same", s))
        assert await p.select_option("#same", "s5") == ["s5"]
        assert len([e for e in kinds(p, "press") if e[1] == "ArrowDown"]) == 5 and not kinds(p, "kdown")
    run(go())


@pytest.mark.parametrize("value,start", [("m10", 0.0), ("m22", 0.0), ("m03", 250.0)])
def test_a_list_box_row_out_of_view_is_wheeled_in_with_few_notches(fast, value, start):
    async def go():
        lb = Field(tag="select", options=[("m%02d" % i, "Mid %02d" % i) for i in range(25)], size=6,
                   rect=(100.0, 100.0, 200.0, 6 * ROW_H + 4))
        lb.st = start
        p = await attached(("#lbmid", lb))
        assert await p.select_option("#lbmid", value) == [value]
        assert lb.selected == {int(value[1:])} and p.native == []
        assert 1 <= len(kinds(p, "wheel")) <= 6, kinds(p, "wheel")
        assert all(abs(w[2]) == 100 for w in kinds(p, "wheel")), "whole Windows notches"
    run(go())


def test_a_list_box_at_its_end_goes_native(fast):
    async def go():
        lb = Field(tag="select", options=[("m%02d" % i, "Mid %02d" % i) for i in range(25)], size=6,
                   rect=(100.0, 100.0, 200.0, 6 * ROW_H + 4))
        p = await attached(("#lbmid", lb))
        lb.max_st = lambda: 0.0   # a box that cannot scroll: the row never comes into view
        assert await p.select_option("#lbmid", "m20") == ["native"]
        assert not kinds(p, "wheel")
    run(go())


# ----------------------------------------------------- SC 210 fix 2: Playwright's error wording
def test_errors_are_worded_as_the_api_the_caller_used():
    from playwright._impl._errors import Error as PWError, TimeoutError as PWTimeout
    from clearcote._humanize import as_api_error
    e = PWTimeout('Locator.wait_for: Timeout 800ms exceeded.\nCall log:\n  - waiting for locator("#x")')
    n = as_api_error(e, "Page.click")
    assert type(n) is PWTimeout and str(n).split("\n")[0] == "Page.click: Timeout 800ms exceeded."
    assert n.message.endswith('waiting for locator("#x")')
    mine = as_api_error(PWError("Clicking the checkbox did not change its state"), "Locator.check")
    assert str(mine) == "Locator.check: Clicking the checkbox did not change its state"
    same = PWError("Page.fill: Error: not an input")
    assert as_api_error(same, "Page.fill") is same
    other = RuntimeError("Locator.wait_for: not Playwright's")
    assert as_api_error(other, "Page.click") is other


def test_a_humanized_call_raises_the_callers_api_name(fast):
    from playwright._impl._errors import TimeoutError as PWTimeout

    async def go():
        p = await attached(_btn())

        def boom(*a, **k):
            raise PWTimeout("Locator.is_checked: Timeout 500ms exceeded.")
        p.locator = lambda sel: type("L", (), {"first": type("F", (), {"is_checked": staticmethod(boom)})()})()
        with pytest.raises(PWTimeout) as ei:
            await p.check("#b", timeout=500)
        assert str(ei.value) == "Page.check: Timeout 500ms exceeded."
    run(go())


# ------------------------------------------- SC 210 fix 2: paths stay in the viewport; whole-pixel press
def test_glides_near_the_edges_stay_inside_the_viewport(fast):
    """The planner's overshoot arcs left the window near its edges (verifier B: x past the right edge,
    y < 0, then 138-383 ms with no events). This fake's viewport is 1280 x 800."""
    async def go():
        p = await attached(_btn())
        for x, y in [(1276, 400), (640, 3), (4, 790), (1277, 797), (100, 2), (1270, 6), (3, 3)] * 3:
            await p.mouse.move(x, y)
        out = [e for e in kinds(p, "move") if not (0 <= e[1] <= 1280 and 0 <= e[2] <= 800)]
        assert not out, "%d samples outside the viewport, e.g. %s" % (len(out), out[:3])
    run(go())


def test_a_humanized_click_presses_the_whole_pixel_the_hit_test_proved(fast):
    """DOM.getNodeForLocation answers for a whole pixel: a press at 971.6 was proven at 972 and landed
    on a badge next to the target (verifier B). The press goes to the pixel that was checked."""
    async def go():
        p = await attached(_btn())
        for _ in range(6):
            await p.click("#b")
            moves = kinds(p, "move")
            x, y = moves[-1][1], moves[-1][2]
            assert x == int(x) and y == int(y), (x, y)
    run(go())


# ------------------------------------------------------------------------- lane F (SC 232): coverage
# option dicts, label-aware checkbox aim, tap on touch contexts, attach on every context's page event.
@pytest.mark.parametrize("args,kw,want", [
    (({"value": "gre4"},), {}, "gre4"),
    (({"label": "Uruguay"},), {}, "uru6"),
    (({"index": 3},), {}, "gha3"),
    (([{"label": "Ghana"}, {"value": "alb1"}],), {}, "alb1"),   # the first in document order wins
])
def test_option_dicts_are_humanized_not_native(fast, args, kw, want):
    async def go():
        sel = _select()
        p = await attached(sel)
        assert await p.select_option("#s", *args, **kw) == [want]
        s = sel[1]
        assert s.options[s.selected_index]["value"] == want
        assert s.changes == 1 and s.opens == 1, "one trusted change through the opened drop-down"
        assert p.native == [], "no native select_option"
    run(go())


def test_a_value_dict_matches_the_value_only_and_unmatched_dicts_reach_playwright_translated(fast):
    async def go():
        sel = _select()
        p = await attached(sel)
        # {"value": "Germany"}: Playwright matches an option object's value alone -- no option has it
        assert await p.select_option("#s", {"value": "Germany"}) == ["native"]
        assert p.native[-1][2] == ["Germany"], "the dict is translated for Playwright Python"
        # two fields in one dict: not an option Playwright can name either -> native, untouched
        assert await p.select_option("#s", {"value": "gre4", "label": "Greece"}) == ["native"]
        assert sel[1].opens == 0
    run(go())


def test_select_wants_pins_the_node_shapes():
    from clearcote._humanize import select_wants
    assert select_wants("a") == [("value", "a")]
    assert select_wants({"value": "a"}) == [("valueOnly", "a")]
    assert select_wants([{"label": "L"}, {"index": 2}]) == [("label", "L"), ("index", 2)]
    assert select_wants(None, index=[1, 2], label="x") == [("index", 1), ("index", 2), ("label", "x")]
    assert select_wants({"index": "2"}) is None and select_wants({"index": True}) is None
    assert select_wants({"value": "a", "label": "b"}) is None and select_wants({}) is None
    assert select_wants(object()) is None and select_wants(None) is None


def _cb_with_label(hidden):
    cb = Field(type="checkbox", rect=(0.0, 0.0, 0.0, 0.0) if hidden else (100.0, 100.0, 16.0, 16.0),
               visible=not hidden)
    lab = Field(tag="label", type="", rect=(96.0, 96.0, 140.0, 24.0) if hidden else (122.0, 98.0, 120.0, 20.0),
                control=cb)
    return cb, lab


def test_a_hidden_custom_checkbox_is_checked_by_a_trusted_click_on_its_label(fast):
    async def go():
        cb, lab = _cb_with_label(hidden=True)
        p = await attached(("#cb", cb), ("#lab", lab))
        await p.check("#cb")
        assert cb.checked, "the label passed the click on"
        downs = kinds(p, "down")
        assert len(downs) == 1 and lab.inside(*downs[0][4]), "pressed inside the label's box"
        assert not non_trial(p), "no native check"
        await p.uncheck("#cb")
        assert not cb.checked and len(kinds(p, "down")) == 2
    run(go())


def test_a_visible_checkbox_is_clicked_on_its_label_text_about_40_percent_of_the_time(fast):
    async def go():
        on_label = []
        for i in range(60):
            cb, lab = _cb_with_label(hidden=False)
            p = Page(("#cb", cb), ("#lab", lab))
            await H.attach_humanize(None, p, humanize=True, seed="label-share-%d" % (i // 6))
            await p.check("#cb")
            assert cb.checked and not non_trial(p)
            on_label.append(lab.inside(*kinds(p, "down")[0][4]))
        share = sum(on_label) / len(on_label)
        assert 0.2 <= share <= 0.6, share
        assert any(on_label) and not all(on_label)
    run(go())


def test_same_seed_same_label_trait_but_unseeded_per_click_choices():
    # The seed fixes the TRAIT (how often this identity clicks the label); each click's choice is an
    # unseeded draw, or every session of an identity would choose the same way in the same order.
    from clearcote import _humanize
    from clearcote._motion import make_persona
    a, b = make_persona("same-identity"), make_persona("same-identity")
    assert _humanize.label_share(a) == _humanize.label_share(b) == _humanize.LABEL_SHARE
    da = [_humanize.label_draw(a) for _ in range(20)]
    db = [_humanize.label_draw(b) for _ in range(20)]
    assert da != db, "two sessions of one identity made the same 20 choices"


def test_the_label_draw_leaves_the_global_random_stream_alone():
    from clearcote import _humanize
    from clearcote._motion import make_persona
    p = make_persona("x")
    random.seed(11)
    want = [random.random() for _ in range(5)]
    random.seed(11)
    for _ in range(50):
        _humanize.label_draw(p)
    assert [random.random() for _ in range(5)] == want


def test_a_tests_own_rng_and_the_personas_share_decide():
    from clearcote import _humanize
    from clearcote._motion import make_persona
    p = make_persona("x")
    r1, r2 = random.Random(5), random.Random(5)
    assert [_humanize.label_draw(p, r1) for _ in range(20)] == [_humanize.label_draw(p, r2) for _ in range(20)]
    p.label_share = 1.0          # lane B may give personas their own share
    assert all(_humanize.label_draw(p) for _ in range(20))
    p.label_share = 0.0
    assert not any(_humanize.label_draw(p) for _ in range(20))

class _TouchCtx:
    def __init__(self, touch):
        self._impl_obj = type("I", (), {"_options": {"hasTouch": touch}})()


class _TapPage:
    _clearcote_humanized = True

    def __init__(self, strict=False, touch=True):
        self.calls = []
        self.context = _TouchCtx(touch)
        if strict:
            self._clearcote_strict = True

    async def click(self, sel, **kw):
        self.calls.append(("click", sel, kw))


class _TapSelf:
    def __init__(self, page):
        self._impl_obj, self.page = _Impl(None, "#b"), page

    async def count(self):
        return 1


def test_tap_on_a_touch_context_raises_in_strict_mode(Locator):
    from playwright.async_api import Error

    async def go():
        s = _TapSelf(_TapPage(strict=True))
        with pytest.raises(Error, match="tap is not humanized for touch contexts yet"):
            await Locator.tap(s)
        assert s.page.calls == [] and s._impl_obj.calls == [], "nothing was sent"
    run(go())


def test_tap_on_a_touch_context_reports_the_fallback_and_clicks(Locator):
    async def go():
        page = _TapPage()
        s = _TapSelf(page)
        await Locator.tap(s, position={"x": 1, "y": 2})
        assert page.calls == [("click", "#b", {"position": {"x": 1, "y": 2}})]
        ev = page._clearcote_last_fallback
        assert ev["api"] == "Locator.tap" and "not humanized for touch" in ev["reason"] and ev["selector"] == "#b"
        seen = []
        page2 = _TapPage()
        page2._report_fallback = seen.append   # lane A's hook, when present, gets the event instead
        await Locator.tap(_TapSelf(page2))
        assert len(seen) == 1 and not hasattr(page2, "_clearcote_last_fallback")
    run(go())


class _EvCtx:
    """A context that emits "page" like Playwright's: no Browser (a persistent context's)."""

    def __init__(self, pages=()):
        self.pages, self.handlers, self.browser = list(pages), [], None

    def on(self, ev, fn):
        if ev == "page":
            self.handlers.append(fn)

    async def add_init_script(self, *_a, **_k):
        return None

    async def emit(self, page):
        self.pages.append(page)
        for fn in self.handlers:
            r = fn(page)
            if r is not None and hasattr(r, "__await__"):
                await r


def test_a_persistent_context_without_a_browser_attaches(fast):
    async def go():
        first = Page(_btn())
        ctx = _EvCtx([first])
        await H.install_humanize_on_context(ctx, humanize=True, seed="persist")
        assert getattr(first, "_clearcote_humanized", False) is True, "context.browser is None: still attached"
        later = Page(_btn())
        await ctx.emit(later)
        await _real_sleep(0)
        assert getattr(later, "_clearcote_humanized", False) is True, "a page it opens later too"
        await H.install_humanize_on_context(ctx, humanize=True, seed="persist")
        assert len(ctx.handlers) == 1, "hooking a context twice adds no second listener"
    run(go())


def test_a_popup_of_the_implicit_context_of_browser_new_page_is_humanized(fast):
    async def go():
        ctx = _EvCtx()

        class _B:
            async def new_page(self, **kw):
                pg = Page(_btn())
                pg.context = ctx
                ctx.pages.append(pg)
                return pg

            async def new_context(self, **kw):
                return _EvCtx()

        b = _B()
        await H.install_humanize(b, humanize=True, seed="popup")
        page = await b.new_page()
        assert page._clearcote_humanized is True
        mouse = page.mouse.move
        popup = Page(_btn())
        await ctx.emit(popup)             # window.open / target=_blank in that context
        await _real_sleep(0)
        assert getattr(popup, "_clearcote_humanized", False) is True
        assert page.mouse.move is mouse, "the opener was not wrapped a second time"
    run(go())
