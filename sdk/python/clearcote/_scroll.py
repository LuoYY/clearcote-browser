"""Wheel-driven scrolling for the humanizer: scroll-into-view, the shared wheel anchor, trackpad streams.

Measured on r42 / SDK 0.51.0 (SC 232 review): before a humanized click, fill or hold on an off-screen
target the SDK scrolled PROGRAMMATICALLY -- ``scrollIntoViewIfNeeded`` in the isolated world or an
instant ``scrollBy`` -- so the page saw a ``scroll`` event with no wheel event before it, and the
target always arrived at the exact centre of the viewport. A hand brings a target in with the wheel:
whole notches (a mouse) or a decaying pixel stream (a trackpad), undershooting then correcting, and
leaves the target somewhere in the upper half of the viewport, never on the centre line.

Everything that decides WHAT to send is pure and shared with ``scroll.ts`` / ``Scroll.cs``
(``anchor_point``, ``wheel_drift``, ``scroll_need``, ``plan_trackpad`` ...; pinned by
``sdk/testdata/humanize_scroll_vectors.json``). :func:`bring_into_view` is the I/O loop; it talks to
the humanizer through a small duck-typed ``io`` object (see :class:`ScrollIO`), so the sync and async
Python humanizers, with their closures, both drive it.

Devices. Playwright's ``mouse.wheel`` sends pixel deltas, so a page sees ``deltaMode 0`` whatever the
persona; what tells a mouse from a trackpad is the SHAPE of the stream: a mouse persona sends whole
multiples of the platform notch (100 px Windows, 120 Linux, 40 macOS; ``_motion.WHEEL_NOTCH_PX``) in
finger-flick bursts, a trackpad persona sends 2-40 px events at 60-120 events/s following a min-jerk
velocity profile, then an inertia tail that decays exponentially for at least 300 ms.
"""

import math
import random
import time
from typing import Callable, Optional

from ._motion import _clamp, _js_round, _lerp, gauss_from, min_jerk, wheel_notches

# ---------------------------------------------------------------- the anchor (one set of numbers)
# Where a reading hand parks the pointer before scrolling: upper-middle of the viewport, gaussian
# spread, never the exact centre. The three SDKs used to differ (TS 0.48/0.36 clamp [0.18,0.82]; .NET
# 0.5/0.38 sd 0.12/0.10 clamp [0.12,0.88]x[0.12,0.75]); these are the Python numbers, now shared.
ANCHOR_X = (0.5, 0.10, 0.12, 0.88)     # centre frac, sd frac, clamp lo, clamp hi
ANCHOR_Y = (0.34, 0.09, 0.10, 0.62)
ANCHOR_TARGET_W = 140.0                # coarse placement, not a target acquisition
ANCHOR_MARGIN_PX = 8.0                 # a pointer this close to an edge is re-homed
# A hand resting on the mouse through a long scroll: ONE native move of a couple of px, so the wheel
# events do not all share one byte-identical clientX/clientY. (TS glided 3.5/2.5; .NET moved 2.2/2.2.)
DRIFT_SD = (1.5, 1.2)

# ---------------------------------------------------------------- scroll-into-view
TARGET_FRAC = (0.25, 0.60)             # where (share of the viewport height) the target ends up
UNDERSHOOT = (0.60, 0.90)              # the first flick covers this share of the need
NOTCH_BUDGET = 60                      # give up (programmatic fallback) after this many notches
SETTLE_MS = 50.0                       # two equal box reads this far apart = the scroll has settled
SETTLE_CAP_MS = 1200.0
MIN_MOVE_PX = 0.5                      # a burst that moved the box less than this did not scroll it
NOTCH_GAP_MS = (18.0, 55.0)            # inside a flick
FLICK_GAP_MS = (110.0, 320.0)          # the finger resets between flicks
FLICK_NOTCHES = (2, 5)
READ_PAUSE_RATE = 0.07
READ_PAUSE_MS = (150.0, 450.0)

