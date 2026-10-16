"""Humanized input for elements inside child frames (FrameLocator / Frame actions).

A locator that enters an iframe (``page.frame_locator("#f").locator("#x")``, ``frame.locator(...)``,
``frame.click(...)``) used to go straight to Playwright's own action: an untrusted ``change`` for a
select (verifier B #4), a ~0 ms click, keys with no dwell. The pointer and the keyboard are the PAGE's,
so a frame needs no input channel of its own -- only a way to find, in viewport coordinates, the point
a press inside the frame lands on, and to PROVE that point reaches the element. Then the humanized
``page.mouse`` / ``page.keyboard`` paths drive it like any main-frame target.

How the point is found and proven, with nothing run in any page world:

  1. Playwright waits for the element and scrolls it into view (every nested scroller), and gives its
     box in MAIN-frame viewport coordinates (``bounding_box``).
  2. The browser's hit test at a viewport point (``DOM.getNodeForLocation`` on the page's CDP session)
     answers with the deepest node there AND the frame that owns it: for a same-process child frame,
     the element inside the frame, not the <iframe>.
  3. That frame's own isolated world (``Page.createIsolatedWorld`` with its frameId) reads the node:
     the shared HIT_IN_BOX / DESCRIBE functions, with the box converted to the frame's viewport by the
     origin of each owning <iframe>'s content box, summed up the frame chain (each read in the
     parent frame's isolated world).

A frame whose nodes the page's session does not hold -- a cross-origin frame running out of process --
is answered by its <iframe> element in the main frame: nothing can be proven there, so the action goes
to Playwright's own call and the fallback is reported (lane A's hook when present, else
``page._clearcote_last_fallback``). So does anything this route does not cover: list boxes, modifiers,
``force``/``trial``, a field that is not text, a hidden checkbox (its label is not looked for inside
frames).
"""

import asyncio
import random
import time

from ._forms import classify_field, formatted_equal, plan_select
from ._isolated import (
    DESCRIBE, FORM_CONTROL, HIT_IN_BOX, _call_params, _node_at_params, _parents, _value, async_world_for,
    world_for,
)

# What to return when the frame route does not apply: the caller runs Playwright's own method.
NATIVE = object()

# The origin of an <iframe>'s CONTENT box (where its document's viewport starts), in the viewport of the
# document that holds the <iframe>: border and padding inside the border box.
OWNER_ORIGIN = """function () {
  const r = this.getBoundingClientRect(), cs = getComputedStyle(this);
  return [r.x + this.clientLeft + (parseFloat(cs.paddingLeft) || 0),
          r.y + this.clientTop + (parseFloat(cs.paddingTop) || 0)]; }"""
# Does this element (or something inside it) hold the focus of its document?
FOCUSED = """function () { const a = document.activeElement;
  return !!a && (a === this || this.contains(a) || (this.shadowRoot && this.shadowRoot.activeElement != null)); }"""
# Is the hit node an <iframe>/<frame> (the owner of a frame the session cannot see into)?
IS_FRAME = """function () { const e = this.nodeType === 1 ? this : this.parentElement;
  return !!e && (e.tagName === 'IFRAME' || e.tagName === 'FRAME'); }"""

PROBES = ((0.5, 0.5), (0.82, 0.5), (0.18, 0.5), (0.5, 0.8), (0.5, 0.2))
# Click actions this route takes; anything else in the options goes native.
_CLICK_KEYS = {"button", "click_count", "delay", "position", "timeout", "no_wait_after", "strict"}
_ARROW_HOLD = 40   # an arrow run this long is not walked key by key inside a frame: native


class _NativeFill(Exception):
    """Keys were sent, the result is wrong: Playwright's own call puts it right (reported)."""


class _Native(Exception):
    def __init__(self, why):
        super().__init__(why)
        self.why = why


def _is_timeout(err):
    return type(err).__name__ == "TimeoutError"


def _is_playwright_error(err):
    if _is_timeout(err):
        return True
    try:
        from playwright._impl._errors import Error as PlaywrightImplError
        return isinstance(err, PlaywrightImplError)
    except Exception:  # noqa: BLE001
        return False


def frame_locator_of(loc):
    """Does ``loc`` reach into a child frame (a FrameLocator's locator, or a locator of a child Frame)?"""
    try:
        impl = loc._impl_obj
        if "internal:control=enter-frame" in (impl._selector or ""):
            return True
        return impl._frame.parent_frame is not None
    except Exception:  # noqa: BLE001
        return False


