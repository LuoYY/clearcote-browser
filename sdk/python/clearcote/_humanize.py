"""Humanized input + cursor visualization (Python).

``humanize=True`` installs ONE consistent human-input standard that covers moving, clicking,
dragging, scrolling and typing — dispatched as native trusted input (isTrusted=True,
navigator.webdriver stays False). It works whether you drive the page directly or via locators:

  * page level   — page.click / page.hover / page.dblclick / page.type / page.fill / page.press,
                   page.mouse.move / mouse.click / mouse.wheel and held-button drags
                   (mouse.down → mouse.move → mouse.up), page.keyboard.type
  * locator level — locator.click / type / fill / hover / dblclick / press /
                    press_sequentially / clear / check / uncheck / tap / drag_to
                    (routed through the humanized page methods; main-frame locators only,
                    with a safe fall-through to native for anything exotic)

``show_cursor=True`` injects a red cursor dot that follows the real mousemove events.

Design notes:
  * Mouse moves build an eased, slightly bowed cubic-bezier path SDK-side from the *tracked*
    cursor position (continuous — no snap back to the 0,0 corner between moves) using
    Playwright's NATIVE mouse.move. Because it is native input, the button state is carried
    correctly: down() → move() → up() is a real held-button drag (sliders work) with no
    separate CDP channel to desync.
  * Typing goes key-by-key through Playwright's native keyboard (trusted, shift handled by the
    engine) with randomized inter-key timing, occasional thinking pauses, and a small
    fat-finger-then-correct chance.
"""

import random
import sys
import time

import os

from ._forms import (
    DEFAULT_TYPO_RATE, NEARBY_LETTERS, classify_field, datetime_equal, engine_path_ok, formatted_equal,
    match_option, normalize_click_options, numeric_equal, plan_datetime, plan_select, plan_typo, range_attrs,
    range_fraction, range_nudge, range_point, range_sanitize, range_value_ok, segment_nodes_from_tree,
    typo_eligible, typos_allowed,
)
from ._isolated import (
    ACTIVE_FIELD as ISO_ACTIVE_FIELD, ACTIVE_VALUE as ISO_ACTIVE_VALUE, COVER_CHECK_MS, COVER_RETRY_MS,
    CSS_BOX as ISO_CSS_BOX, DESCRIBE as ISO_DESCRIBE, FORM_CONTROL, HIT_IN_BOX as ISO_HIT_IN_BOX,
    LISTBOX_ROW as ISO_LISTBOX_ROW, SCROLL_BY as ISO_SCROLL_BY,
    PLATFORM as ISO_PLATFORM, VIEWPORT as ISO_VIEWPORT, world_for,
)
from ._motion import (
    WHEEL_NOTCH_PX, make_persona, plan_move, drag_dwell, click_hold, key_dwell, click_point, plan_ambient,
    platform_from_navigator, rollover_rate, wheel_notches,
)

# IIFE so it runs both as an add_init_script source AND when passed to page.evaluate().
CURSOR_OVERLAY = """(() => {
  if (window.__clearcoteCursor) return; window.__clearcoteCursor = 1;
  const make = () => {
    if (document.getElementById('__clearcote_cursor')) return;
    const d = document.createElement('div'); d.id = '__clearcote_cursor';
    d.style.cssText = 'position:fixed;left:0;top:0;width:20px;height:20px;margin:-10px 0 0 -10px;' +
      'border-radius:50%;border:2px solid #ff3b3b;background:rgba(255,59,59,.22);' +
      'box-shadow:0 0 10px rgba(255,59,59,.6);pointer-events:none;z-index:2147483647';
    (document.body || document.documentElement).appendChild(d);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', make); else make();
  document.addEventListener('mousemove', (e) => {
    const d = document.getElementById('__clearcote_cursor');
    if (d) { d.style.left = e.clientX + 'px'; d.style.top = e.clientY + 'px'; }
  }, true);
})();"""

# Kept for callers of the old name; the map itself lives in _forms (shared with async/Node/.NET).
_NEARBY = NEARBY_LETTERS


def _rand(a, b):
    return a + random.random() * (b - a)


def typo_rate():
    """Per-character fat-finger rate. CLEARCOTE_HUMANIZE_TYPO_RATE overrides it (0 disables typos;
    the test suites raise it to exercise the correction paths)."""
    v = os.environ.get("CLEARCOTE_HUMANIZE_TYPO_RATE")
    if v is None or v == "":
        return DEFAULT_TYPO_RATE
    try:
        f = float(v)
    except ValueError:
        return DEFAULT_TYPO_RATE
    if f != f:   # NaN: not a rate (Node/.NET read it as the default too)
        return DEFAULT_TYPO_RATE
    return min(1.0, max(0.0, f))


# The characters a US-layout keyboard (Playwright's, and the engine's keyboard.getLayoutMap())
# types with Shift held: capitals and the shifted symbols.
_SHIFTED_SYMBOLS = frozenset('~!@#$%^&*()_+{}|:"<>?')


def _needs_shift(ch):
    return "A" <= ch <= "Z" or ch in _SHIFTED_SYMBOLS


# The engine's trajectory command, new name first. r34 renamed it and dropped both names from the
# served /json/protocol: "humanizedClick" was listed there, and it is a known tool's vocabulary. The
# old name still works on r34 and is the only one r33 and older know.
ENGINE_POINTER_PATH = ("Browser.dispatchPointerPath", "Browser.humanizedClick")


def _cdp_method_missing(err):
    """True for CDP's "method not found" (-32601), which Playwright reports as "'X' wasn't found"."""
    m = str(err)
    return "wasn't found" in m or "-32601" in m or "method not found" in m.lower()


# Keys a person rolls over: unshifted, one physical key each. Shifted characters, Enter/Backspace and
# the typo path keep the plain one-press-at-a-time path.
_ROLL_KEYS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789 ,.;'/-=")


def _host_platform():
    """The persona platform when the page cannot say: the SDK's default persona follows the host."""
    if sys.platform.startswith("win"):
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def _shift_lead_s():
    """ShiftLeft keydown -> the shifted key's keydown (a person presses Shift first)."""
    return _rand(35, 120) / 1000.0


def _shift_lag_s():
    """The shifted key's keyup -> ShiftLeft keyup (Shift comes up just after the letter)."""
    return _rand(10, 70) / 1000.0


def _nearby_key(ch):
    lo = ch.lower()
    if lo in _NEARBY:
        w = random.choice(_NEARBY[lo])
        return w.upper() if ch.isupper() else w
    return ch


def _sel_kw(options, *names):
    """The subset of Playwright options a native fallback call understands."""
    return {k: options[k] for k in names if k in options}


