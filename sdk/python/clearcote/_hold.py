"""Press and hold: one long, humanized button press on an element.

For controls that act only while the button stays down: a hold-to-confirm button, a long-press
menu, a "press and hold" verification button. ``page.press_and_hold(selector)`` /
``locator.press_and_hold()`` (with ``humanize=True``), or ``press_and_hold(locator)`` on any page.

What it does, in order:

  1. waits for the element to be visible and brings it into view: on a humanized main-frame page with
     the WHEEL (_scroll.bring_into_view: whole notches or a trackpad swipe to 25-60 % of the viewport;
     before 0.52 this was a programmatic scroll despite what this line said), falling back to the
     programmatic scroll -- reported -- only for a container the wheel cannot move; Playwright's scroll
     on any other page;
  2. moves the pointer to a point inside it (a persona-shaped point near the centre, or the
     caller's ``position``, relative to the PADDING box like Playwright's and click's) -- a humanized
     glide (Fitts width = the element's smaller side) when humanize is on. On a humanized page the
     point is first put to the same isolated hit test a click uses (retried COVER_CHECK_MS); a covered
     persona point moves to the first probe point (edges, top, bottom) the hit test proves;
  3. presses (after the humanized pre-press pause) and holds for ``duration`` ms, or until
     ``until()`` is true -- then lets go after a human reaction delay (160-350 ms), not on the
     same tick the condition flipped;
  4. while the button is down, a resting hand still moves the mouse now and then: sparse whole-pixel
     drift, never more than 2 px from the press point (``tremor=False`` turns it off). Every one of
     those moves is a trusted move with the button reported held.

The button is always released, also when the wait raises. The plan below is pure (no I/O), so
Node and .NET follow the same numbers.
"""

import asyncio
import inspect
import math
import random
import time

DEFAULT_HOLD_MS = (2800.0, 3800.0)
UNTIL_CAP_MS = 15000.0     # `until` with no `duration`: give up after this long
POLL_MS = 100.0            # how often `until` is asked
TREMOR_RADIUS_PX = 2       # drift never leaves this square around the press point
REACTION_MS = (160.0, 350.0)


def hold_ms(duration=None, until=None, rng=None):
    """How long the button stays down: the caller's ``duration`` (ms) exactly; else, with ``until``,
    the cap after which the hold gives up; else a fresh 2.8-3.8 s."""
    rng = rng or random.random
    if duration is not None:
        try:
            d = float(duration)
        except (TypeError, ValueError):
            raise ValueError("press_and_hold: duration must be a number of milliseconds") from None
        if not math.isfinite(d) or d <= 0:
            raise ValueError("press_and_hold: duration must be a positive number of milliseconds")
        return d
    if until is not None:
        return UNTIL_CAP_MS
    lo, hi = DEFAULT_HOLD_MS
    return lo + rng() * (hi - lo)


def tremor_plan(total_ms, rng=None):
    """Whole-pixel offsets from the press point, as (t_ms, dx, dy), while the button is held.
    A first move 250-650 ms in, then one every 250-900 ms; each moves one pixel on x and/or y and
    stays within TREMOR_RADIUS_PX of the press point (a step that would leave it goes back toward it).
    None in the last 120 ms: the hand is already letting go."""
    rng = rng or random.random
    out = []
    ox = oy = 0
    t = 250.0 + rng() * 400.0
    while t < total_ms - 120.0 and len(out) < 1000:
        while True:
            dx = int(rng() * 3) - 1
            dy = int(rng() * 3) - 1
            if dx or dy:
                break
        nx, ny = ox + dx, oy + dy
        if abs(nx) > TREMOR_RADIUS_PX:
            nx = ox - (1 if nx > 0 else -1)
        if abs(ny) > TREMOR_RADIUS_PX:
            ny = oy - (1 if ny > 0 else -1)
        ox, oy = nx, ny
        out.append((t, ox, oy))
        t += 250.0 + rng() * 650.0
    return out


def reaction_ms(rng=None):
    rng = rng or random.random
    return REACTION_MS[0] + rng() * (REACTION_MS[1] - REACTION_MS[0])