# ---------------------------------------------------------------- trackpad
TRACKPAD_HZ = (60.0, 120.0)            # events per second during the finger phase
TRACKPAD_PX = (2.0, 40.0)              # per-event delta during the finger phase
INERTIA_MIN_MS = 300.0                 # the tail lasts at least this long
INERTIA_STOP_PX = 1.0                  # ... and runs until |delta| falls under this
TRACKPAD_PAUSE_RATE = 0.20
TRACKPAD_PAUSE_MS = (120.0, 400.0)


def anchor_point(vw, vh, rng: Optional[Callable[[], float]] = None):
    """The wheel anchor for a vw x vh viewport: x ~ 0.5w +- 0.10w clamped to [0.12w, 0.88w],
    y ~ 0.34h +- 0.09h clamped to [0.10h, 0.62h]. Two gaussians from ``rng`` (x first)."""
    if rng is None:
        rng = random.random
    cx, sx, lx, hx = ANCHOR_X
    cy, sy, ly, hy = ANCHOR_Y
    x = _clamp(vw * cx + gauss_from(rng) * vw * sx, vw * lx, vw * hx)
    y = _clamp(vh * cy + gauss_from(rng) * vh * sy, vh * ly, vh * hy)
    return x, y


def anchor_needed(pos, vw, vh, placed, held):
    """Should the pointer be re-homed before a wheel? Not with a button down (a move is a drag leg),
    not when it has been placed and sits inside the viewport margins."""
    if held:
        return False
    x, y = pos
    return not (placed and ANCHOR_MARGIN_PX <= x <= vw - ANCHOR_MARGIN_PX
                and ANCHOR_MARGIN_PX <= y <= vh - ANCHOR_MARGIN_PX)


def wheel_drift(rng: Optional[Callable[[], float]] = None):
    """(dx, dy) of the hand's drift between wheel events: N(0, 1.5) x N(0, 1.2) px, x first."""
    if rng is None:
        rng = random.random
    return gauss_from(rng) * DRIFT_SD[0], gauss_from(rng) * DRIFT_SD[1]


def input_device(platform: Optional[str], option=None) -> str:
    """"trackpad" or "mouse": the caller's option when it names one, else a trackpad for a macOS
    persona and a mouse for every other platform."""
    o = (str(option).strip().lower() if option is not None else "")
    if o in ("trackpad", "touchpad"):
        return "trackpad"
    if o == "mouse":
        return "mouse"
    return "trackpad" if (platform or "").lower() == "macos" else "mouse"


def target_frac(rng: Optional[Callable[[], float]] = None) -> float:
    """Where the target lands: U(0.25, 0.60) of the viewport height (Playwright: 0.5 exactly)."""
    if rng is None:
        rng = random.random
    return _lerp(TARGET_FRAC[0], TARGET_FRAC[1], rng())


def in_view(box, view):
    """Is ``box`` (x, y, width, height dict) wholly inside ``view`` ((x, y, w, h) tuple)?"""
    vx, vy, vw, vh = view
    return (box["x"] >= vx and box["y"] >= vy and box["x"] + box["width"] <= vx + vw
            and box["y"] + box["height"] <= vy + vh)


def scroll_need(box, view, frac):
    """(dx, dy) the content under the wheel must move (positive = wheel down / right) so that
    ``box`` shows inside ``view``: its centre at ``frac`` of the view's height when it is out
    vertically, centred horizontally when it is out that way; 0 on an axis already in view. A box
    taller than the view is brought in by its top."""
    vx, vy, vw, vh = view
    dx = dy = 0.0
    if box["y"] < vy or box["y"] + box["height"] > vy + vh:
        if box["height"] > vh * 0.9:
            dy = box["y"] - (vy + vh * 0.05)
        else:
            dy = (box["y"] + box["height"] / 2) - (vy + vh * frac)
    if box["x"] < vx or box["x"] + box["width"] > vx + vw:
        if box["width"] > vw * 0.9:
            dx = box["x"] - (vx + vw * 0.05)
        else:
            dx = (box["x"] + box["width"] / 2) - (vx + vw / 2)
    return dx, dy


