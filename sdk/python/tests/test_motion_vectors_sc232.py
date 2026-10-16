"""SC 232 lane G: cross-SDK vectors for plan_drag, plan_ambient, the persona timings and the wheel
scroll-anchor model (sdk/testdata/humanize_motion_vectors.json: drags / ambient / timings / wheel_anchor).
node/test/motion-vectors-sc232.test.ts and MotionVectorsSc232Tests.cs read the same file.

The vectors pin what origin/main ceb7983 plans; a planner change regenerates them with
tests/gen_motion_vectors.py together with the matching change in all three SDKs.

wheel_anchor: the anchor is inside the humanize closures today (and differs per SDK, see the generator).
These tests check a pure ``_motion.wheel_anchor_point(vw, vh, rng)`` once lane E adds it, and skip with
"API missing" until then.
"""
import json
import os

import pytest

from clearcote import _motion
from clearcote._motion import click_hold, drag_dwell, key_dwell, make_persona, mulberry32, plan_ambient, plan_drag, rollover_rate

VECTORS = os.path.join(os.path.dirname(__file__), "..", "..", "testdata", "humanize_motion_vectors.json")
with open(VECTORS, encoding="utf-8") as _f:
    VEC = json.load(_f)


def _check(steps, case):
    assert len(steps) == case["n"]
    assert sum(s[2] for s in steps) == pytest.approx(case["total_ms"], abs=1e-6)
    for i, x, y, sl in case["samples"]:
        assert steps[i][0] == pytest.approx(x, abs=1e-6)
        assert steps[i][1] == pytest.approx(y, abs=1e-6)
        assert steps[i][2] == pytest.approx(sl, abs=1e-6)


@pytest.mark.parametrize("case", VEC["drags"], ids=lambda c: c["persona"])
def test_plan_drag_vectors(case):
    steps = plan_drag(tuple(case["from"]), tuple(case["to"]), make_persona(case["persona"]), settle=case["settle"],
                      rng=mulberry32(case["rng"]))
    _check(steps, case)


@pytest.mark.parametrize("case", VEC["ambient"], ids=lambda c: c["persona"])
def test_plan_ambient_vectors(case):
    steps = plan_ambient(tuple(case["from"]), case["viewport"], make_persona(case["persona"]), case["ms"],
                         rng=mulberry32(case["rng"]))
    _check(steps, case)


@pytest.mark.parametrize("case", VEC["timings"], ids=lambda c: c["persona"])
def test_timing_vectors(case):
    p = make_persona(case["persona"])
    r = mulberry32(case["rng"])
    g, rel = drag_dwell(p, r)
    assert (g, rel) == pytest.approx((case["grab_ms"], case["release_ms"]), abs=1e-9)
    assert click_hold(p, r) == pytest.approx(case["click_hold"], abs=1e-9)
    assert key_dwell(p, r) == pytest.approx(case["key_dwell"], abs=1e-9)
    assert rollover_rate(p) == pytest.approx(case["rollover_rate"], abs=1e-12)


def test_wheel_anchor_model_matches_plan():
    m = VEC["wheel_anchor"]["model"]
    assert m["x"] == {"mean": 0.5, "sd": 0.10, "lo": 0.12, "hi": 0.88}
    assert m["y"] == {"mean": 0.34, "sd": 0.09, "lo": 0.10, "hi": 0.62}
    assert m["target_w"] == 140


@pytest.mark.parametrize("case", VEC["wheel_anchor"]["cases"], ids=lambda c: str(c["rng"]))
def test_wheel_anchor_point_vectors(case):
    fn = getattr(_motion, "wheel_anchor_point", None)
    if fn is None:
        pytest.skip("API missing: _motion.wheel_anchor_point (lane E)")
    r = mulberry32(case["rng"])
    for x, y in case["points"]:
        got = fn(case["viewport"][0], case["viewport"][1], r)
        assert got[0] == pytest.approx(x, abs=1e-6) and got[1] == pytest.approx(y, abs=1e-6)
