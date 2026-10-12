"""Wall-clock pacing for planned pointer paths.

The motion planner (``_motion.plan_move`` / ``plan_ambient``) returns samples ``(x, y, sleep_ms)``
at the persona's device rate (~6-8 ms apart). Walking them as "dispatch, then sleep the planned
gap" adds the dispatch round trip to EVERY sample: measured on PRO r38, one ``mouse.move`` costs
27 ms median, so a 1.05 s planned glide took 3.5-5.4 s and the page saw ~25 pointermove/s
(SC 209). Real hands do not slow down because the driver is slow.

``walk`` schedules each sample at its planned offset from the start (the cumulative sum of the
sleeps before it) and keeps that clock:

* on time   -> sleep until the sample's planned time, then dispatch it;
* behind    -> skip the samples whose time has already passed and dispatch the most recent due
               one, so the path keeps the plan's shape and the move keeps the plan's duration;
* never skipped: the final sample (the move must land exactly where it was aimed), and a sample
  marked as changing button state;
* never a teleport: a skip may not put more than ``max_jump`` (40 px) between two dispatched points,
  and no more than IDLE_HOP_PX (20 px) after IDLE_MS (40 ms) with nothing sent; a longer hop dispatches
  the in-between sample instead, so a late walk stretches in time rather than jumping (SC 210 lab v2:
  the old cap, 3 px/ms x the time since the last dispatch, let a slow dispatch under load turn into
  35 hops of 60-103 px a few ms apart -- and an r40 engine walks any hop >= 24 px after >= 40 ms idle);
* a stall is a pause: a single dispatch slower than ``STALL_MS`` shifts the schedule by the excess
  instead of being caught up by skipping.

Sleeps are local (``time.sleep`` / ``asyncio.sleep``), never ``page.wait_for_timeout``: that is a
CDP round trip per call, protocol traffic a detector can score inside the humanize path.

Nothing here changes what the planner returns: the samples, their coordinates and their timing are
the planner's; this module only decides WHEN each one goes out and which late ones are dropped.
"""
import asyncio
import math
import time

MAX_JUMP_PX = 40.0
# A single dispatch slower than this is a STALL (the page or the box was busy), not latency: nothing
# reached the page while it lasted, so the plan pauses with it instead of skipping ahead afterwards.
STALL_MS = 60.0
# A fast hand: ~3 px/ms (a quick flick across a 1080p screen in ~0.6 s).
FAST_PX_PER_MS = 3.0
# After this long with nothing dispatched, a skip may hop at most IDLE_HOP_PX: what a hand moving at a
# moderate 2.5 px/ms covers in one 125 Hz report (8 ms), and below the 24 px an r40 engine walks.
IDLE_MS = 40.0
IDLE_HOP_PX = 20.0


def schedule(steps):
    """Planned dispatch offsets (ms from the start) of every step, and the planned total."""
    due = []
    t = 0.0
    for s in steps:
        due.append(t)
        t += max(0.0, float(s[2]))
    return due, t


def _default_pinned(step):
    # A planner sample is (x, y, sleep_ms); a 4th truthy field marks one that changes button state.
    return len(step) > 3 and bool(step[3])


def next_index(steps, due, i, last, elapsed_ms, max_jump=MAX_JUMP_PX, pinned=_default_pinned):
    """The step to dispatch next, given step ``i`` is the first not yet sent and ``elapsed_ms`` have
    passed since the start. ``last`` is the last dispatched (x, y), or None."""
    n = len(steps)
    j = i
    # Behind: advance to the most recent due step, but never past a pinned one.
    while j + 1 < n and due[j + 1] <= elapsed_ms and not pinned(steps[j]):
        j += 1
    if last is not None:
        while j > i and math.hypot(steps[j][0] - last[0], steps[j][1] - last[1]) > max_jump:
            j -= 1
    return j


def _jump_cap(max_jump, dt_ms):
    """The largest hop a SKIP may make between two dispatched points ``dt_ms`` apart: ``max_jump``, and
    IDLE_HOP_PX after an idle gap. Never more for a longer gap: a walk that fell behind stretches in time
    instead of catching up with a jump (the planner's own consecutive samples always go out)."""
    return max_jump if dt_ms < IDLE_MS else min(max_jump, IDLE_HOP_PX)


def walk(steps, move, clock=None, sleep=None, max_jump=MAX_JUMP_PX, pinned=_default_pinned):
    """Dispatch ``steps`` through ``move(x, y)`` against a wall clock (see module doc). Stops at the
    first ``move`` that raises. Returns the number of samples dispatched."""
    clock = clock or time.perf_counter
    sleep = sleep or time.sleep
    if not steps:
        return 0
    due, total = schedule(steps)
    t0 = clock()
    n = len(steps)
    i = 0
    last = None
    t_last = t0
    sent = 0
    while i < n:
        el = (clock() - t0) * 1000.0
        if el < due[i]:
            sleep((due[i] - el) / 1000.0)
        else:
            i = next_index(steps, due, i, last, el, _jump_cap(max_jump, (clock() - t_last) * 1000.0), pinned)
        x, y = steps[i][0], steps[i][1]
        tb = clock()
        try:
            move(x, y)
        except Exception:  # noqa: BLE001 - page gone / navigating: stop walking
            return sent
        sent += 1
        t0 += _stall_s(tb, clock())
        last, t_last = (x, y), tb
        i += 1
    el = (clock() - t0) * 1000.0
    if el < total:
        sleep((total - el) / 1000.0)   # the last sample's own dwell (a settle hold, a frame)
    return sent