def aim_need(box, view, frac, ax, ay):
    """(dx, dy) still to scroll so the target sits where :func:`scroll_need` aimed it, on the axes
    (``ax``, ``ay``) that were out of view when the scroll began -- also once it shows: a person who
    meant to bring it to the upper half does not stop the moment its bottom edge peeks in."""
    vx, vy, vw, vh = view
    dx = dy = 0.0
    if ay:
        dy = (box["y"] - (vy + vh * 0.05)) if box["height"] > vh * 0.9 else             (box["y"] + box["height"] / 2) - (vy + vh * frac)
    if ax:
        dx = (box["x"] - (vx + vw * 0.05)) if box["width"] > vw * 0.9 else             (box["x"] + box["width"] / 2) - (vx + vw / 2)
    return dx, dy


def undershoot(need_px, rng: Optional[Callable[[], float]] = None):
    """The first flick's distance: 60-90 % of the need (a hand undershoots and corrects)."""
    if rng is None:
        rng = random.random
    return need_px * _lerp(UNDERSHOOT[0], UNDERSHOOT[1], rng())


TRACKPAD_STROKE_PX = 900.0             # one swipe covers at most this; a longer scroll is several swipes
TRACKPAD_PEAK_PX = 38.0                # the finger phase's largest event stays under the 40 px bound
TRACKPAD_GAP_MS = (80.0, 200.0)        # the finger lifts and comes back for the next swipe


# Under this a scroll is a nudge: its inertia tail is not stretched to INERTIA_MIN_MS (a 300 ms glide
# would carry more than the whole distance).
TRACKPAD_NUDGE_PX = 60.0


