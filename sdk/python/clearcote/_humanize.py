"""Humanized input + cursor visualization (Python).

``humanize=True`` installs ONE consistent human-input standard that covers moving, clicking,
dragging, scrolling and typing — dispatched as native trusted input (isTrusted=True,
navigator.webdriver stays False). It works whether you drive the page directly or via locators:

  * page level   — page.click / page.hover / page.dblclick / page.type / page.fill / page.press,
                   page.mouse.move / mouse.click / mouse.wheel and held-button drags
                   (mouse.down → mouse.move → mouse.up), page.keyboard.type
  * locator level — locator.click / type / fill / hover / dblclick / press /
                    press_sequentially / clear / check / uncheck / tap / drag_to
  * press and hold — page.press_and_hold(selector) / locator.press_and_hold() (_hold.py)
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

import functools
import random
import re
import sys
import time

import os

from ._forms import (
    DEFAULT_TYPO_RATE, NEARBY_LETTERS, classify_field, datetime_equal, engine_path_ok, fit_maxlength,
    formatted_equal, mask_kept, match_option, normalize_click_options, numeric_equal, plan_datetime, plan_select,
    plan_typo, pre_press_ms, range_attrs,
    range_fraction, range_nudge, range_point, range_sanitize, range_value_ok, segment_nodes_from_tree,
    typo_eligible, typos_allowed,
)
from ._isolated import (
    ACTIVE_FIELD as ISO_ACTIVE_FIELD, ACTIVE_VALUE as ISO_ACTIVE_VALUE, COVER_CHECK_MS, COVER_RETRY_MS,
    CSS_BOX as ISO_CSS_BOX, DESCRIBE as ISO_DESCRIBE, FORM_CONTROL, HIT_IN_BOX as ISO_HIT_IN_BOX,
    FRAMES as ISO_FRAMES, LISTBOX_ROW as ISO_LISTBOX_ROW, SCROLL_BY as ISO_SCROLL_BY,
    PLATFORM as ISO_PLATFORM, VIEWPORT as ISO_VIEWPORT, world_for,
)
from ._motion import (
    WHEEL_NOTCH_PX, make_persona, plan_move, drag_dwell, click_hold, key_dwell, click_point, plan_ambient,
    platform_from_navigator, rollover_rate, wheel_notches,
)
from ._hold import press_and_hold
from . import _motion as _motion_mod
from ._keys import (
    BLANK_POINT, FOCUSABLE, SEARCH_SLIP_RATE, SELECTION_STATE, TAB_DISTANCE, WORD_FIX_RATE, flight_ms,
    modifier_lag_ms, modifier_lead_ms, plan_gap, plan_repeat, split_chord, submit_pause_ms, tab_pause_ms,
    typing_traits,
)
from ._pacing import keep_in_view as _keep_in_view, trim_to as _trim_ambient, walk as _paced_walk

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


# --------------------------------------------------------------- Playwright's error wording
# Playwright words every error after the API the caller used: "Page.click: Timeout 800ms exceeded.",
# "Locator.check: Error: Not a checkbox or radio button". A humanized call raised the error of whatever
# Playwright call it made inside (Locator.wait_for, Locator.is_checked; verifier B, SC 210) -- the
# same type, but a message a caller matching on it would not recognise. Pre-existing in 0.48.0.
_API_PREFIX = re.compile(r"^[A-Za-z_]+\.[A-Za-z_]+: ")


def as_api_error(err, api):
    """``err`` worded as Playwright words an error of ``api`` ("Page.click", "Locator.set_checked"):
    the same class, the inner call's "Locator.wait_for: " prefix replaced (or, for an error the SDK
    raised itself -- a trial that never passed, a checkbox that would not change --, added). Anything
    that is not a Playwright error is returned as it is."""
    try:
        from playwright._impl._errors import Error as PlaywrightImplError
    except Exception:  # noqa: BLE001
        return err
    if not isinstance(err, PlaywrightImplError):
        return err
    msg = err.message if isinstance(getattr(err, "message", None), str) else str(err)
    m = _API_PREFIX.match(msg)
    want = api + ": " + (msg[m.end():] if m else msg)
    if want == msg:
        return err
    try:
        new = type(err)(want)
        for k in ("_name", "_stack", "_details", "_log"):
            if hasattr(err, k):
                setattr(new, k, getattr(err, k))
    except Exception:  # noqa: BLE001
        return err
    return new


def api_errors(api, fn):
    """``fn``, with every Playwright error it raises worded as ``api``'s (as_api_error)."""
    @functools.wraps(fn)
    def call(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            n = as_api_error(e, api)
            if n is e:
                raise
            raise n.with_traceback(e.__traceback__) from None
    return call


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


# The engine's trajectory command, new name first. r34 renamed it to a neutral name ("humanizedClick" is
# a known tool's vocabulary). The served /json/protocol still lists "humanizedClick" (measured on r38,
# SC 210); r40 keeps "dispatchPointerPath" out of it. The old name still works and is the only one r33
# and older know.
ENGINE_POINTER_PATH = ("Browser.dispatchPointerPath", "Browser.humanizedClick")

# The longest an engine walk waits for the page to draw (ISO_FRAMES): measured 111-812 ms on a fresh
# headless browser's first page (r40, SC 216). A page that never draws (a hidden tab) walks after it.
FRAMES_WAIT_MS = 1500


def _cdp_method_missing(err):
    """True for CDP's "method not found" (-32601), which Playwright reports as "'X' wasn't found"."""
    m = str(err)
    return "wasn't found" in m or "-32601" in m or "method not found" in m.lower()


def engine_path_memo(page):
    """Which engine pointer-path name this page's BROWSER answers, shared by every page on it: a dict
    whose "method" is the name that worked, False when the engine knows neither, absent until asked.
    The engine is one per browser connection, so its answer is too: kept per page, every new page
    paid the failed Browser.dispatchPointerPath probe (-32601) again on an engine older than r34.
    Kept on the Browser (or, with no Browser handle -- a persistent context --, on the context)."""
    ctx = getattr(page, "context", None)
    owner = getattr(ctx, "browser", None) or ctx
    if owner is None:
        return {}
    owner = getattr(owner, "_impl_obj", owner)   # the same object behind every API wrapper
    memo = getattr(owner, "_cc_engine_path", None)
    if memo is None:
        memo = {}
        try:
            owner._cc_engine_path = memo
        except Exception:  # noqa: BLE001
            pass
    return memo


def default_timeout_ms(page):
    """The page's default action timeout (set_default_timeout, else the context's, else 30 s)."""
    try:
        return float(page._impl_obj._timeout_settings.timeout())
    except Exception:  # noqa: BLE001
        return 30000.0


def trial_timeout_error(selector, ms, why):
    """A Playwright TimeoutError for a trial whose checks never passed, worded like Playwright's."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    return PlaywrightTimeoutError("Timeout %dms exceeded.\nCall log:\n  - waiting for locator(%r)\n  - %s"
                                  % (ms, selector, why))


def _engine_clicks_mode():
    """Which clicks the engine pointer path (Browser.dispatchPointerPath) may walk, from
    CLEARCOTE_HUMANIZE_ENGINE_CLICKS: "off" (also 0/false/no) never; "all" (also "selector") coordinate
    clicks AND targeted page.click(selector) / locator.click() clicks; anything else (the default)
    coordinate clicks only, as before.

    Targeted clicks stay on the SDK planner by default (SC 210 lane S, measured on r38): the engine
    presses 9 ms after its last move (the planner's press waits 40-130 ms), and it walks from its own
    last position, which an SDK glide does not update."""
    v = (os.environ.get("CLEARCOTE_HUMANIZE_ENGINE_CLICKS") or "").strip().lower()
    if v in ("0", "off", "false", "no", "none"):
        return "off"
    if v in ("all", "selector", "selectors"):
        return "all"
    return "coords"


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

    # The typing identity (WPM, bigram factors, Tab preference, typo rate, pauses), from a salted side
    # stream of the persona seed (_keys.typing_traits): the persona's own draws are unchanged.
    traits = typing_traits(persona.seed)
    # Keys the CALLER holds through keyboard.down (key -> when it went down): a chord does not press a
    # modifier that is already down, typing does not press Shift under a caller's Shift, and the key's
    # keyup waits out a human hold (hkb_down / hkb_up).
    held_keys = {}

    def _dwell():
        """A keydown->keyup hold: the persona's window when it has one (lane B's key_dwell_window), else
        _motion.key_dwell (45-120 ms, right-skewed)."""
        w = getattr(persona, "key_dwell_window", None)
        if w:
            lo, hi = w
            return lo + min(1.0, abs(random.gauss(0.0, 1.0)) * 0.45) * (hi - lo)
        return key_dwell(persona)

    def _tab_pref():
        v = getattr(persona, "tab_pref", None)
        return float(v) if isinstance(v, (int, float)) else traits.tab_pref

    def _typo_rate():
        """Per-character slip rate: CLEARCOTE_HUMANIZE_TYPO_RATE when set, else the persona's (lane B's
        typo_rate field when present, else the typing identity's 0.5-4 %)."""
        if os.environ.get("CLEARCOTE_HUMANIZE_TYPO_RATE") not in (None, ""):
            return typo_rate()
        lane_b = getattr(_motion_mod, "typo_rate", None)   # lane B's typo_rate(persona) on hz2/timing
        if callable(lane_b):
            try:
                return float(lane_b(persona))
            except Exception:  # noqa: BLE001
                pass
        v = getattr(persona, "typo_rate", None)
        return float(v) if isinstance(v, (int, float)) else traits.typo_rate

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
        mouse.down() stays pressed across the whole path (drag).

        Paced against a WALL CLOCK (_pacing.walk): each sample goes out at its planned time, and
        late samples are skipped (never the last, and no skip hops more than 40 px, or 20 px once
        nothing has been dispatched for 40 ms: _pacing.MAX_JUMP_PX / IDLE_HOP_PX). "Move, then sleep the
        planned gap" added the ~27 ms mouse.move round trip to every 6-8 ms sample: a 1.05 s planned
        glide took 3.5-5.4 s on PRO r38 (SC 209), ambient_motion(1500) 5.7x as long."""
        def mv(sx, sy):
            native_move(sx, sy)
            st["placed"] = True
        vp = _view_bounds()
        _paced_walk(_keep_in_view(steps, *vp) if vp else steps, mv)

    def _view_bounds():
        """The viewport (innerWidth, innerHeight) a planned path is kept inside (_pacing.keep_in_view:
        the planner's overshoot arcs left the window near its edges, verifier B), read in the isolated
        world at most every 2 s; None when it cannot be read."""
        now = time.monotonic()
        c = st.get("vp")
        if c is None or now - c[0] > 2.0:
            try:
                wh = world_for(page).evaluate(ISO_VIEWPORT)
                vp = (float(wh[0]), float(wh[1])) if wh and wh[0] and wh[1] else None
            except Exception:  # noqa: BLE001
                vp = None
            st["vp"] = c = (now, vp)
        return c[1]

    def _glide(x, y, settle=False, target_w=24, held=False):
        """Move to (x, y) as a minimum-jerk sum-of-submovements path (primary ballistic + corrective
        homing) with colored sub-pixel noise and Fitts-scaled duration (see _motion.plan_move).
        Continuous (starts at the tracked position, lands exactly on target)."""
        _dispatch(plan_move(st["pos"], (x, y), persona, target_w=target_w, settle=settle, held=held))
        st["pos"] = (x, y)
        st["typed"] = None   # the hand left the keyboard: no submit / Tab pause owed (_after_typing)

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
    _eng = {"session": None, "tid": None, "ok": True, "method": None, "at": None}

    def _engine_in_sync():
        """The engine walks from ITS OWN last position on this page, not from where an SDK glide left
        the pointer. Measured on r38 (SC 210): an engine click at (300,300), an SDK glide to (800,550),
        then mouse.click(400,200) through the engine -- its first sample was at (300,300), a 559 px
        teleport from where the pointer was. So the engine walks only while nothing else has moved
        the pointer since its own last walk (before its first walk: while nothing has moved it)."""
        if _eng["at"] is None:
            return not st["placed"]
        return st["pos"] == _eng["at"]

    def _engine_glide(x, y, no_click):
        if not _eng["ok"] or not _engine_in_sync():
            return False
        memo = engine_path_memo(page)
        if memo.get("method") is False:   # this browser's engine has neither name: no probe again
            _eng["ok"] = False
            return False
        try:
            if _eng["session"] is None:
                tsession = page.context.new_cdp_session(page)
                _eng["tid"] = tsession.send(
                    "Target.getTargetInfo")["targetInfo"]["targetId"]
                b = getattr(page.context, "browser", None)
                _eng["session"] = (b.new_browser_cdp_session()
                                   if b is not None else tsession)
            # The engine does not wait for the page: what it walks into a page that has not drawn yet is
            # lost (a fresh headless browser's first page, SC 216). Wait for two frames first; free once
            # drawn. Best effort: a wait that fails still walks (the engine route stays on).
            try:
                world_for(page).evaluate(ISO_FRAMES, FRAMES_WAIT_MS, await_promise=True)
            except Exception:  # noqa: BLE001
                pass
            dx, dy = x - st["pos"][0], y - st["pos"][1]
            dist = (dx * dx + dy * dy) ** 0.5
            dur = min(1.10, max(0.28, 0.30 + dist / 1700.0)) * (0.85 + 0.30 * random.random())
            params = {"targetId": _eng["tid"], "x": float(x), "y": float(y),
                      "duration": dur, "noClick": bool(no_click)}
            t_send = time.monotonic()
            method = _eng.get("method") or memo.get("method")
            if method:
                _eng["session"].send(method, params)
            else:
                # New name first; an engine older than r34 answers "wasn't found" (-32601)
                # and gets the old one. Any other error is a real failure (below). The answer is
                # remembered for the whole browser (engine_path_memo), not just this page.
                try:
                    _eng["session"].send(ENGINE_POINTER_PATH[0], params)
                    method = ENGINE_POINTER_PATH[0]
                except Exception as e:  # noqa: BLE001
                    if not _cdp_method_missing(e):
                        raise
                    try:
                        _eng["session"].send(ENGINE_POINTER_PATH[1], params)
                    except Exception as e2:  # noqa: BLE001
                        if _cdp_method_missing(e2):
                            memo["method"] = False
                        raise
                    method = ENGINE_POINTER_PATH[1]
                memo["method"] = method
            _eng["method"] = method
            # The engine runs the trajectory in the BROWSER process and answers this command
            # before it has finished walking it. Without waiting, a following mouse.down() fires
            # at the cursor's stale position -- measured: a move to (110,58) then a press landed
            # at (981,629), on <body> instead of the slider, so every move-then-press sequence
            # (drag, press-and-hold, drag-and-drop) acted on the wrong element.
            # r38 answers only after the walk AND the click (send returned 772 ms after a 600 ms
            # walk, 2 ms after the release, SC 210): wait only for what is left of `dur`, so an
            # engine that answers late does not add a second, idle `dur` after every click.
            time.sleep(max(0.0, dur - (time.monotonic() - t_send)))
            # Keep Playwright's own cursor position in sync so a following
            # mouse.down()/drag presses where the engine left the cursor.
            try:
                native_move(x, y)
            except Exception:  # noqa: BLE001
                pass
            st["pos"] = (x, y)
            st["placed"] = True
            _eng["at"] = st["pos"]
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
        # A move while the button is held is a DRAG leg: plan_drag keeps it on the line between grab
        # and drop (an aimed reach's arc took a slider handle 47 px off a 40 px track, SC 209).
        _glide(x, y, held=st["held"])

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
            # The hand has arrived; it confirms the target before the button goes down. The pin
            # above is itself a pointermove, so the pause comes after it: pressed straight after
            # the move, a drag grab came ~21 ms after the last pointermove (measured on r38). A
            # down() with no move before it (never placed) stays instant: nothing arrived.
            _pre_press()
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

    def _pre_press():
        """The pause between the pointer coming to rest and the button going down (_forms.pre_press_ms:
        40-250 ms, right-skewed, centred per identity)."""
        time.sleep(pre_press_ms(persona.seed) / 1000.0)

    def _press_here(x, y, hold=None):
        """Press and release the left button where the last glide left the pointer, after the
        pre-press pause. Native down/up, so nothing moves: Playwright's mouse.click(x, y) sends one more
        mouseMoved first, and that move -- not the glide -- was the last pointermove the page saw, ~4 ms
        before the press (the typing focus click, measured on r38)."""
        hold = click_hold(persona) if hold is None else hold
        if native_down is None or native_up is None:
            _pre_press()
            native_click(x, y, delay=hold)
            return
        _pre_press()
        t0 = time.monotonic()
        native_down()
        try:
            time.sleep(_hold_rest(hold, t0))
        finally:
            native_up()   # never leave the button down

    def _hold_rest(hold_ms, t0):
        """What is left of a button hold once mouse.down() has returned. The hold runs from when the press
        was SENT: mouse.down() returns only after the page has handled the press, and a <select> opens
        its popup inside that handling, so a hold slept after it reached the page ~30-80 ms longer than
        planned (206 ms median for the open click in the SC 209 lab, planned 60-150 ms)."""
        return max(0.0, hold_ms / 1000.0 - (time.monotonic() - t0))

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
            _pre_press()    # the hand confirms the target before it presses
            n = o["click_count"]
            for i in range(1, n + 1):
                hold = o["delay"] if o["delay"] is not None else click_hold(persona)
                t0 = time.monotonic()
                native_down(button=o["button"], click_count=i)
                try:
                    time.sleep(_hold_rest(hold, t0))
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
        if engine_path_ok(o) and _engine_clicks_mode() != "off" and _engine_glide(x, y, no_click=False):
            return
        _glide(x, y)
        try:
            if native_down is None or native_up is None or (o["modifiers"] and native_kb_down is None):
                _pre_press()
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
            # plan_ambient overruns its budget (whole excursions, rests drawn at 0.9-1.4x a frame):
            # cut the plan at a slow sample near ms so ambient_motion(ms) lasts about ms.
            steps = _trim_ambient(plan_ambient(st["pos"], {"width": vp["width"], "height": vp["height"]},
                                               persona, ms), ms)
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
    native_kb_insert = getattr(kb, "insert_text", None)   # a bulk fill's paste (Input.insertText)
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
            native_kb_press(ch, delay=_dwell())
            return
        except Exception:  # noqa: BLE001
            pass
        # Outside the catch: in strict mode lane A's report raises, and that must reach the caller.
        _fallback("Keyboard.press", "insertText: a character keyboard.press cannot map")
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

    def _flight_s(prev=None, ch=None):
        """One keyup->keydown flight of the persona's cadence (_keys.flight_ms), in seconds."""
        return flight_ms(traits, prev, ch, random.random) / 1000.0

    def _backspaces(n):
        for k in range(n):
            native_kb_press("Backspace", delay=_dwell())
            if k < n - 1:
                time.sleep(_rand(70, 150) / 1000.0)

    def _landed(before, now):
        """How many characters arrived since ``before`` (None = cannot tell)."""
        if before is None or now is None:
            return None
        return len(now) - len(before)

    def _restore(target):
        """Bring the focused field back to ``target`` (what it held before a slip) from wherever a word
        delete left it: Backspace to the common prefix, then type the rest. Nothing when unreadable."""
        got = _active_value()
        if got is None or got == target:
            return
        k = 0
        while k < min(len(got), len(target)) and got[k] == target[k]:
            k += 1
        if len(got) - k > 64 or len(target) - k > 64:
            return
        if len(got) > k:
            _backspaces(len(got) - k)
            time.sleep(_rand(80, 200) / 1000.0)
        prev = None
        for c in target[k:]:
            if prev is not None:
                time.sleep(_flight_s(prev, c))
            _emit_key(c)
            prev = c

    def _word_delete():
        """The word-delete chord the ENGINE's editing honours: Alt+Backspace on macOS, else Ctrl."""
        _press_key(("Alt" if _host_platform() == "macos" else "Control") + "+Backspace")

    def _do_typo(text, i, tp, leave=False):
        """Make the planned slip at text[i] and correct it. Returns True when it already typed
        text[i] itself (the doubled-key case) -- or left a substitution uncorrected (``leave``: a search
        field) --, so the caller must not type it again.

        Only what actually LANDED is deleted: an input mask or a maxlength can swallow the wrong key,
        and a Backspace pressed for a key that never arrived deletes a real character (the
        letter-typo-in-a-numeric-mask failure). Measured before and after the slip, in the isolated
        world, one read each -- typos are 0.5-4 % of keys, so this costs nearly nothing.

        A substitution noticed inside a word is fixed with the word-delete chord and the word typed
        again in WORD_FIX_RATE of the cases (when the field can be read to put it back exactly)."""
        before = _active_value()
        kind = tp["kind"]
        if kind == "sub":
            _emit_key(tp["wrong"])
            got = _landed(before, _active_value())
            if got is not None and got <= 0:
                return False                     # swallowed: nothing to correct
            if leave:
                return True                      # the slip stays: the wrong key took this place
            typed = 1
            prev = tp["wrong"]
            for c in text[i + 1:i + 1 + tp.get("late", 0)]:   # late notice: a key or two more
                time.sleep(_flight_s(prev, c))
                _emit_key(c)
                prev = c
                typed += 1
            time.sleep(_rand(140, 340) / 1000.0)  # noticing
            ws = max(text.rfind(" ", 0, i), text.rfind("\n", 0, i)) + 1
            if (before is not None and i - ws + typed >= 2 and native_kb_down is not None
                    and random.random() < WORD_FIX_RATE):
                _word_delete()
                time.sleep(_rand(120, 260) / 1000.0)
                _restore(before)
                time.sleep(_rand(80, 200) / 1000.0)
                return False
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
            time.sleep(_flight_s(text[i + 1], text[i]))
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

    def _human_type_text(text, field=None, typos=True, floor_ms=0.0, exact=True):
        """Type text key-by-key with human timing. Each char goes through Playwright's native
        keyboard (trusted), so this stays isTrusted=True.

        Cadence (_keys.plan_gap, the same planner in Node and .NET): the persona's WPM sets a base
        flight, scaled by how the hand travels between the two keys (same finger 1.5-1.9x, same hand
        1.1-1.3x, the other hand 0.85-1.0x, a two-row reach 1.15x) and log-normal noise, plus a pause
        before a new word and after punctuation, the old whitespace pause and a 6 % thinking pause.
        ``floor_ms`` (the caller's `delay`) is the shortest gap between two keys.

        Capitals and shifted symbols are typed the way a person types them: ShiftLeft goes down a
        moment before the key and comes up just after it, held across a run of them. Playwright's
        press("A") alone sent the key with shiftKey=false and no Shift key at all (measured on r28
        with "Ab!") -- input no keyboard produces. Under a Shift the caller holds (keyboard.down) no
        second Shift is pressed.

        Fat-finger typos are planned by _forms.plan_typo at the persona's rate and never happen where a
        slip is a failure rather than a slip (passwords, e-mail, numbers, phone numbers, URLs, one-time
        codes). ``exact`` False (type / press_sequentially / keyboard.type, never fill) lets a
        substitution in a search field stay uncorrected (SEARCH_SLIP_RATE)."""
        n = len(text)
        shift = False
        ext_shift = "Shift" in held_keys
        roll_rate = rollover_rate(persona)
        rate = _typo_rate() if typos else 0.0
        fld = _typing_field(field) if rate > 0 else None
        allow = rate > 0 and typos_allowed(fld)
        search = bool(allow and not exact and fld and (fld.get("type") or "").lower() == "search")
        floor = max(0.0, float(floor_ms or 0.0))
        i = 0
        try:
            while i < n:
                ch = text[i]
                need = _needs_shift(ch) and not ext_shift
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
                        leave = search and tp["kind"] == "sub" and random.random() < SEARCH_SLIP_RATE
                        try:
                            consumed = _do_typo(text, i, tp, leave)
                        except Exception as e:  # noqa: BLE001
                            if _is_strict(e):
                                raise
                # ROLLOVER: the next key goes down before this one comes up, the way people type fast
                # pairs. One press() per character never overlaps (0 of 32 keydowns, measured on
                # r32/r33), a keystroke-dynamics tell. Unshifted single-key pairs only; never under a
                # caller's delay floor (the next key would come sooner than asked).
                nxt = text[i + 1] if i + 1 < n else None
                d1 = _dwell()
                if (nxt is not None and not typo and not shift and native_kb_down and native_kb_up
                        and ch in _ROLL_KEYS and nxt in _ROLL_KEYS and ch != nxt and d1 - 8 >= 12
                        and floor <= 0 and random.random() < roll_rate):
                    overlap = _rand(12, min(55, d1 - 8))
                    try:
                        native_kb_down(ch)
                        time.sleep((d1 - overlap) / 1000.0)
                        native_kb_down(nxt)
                        time.sleep(overlap / 1000.0)
                        native_kb_up(ch)
                        time.sleep(max(5.0, _dwell() - overlap) / 1000.0)
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
                    except Exception as e:  # noqa: BLE001
                        if _is_strict(e):
                            raise
                        break
                if shift and (i == n - 1 or not _needs_shift(text[i + 1])):
                    time.sleep(_shift_lag_s())
                    native_kb_up("Shift")
                    shift = False
                if i < n - 1:
                    time.sleep(max(floor, plan_gap(traits, ch, text[i + 1], random.random,
                                         getattr(persona, "tempo", 1.0), getattr(persona, "pause_scale", 1.0))) / 1000.0)
                i += 1
        finally:
            if shift:  # never leave Shift held for the caller's next keystroke
                try:
                    native_kb_up("Shift")
                except Exception:  # noqa: BLE001
                    pass
            if n:
                st["typed"] = time.monotonic()   # a following Enter / Tab waits (_after_typing)

    def _after_typing(key):
        """The pause a person makes between the last typed character and Enter (250-900 ms) or Tab
        (120-400 ms), centred per persona, less whatever the caller already spent. Once per typed run;
        a pointer action in between cancels it (_glide clears st["typed"])."""
        t = st.get("typed")
        st["typed"] = None
        if t is None:
            return
        mods, k = split_chord(key)
        if k == "Enter" and not mods:
            ms = submit_pause_ms(traits, random.random)
        elif k == "Tab" and set(mods) <= {"Shift"}:
            ms = tab_pause_ms(traits, random.random)
        else:
            return
        rest = ms / 1000.0 - (time.monotonic() - t)
        if rest > 0:
            time.sleep(rest)

    def _hold_key(k, delay=None, repeat=1):
        """One key: pressed with the persona's hold (or the caller's ``delay``); with ``repeat`` > 1 HELD
        the way an OS auto-repeats it -- one keydown, the repeat delay (250-500 ms), then a keydown with
        repeat=true every 25-40 ms (Playwright sends a key that is already down as an autoRepeat
        keydown), ``repeat`` keydowns in all, then one keyup."""
        repeat = max(1, int(repeat or 1))
        if repeat == 1 or native_kb_down is None or native_kb_up is None:
            for j in range(repeat):
                native_kb_press(k, delay=delay if delay and delay > 0 else _dwell())
                if j < repeat - 1:
                    time.sleep(_rand(60, 150) / 1000.0)
            return
        rp = plan_repeat(random.random)
        native_kb_down(k)
        try:
            time.sleep(rp.first_ms / 1000.0)
            for j in range(repeat - 1):
                if j:
                    time.sleep(rp.gap_ms / 1000.0)
                native_kb_down(k)
            time.sleep(rp.release_ms / 1000.0)
        finally:
            native_kb_up(k)   # never leave the key held

    def _press_key(key, delay=None, repeat=1):
        """keyboard.press, humanized: a chord ("Control+a", "ControlOrMeta+Shift+ArrowLeft") is split
        into its modifiers and its key -- each modifier goes down in order, 40-110 ms before the next,
        the key is held, 20-80 ms later the modifiers come up in reverse order. As one Playwright press
        the modifier and the key went down within ~1 ms of each other. A modifier the caller already
        holds (keyboard.down) is not pressed again. Playwright's key names are kept (ControlOrMeta is
        resolved by Playwright, for the engine's platform)."""
        mods, k = split_chord(key)
        mods = [m for m in mods if m not in held_keys]
        if not mods:
            return _hold_key(k, delay, repeat)
        if native_kb_down is None or native_kb_up is None:
            return native_kb_press(key, delay=delay if delay and delay > 0 else _dwell())
        down = []
        try:
            for m in mods:
                native_kb_down(m)
                down.append(m)
                time.sleep(modifier_lead_ms(random.random) / 1000.0)
            _hold_key(k, delay, repeat)
            time.sleep(modifier_lag_ms(random.random) / 1000.0)
        finally:
            for j, m in enumerate(reversed(down)):
                if j:
                    time.sleep(_rand(10, 40) / 1000.0)
                try:
                    native_kb_up(m)   # one failed keyup must not strand the others
                except Exception:  # noqa: BLE001
                    pass

    def hkb_type(text, **kw):
        _pointer_presence()
        _human_type_text(text, floor_ms=kw.get("delay") or 0.0, exact=False)

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
        that. Only the DEFAULT changes, from "no hold at all" to "a human hold". A chord is pressed
        modifier first (_press_key); ``repeat=n`` holds the key through n auto-repeat keydowns; Enter
        or Tab right after typing waits the persona's submit / Tab pause first (_after_typing)."""
        _pointer_presence()
        repeat = kw.pop("repeat", 1)
        _after_typing(key)
        d = kw.get("delay")
        return _press_key(key, d if d and d > 0 else None, repeat)

    def hkb_down(key):
        """keyboard.down, tracked: the chord model and typing honour a modifier the caller holds, and
        hkb_up gives the key at least a persona hold."""
        _pointer_presence()
        if key not in held_keys:
            held_keys[key] = (time.monotonic(), _dwell())
        return native_kb_down(key)

    def hkb_up(key):
        t = held_keys.pop(key, None)
        if t is not None:
            rest = t[1] / 1000.0 - (time.monotonic() - t[0])
            if rest > 0:
                time.sleep(rest)   # a down() then an up() at once: no finger lifts a key in 0 ms
        return native_kb_up(key)

    kb.type = hkb_type
    kb.press = hkb_press
    if native_kb_down is not None and native_kb_up is not None:
        kb.down, kb.up = hkb_down, hkb_up

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
        if box and not (_in_view(box) and _hit_ok(box, selector)):
            # scrollIntoViewIfNeeded also scrolls a scrolling container that clips it, and does
            # nothing when it already shows (then the cover is an overlay: the click's own check)
            box = _rect_box(w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": True}))
        return box

    def _hit_ok(box, sel=None):
        """Does the element filling ``box`` own its centre -- not clipped by a scrolling container,
        not covered? The browser's own hit test, read in the isolated world."""
        return _hit_in_box(box, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, sel) is True

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
        if box and not (_in_view(box) and _hit_ok(box, selector)):
            # Off-screen: scroll the window to it and prove it shows there (hit test), with no new
            # handle. Clipped by a scrolling container, covered, or the window scroll did not bring
            # it out: Playwright's scroll, which also scrolls containers, and a fresh box.
            moved = _scroll_window_to(box) if not _in_view(box) else None
            if moved and _in_view(moved) and _hit_ok(moved, selector):
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
        pt = (float(round(pt[0])), float(round(pt[1])))   # the pixel the hit test answered for
        _glide(pt[0], pt[1])
        _press_here(pt[0], pt[1])
        return True

    def _key(k, n=1, gap=(60, 150)):
        for j in range(n):
            native_kb_press(k, delay=_dwell())
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
            native_kb_press("a", delay=_dwell())
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

    def _tab_distance(info, selector=None):
        """Where the control ``info`` (described, with its hit-test node) sits in the page's Tab order
        relative to the focused element (_keys.TAB_DISTANCE, read in the isolated world): {dist, fromText,
        focused, activatable, text, ...}, or None when either is outside the order or it cannot be read."""
        if not info or info.get("node") is None or not info.get("rect"):
            return None
        x, y, w, h = info["rect"]
        arg = {"x": x, "y": y, "width": w, "height": h}
        if isinstance(selector, str) and selector:
            arg["sel"] = selector
        try:
            return world_for(page).call_on_node(info["node"], TAB_DISTANCE, arg)
        except Exception:  # noqa: BLE001
            return None

    def _tab_steps(d):
        """Press Tab ``d`` times (Shift+Tab when ``d`` < 0), each with a hold, the first after the
        persona's Tab pause when it follows typing, the rest 110-260 ms apart (the eye follows the
        focus ring)."""
        key = "Tab" if d > 0 else "Shift+Tab"
        for j in range(abs(d)):
            if j == 0:
                _after_typing(key)
            else:
                time.sleep(_rand(110, 260) / 1000.0)
            _press_key(key)

    def _tab_into(loc, info, selector=None, need_empty=False, force=False):
        """Tab into the control ``info`` instead of clicking it, the way a person moves between
        adjacent fields: when focus sits in a TEXT field 1-3 Tab stops before it (or 1 after: Shift+Tab),
        in the persona's share of cases (60-85 %, _keys.typing_traits.tab_pref). ``force``: any focused
        element, up to 3 stops either way, no roll (a button a press goes to). ``need_empty``: only an
        empty field -- Tab selects a field's text, and type() appends where a click leaves the caret.
        True once focus is verified there; False (nothing pressed, or focus went elsewhere) = click."""
        if native_kb_press is None or (need_empty and info and info.get("value")):
            return False
        td = _tab_distance(info, selector)
        if not td or td.get("dist") is None or td.get("focused"):
            return False
        d = td["dist"]
        if force:
            if not (1 <= abs(d) <= 3):
                return False
        else:
            if not td.get("fromText") or not (1 <= d <= 3 or d == -1) or random.random() >= _tab_pref():
                return False
        _tab_steps(d)
        time.sleep(_rand(40, 120) / 1000.0)
        return _focused(loc)

    def _click_into(selector, timeout, tab=True):
        """Make ``selector`` the focus the way a person does -- Tab from the field before it (_tab_into)
        or a click --, unless it already is. Only text-entry controls are clicked; for anything else, and
        when the click did not actually put the caret there (an overlay took it), the caller goes
        native. Tab only into an EMPTY field: type() appends at the caret, and Tab selects the text."""
        loc = page.locator(selector).first
        if _focused(loc):
            return
        te = _text_entry(loc, timeout, selector)
        if te is None:
            raise _Native()
        if tab and _tab_into(loc, te[1], selector, need_empty=True):
            return
        if not _focus_click(te[0], _proven_point(*te), loc, selector) or not _focused(loc):
            raise _Native()
        time.sleep(_rand(40, 120) / 1000.0)

    def _focus_target(selector, timeout):
        """Give ``selector`` -- any focusable element, a button or a link as well as a field -- the
        keyboard focus the way a person does. Tab when it is 1-3 stops from the focused element (always
        for something a click would ACTIVATE: a button, a link, a checkbox); otherwise a humanized click
        (glide + press) for what a click only focuses (a field, a tabindex container). True when focus
        is verified there. False for an activatable target out of Tab reach: the pointer has glided
        onto it and the caller gives it the focus natively (a reported fallback), never the 0 ms
        native press."""
        loc = page.locator(selector).first
        if _focused(loc):
            return True
        box = _box_of(loc, timeout, selector=selector)
        info = _describe(box, FOCUSABLE) if box else None
        if info is None:
            raise _Native()
        td = _tab_distance(info, selector)
        act = td is None or bool(td.get("activatable"))   # unknown: never risk an activating click
        if td and td.get("dist") is not None and 1 <= abs(td["dist"]) <= 3:
            if act or random.random() < _tab_pref():
                if _tab_into(loc, info, selector, force=True):
                    return True
        if not act and not info.get("disabled"):
            if _focus_click(box, _proven_point(box, info), loc, selector) and _focused(loc):
                time.sleep(_rand(40, 120) / 1000.0)
                return True
            raise _Native()
        x, y = click_point(box, st["pos"], persona)
        _glide(float(round(x)), float(round(y)))   # the hand goes to it; the focus has to be given
        return False

    def htype(selector, text, **options):
        try:
            _click_into(selector, options.get("timeout"))
            _human_type_text(text, floor_ms=options.get("delay") or 0.0, exact=False)
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
        is_ce = bool(info and info.get("ce")) or info is None and (loc.get_attribute("contenteditable") or "false") != "false"
        # What the field can hold: a maxlength keeps the first N characters (what Playwright's
        # insertText leaves too), and a person types only those -- the field stops taking keys.
        if is_ce:
            want, limit = value, (None, None, None)
        elif info is not None:
            limit = (info.get("maxlength"), info.get("tag"), info.get("type"))
            want = fit_maxlength(value, *limit)
        else:
            limit = (loc.get_attribute("maxlength"), _classify_tag(loc), loc.get_attribute("type") or "")
            want = fit_maxlength(value, *limit)
        # Bulk values (over 200 characters) are still inserted at once -- typing thousands of keys
        # would crawl -- but the way a person pastes: glide to the field, click into it, then insert.
        bulk = len(want) > 200   # what the field can hold: a 300-character value cut to 5 is typed
        if bulk and native_kb_insert is None:
            return False
        # From the field before it, a person Tabs (persona share, focus verified); else a click.
        if not (info is not None and _tab_into(loc, info, selector)):
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

        def put(text, typos=True):
            if bulk:
                time.sleep(_rand(120, 320) / 1000.0)   # the paste shortcut, after the click
                native_kb_insert(text)
            else:
                _human_type_text(text, field=info, typos=typos)

        if is_ce:
            cur = info.get("value") if info else None
            if cur:
                _clear_focused()
            put(want)
            return True   # contenteditable: Playwright's fill sets text the same way; no value to read
        cur = read()
        if cur is None:
            return False
        if cur:  # a person does not select-all an empty field
            _clear_focused(read)
        put(want)
        got = read()
        if got is None:
            return False
        if got == want or formatted_equal(got, want) or mask_kept(got, want, *limit):
            # exact; a mask that only re-punctuated what was typed (a phone format); or one that
            # re-punctuated it and then stopped taking keys (MM/YY on maxlength 5): no wipe and retype
            return True
        if bulk:
            return False   # an inserted value that did not land: Playwright's fill, not a second paste
        # Wrong value (an autocompleter interfered, a key was lost): one clean retry without slips,
        # then give the field to Playwright -- a correct value beats pretty input.
        _clear_focused(read)
        put(want, typos=False)
        got = read()
        return got is not None and (got == want or formatted_equal(got, want) or mask_kept(got, want, *limit))

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
            _pre_press()
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
            _press_here(tx, ty)
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
        _press_here(px, py)
        time.sleep(_rand(150, 350) / 1000.0)
        for a in plan:
            if a[0] == "type":
                # The same typing model as text (persona dwell, gaussian gaps, thinking pauses,
                # rollover): the old flat 60-150 ms gap had a CV of 0.24 and no rollover at all. A
                # segment moves on by itself after its last digit, and keys go down in order, so a
                # rolled-over digit still lands in the right field (checked on r38, all five types).
                # No typos: a slip in a date is a wrong date.
                _human_type_text(a[1], typos=False)
            else:
                _key(a[1], a[2] if len(a) > 2 else 1, gap=(70, 170))
            time.sleep(_rand(60, 220) / 1000.0)   # between fields: the eye checks what it typed
        return datetime_equal(kind, loc.input_value(), value)

    def _classify_tag(loc):
        """'textarea' | 'input' | None, through Playwright's utility-world queries."""
        for tag in ("textarea", "input"):
            if loc.locator("xpath=self::%s" % tag).count():
                return tag
        return None

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

    native_page_focus = getattr(page, "focus", None)

    def _fallback(api, reason):
        """A humanized route handed this call to Playwright: told to lane A's on_fallback hook when the
        page carries one (page._clearcote_on_fallback), else nothing (as before)."""
        report = _lane_a_report()
        if report is not None:
            return report(page, api, reason)   # not caught: strict mode raises ClearcoteStrictError
        cb = getattr(page, "_clearcote_on_fallback", None)
        if callable(cb):
            try:
                cb({"api": api, "reason": reason})
            except Exception:  # noqa: BLE001
                pass

    def _focus_for_press(selector, timeout):
        """Give ``selector`` the focus before a key goes to it. A text field: Tab from the field before
        it (only when empty: Tab selects the text) or a click into it. Anything else -- a button, a
        link, a tabindex container -- through _focus_target (Tab within 3 stops, or a click that only
        focuses); an activatable target out of Tab reach gets the pointer over it and the focus from
        Playwright (reported), and the key is still pressed with a human hold."""
        loc = page.locator(selector).first
        if _focused(loc):
            return
        if not loc.is_visible():
            raise _Native()   # Playwright waits for it
        box = _box_of(loc, timeout, selector=selector)
        info = _describe(box)
        if info and classify_field(info["tag"], info["type"], info["ce"]) == "text":
            if _tab_into(loc, info, selector, need_empty=True):
                return
            if not _focus_click(box, _proven_point(box, info), loc, selector) or not _focused(loc):
                raise _Native()
            time.sleep(_rand(40, 120) / 1000.0)
            return
        if _focus_target(selector, timeout):
            return
        if native_page_focus is None:
            raise _Native()
        _fallback("Page.press", "focus: an activatable target out of Tab reach")
        native_page_focus(selector, **({"timeout": timeout} if timeout is not None else {}))
        time.sleep(_rand(40, 120) / 1000.0)

    def hpress(selector, key, **options):
        """page.press (and Locator.press): the target gets the focus the way a person gives it
        (_focus_for_press: never Playwright's focus-and-press in 0 ms), then the key is pressed through
        the keyboard model -- a chord modifier first, the persona's hold (or the caller's `delay`),
        ``repeat=n`` as OS auto-repeat, and the submit / Tab pause after typing."""
        repeat = options.pop("repeat", 1)
        try:
            _focus_for_press(selector, options.get("timeout"))
            _after_typing(key)
            d = options.get("delay")
            _press_key(key, d if d and d > 0 else None, repeat)
            return None
        except _Native:
            pass
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        _fallback("Page.press", "native press")
        r = None
        for _ in range(max(1, int(repeat or 1))):
            r = native_page_press(selector, key, **options)
        return r

    def hfocus(selector, **options):
        """page.focus / Locator.focus: Tab or a click (_focus_target), verified; Playwright's focus for
        an activatable target out of Tab reach (reported) and for anything the humane route cannot do."""
        try:
            if _focus_target(selector, options.get("timeout")):
                return None
            _fallback("Page.focus", "an activatable target out of Tab reach")
        except _Native:
            _fallback("Page.focus", "native focus")
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        return native_page_focus(selector, **options)

    def _blur(selector, options):
        """Locator.blur, humanized: a focused element is left the way the persona leaves fields -- Tab
        to the next stop, or a click on a blank point beside it (_keys.BLANK_POINT; Tab when there is
        none). True when focus is verified gone; False = Playwright's blur (nothing to blur, or the
        humane route did not take the focus away)."""
        loc = page.locator(selector).first
        if not _focused(loc):
            return False
        if traits.blur_style == "click" and native_down is not None and native_up is not None:
            box = _box_of(loc, options.get("timeout"), selector=selector)
            vw, vh = _viewport()
            pt = world_for(page).evaluate(BLANK_POINT, {"box": box, "vw": vw, "vh": vh}) if box else None
            if isinstance(pt, list) and len(pt) == 2:
                x, y = float(round(pt[0] + random.gauss(0, 3))), float(round(pt[1] + random.gauss(0, 3)))
                _glide(x, y, target_w=60)
                _press_here(x, y)
                time.sleep(_rand(40, 120) / 1000.0)
                if not _focused(loc):
                    return True
        _after_typing("Tab")
        _press_key("Tab")
        time.sleep(_rand(40, 120) / 1000.0)
        return not _focused(loc)

    def _selected_all():
        s = world_for(page).evaluate(SELECTION_STATE)
        return isinstance(s, list) and len(s) == 3 and min(s[0], s[1]) == 0 and max(s[0], s[1]) == s[2]

    def _select_text(selector, options):
        """Locator.select_text, humanized: a text control's text selected the way the persona does it --
        a triple click (an <input>), or the keys: End then Shift+Home (Ctrl+End, Ctrl+Shift+Home in a
        textarea; Cmd+A on macOS, where Home/End do not move the caret). Verified in the isolated world
        (SELECTION_STATE), one select-all retry; False = Playwright's select_text."""
        loc = page.locator(selector).first
        box = _box_of(loc, options.get("timeout"), options.get("force"), selector)
        info = _describe(box) if box else None
        if not info or info.get("disabled") or classify_field(info["tag"], info["type"], info["ce"]) != "text":
            return False
        tag = info["tag"]
        if traits.select_style == "triple" and tag == "input" and native_down is not None and native_up is not None:
            at = _proven_point(box, info)
            x, y = at if at is not None else click_point(box, st["pos"], persona)
            x, y = float(round(x)), float(round(y))
            _glide(x, y)
            _press_at(x, y, normalize_click_options({"click_count": 3}, _platform()))
        else:
            if not _focused(loc):
                if not _focus_click(box, _proven_point(box, info), loc, selector) or not _focused(loc):
                    return False
                time.sleep(_rand(120, 300) / 1000.0)
            if _host_platform() == "macos":
                _press_key("ControlOrMeta+a")
            elif tag == "input":
                _press_key("End")
                time.sleep(_rand(80, 200) / 1000.0)
                _press_key("Shift+Home")
            else:
                _press_key("ControlOrMeta+End")
                time.sleep(_rand(80, 200) / 1000.0)
                _press_key("ControlOrMeta+Shift+Home")
        time.sleep(_rand(60, 160) / 1000.0)
        if _selected_all():
            return True
        _select_all()
        time.sleep(_rand(60, 160) / 1000.0)
        return _selected_all()

    def _guarded(api, fn, native):
        """Run the humane ``fn`` (True = done); Playwright's ``native`` when it declines or fails
        (a timeout propagates)."""
        def call(selector, **options):
            try:
                if fn(selector, options):
                    return None
            except _Native:
                pass
            except Exception as e:  # noqa: BLE001
                if _is_timeout(e):
                    raise
            _fallback(api, "native")
            return native(selector, **options)
        return call

    page._clearcote_blur = _guarded(
        "Locator.blur", _blur, lambda s, **o: _LOCATOR_NATIVE["blur"](page.locator(s).first, **o))
    page._clearcote_select_text = _guarded(
        "Locator.select_text", _select_text, lambda s, **o: _LOCATOR_NATIVE["select_text"](page.locator(s).first, **o))

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
            native_kb_press(ch, delay=_dwell())
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
        crosses a long list: one press, the keyboard's repeat delay, then auto-repeat at its rate
        (_hold_key, _keys.plan_repeat). Pressed one by one at a human ~180 ms each, 370 rows took 68 s
        (verifier B). The open popup takes these keys, so the page sees none of them, only how long the
        pick took."""
        if n < 8:
            _key(key, n)
            return
        _hold_key(key, None, n)

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
                _press_here(px, py)
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
        the state is read (a non-checkbox raises, as in Playwright) and, when a click would be needed,
        its checks run here (_trial) -- not Playwright's trial, which moves the pointer and presses."""
        loc = page.locator(selector).first
        if options.get("trial"):
            if loc.is_checked(timeout=options.get("timeout")) == checked:
                return None
            return _trial(selector, options, no_click=False)
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
        raise PlaywrightError("Clicking the checkbox did not change its state")   # worded per API (api_errors)

    page.type = api_errors("Page.type", htype)
    page.fill = api_errors("Page.fill", hfill)
    page.dblclick = api_errors("Page.dblclick", hdblclick)
    page.press = api_errors("Page.press", hpress)
    if native_page_focus is not None:
        page.focus = api_errors("Page.focus", hfocus)
    if native_page_select_option is not None:
        page.select_option = api_errors("Page.select_option", hselect_option)
    for name, want in (("check", True), ("uncheck", False)):
        orig_c = getattr(page, name, None)
        if orig_c is not None:
            setattr(page, name, api_errors("Page." + name, (lambda o, w: lambda selector, **opts: _set_checked(
                o, selector, w, opts))(orig_c, want)))
    orig_sc = getattr(page, "set_checked", None)
    if orig_sc is not None:
        page.set_checked = api_errors("Page.set_checked", lambda selector, checked, **opts: _set_checked(
            lambda s, **o: orig_sc(s, checked, **o), selector, bool(checked), opts))

    def _hit_in_box(box, x, y, sel=None):
        """Does a press at viewport (x, y) reach the element filling ``box``? True / False, or None
        when it cannot be told here (CDP is unavailable). A point inside a child frame is answered
        by the <iframe> element that holds it (IsolatedWorld.node_at)."""
        w = world_for(page)
        nid = w.node_at(x, y)
        if nid is None:
            return None
        b = {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}
        if sel:
            b["sel"] = sel   # a plain-CSS selector: whether the hit is THAT element (see HIT_IN_BOX)
        res = w.call_on_node(nid, ISO_HIT_IN_BOX, b)
        return None if res is None else bool(res)

    def _reaches_point(box, x, y, sel=None):
        """The covered-point check, through the browser's own hit test read in the isolated world:
        nothing is dispatched and the pointer does not move. It replaced a Playwright TRIAL action,
        which moves the REAL pointer onto the element and back and performs a press with its events
        blocked: verifier B saw, before every humanized click since 0.46.0, a mousemove inside the
        target, a 450-500 px jump back, then the glide, and a pointerdown/pointerup/click (left, even
        for a right click) that window capture listeners could see. A covered point is looked at
        again for COVER_CHECK_MS (an overlay fading out). A point inside a child frame is answered by
        the <iframe> element that holds it (IsolatedWorld.node_at), so no Playwright trial runs for it
        either. False when covered; None when the hit test cannot run (no CDP): the caller goes native."""
        deadline = time.monotonic() + COVER_CHECK_MS / 1000.0
        hit = _hit_in_box(box, x, y, sel)
        while hit is False and time.monotonic() < deadline:
            time.sleep(0.08)
            hit = _hit_in_box(box, x, y, sel)
        return hit

    def _position_point(box, o):
        """The viewport point of Playwright's `position` (relative to the PADDING box: the border
        widths are added)."""
        info = _describe(box, None)
        bl, bt = (info.get("bl", 0), info.get("bt", 0)) if info else (0, 0)
        return box["x"] + bl + o["position"]["x"], box["y"] + bt + o["position"]["y"]

    def _trial(selector, options, no_click):
        """`trial=True`, answered by the SDK: Playwright's actionability checks -- visible, enabled
        (not for hover), stable, and the click point receives the press (the browser's own hit test,
        read in the isolated world) -- retried until the caller's timeout (or the page default), then
        a Playwright TimeoutError. NO pointer move and NO press: Playwright's own trial moves the real
        pointer onto the target and dispatches pointerdown/pointerup/click that page listeners
        registered before Playwright's blocker see (SC 209: 6/6). The point checked is Playwright's:
        `position`, else the centre. Scrolling the element into view is part of the checks, as in
        Playwright. With `force` the checks are skipped: only the element must exist."""
        o = normalize_click_options(options, _platform())
        timeout = options.get("timeout")
        ms = float(timeout) if timeout is not None else default_timeout_ms(page)
        end = time.monotonic() + ms / 1000.0 if ms > 0 else None
        loc = page.locator(selector).first

        def left():   # what is left of the timeout for a Playwright wait (0 there: no limit)
            return 0 if end is None else max(1, int((end - time.monotonic()) * 1000.0))

        if o["force"]:
            loc.wait_for(state="attached", timeout=left())
            return None
        while True:
            why = "element is not visible"
            box = _box_of(loc, left(), selector=selector)   # waits for visible; raises Playwright's timeout
            if box:
                if not no_click and not loc.is_enabled():
                    why = "element is not enabled"
                else:
                    if o["position"]:
                        x, y = _position_point(box, o)
                    else:
                        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                    time.sleep(0.05)   # stable: 50 ms on, the element still fills `box` (HIT_IN_BOX)
                    if _hit_in_box(box, x, y, selector) is not False:
                        return None    # reached (None: the hit test cannot run here -- nothing to add)
                    why = ("element is not stable, or another element would receive the pointer at "
                           "(%d, %d)" % (x, y))
            if end is not None and time.monotonic() >= end:
                raise trial_timeout_error(selector, ms, why)
            time.sleep(0.1 if end is None else max(0.0, min(0.1, end - time.monotonic())))

    def _targeted_click(orig, selector, options, no_click, native_opts):
        """The humanized form of page.click / dblclick / hover (and the click inside check/uncheck).

        Every Playwright option is honoured: `button`, `click_count`, `delay` (the hold) and
        `modifiers` shape the press; `position` replaces the persona's point; `force` skips the
        hit test; `trial` performs NO action: its checks run here (_trial), with no pointer move and
        no press. The old wrapper forwarded only `timeout`, so a right click became a left click and
        a trial clicked."""
        o = normalize_click_options(options, _platform())

        aim = []   # where Playwright's own click lands, once the box is known (_native)

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
                # ... and away from where the engine walk left it: the engine path must not walk
                # from its own stale point (_engine_in_sync; verifier A #6). The next SDK glide
                # starts where Playwright pressed: `position`, else the centre of the box.
                _eng["at"] = None
                if aim:
                    st["pos"] = aim[-1]

        if o["trial"]:
            return _trial(selector, options, no_click)
        try:
            timeout = options.get("timeout")   # None: the page's default, as Playwright would use
            loc = page.locator(selector).first
            # Playwright skips actionability with force: no wait then -- a target it would click
            # hidden goes native at once instead of timing out first.
            box = _box_of(loc, timeout, o["force"], selector)
            if not box:
                raise _Native()
            aim.append((box["x"] + box["width"] / 2, box["y"] + box["height"] / 2))
            if not o["force"] and not loc.is_enabled():
                raise _Native()
            if o["position"]:
                x, y = _position_point(box, o)
                aim.append((x, y))
            else:
                x, y = click_point(box, st["pos"], persona)
            # Whole pixels: the hit test below answers for the pixel DOM.getNodeForLocation rounds to,
            # so the press goes to that pixel too. A press at 971.6 next to a badge ending at 972 was
            # proven at 972 and landed on the badge, reported as a success (verifier B, SC 210).
            x, y = float(round(x)), float(round(y))
            if not o["force"] and orig is not None:
                # Stability, then covered-by: 50 ms on, the element must still fill `box` (an
                # animation in flight does not) and own the click point -- no trusted press at a
                # point some overlay owns. Either failing goes native: Playwright waits for it.
                time.sleep(0.05)
                if not _reaches_point(box, x, y, selector):
                    raise _Native()
            if o["modifiers"] and native_kb_down is None:
                raise _Native()
            if (not no_click and not o["force"] and orig is not None and _engine_clicks_mode() == "all"
                    and engine_path_ok(o) and _engine_glide(x, y, no_click=False)):
                # Opt-in (CLEARCOTE_HUMANIZE_ENGINE_CLICKS=all): the engine walks the approach and
                # clicks the point the hit test above proved; plain left single clicks only, and only
                # while the engine knows where the pointer is (_engine_in_sync). Otherwise: planner.
                return None
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
                    _pre_press()
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

    page.click = api_errors("Page.click", _make_targeted(page.click, False))
    page.hover = api_errors("Page.hover", _make_targeted(page.hover, True))
    # Locator.drag_to(trial=True) answers its checks here too (see the Locator patch).
    page._clearcote_trial = _trial

    # press_and_hold (_hold.py) runs on the public page.mouse (so its glide and pre-press pause are
    # these wrappers'), and takes three things from here: the box helper (wait, wheel scroll, hit-test
    # proof), where the pointer is (for the persona's click point), and a move that does NOT glide --
    # the held hand's one-pixel drift, which a planned drag leg would turn into a walk.
    page._clearcote_box_of = lambda loc, timeout, selector=None: _box_of(loc, timeout, False, selector)
    page._clearcote_pointer = lambda: st["pos"]

    def _nudge(x, y):
        native_move(x, y)
        st["pos"] = (x, y)
        st["placed"] = True
    page._clearcote_nudge = _nudge
    page.press_and_hold = api_errors("Page.press_and_hold", lambda selector, duration=None, **kw: press_and_hold(
        page.locator(selector).first, duration, **kw))

    # Marker the Locator-class patch keys off: humanize is active for THIS page.
    page._clearcote_humanized = True
    _patch_locator_class()


def _is_strict(err):
    """Lane A's ClearcoteStrictError (strict mode): never swallowed by a best-effort path."""
    return type(err).__name__ == "ClearcoteStrictError"


def _lane_a_report():
    """Lane A's fallback reporter (_report_fallback(page, api, reason, selector=None)) when this build has it."""
    import importlib
    for mod in ("clearcote._humanize", "clearcote._fallback", "clearcote._options"):
        try:
            fn = getattr(importlib.import_module(mod), "_report_fallback", None)
        except Exception:  # noqa: BLE001
            fn = None
        if callable(fn):
            return fn
    return None


class _Native(Exception):
    """Internal: the humane route cannot apply here; run the native Playwright call instead."""


def _no_touch(loc):
    """Is ``loc``'s page in a context WITHOUT touch (no hasTouch), where Playwright's tap raises? A
    page-like object whose context cannot be read counts as touch-capable (the tap is routed)."""
    try:
        return loc.page.context._impl_obj._options.get("hasTouch") is not True
    except Exception:  # noqa: BLE001
        return False


def _is_timeout(err):
    """A Playwright timeout: the element never became actionable. Falling back to the native call
    after one would wait the FULL timeout again (a missing element took 2-3x `timeout` to fail), so
    timeouts propagate instead."""
    return type(err).__name__ == "TimeoutError"

# --------------------------------------------------------------------------- locator patch
_locator_patched = False
# Playwright's own Locator methods the humanized ones fall back to (blur / select_text), by name.
_LOCATOR_NATIVE = {}


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
    o_focus = getattr(Locator, "focus", None)
    o_blur = getattr(Locator, "blur", None)
    o_select_text = getattr(Locator, "select_text", None)
    _LOCATOR_NATIVE.update(blur=o_blur, select_text=o_select_text)

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
    # `delay` travels: humanized typing keeps it as the shortest gap between two keys (it was dropped).
    _TYPE = ("delay", "timeout")
    # `repeat` is the SDK's (OS auto-repeat, hpress); Playwright's own press does not take it.
    _PRESS = ("delay", "no_wait_after", "timeout", "repeat")
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
        if done:
            return r
        n = max(1, int(kw.pop("repeat", 1) or 1))   # not humanized: n separate Playwright presses
        for _ in range(n):
            r = o_press(self, key, **kw)
        return r

    def focus(self, **kw):
        done, r = _route(self, kw, lambda: self.page.focus(_sel(self), **_fwd(kw, ("timeout",))))
        return r if done else o_focus(self, **kw)

    def blur(self, **kw):
        fn = getattr(self.page, "_clearcote_blur", None)
        if fn is not None:
            done, r = _route(self, kw, lambda: fn(_sel(self), **_fwd(kw, ("timeout",))))
            if done:
                return r
        return o_blur(self, **kw)

    def select_text(self, **kw):
        fn = getattr(self.page, "_clearcote_select_text", None)
        if fn is not None:
            done, r = _route(self, kw, lambda: fn(_sel(self), **_fwd(kw, ("force", "timeout"))))
            if done:
                return r
        return o_select_text(self, **kw)

    def press_sequentially(self, text, **kw):
        done, r = _route(self, kw, lambda: self.page.type(_sel(self), text, **_fwd(kw, _TYPE)))
        return r if done else o_press_seq(self, text, **kw)

    def clear(self, **kw):
        done, r = _route(self, kw, lambda: self.page.fill(_sel(self), "", **_fwd(kw, _FILL)))
        return r if done else o_clear(self, **kw)

    def tap(self, **kw):
        if _no_touch(self):
            # Playwright's tap raises at once on a context without hasTouch ("The page does not
            # support tap"); routed as a click it clicked instead, or waited out the timeout for a
            # missing element and raised a TimeoutError (SC 210 error parity).
            return o_tap(self, **kw)
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
        if kw.get("trial") and _on(self) and _on(target) and _strict(self) and _strict(target):
            # Playwright's drag trial moves the pointer onto the source and presses there (SC 209);
            # the SDK runs the checks of both ends instead: visible, stable, receives the pointer.
            trial = getattr(self.page, "_clearcote_trial", None)
            if trial is not None:
                for loc, pos in ((self, kw.get("source_position")), (target, kw.get("target_position"))):
                    opts = {k: kw[k] for k in ("force", "timeout") if kw.get(k) is not None}
                    if pos:
                        opts["position"] = pos
                    trial(_sel(loc), opts, True)
                return None
        if _on(self) and not kw.get("trial"):
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
                    page.mouse.down()                     # pre-press pause, then held across the glide
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

    # Each worded as Playwright words the Locator method (api_errors): routed through page.*, an
    # error read "Page.click: ..." or "Locator.wait_for: ...".
    Locator.fill = api_errors("Locator.fill", fill)
    Locator.click = api_errors("Locator.click", click)
    Locator.type = api_errors("Locator.type", type_)
    Locator.dblclick = api_errors("Locator.dblclick", dblclick)
    Locator.hover = api_errors("Locator.hover", hover)
    Locator.press = api_errors("Locator.press", press)
    Locator.press_sequentially = api_errors("Locator.press_sequentially", press_sequentially)
    Locator.clear = api_errors("Locator.clear", clear)
    Locator.tap = api_errors("Locator.tap", tap)
    Locator.check = api_errors("Locator.check", check)
    Locator.uncheck = api_errors("Locator.uncheck", uncheck)
    Locator.drag_to = api_errors("Locator.drag_to", drag_to)
    Locator.select_option = api_errors("Locator.select_option", select_option)
    # New, not a Playwright method: press and hold this element (_hold.py). Works on frame locators too:
    # it presses at viewport coordinates through page.mouse.
    Locator.press_and_hold = api_errors(
        "Locator.press_and_hold", lambda self, duration=None, **kw: press_and_hold(self, duration, **kw))
    if o_set_checked is not None:
        Locator.set_checked = api_errors("Locator.set_checked", set_checked)
    if o_focus is not None:
        Locator.focus = api_errors("Locator.focus", focus)
    if o_blur is not None:
        Locator.blur = api_errors("Locator.blur", blur)
    if o_select_text is not None:
        Locator.select_text = api_errors("Locator.select_text", select_text)


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
