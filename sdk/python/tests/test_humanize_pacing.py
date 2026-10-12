"""Wall-clock pacing of planned pointer paths (SC 210 lane P).

The planner returns ~130 samples ~6.6 ms apart for a ~1 s glide. The old dispatch loop awaited
mouse.move for each sample and THEN slept the planned gap, so the dispatch round trip (27 ms median on
PRO r38) was added to every sample: glides ran 3.5-5.4 s, ambient_motion(1500) 5.7x as long (SC 209).

These tests drive the real attach_humanize wrappers with a fake mouse whose move costs 0 ms or 25 ms
of a FAKE clock (time.sleep / time.perf_counter / asyncio.sleep advance it), so they are exact and
fast. On 0.48.0 the 25 ms duration checks and the ambient checks fail.
"""
import asyncio
import math
import time

import pytest

import clearcote._humanize as hz
import clearcote._humanize_async as hza
from clearcote import _pacing

LATENCIES = (0.0, 25.0)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        if s > 0:
            self.t += s

    async def asleep(self, s, *a, **k):
        if s and s > 0:
            self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(time, "sleep", c.sleep)
    monkeypatch.setattr(time, "perf_counter", c.now)
    monkeypatch.setattr(time, "monotonic", c.now)
    return c


class Mouse:
    def __init__(self, clock, latency_ms):
        self.clock, self.lat = clock, latency_ms / 1000.0
        self.log = []   # (op, t_ms, x, y)

    def _ev(self, op, x=None, y=None):
        self.clock.t += self.lat
        self.log.append((op, self.clock.t * 1000.0, x, y))

    def move(self, x, y, **k):
        self._ev("move", x, y)

    def down(self, **k):
        self._ev("down")

    def up(self, **k):
        self._ev("up")

    def click(self, *a, **k):
        pass

    def wheel(self, *a, **k):
        pass

    def dblclick(self, *a, **k):
        pass


class AsyncMouse(Mouse):
    async def move(self, x, y, **k):
        self._ev("move", x, y)

    async def down(self, **k):
        self._ev("down")

    async def up(self, **k):
        self._ev("up")

    async def click(self, *a, **k):
        pass

    async def wheel(self, *a, **k):
        pass

    async def dblclick(self, *a, **k):
        pass


class Keyboard:
    def __getattr__(self, name):
        return lambda *a, **k: None


class AsyncKeyboard:
    def __getattr__(self, name):
        async def f(*a, **k):
            return None
        return f


class Page:
    viewport_size = {"width": 1280, "height": 800}

    def __init__(self, mouse, keyboard):
        self.mouse, self.keyboard = mouse, keyboard
        self.main_frame = object()

    def on(self, *a, **k):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


def _spy_plans(monkeypatch, mod):
    plans = []
    real = mod.plan_move

    def spy(frm, *a, **k):
        p = real(frm, *a, **k)
        plans.append((tuple(frm), p))
        return p
    monkeypatch.setattr(mod, "plan_move", spy)
    return plans


def _moves(mouse, t0=None, t1=None):
    return [e for e in mouse.log if e[0] == "move" and (t0 is None or e[1] > t0) and (t1 is None or e[1] <= t1)]


def _max_gap(pts):
    return max((math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])), default=0.0)


def _check_walk(plan, moves, dur_ms, start):
    planned = sum(s[2] for s in plan)
    assert 0.8 * planned <= dur_ms <= 1.2 * planned, f"glide took {dur_ms:.0f} ms, planned {planned:.0f} ms"
    assert (moves[-1][2], moves[-1][3]) == (plan[-1][0], plan[-1][1]), "the final sample must land exactly"
    pts = [start] + [(m[2], m[3]) for m in moves]
    own = _max_gap([start] + [(s[0], s[1]) for s in plan])
    assert _max_gap(pts) <= max(40.0, own) + 1e-6, "a skipped stretch teleported the cursor"


@pytest.mark.parametrize("lat", LATENCIES)
def test_glide_lasts_its_planned_duration(clock, monkeypatch, lat):
    plans = _spy_plans(monkeypatch, hz)
    m = Mouse(clock, lat)
    page = Page(m, Keyboard())
    hz.attach_humanize(None, page, humanize=True, seed="pace-1")
    targets = [(900, 600), (150, 150), (640, 420), (1100, 120)]
    for (x, y) in targets:
        t0 = clock.now() * 1000.0
        n0 = len(plans)
        page.mouse.move(x, y)
        t1 = clock.now() * 1000.0
        assert len(plans) == n0 + 1
        frm, plan = plans[-1]
        mv = _moves(m, t0, t1)
        _check_walk(plan, mv, t1 - t0, frm)
        assert (mv[-1][2], mv[-1][3]) == (x, y)


@pytest.mark.parametrize("lat", LATENCIES)
def test_held_drag_keeps_button_order_and_duration(clock, monkeypatch, lat):
    plans = _spy_plans(monkeypatch, hz)
    m = Mouse(clock, lat)
    page = Page(m, Keyboard())
    hz.attach_humanize(None, page, humanize=True, seed="pace-2")
    page.mouse.move(200, 300)
    page.mouse.down()
    t0 = clock.now() * 1000.0
    page.mouse.move(560, 310)          # held leg: the slider-captcha shape
    t1 = clock.now() * 1000.0
    page.mouse.up()
    ops = [e[0] for e in m.log]
    d, u = ops.index("down"), ops.index("up")
    assert ops.count("down") == 1 and ops.count("up") == 1 and d < u
    leg = [e for e in m.log[d + 1:u]]
    assert leg and all(e[0] == "move" for e in leg)
    assert (leg[-1][2], leg[-1][3]) == (560, 310), "the button must come up at the target"
    frm, plan = plans[-1]
    _check_walk(plan, leg, t1 - t0, frm)