def attach_humanize(browser, page, humanize=False, show_cursor=False, seed=None):
    """Wrap one page's input methods + (optionally) inject the cursor overlay.

    ``seed`` (the fingerprint seed) selects a stable per-identity motor persona; unset ⇒ random."""
    if show_cursor:
        def inject():
            try:
                page.evaluate(CURSOR_OVERLAY)
            except Exception:  # noqa: BLE001
                pass
        inject()
        page.on("load", lambda _=None: inject())
        page.on("framenavigated", lambda f: inject() if f == page.main_frame else None)
    if not humanize:
        return

    # Per-identity motor persona (cadence, tremor, overshoot, handedness) seeded from the fingerprint
    # so behavior is consistent within a session and unlinkable across seeds. Stored on the page so
    # the module-level Locator.drag_to patch can reach it too.
    persona = make_persona(seed)
    page._clearcote_persona = persona

    # Tracked cursor position so each move continues from where the last one ended.
    # Seed at a plausible on-page spot (not 0,0) so the first glide doesn't start in the corner.
    # "placed" records whether the REAL pointer has been moved on this page yet: until it has, the
    # browser's cursor is still at the (0,0) origin no matter what pos claims. That distinction is
    # what a wheel event (dispatched at the current pointer), a bare mouse.down() and a
    # keyboard-only session all depend on — see _wheel_anchor / hdown / _pointer_presence.
    st = {"pos": (_rand(140, 380), _rand(90, 240)), "held": False,
          "placed": False, "kb_primed": False}

    mouse = page.mouse
    native_move, native_click, native_wheel = mouse.move, mouse.click, mouse.wheel
    # getattr, not attribute access: attach must not raise on a page-like object that lacks a
    # method we only optionally wrap. Reading these directly widened the attach-time contract and
    # took four previously-green tests down with an AttributeError before anything was wrapped —
    # humanize failing closed on attach is the one outcome this module must never produce, because
    # it silently disables every other humanization along with it.
    native_down = getattr(mouse, "down", None)
    native_up = getattr(mouse, "up", None)

    def _dispatch(steps):
        """Walk a planned trajectory (list of (x, y, sleep_ms)) via native trusted mouse.move +
        off-protocol time.sleep. Native input carries button state, so a button held via
        mouse.down() stays pressed across the whole path (drag)."""
        for sx, sy, sl in steps:
            try:
                native_move(sx, sy)
            except Exception:  # noqa: BLE001
                break
            st["placed"] = True
            time.sleep(sl / 1000.0)

    def _glide(x, y, settle=False, target_w=24):
        """Move to (x, y) as a minimum-jerk sum-of-submovements path (primary ballistic + corrective
        homing) with colored sub-pixel noise and Fitts-scaled duration (see _motion.plan_move).
        Continuous (starts at the tracked position, lands exactly on target)."""
        _dispatch(plan_move(st["pos"], (x, y), persona, target_w=target_w, settle=settle))
        st["pos"] = (x, y)

    # --- Engine real-trajectory routing (PRO) --------------------------------
    # Route point-to-point moves/clicks through the browser engine's
    # Browser.dispatchPointerPath (humanizedClick before r34): a trusted WebMouseEvent path built in the browser
    # process from REAL recorded human trajectories (PRO) or the engine bezier
    # (free) -- tiered automatically by the engine's runtime license gate, so the
    # SDK stays tier-agnostic. One CDP call per action (far less Input-domain
    # traffic than per-step native moves); continuity is tracked engine-side.
    # Held-button DRAGS stay on the native path (the engine click can't hold a
    # button across the path -- sliders depend on it); wheel/typing unchanged.
    # Falls back to the SDK bezier (plan_move) permanently if the method is
    # unavailable (older engine, or a persistent context with no Browser handle).
    _eng = {"session": None, "tid": None, "ok": True, "method": None}

    def _engine_glide(x, y, no_click):
        if not _eng["ok"]:
            return False
        try:
            if _eng["session"] is None:
                tsession = page.context.new_cdp_session(page)
                _eng["tid"] = tsession.send(
                    "Target.getTargetInfo")["targetInfo"]["targetId"]
                b = getattr(page.context, "browser", None)
                _eng["session"] = (b.new_browser_cdp_session()
                                   if b is not None else tsession)
            dx, dy = x - st["pos"][0], y - st["pos"][1]
            dist = (dx * dx + dy * dy) ** 0.5
            dur = min(1.10, max(0.28, 0.30 + dist / 1700.0)) * (0.85 + 0.30 * random.random())
            params = {"targetId": _eng["tid"], "x": float(x), "y": float(y),
                      "duration": dur, "noClick": bool(no_click)}
            if _eng.get("method"):
                _eng["session"].send(_eng["method"], params)
            else:
                # New name first; an engine older than r34 answers "wasn't found" (-32601)
                # and gets the old one. Any other error is a real failure (below).
                try:
                    _eng["session"].send(ENGINE_POINTER_PATH[0], params)
                    _eng["method"] = ENGINE_POINTER_PATH[0]
                except Exception as e:  # noqa: BLE001
                    if not _cdp_method_missing(e):
                        raise
                    _eng["session"].send(ENGINE_POINTER_PATH[1], params)
                    _eng["method"] = ENGINE_POINTER_PATH[1]
            # The engine runs the trajectory in the BROWSER process and answers this command
            # before it has finished walking it. Without waiting, a following mouse.down() fires
            # at the cursor's stale position -- measured: a move to (110,58) then a press landed
            # at (981,629), on <body> instead of the slider, so every move-then-press sequence
            # (drag, press-and-hold, drag-and-drop) acted on the wrong element.
            time.sleep(dur)
            # Keep Playwright's own cursor position in sync so a following
            # mouse.down()/drag presses where the engine left the cursor.
            try:
                native_move(x, y)
            except Exception:  # noqa: BLE001
                pass
            st["pos"] = (x, y)
            st["placed"] = True
            return True
        except Exception:  # noqa: BLE001 - method missing / no browser session: stop trying
            _eng["ok"] = False
            return False

    def hmove(x, y, **kw):
        # A HELD BUTTON MUST STAY ON THE NATIVE PATH.
        #
        # The engine trajectory (Browser.humanizedClick) builds its own WebMouseEvents and cannot
        # hold a button across them, so a drag leg routed through it emits moves reporting
        # MouseEvent.buttons 0 in the middle of a press. That is both a contradiction no real
        # mouse produces (buttons is derived from physical state) and a functional break: a range
        # thumb ignores a move that says nothing is down. Measured on this file without the guard,
        # dragging a 420px slider: the press landed on the INPUT correctly and 349 of 354 moves
        # still reported buttons 0, so the thumb fired one input event and the value went 0 -> 1.
        #
        # A BARE MOVE IS ALSO NATIVE, for a second and independent reason: the engine walks its
        # path ASYNCHRONOUSLY in the browser process and answers before it has finished, so it
        # keeps moving the cursor AFTER the pre-press pin in hdown. Measured here on this file,
        # move to (56,120) then mouse.down(): the button was carried correctly (407 of 560 moves
        # reported buttons != 0) but the press landed on HTML at (856,213) instead of the INPUT,
        # so the thumb never received it and the value stayed 0. Re-pinning immediately before the
        # press does not help — the overrunning trajectory outlives the pin.
        #
        # _glide is still fully humanized (minimum-jerk sum-of-submovements, colored noise,
        # Fitts-scaled duration); only the engine's recorded-trajectory SOURCING is given up for
        # bare moves. hclick keeps it, because it performs the whole move+click engine-side where
        # nothing races the trajectory.
        _glide(x, y)

    def hdown(**kw):
        # A press needs a POSITION behind it. mouse.down() with nothing having moved the pointer
        # first presses at the (0,0) origin, so a bare down -> move -> up drag grabs the corner of
        # <body> instead of the thing it meant to drag, and the slide silently does nothing. Give
        # the press a real origin: a short ambient placement, then travel to the point we are about
        # to press, so the button goes down at the END of a movement the way a hand does it.
        # DELIBERATELY NO PLACEMENT WHEN THE POINTER HAS NEVER MOVED.
        #
        # An earlier revision ran an ambient walk here and pressed wherever it ended. But before
        # any real move, st["pos"] is the RANDOM SEED assigned at attach (_rand(140,380),
        # _rand(90,240)) -- not a coordinate the caller ever named -- so that turned a harmless
        # no-op press into a TRUSTED click at an arbitrary page position, which can land on a
        # link and navigate. A bare down() with no preceding move is a caller mistake; inventing
        # a target makes it a worse one. Callers wanting a humanized drag move first, and that
        # move glides and sets "placed".
        if st["placed"]:
            # Pin the cursor to the tracked target before pressing. The engine answers
            # humanizedClick before its trajectory has finished AND overruns the duration it
            # reports, so waiting that duration is not sufficient on its own -- measured on Node,
            # a move to (110,58) was still mid-flight at (416,504) when the press fired, landing
            # on <body>. Only meaningful once a real move has happened: that is what makes
            # st["pos"] correspond to where the cursor actually is.
            try:
                native_move(*st["pos"])
            except Exception:  # noqa: BLE001 - best effort: keep the press on target
                pass
        # try/finally semantics so a raising press cannot strand held=True and turn every later
        # move into a phantom drag leg. hup already guarded this; hdown did not.
        try:
            st["held"] = True
            native_down(**kw)
        except Exception:
            st["held"] = False
            raise

    def hup(**kw):
        try:
            native_up(**kw)
        finally:
            st["held"] = False

    def _platform():
        """The platform the page believes (the persona's), read once per page."""
        if "platform" not in st:
            plat = None
            try:
                plat = platform_from_navigator(world_for(page).evaluate(ISO_PLATFORM))
            except Exception:  # noqa: BLE001
                pass
            st["platform"] = plat or _host_platform()
        return st["platform"]

    def _press_at(x, y, o):
        """Press at (x, y) -- where the last glide ended -- the way the options ask: the button,
        the click count (a double click is two presses with a human gap, not one event), the
        caller's hold (`delay`) or the persona's, and modifiers held around the whole press with a
        lead before it and a lag after, as fingers do. Native down/up, so nothing moves."""
        mods = o["modifiers"]
        held = []
        try:
            for m in mods:
                native_kb_down(m)
                held.append(m)
                time.sleep(_rand(40, 110) / 1000.0)
            time.sleep(_rand(40, 130) / 1000.0)    # brief dwell before pressing, like a human
            n = o["click_count"]
            for i in range(1, n + 1):
                hold = o["delay"] if o["delay"] is not None else click_hold(persona)
                native_down(button=o["button"], click_count=i)
                try:
                    time.sleep(hold / 1000.0)
                finally:
                    native_up(button=o["button"], click_count=i)   # never leave the button down
                if i < n:
                    time.sleep(_rand(70, 170) / 1000.0)   # the gap inside a double click
        finally:
            for m in reversed(held):
                time.sleep(_rand(20, 80) / 1000.0)
                try:
                    native_kb_up(m)
                except Exception:  # noqa: BLE001
                    pass

    def hclick(x, y, **kw):
        # The engine path performs ONE plain left click. Anything else -- right/middle button, a
        # double click, a caller-chosen hold -- glides SDK-side and presses as asked; the old
        # wrapper dropped those options and always left-clicked once.
        o = normalize_click_options(kw, _platform())
        if engine_path_ok(o) and _engine_glide(x, y, no_click=False):
            return
        _glide(x, y)
        try:
            if native_down is None or native_up is None or (o["modifiers"] and native_kb_down is None):
                time.sleep(_rand(40, 130) / 1000.0)
                native_click(x, y, delay=o["delay"] if o["delay"] is not None else click_hold(persona),
                             **_sel_kw(kw, "button", "click_count"))
                return
            _press_at(x, y, o)
        except Exception:  # noqa: BLE001
            pass

    def hdblclick_xy(x, y, **kw):
        kw = dict(kw)
        kw["click_count"] = 2
        hclick(x, y, **kw)

    def _held_glide(x, y):
        _glide(x, y, settle=True)                # held-button drag leg + seating jiggle
    page._clearcote_held_glide = _held_glide

    def hambient(ms=1200):
        """Opt-in ambient / pre-challenge cursor activity (idle drift + non-goal moves) so a
        behavioral collector sees pointer entropy BEFORE the first goal action. page.ambient_motion(ms)."""
        try:
            vp = page.viewport_size or {"width": 1280, "height": 800}
            steps = plan_ambient(st["pos"], {"width": vp["width"], "height": vp["height"]}, persona, ms)
            _dispatch(steps)
            if steps:
                st["pos"] = (steps[-1][0], steps[-1][1])
        except Exception:  # noqa: BLE001
            pass

    page.ambient_motion = hambient

    # OPT-IN: ambient pointer entropy on page load is OFF by default. A burst that fires
    # on every load (including iframe loads mid-challenge) interleaves with whatever the
    # caller is driving and corrupts precise input like a slider drag; it is also motion
    # no real user necessarily produces at that instant. Callers that want pre-challenge
    # entropy either call page.ambient_motion(ms) explicitly at the moment they want it,
    # or re-enable the per-load burst with page._clearcote_auto_ambient = True.
    page._clearcote_auto_ambient = False

    def _auto_ambient(_=None):
        if getattr(page, "_clearcote_auto_ambient", False):
            try:
                hambient(_rand(450, 950))
            except Exception:  # noqa: BLE001
                pass

    page.on("load", lambda _=None: _auto_ambient())

    def _viewport():
        """The REAL viewport, in CSS px.

        Not page.viewport_size: launch() and launch_persistent_context() both force
        viewport=None on every headed page (so innerWidth tracks the real OS window instead of
        Playwright's emulated 1280x720 -- an emulated viewport on a headed window is itself a
        tell). With viewport=None, viewport_size() returns None for the life of the page, so the
        old `page.viewport_size or {1280, 800}` fell through to that constant on EVERY headed
        launch. The anchor gate below then judged "is the pointer inside the viewport" against a
        box unrelated to the window: on a maximized display a cursor legitimately at x=1400 read
        as out-of-bounds and got re-homed on every single scroll, which is exactly the behaviour
        the gate exists to prevent. innerWidth/innerHeight are the truth in both modes.
        """
        try:
            wh = world_for(page).evaluate(ISO_VIEWPORT)
            if wh and wh[0] and wh[1]:
                return float(wh[0]), float(wh[1])
        except Exception:  # noqa: BLE001
            pass
        vp = page.viewport_size or {"width": 1280, "height": 800}
        return float(vp["width"]), float(vp["height"])

    def _wheel_anchor():
        """Put the cursor over the content before scrolling it.

        A wheel event is delivered at wherever the pointer currently is, and until something has
        moved it that is the (0,0) origin — so the first scroll on a freshly loaded page arrives
        in the top-left corner, over whatever element happens to live there, which is not where a
        hand ever scrolls. Only re-home when the pointer is nowhere sensible: a person does not
        move the mouse between two scrolls of the same page, so re-anchoring on every wheel call
        would be its own tell.
        """
        try:
            vw, vh = _viewport()
            x, y = st["pos"]
            # With a button down every move is a DRAG leg, so anchoring mid-drag would haul the
            # grabbed element across the page instead of scrolling under it.
            if st["held"] or (st["placed"] and 8 <= x <= vw - 8 and 8 <= y <= vh - 8):
                return
            # Reading position: upper-middle, gaussian-spread. The exact centre is itself a tell —
            # nobody parks the pointer on the centre pixel of the viewport.
            ax = min(max(vw * 0.5 + random.gauss(0, vw * 0.10), vw * 0.12), vw * 0.88)
            ay = min(max(vh * 0.34 + random.gauss(0, vh * 0.09), vh * 0.10), vh * 0.62)
            _glide(ax, ay, target_w=140)   # coarse placement, not a target acquisition
        except Exception:  # noqa: BLE001 - never let the anchor stop the scroll
            pass

    def _notch_px():
        """Pixels per wheel notch for the platform the page believes, read once per page."""
        if "notch" not in st:
            plat = None
            try:
                plat = platform_from_navigator(world_for(page).evaluate(ISO_PLATFORM))
            except Exception:  # noqa: BLE001
                pass
            st["notch"] = WHEEL_NOTCH_PX[plat or _host_platform()]
        return st["notch"]

    def hwheel(delta_x, delta_y):
        """Scroll in WHOLE NOTCHES, the only thing a wheel emits.

        A CDP wheel event always reports wheelDelta +-120 (one notch), whatever its distance. The
        eased slices this replaced (39/28/20/10/3 px for a 100 px scroll, each at wheelDelta 120,
        measured on r32/r33) were events no mouse produces: a real notch moves 100 px on Windows,
        120 on Linux, 40 on macOS. So the total is rounded to the nearest whole notch (at least
        one), and the notches come in finger-flick bursts."""
        _wheel_anchor()
        notch = _notch_px()
        plan = ([(0, notch if delta_y > 0 else -notch)] * wheel_notches(delta_y, notch)
                + [(notch if delta_x > 0 else -notch, 0)] * wheel_notches(delta_x, notch))
        burst = random.randint(2, 5)
        for k, (ex, ey) in enumerate(plan):
            native_wheel(ex, ey)
            if k == len(plan) - 1:
                break
            # local sleep, NOT page.wait_for_timeout: the latter is a CDP round-trip per call,
            # so it emits protocol traffic that bot-detectors (e.g. reCAPTCHA) can score — a
            # self-inflicted tell inside the humanize path. time.sleep is off-protocol.
            burst -= 1
            if burst == 0:
                time.sleep(_rand(110, 320) / 1000.0)  # the finger resets for the next flick
                burst = random.randint(2, 5)
            else:
                time.sleep(_rand(18, 55) / 1000.0)
            if random.random() < 0.07:
                time.sleep(_rand(150, 450) / 1000.0)  # occasional mid-scroll pause (reading)
            # A hand resting on the mouse does not hold it perfectly still through a long scroll,
            # so a burst of wheel events sharing one identical clientX/clientY is a signature
            # only a script produces. A couple of px, so the scroll stays over the same element
            # (and never while a button is down, where a move is a drag leg).
            if len(plan) >= 6 and not st["held"] and random.random() < 0.06:
                try:
                    dx, dy = random.gauss(0, 1.5), random.gauss(0, 1.2)
                    native_move(st["pos"][0] + dx, st["pos"][1] + dy)
                    st["pos"] = (st["pos"][0] + dx, st["pos"][1] + dy)
                except Exception:  # noqa: BLE001
                    pass

    mouse.move, mouse.click, mouse.wheel = hmove, hclick, hwheel
    if getattr(mouse, "dblclick", None) is not None:
        mouse.dblclick = hdblclick_xy   # stock dblclick teleported and pressed for ~0 ms
    # down/up are wrapped only to TRACK the held state hmove needs; the presses themselves stay
    # native and unchanged.
    mouse.down, mouse.up = hdown, hup

    # ----------------------------------------------------------------- keyboard
    kb = page.keyboard
    native_kb_type, native_kb_press = kb.type, kb.press
    # getattr: a page-like keyboard without down/up still gets every other humanization; it just
    # types capitals without the Shift key, as before.
    native_kb_down, native_kb_up = getattr(kb, "down", None), getattr(kb, "up", None)

    def _pointer_presence():
        """Give a keyboard-only session a mouse, once, before its first keystroke.

        Selector-driven typing already glides (page.type/fill/press go through _focus_click), but a
        bare page.keyboard.type/press types into whatever is focused — from a session in which the
        pointer never existed. A collector reading keydowns with zero preceding pointer events sees
        input no person can produce, however good the keystroke dynamics are.

        AMBIENT ONLY, deliberately not a move toward the focused field: the caller has already put
        focus somewhere (often from script), and gliding onto a control could hover or re-target it
        and land the text in the wrong place. plan_ambient never clicks, so focus cannot change.
        """
        if st["placed"] or st["kb_primed"]:
            return
        st["kb_primed"] = True   # one attempt per page, even if the ambient burst itself fails
        try:
            hambient(_rand(280, 620))
        except Exception:  # noqa: BLE001
            pass

    def _emit_key(ch):
        """Emit one character with a human keydown->keyup DWELL. keyboard.press's `delay` IS the hold
        (keyboard.type's delay is only inter-key flight). Fall back to type() for chars press can't map."""
        try:
            native_kb_press(ch, delay=key_dwell(persona))
        except Exception:  # noqa: BLE001
            try:
                native_kb_type(ch)
            except Exception:  # noqa: BLE001
                pass

    def _active_value():
        """The focused field's current value, read in the isolated world (None if unknown)."""
        try:
            return world_for(page).evaluate(ISO_ACTIVE_VALUE)
        except Exception:  # noqa: BLE001
            return None

    def _flight_s():
        # gaussian inter-key cadence (a realistic distribution, not a uniform band)
        return max(0.025, random.gauss(0.085, 0.045))

    def _backspaces(n):
        for k in range(n):
            native_kb_press("Backspace", delay=key_dwell(persona))
            if k < n - 1:
                time.sleep(_rand(70, 150) / 1000.0)

    def _landed(before, now):
        """How many characters arrived since ``before`` (None = cannot tell)."""
        if before is None or now is None:
            return None
        return len(now) - len(before)

    def _do_typo(text, i, tp):
        """Make the planned slip at text[i] and correct it. Returns True when it already typed
        text[i] itself (the doubled-key case), so the caller must not type it again.

        Only what actually LANDED is deleted: an input mask or a maxlength can swallow the wrong key,
        and a Backspace pressed for a key that never arrived deletes a real character (the
        letter-typo-in-a-numeric-mask failure). Measured before and after the slip, in the isolated
        world, one read each -- typos are ~2 % of keys, so this costs nearly nothing."""
        before = _active_value()
        kind = tp["kind"]
        if kind == "sub":
            _emit_key(tp["wrong"])
            got = _landed(before, _active_value())
            if got is not None and got <= 0:
                return False                     # swallowed: nothing to correct
            typed = 1
            for c in text[i + 1:i + 1 + tp.get("late", 0)]:   # late notice: a key or two more
                time.sleep(_flight_s())
                _emit_key(c)
                typed += 1
            time.sleep(_rand(140, 340) / 1000.0)  # noticing
            got = _landed(before, _active_value())
            _backspaces(typed if got is None else max(0, min(typed, got)))
            time.sleep(_rand(80, 200) / 1000.0)
            return False
        if kind == "double":
            _emit_key(text[i])
            time.sleep(_rand(45, 95) / 1000.0)      # the bounce comes fast
            _emit_key(text[i])
            got = _landed(before, _active_value())
            time.sleep(_rand(140, 340) / 1000.0)
            extra = 1 if got is None else max(0, got - 1)
            if extra:
                _backspaces(extra)
                time.sleep(_rand(80, 200) / 1000.0)
            return True
        if kind == "swap":
            _emit_key(text[i + 1])
            time.sleep(_flight_s())
            _emit_key(text[i])
            got = _landed(before, _active_value())
            time.sleep(_rand(140, 340) / 1000.0)
            _backspaces(2 if got is None else max(0, min(2, got)))
            time.sleep(_rand(80, 200) / 1000.0)
            return False
        return False

    def _typing_field(field):
        """What the typo gate needs about the field keys go to: the caller's description, else
        the focused element read in the isolated world."""
        if field is not None:
            return field
        try:
            return world_for(page).evaluate(ISO_ACTIVE_FIELD)
        except Exception:  # noqa: BLE001
            return None

    def _human_type_text(text, field=None, typos=True):
        """Type text key-by-key with human timing. Each char goes through Playwright's native
        keyboard (trusted), so this stays isTrusted=True.

        Capitals and shifted symbols are typed the way a person types them: ShiftLeft goes down a
        moment before the key and comes up just after it, held across a run of them. Playwright's
        press("A") alone sent the key with shiftKey=false and no Shift key at all (measured on r28
        with "Ab!") -- input no keyboard produces.

        Fat-finger typos are planned by _forms.plan_typo and never happen where a slip is a failure
        rather than a slip (passwords, e-mail, numbers, phone numbers, URLs, one-time codes)."""
        n = len(text)
        shift = False
        roll_rate = rollover_rate(persona)
        rate = typo_rate() if typos else 0.0
        allow = rate > 0 and typos_allowed(_typing_field(field))
        i = 0
        try:
            while i < n:
                ch = text[i]
                need = _needs_shift(ch)
                if need and not shift and native_kb_down and native_kb_up:
                    native_kb_down("Shift")
                    shift = True
                    time.sleep(_shift_lead_s())
                typo = False
                consumed = False
                if allow and not need and not shift and typo_eligible(ch):
                    tp = plan_typo(text, i, random, rate)
                    if tp is not None:
                        typo = True
                        try:
                            consumed = _do_typo(text, i, tp)
                        except Exception:  # noqa: BLE001
                            pass
                # ROLLOVER: the next key goes down before this one comes up, the way people type fast
                # pairs. One press() per character never overlaps (0 of 32 keydowns, measured on
                # r32/r33), a keystroke-dynamics tell. Unshifted single-key pairs only.
                nxt = text[i + 1] if i + 1 < n else None
                d1 = key_dwell(persona)
                if (nxt is not None and not typo and not shift and native_kb_down and native_kb_up
                        and ch in _ROLL_KEYS and nxt in _ROLL_KEYS and ch != nxt and d1 - 8 >= 12
                        and random.random() < roll_rate):
                    overlap = _rand(12, min(55, d1 - 8))
                    try:
                        native_kb_down(ch)
                        time.sleep((d1 - overlap) / 1000.0)
                        native_kb_down(nxt)
                        time.sleep(overlap / 1000.0)
                        native_kb_up(ch)
                        time.sleep(max(5.0, key_dwell(persona) - overlap) / 1000.0)
                        native_kb_up(nxt)
                    except Exception:  # noqa: BLE001
                        for k in (ch, nxt):
                            try:
                                native_kb_up(k)
                            except Exception:  # noqa: BLE001
                                pass
                        break
                    i += 1          # nxt is typed; the cadence below runs as if after it
                    ch = nxt
                elif not consumed:
                    try:
                        _emit_key(ch)
                    except Exception:  # noqa: BLE001
                        break
                if shift and (i == n - 1 or not _needs_shift(text[i + 1])):
                    time.sleep(_shift_lag_s())
                    native_kb_up("Shift")
                    shift = False
                if i < n - 1:
                    # gaussian inter-key cadence (a realistic distribution, not a uniform band)
                    d = max(0.025, random.gauss(0.085, 0.045))   # ~85ms +- 45ms, floored at 25ms
                    if ch in " \t\n":
                        d += _rand(0.02, 0.10)                    # slight pause at word boundaries
                    if random.random() < 0.06:
                        d += _rand(0.18, 0.45)                    # occasional thinking pause
                    time.sleep(d)
                i += 1
        finally:
            if shift:  # never leave Shift held for the caller's next keystroke
                try:
                    native_kb_up("Shift")
                except Exception:  # noqa: BLE001
                    pass

    def hkb_type(text, **kw):
        _pointer_presence()
        _human_type_text(text)

    def hkb_press(key, **kw):
        """keyboard.press with the persona's hold, unless the caller specified their own.

        WHY THIS EXISTS. keyboard.type was humanised from the start and keyboard.press was not,
        which left the most common single-key call in any script -- Enter, Tab, Escape, arrow keys
        -- emitting a keydown and keyUp in the same instant. Measured on this machine with
        humanize ON, before this wrapper: keyboard.type held keys 58-107ms while keyboard.press
        held them 1.4-3.8ms. A finger cannot press and release a key in under a millisecond, and
        a detector reading the shortest dwell in a session sees the press path, not the type path
        -- so one un-humanised call undid the whole keyboard persona.

        The caller's own `delay` wins: press(key, delay=...) is the documented way to hold a key
        for a specific time (game input, long-press UI), and silently overriding it would break
        that. Only the DEFAULT changes, from "no hold at all" to "a human hold".
        """
        _pointer_presence()
        if not kw.get("delay") or kw["delay"] <= 0:
            # delay=0 is Playwright's default ("no hold"), so an explicit 0 is no different from none
            kw["delay"] = key_dwell(persona)
        return native_kb_press(key, **kw)

    kb.type = hkb_type
    kb.press = hkb_press

    # ------------------------------------------------------- page-level targeted helpers
    # Points tried when resolving a control through the browser's hit test: a floating label or an
    # icon often sits over a field's CENTRE, so the edges get a chance too.
    probe_points = ((0.5, 0.5), (0.82, 0.5), (0.18, 0.5), (0.5, 0.8), (0.5, 0.2))

    def _describe(box, want=FORM_CONTROL):
        """The control filling ``box`` (Playwright's resolved box for a locator), read in the isolated
        world through the browser's own hit test: works for every Playwright selector (get_by_role,
        get_by_label, `>> nth=`, .filter()) where document.querySelector threw and dropped the call
        to stock input. The element found must fill the SAME box, or it is not the one the locator
        means; None when no probe point proves that."""
        if not box or box["width"] <= 0 or box["height"] <= 0:
            return None
        w = world_for(page)
        scroll = w.scroll()   # once: the hit test wants document coordinates
        for fx, fy in probe_points:
            nid = w.node_at(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy, scroll)
            if nid is None:
                continue
            info = w.call_on_node(nid, ISO_DESCRIBE, {"want": want})
            if not info or not info.get("rect"):
                continue
            rx, ry, rw, rh = info["rect"]
            if (abs(rx - box["x"]) <= 1.5 and abs(ry - box["y"]) <= 1.5
                    and abs(rw - box["width"]) <= 1.5 and abs(rh - box["height"]) <= 1.5):
                info["node"] = nid
                # The point that PROVED the control: when the centre is covered (a floating label),
                # this is where a click actually reaches it.
                info["probe"] = (fx, fy)
                return info
        return None

    def _proven_point(box, info):
        """A click point the hit test proved reaches the control, when its centre did not; None when
        the centre itself was fine (then the persona's own point is used, as for any click)."""
        if not info or info.get("probe", (0.5, 0.5)) == (0.5, 0.5):
            return None
        fx, fy = info["probe"]
        x = box["x"] + box["width"] * fx + random.gauss(0, 1.5)
        y = box["y"] + box["height"] * fy + random.gauss(0, 1.0)
        return (min(max(x, box["x"] + 1), box["x"] + box["width"] - 1),
                min(max(y, box["y"] + 1), box["y"] + box["height"] - 1))

    def _redescribe(info, want=FORM_CONTROL):
        """Describe the SAME element again, from the node the first hit test found: no new
        Playwright handle (see _box_of), so nothing the page can see. None if it is gone."""
        again = world_for(page).call_on_node(info["node"], ISO_DESCRIBE, {"want": want})
        if again:
            again["node"] = info["node"]
            again["probe"] = info.get("probe", (0.5, 0.5))
        return again

    def _in_view(box):
        vw, vh = _viewport()
        return box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= vw and box["y"] + box["height"] <= vh

    def _rect_box(r):
        if not r or not r.get("rect"):
            return None
        x, y, w, h = r["rect"]
        return {"x": x, "y": y, "width": w, "height": h} if w > 0 and h > 0 else None

    def _css_box(selector):
        """The box of a plain-CSS selector's first match, read (and scrolled into view) in the
        isolated world: not even the one bounding_box() handle, and its textContent preview, is made.
        Used only when Playwright counts exactly the elements the browser's own querySelectorAll
        finds -- Playwright's CSS also pierces open shadow roots, so equal counts mean the same
        elements in the same order. None otherwise (get_by_role, text=, `>>` chains, :has-text()...:
        then Playwright's bounding_box)."""
        if not isinstance(selector, str) or not selector:
            return None
        w = world_for(page)
        r = w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": False})
        if not r or not r.get("n"):
            return None
        try:
            if page.locator(selector).count() != r["n"]:
                return None
        except Exception:  # noqa: BLE001
            return None
        box = _rect_box(r)
        if box and not (_in_view(box) and _hit_ok(box)):
            # scrollIntoViewIfNeeded also scrolls a scrolling container that clips it, and does
            # nothing when it already shows (then the cover is an overlay: the click's own check)
            box = _rect_box(w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": True}))
        return box

    def _hit_ok(box):
        """Does the element filling ``box`` own its centre -- not clipped by a scrolling container,
        not covered? The browser's own hit test, read in the isolated world."""
        return _hit_in_box(box, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2) is True

    def _scroll_window_to(box):
        """Scroll the WINDOW so ``box`` sits centred where it was off-screen, the way Playwright's
        scroll centres it, and return where the box is now (moved by exactly the scrolled distance) --
        or None. Needs no handle: the box is already known."""
        vw, vh = _viewport()
        if box["width"] > vw or box["height"] > vh:
            return None
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        dx = cx - vw / 2 if box["x"] < 0 or box["x"] + box["width"] > vw else 0
        dy = cy - vh / 2 if box["y"] < 0 or box["y"] + box["height"] > vh else 0
        moved = world_for(page).evaluate(ISO_SCROLL_BY, [dx, dy])
        if not isinstance(moved, list) or len(moved) != 2:
            return None
        return {"x": box["x"] - moved[0], "y": box["y"] - moved[1], "width": box["width"], "height": box["height"]}

    def _box_of(loc, timeout, force=False, selector=None):
        """Wait the way Playwright would, with the CALLER's timeout (None = the page default; a
        hard-coded 30 s used to override set_default_timeout both ways), and return the box, in view.
        With `force` Playwright skips actionability, so nothing is waited for: a hidden target goes
        native at once instead of timing out where the native call would have acted.

        Each Playwright call that resolves the element to a handle (wait_for,
        scroll_into_view_if_needed, bounding_box) makes a MAIN-world handle whose preview reads the
        element's textContent where the page can see it: verifier B counted 4 such reads per
        humanized click and 7 per textarea fill. So each is made only when needed: the wait only for
        an element not visible yet (is_visible runs in Playwright's utility world), the scroll only
        for a box not already inside the viewport, and a plain-CSS ``selector`` needs no handle at
        all (_css_box). What remains, for other selectors, is the one bounding_box."""
        if not loc.is_visible():
            if force:
                raise _Native()
            loc.wait_for(state="visible", timeout=timeout)
        box = _css_box(selector)
        if box is not None:
            return box
        box = loc.bounding_box()
        if box and not (_in_view(box) and _hit_ok(box)):
            # Off-screen: scroll the window to it and prove it shows there (hit test), with no new
            # handle. Clipped by a scrolling container, covered, or the window scroll did not bring
            # it out: Playwright's scroll, which also scrolls containers, and a fresh box.
            moved = _scroll_window_to(box) if not _in_view(box) else None
            if moved and _in_view(moved) and _hit_ok(moved):
                return moved
            loc.scroll_into_view_if_needed(timeout=timeout)
            box = loc.bounding_box()
        return box

    def _focused(loc):
        """Does ``loc`` hold the keyboard focus? Asked through Playwright's utility-world selector
        engine (`:focus`), so it works for every element and every locator and the page sees nothing.
        The isolated hit test only knew form controls, so a focused BUTTON read as unfocused and got
        clicked before a press -- two activations."""
        try:
            return loc.and_(page.locator(":focus")).count() > 0
        except Exception:  # noqa: BLE001
            return False

    def _is_focused(selector):
        return _focused(page.locator(selector).first)

    def _text_entry(loc, timeout, selector=None):
        """(box, info) when ``loc`` is a visible text-entry control a person would click into before
        typing, else None. Anything else -- a button, a link, a hidden helper textarea, a slider -- is
        left to Playwright, which focuses it without a click (a click on a button activates it; on a
        slider it moves it)."""
        if not loc.is_visible():
            return None
        box = _box_of(loc, timeout, selector=selector)
        info = _describe(box)
        if info and classify_field(info["tag"], info["type"], info["ce"]) == "text":
            return box, info
        return None

    def _focus_click(box, at=None, loc=None, selector=None):
        """Click into the control filling ``box`` (resolved, visible and in view). ``at``: the point
        the hit test proved when the centre is covered (a floating label); else the persona's point.
        Stability is checked in the isolated world -- the control must still fill the same box 50 ms
        later (an animation in flight goes native) -- not by a second bounding_box(), which the page
        could see (see _box_of).

        A control no probe point reaches -- a label or an icon covers all of it -- is clicked anyway,
        where a person clicks (a floating label passes the focus on), and the caller checks that the caret
        arrived. Its stability then comes from a fresh box: the isolated CSS read, else bounding_box().
        Requiring the hit test there sent a fully labelled field to Playwright's insertText (found by the
        .NET port)."""
        if not box:
            return False
        time.sleep(0.05)
        info = _describe(box)
        if info is None:
            again = _css_box(selector) if selector else None
            if again is None and loc is not None:
                again = loc.bounding_box()
            if not again or abs(again["x"] - box["x"]) > 1 or abs(again["y"] - box["y"]) > 1:
                return False
        elif info.get("disabled"):
            return False
        pt = at if at is not None else click_point(box, st["pos"], persona)  # 2D gaussian toward centre
        _glide(pt[0], pt[1])
        time.sleep(_rand(40, 130) / 1000.0)
        native_click(pt[0], pt[1], delay=click_hold(persona))
        return True

    def _key(k, n=1, gap=(60, 150)):
        for j in range(n):
            native_kb_press(k, delay=key_dwell(persona))
            if j < n - 1:
                time.sleep(_rand(*gap) / 1000.0)

    def _select_all():
        """Ctrl+A (Cmd+A on a Mac engine) the way a hand does it: the modifier goes down a moment
        before the A, the A is held, the modifier comes up just after. Sent as one Playwright chord the
        modifier and the A went down within ~1 ms of each other. ControlOrMeta lets Playwright pick the
        chord the ENGINE's editing honours: a Meta+A sent to a Linux engine selects nothing."""
        if native_kb_down is None or native_kb_up is None:
            _key("ControlOrMeta+a")
            return
        native_kb_down("ControlOrMeta")
        try:
            time.sleep(_rand(40, 110) / 1000.0)
            native_kb_press("a", delay=key_dwell(persona))
            time.sleep(_rand(20, 80) / 1000.0)
        finally:
            native_kb_up("ControlOrMeta")

    def _clear_focused(read=None):
        """Select all, then Backspace -- each key HELD like a finger holds it. The old clear sent
        Control, a and Backspace with ~1 ms holds, which is exactly what a key-dwell check flags.
        If the field is still not empty (``read`` -- the field's value -- says so), it is emptied from
        the end, one Backspace per character."""
        read = read or _active_value
        _select_all()
        time.sleep(_rand(60, 160) / 1000.0)
        _key("Backspace")
        time.sleep(_rand(60, 160) / 1000.0)
        left = read()
        if left:
            if len(left) > 64:
                raise RuntimeError("field would not clear")
            _key("End")
            time.sleep(_rand(80, 180) / 1000.0)
            _key("Backspace", len(left), gap=(55, 120))
            time.sleep(_rand(60, 160) / 1000.0)
            if read():
                raise RuntimeError("field would not clear")

    native_page_click = page.click
    native_page_type = page.type
    native_page_fill = page.fill
    native_page_press = page.press
    native_page_dblclick = page.dblclick

    def _click_into(selector, timeout):
        """Make ``selector`` the focus the way a person does (a click), unless it already is. Only
        text-entry controls are clicked; for anything else, and when the click did not actually put
        the caret there (an overlay took it), the caller goes native."""
        loc = page.locator(selector).first
        if _focused(loc):
            return
        te = _text_entry(loc, timeout, selector)
        if te is None or not _focus_click(te[0], _proven_point(*te), loc, selector) or not _focused(loc):
            raise _Native()
        time.sleep(_rand(40, 120) / 1000.0)

    def htype(selector, text, **options):
        try:
            _click_into(selector, options.get("timeout"))
            _human_type_text(text)
            return None
        except _Native:
            pass
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        # outside the try: a native call that raises must raise ONCE, not be retried by the handler
        return native_page_type(selector, text, **options)

    def _fill_text(loc, box, value, info, selector=None):
        """Type ``value`` into a text control and PROVE it landed. The value is read through
        Playwright's utility world (`input_value`): it sees the control the locator means, wherever
        focus is, and the page cannot observe it. A field whose value cannot be read, or a click that
        did not put the caret in it (an overlay took it), is left to Playwright -- "could not check"
        is never treated as success."""
        if len(value) > 200:
            return False   # bulk values: keep fill fast/atomic (humanizing 1000s of chars would crawl)
        is_ce = bool(info and info.get("ce")) or info is None and (loc.get_attribute("contenteditable") or "false") != "false"
        if not _focus_click(box, _proven_point(box, info) if box else None, loc, selector):
            return False
        time.sleep(_rand(40, 120) / 1000.0)
        if not _focused(loc):
            return False

        def read():
            try:
                return loc.input_value(timeout=COVER_RETRY_MS)
            except Exception:  # noqa: BLE001
                return None

        if is_ce:
            cur = info.get("value") if info else None
            if cur:
                _clear_focused()
            _human_type_text(value, field=info)
            return True   # contenteditable: Playwright's fill sets text the same way; no value to read
        cur = read()
        if cur is None:
            return False
        if cur:  # a person does not select-all an empty field
            _clear_focused(read)
        _human_type_text(value, field=info)
        got = read()
        if got is None:
            return False
        if got == value or formatted_equal(got, value):
            return True   # exact, or a mask that only re-punctuated what was typed (a phone format)
        # Wrong value (an autocompleter interfered, a key was lost): one clean retry without slips,
        # then give the field to Playwright -- a correct value beats pretty input.
        _clear_focused(read)
        _human_type_text(value, field=info, typos=False)
        got = read()
        return got is not None and (got == value or formatted_equal(got, value))

    def _fill_range(loc, box, info, value):
        """A range input: drag the thumb (or click the track) to the value, then arrow-nudge to it
        exactly. Typing digits into a slider -- what fill() did before -- changes nothing, and the
        click before it left the thumb wherever it landed, with no error."""
        if info is None:
            info = {"min": loc.get_attribute("min"), "max": loc.get_attribute("max"),
                    "step": loc.get_attribute("step"), "value": loc.input_value(), "dir": "ltr", "wm": ""}
        if not range_value_ok(value, info.get("min"), info.get("max"), info.get("step")):
            # Off the step, out of range, not a number: Playwright raises "Malformed value" for these,
            # so does this route (by leaving them to it) -- never a silently snapped or clamped value.
            return False
        if (info.get("wm") or "").startswith("vertical"):
            return False
        rtl = info.get("dir") == "rtl"
        lo, hi, step = range_attrs(info.get("min"), info.get("max"), info.get("step"))
        target = range_sanitize(value, info.get("min"), info.get("max"), info.get("step"))

        def now():
            try:
                return float(loc.input_value())
            except (TypeError, ValueError):
                return None

        cur = now()
        if cur is not None and numeric_equal(cur, target):
            return True
        tx, ty = range_point(box, range_fraction(target, lo, hi), rtl)
        ty += random.gauss(0, box["height"] * 0.08)
        if cur is not None and native_down and native_up and random.random() < 0.65:
            # Grab the thumb where it is and slide it: what most hands do with a slider.
            cx, cy = range_point(box, range_fraction(cur, lo, hi), rtl)
            cy += random.gauss(0, box["height"] * 0.08)
            _glide(cx, cy, target_w=16)
            time.sleep(_rand(60, 160) / 1000.0)
            grab_ms, release_ms = drag_dwell(persona)
            native_down()
            st["held"] = True
            try:
                time.sleep(grab_ms / 1000.0)
                _glide(tx, ty, settle=True)
                time.sleep(release_ms / 1000.0)
            finally:
                native_up()
                st["held"] = False
        else:
            _glide(tx, ty, target_w=16)
            time.sleep(_rand(40, 130) / 1000.0)
            native_click(tx, ty, delay=click_hold(persona))
        for _ in range(3):
            v = now()
            if v is None:
                return False
            if numeric_equal(v, target):
                return True
            key, n = range_nudge(v, target, step)
            if key is None or n <= 0 or n > 40:
                break
            if rtl:
                key = "ArrowLeft" if key == "ArrowRight" else "ArrowRight"
            time.sleep(_rand(150, 350) / 1000.0)
            _key(key, n)
        v = now()
        return v is not None and numeric_equal(v, target)

    def _fill_datetime(loc, box, info, value):
        """A native date / time / month / week / datetime-local editor: click into its first field
        and type each field in the order the editor itself shows them (read from its UA shadow tree
        over CDP -- the order follows the browser, not the persona, so it is never assumed)."""
        if not info or not info.get("node"):
            return False
        kind = info.get("type")
        w = world_for(page)
        tree = w.send("DOM.describeNode", {"backendNodeId": info["node"], "depth": -1, "pierce": True})
        if not tree or "node" not in tree:
            return False
        segs = segment_nodes_from_tree(tree["node"])
        plan = plan_datetime(kind, [s for s, _ in segs], value, current=info.get("value"))
        if not plan or not segs:
            return False
        px, py, fw = None, None, 12.0
        bm = w.send("DOM.getBoxModel", {"backendNodeId": segs[0][1]}) if segs[0][1] else None
        if bm and bm.get("model"):
            c = bm["model"]["content"]
            x0, y0, x1, y1 = min(c[0::2]), min(c[1::2]), max(c[0::2]), max(c[1::2])
            # trust the field box only when it lies inside the input's own box
            if box["x"] - 1 <= x0 < x1 <= box["x"] + box["width"] + 1 and box["y"] - 1 <= y0 < y1 <= box["y"] + box["height"] + 1:
                px = _rand(x0 + (x1 - x0) * 0.25, x0 + (x1 - x0) * 0.75)
                py = (y0 + y1) / 2 + random.gauss(0, max(0.5, (y1 - y0) * 0.1))
                fw = max(6.0, x1 - x0)
        if px is None:
            px = box["x"] + min(14.0, box["width"] * 0.12)
            py = box["y"] + box["height"] / 2
        _glide(px, py, target_w=fw)
        time.sleep(_rand(40, 130) / 1000.0)
        native_click(px, py, delay=click_hold(persona))
        time.sleep(_rand(150, 350) / 1000.0)
        for a in plan:
            if a[0] == "type":
                for j, ch in enumerate(a[1]):
                    native_kb_press(ch, delay=key_dwell(persona))
                    if j < len(a[1]) - 1:
                        time.sleep(max(0.03, random.gauss(0.11, 0.04)))
            else:
                _key(a[1], a[2] if len(a) > 2 else 1, gap=(70, 170))
            time.sleep(_rand(60, 220) / 1000.0)   # between fields: the eye checks what it typed
        return datetime_equal(kind, loc.input_value(), value)

    def _classify_fallback(loc):
        """Classify through Playwright's utility-world queries when the hit test could not prove the
        element (it is covered, or inside a frame): attributes only, nothing the page can see."""
        for tag in ("textarea", "select"):
            if loc.locator("xpath=self::%s" % tag).count():
                return classify_field(tag)
        if loc.locator("xpath=self::input").count():
            return classify_field("input", loc.get_attribute("type"))
        ce = loc.get_attribute("contenteditable")
        return classify_field("div", None, ce is not None and ce.lower() != "false")

    def hfill(selector, value, **options):
        try:
            if isinstance(value, str):
                timeout = options.get("timeout")
                loc = page.locator(selector).first
                box = _box_of(loc, timeout, options.get("force"), selector)
                info = _describe(box)
                kind = (classify_field(info["tag"], info["type"], info["ce"]) if info
                        else _classify_fallback(loc))
                if not (info and (info.get("disabled") or info.get("readonly"))):
                    if kind == "text" and _fill_text(loc, box, value, info, selector):
                        return None
                    if kind == "range" and _fill_range(loc, box, info, value):
                        return None
                    if kind == "datetime" and _fill_datetime(loc, box, info, value):
                        return None
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        # Native: color (Playwright sets it -- the OS dialog is no route), unfillable types (Playwright
        # raises, as before humanize), and anything the humane route could not prove it did right.
        return native_page_fill(selector, value, **options)

    def hdblclick(selector, **options):
        o = dict(options)
        o.setdefault("click_count", 2)
        return _targeted_click(native_page_dblclick, selector, o, no_click=False, native_opts=options)

    def hpress(selector, key, **options):
        try:
            # Click into text fields only: a press on a button or a link that is not focused yet is
            # Playwright's (it focuses without activating); clicking it first activated it twice.
            _click_into(selector, options.get("timeout"))
            # The persona's hold unless the caller set a real one, for the same reason hkb_press
            # carries one: this is the path Locator.press delegates to.
            d = options.get("delay")
            native_kb_press(key, delay=d if d else key_dwell(persona))
            return None
        except _Native:
            pass
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        return native_page_press(selector, key, **options)

    # getattr for the same reason as mouse.down/up above: a page-like object that does not
    # implement select_option must still get every OTHER humanization, not lose all of it to
    # an AttributeError raised while wiring this one up.
    native_page_select_option = getattr(page, "select_option", None)

    def _open_and_pick(selector, plan, at=None):
        """Open the drop-down with a click, move its highlight with the planned keys, commit with
        Enter: one trusted input+change, the sequence a mouse-and-keyboard user produces. While the
        popup is open its keys never reach the page (measured on r37: value unchanged until Enter).
        The opening click is the full humanized click -- including the covered-point check -- at the
        point the hit test proved when the centre is covered (``at``, a Playwright `position`), so a
        floating label over the select's centre cannot swallow it and leave the letters to change a
        closed select one per key."""
        _targeted_click(native_page_click, selector, {"position": at} if at else {}, no_click=False,
                        native_opts=None)
        time.sleep(_rand(220, 480) / 1000.0)        # the list opens; the eye finds the option
        for ch in plan["keys"]:
            native_kb_press(ch, delay=key_dwell(persona))
            time.sleep(max(0.04, random.gauss(0.10, 0.035)))   # well inside type-ahead's 1 s window
        n = plan["arrows"]
        if n:
            time.sleep(_rand(120, 300) / 1000.0)
            _arrow_run("ArrowDown" if n > 0 else "ArrowUp", abs(n))
        time.sleep(_rand(150, 400) / 1000.0)
        _key("Enter")
        time.sleep(_rand(80, 160) / 1000.0)

    def _arrow_run(key, n):
        """``n`` presses of an arrow key in an OPEN drop-down. A long run is HELD, the way a person
        crosses a long list: one press, the keyboard's repeat delay, then auto-repeat at its rate --
        Playwright sends a key that is already down again as an autoRepeat keydown. Pressed one by
        one at a human ~180 ms each, 370 rows took 68 s (verifier B). The open popup takes these keys,
        so the page sees none of them, only how long the pick took."""
        if n < 8 or native_kb_down is None or native_kb_up is None:
            _key(key, n)
            return
        native_kb_down(key)
        try:
            time.sleep(_rand(380, 560) / 1000.0)    # the repeat delay
            gap = _rand(28, 36) / 1000.0            # ~30 repeats a second
            for _ in range(n - 1):
                time.sleep(gap)
                native_kb_down(key)
        finally:
            time.sleep(_rand(20, 60) / 1000.0)
            native_kb_up(key)

    def _listbox_row(info, i):
        return world_for(page).call_on_node(info["node"], ISO_LISTBOX_ROW, {"i": i})

    def _settled_row(info, i):
        """The list box's state once its scroll has come to rest. A wheel notch scrolls the box
        SMOOTHLY over a few frames, and a read taken mid-animation shows the row where it was, not
        where it is going: the old loop then wheeled the wrong way and oscillated (verifier B:
        scrollTop 120 -> 240 -> 323 -> 203 -> 83 -> 0, twice, with the row in view at 120, then a
        native, untrusted fallback). Two equal reads 50 ms apart = at rest."""
        prev = _listbox_row(info, i)
        for _ in range(30):
            time.sleep(0.05)
            cur = _listbox_row(info, i)
            if cur is None or prev is None:
                return cur
            if abs(cur["st"] - prev["st"]) < 0.5 and abs(cur["row"][1] - prev["row"][1]) < 0.5:
                return cur
            prev = cur
        return prev

    def _row_span(s):
        """The visible vertical span (top, bottom) of the row, or None when too little of it shows.
        A row cut off by the box edge is still a target a person clicks when most of it shows."""
        x, y, w, h = s["row"]
        if w <= 0 or h <= 0:
            return None
        top, bottom = max(y, s["top"]), min(y + h, s["bottom"])
        return (top, bottom) if bottom - top >= min(h, max(6.0, h * 0.6)) else None

    def _scroll_row_into_view(info, i):
        """Wheel the list box until row ``i`` shows: the pointer goes over the box once, then as many
        notches as the distance needs -- one at first, to learn how far a notch scrolls this box --
        each burst read only once the scroll has settled. (top, bottom, state), or None when it
        cannot be brought into view (the wheel does not move the box, or it is at its end)."""
        s = _settled_row(info, i)
        if s is None:
            return None
        span, per, over, notches = _row_span(s), None, False, 0
        while span is None and notches < 60:
            x, y, w, h = s["row"]
            room = s["bottom"] - s["top"]
            margin = _rand(2, max(3, min(12, (room - h) * 0.4)))
            need = (y + h) - s["bottom"] + margin if y + h > s["bottom"] else y - s["top"] - margin
            if (need > 0 and s["st"] >= s["max"] - 0.5) or (need < 0 and s["st"] <= 0.5):
                return None
            if not over:
                bx, by, bw, bh = s["box"]
                _glide(bx + bw * _rand(0.3, 0.7), s["top"] + room * _rand(0.3, 0.7), target_w=max(20, room))
                time.sleep(_rand(80, 200) / 1000.0)
                over = True
            n = 1 if not per else max(1, min(8, int(abs(need) // per) + (1 if abs(need) % per > per * 0.25 else 0)))
            for j in range(n):
                native_wheel(0, _notch_px() if need > 0 else -_notch_px())
                notches += 1
                if j < n - 1:
                    time.sleep(_rand(30, 70) / 1000.0)
            before = s["st"]
            s = _settled_row(info, i)
            if s is None:
                return None
            moved = abs(s["st"] - before)
            if moved < 0.5:
                return None   # the wheel did not scroll the box
            per = moved / n
            span = _row_span(s)
        return (span[0], span[1], s) if span else None

    def _select_listbox(loc, box, info, idx):
        """A list box (multiple or size > 1): click the rows -- the first plainly (which also drops
        any other selection), the rest with Ctrl/Cmd held, scrolling the box with the wheel to bring
        a row into view the way a person does."""
        want = sorted(idx)
        opts = info["options"]
        if set(want) == {i for i, o in enumerate(opts) if o.get("selected")}:
            return [opts[i]["value"] for i in want]
        for k, i in enumerate(want):
            got = _scroll_row_into_view(info, i)
            if got is None:
                return None
            top, bottom, s = got
            r = s["row"]
            px = _rand(r[0] + r[2] * 0.12, r[0] + min(r[2] * 0.6, 140))
            py = (top + bottom) / 2 + random.gauss(0, max(0.5, (bottom - top) * 0.12))
            py = min(max(py, top + 1), bottom - 1)
            _glide(px, py, target_w=bottom - top)
            if k and native_kb_down is not None:
                # ControlOrMeta: Playwright sends the add-to-selection key the ENGINE honours
                _press_at(px, py, normalize_click_options({"modifiers": ["ControlOrMeta"]}))
            else:
                time.sleep(_rand(40, 130) / 1000.0)
                native_click(px, py, delay=click_hold(persona))
            time.sleep(_rand(150, 350) / 1000.0)
        final = _listbox_row(info, want[0])
        if not final:
            return None
        got = [i for i, sel in enumerate(final["sel"]) if sel]
        return [opts[i]["value"] for i in want] if got == want else None

    def _select_humanized(selector, value, options):
        """Pick options the way a person does (see _open_and_pick / _select_listbox). Returns the
        selected values like Playwright, or None when the humane route cannot apply or cannot be
        shown to have worked -- the caller then runs stock select_option."""
        if options.get("element") is not None:
            return None
        wants = []
        for k, v in (("value", value), ("index", options.get("index")), ("label", options.get("label"))):
            if v is None:
                continue
            for x in (v if isinstance(v, (list, tuple)) else [v]):
                if isinstance(x, dict):
                    return None   # {value, label, index} option dicts: native
                wants.append((k, x))
        if not wants:
            return None
        loc = page.locator(selector).first
        box = _box_of(loc, options.get("timeout"), options.get("force"), selector)
        info = _describe(box, "select")
        if not info or info.get("tag") != "select" or info.get("disabled"):
            return None
        opts = info["options"]
        idx = []
        for kind, w in wants:
            i = match_option(opts, **{kind: w})
            if i < 0 or opts[i].get("disabled"):
                return None   # not there (yet) or disabled: Playwright waits / reports it
            if i not in idx:
                idx.append(i)
        if info.get("multiple") or (info.get("size") or 0) > 1:
            if not info.get("multiple"):
                idx = [min(idx)]
            return _select_listbox(loc, box, info, idx)
        t = min(idx)   # Playwright: the FIRST option (document order) matching any wanted one
        if info.get("selectedIndex") == t:
            return [opts[t]["value"]]
        labels = [None if o.get("disabled") else (o.get("label") or "") for o in opts]
        plan = plan_select(labels, info.get("selectedIndex", -1), t)
        if plan is None:
            return None
        pt = _proven_point(box, info)
        at = ({"x": pt[0] - box["x"] - info.get("bl", 0), "y": pt[1] - box["y"] - info.get("bt", 0)}
              if pt else None)
        _open_and_pick(selector, plan, at)
        if loc.input_value() == opts[t]["value"]:
            return [opts[t]["value"]]
        # One correction from wherever the highlight landed (arrows only), then give up to native.
        again = _redescribe(info, "select")
        if not again:
            return None
        fix = plan_select(labels, again.get("selectedIndex", -1), t, max_prefix=0)
        if fix is None:
            return None
        _open_and_pick(selector, fix, at)
        return [opts[t]["value"]] if loc.input_value() == opts[t]["value"] else None

    def hselect_option(selector, value=None, **options):
        """page.select_option, humanized (see _select_humanized)."""
        try:
            res = _select_humanized(selector, value, options)
            if res is not None:
                return res
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        return native_page_select_option(selector, value, **options)

    def _set_checked(orig, selector, checked, options):
        """check / uncheck / set_checked: a humanized click when the state must change, then the
        state verified -- Playwright raises when a click does not change it, so do we (by falling
        through to the native call, which raises with its own message). `trial` performs no action:
        Playwright's own trial runs."""
        if options.get("trial"):
            return orig(selector, **options)
        loc = page.locator(selector).first
        # Errors propagate as Playwright's own (not a checkbox, timed out): the click below owns its
        # native fallback, so nothing here may click a second time.
        if loc.is_checked(timeout=options.get("timeout")) == checked:
            return None
        _targeted_click(native_page_click, selector, dict(options), no_click=False, native_opts=None)
        if loc.is_checked() == checked:
            return None
        # One click did not change it (a page that cancels the click): Playwright's check raises here
        # rather than clicking again, and so does this.
        from playwright.sync_api import Error as PlaywrightError
        raise PlaywrightError("Clicking the checkbox did not change its state")

    page.type = htype
    page.fill = hfill
    page.dblclick = hdblclick
    page.press = hpress
    if native_page_select_option is not None:
        page.select_option = hselect_option
    for name, want in (("check", True), ("uncheck", False)):
        orig_c = getattr(page, name, None)
        if orig_c is not None:
            setattr(page, name, (lambda o, w: lambda selector, **opts: _set_checked(o, selector, w, opts))(orig_c, want))
    orig_sc = getattr(page, "set_checked", None)
    if orig_sc is not None:
        page.set_checked = lambda selector, checked, **opts: _set_checked(
            lambda s, **o: orig_sc(s, checked, **o), selector, bool(checked), opts)

    def _hit_in_box(box, x, y):
        """Does a press at viewport (x, y) reach the element filling ``box``? True / False, or None
        when it cannot be told here: the point lies in a child frame, or CDP is unavailable."""
        w = world_for(page)
        nid = w.node_at(x, y)
        if nid is None:
            return None
        res = w.call_on_node(nid, ISO_HIT_IN_BOX,
                             {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]})
        return None if res is None else bool(res)

    def _reaches_point(orig, selector, box, x, y):
        """The covered-point check, through the browser's own hit test read in the isolated world:
        nothing is dispatched and the pointer does not move. It replaced a Playwright TRIAL action,
        which moves the REAL pointer onto the element and back and performs a press with its events
        blocked: verifier B saw, before every humanized click since 0.46.0, a mousemove inside the
        target, a 450-500 px jump back, then the glide, and a pointerdown/pointerup/click (left, even
        for a right click) that window capture listeners could see. A covered point is looked at
        again for COVER_CHECK_MS (an overlay fading out)."""
        deadline = time.monotonic() + COVER_CHECK_MS / 1000.0
        hit = _hit_in_box(box, x, y)
        while hit is False and time.monotonic() < deadline:
            time.sleep(0.08)
            hit = _hit_in_box(box, x, y)
        if hit is not None:
            return hit
        # Only Playwright's trial can look inside a child frame. It moves the pointer: the cost of a
        # point the isolated world cannot see, never of an ordinary click.
        at = {"x": x - box["x"], "y": y - box["y"]}
        try:
            orig(selector, trial=True, timeout=COVER_CHECK_MS, position=at)
            return True
        except Exception as e:  # noqa: BLE001
            if not _is_timeout(e) or "intercepts pointer events" in str(e):
                return False
        try:
            # Not covered, just not "stable" yet: on the first page of a fresh headless browser
            # Playwright's animation-frame wait can outlast the first look. One longer look.
            orig(selector, trial=True, timeout=COVER_RETRY_MS, position=at)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _targeted_click(orig, selector, options, no_click, native_opts):
        """The humanized form of page.click / dblclick / hover (and the click inside check/uncheck).

        Every Playwright option is honoured: `button`, `click_count`, `delay` (the hold) and
        `modifiers` shape the press; `position` replaces the persona's point; `force` skips the
        hit test; `trial` performs NO action (it runs Playwright's own trial, as documented). The old
        wrapper forwarded only `timeout`, so a right click became a left click and a trial clicked."""
        o = normalize_click_options(options, _platform())

        def _native():
            if orig is None:
                raise RuntimeError("humanized click could not run")
            try:
                return orig(selector, **(native_opts if native_opts is not None else options))
            finally:
                # Native click/hover genuinely move the browser pointer to the element. Leaving
                # st["placed"] False afterwards made the next wheel anchor believe the pointer was
                # still unplaced, so it glided away from the element the click had just left.
                st["placed"] = True

        if o["trial"]:
            return _native()
        try:
            timeout = options.get("timeout")   # None: the page's default, as Playwright would use
            loc = page.locator(selector).first
            # Playwright skips actionability with force: no wait then -- a target it would click
            # hidden goes native at once instead of timing out first.
            box = _box_of(loc, timeout, o["force"], selector)
            if not box:
                raise _Native()
            if not o["force"] and not loc.is_enabled():
                raise _Native()
            if o["position"]:
                # Playwright's position is relative to the PADDING box: add the border widths.
                info = _describe(box, None)
                bl, bt = (info.get("bl", 0), info.get("bt", 0)) if info else (0, 0)
                x, y = box["x"] + bl + o["position"]["x"], box["y"] + bt + o["position"]["y"]
            else:
                x, y = click_point(box, st["pos"], persona)
            if not o["force"] and orig is not None:
                # Stability, then covered-by: 50 ms on, the element must still fill `box` (an
                # animation in flight does not) and own the click point -- no trusted press at a
                # point some overlay owns. Either failing goes native: Playwright waits for it.
                time.sleep(0.05)
                if not _reaches_point(orig, selector, box, x, y):
                    raise _Native()
            if o["modifiers"] and native_kb_down is None:
                raise _Native()
            if no_click and o["modifiers"]:
                # hover with modifiers: Playwright holds them across the move
                held = []
                try:
                    for m in o["modifiers"]:
                        native_kb_down(m)
                        held.append(m)
                        time.sleep(_rand(40, 110) / 1000.0)
                    _glide(x, y)
                finally:
                    for m in reversed(held):
                        time.sleep(_rand(20, 80) / 1000.0)
                        try:
                            native_kb_up(m)   # one failed keyup must not strand the others
                        except Exception:  # noqa: BLE001
                            pass
                return None
            _glide(x, y)
            if not no_click:
                if native_down is None or native_up is None:
                    time.sleep(_rand(40, 130) / 1000.0)
                    native_click(x, y, delay=o["delay"] if o["delay"] is not None else click_hold(persona),
                                 button=o["button"], click_count=o["click_count"])
                else:
                    _press_at(x, y, o)
            return None
        except _Native:
            pass
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        # outside the try, so a native call that raises is not run a second time by the handler
        return _native()

    def _make_targeted(orig, no_click):
        def wrapped(selector, **options):
            return _targeted_click(orig, selector, options, no_click, None)
        return wrapped

    page.click = _make_targeted(page.click, False)
    page.hover = _make_targeted(page.hover, True)

    # Marker the Locator-class patch keys off: humanize is active for THIS page.
    page._clearcote_humanized = True
    _patch_locator_class()


class _Native(Exception):
    """Internal: the humane route cannot apply here; run the native Playwright call instead."""


def _is_timeout(err):
    """A Playwright timeout: the element never became actionable. Falling back to the native call
    after one would wait the FULL timeout again (a missing element took 2-3x `timeout` to fail), so
    timeouts propagate instead."""
    return type(err).__name__ == "TimeoutError"

# --------------------------------------------------------------------------- locator patch
_locator_patched = False


def _patch_locator_class():
    """Patch the sync Locator class once so locator.* interactions route through the humanized
    page.* methods. Each method is a no-op unless the locator's page has humanize active AND the
    locator targets the main frame; anything else (frame locators, exotic selectors, errors)
    falls straight through to the original Playwright behaviour. Composes safely if another
    library has already patched Locator — we chain to whatever was there before."""
    global _locator_patched
    if _locator_patched:
        return
    _locator_patched = True
    try:
        from playwright.sync_api._generated import Locator
    except Exception:  # noqa: BLE001
        return

    o_fill = Locator.fill
    o_click = Locator.click
    o_type = Locator.type
    o_dblclick = Locator.dblclick
    o_hover = Locator.hover
    o_press = Locator.press
    o_press_seq = Locator.press_sequentially
    o_clear = Locator.clear
    o_tap = Locator.tap
    o_check = Locator.check
    o_uncheck = Locator.uncheck
    o_drag_to = Locator.drag_to
    o_select_option = Locator.select_option
    o_set_checked = getattr(Locator, "set_checked", None)

    def _on(self):
        # humanize active for this page?
        if not getattr(self.page, "_clearcote_humanized", False):
            return False
        # main-frame only: frame-locator selectors don't round-trip through page.* reliably,
        # so let those use native Playwright (which handles frames correctly). A FrameLocator's
        # child locator keeps the MAIN frame as its _frame and enters the iframe through its
        # selector, so the selector is what tells: on the humanized path its prefilled field was
        # never cleared ("old" + "new" = "oldnew").
        try:
            if "internal:control=enter-frame" in (self._impl_obj._selector or ""):
                return False
            return self._impl_obj._frame.parent_frame is None
        except Exception:  # noqa: BLE001
            return False

    def _strict(self):
        """Locator actions are STRICT in Playwright: more than one match is an error. The page-level
        methods they route through take the first match, so check the count here and let the native
        call raise its own strict-mode error when it is not exactly one."""
        try:
            return self.count() <= 1
        except Exception:  # noqa: BLE001
            return False

    def _sel(self):
        return self._impl_obj._selector

    # The options each page-level humanized method understands. Everything the caller passed that a
    # page method accepts travels with it: the old _fwd sent ONLY `timeout`, which turned
    # click(button="right") into a left click and click(trial=True) into a real one.
    _CLICK = ("button", "click_count", "delay", "force", "modifiers", "no_wait_after", "position",
              "timeout", "trial")
    _HOVER = ("force", "modifiers", "no_wait_after", "position", "timeout", "trial")
    _DBL = ("button", "delay", "force", "modifiers", "no_wait_after", "position", "timeout", "trial")
    _FILL = ("force", "no_wait_after", "timeout")
    _TYPE = ("timeout",)
    _PRESS = ("delay", "no_wait_after", "timeout")
    _SELECT = ("index", "label", "element", "force", "no_wait_after", "timeout")
    _CHECK = ("force", "no_wait_after", "position", "timeout", "trial")

    def _fwd(kw, names=("timeout",)):
        return {k: kw[k] for k in names if k in kw and kw[k] is not None}

    def _route(self, kw, fn):
        # Once routed, every error propagates: the page-level wrappers own the native fallback, so
        # retrying the original locator method here ran the action a second (or third) time -- a
        # checkbox whose click is cancelled was clicked three times.
        if _on(self) and _strict(self):
            return True, fn()
        return False, None

    def fill(self, value, **kw):
        done, r = _route(self, kw, lambda: self.page.fill(_sel(self), value, **_fwd(kw, _FILL)))
        return r if done else o_fill(self, value, **kw)

    def click(self, **kw):
        done, r = _route(self, kw, lambda: self.page.click(_sel(self), **_fwd(kw, _CLICK)))
        return r if done else o_click(self, **kw)

    def type_(self, text, **kw):
        done, r = _route(self, kw, lambda: self.page.type(_sel(self), text, **_fwd(kw, _TYPE)))
        return r if done else o_type(self, text, **kw)

    def dblclick(self, **kw):
        done, r = _route(self, kw, lambda: self.page.dblclick(_sel(self), **_fwd(kw, _DBL)))
        return r if done else o_dblclick(self, **kw)

    def hover(self, **kw):
        done, r = _route(self, kw, lambda: self.page.hover(_sel(self), **_fwd(kw, _HOVER)))
        return r if done else o_hover(self, **kw)

    def press(self, key, **kw):
        done, r = _route(self, kw, lambda: self.page.press(_sel(self), key, **_fwd(kw, _PRESS)))
        return r if done else o_press(self, key, **kw)

    def press_sequentially(self, text, **kw):
        done, r = _route(self, kw, lambda: self.page.type(_sel(self), text, **_fwd(kw, _TYPE)))
        return r if done else o_press_seq(self, text, **kw)

    def clear(self, **kw):
        done, r = _route(self, kw, lambda: self.page.fill(_sel(self), "", **_fwd(kw, _FILL)))
        return r if done else o_clear(self, **kw)

    def tap(self, **kw):
        done, r = _route(self, kw, lambda: self.page.click(_sel(self), **_fwd(kw, _HOVER)))
        return r if done else o_tap(self, **kw)

    def check(self, **kw):
        done, r = _route(self, kw, lambda: self.page.check(_sel(self), **_fwd(kw, _CHECK)))
        return r if done else o_check(self, **kw)

    def uncheck(self, **kw):
        done, r = _route(self, kw, lambda: self.page.uncheck(_sel(self), **_fwd(kw, _CHECK)))
        return r if done else o_uncheck(self, **kw)

    def set_checked(self, checked, **kw):
        done, r = _route(self, kw, lambda: self.page.set_checked(_sel(self), checked, **_fwd(kw, _CHECK)))
        return r if done else o_set_checked(self, checked, **kw)

    def select_option(self, value=None, **kw):
        """Delegate to the humanized page.select_option, like every other method here: one
        implementation behind the page method is what stops the two entry points drifting apart
        (which is how page.select_option once stayed untrusted while the locator call was not)."""
        done, r = _route(self, kw, lambda: self.page.select_option(_sel(self), value, **_fwd(kw, _SELECT)))
        return r if done else o_select_option(self, value, **kw)

    def drag_to(self, target, **kw):
        if _on(self):
            try:
                page = self.page
                sb = self.bounding_box()
                tb = target.bounding_box()
                if sb and tb:
                    sx, sy = sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2
                    tx, ty = tb["x"] + tb["width"] / 2, tb["y"] + tb["height"] / 2
                    # Human endpoint dynamics (the two worst slider tells): grab hesitation AFTER
                    # pressing + a settle dwell BEFORE releasing, from the page's motor persona.
                    persona = getattr(page, "_clearcote_persona", None)
                    grab_ms, release_ms = drag_dwell(persona) if persona else (_rand(130, 360), _rand(90, 230))
                    page.mouse.move(sx, sy)
                    time.sleep(_rand(100, 200) / 1000.0)
                    page.mouse.down()                     # native -> button held across the glide
                    time.sleep(grab_ms / 1000.0)          # grab hesitation
                    held_glide = getattr(page, "_clearcote_held_glide", None)
                    if held_glide:
                        held_glide(tx, ty)                # humanized held-button drag + seating jiggle (settle)
                    else:
                        page.mouse.move(tx, ty)
                    time.sleep(release_ms / 1000.0)       # pre-release settle before letting go
                    page.mouse.up()
                    return None
            except Exception:  # noqa: BLE001
                pass
        return o_drag_to(self, target, **kw)

    Locator.fill = fill
    Locator.click = click
    Locator.type = type_
    Locator.dblclick = dblclick
    Locator.hover = hover
    Locator.press = press
    Locator.press_sequentially = press_sequentially
    Locator.clear = clear
    Locator.tap = tap
    Locator.check = check
    Locator.uncheck = uncheck
    Locator.drag_to = drag_to
    Locator.select_option = select_option
    if o_set_checked is not None:
        Locator.set_checked = set_checked


def install_humanize_on_context(context, humanize=False, show_cursor=False, browser=None, seed=None):
    """Install humanize/show_cursor on a single context (used for launch_persistent_context)."""
    if not humanize and not show_cursor:
        return
    b = browser or context.browser
    if show_cursor:
        try:
            context.add_init_script(CURSOR_OVERLAY)
        except Exception:  # noqa: BLE001
            pass
    if b is not None:
        context.on("page", lambda p: attach_humanize(b, p, humanize, show_cursor, seed))
        for p in context.pages:
            attach_humanize(b, p, humanize, show_cursor, seed)


def install_humanize(browser, humanize=False, show_cursor=False, seed=None):
    """Install humanize/show_cursor on a browser: wrap new_page/new_context so every page is covered.

    ``seed`` (the fingerprint seed) selects the stable per-identity motor persona."""
    if not humanize and not show_cursor:
        return
    orig_new_page = browser.new_page
    orig_new_context = browser.new_context

    def new_page(**kw):
        page = orig_new_page(**kw)
        attach_humanize(browser, page, humanize, show_cursor, seed)
        return page

    def new_context(**kw):
        ctx = orig_new_context(**kw)
        install_humanize_on_context(ctx, humanize, show_cursor, browser, seed)
        return ctx

    browser.new_page = new_page
    browser.new_context = new_context