def _swipe(dist, dt, tau_r, decay_ms):
    """One swipe of ``dist`` px: (finger, tail) lists of per-event magnitudes.

    The finger follows a min-jerk velocity bell over N events and lifts at ``tau_r`` of it, while still
    moving; the content then glides on, its velocity decaying as exp(-t / decay) until under 1 px, for at
    least INERTIA_MIN_MS (a nudge under TRACKPAD_NUDGE_PX: just its natural decay). The bell's amplitude is solved so finger + tail = ``dist``. Events of the
    finger phase under 2 px (the start of the bell) fold into their neighbour, as a sensor reports them."""
    def build(nfull, amp):
        k = max(2, int(math.floor(nfull * tau_r)))
        finger = []
        prev = 0.0
        for i in range(1, k + 1):
            e = min_jerk(i / nfull)
            finger.append(amp * (e - prev))
            prev = e
        vr = max(finger[-1], INERTIA_STOP_PX * 1.5)
        tau = decay_ms if dist < TRACKPAD_NUDGE_PX else max(
            decay_ms, INERTIA_MIN_MS * 1.15 / math.log(vr / INERTIA_STOP_PX))
        tail = []
        t = dt
        while len(tail) < 400:
            v = vr * math.exp(-t / tau)
            if v < INERTIA_STOP_PX:
                break
            tail.append(v)
            t += dt
        return finger, tail

    nfull = max(4, _js_round(dist / 12.0))
    finger, tail = [], []
    for _ in range(40):
        amp = dist
        for _i in range(6):   # amplitude so that finger + tail = dist
            finger, tail = build(nfull, amp)
            tot = sum(finger) + sum(tail)
            if tot <= 0:
                break
            amp *= dist / tot
        finger, tail = build(nfull, amp)
        if max(finger) <= TRACKPAD_PEAK_PX or nfull >= 600:
            break
        nfull = int(math.ceil(nfull * 1.15))
    # fold sub-threshold finger events forward
    folded = []
    carry = 0.0
    for v in finger:
        v += carry
        carry = 0.0
        if v < TRACKPAD_PX[0]:
            carry = v
        else:
            folded.append(v)
    if carry:
        if folded:
            folded[-1] += carry
        else:
            folded.append(carry)
    rest = dist - sum(folded) - sum(tail)   # float residue (and a tail cut at 400) goes to the finger
    folded[-1] += rest
    if folded[-1] < TRACKPAD_PX[0]:
        # too short for a glide of its own (a few px): a couple of plain finger events
        k = max(1, int(dist // 3.0))
        return [dist / k] * k, []
    return folded, tail


def plan_trackpad_strokes(dx, dy, rng: Optional[Callable[[], float]] = None):
    """The swipes of a trackpad scroll of (dx, dy): a list of {"finger": [(ex, ey, sleep_ms)],
    "tail": [...], "gap_ms": float}. See :func:`plan_trackpad`. Draw order (shared with scroll.ts /
    Scroll.cs): rate, pause?, pause length, pause swipe, pause position, then per swipe share, lift
    point, decay, gap; then one timing jitter per event, finger before tail, swipe by swipe."""
    if rng is None:
        rng = random.random
    d = math.hypot(dx, dy)
    if d < TRACKPAD_PX[0]:
        return [{"finger": [(dx, dy, 0.0)], "tail": [], "gap_ms": 0.0}] if (dx or dy) else []
    ux, uy = dx / d, dy / d
    hz = _lerp(TRACKPAD_HZ[0], TRACKPAD_HZ[1], rng())
    dt = 1000.0 / hz
    n = max(1, int(math.ceil(d / TRACKPAD_STROKE_PX)))
    pause = rng() < TRACKPAD_PAUSE_RATE
    pause_ms = _lerp(TRACKPAD_PAUSE_MS[0], TRACKPAD_PAUSE_MS[1], rng())
    pause_swipe = min(n - 1, int(rng() * n))
    pause_at = _lerp(0.25, 0.75, rng())
    specs = []
    for _ in range(n):
        specs.append((_lerp(0.9, 1.1, rng()), _lerp(0.55, 0.75, rng()), _lerp(140.0, 220.0, rng()),
                      _lerp(TRACKPAD_GAP_MS[0], TRACKPAD_GAP_MS[1], rng())))
    total_share = sum(sp[0] for sp in specs)
    out = []
    done = 0.0
    for si, (share, tau_r, decay, gap) in enumerate(specs):
        dist = d - done if si == n - 1 else d * share / total_share
        done += dist
        finger, tail = _swipe(dist, dt, tau_r, decay)
        stall = int(pause_at * (len(finger) - 1)) if (pause and si == pause_swipe and len(finger) > 2) else -1
        f_ev = []
        for i, v in enumerate(finger):
            sl = dt * _lerp(0.92, 1.08, rng())
            if i == stall:
                sl += pause_ms
            f_ev.append((ux * v, uy * v, sl))
        t_ev = [(ux * v, uy * v, dt * _lerp(0.92, 1.08, rng())) for v in tail]
        out.append({"finger": f_ev, "tail": t_ev, "gap_ms": gap if si < n - 1 else 0.0})
    return out


def plan_trackpad(dx, dy, rng: Optional[Callable[[], float]] = None):
    """A two-finger trackpad scroll of (dx, dy) px as (ex, ey, sleep_ms) wheel events, in order.

    Per swipe (one per <= 900 px): a finger phase of 2-40 px events at 60-120 events/s following a
    min-jerk velocity bell, lifted while still moving; then an inertia tail decaying exponentially for
    at least 300 ms, events until |delta| < 1 px. In 20 % of scrolls the finger stalls once mid-swipe
    for 120-400 ms. Deltas sum to (dx, dy); ``sleep_ms`` is the wait AFTER each event (a swipe's last
    event carries the gap before the next swipe)."""
    out = []
    for sw in plan_trackpad_strokes(dx, dy, rng):
        evs = sw["finger"] + sw["tail"]
        if evs and sw["gap_ms"]:
            ex, ey, sl = evs[-1]
            evs[-1] = (ex, ey, sl + sw["gap_ms"])
        out.extend(evs)
    return out


# ---------------------------------------------------------------- isolated-world read
# The nearest scrollable ancestor of a plain-CSS selector's first match (read in the isolated world, like
# every humanize read): the visible client rect of the container and which axes it scrolls, or null
# when the window is what scrolls it. A container with overflow:hidden is not scrollable by a wheel.
SCROLLER = """(a) => { let e;
  try { e = document.querySelector(a.sel); } catch (x) { return null; }
  if (!e) return null;
  const top = document.scrollingElement || document.documentElement;
  for (let p = e.parentElement; p && p !== top && p !== document.body; p = p.parentElement) {
    const s = getComputedStyle(p), sc = v => v === 'auto' || v === 'scroll' || v === 'overlay';
    const y = sc(s.overflowY) && p.scrollHeight > p.clientHeight + 1;
    const x = sc(s.overflowX) && p.scrollWidth > p.clientWidth + 1;
    if (x || y) { const r = p.getBoundingClientRect();
      return { rect: [r.x + p.clientLeft, r.y + p.clientTop, p.clientWidth, p.clientHeight], x: x, y: y }; }
  }
  return null; }"""


# ---------------------------------------------------------------- the I/O loop
class ScrollIO:
    """What :func:`bring_into_view` needs from a humanizer (duck-typed; this class only documents it):

    ``viewport()`` -> (vw, vh); ``pointer()`` -> (x, y); ``placed()`` / ``held()`` -> bool;
    ``glide(x, y, target_w)``; ``wheel(dx, dy)`` (one NATIVE wheel event); ``sleep(seconds)``;
    ``notch_px()`` -> int; ``device()`` -> "mouse" | "trackpad";
    ``scroller()`` -> None (the window scrolls) or {"rect": (x, y, w, h), "x": bool, "y": bool}, the
    nearest scrollable ancestor of the target; ``rng`` -> a () -> float."""


def _read_settled(box_fn, io):
    """The box once two reads SETTLE_MS apart agree (a smooth scroll still animating does not), or the
    last read after SETTLE_CAP_MS."""
    a = box_fn()
    t0 = time.monotonic()
    while True:
        io.sleep(SETTLE_MS / 1000.0)
        b = box_fn()
        if a is None or b is None:
            return b
        if abs(a["x"] - b["x"]) < 0.01 and abs(a["y"] - b["y"]) < 0.01:
            return b
        if (time.monotonic() - t0) * 1000.0 > SETTLE_CAP_MS:
            return b
        a = b


def _send_notches(io, n, axis, sign, rng):
    """``n`` whole notches on one axis, in finger-flick bursts with the hwheel cadence."""
    notch = io.notch_px()
    burst = int(_lerp(FLICK_NOTCHES[0], FLICK_NOTCHES[1] + 1, rng()))
    for k in range(n):
        io.wheel(sign * notch if axis == "x" else 0, sign * notch if axis == "y" else 0)
        if k == n - 1:
            break
        burst -= 1
        if burst == 0:
            io.sleep(_lerp(FLICK_GAP_MS[0], FLICK_GAP_MS[1], rng()) / 1000.0)
            burst = int(_lerp(FLICK_NOTCHES[0], FLICK_NOTCHES[1] + 1, rng()))
        else:
            io.sleep(_lerp(NOTCH_GAP_MS[0], NOTCH_GAP_MS[1], rng()) / 1000.0)
        if rng() < READ_PAUSE_RATE:
            io.sleep(_lerp(READ_PAUSE_MS[0], READ_PAUSE_MS[1], rng()) / 1000.0)


def _send_trackpad(io, dx, dy, rng):
    for ex, ey, sl in plan_trackpad(dx, dy, rng):
        io.wheel(ex, ey)
        if sl:
            io.sleep(sl / 1000.0)


def _view_of(io, scroller):
    vw, vh = io.viewport()
    if not scroller:
        return (0.0, 0.0, float(vw), float(vh))
    x, y, w, h = scroller["rect"]
    # the part of the container that shows: its rect clipped to the viewport
    x0, y0 = max(0.0, x), max(0.0, y)
    x1, y1 = min(float(vw), x + w), min(float(vh), y + h)
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


def _place_over(io, view, rng):
    """Park the pointer where the wheel will act: the anchor distribution over ``view`` (the
    viewport, or a nested scroller's visible part), unless it already sits there and no button is down."""
    vx, vy, vw, vh = view
    if vw <= 0 or vh <= 0:
        return
    px, py = io.pointer()
    if not anchor_needed((px - vx, py - vy), vw, vh, io.placed(), io.held()):
        return
    ax, ay = anchor_point(vw, vh, rng)
    io.glide(vx + ax, vy + ay, ANCHOR_TARGET_W)
    io.sleep(_lerp(60.0, 180.0, rng()) / 1000.0)   # eyes land before the finger rolls the wheel


def bring_into_view(io, box_fn, frac=None, rng: Optional[Callable[[], float]] = None, scroller_fn=None):
    """Wheel the target whose current box ``box_fn()`` returns into view.

    Computes the distance from the box, parks the pointer at the anchor (or over the nearest
    scrollable ancestor when ``scroller_fn()`` names one; a container itself off-screen is wheeled in
    first), sends whole notches (a mouse persona) or a trackpad stream covering 60-90 % of the need,
    waits for the scroll to settle (two equal reads 50 ms apart), re-reads the box and corrects, up to
    ``NOTCH_BUDGET`` notches. Returns ``(box, ok, notches)``: ``ok`` False when the wheel did not move
    the target (a non-scrollable container) or the budget ran out -- the caller's cue for the
    programmatic fallback, which it reports."""
    if rng is None:
        rng = io.rng if getattr(io, "rng", None) else random.random
    box = box_fn()
    if box is None:
        return None, False, 0
    scroller = scroller_fn() if scroller_fn else None
    if scroller:
        srect = scroller["rect"]
        vw, vh = io.viewport()
        sbox = {"x": srect[0], "y": srect[1], "width": srect[2], "height": srect[3]}
        if not in_view(sbox, (0.0, 0.0, float(vw), float(vh))):
            # the container must show before the wheel can act on it
            def _srect():
                s = scroller_fn()
                return {"x": s["rect"][0], "y": s["rect"][1], "width": s["rect"][2], "height": s["rect"][3]} if s else None
            _, ok0, _ = bring_into_view(io, _srect, frac, rng, None)
            if not ok0:
                return box_fn(), False, 0
            scroller = scroller_fn()
            box = box_fn()
            if box is None:
                return None, False, 0
    view = _view_of(io, scroller)
    if in_view(box, view):
        return box, True, 0
    if frac is None:
        frac = target_frac(rng)
    notches = 0
    first = True
    device = io.device()
    notch = io.notch_px()
    placed = False
    ax, ay = (v != 0 for v in scroll_need(box, view, frac))
    if scroller:
        ax, ay = ax and bool(scroller.get("x")), ay and bool(scroller.get("y"))
    tol = notch * 0.5 if device != "trackpad" else 6.0
    shown_rounds = 0
    while notches < NOTCH_BUDGET:
        dx, dy = aim_need(box, view, frac, ax, ay)
        if in_view(box, view):
            # in view: done once it is about where it was aimed (a whole notch cannot do better than
            # half a notch), or after two corrections
            if (abs(dx) < tol and abs(dy) < tol) or shown_rounds >= 2:
                return box, True, notches
            shown_rounds += 1
        elif abs(dx) < 1 and abs(dy) < 1:
            return box, False, notches
        if not placed:
            _place_over(io, view, rng)
            placed = True
        ask_x = undershoot(dx, rng) if first else dx
        ask_y = undershoot(dy, rng) if first else dy
        first = False
        if device == "trackpad":
            _send_trackpad(io, ask_x, ask_y, rng)
            notches += max(1, _js_round(math.hypot(ask_x, ask_y) / notch))
        else:
            ny = wheel_notches(ask_y, notch) if abs(ask_y) >= tol else 0
            nx = wheel_notches(ask_x, notch) if abs(ask_x) >= tol else 0
            ny = min(ny, NOTCH_BUDGET - notches)
            if ny:
                _send_notches(io, ny, "y", 1 if ask_y > 0 else -1, rng)
                notches += ny
            nx = min(nx, NOTCH_BUDGET - notches)
            if nx:
                _send_notches(io, nx, "x", 1 if ask_x > 0 else -1, rng)
                notches += nx
            if not (nx or ny):
                return box, in_view(box, view), notches
        before = box
        box = _read_settled(box_fn, io)
        if box is None:
            return None, False, notches
        if scroller:
            scroller = scroller_fn() if scroller_fn else scroller
            view = _view_of(io, scroller)
        if abs(box["x"] - before["x"]) < MIN_MOVE_PX and abs(box["y"] - before["y"]) < MIN_MOVE_PX:
            # the wheel did not move it: the end of the scroll (fine when it shows), or not a
            # scrollable container (the caller falls back)
            return box, in_view(box, view), notches
    return box, False, notches


# ---------------------------------------------------------------- async mirror (the same loop, awaited)
async def _read_settled_async(box_fn, io):
    """The box once two reads SETTLE_MS apart agree (a smooth scroll still animating does not), or the
    last read after SETTLE_CAP_MS."""
    a = await box_fn()
    t0 = time.monotonic()
    while True:
        await io.sleep(SETTLE_MS / 1000.0)
        b = await box_fn()
        if a is None or b is None:
            return b
        if abs(a["x"] - b["x"]) < 0.01 and abs(a["y"] - b["y"]) < 0.01:
            return b
        if (time.monotonic() - t0) * 1000.0 > SETTLE_CAP_MS:
            return b
        a = b


async def _send_notches_async(io, n, axis, sign, rng):
    """``n`` whole notches on one axis, in finger-flick bursts with the hwheel cadence."""
    notch = await io.notch_px()
    burst = int(_lerp(FLICK_NOTCHES[0], FLICK_NOTCHES[1] + 1, rng()))
    for k in range(n):
        await io.wheel(sign * notch if axis == "x" else 0, sign * notch if axis == "y" else 0)
        if k == n - 1:
            break
        burst -= 1
        if burst == 0:
            await io.sleep(_lerp(FLICK_GAP_MS[0], FLICK_GAP_MS[1], rng()) / 1000.0)
            burst = int(_lerp(FLICK_NOTCHES[0], FLICK_NOTCHES[1] + 1, rng()))
        else:
            await io.sleep(_lerp(NOTCH_GAP_MS[0], NOTCH_GAP_MS[1], rng()) / 1000.0)
        if rng() < READ_PAUSE_RATE:
            await io.sleep(_lerp(READ_PAUSE_MS[0], READ_PAUSE_MS[1], rng()) / 1000.0)


async def _send_trackpad_async(io, dx, dy, rng):
    for ex, ey, sl in plan_trackpad(dx, dy, rng):
        await io.wheel(ex, ey)
        if sl:
            await io.sleep(sl / 1000.0)


async def _view_of_async(io, scroller):
    vw, vh = await io.viewport()
    if not scroller:
        return (0.0, 0.0, float(vw), float(vh))
    x, y, w, h = scroller["rect"]
    # the part of the container that shows: its rect clipped to the viewport
    x0, y0 = max(0.0, x), max(0.0, y)
    x1, y1 = min(float(vw), x + w), min(float(vh), y + h)
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


async def _place_over_async(io, view, rng):
    """Park the pointer where the wheel will act: the anchor distribution over ``view`` (the
    viewport, or a nested scroller's visible part), unless it already sits there and no button is down."""
    vx, vy, vw, vh = view
    if vw <= 0 or vh <= 0:
        return
    px, py = io.pointer()
    if not anchor_needed((px - vx, py - vy), vw, vh, io.placed(), io.held()):
        return
    ax, ay = anchor_point(vw, vh, rng)
    await io.glide(vx + ax, vy + ay, ANCHOR_TARGET_W)
    await io.sleep(_lerp(60.0, 180.0, rng()) / 1000.0)   # eyes land before the finger rolls the wheel


async def bring_into_view_async(io, box_fn, frac=None, rng: Optional[Callable[[], float]] = None, scroller_fn=None):
    """Wheel the target whose current box ``await box_fn()`` returns into view.

    Computes the distance from the box, parks the pointer at the anchor (or over the nearest
    scrollable ancestor when ``await scroller_fn()`` names one; a container itself off-screen is wheeled in
    first), sends whole notches (a mouse persona) or a trackpad stream covering 60-90 % of the need,
    waits for the scroll to settle (two equal reads 50 ms apart), re-reads the box and corrects, up to
    ``NOTCH_BUDGET`` notches. Returns ``(box, ok, notches)``: ``ok`` False when the wheel did not move
    the target (a non-scrollable container) or the budget ran out -- the caller's cue for the
    programmatic fallback, which it reports."""
    if rng is None:
        rng = io.rng if getattr(io, "rng", None) else random.random
    box = await box_fn()
    if box is None:
        return None, False, 0
    scroller = await scroller_fn() if scroller_fn else None
    if scroller:
        srect = scroller["rect"]
        vw, vh = await io.viewport()
        sbox = {"x": srect[0], "y": srect[1], "width": srect[2], "height": srect[3]}
        if not in_view(sbox, (0.0, 0.0, float(vw), float(vh))):
            # the container must show before the wheel can act on it
            async def _srect():
                s = await scroller_fn()
                return {"x": s["rect"][0], "y": s["rect"][1], "width": s["rect"][2], "height": s["rect"][3]} if s else None
            _, ok0, _ = await bring_into_view_async(io, _srect, frac, rng, None)
            if not ok0:
                return await box_fn(), False, 0
            scroller = await scroller_fn()
            box = await box_fn()
            if box is None:
                return None, False, 0
    view = await _view_of_async(io, scroller)
    if in_view(box, view):
        return box, True, 0
    if frac is None:
        frac = target_frac(rng)
    notches = 0
    first = True
    device = await io.device()
    notch = await io.notch_px()
    placed = False
    ax, ay = (v != 0 for v in scroll_need(box, view, frac))
    if scroller:
        ax, ay = ax and bool(scroller.get("x")), ay and bool(scroller.get("y"))
    tol = notch * 0.5 if device != "trackpad" else 6.0
    shown_rounds = 0
    while notches < NOTCH_BUDGET:
        dx, dy = aim_need(box, view, frac, ax, ay)
        if in_view(box, view):
            # in view: done once it is about where it was aimed (a whole notch cannot do better than
            # half a notch), or after two corrections
            if (abs(dx) < tol and abs(dy) < tol) or shown_rounds >= 2:
                return box, True, notches
            shown_rounds += 1
        elif abs(dx) < 1 and abs(dy) < 1:
            return box, False, notches
        if not placed:
            await _place_over_async(io, view, rng)
            placed = True
        ask_x = undershoot(dx, rng) if first else dx
        ask_y = undershoot(dy, rng) if first else dy
        first = False
        if device == "trackpad":
            await _send_trackpad_async(io, ask_x, ask_y, rng)
            notches += max(1, _js_round(math.hypot(ask_x, ask_y) / notch))
        else:
            ny = wheel_notches(ask_y, notch) if abs(ask_y) >= tol else 0
            nx = wheel_notches(ask_x, notch) if abs(ask_x) >= tol else 0
            ny = min(ny, NOTCH_BUDGET - notches)
            if ny:
                await _send_notches_async(io, ny, "y", 1 if ask_y > 0 else -1, rng)
                notches += ny
            nx = min(nx, NOTCH_BUDGET - notches)
            if nx:
                await _send_notches_async(io, nx, "x", 1 if ask_x > 0 else -1, rng)
                notches += nx
            if not (nx or ny):
                return box, in_view(box, view), notches
        before = box
        box = await _read_settled_async(box_fn, io)
        if box is None:
            return None, False, notches
        if scroller:
            scroller = await scroller_fn() if scroller_fn else scroller
            view = await _view_of_async(io, scroller)
        if abs(box["x"] - before["x"]) < MIN_MOVE_PX and abs(box["y"] - before["y"]) < MIN_MOVE_PX:
            # the wheel did not move it: the end of the scroll (fine when it shows), or not a
            # scrollable container (the caller falls back)
            return box, in_view(box, view), notches
    return box, False, notches