def hold_point(box, frm=None, persona=None, position=None, border=(0.0, 0.0)):
    """The viewport point to press: ``position`` (relative to the element's PADDING box, as Playwright's
    `position` and the humanized click's: ``border`` = (left, top) border widths, added), else the
    persona's click point when there is one, else the centre. Before 0.52 ``position`` was taken from
    the border box here while click took it from the padding box; the two now agree."""
    if position:
        bl, bt = border
        return box["x"] + bl + float(position["x"]), box["y"] + bt + float(position["y"])
    if persona is not None and frm is not None:
        from ._motion import click_point
        return click_point(box, frm, persona)
    return box["x"] + box["width"] / 2.0, box["y"] + box["height"] / 2.0


def _button(button):
    if button not in (None, "left", "right", "middle"):
        raise ValueError("press_and_hold: button must be 'left', 'right' or 'middle'")
    return button or "left"


def _main_frame_locator(loc):
    """Does ``loc`` target the page's main frame (so the humanized box/scroll helpers apply)?"""
    try:
        impl = loc._impl_obj
        if "internal:control=enter-frame" in (impl._selector or ""):
            return False
        return impl._frame.parent_frame is None
    except Exception:  # noqa: BLE001
        return False


def _selector_of(loc):
    try:
        return loc._impl_obj._selector
    except Exception:  # noqa: BLE001
        return None


def _border(page, box, position, iso):
    """(left, top) border widths of the element in ``box`` on a humanized page (its isolated-world
    read), so a caller's ``position`` is padding-box relative like click's; (0, 0) otherwise."""
    border_of = getattr(page, "_clearcote_border_of", None)
    if not (position and iso and border_of is not None):
        return (0.0, 0.0)
    try:
        return tuple(border_of(box))
    except Exception:  # noqa: BLE001
        return (0.0, 0.0)


async def _border_async(page, box, position, iso):
    border_of = getattr(page, "_clearcote_border_of", None)
    if not (position and iso and border_of is not None):
        return (0.0, 0.0)
    try:
        return tuple(await border_of(box))
    except Exception:  # noqa: BLE001
        return (0.0, 0.0)


def _checked_point(page, box, x, y, position, iso, sel):
    """The press point after the covered-point check (the humanized click's isolated hit test, retried
    COVER_CHECK_MS): the point itself when it reaches the element or the check cannot run; the first
    probe point the hit test proves when the PERSONA's point is covered (a caller's ``position`` is
    kept: it asked for that pixel); the point itself when nothing proves -- there is no native hold to
    fall back to, so the press goes where a person would press."""
    reaches = getattr(page, "_clearcote_reaches_point", None)
    if not iso or reaches is None:
        return x, y
    if reaches(box, x, y, sel) is not False or position:
        return x, y
    proven = getattr(page, "_clearcote_proven_point", None)
    alt = proven(box, sel) if proven is not None else None
    return alt if alt else (x, y)


async def _checked_point_async(page, box, x, y, position, iso, sel):
    reaches = getattr(page, "_clearcote_reaches_point", None)
    if not iso or reaches is None:
        return x, y
    if (await reaches(box, x, y, sel)) is not False or position:
        return x, y
    proven = getattr(page, "_clearcote_proven_point", None)
    alt = (await proven(box, sel)) if proven is not None else None
    return alt if alt else (x, y)


# --- sync ---------------------------------------------------------------------------------------