async def walk_async(steps, move, clock=None, sleep=None, max_jump=MAX_JUMP_PX, pinned=_default_pinned):
    """``walk`` for the async API: ``move`` and ``sleep`` are awaited."""
    clock = clock or time.perf_counter
    sleep = sleep or asyncio.sleep
    if not steps:
        return 0
    due, total = schedule(steps)
    t0 = clock()
    n = len(steps)
    i = 0
    last = None
    t_last = t0
    sent = 0
    while i < n:
        el = (clock() - t0) * 1000.0
        if el < due[i]:
            await sleep((due[i] - el) / 1000.0)
        else:
            i = next_index(steps, due, i, last, el, _jump_cap(max_jump, (clock() - t_last) * 1000.0), pinned)
        x, y = steps[i][0], steps[i][1]
        tb = clock()
        try:
            await move(x, y)
        except Exception:  # noqa: BLE001
            return sent
        sent += 1
        t0 += _stall_s(tb, clock())
        last, t_last = (x, y), tb
        i += 1
    el = (clock() - t0) * 1000.0
    if el < total:
        await sleep((total - el) / 1000.0)
    return sent


# How far inside the viewport a planned path is kept (SC 210 fix 2, verifier B: the planner's overshoot
# arcs left the window near its edges -- x past the right edge, y < 0 -- and the page saw no events for
# 138-383 ms while the pointer was outside).
VIEW_MARGIN_PX = 2.0


def keep_in_view(steps, vw, vh, margin=VIEW_MARGIN_PX):
    """``steps`` with every sample but the last moved inside the viewport ``vw`` x ``vh`` (``margin`` px
    in). The last sample -- where the move was aimed -- is never changed, and a move aimed outside the
    viewport (the caller's own choice) is returned as it is. Only coordinates change: the timing, and
    any further fields of a step, are kept."""
    if not steps or not vw or not vh:
        return steps
    tx, ty = steps[-1][0], steps[-1][1]
    if not (0 <= tx <= vw and 0 <= ty <= vh):
        return steps
    lo_x, hi_x = min(margin, tx), max(vw - margin, tx)
    lo_y, hi_y = min(margin, ty), max(vh - margin, ty)
    out = []
    for k, st in enumerate(steps):
        if k == len(steps) - 1:
            out.append(st)
            continue
        x = min(max(st[0], lo_x), hi_x)
        y = min(max(st[1], lo_y), hi_y)
        out.append(st if (x, y) == (st[0], st[1]) else (x, y) + tuple(st[2:]))
    return out


def _stall_s(tb, ta):
    """Seconds to shift the schedule by after a dispatch that ran from ``tb`` to ``ta``.

    Measured on PRO r38 under load: one move answered after 523 ms. Catching that up by skipping
    coarsened the rest of the glide to maximum hops, and across the fixture's iframe the page saw a
    110 px jump. A single dispatch slower than STALL_MS is a stall: nothing reached the page while it
    lasted, so the plan pauses with it and only STALL_MS is caught up."""
    took = (ta - tb) * 1000.0
    return (took - STALL_MS) / 1000.0 if took > STALL_MS else 0.0


def _speed(steps, k):
    """px/ms arriving at sample k (0 for the first)."""
    if k == 0:
        return 0.0
    a, b = steps[k - 1], steps[k]
    return math.hypot(b[0] - a[0], b[1] - a[1]) / max(1e-3, float(a[2]))


def trim_to(steps, ms, lo=0.85, hi=1.10, stretch=2.0):
    """Cut an ambient plan so it lasts about ``ms``.

    ``plan_ambient`` keeps adding whole excursions + rests until its budget is spent and draws each
    rest sample's gap from 0.9-1.4x the frame time it counts, so its plan runs well past what the
    caller asked for (ambient_motion(1500) planned up to ~2.5 s). Of the samples whose end falls in
    [lo*ms, hi*ms], keep up to the slowest one -- a point inside a rest or the tail of a move. Only a
    tail is dropped; no sample is changed. A plan already no longer than hi*ms is returned whole.

    The one exception to "about ms": when the whole window lies inside the first excursion (a short
    ask, ~300 ms, against a ~0.5-1 s glide) every candidate is mid-flight, and a pointer that stops dead
    at speed is a kinematic no hand produces. Then the cut moves forward to where that move has slowed
    down (<= 10 % of the plan's peak speed), as long as that is within ``stretch`` x ms."""
    if not steps or ms <= 0:
        return steps
    due, total = schedule(steps)
    if total <= hi * ms:
        return steps
    end = [due[k] + max(0.0, float(steps[k][2])) for k in range(len(steps))]
    best, best_v = None, None
    for k in range(len(steps)):
        if end[k] < lo * ms:
            continue
        if end[k] > hi * ms:
            break
        v = _speed(steps, k)
        if best_v is None or v < best_v:
            best, best_v = k, v
    if best is None:
        # One sample spans the whole window (a long settle dwell): cut at the last one before it.
        ends = [k for k in range(len(steps)) if end[k] <= hi * ms]
        best = ends[-1] if ends else 0
    peak = max(_speed(steps, k) for k in range(len(steps)))
    if peak > 0 and _speed(steps, best) > 0.3 * peak:
        # First the rest of the +-20 % the caller was promised, then forward.
        wide = [k for k in range(len(steps)) if 0.8 * ms <= end[k] <= 1.15 * ms]
        if wide:
            k = min(wide, key=lambda k: _speed(steps, k))
            if _speed(steps, k) <= 0.3 * peak:
                return steps[:k + 1]
        for k in range(best + 1, len(steps)):
            if end[k] > stretch * ms:
                break
            if _speed(steps, k) <= 0.1 * peak:
                best = k
                break
    return steps[:best + 1]