@pytest.mark.parametrize("lat", LATENCIES)
@pytest.mark.parametrize("ms", (600, 1500, 3000))
def test_ambient_motion_lasts_what_was_asked(clock, lat, ms):
    m = Mouse(clock, lat)
    page = Page(m, Keyboard())
    hz.attach_humanize(None, page, humanize=True, seed="pace-3")
    t0 = clock.now() * 1000.0
    page.ambient_motion(ms)
    took = clock.now() * 1000.0 - t0
    assert _moves(m), "ambient dispatched nothing"
    assert 0.8 * ms <= took <= 1.2 * ms, f"ambient_motion({ms}) took {took:.0f} ms"
    pts = [(e[2], e[3]) for e in _moves(m)]
    assert _max_gap(pts) <= 40.0 + 1e-6


@pytest.mark.parametrize("lat", LATENCIES)
def test_async_glide_and_ambient_are_paced(clock, monkeypatch, lat):
    monkeypatch.setattr(asyncio, "sleep", clock.asleep)
    plans = _spy_plans(monkeypatch, hza)
    m = AsyncMouse(clock, lat)
    page = Page(m, AsyncKeyboard())

    async def run():
        await hza.attach_humanize(None, page, humanize=True, seed="pace-4")
        for (x, y) in [(900, 600), (150, 150)]:
            t0 = clock.now() * 1000.0
            await page.mouse.move(x, y)
            t1 = clock.now() * 1000.0
            mv = _moves(m, t0, t1)
            frm, plan = plans[-1]
            _check_walk(plan, mv, t1 - t0, frm)
        t0 = clock.now() * 1000.0
        await page.ambient_motion(1500)
        took = clock.now() * 1000.0 - t0
        assert 1200 <= took <= 1800, f"async ambient_motion(1500) took {took:.0f} ms"

    asyncio.run(run())


# ---------------------------------------------------------------- the pacer itself

def _fake(lat_ms):
    c = Clock()
    sent = []

    def move(x, y):
        c.t += lat_ms / 1000.0
        sent.append((x, y, c.t))
    return c, sent, move


def test_pacer_never_drops_a_pinned_step_or_the_last():
    # 200 samples 2 ms apart with 25 ms dispatch: most must be skipped, but never one marked as
    # changing button state, and never the last.
    steps = [(i * 1.0, 0.0, 2.0, i in (37, 38, 120)) for i in range(200)]
    c, sent, move = _fake(25.0)
    _pacing.walk(steps, move, clock=c.now, sleep=c.sleep)
    xs = [s[0] for s in sent]
    assert len(xs) < 100
    for k in (37, 38, 120, 199):
        assert float(k) in xs
    assert xs == sorted(xs)


def test_pacer_on_time_dispatches_every_sample_at_its_planned_time():
    steps = [(i * 3.0, 0.0, 7.0) for i in range(50)]
    c, sent, move = _fake(0.0)
    t0 = c.t
    assert _pacing.walk(steps, move, clock=c.now, sleep=c.sleep) == 50
    for k, s in enumerate(sent):
        assert abs((s[2] - t0) * 1000.0 - 7.0 * k) < 1e-6
    assert abs((c.t - t0) * 1000.0 - 350.0) < 1e-6


def test_pacer_inserts_the_in_between_point_instead_of_a_long_jump():
    # 30 px per sample, 1 ms apart, 25 ms dispatch: skipping to the most recent due sample would jump
    # ~750 px; the pacer must keep every dispatched gap <= 40 px.
    steps = [(i * 30.0, 0.0, 1.0) for i in range(40)]
    c, sent, move = _fake(25.0)
    _pacing.walk(steps, move, clock=c.now, sleep=c.sleep)
    pts = [(s[0], s[1]) for s in sent]
    assert _max_gap([(0.0, 0.0)] + pts) <= 40.0
    assert pts[-1] == (39 * 30.0, 0.0)


def test_pacer_stops_at_a_failing_move():
    calls = []

    def move(x, y):
        calls.append(x)
        if len(calls) == 3:
            raise RuntimeError("Target closed")
    c = Clock()
    assert _pacing.walk([(i, 0, 5) for i in range(10)], move, clock=c.now, sleep=c.sleep) == 2
    assert len(calls) == 3


def test_trim_to_cuts_near_the_budget_at_a_slow_sample():
    from clearcote._motion import make_persona, plan_ambient
    p = make_persona("trim")
    for ms in (300, 1200, 1500, 4000):
        plan = plan_ambient((300, 300), {"width": 1280, "height": 800}, p, ms)
        cut = _pacing.trim_to(plan, ms)
        total = sum(s[2] for s in cut)
        assert 0.85 * ms <= total <= 1.1 * ms, (ms, total)
        assert cut == plan[:len(cut)], "trim only drops a tail; it never changes a sample"