def press_and_hold(target, duration=None, *, position=None, button="left", timeout=None,
                   until=None, tremor=True):
    """Press and hold the element ``target`` (a Playwright Locator) for ``duration`` ms.

    ``until``: a callable polled every 100 ms while held; once it returns truthy the button is let
    go (after a human reaction delay). ``duration`` is then the longest the hold may last (15 s when
    not given). Returns True when the hold ran its full ``duration`` or ``until`` became true, False
    when ``until`` never did. ``position``: {"x", "y"} relative to the element, as in Playwright.
    ``timeout``: for the element to become visible (ms; None = the page default)."""
    btn = _button(button)
    total = hold_ms(duration, until)
    loc = target
    page = loc.page
    box = None
    box_of = getattr(page, "_clearcote_box_of", None)
    if box_of is not None and _main_frame_locator(loc):
        try:
            box = box_of(loc, timeout, _selector_of(loc))
        except Exception as e:  # noqa: BLE001
            if "Timeout" in type(e).__name__:
                raise
            box = None
    iso = bool(box)   # the humanizer's own box: its hit test, probe points and aimed glide apply
    if not box:
        loc.wait_for(state="visible", timeout=timeout)
        loc.scroll_into_view_if_needed(timeout=timeout)
        box = loc.bounding_box()
    if not box:
        raise RuntimeError("press_and_hold: the element has no box (not rendered)")
    persona = getattr(page, "_clearcote_persona", None)
    pos_of = getattr(page, "_clearcote_pointer", None)
    x, y = hold_point(box, pos_of() if pos_of else None, persona, position, _border(page, box, position, iso))
    x, y = float(round(x)), float(round(y))
    x, y = _checked_point(page, box, x, y, position, iso, _selector_of(loc))
    nudge = getattr(page, "_clearcote_nudge", None) or page.mouse.move

    aim = getattr(page, "_clearcote_aim_glide", None)
    if iso and aim is not None:
        aim(x, y, box)              # the humanized glide, Fitts width = the element's smaller side
    else:
        page.mouse.move(x, y)       # a humanized glide when humanize is on
    page.mouse.down(button=btn)     # humanized: the pre-press pause first
    t0 = time.monotonic()
    done = until is None
    try:
        drift = tremor_plan(total) if tremor else []
        i = 0
        next_poll = 0.0
        while True:
            now = (time.monotonic() - t0) * 1000.0
            if now >= total:
                break
            if until is not None and now >= next_poll:
                next_poll = now + POLL_MS
                if until():
                    done = True
                    time.sleep(reaction_ms() / 1000.0)
                    break
            while i < len(drift) and drift[i][0] <= now:
                nudge(x + drift[i][1], y + drift[i][2])
                i += 1
            wake = total
            if i < len(drift):
                wake = min(wake, drift[i][0])
            if until is not None:
                wake = min(wake, next_poll)
            time.sleep(max(0.0, min(wake - (time.monotonic() - t0) * 1000.0, 50.0)) / 1000.0)
    finally:
        page.mouse.up(button=btn)   # never leave the button down
    return done


# --- async --------------------------------------------------------------------------------------

async def _maybe_await(v):
    return await v if inspect.isawaitable(v) else v


async def press_and_hold_async(target, duration=None, *, position=None, button="left", timeout=None,
                               until=None, tremor=True):
    """Async mirror of :func:`press_and_hold` (``until`` may be a plain or an async callable).
    Cancelling the task still releases the button."""
    btn = _button(button)
    total = hold_ms(duration, until)
    loc = target
    page = loc.page
    box = None
    box_of = getattr(page, "_clearcote_box_of", None)
    if box_of is not None and _main_frame_locator(loc):
        try:
            box = await box_of(loc, timeout, _selector_of(loc))
        except Exception as e:  # noqa: BLE001
            if "Timeout" in type(e).__name__:
                raise
            box = None
    iso = bool(box)
    if not box:
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.scroll_into_view_if_needed(timeout=timeout)
        box = await loc.bounding_box()
    if not box:
        raise RuntimeError("press_and_hold: the element has no box (not rendered)")
    persona = getattr(page, "_clearcote_persona", None)
    pos_of = getattr(page, "_clearcote_pointer", None)
    x, y = hold_point(box, pos_of() if pos_of else None, persona, position, await _border_async(page, box, position, iso))
    x, y = float(round(x)), float(round(y))
    x, y = await _checked_point_async(page, box, x, y, position, iso, _selector_of(loc))
    nudge = getattr(page, "_clearcote_nudge", None) or page.mouse.move

    aim = getattr(page, "_clearcote_aim_glide", None)
    if iso and aim is not None:
        await aim(x, y, box)
    else:
        await page.mouse.move(x, y)
    await page.mouse.down(button=btn)
    t0 = time.monotonic()
    done = until is None
    try:
        drift = tremor_plan(total) if tremor else []
        i = 0
        next_poll = 0.0
        while True:
            now = (time.monotonic() - t0) * 1000.0
            if now >= total:
                break
            if until is not None and now >= next_poll:
                next_poll = now + POLL_MS
                if await _maybe_await(until()):
                    done = True
                    await asyncio.sleep(reaction_ms() / 1000.0)
                    break
            while i < len(drift) and drift[i][0] <= now:
                await nudge(x + drift[i][1], y + drift[i][2])
                i += 1
            wake = total
            if i < len(drift):
                wake = min(wake, drift[i][0])
            if until is not None:
                wake = min(wake, next_poll)
            await asyncio.sleep(max(0.0, min(wake - (time.monotonic() - t0) * 1000.0, 50.0)) / 1000.0)
    finally:
        await asyncio.shield(page.mouse.up(button=btn))
    return done