def humanized(page):
    return getattr(page, "_clearcote_humanized", False) is True


def _report(page, api, why, selector=None):
    try:
        from ._humanize import report_fallback
        report_fallback(page, api, why, selector)
    except Exception:  # noqa: BLE001
        pass


def _selector_of(loc):
    try:
        return loc._impl_obj._selector
    except Exception:  # noqa: BLE001
        return None


def _shift(box, o):
    return {"x": box["x"] - o[0], "y": box["y"] - o[1], "width": box["width"], "height": box["height"]}


def _same(rect, box):
    return (abs(rect[0] - box["x"]) <= 1.5 and abs(rect[1] - box["y"]) <= 1.5
            and abs(rect[2] - box["width"]) <= 1.5 and abs(rect[3] - box["height"]) <= 1.5)


def _rand(a, b):
    return a + random.random() * (b - a)


# ======================================================================================== sync
class FrameWorlds:
    """Isolated worlds for every frame of one page, on the page's CDP session (the main-frame
    IsolatedWorld's), one per frameId, made on first use and again after a navigation."""

    def __init__(self, page):
        self.page = page
        self.w = world_for(page)
        self.ctx = {}

    def main(self):
        self.w._ensure()
        return self.w._frame

    def send(self, method, params=None):
        return self.w.send(method, params)

    def _context(self, fid):
        c = self.ctx.get(fid)
        if c is None:
            r = self.send("Page.createIsolatedWorld", {"frameId": fid})
            c = (r or {}).get("executionContextId")
            if c is None:
                return None
            self.ctx[fid] = c
        return c

    def call(self, fid, nid, fn, arg=None):
        """Run ``fn`` (``this`` = the node) in frame ``fid``'s isolated world; its JSON value or None."""
        for _ in range(2):
            c = self._context(fid)
            if c is None:
                return None
            obj = self.send("DOM.resolveNode", {"backendNodeId": nid, "executionContextId": c})
            if not obj or "object" not in obj:
                self.ctx.pop(fid, None)   # a stale world (the frame navigated): once more
                continue
            oid = obj["object"]["objectId"]
            try:
                return _value(self.send("Runtime.callFunctionOn", _call_params(oid, fn, arg)))
            finally:
                self.send("Runtime.releaseObject", {"objectId": oid})
        return None

    def hit(self, x, y):
        """(backendNodeId, frameId) of the deepest node at viewport (x, y), or None."""
        try:
            main = self.main()
        except Exception:  # noqa: BLE001
            return None
        h = self.send("DOM.getNodeForLocation", _node_at_params(x, y, self.w.scroll()))
        if not h or "backendNodeId" not in h:
            return None
        return h["backendNodeId"], h.get("frameId") or main

    def origin(self, fid):
        """Where frame ``fid``'s viewport starts in the main frame's viewport: the content-box origins
        of its <iframe> and of every <iframe> above it, summed. None when the chain cannot be read."""
        main = self.main()
        tree = self.send("Page.getFrameTree")
        parents = _parents((tree or {}).get("frameTree"))
        ox = oy = 0.0
        f = fid
        for _ in range(16):
            if f == main:
                return ox, oy
            up = parents.get(f)
            own = self.send("DOM.getFrameOwner", {"frameId": f})
            if up is None or not own or "backendNodeId" not in own:
                return None
            o = self.call(up, own["backendNodeId"], OWNER_ORIGIN)
            if not isinstance(o, list) or len(o) != 2:
                return None
            ox += float(o[0])
            oy += float(o[1])
            f = up
        return None


def worlds_for(page):
    fw = getattr(page, "_cc_frame_worlds", None)
    if fw is None:
        fw = FrameWorlds(page)
        try:
            page._cc_frame_worlds = fw
        except Exception:  # noqa: BLE001
            pass
    return fw


class Target:
    def __init__(self, fid, nid, box, fbox, info, probe):
        self.fid, self.nid, self.box, self.fbox, self.info, self.probe = fid, nid, box, fbox, info, probe


