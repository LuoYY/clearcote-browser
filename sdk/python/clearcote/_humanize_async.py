"""Async mirror of _humanize.py for clearcote.async_api.

Same behaviour as the sync module, function by function: eased glides from the last cursor position,
native trusted input, click options honoured (button, click_count, delay, modifiers, position, force,
trial), field-classifying fill (text / range / date-like / native), fat-finger typos with mask-aware
correction, select_option by opening the drop-down (or clicking list-box rows), check/uncheck/
set_checked, and the Locator patch -- but every Playwright call is awaited and pacing uses
asyncio.sleep (off-protocol). The decisions come from the shared _forms module and the DOM reads go
through the isolated world (_isolated.AsyncIsolatedWorld), never page.evaluate. See _humanize.py for
the rationale and the measurements behind each step; comments here note what is async-specific.
"""

import asyncio
import random

from ._forms import (
    classify_field, datetime_equal, engine_path_ok, formatted_equal, match_option, normalize_click_options,
    numeric_equal, plan_datetime, plan_select, plan_typo, range_attrs, range_fraction, range_nudge, range_point,
    range_sanitize, range_value_ok, segment_nodes_from_tree, typo_eligible, typos_allowed,
)
from ._humanize import (
    CURSOR_OVERLAY, ENGINE_POINTER_PATH, _ROLL_KEYS, _Native, _cdp_method_missing, _host_platform, _is_timeout,
    _needs_shift, _rand, _sel_kw, _shift_lag_s, _shift_lead_s, typo_rate,
)
from ._isolated import (
    ACTIVE_FIELD as ISO_ACTIVE_FIELD, ACTIVE_VALUE as ISO_ACTIVE_VALUE, COVER_CHECK_MS, COVER_RETRY_MS,
    CSS_BOX as ISO_CSS_BOX, DESCRIBE as ISO_DESCRIBE, FORM_CONTROL, HIT_IN_BOX as ISO_HIT_IN_BOX,
    LISTBOX_ROW as ISO_LISTBOX_ROW, SCROLL_BY as ISO_SCROLL_BY,
    PLATFORM as ISO_PLATFORM, VIEWPORT as ISO_VIEWPORT, async_world_for,
)
from ._motion import (
    WHEEL_NOTCH_PX, make_persona, plan_move, drag_dwell, click_hold, key_dwell, click_point, plan_ambient,
    platform_from_navigator, rollover_rate, wheel_notches,
)
from ._pacing import trim_to as _trim_ambient, walk_async as _paced_walk


async def _release(coro):
    """Run a release -- a mouse button or a key coming up -- to completion even while the calling task
    is being cancelled. The finally blocks that call this already run on a first cancellation;
    asyncio.shield keeps the release going if a SECOND one arrives while it is awaited, so a cancelled
    action (asyncio.wait_for timing out, task.cancel()) never leaves a button or a key down. A button
    left down turns the next move into a drag; a key left down makes its next press an auto-repeat."""
    await asyncio.shield(coro)


