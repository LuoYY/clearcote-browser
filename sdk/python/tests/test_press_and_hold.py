"""press_and_hold's plan (_hold.py): hold length, drift, release reaction, press point."""
import random

import pytest

from clearcote import _hold


def test_duration_is_taken_exactly():
    assert _hold.hold_ms(1234) == 1234.0
    assert _hold.hold_ms("500") == 500.0


@pytest.mark.parametrize("bad", [0, -5, float("nan"), float("inf"), "soon"])
def test_bad_duration_raises(bad):
    with pytest.raises(ValueError):
        _hold.hold_ms(bad)


def test_default_and_until_cap():
    for _ in range(200):
        assert 2800.0 <= _hold.hold_ms() <= 3800.0
    assert _hold.hold_ms(None, until=lambda: True) == _hold.UNTIL_CAP_MS
    assert _hold.hold_ms(4000, until=lambda: True) == 4000.0


def test_tremor_stays_near_the_press_point_and_is_sparse():
    rng = random.Random(7).random
    for _ in range(50):
        plan = _hold.tremor_plan(10000, rng)
        assert plan, "a 10 s hold drifts at least once"
        prev_t, prev = 0.0, (0, 0)
        for t, dx, dy in plan:
            assert t > prev_t and t < 10000 - 120
            assert abs(dx) <= _hold.TREMOR_RADIUS_PX and abs(dy) <= _hold.TREMOR_RADIUS_PX
            assert (dx, dy) != prev, "every planned move moves"
            assert abs(dx - prev[0]) <= 1 and abs(dy - prev[1]) <= 1, "one pixel at a time"
            prev_t, prev = t, (dx, dy)
        gaps = [b[0] - a[0] for a, b in zip(plan, plan[1:])]
        assert all(250 <= g <= 900 for g in gaps)
        assert 250 <= plan[0][0] <= 650


def test_short_hold_has_no_drift():
    # the first drift comes 250-650 ms in, and none in the last 120 ms
    assert _hold.tremor_plan(300, lambda: 0.0) == []
    assert _hold.tremor_plan(700, lambda: 0.99) == []


def test_reaction_range():
    rng = random.Random(3).random
    vals = [_hold.reaction_ms(rng) for _ in range(500)]
    assert min(vals) >= 160 and max(vals) <= 350


def test_hold_point():
    box = {"x": 100, "y": 50, "width": 200, "height": 40}
    assert _hold.hold_point(box) == (200.0, 70.0)
    assert _hold.hold_point(box, position={"x": 10, "y": 5}) == (110.0, 55.0)
    from clearcote._motion import make_persona
    x, y = _hold.hold_point(box, (0, 0), make_persona("hold-1"))
    assert 102 <= x <= 298 and 52 <= y <= 88


def test_bad_button_raises():
    with pytest.raises(ValueError):
        _hold._button("both")
    assert _hold._button(None) == "left"


class _FakeMouse:
    def __init__(self, log):
        self.log = log

    def move(self, x, y, **kw):
        self.log.append(("move", x, y))

    def down(self, **kw):
        self.log.append(("down", kw.get("button")))

    def up(self, **kw):
        self.log.append(("up", kw.get("button")))


class _FakePage:
    def __init__(self, log):
        self.mouse = _FakeMouse(log)


class _FakeLocator:
    def __init__(self, log):
        self.page = _FakePage(log)

    def wait_for(self, **kw):
        pass

    def scroll_into_view_if_needed(self, **kw):
        pass

    def bounding_box(self):
        return {"x": 10, "y": 10, "width": 100, "height": 30}


def test_releases_when_until_raises():
    log = []

    def boom():
        raise RuntimeError("until failed")

    with pytest.raises(RuntimeError):
        _hold.press_and_hold(_FakeLocator(log), 2000, until=boom)
    assert log[-1] == ("up", "left"), log


def test_until_releases_early_and_reports_it():
    import time
    log = []
    t0 = time.monotonic()
    calls = []

    def until():
        calls.append(1)
        return len(calls) >= 3
    assert _hold.press_and_hold(_FakeLocator(log), 5000, until=until, tremor=False) is True
    took = time.monotonic() - t0
    assert 0.3 <= took <= 1.0, took
    assert [e[0] for e in log] == ["move", "down", "up"]


def test_until_never_true_returns_false():
    log = []
    assert _hold.press_and_hold(_FakeLocator(log), 400, until=lambda: False, tremor=False) is False
    assert log[-1][0] == "up"