def _locate(page, loc, timeout, want):
    """Wait for ``loc`` (Playwright's own waits), bring it into view, and prove where a press reaches
    it: a Target, or raise _Native(why). ``want``: None (any element: a click target -- the hit may be
    a child of it), or the CSS of the control to describe (FORM_CONTROL, "select", "input")."""
    loc.wait_for(state="visible", timeout=timeout)
    loc.scroll_into_view_if_needed(timeout=timeout)
    box = loc.bounding_box()
    if not box or box["width"] <= 0 or box["height"] <= 0:
        raise _Native("frame: the element has no box")
    fw = worlds_for(page)
    main = fw.main()
    owners = 0
    for fx, fy in PROBES:
        h = fw.hit(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
        if h is None:
            raise _Native("frame: no CDP hit test here")
        nid, fid = h
        if fid == main:
            if fw.call(main, nid, IS_FRAME):
                owners += 1   # the point is answered by an <iframe>: a frame this session cannot see into
            continue
        o = fw.origin(fid)
        if o is None:
            continue
        fbox = _shift(box, o)
        if want is None:
            if fw.call(fid, nid, HIT_IN_BOX, fbox) is True:
                return Target(fid, nid, box, fbox, None, (fx, fy))
            continue
        info = fw.call(fid, nid, DESCRIBE, {"want": want})
        if info and info.get("rect") and _same(info["rect"], fbox):
            return Target(fid, nid, box, fbox, info, (fx, fy))
    if owners:
        raise _Native("frame: cross-origin frame (its document runs in another process; the SDK holds no "
                      "CDP session for it)")
    raise _Native("frame: no point of the element is proven to receive the press (covered)")


def _point(page, t, position=None):
    """The press point in viewport coordinates: `position` (from the padding box), else the persona's
    point when the hit test proves it, else the probe point that proved the element."""
    box = t.box
    if position:
        bl = (t.info or {}).get("bl", 0)
        bt = (t.info or {}).get("bt", 0)
        return float(round(box["x"] + bl + position["x"])), float(round(box["y"] + bt + position["y"]))
    persona = getattr(page, "_clearcote_persona", None)
    pointer = getattr(page, "_clearcote_pointer", None)
    if persona is not None and pointer is not None:
        from ._motion import click_point
        x, y = click_point(box, pointer(), persona)
        x, y = float(round(x)), float(round(y))
        if _reaches(page, t, x, y):
            return x, y
    fx, fy = t.probe
    return float(round(box["x"] + box["width"] * fx)), float(round(box["y"] + box["height"] * fy))


def _reaches(page, t, x, y):
    fw = worlds_for(page)
    h = fw.hit(x, y)
    if h is None or h[1] != t.fid:
        return False
    return fw.call(t.fid, h[0], HIT_IN_BOX, t.fbox) is True


def _focused(page, t):
    return worlds_for(page).call(t.fid, t.nid, FOCUSED) is True


def _value_of(page, t):
    info = worlds_for(page).call(t.fid, t.nid, DESCRIBE, {"want": FORM_CONTROL})
    return None if not info else info.get("value")


def _click_options(kw):
    extra = {k for k, v in kw.items() if v is not None and v is not False} - _CLICK_KEYS
    if extra:
        raise _Native("frame: %s is not humanized inside frames" % ", ".join(sorted(extra)))
    return {k: kw[k] for k in ("button", "click_count", "delay") if kw.get(k) is not None}


def _click_into(page, t):
    if _focused(page, t):
        return
    x, y = _point(page, t)
    page.mouse.click(x, y)
    time.sleep(_rand(0.04, 0.12))
    if not _focused(page, t):
        raise _Native("frame: the click did not put the caret in the field")


def _text_target(page, loc, timeout):
    t = _locate(page, loc, timeout, FORM_CONTROL)
    i = t.info
    if classify_field(i.get("tag"), i.get("type"), i.get("ce")) != "text" or i.get("disabled") or i.get("readonly"):
        raise _Native("frame: not a text field (Playwright's own call)")
    return t


def _select_all(page):
    sa = getattr(page, "_clearcote_select_all", None)
    if sa is not None:
        sa()
    else:
        page.keyboard.press("ControlOrMeta+a")


# --- actions (sync). Each returns its result, or raises _Native(why) before anything was sent. ------
def do_click(page, loc, kw, no_click=False, count=None):
    if kw.get("force") or kw.get("trial") or kw.get("modifiers"):
        raise _Native("frame: force/trial/modifiers are Playwright's own inside frames")
    opts = {} if no_click else _click_options(kw)
    if count:
        opts["click_count"] = count
    t = _locate(page, loc, kw.get("timeout"), None)
    x, y = _point(page, t, kw.get("position"))
    if no_click:
        page.mouse.move(x, y)
    else:
        page.mouse.click(x, y, **opts)
    return None


def do_fill(page, loc, value, kw):
    if not isinstance(value, str) or kw.get("force"):
        raise _Native("frame: fill(force / non-string) is Playwright's own")
    t = _text_target(page, loc, kw.get("timeout"))
    if t.info.get("value") == value:
        return None
    _click_into(page, t)
    if _value_of(page, t):
        _select_all(page)
        time.sleep(_rand(0.06, 0.16))
        page.keyboard.press("Backspace")
        time.sleep(_rand(0.06, 0.16))
        if _value_of(page, t):
            raise _Native("frame: the field would not clear")
    if value:
        page.keyboard.type(value)
    got = _value_of(page, t)
    if got != value and not formatted_equal(got, value):
        # the keys are in already: Playwright's fill puts the value right (reported by route)
        raise _NativeFill("frame: the typed value did not land; Playwright's fill corrected it")
    return None


def do_type(page, loc, text, kw):
    t = _text_target(page, loc, kw.get("timeout"))
    _click_into(page, t)
    page.keyboard.type(text)
    return None


def do_press(page, loc, key, kw):
    t = _locate(page, loc, kw.get("timeout"), None)
    try:
        info = worlds_for(page).call(t.fid, t.nid, DESCRIBE, {"want": FORM_CONTROL})
    except Exception:  # noqa: BLE001
        info = None
    if info and classify_field(info.get("tag"), info.get("type"), info.get("ce")) == "text" \
            and _same(info["rect"], t.fbox):
        t.info = info
        _click_into(page, t)
    elif not _focused(page, t):
        loc.focus(timeout=kw.get("timeout"))   # a button / link: focused without a click, as Playwright does
        time.sleep(_rand(0.04, 0.12))
    page.keyboard.press(key, **({"delay": kw["delay"]} if kw.get("delay") else {}))
    return None


def do_select(page, loc, value, kw):
    from ._humanize import select_wants, want_index
    if kw.get("element") is not None or kw.get("force"):
        raise _Native("frame: element handles / force are Playwright's own")
    wants = select_wants(value, kw.get("index"), kw.get("label"))
    if not wants:
        raise _Native("frame: nothing to select by value/label/index")
    t = _locate(page, loc, kw.get("timeout"), "select")
    info = t.info
    if info.get("tag") != "select" or info.get("disabled"):
        raise _Native("frame: not an enabled select")
    if info.get("multiple") or (info.get("size") or 0) > 1:
        raise _Native("frame: list boxes inside frames are Playwright's own")
    opts = info.get("options") or []
    idx = []
    for kind, w in wants:
        i = want_index(opts, kind, w)
        if i < 0 or opts[i].get("disabled"):
            raise _Native("frame: option missing or disabled (Playwright waits / reports it)")
        idx.append(i)
    tgt = min(idx)
    if info.get("selectedIndex") == tgt:
        return [opts[tgt]["value"]]
    labels = [None if o.get("disabled") else (o.get("label") or "") for o in opts]
    plan = plan_select(labels, info.get("selectedIndex", -1), tgt)
    if plan is None or abs(plan["arrows"]) > _ARROW_HOLD:
        raise _Native("frame: no short key route to the option")
    x, y = _point(page, t)
    page.mouse.click(x, y)                      # opens the drop-down (the browser's own popup)
    time.sleep(_rand(0.22, 0.48))               # the list opens; the eye finds the option
    for ch in plan["keys"]:
        page.keyboard.press(ch)
        time.sleep(max(0.04, random.gauss(0.10, 0.035)))   # inside type-ahead's 1 s window
    n = plan["arrows"]
    if n:
        time.sleep(_rand(0.12, 0.30))
        for _ in range(abs(n)):
            page.keyboard.press("ArrowDown" if n > 0 else "ArrowUp")
            time.sleep(_rand(0.06, 0.15))
    time.sleep(_rand(0.15, 0.40))
    page.keyboard.press("Enter")               # commits: ONE trusted input + change
    time.sleep(_rand(0.08, 0.16))
    again = worlds_for(page).call(t.fid, t.nid, DESCRIBE, {"want": "select"})
    if again and again.get("selectedIndex") == tgt:
        return [opts[tgt]["value"]]
    raise _NativeFill("frame: the drop-down did not commit the option")


def do_set_checked(page, loc, checked, kw):
    if kw.get("force") or kw.get("trial"):
        raise _Native("frame: force/trial are Playwright's own inside frames")
    if loc.is_checked(timeout=kw.get("timeout")) == checked:
        return None
    t = _locate(page, loc, kw.get("timeout"), "input")
    if t.info.get("type") not in ("checkbox", "radio") or t.info.get("disabled"):
        raise _Native("frame: not an enabled checkbox / radio")
    x, y = _point(page, t, kw.get("position"))
    page.mouse.click(x, y)
    if loc.is_checked() == checked:
        return None
    from playwright.sync_api import Error as PlaywrightError
    raise PlaywrightError("Clicking the checkbox did not change its state")


def route(loc, action, args, kw):
    """Run ``action`` on frame locator ``loc`` through the humanized page input, or return NATIVE
    (after reporting why) for the caller to run Playwright's own method. Errors that Playwright would
    raise -- a timeout, the strict-mode error -- propagate."""
    page = loc.page
    api = "Locator." + action
    try:
        if action in ("click", "dblclick", "hover"):
            return do_click(page, loc, kw, no_click=action == "hover", count=2 if action == "dblclick" else None)
        if action in ("fill", "clear"):
            return do_fill(page, loc, "" if action == "clear" else args[0], kw)
        if action in ("type", "press_sequentially"):
            return do_type(page, loc, args[0], kw)
        if action == "press":
            return do_press(page, loc, args[0], kw)
        if action == "select_option":
            return do_select(page, loc, args[0] if args else None, kw)
        if action in ("check", "uncheck", "set_checked"):
            want = {"check": True, "uncheck": False}.get(action)
            return do_set_checked(page, loc, bool(args[0]) if want is None else want, kw)
    except _Native as e:
        _report(page, api, e.why, _selector_of(loc))
        return NATIVE
    except _NativeFill as e:
        _report(page, api, str(e), _selector_of(loc))
        return NATIVE
    except Exception as e:  # noqa: BLE001
        # Playwright's own errors (a timeout, strict mode, "did not change its state") are the caller's;
        # anything else is this route failing on an object it cannot read: Playwright's method instead.
        if _is_playwright_error(e):
            raise
        _report(page, api, "frame: %s: %s" % (type(e).__name__, e), _selector_of(loc))
        return NATIVE
    return NATIVE


# ======================================================================================== async
class AsyncFrameWorlds:
    """Async FrameWorlds."""

    def __init__(self, page):
        self.page = page
        self.w = async_world_for(page)
        self.ctx = {}

    async def main(self):
        await self.w._ensure()
        return self.w._frame

    async def send(self, method, params=None):
        return await self.w.send(method, params)

    async def _context(self, fid):
        c = self.ctx.get(fid)
        if c is None:
            r = await self.send("Page.createIsolatedWorld", {"frameId": fid})
            c = (r or {}).get("executionContextId")
            if c is None:
                return None
            self.ctx[fid] = c
        return c

    async def call(self, fid, nid, fn, arg=None):
        for _ in range(2):
            c = await self._context(fid)
            if c is None:
                return None
            obj = await self.send("DOM.resolveNode", {"backendNodeId": nid, "executionContextId": c})
            if not obj or "object" not in obj:
                self.ctx.pop(fid, None)
                continue
            oid = obj["object"]["objectId"]
            try:
                return _value(await self.send("Runtime.callFunctionOn", _call_params(oid, fn, arg)))
            finally:
                await self.send("Runtime.releaseObject", {"objectId": oid})
        return None

    async def hit(self, x, y):
        try:
            main = await self.main()
        except Exception:  # noqa: BLE001
            return None
        h = await self.send("DOM.getNodeForLocation", _node_at_params(x, y, await self.w.scroll()))
        if not h or "backendNodeId" not in h:
            return None
        return h["backendNodeId"], h.get("frameId") or main

    async def origin(self, fid):
        main = await self.main()
        tree = await self.send("Page.getFrameTree")
        parents = _parents((tree or {}).get("frameTree"))
        ox = oy = 0.0
        f = fid
        for _ in range(16):
            if f == main:
                return ox, oy
            up = parents.get(f)
            own = await self.send("DOM.getFrameOwner", {"frameId": f})
            if up is None or not own or "backendNodeId" not in own:
                return None
            o = await self.call(up, own["backendNodeId"], OWNER_ORIGIN)
            if not isinstance(o, list) or len(o) != 2:
                return None
            ox += float(o[0])
            oy += float(o[1])
            f = up
        return None


def async_worlds_for(page):
    fw = getattr(page, "_cc_frame_worlds", None)
    if fw is None:
        fw = AsyncFrameWorlds(page)
        try:
            page._cc_frame_worlds = fw
        except Exception:  # noqa: BLE001
            pass
    return fw


async def _alocate(page, loc, timeout, want):
    await loc.wait_for(state="visible", timeout=timeout)
    await loc.scroll_into_view_if_needed(timeout=timeout)
    box = await loc.bounding_box()
    if not box or box["width"] <= 0 or box["height"] <= 0:
        raise _Native("frame: the element has no box")
    fw = async_worlds_for(page)
    main = await fw.main()
    owners = 0
    for fx, fy in PROBES:
        h = await fw.hit(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
        if h is None:
            raise _Native("frame: no CDP hit test here")
        nid, fid = h
        if fid == main:
            if await fw.call(main, nid, IS_FRAME):
                owners += 1
            continue
        o = await fw.origin(fid)
        if o is None:
            continue
        fbox = _shift(box, o)
        if want is None:
            if await fw.call(fid, nid, HIT_IN_BOX, fbox) is True:
                return Target(fid, nid, box, fbox, None, (fx, fy))
            continue
        info = await fw.call(fid, nid, DESCRIBE, {"want": want})
        if info and info.get("rect") and _same(info["rect"], fbox):
            return Target(fid, nid, box, fbox, info, (fx, fy))
    if owners:
        raise _Native("frame: cross-origin frame (its document runs in another process; the SDK holds no "
                      "CDP session for it)")
    raise _Native("frame: no point of the element is proven to receive the press (covered)")


async def _areaches(page, t, x, y):
    fw = async_worlds_for(page)
    h = await fw.hit(x, y)
    if h is None or h[1] != t.fid:
        return False
    return await fw.call(t.fid, h[0], HIT_IN_BOX, t.fbox) is True


async def _apoint(page, t, position=None):
    box = t.box
    if position:
        bl = (t.info or {}).get("bl", 0)
        bt = (t.info or {}).get("bt", 0)
        return float(round(box["x"] + bl + position["x"])), float(round(box["y"] + bt + position["y"]))
    persona = getattr(page, "_clearcote_persona", None)
    pointer = getattr(page, "_clearcote_pointer", None)
    if persona is not None and pointer is not None:
        from ._motion import click_point
        x, y = click_point(box, pointer(), persona)
        x, y = float(round(x)), float(round(y))
        if await _areaches(page, t, x, y):
            return x, y
    fx, fy = t.probe
    return float(round(box["x"] + box["width"] * fx)), float(round(box["y"] + box["height"] * fy))


async def _afocused(page, t):
    return await async_worlds_for(page).call(t.fid, t.nid, FOCUSED) is True


async def _avalue_of(page, t):
    info = await async_worlds_for(page).call(t.fid, t.nid, DESCRIBE, {"want": FORM_CONTROL})
    return None if not info else info.get("value")


async def _aclick_into(page, t):
    if await _afocused(page, t):
        return
    x, y = await _apoint(page, t)
    await page.mouse.click(x, y)
    await asyncio.sleep(_rand(0.04, 0.12))
    if not await _afocused(page, t):
        raise _Native("frame: the click did not put the caret in the field")


async def _atext_target(page, loc, timeout):
    t = await _alocate(page, loc, timeout, FORM_CONTROL)
    i = t.info
    if classify_field(i.get("tag"), i.get("type"), i.get("ce")) != "text" or i.get("disabled") or i.get("readonly"):
        raise _Native("frame: not a text field (Playwright's own call)")
    return t


async def _aselect_all(page):
    sa = getattr(page, "_clearcote_select_all", None)
    if sa is not None:
        await sa()
    else:
        await page.keyboard.press("ControlOrMeta+a")


async def ado_click(page, loc, kw, no_click=False, count=None):
    if kw.get("force") or kw.get("trial") or kw.get("modifiers"):
        raise _Native("frame: force/trial/modifiers are Playwright's own inside frames")
    opts = {} if no_click else _click_options(kw)
    if count:
        opts["click_count"] = count
    t = await _alocate(page, loc, kw.get("timeout"), None)
    x, y = await _apoint(page, t, kw.get("position"))
    if no_click:
        await page.mouse.move(x, y)
    else:
        await page.mouse.click(x, y, **opts)
    return None


async def ado_fill(page, loc, value, kw):
    if not isinstance(value, str) or kw.get("force"):
        raise _Native("frame: fill(force / non-string) is Playwright's own")
    t = await _atext_target(page, loc, kw.get("timeout"))
    if t.info.get("value") == value:
        return None
    await _aclick_into(page, t)
    if await _avalue_of(page, t):
        await _aselect_all(page)
        await asyncio.sleep(_rand(0.06, 0.16))
        await page.keyboard.press("Backspace")
        await asyncio.sleep(_rand(0.06, 0.16))
        if await _avalue_of(page, t):
            raise _Native("frame: the field would not clear")
    if value:
        await page.keyboard.type(value)
    got = await _avalue_of(page, t)
    if got != value and not formatted_equal(got, value):
        raise _NativeFill("frame: the typed value did not land; Playwright's fill corrected it")
    return None


async def ado_type(page, loc, text, kw):
    t = await _atext_target(page, loc, kw.get("timeout"))
    await _aclick_into(page, t)
    await page.keyboard.type(text)
    return None


async def ado_press(page, loc, key, kw):
    t = await _alocate(page, loc, kw.get("timeout"), None)
    info = await async_worlds_for(page).call(t.fid, t.nid, DESCRIBE, {"want": FORM_CONTROL})
    if info and classify_field(info.get("tag"), info.get("type"), info.get("ce")) == "text" \
            and _same(info["rect"], t.fbox):
        t.info = info
        await _aclick_into(page, t)
    elif not await _afocused(page, t):
        await loc.focus(timeout=kw.get("timeout"))
        await asyncio.sleep(_rand(0.04, 0.12))
    await page.keyboard.press(key, **({"delay": kw["delay"]} if kw.get("delay") else {}))
    return None


async def ado_select(page, loc, value, kw):
    from ._humanize import select_wants, want_index
    if kw.get("element") is not None or kw.get("force"):
        raise _Native("frame: element handles / force are Playwright's own")
    wants = select_wants(value, kw.get("index"), kw.get("label"))
    if not wants:
        raise _Native("frame: nothing to select by value/label/index")
    t = await _alocate(page, loc, kw.get("timeout"), "select")
    info = t.info
    if info.get("tag") != "select" or info.get("disabled"):
        raise _Native("frame: not an enabled select")
    if info.get("multiple") or (info.get("size") or 0) > 1:
        raise _Native("frame: list boxes inside frames are Playwright's own")
    opts = info.get("options") or []
    idx = []
    for kind, w in wants:
        i = want_index(opts, kind, w)
        if i < 0 or opts[i].get("disabled"):
            raise _Native("frame: option missing or disabled (Playwright waits / reports it)")
        idx.append(i)
    tgt = min(idx)
    if info.get("selectedIndex") == tgt:
        return [opts[tgt]["value"]]
    labels = [None if o.get("disabled") else (o.get("label") or "") for o in opts]
    plan = plan_select(labels, info.get("selectedIndex", -1), tgt)
    if plan is None or abs(plan["arrows"]) > _ARROW_HOLD:
        raise _Native("frame: no short key route to the option")
    x, y = await _apoint(page, t)
    await page.mouse.click(x, y)
    await asyncio.sleep(_rand(0.22, 0.48))
    for ch in plan["keys"]:
        await page.keyboard.press(ch)
        await asyncio.sleep(max(0.04, random.gauss(0.10, 0.035)))
    n = plan["arrows"]
    if n:
        await asyncio.sleep(_rand(0.12, 0.30))
        for _ in range(abs(n)):
            await page.keyboard.press("ArrowDown" if n > 0 else "ArrowUp")
            await asyncio.sleep(_rand(0.06, 0.15))
    await asyncio.sleep(_rand(0.15, 0.40))
    await page.keyboard.press("Enter")
    await asyncio.sleep(_rand(0.08, 0.16))
    again = await async_worlds_for(page).call(t.fid, t.nid, DESCRIBE, {"want": "select"})
    if again and again.get("selectedIndex") == tgt:
        return [opts[tgt]["value"]]
    raise _NativeFill("frame: the drop-down did not commit the option")


async def ado_set_checked(page, loc, checked, kw):
    if kw.get("force") or kw.get("trial"):
        raise _Native("frame: force/trial are Playwright's own inside frames")
    if await loc.is_checked(timeout=kw.get("timeout")) == checked:
        return None
    t = await _alocate(page, loc, kw.get("timeout"), "input")
    if t.info.get("type") not in ("checkbox", "radio") or t.info.get("disabled"):
        raise _Native("frame: not an enabled checkbox / radio")
    x, y = await _apoint(page, t, kw.get("position"))
    await page.mouse.click(x, y)
    if await loc.is_checked() == checked:
        return None
    from playwright.async_api import Error as PlaywrightError
    raise PlaywrightError("Clicking the checkbox did not change its state")


async def aroute(loc, action, args, kw):
    """Async ``route``."""
    page = loc.page
    api = "Locator." + action
    try:
        if action in ("click", "dblclick", "hover"):
            return await ado_click(page, loc, kw, no_click=action == "hover",
                                   count=2 if action == "dblclick" else None)
        if action in ("fill", "clear"):
            return await ado_fill(page, loc, "" if action == "clear" else args[0], kw)
        if action in ("type", "press_sequentially"):
            return await ado_type(page, loc, args[0], kw)
        if action == "press":
            return await ado_press(page, loc, args[0], kw)
        if action == "select_option":
            return await ado_select(page, loc, args[0] if args else None, kw)
        if action in ("check", "uncheck", "set_checked"):
            want = {"check": True, "uncheck": False}.get(action)
            return await ado_set_checked(page, loc, bool(args[0]) if want is None else want, kw)
    except _Native as e:
        _report(page, api, e.why, _selector_of(loc))
        return NATIVE
    except _NativeFill as e:
        _report(page, api, str(e), _selector_of(loc))
        return NATIVE
    except Exception as e:  # noqa: BLE001
        # Playwright's own errors (a timeout, strict mode, "did not change its state") are the caller's;
        # anything else is this route failing on an object it cannot read: Playwright's method instead.
        if _is_playwright_error(e):
            raise
        _report(page, api, "frame: %s: %s" % (type(e).__name__, e), _selector_of(loc))
        return NATIVE
    return NATIVE


# ================================================================================== Frame class
_FRAME_METHODS = ("click", "dblclick", "hover", "fill", "type", "press", "select_option", "check", "uncheck",
                  "set_checked")
_frame_patched = set()


def patch_frame_class(is_async=False):
    """Route a CHILD frame's selector actions (``page.frames[1].click("#x")``, ``frame.fill(...)``)
    through ``frame.locator(selector).first`` -- the patched Locator, which takes the frame route on a
    humanized page. The main frame and pages without humanize keep Playwright's own methods (a main
    frame's page.* methods are the humanized ones already). Patched once per API."""
    key = "async" if is_async else "sync"
    if key in _frame_patched:
        return
    _frame_patched.add(key)
    try:
        if is_async:
            from playwright.async_api._generated import Frame
        else:
            from playwright.sync_api._generated import Frame
    except Exception:  # noqa: BLE001
        return

    def wrap(name, orig):
        if is_async:
            async def m(self, selector, *a, **kw):
                if humanized(self.page) and self.parent_frame is not None:
                    kw.pop("strict", None)   # Frame actions are not strict: .first, as Playwright's
                    return await getattr(self.locator(selector).first, name)(*a, **kw)
                return await orig(self, selector, *a, **kw)
        else:
            def m(self, selector, *a, **kw):
                if humanized(self.page) and self.parent_frame is not None:
                    kw.pop("strict", None)
                    return getattr(self.locator(selector).first, name)(*a, **kw)
                return orig(self, selector, *a, **kw)
        m.__name__ = name
        m.__doc__ = getattr(orig, "__doc__", None)
        return m

    for name in _FRAME_METHODS:
        orig = getattr(Frame, name, None)
        if orig is not None:
            setattr(Frame, name, wrap(name, orig))