async def attach_humanize(browser, page, humanize=False, show_cursor=False, seed=None):
    """Wrap one page's input methods + (optionally) inject the cursor overlay (async).

    ``seed`` (the fingerprint seed) selects a stable per-identity motor persona; unset ⇒ random."""
    if show_cursor:
        async def inject():
            try:
                await page.evaluate(CURSOR_OVERLAY)
            except Exception:  # noqa: BLE001
                pass
        await inject()
        page.on("load", lambda _=None: asyncio.ensure_future(inject()))
        page.on("framenavigated",
                lambda f: asyncio.ensure_future(inject()) if f == page.main_frame else None)
    if not humanize:
        return

    # Per-identity motor persona (cadence, tremor, overshoot, handedness) seeded from the fingerprint
    # so behavior is stable within a session and unlinkable across seeds. Stored on the page.
    persona = make_persona(seed)
    page._clearcote_persona = persona

    # Tracked cursor position so each move continues from where the last ended (seed off (0,0)).
    # "placed" is whether the REAL pointer has moved on this page yet: until it has, the browser's
    # cursor sits at the (0,0) origin whatever pos says. See _wheel_anchor / hdown /
    # _pointer_presence, and _humanize.py for the reasoning.
    st = {"pos": (_rand(140, 380), _rand(90, 240)), "held": False,
          "placed": False, "kb_primed": False}

    # ONE humanized action at a time on this page (async only). Two actions interleaved on one page
    # share the tracked cursor, the keyboard and the focus: asyncio.gather(page.fill("#a", A),
    # page.fill("#b", B)) returned success with a = "the q" and b holding A's text (verify-final-A,
    # S5a). Every public entry point below takes this lock; the helpers inside never call a locked
    # entry point (they use the native handles and the internal functions), so nothing deadlocks.
    # asyncio.Lock releases on cancellation, so a cancelled action does not block the next one.
    lock = asyncio.Lock()

    def _serial(fn):
        async def serial(*a, **kw):
            async with lock:
                return await fn(*a, **kw)
        serial.__name__ = getattr(fn, "__name__", "serial")
        return serial

    mouse = page.mouse
    native_move, native_click, native_wheel = mouse.move, mouse.click, mouse.wheel
    # getattr, not attribute access: attach must not raise on a page-like object that lacks a
    # method we only optionally wrap. Reading mouse.down/up directly widened the attach-time
    # contract and took four previously-green async tests down with an AttributeError before
    # anything was even wrapped — humanize failing closed on attach is the one outcome this
    # module must never produce, since it silently disables every other humanization too.
    native_down = getattr(mouse, "down", None)
    native_up = getattr(mouse, "up", None)
    # The keyboard natives are captured here (the sync file captures them further down): the press
    # helpers below hold modifiers with them.
    kb = page.keyboard
    native_kb_type, native_kb_press = kb.type, kb.press
    # getattr: a page-like keyboard without down/up still gets every other humanization; it just
    # types capitals without the Shift key, as before.
    native_kb_down, native_kb_up = getattr(kb, "down", None), getattr(kb, "up", None)

    async def _dispatch(steps):
        """Walk a planned trajectory (list of (x, y, sleep_ms)) via native trusted mouse.move +
        off-protocol asyncio.sleep. Native input carries button state (held-button drag works).
        Paced against a wall clock (_pacing.walk_async): samples go out at their planned time, late
        ones are skipped (never the last, never a > 40 px jump), so the dispatch round trip no
        longer stretches every glide 3-5x (SC 209)."""
        async def mv(sx, sy):
            await native_move(sx, sy)
            st["placed"] = True
        await _paced_walk(steps, mv)

    async def _glide(x, y, settle=False, target_w=24):
        # minimum-jerk sum-of-submovements path with colored noise + Fitts duration (_motion.plan_move)
        await _dispatch(plan_move(st["pos"], (x, y), persona, target_w=target_w, settle=settle))
        st["pos"] = (x, y)

    # Engine real-trajectory routing (PRO): send point-to-point moves/clicks through
    # Browser.dispatchPointerPath, humanizedClick before r34 (real recorded human paths for PRO, engine bezier for free,
    # tiered by the engine license gate). Drags stay native (held button); fall back to
    # the SDK bezier if the method is unavailable.
    _eng = {"session": None, "tid": None, "ok": True, "method": None}

    async def _engine_glide(x, y, no_click):
        if not _eng["ok"]:
            return False
        try:
            if _eng["session"] is None:
                tsession = await page.context.new_cdp_session(page)
                info = await tsession.send("Target.getTargetInfo")
                _eng["tid"] = info["targetInfo"]["targetId"]
                b = getattr(page.context, "browser", None)
                _eng["session"] = (await b.new_browser_cdp_session()
                                   if b is not None else tsession)
            dx, dy = x - st["pos"][0], y - st["pos"][1]
            dist = (dx * dx + dy * dy) ** 0.5
            dur = min(1.10, max(0.28, 0.30 + dist / 1700.0)) * (0.85 + 0.30 * random.random())
            params = {"targetId": _eng["tid"], "x": float(x), "y": float(y),
                      "duration": dur, "noClick": bool(no_click)}
            if _eng.get("method"):
                await _eng["session"].send(_eng["method"], params)
            else:
                # New name first; an engine older than r34 answers "wasn't found" (-32601)
                # and gets the old one. Any other error is a real failure (below).
                try:
                    await _eng["session"].send(ENGINE_POINTER_PATH[0], params)
                    _eng["method"] = ENGINE_POINTER_PATH[0]
                except Exception as e:  # noqa: BLE001
                    if not _cdp_method_missing(e):
                        raise
                    await _eng["session"].send(ENGINE_POINTER_PATH[1], params)
                    _eng["method"] = ENGINE_POINTER_PATH[1]
            # The engine walks the trajectory in the BROWSER process and answers before it has
            # finished. Without waiting, a following mouse.down() fires at the cursor's stale
            # position -- measured on the sync path: a move to (110,58) then a press landed at
            # (981,629), on <body> instead of the target, breaking every move-then-press
            # sequence (drag, press-and-hold, drag-and-drop). See _humanize.py.
            await asyncio.sleep(dur)
            try:
                await native_move(x, y)
            except Exception:  # noqa: BLE001
                pass
            st["pos"] = (x, y)
            st["placed"] = True
            return True
        except Exception:  # noqa: BLE001
            _eng["ok"] = False
            return False

    async def hmove(x, y, **kw):
        # A BARE MOVE ALWAYS USES THE NATIVE PATH -- never the engine trajectory. Two independent
        # reasons, both measured on the sync file (see _humanize.py):
        #   * a held button cannot be carried across the engine's own WebMouseEvents, so a drag
        #     leg routed through it reports buttons 0 mid-press and a range thumb ignores it;
        #   * the engine walks its path ASYNCHRONOUSLY and answers before finishing, so it keeps
        #     moving the cursor after hdown's pre-press pin -- a move to (56,120) then down()
        #     pressed on HTML at (856,213) instead of the INPUT, and the slider stayed at 0.
        # hclick keeps engine routing: it does move+click entirely engine-side, where nothing
        # races the trajectory. Kept identical to the sync file and to Node.
        await _glide(x, y)

    async def hdown(**kw):
        # DELIBERATELY NO PLACEMENT WHEN THE POINTER HAS NEVER MOVED.
        #
        # An earlier revision ran an ambient walk here and pressed wherever it ended. But st["pos"]
        # before any real move is the RANDOM SEED assigned at attach (_rand(140,380), _rand(90,240)) —
        # not a coordinate the caller ever named — so that turned a harmless no-op press into a
        # trusted click at an arbitrary page position, which can land on a link. A bare down() with
        # no preceding move is a caller mistake; inventing a target makes it a worse one. Callers
        # who want a humanized drag move first, and that move glides and sets "placed".
        if st["placed"]:
            # Pin the cursor to the tracked target before pressing: the engine answers
            # humanizedClick before its trajectory has finished and overruns the duration it
            # reports, so the wait is not sufficient on its own. Only meaningful once a real move
            # has happened — that is what makes st["pos"] correspond to the actual cursor.
            try:
                await native_move(*st["pos"])
            except Exception:  # noqa: BLE001 - best effort: keep the press on target
                pass
        # try/finally so a raising press cannot strand held=True and make every later move a
        # phantom drag leg (hup already had this; hdown did not).
        try:
            st["held"] = True
            await native_down(**kw)
        except Exception:
            st["held"] = False
            raise

    async def hup(**kw):
        try:
            await native_up(**kw)
        finally:
            st["held"] = False

    async def _platform():
        """The platform the page believes (the persona's), read once per page."""
        if "platform" not in st:
            plat = None
            try:
                plat = platform_from_navigator(await async_world_for(page).evaluate(ISO_PLATFORM))
            except Exception:  # noqa: BLE001
                pass
            st["platform"] = plat or _host_platform()
        return st["platform"]

    async def _press_at(x, y, o):
        """Press at (x, y) -- where the last glide ended -- the way the options ask: the button, the
        click count (a double click is two presses with a human gap), the caller's hold (`delay`) or
        the persona's, and modifiers held around the whole press with a lead before it and a lag
        after. Native down/up, so nothing moves. See _humanize._press_at."""
        mods = o["modifiers"]
        held = []
        try:
            for m in mods:
                held.append(m)   # before the await: a keydown in flight when cancelled was still sent
                await native_kb_down(m)
                await asyncio.sleep(_rand(40, 110) / 1000.0)
            await asyncio.sleep(_rand(40, 130) / 1000.0)    # brief dwell before pressing, like a human
            n = o["click_count"]
            for i in range(1, n + 1):
                hold = o["delay"] if o["delay"] is not None else click_hold(persona)
                try:
                    # inside the try: a press in flight when the task is cancelled was still sent
                    await native_down(button=o["button"], click_count=i)
                    await asyncio.sleep(hold / 1000.0)
                finally:
                    # never leave the button down -- not on an error, and not when this task is
                    # cancelled mid-hold (the next move would be a drag)
                    await _release(native_up(button=o["button"], click_count=i))
                if i < n:
                    await asyncio.sleep(_rand(70, 170) / 1000.0)   # the gap inside a double click
        finally:
            if held:
                async def lift(keys=tuple(reversed(held))):
                    for m in keys:
                        await asyncio.sleep(_rand(20, 80) / 1000.0)
                        try:
                            await native_kb_up(m)   # one failed keyup must not strand the others
                        except Exception:  # noqa: BLE001
                            pass
                await _release(lift())

    async def hclick(x, y, **kw):
        # The engine path performs ONE plain left click. Anything else -- right/middle button, a
        # double click, a caller-chosen hold, modifiers -- glides SDK-side and presses as asked.
        o = normalize_click_options(kw, await _platform())
        if engine_path_ok(o) and await _engine_glide(x, y, no_click=False):
            return
        await _glide(x, y)
        try:
            if native_down is None or native_up is None or (o["modifiers"] and native_kb_down is None):
                await asyncio.sleep(_rand(40, 130) / 1000.0)
                await native_click(x, y, delay=o["delay"] if o["delay"] is not None else click_hold(persona),
                                   **_sel_kw(kw, "button", "click_count"))
                return
            await _press_at(x, y, o)
        except Exception:  # noqa: BLE001
            pass

    async def hdblclick_xy(x, y, **kw):
        kw = dict(kw)
        kw["click_count"] = 2
        await hclick(x, y, **kw)   # the internal click: the public mouse.click is the locked one

    async def _held_glide(x, y):
        await _glide(x, y, settle=True)              # held-button drag leg + seating jiggle
    page._clearcote_held_glide = _held_glide

    async def hambient(ms=1200):
        """Opt-in ambient / pre-challenge cursor entropy before the first goal action. await page.ambient_motion(ms)."""
        try:
            vp = page.viewport_size or {"width": 1280, "height": 800}
            # plan_ambient overruns its budget (whole excursions, rests drawn at 0.9-1.4x a frame):
            # cut the plan at a slow sample near ms so ambient_motion(ms) lasts about ms.
            steps = _trim_ambient(plan_ambient(st["pos"], {"width": vp["width"], "height": vp["height"]},
                                               persona, ms), ms)
            await _dispatch(steps)
            if steps:
                st["pos"] = (steps[-1][0], steps[-1][1])
        except Exception:  # noqa: BLE001
            pass

    page.ambient_motion = hambient

    # OPT-IN: no ambient burst on load by default (it interleaves with caller-driven input,
    # e.g. a slider drag). Call page.ambient_motion(ms) explicitly, or set
    # page._clearcote_auto_ambient = True to restore the per-load burst.
    page._clearcote_auto_ambient = False

    async def _auto_ambient():
        if getattr(page, "_clearcote_auto_ambient", False):
            try:
                await hambient(_rand(450, 950))
            except Exception:  # noqa: BLE001
                pass

    page.on("load", lambda _=None: asyncio.ensure_future(_auto_ambient()))

    async def _viewport():
        """The REAL viewport, in CSS px.

        Not page.viewport_size: launch()/launch_persistent_context() force viewport=None on every
        headed page (so innerWidth tracks the real OS window rather than Playwright's emulated
        1280x720), and with viewport=None that property stays None for the life of the page. The
        old fallback therefore hit the 1280x800 constant on EVERY headed launch, so the anchor
        gate judged "is the pointer inside the viewport" against a box unrelated to the window and
        re-homed a correctly-placed cursor on every scroll.
        """
        try:
            wh = await async_world_for(page).evaluate(ISO_VIEWPORT)
            if wh and wh[0] and wh[1]:
                return float(wh[0]), float(wh[1])
        except Exception:  # noqa: BLE001
            pass
        vp = page.viewport_size or {"width": 1280, "height": 800}
        return float(vp["width"]), float(vp["height"])

    async def _wheel_anchor():
        """Put the cursor over the content before scrolling it.

        A wheel event is delivered wherever the pointer currently is, and until something has moved
        it that is the (0,0) origin — so the first scroll of a freshly loaded page arrives in the
        top-left corner, which is not where a hand ever scrolls. Only re-home when the pointer is
        nowhere sensible: a person does not move the mouse between two scrolls of the same page, so
        re-anchoring on every wheel call would be its own tell.
        """
        try:
            vw, vh = await _viewport()
            x, y = st["pos"]
            # With a button down every move is a DRAG leg, so anchoring mid-drag would haul the
            # grabbed element across the page instead of scrolling under it.
            if st["held"] or (st["placed"] and 8 <= x <= vw - 8 and 8 <= y <= vh - 8):
                return
            # Reading position: upper-middle, gaussian-spread. The exact centre is itself a tell.
            ax = min(max(vw * 0.5 + random.gauss(0, vw * 0.10), vw * 0.12), vw * 0.88)
            ay = min(max(vh * 0.34 + random.gauss(0, vh * 0.09), vh * 0.10), vh * 0.62)
            await _glide(ax, ay, target_w=140)   # coarse placement, not a target acquisition
        except Exception:  # noqa: BLE001 - never let the anchor stop the scroll
            pass

    async def _notch_px():
        """Pixels per wheel notch for the platform the page believes, read once per page."""
        if "notch" not in st:
            plat = None
            try:
                plat = platform_from_navigator(await async_world_for(page).evaluate(ISO_PLATFORM))
            except Exception:  # noqa: BLE001
                pass
            st["notch"] = WHEEL_NOTCH_PX[plat or _host_platform()]
        return st["notch"]

    async def hwheel(delta_x, delta_y):
        # Whole notches only: a CDP wheel event always reports wheelDelta +-120, so the eased
        # sub-notch slices this replaced were events no mouse emits (see _humanize.hwheel).
        await _wheel_anchor()
        notch = await _notch_px()
        plan = ([(0, notch if delta_y > 0 else -notch)] * wheel_notches(delta_y, notch)
                + [(notch if delta_x > 0 else -notch, 0)] * wheel_notches(delta_x, notch))
        burst = random.randint(2, 5)
        for k, (ex, ey) in enumerate(plan):
            await native_wheel(ex, ey)
            if k == len(plan) - 1:
                break
            burst -= 1
            if burst == 0:
                await asyncio.sleep(_rand(110, 320) / 1000.0)  # the finger resets for the next flick
                burst = random.randint(2, 5)
            else:
                await asyncio.sleep(_rand(18, 55) / 1000.0)
            if random.random() < 0.07:
                await asyncio.sleep(_rand(150, 450) / 1000.0)
            # A hand resting on the mouse does not hold it perfectly still through a long scroll, so
            # wheel events sharing one identical clientX/clientY are a script signature. A couple
            # of px, so the scroll stays over the same element (and never while a button is down,
            # where a move is a drag leg).
            if len(plan) >= 6 and not st["held"] and random.random() < 0.06:
                try:
                    dx, dy = random.gauss(0, 1.5), random.gauss(0, 1.2)
                    await native_move(st["pos"][0] + dx, st["pos"][1] + dy)
                    st["pos"] = (st["pos"][0] + dx, st["pos"][1] + dy)
                except Exception:  # noqa: BLE001
                    pass

    mouse.move, mouse.click, mouse.wheel = hmove, _serial(hclick), hwheel
    if getattr(mouse, "dblclick", None) is not None:
        mouse.dblclick = _serial(hdblclick_xy)   # stock dblclick teleported and pressed for ~0 ms
    # down/up wrapped only to TRACK held state for hmove; the presses stay native.
    mouse.down, mouse.up = hdown, hup

    # ----------------------------------------------------------------- keyboard
    async def _pointer_presence():
        """Give a keyboard-only session a mouse, once, before its first keystroke.

        Selector-driven typing already glides (page.type/fill/press go through _focus_click), but a
        bare page.keyboard.type/press types into whatever is focused — from a session in which the
        pointer never existed, i.e. keydowns with zero preceding pointer events.

        AMBIENT ONLY, deliberately not a move toward the focused field: the caller has already put
        focus somewhere (often from script), and gliding onto a control could hover or re-target it
        and land the text in the wrong place. plan_ambient never clicks, so focus cannot change.
        """
        if st["placed"] or st["kb_primed"]:
            return
        st["kb_primed"] = True   # one attempt per page, even if the ambient burst itself fails
        try:
            await hambient(_rand(280, 620))
        except Exception:  # noqa: BLE001
            pass

    async def _emit_key(ch):
        """One character with a human keydown->keyup DWELL (keyboard.press's delay = the hold)."""
        try:
            await native_kb_press(ch, delay=key_dwell(persona))
        except Exception:  # noqa: BLE001
            try:
                await native_kb_type(ch)
            except Exception:  # noqa: BLE001
                pass

    async def _active_value():
        """The focused field's current value, read in the isolated world (None if unknown)."""
        try:
            return await async_world_for(page).evaluate(ISO_ACTIVE_VALUE)
        except Exception:  # noqa: BLE001
            return None

    def _flight_s():
        # gaussian inter-key cadence (a realistic distribution, not a uniform band)
        return max(0.025, random.gauss(0.085, 0.045))

    async def _backspaces(n):
        for k in range(n):
            await native_kb_press("Backspace", delay=key_dwell(persona))
            if k < n - 1:
                await asyncio.sleep(_rand(70, 150) / 1000.0)

    def _landed(before, now):
        """How many characters arrived since ``before`` (None = cannot tell)."""
        if before is None or now is None:
            return None
        return len(now) - len(before)

    async def _do_typo(text, i, tp):
        """Make the planned slip at text[i] and correct it. Returns True when it already typed
        text[i] itself (the doubled-key case), so the caller must not type it again.

        Only what actually LANDED is deleted: an input mask or a maxlength can swallow the wrong key,
        and a Backspace pressed for a key that never arrived deletes a real character. Measured
        before and after the slip, in the isolated world (see _humanize._do_typo)."""
        before = await _active_value()
        kind = tp["kind"]
        if kind == "sub":
            await _emit_key(tp["wrong"])
            got = _landed(before, await _active_value())
            if got is not None and got <= 0:
                return False                     # swallowed: nothing to correct
            typed = 1
            for c in text[i + 1:i + 1 + tp.get("late", 0)]:   # late notice: a key or two more
                await asyncio.sleep(_flight_s())
                await _emit_key(c)
                typed += 1
            await asyncio.sleep(_rand(140, 340) / 1000.0)  # noticing
            got = _landed(before, await _active_value())
            await _backspaces(typed if got is None else max(0, min(typed, got)))
            await asyncio.sleep(_rand(80, 200) / 1000.0)
            return False
        if kind == "double":
            await _emit_key(text[i])
            await asyncio.sleep(_rand(45, 95) / 1000.0)      # the bounce comes fast
            await _emit_key(text[i])
            got = _landed(before, await _active_value())
            await asyncio.sleep(_rand(140, 340) / 1000.0)
            extra = 1 if got is None else max(0, got - 1)
            if extra:
                await _backspaces(extra)
                await asyncio.sleep(_rand(80, 200) / 1000.0)
            return True
        if kind == "swap":
            await _emit_key(text[i + 1])
            await asyncio.sleep(_flight_s())
            await _emit_key(text[i])
            got = _landed(before, await _active_value())
            await asyncio.sleep(_rand(140, 340) / 1000.0)
            await _backspaces(2 if got is None else max(0, min(2, got)))
            await asyncio.sleep(_rand(80, 200) / 1000.0)
            return False
        return False

    async def _typing_field(field):
        """What the typo gate needs about the field keys go to: the caller's description, else
        the focused element read in the isolated world."""
        if field is not None:
            return field
        try:
            return await async_world_for(page).evaluate(ISO_ACTIVE_FIELD)
        except Exception:  # noqa: BLE001
            return None

    async def _human_type_text(text, field=None, typos=True):
        """Type text key-by-key with human timing through Playwright's native (trusted) keyboard.

        Capitals and shifted symbols go behind a real ShiftLeft held across the run; unshifted pairs
        roll over; fat-finger typos are planned by _forms.plan_typo and never happen where a slip is
        a failure rather than a slip (see _humanize._human_type_text)."""
        text = str(text)
        n = len(text)
        shift = False
        roll_rate = rollover_rate(persona)
        rate = typo_rate() if typos else 0.0
        allow = rate > 0 and typos_allowed(await _typing_field(field))
        i = 0
        try:
            while i < n:
                ch = text[i]
                need = _needs_shift(ch)
                if need and not shift and native_kb_down and native_kb_up:
                    shift = True   # before the await: a keydown in flight when cancelled was still sent
                    await native_kb_down("Shift")
                    await asyncio.sleep(_shift_lead_s())
                typo = False
                consumed = False
                if allow and not need and not shift and typo_eligible(ch):
                    tp = plan_typo(text, i, random, rate)
                    if tp is not None:
                        typo = True
                        try:
                            consumed = await _do_typo(text, i, tp)
                        except Exception:  # noqa: BLE001
                            pass
                # ROLLOVER: the next key goes down before this one comes up (see
                # _humanize._human_type_text). Unshifted single-key pairs only.
                nxt = text[i + 1] if i + 1 < n else None
                d1 = key_dwell(persona)
                if (nxt is not None and not typo and not shift and native_kb_down and native_kb_up
                        and ch in _ROLL_KEYS and nxt in _ROLL_KEYS and ch != nxt and d1 - 8 >= 12
                        and random.random() < roll_rate):
                    overlap = _rand(12, min(55, d1 - 8))
                    # A key counts as down from the moment its keydown is SENT: a cancellation that
                    # lands while that call is awaited does not unsend it (measured on r37: a key left
                    # down after a cancel mid-call). Likewise it stops counting once its keyup is sent.
                    down = []
                    try:
                        down.append(ch)
                        await native_kb_down(ch)
                        await asyncio.sleep((d1 - overlap) / 1000.0)
                        down.append(nxt)
                        await native_kb_down(nxt)
                        await asyncio.sleep(overlap / 1000.0)
                        down.remove(ch)
                        await native_kb_up(ch)
                        await asyncio.sleep(max(5.0, key_dwell(persona) - overlap) / 1000.0)
                        down.remove(nxt)
                        await native_kb_up(nxt)
                    except BaseException as e:  # noqa: BLE001
                        # Lift whatever is still down -- also when this task is CANCELLED mid-rollover
                        # (CancelledError is not an Exception, so the sync file's handler would miss
                        # it and strand the key). A cancellation is re-raised; an error ends the typing,
                        # as in the sync file. (A coroutine being closed may not await: re-raise.)
                        if isinstance(e, GeneratorExit):
                            raise

                        async def lift(keys=tuple(down)):
                            for k in keys:
                                try:
                                    await native_kb_up(k)
                                except Exception:  # noqa: BLE001
                                    pass
                        await _release(lift())
                        if not isinstance(e, Exception):
                            raise
                        break
                    i += 1          # nxt is typed; the cadence below runs as if after it
                    ch = nxt
                elif not consumed:
                    try:
                        await _emit_key(ch)
                    except Exception:  # noqa: BLE001
                        break
                if shift and (i == n - 1 or not _needs_shift(text[i + 1])):
                    await asyncio.sleep(_shift_lag_s())
                    shift = False   # before the await: the keyup is sent even if cancelled mid-call
                    await native_kb_up("Shift")
                if i < n - 1:
                    # gaussian inter-key cadence (a realistic distribution, not a uniform band)
                    d = max(0.025, random.gauss(0.085, 0.045))   # ~85ms +- 45ms, floored at 25ms
                    if ch in " \t\n":
                        d += _rand(0.02, 0.10)                    # slight pause at word boundaries
                    if random.random() < 0.06:
                        d += _rand(0.18, 0.45)                    # occasional thinking pause
                    await asyncio.sleep(d)
                i += 1
        finally:
            if shift:  # never leave Shift held for the caller's next keystroke (nor on cancellation)
                try:
                    await _release(native_kb_up("Shift"))
                except Exception:  # noqa: BLE001
                    pass

    async def hkb_type(text, **kw):
        await _pointer_presence()
        await _human_type_text(text)

    async def hkb_press(key, **kw):
        """keyboard.press with the persona's hold unless the caller specified their own — the sync
        wrapper's rationale applies unchanged (a press that holds ~2ms undoes the whole keyboard
        persona; press(key, delay=...) is the documented way to hold a key, so the caller wins)."""
        await _pointer_presence()
        if not kw.get("delay") or kw["delay"] <= 0:
            # delay=0 is Playwright's default ("no hold"), so an explicit 0 is no different from none
            kw["delay"] = key_dwell(persona)
        return await native_kb_press(key, **kw)

    kb.type = _serial(hkb_type)
    kb.press = _serial(hkb_press)

    # ------------------------------------------------------- page-level targeted helpers
    # Points tried when resolving a control through the browser's hit test: a floating label or an
    # icon often sits over a field's CENTRE, so the edges get a chance too.
    probe_points = ((0.5, 0.5), (0.82, 0.5), (0.18, 0.5), (0.5, 0.8), (0.5, 0.2))

    async def _describe(box, want=FORM_CONTROL):
        """The control filling ``box`` (Playwright's resolved box for a locator), read in the isolated
        world through the browser's own hit test (DOM.getNodeForLocation in DOCUMENT coordinates, then
        DESCRIBE on that node). The element found must fill the SAME box (±1.5 px), or it is not the
        one the locator means; None when no probe point proves that."""
        if not box or box["width"] <= 0 or box["height"] <= 0:
            return None
        w = async_world_for(page)
        scroll = await w.scroll()   # once: the hit test wants document coordinates
        for fx, fy in probe_points:
            nid = await w.node_at(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy, scroll)
            if nid is None:
                continue
            info = await w.call_on_node(nid, ISO_DESCRIBE, {"want": want})
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

    async def _redescribe(info, want=FORM_CONTROL):
        """Describe the SAME element again, from the node the first hit test found: no new
        Playwright handle (see _box_of), so nothing the page can see. None if it is gone."""
        again = await async_world_for(page).call_on_node(info["node"], ISO_DESCRIBE, {"want": want})
        if again:
            again["node"] = info["node"]
            again["probe"] = info.get("probe", (0.5, 0.5))
        return again

    async def _in_view(box):
        vw, vh = await _viewport()
        return box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= vw and box["y"] + box["height"] <= vh

    def _rect_box(r):
        if not r or not r.get("rect"):
            return None
        x, y, w, h = r["rect"]
        return {"x": x, "y": y, "width": w, "height": h} if w > 0 and h > 0 else None

    async def _css_box(selector):
        """The box of a plain-CSS selector's first match, read (and scrolled into view) in the
        isolated world: not even the one bounding_box() handle, and its textContent preview, is made.
        Used only when Playwright counts exactly the elements the browser's own querySelectorAll
        finds -- Playwright's CSS also pierces open shadow roots, so equal counts mean the same
        elements in the same order. None otherwise (get_by_role, text=, `>>` chains, :has-text()...:
        then Playwright's bounding_box). See _humanize._css_box."""
        if not isinstance(selector, str) or not selector:
            return None
        w = async_world_for(page)
        r = await w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": False})
        if not r or not r.get("n"):
            return None
        try:
            if await page.locator(selector).count() != r["n"]:
                return None
        except Exception:  # noqa: BLE001
            return None
        box = _rect_box(r)
        if box and not (await _in_view(box) and await _hit_ok(box)):
            # scrollIntoViewIfNeeded also scrolls a scrolling container that clips it, and does
            # nothing when it already shows (then the cover is an overlay: the click's own check)
            box = _rect_box(await w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": True}))
        return box

    async def _hit_in_box(box, x, y):
        """Does a press at viewport (x, y) reach the element filling ``box``? True / False, or None
        when it cannot be told here: the point lies in a child frame, or CDP is unavailable."""
        w = async_world_for(page)
        nid = await w.node_at(x, y)
        if nid is None:
            return None
        res = await w.call_on_node(nid, ISO_HIT_IN_BOX,
                                   {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]})
        return None if res is None else bool(res)

    async def _hit_ok(box):
        """Does the element filling ``box`` own its centre -- not clipped by a scrolling container,
        not covered? The browser's own hit test, read in the isolated world."""
        return await _hit_in_box(box, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2) is True

    async def _scroll_window_to(box):
        """Scroll the WINDOW so ``box`` sits centred where it was off-screen, the way Playwright's
        scroll centres it, and return where the box is now (moved by exactly the scrolled distance) --
        or None. Needs no handle: the box is already known."""
        vw, vh = await _viewport()
        if box["width"] > vw or box["height"] > vh:
            return None
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        dx = cx - vw / 2 if box["x"] < 0 or box["x"] + box["width"] > vw else 0
        dy = cy - vh / 2 if box["y"] < 0 or box["y"] + box["height"] > vh else 0
        moved = await async_world_for(page).evaluate(ISO_SCROLL_BY, [dx, dy])
        if not isinstance(moved, list) or len(moved) != 2:
            return None
        return {"x": box["x"] - moved[0], "y": box["y"] - moved[1], "width": box["width"], "height": box["height"]}

    async def _box_of(loc, timeout, force=False, selector=None):
        """Wait the way Playwright would, with the CALLER's timeout (None = the page default), and
        return the box, in view. With `force` Playwright skips actionability, so nothing is waited
        for: a hidden target goes native at once.

        Each Playwright call that resolves the element to a handle (wait_for,
        scroll_into_view_if_needed, bounding_box) makes a MAIN-world handle whose preview reads the
        element's textContent where the page can see it (verifier B: 4 such reads per humanized click,
        7 per textarea fill). So each is made only when needed: the wait only for an element not
        visible yet (is_visible runs in Playwright's utility world), the scroll only for a box not
        already in view, and a plain-CSS ``selector`` needs no handle at all (_css_box). What remains,
        for other selectors, is the one bounding_box. See _humanize._box_of."""
        if not await loc.is_visible():
            if force:
                raise _Native()
            await loc.wait_for(state="visible", timeout=timeout)
        box = await _css_box(selector)
        if box is not None:
            return box
        box = await loc.bounding_box()
        if box and not (await _in_view(box) and await _hit_ok(box)):
            # Off-screen: scroll the window to it and prove it shows there (hit test), with no new
            # handle. Clipped by a scrolling container, covered, or the window scroll did not bring
            # it out: Playwright's scroll, which also scrolls containers, and a fresh box.
            moved = await _scroll_window_to(box) if not await _in_view(box) else None
            if moved and await _in_view(moved) and await _hit_ok(moved):
                return moved
            await loc.scroll_into_view_if_needed(timeout=timeout)
            box = await loc.bounding_box()
        return box

    async def _focused(loc):
        """Does ``loc`` hold the keyboard focus? Asked through Playwright's utility-world selector
        engine (`:focus`), so it works for every element and every locator and the page sees nothing.
        The isolated hit test only knew form controls, so a focused BUTTON read as unfocused and got
        clicked before a press -- two activations."""
        try:
            return await loc.and_(page.locator(":focus")).count() > 0
        except Exception:  # noqa: BLE001
            return False

    async def _is_focused(selector):
        return await _focused(page.locator(selector).first)

    async def _text_entry(loc, timeout, selector=None):
        """(box, info) when ``loc`` is a visible text-entry control a person would click into before
        typing, else None. Anything else -- a button, a link, a hidden helper textarea, a slider -- is
        left to Playwright, which focuses it without a click (a click on a button activates it; on a
        slider it moves it)."""
        if not await loc.is_visible():
            return None
        box = await _box_of(loc, timeout, selector=selector)
        info = await _describe(box)
        if info and classify_field(info["tag"], info["type"], info["ce"]) == "text":
            return box, info
        return None

    async def _focus_click(box, at=None, loc=None, selector=None):
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
        await asyncio.sleep(0.05)
        info = await _describe(box)
        if info is None:
            again = await _css_box(selector) if selector else None
            if again is None and loc is not None:
                again = await loc.bounding_box()
            if not again or abs(again["x"] - box["x"]) > 1 or abs(again["y"] - box["y"]) > 1:
                return False
        elif info.get("disabled"):
            return False
        pt = at if at is not None else click_point(box, st["pos"], persona)  # 2D gaussian toward centre
        await _glide(pt[0], pt[1])
        await asyncio.sleep(_rand(40, 130) / 1000.0)
        await native_click(pt[0], pt[1], delay=click_hold(persona))
        return True

    async def _key(k, n=1, gap=(60, 150)):
        for j in range(n):
            await native_kb_press(k, delay=key_dwell(persona))
            if j < n - 1:
                await asyncio.sleep(_rand(*gap) / 1000.0)

    async def _select_all():
        """Ctrl+A (Cmd+A on a Mac engine) the way a hand does it: the modifier goes down a moment
        before the A, the A is held, the modifier comes up just after (as one Playwright chord the two
        went down within ~1 ms). ControlOrMeta lets Playwright pick the chord the ENGINE's editing
        honours: a Meta+A sent to a Linux engine selects nothing."""
        if native_kb_down is None or native_kb_up is None:
            await _key("ControlOrMeta+a")
            return
        try:
            await native_kb_down("ControlOrMeta")   # inside the try: sent even if cancelled mid-call
            await asyncio.sleep(_rand(40, 110) / 1000.0)
            await native_kb_press("a", delay=key_dwell(persona))
            await asyncio.sleep(_rand(20, 80) / 1000.0)
        finally:
            await _release(native_kb_up("ControlOrMeta"))   # also when cancelled mid-chord

    async def _clear_focused(read=None):
        """Select all, then Backspace -- each key HELD like a finger holds it (never ~1 ms). If the
        field is still not empty (``read`` -- the field's value -- says so), it is emptied from the
        end, one Backspace per character (≤ 64)."""
        read = read or _active_value
        await _select_all()
        await asyncio.sleep(_rand(60, 160) / 1000.0)
        await _key("Backspace")
        await asyncio.sleep(_rand(60, 160) / 1000.0)
        left = await read()
        if left:
            if len(left) > 64:
                raise RuntimeError("field would not clear")
            await _key("End")
            await asyncio.sleep(_rand(80, 180) / 1000.0)
            await _key("Backspace", len(left), gap=(55, 120))
            await asyncio.sleep(_rand(60, 160) / 1000.0)
            if await read():
                raise RuntimeError("field would not clear")

    # getattr: a page-like object without one of these still gets every OTHER humanization (the
    # async fakes in the test suite implement only a few page methods).
    native_page_click = getattr(page, "click", None)
    native_page_hover = getattr(page, "hover", None)
    native_page_type = getattr(page, "type", None)
    native_page_fill = getattr(page, "fill", None)
    native_page_press = getattr(page, "press", None)
    native_page_dblclick = getattr(page, "dblclick", None)
    native_page_select_option = getattr(page, "select_option", None)

    async def _click_into(selector, timeout):
        """Make ``selector`` the focus the way a person does (a click), unless it already is. Only
        text-entry controls are clicked; for anything else, and when the click did not actually put
        the caret there (an overlay took it), the caller goes native."""
        loc = page.locator(selector).first
        if await _focused(loc):
            return
        te = await _text_entry(loc, timeout, selector)
        if (te is None or not await _focus_click(te[0], _proven_point(*te), loc, selector)
                or not await _focused(loc)):
            raise _Native()
        await asyncio.sleep(_rand(40, 120) / 1000.0)

    async def htype(selector, text, **options):
        try:
            await _click_into(selector, options.get("timeout"))
            await _human_type_text(text)
            return None
        except _Native:
            pass
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        # outside the try: a native call that raises must raise ONCE, not be retried by the handler
        return await native_page_type(selector, text, **options)

    async def _fill_text(loc, box, value, info, selector=None):
        """Type ``value`` into a text control and PROVE it landed. The value is read through
        Playwright's utility world (`input_value`): it sees the control the locator means, wherever
        focus is, and the page cannot observe it. A field whose value cannot be read, or a click that
        did not put the caret in it (an overlay took it), is left to Playwright -- "could not check"
        is never treated as success."""
        if len(value) > 200:
            return False   # bulk values: keep fill fast/atomic (humanizing 1000s of chars would crawl)
        is_ce = bool(info and info.get("ce")) or (
            info is None and ((await loc.get_attribute("contenteditable")) or "false") != "false")
        if not await _focus_click(box, _proven_point(box, info) if box else None, loc, selector):
            return False
        await asyncio.sleep(_rand(40, 120) / 1000.0)
        if not await _focused(loc):
            return False

        async def read():
            try:
                return await loc.input_value(timeout=COVER_RETRY_MS)
            except Exception:  # noqa: BLE001
                return None

        if is_ce:
            cur = info.get("value") if info else None
            if cur:
                await _clear_focused()
            await _human_type_text(value, field=info)
            return True   # contenteditable: Playwright's fill sets text the same way; no value to read
        cur = await read()
        if cur is None:
            return False
        if cur:  # a person does not select-all an empty field
            await _clear_focused(read)
        await _human_type_text(value, field=info)
        got = await read()
        if got is None:
            return False
        if got == value or formatted_equal(got, value):
            return True   # exact, or a mask that only re-punctuated what was typed (a phone format)
        # Wrong value (an autocompleter interfered, a key was lost): one clean retry without slips,
        # then give the field to Playwright -- a correct value beats pretty input.
        await _clear_focused(read)
        await _human_type_text(value, field=info, typos=False)
        got = await read()
        return got is not None and (got == value or formatted_equal(got, value))

    async def _fill_range(loc, box, info, value):
        """A range input: drag the thumb (or click the track) to the value, then arrow-nudge to it
        exactly (see _humanize._fill_range)."""
        if info is None:
            info = {"min": await loc.get_attribute("min"), "max": await loc.get_attribute("max"),
                    "step": await loc.get_attribute("step"), "value": await loc.input_value(),
                    "dir": "ltr", "wm": ""}
        if not range_value_ok(value, info.get("min"), info.get("max"), info.get("step")):
            # Off the step, out of range, not a number: Playwright raises "Malformed value" for these,
            # so does this route (by leaving them to it) -- never a silently snapped or clamped value.
            return False
        if (info.get("wm") or "").startswith("vertical"):
            return False
        rtl = info.get("dir") == "rtl"
        lo, hi, step = range_attrs(info.get("min"), info.get("max"), info.get("step"))
        target = range_sanitize(value, info.get("min"), info.get("max"), info.get("step"))

        async def now():
            try:
                return float(await loc.input_value())
            except (TypeError, ValueError):
                return None

        cur = await now()
        if cur is not None and numeric_equal(cur, target):
            return True
        tx, ty = range_point(box, range_fraction(target, lo, hi), rtl)
        ty += random.gauss(0, box["height"] * 0.08)
        if cur is not None and native_down and native_up and random.random() < 0.65:
            # Grab the thumb where it is and slide it: what most hands do with a slider.
            cx, cy = range_point(box, range_fraction(cur, lo, hi), rtl)
            cy += random.gauss(0, box["height"] * 0.08)
            await _glide(cx, cy, target_w=16)
            await asyncio.sleep(_rand(60, 160) / 1000.0)
            grab_ms, release_ms = drag_dwell(persona)
            st["held"] = True
            try:
                await native_down()   # inside the try: a press in flight when cancelled was still sent
                await asyncio.sleep(grab_ms / 1000.0)
                await _glide(tx, ty, settle=True)
                await asyncio.sleep(release_ms / 1000.0)
            finally:
                st["held"] = False
                await _release(native_up())   # the thumb is let go even when cancelled mid-drag
        else:
            await _glide(tx, ty, target_w=16)
            await asyncio.sleep(_rand(40, 130) / 1000.0)
            await native_click(tx, ty, delay=click_hold(persona))
        for _ in range(3):
            v = await now()
            if v is None:
                return False
            if numeric_equal(v, target):
                return True
            key, n = range_nudge(v, target, step)
            if key is None or n <= 0 or n > 40:
                break
            if rtl:
                key = "ArrowLeft" if key == "ArrowRight" else "ArrowRight"
            await asyncio.sleep(_rand(150, 350) / 1000.0)
            await _key(key, n)
        v = await now()
        return v is not None and numeric_equal(v, target)

    async def _fill_datetime(loc, box, info, value):
        """A native date / time / month / week / datetime-local editor: click into its first field
        and type each field in the order the editor itself shows them (read from its UA shadow tree
        over CDP -- the order follows the browser, never assumed). See _humanize._fill_datetime."""
        if not info or not info.get("node"):
            return False
        kind = info.get("type")
        w = async_world_for(page)
        tree = await w.send("DOM.describeNode", {"backendNodeId": info["node"], "depth": -1, "pierce": True})
        if not tree or "node" not in tree:
            return False
        segs = segment_nodes_from_tree(tree["node"])
        plan = plan_datetime(kind, [s for s, _ in segs], value, current=info.get("value"))
        if not plan or not segs:
            return False
        px, py, fw = None, None, 12.0
        bm = await w.send("DOM.getBoxModel", {"backendNodeId": segs[0][1]}) if segs[0][1] else None
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
        await _glide(px, py, target_w=fw)
        await asyncio.sleep(_rand(40, 130) / 1000.0)
        await native_click(px, py, delay=click_hold(persona))
        await asyncio.sleep(_rand(150, 350) / 1000.0)
        for a in plan:
            if a[0] == "type":
                for j, ch in enumerate(a[1]):
                    await native_kb_press(ch, delay=key_dwell(persona))
                    if j < len(a[1]) - 1:
                        await asyncio.sleep(max(0.03, random.gauss(0.11, 0.04)))
            else:
                await _key(a[1], a[2] if len(a) > 2 else 1, gap=(70, 170))
            await asyncio.sleep(_rand(60, 220) / 1000.0)   # between fields: the eye checks what it typed
        return datetime_equal(kind, await loc.input_value(), value)

    async def _classify_fallback(loc):
        """Classify through Playwright's utility-world queries when the hit test could not prove the
        element (it is covered, or inside a frame): attributes only, nothing the page can see."""
        for tag in ("textarea", "select"):
            if await loc.locator("xpath=self::%s" % tag).count():
                return classify_field(tag)
        if await loc.locator("xpath=self::input").count():
            return classify_field("input", await loc.get_attribute("type"))
        ce = await loc.get_attribute("contenteditable")
        return classify_field("div", None, ce is not None and ce.lower() != "false")

    async def hfill(selector, value, **options):
        try:
            if isinstance(value, str):
                timeout = options.get("timeout")   # None: the page's default, as Playwright would use
                loc = page.locator(selector).first
                box = await _box_of(loc, timeout, options.get("force"), selector)
                info = await _describe(box)
                kind = (classify_field(info["tag"], info["type"], info["ce"]) if info
                        else await _classify_fallback(loc))
                if not (info and (info.get("disabled") or info.get("readonly"))):
                    if kind == "text" and await _fill_text(loc, box, value, info, selector):
                        return None
                    if kind == "range" and await _fill_range(loc, box, info, value):
                        return None
                    if kind == "datetime" and await _fill_datetime(loc, box, info, value):
                        return None
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        # Native: color (Playwright sets it -- the OS dialog is no route), unfillable types (Playwright
        # raises, as before humanize), and anything the humane route could not prove it did right.
        return await native_page_fill(selector, value, **options)

    async def hdblclick(selector, **options):
        o = dict(options)
        o.setdefault("click_count", 2)
        return await _targeted_click(native_page_dblclick, selector, o, no_click=False, native_opts=options)

    async def hpress(selector, key, **options):
        try:
            # Click into text fields only: a press on a button or a link that is not focused yet is
            # Playwright's (it focuses without activating); clicking it first activated it twice.
            await _click_into(selector, options.get("timeout"))
            # The persona's hold unless the caller set a real one, for the same reason hkb_press
            # carries one: this is the path Locator.press delegates to.
            d = options.get("delay")
            await native_kb_press(key, delay=d if d else key_dwell(persona))
            return None
        except _Native:
            pass
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        return await native_page_press(selector, key, **options)

    async def _open_and_pick(selector, plan, at=None):
        """Open the drop-down with a click, move its highlight with the planned keys, commit with
        Enter: one trusted input+change. While the popup is open its keys never reach the page
        (measured on r37: value unchanged until Enter). The opening click is the full humanized click
        -- including the covered-point check -- at the point the hit test proved when the centre is
        covered (``at``, a Playwright `position`), so a floating label over the select's centre
        cannot swallow it and leave the letters to change a closed select one per key."""
        await _targeted_click(native_page_click, selector, {"position": at} if at else {}, no_click=False,
                              native_opts=None)
        await asyncio.sleep(_rand(220, 480) / 1000.0)        # the list opens; the eye finds the option
        for ch in plan["keys"]:
            await native_kb_press(ch, delay=key_dwell(persona))
            await asyncio.sleep(max(0.04, random.gauss(0.10, 0.035)))   # well inside type-ahead's 1 s window
        n = plan["arrows"]
        if n:
            await asyncio.sleep(_rand(120, 300) / 1000.0)
            await _arrow_run("ArrowDown" if n > 0 else "ArrowUp", abs(n))
        await asyncio.sleep(_rand(150, 400) / 1000.0)
        await _key("Enter")
        await asyncio.sleep(_rand(80, 160) / 1000.0)

    async def _arrow_run(key, n):
        """``n`` presses of an arrow key in an OPEN drop-down. A long run is HELD, the way a person
        crosses a long list: one press, the keyboard's repeat delay, then auto-repeat at its rate --
        Playwright sends a key that is already down again as an autoRepeat keydown. Pressed one by
        one at ~180 ms each, 370 rows took 68 s (verifier B). See _humanize._arrow_run."""
        if n < 8 or native_kb_down is None or native_kb_up is None:
            await _key(key, n)
            return
        try:
            # inside the try: a keydown in flight when the task is cancelled was still sent
            await native_kb_down(key)
            await asyncio.sleep(_rand(380, 560) / 1000.0)    # the repeat delay
            gap = _rand(28, 36) / 1000.0                     # ~30 repeats a second
            for _ in range(n - 1):
                await asyncio.sleep(gap)
                await native_kb_down(key)
        finally:
            async def lift():
                await asyncio.sleep(_rand(20, 60) / 1000.0)
                await native_kb_up(key)
            await _release(lift())   # the key comes up even when cancelled mid-run

    async def _listbox_row(info, i):
        return await async_world_for(page).call_on_node(info["node"], ISO_LISTBOX_ROW, {"i": i})

    async def _settled_row(info, i):
        """The list box's state once its scroll has come to rest. A wheel notch scrolls the box
        SMOOTHLY over a few frames, and a read taken mid-animation shows the row where it was, not
        where it is going: the old loop then wheeled the wrong way and oscillated (verifier B). Two
        equal reads 50 ms apart = at rest."""
        prev = await _listbox_row(info, i)
        for _ in range(30):
            await asyncio.sleep(0.05)
            cur = await _listbox_row(info, i)
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

    async def _scroll_row_into_view(info, i):
        """Wheel the list box until row ``i`` shows: the pointer goes over the box once, then as many
        notches as the distance needs -- one at first, to learn how far a notch scrolls this box --
        each burst read only once the scroll has settled. (top, bottom, state), or None when it
        cannot be brought into view (the wheel does not move the box, or it is at its end)."""
        s = await _settled_row(info, i)
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
                await _glide(bx + bw * _rand(0.3, 0.7), s["top"] + room * _rand(0.3, 0.7), target_w=max(20, room))
                await asyncio.sleep(_rand(80, 200) / 1000.0)
                over = True
            n = 1 if not per else max(1, min(8, int(abs(need) // per) + (1 if abs(need) % per > per * 0.25 else 0)))
            notch = await _notch_px()
            for j in range(n):
                await native_wheel(0, notch if need > 0 else -notch)
                notches += 1
                if j < n - 1:
                    await asyncio.sleep(_rand(30, 70) / 1000.0)
            before = s["st"]
            s = await _settled_row(info, i)
            if s is None:
                return None
            moved = abs(s["st"] - before)
            if moved < 0.5:
                return None   # the wheel did not scroll the box
            per = moved / n
            span = _row_span(s)
        return (span[0], span[1], s) if span else None

    async def _select_listbox(loc, box, info, idx):
        """A list box (multiple or size > 1): click the rows -- the first plainly (which also drops
        any other selection), the rest with Ctrl/Cmd held, scrolling the box with the wheel to bring
        a row into view the way a person does."""
        want = sorted(idx)
        opts = info["options"]
        if set(want) == {i for i, o in enumerate(opts) if o.get("selected")}:
            return [opts[i]["value"] for i in want]
        for k, i in enumerate(want):
            got = await _scroll_row_into_view(info, i)
            if got is None:
                return None
            top, bottom, s = got
            r = s["row"]
            px = _rand(r[0] + r[2] * 0.12, r[0] + min(r[2] * 0.6, 140))
            py = (top + bottom) / 2 + random.gauss(0, max(0.5, (bottom - top) * 0.12))
            py = min(max(py, top + 1), bottom - 1)
            await _glide(px, py, target_w=bottom - top)
            if k and native_kb_down is not None:
                # ControlOrMeta: Playwright sends the add-to-selection key the ENGINE honours
                await _press_at(px, py, normalize_click_options({"modifiers": ["ControlOrMeta"]}))
            else:
                await asyncio.sleep(_rand(40, 130) / 1000.0)
                await native_click(px, py, delay=click_hold(persona))
            await asyncio.sleep(_rand(150, 350) / 1000.0)
        final = await _listbox_row(info, want[0])
        if not final:
            return None
        got = [i for i, sel in enumerate(final["sel"]) if sel]
        return [opts[i]["value"] for i in want] if got == want else None

    async def _select_humanized(selector, value, options):
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
        box = await _box_of(loc, options.get("timeout"), options.get("force"), selector)
        info = await _describe(box, "select")
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
            return await _select_listbox(loc, box, info, idx)
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
        await _open_and_pick(selector, plan, at)
        if await loc.input_value() == opts[t]["value"]:
            return [opts[t]["value"]]
        # One correction from wherever the highlight landed (arrows only), then give up to native.
        again = await _redescribe(info, "select")
        if not again:
            return None
        fix = plan_select(labels, again.get("selectedIndex", -1), t, max_prefix=0)
        if fix is None:
            return None
        await _open_and_pick(selector, fix, at)
        return [opts[t]["value"]] if await loc.input_value() == opts[t]["value"] else None

    async def hselect_option(selector, value=None, **options):
        """page.select_option, humanized (see _select_humanized)."""
        try:
            res = await _select_humanized(selector, value, options)
            if res is not None:
                return res
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        return await native_page_select_option(selector, value, **options)

    async def _set_checked(orig, selector, checked, options):
        """check / uncheck / set_checked: a humanized click when the state must change, then the
        state verified. `trial` performs no action: Playwright's own trial runs."""
        if options.get("trial"):
            return await orig(selector, **options)
        loc = page.locator(selector).first
        # Errors propagate as Playwright's own (not a checkbox, timed out): the click below owns its
        # native fallback, so nothing here may click a second time.
        if await loc.is_checked(timeout=options.get("timeout")) == checked:
            return None
        await _targeted_click(native_page_click, selector, dict(options), no_click=False, native_opts=None)
        if await loc.is_checked() == checked:
            return None
        # One click did not change it (a page that cancels the click): Playwright's check raises here
        # rather than clicking again, and so does this.
        from playwright.async_api import Error as PlaywrightError
        raise PlaywrightError("Clicking the checkbox did not change its state")

    async def _reaches_point(orig, selector, box, x, y):
        """The covered-point check, through the browser's own hit test read in the isolated world:
        nothing is dispatched and the pointer does not move. It replaced a Playwright TRIAL action,
        which moves the REAL pointer onto the element and back and performs a press with its events
        blocked (verifier B: a mousemove inside the target, a 450-500 px jump back, then the glide,
        and a pointerdown window capture listeners could see). A covered point is looked at again
        for COVER_CHECK_MS (an overlay fading out). See _humanize._reaches_point."""
        deadline = asyncio.get_running_loop().time() + COVER_CHECK_MS / 1000.0
        hit = await _hit_in_box(box, x, y)
        while hit is False and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.08)
            hit = await _hit_in_box(box, x, y)
        if hit is not None:
            return hit
        # Only Playwright's trial can look inside a child frame. It moves the pointer: the cost of a
        # point the isolated world cannot see, never of an ordinary click.
        at = {"x": x - box["x"], "y": y - box["y"]}
        try:
            await orig(selector, trial=True, timeout=COVER_CHECK_MS, position=at)
            return True
        except Exception as e:  # noqa: BLE001
            if not _is_timeout(e) or "intercepts pointer events" in str(e):
                return False
        try:
            # Not covered, just not "stable" yet (slow first frames on a fresh headless browser):
            # one longer look.
            await orig(selector, trial=True, timeout=COVER_RETRY_MS, position=at)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _targeted_click(orig, selector, options, no_click, native_opts):
        """The humanized form of page.click / dblclick / hover (and the click inside check/uncheck).

        Every Playwright option is honoured: `button`, `click_count`, `delay` (the hold) and
        `modifiers` shape the press; `position` (relative to the PADDING box) replaces the persona's
        point; `force` skips the hit test; `trial` performs NO action (it runs Playwright's own trial).
        A Playwright TimeoutError propagates: falling back would wait the full timeout again."""
        o = normalize_click_options(options, await _platform())

        async def _native():
            if orig is None:
                raise RuntimeError("humanized click could not run")
            try:
                return await orig(selector, **(native_opts if native_opts is not None else options))
            finally:
                # Native click/hover genuinely move the browser pointer to the element: record it,
                # or the next wheel anchor glides away from the element the click just left.
                st["placed"] = True

        if o["trial"]:
            return await _native()
        try:
            timeout = options.get("timeout")   # None: the page's default, as Playwright would use
            loc = page.locator(selector).first
            # Playwright skips actionability with force: no wait then -- a target it would click
            # hidden goes native at once instead of timing out first.
            box = await _box_of(loc, timeout, o["force"], selector)
            if not box:
                raise _Native()
            if not o["force"] and not await loc.is_enabled():
                raise _Native()
            if o["position"]:
                # Playwright's position is relative to the PADDING box: add the border widths.
                info = await _describe(box, None)
                bl, bt = (info.get("bl", 0), info.get("bt", 0)) if info else (0, 0)
                x, y = box["x"] + bl + o["position"]["x"], box["y"] + bt + o["position"]["y"]
            else:
                x, y = click_point(box, st["pos"], persona)
            if not o["force"] and orig is not None:
                # Stability, then covered-by: 50 ms on, the element must still fill `box` (an
                # animation in flight does not) and own the click point -- no trusted press at a
                # point some overlay owns. Either failing goes native: Playwright waits for it.
                await asyncio.sleep(0.05)
                if not await _reaches_point(orig, selector, box, x, y):
                    raise _Native()
            if o["modifiers"] and native_kb_down is None:
                raise _Native()
            if no_click and o["modifiers"]:
                # hover with modifiers: Playwright holds them across the move
                held = []
                try:
                    for m in o["modifiers"]:
                        held.append(m)   # before the await (see _press_at)
                        await native_kb_down(m)
                        await asyncio.sleep(_rand(40, 110) / 1000.0)
                    await _glide(x, y)
                finally:
                    if held:
                        async def lift(keys=tuple(reversed(held))):
                            for m in keys:
                                await asyncio.sleep(_rand(20, 80) / 1000.0)
                                try:
                                    await native_kb_up(m)   # one failed keyup must not strand the others
                                except Exception:  # noqa: BLE001
                                    pass
                        await _release(lift())
                return None
            await _glide(x, y)
            if not no_click:
                if native_down is None or native_up is None:
                    await asyncio.sleep(_rand(40, 130) / 1000.0)
                    await native_click(x, y, delay=o["delay"] if o["delay"] is not None else click_hold(persona),
                                       button=o["button"], click_count=o["click_count"])
                else:
                    await _press_at(x, y, o)
            return None
        except _Native:
            pass
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        # outside the try, so a native call that raises is not run a second time by the handler
        return await _native()

    def _make_targeted(orig, no_click):
        async def wrapped(selector, **options):
            return await _targeted_click(orig, selector, options, no_click, None)
        return wrapped

    # The public entry points, each serialised by the page's lock (see `lock` above). The functions
    # behind them call one another only through these internal names, never through page.*.
    if native_page_type is not None:
        page.type = _serial(htype)
    if native_page_fill is not None:
        page.fill = _serial(hfill)
    if native_page_dblclick is not None:
        page.dblclick = _serial(hdblclick)
    if native_page_press is not None:
        page.press = _serial(hpress)
    if native_page_select_option is not None:
        page.select_option = _serial(hselect_option)
    for name, want in (("check", True), ("uncheck", False)):
        orig_c = getattr(page, name, None)
        if orig_c is not None:
            setattr(page, name, _serial(
                (lambda o, w: lambda selector, **opts: _set_checked(o, selector, w, opts))(orig_c, want)))
    orig_sc = getattr(page, "set_checked", None)
    if orig_sc is not None:
        async def hset_checked(selector, checked, **opts):
            async def orig(s, **o):
                return await orig_sc(s, checked, **o)
            return await _set_checked(orig, selector, bool(checked), opts)
        page.set_checked = _serial(hset_checked)
    if native_page_click is not None:
        page.click = _serial(_make_targeted(native_page_click, False))
    if native_page_hover is not None:
        page.hover = _serial(_make_targeted(native_page_hover, True))

    # Marker the Locator-class patch keys off: humanize is active for THIS page.
    page._clearcote_humanized = True
    _patch_locator_class()


# --------------------------------------------------------------------------- locator patch
_async_locator_patched = False


def _patch_locator_class():
    """Patch the async Locator class once so locator.* interactions route through the humanized
    page.* methods. A method routes only when the locator's page has humanize active AND the locator
    targets the main frame (not through a frame locator) AND it matches at most one element (locator
    actions are strict); anything else runs the original Playwright method. Once routed, every error
    propagates: the page-level wrappers own the native fallback. Composes safely if another library
    has already patched Locator — we chain to whatever was there before."""
    global _async_locator_patched
    if _async_locator_patched:
        return
    _async_locator_patched = True
    try:
        from playwright.async_api._generated import Locator
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

    async def _strict(self):
        """Locator actions are STRICT in Playwright: more than one match is an error. The page-level
        methods they route through take the first match, so check the count here and let the native
        call raise its own strict-mode error when it is not exactly one."""
        try:
            return await self.count() <= 1
        except Exception:  # noqa: BLE001
            return False

    def _sel(self):
        return self._impl_obj._selector

    # The options each page-level humanized method understands (see _humanize._patch_locator_class):
    # everything the caller passed that a page method accepts travels with it.
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

    async def _route(self, fn):
        # Once routed, every error propagates: the page-level wrappers own the native fallback, so
        # retrying the original locator method here ran the action a second (or third) time -- a
        # checkbox whose click is cancelled was clicked three times.
        if _on(self) and await _strict(self):
            return True, await fn()
        return False, None

    async def fill(self, value, **kw):
        done, r = await _route(self, lambda: self.page.fill(_sel(self), value, **_fwd(kw, _FILL)))
        return r if done else await o_fill(self, value, **kw)

    async def click(self, **kw):
        done, r = await _route(self, lambda: self.page.click(_sel(self), **_fwd(kw, _CLICK)))
        return r if done else await o_click(self, **kw)

    async def type_(self, text, **kw):
        done, r = await _route(self, lambda: self.page.type(_sel(self), text, **_fwd(kw, _TYPE)))
        return r if done else await o_type(self, text, **kw)

    async def dblclick(self, **kw):
        done, r = await _route(self, lambda: self.page.dblclick(_sel(self), **_fwd(kw, _DBL)))
        return r if done else await o_dblclick(self, **kw)

    async def hover(self, **kw):
        done, r = await _route(self, lambda: self.page.hover(_sel(self), **_fwd(kw, _HOVER)))
        return r if done else await o_hover(self, **kw)

    async def press(self, key, **kw):
        done, r = await _route(self, lambda: self.page.press(_sel(self), key, **_fwd(kw, _PRESS)))
        return r if done else await o_press(self, key, **kw)

    async def press_sequentially(self, text, **kw):
        done, r = await _route(self, lambda: self.page.type(_sel(self), text, **_fwd(kw, _TYPE)))
        return r if done else await o_press_seq(self, text, **kw)

    async def clear(self, **kw):
        done, r = await _route(self, lambda: self.page.fill(_sel(self), "", **_fwd(kw, _FILL)))
        return r if done else await o_clear(self, **kw)

    async def tap(self, **kw):
        done, r = await _route(self, lambda: self.page.click(_sel(self), **_fwd(kw, _HOVER)))
        return r if done else await o_tap(self, **kw)

    async def check(self, **kw):
        done, r = await _route(self, lambda: self.page.check(_sel(self), **_fwd(kw, _CHECK)))
        return r if done else await o_check(self, **kw)

    async def uncheck(self, **kw):
        done, r = await _route(self, lambda: self.page.uncheck(_sel(self), **_fwd(kw, _CHECK)))
        return r if done else await o_uncheck(self, **kw)

    async def set_checked(self, checked, **kw):
        done, r = await _route(self, lambda: self.page.set_checked(_sel(self), checked, **_fwd(kw, _CHECK)))
        return r if done else await o_set_checked(self, checked, **kw)

    async def select_option(self, value=None, **kw):
        """Delegate to the humanized page.select_option, like every other method here: one
        implementation behind the page method is what stops the two entry points drifting apart."""
        done, r = await _route(self, lambda: self.page.select_option(_sel(self), value, **_fwd(kw, _SELECT)))
        return r if done else await o_select_option(self, value, **kw)

    async def drag_to(self, target, **kw):
        if _on(self):
            try:
                page = self.page
                sb = await self.bounding_box()
                tb = await target.bounding_box()
                if sb and tb:
                    sx, sy = sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2
                    tx, ty = tb["x"] + tb["width"] / 2, tb["y"] + tb["height"] / 2
                    # Human endpoint dynamics (the two worst slider tells): grab hesitation AFTER
                    # pressing + a settle dwell BEFORE releasing, from the page's motor persona.
                    persona = getattr(page, "_clearcote_persona", None)
                    grab_ms, release_ms = drag_dwell(persona) if persona else (_rand(130, 360), _rand(90, 230))
                    await page.mouse.move(sx, sy)
                    await asyncio.sleep(_rand(100, 200) / 1000.0)
                    try:
                        await page.mouse.down()                 # native -> button held across the glide
                        await asyncio.sleep(grab_ms / 1000.0)   # grab hesitation
                        held_glide = getattr(page, "_clearcote_held_glide", None)
                        if held_glide:
                            await held_glide(tx, ty)            # humanized held-button drag + seating jiggle
                        else:
                            await page.mouse.move(tx, ty)
                        await asyncio.sleep(release_ms / 1000.0)  # pre-release settle before letting go
                    finally:
                        await _release(page.mouse.up())         # let go, even when cancelled mid-drag
                    return None
            except Exception:  # noqa: BLE001
                pass
        return await o_drag_to(self, target, **kw)

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


async def install_humanize_on_context(context, humanize=False, show_cursor=False, browser=None, seed=None):
    if not humanize and not show_cursor:
        return
    b = browser or context.browser
    if show_cursor:
        try:
            await context.add_init_script(CURSOR_OVERLAY)
        except Exception:  # noqa: BLE001
            pass
    if b is not None:
        context.on("page", lambda p: asyncio.ensure_future(attach_humanize(b, p, humanize, show_cursor, seed)))
        for p in context.pages:
            await attach_humanize(b, p, humanize, show_cursor, seed)


async def install_humanize(browser, humanize=False, show_cursor=False, seed=None):
    if not humanize and not show_cursor:
        return
    orig_new_page = browser.new_page
    orig_new_context = browser.new_context

    async def new_page(**kw):
        page = await orig_new_page(**kw)
        await attach_humanize(browser, page, humanize, show_cursor, seed)
        return page

    async def new_context(**kw):
        ctx = await orig_new_context(**kw)
        await install_humanize_on_context(ctx, humanize, show_cursor, browser, seed)
        return ctx

    browser.new_page = new_page
    browser.new_context = new_context
