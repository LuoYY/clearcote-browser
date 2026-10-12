"""Path SHAPE of the humanize planner (SC 210 lane S), measured on the planned points.

The metrics are the SC 209 Humanize Lab's (analyze.py seg_metrics): straightness = straight distance /
path length, overshoot = how far the path passes the end point along the line of the move (> 2 px
counts), time of peak speed from a 10 ms grid of +-40 ms differences. Measured on r38 / SDK 0.48.0 the
planned aimed moves were 0.99 straight and passed the target in 1 of 36 approaches; a held drag
wandered up to 47 px off its line. Real reaches: straightness ~0.80-0.98, overshoot 15-40 %, speed peak
before the halfway point.

The same bands are asserted by node/test/motion-shape.test.ts and MotionShapeTests.cs, and the three
SDKs are pinned to identical paths by sdk/testdata/humanize_motion_vectors.json.
"""

import json
import math
import os
import statistics

from clearcote._motion import make_persona, mulberry32, plan_move

IDS = ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"]
# SC 209 fixture targets: centre x, centre y, width, height
TARGETS = [(150, 142, 120, 44), (902, 613, 44, 26), (515, 377, 90, 34), (1040, 138, 160, 56),
           (198, 632, 36, 24), (675, 205, 70, 30)]
VECTORS = os.path.join(os.path.dirname(__file__), "..", "..", "testdata", "humanize_motion_vectors.json")


def path_shape(frm, steps, quantize=False):
    """SC 209 seg_metrics on a planned path. Sample i is dispatched after the sleeps of samples < i.
    ``quantize``: what a DPR-1 page sees -- whole pixels, and no event while the pixel is unchanged."""
    pts = [(frm[0], frm[1], 0.0)]
    t = 0.0
    for x, y, s in steps:
        if quantize:
            x, y = math.floor(x), math.floor(y)
            if (x, y) == pts[-1][:2]:
                t += s
                continue
        pts.append((x, y, t))
        t += s
    x0, y0, _ = pts[0]
    x1, y1, t1 = pts[-1]
    d = math.hypot(x1 - x0, y1 - y0)
    length = sum(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) for i in range(1, len(pts)))
    ux, uy = (x1 - x0) / d, (y1 - y0) / d
    over = max(0.0, max((p[0] - x0) * ux + (p[1] - y0) * uy for p in pts) - d)
    dev = max(abs((p[0] - x0) * uy - (p[1] - y0) * ux) for p in pts)
    ts = [p[2] for p in pts]
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))

    def dist_at(tt):
        if tt <= ts[0]:
            return 0.0
        if tt >= ts[-1]:
            return cum[-1]
        lo, hi = 0, len(ts) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if ts[mid] <= tt:
                lo = mid
            else:
                hi = mid
        if ts[hi] == ts[lo]:
            return cum[hi]
        return cum[lo] + (tt - ts[lo]) / (ts[hi] - ts[lo]) * (cum[hi] - cum[lo])

    prof = [(dist_at(10 * k + 40) - dist_at(10 * k - 40)) / 80.0 for k in range(int(t1 // 10) + 1)]
    vmax = max(prof)
    peaks = 0
    for k in range(1, len(prof) - 1):
        if not (prof[k] >= prof[k - 1] and prof[k] > prof[k + 1] and prof[k] > .2 * vmax):
            continue
        lo = k
        while lo > 0 and prof[lo - 1] <= prof[k]:
            lo -= 1
        hi = k
        while hi < len(prof) - 1 and prof[hi + 1] <= prof[k]:
            hi += 1
        if prof[k] - max(min(prof[lo:k + 1]), min(prof[k:hi + 1])) >= .35 * vmax:
            peaks += 1
    return {"eff": d / length, "over": over, "dev": dev, "dur": t1 + steps[-1][2], "peaks": peaks,
            "t_peak": (prof.index(vmax) + 0.5) / len(prof)}


def aimed_moves(rounds=12):
    """(persona id, from, steps) for the SC 209 tour: six targets of different size and distance."""
    out = []
    for pi, pid in enumerate(IDS):
        p = make_persona(pid)
        for r in range(rounds):
            rng = mulberry32(1000 * pi + r)
            cur = (260.0, 165.0)
            for cx, cy, w, h in TARGETS:
                out.append((pid, cur, plan_move(cur, (cx, cy), p, target_w=min(w, h), rng=rng), (cx, cy)))
                cur = (cx, cy)
    return out


def slider_drags(rounds=16):
    """Horizontal 300-400 px held drags (a slider captcha), planned as the humanizer plans them."""
    out = []
    for pi, pid in enumerate(IDS):
        p = make_persona(pid)
        for r in range(rounds):
            rng = mulberry32(7000 + 100 * pi + r)
            frm = (40 + rng() * 60, 300 + rng() * 40)
            to = (frm[0] + 300 + rng() * 100, frm[1] + (rng() - 0.5) * 6)
            out.append((pid, frm, plan_move(frm, to, p, settle=True, rng=rng), to))
    return out


# --- aimed moves -----------------------------------------------------------------------------------

def test_aimed_moves_are_as_straight_as_real_reaches_not_ruler_lines():
    for quantize in (False, True):
        effs = [path_shape(f, s, quantize)["eff"] for _, f, s, _ in aimed_moves()]
        med = statistics.median(effs)
        assert 0.85 <= med <= 0.95, (quantize, med)
        # and never a perfect line in more than a few moves
        assert sum(1 for e in effs if e > 0.995) / len(effs) < 0.1


def test_aimed_moves_overshoot_and_come_back_in_15_to_40_percent():
    moves = aimed_moves()
    share = sum(1 for _, f, s, _ in moves if path_shape(f, s)["over"] > 2) / len(moves)
    assert 0.15 <= share <= 0.40, share


def test_overshoot_rate_is_a_per_persona_trait():
    from clearcote._motion import shape_traits
    for pid in IDS:
        p = make_persona(pid)
        rate = shape_traits(p).overshoot_rate
        assert 0.15 <= rate <= 0.38
        rng = mulberry32(55)
        n = 300
        hits = 0
        for _ in range(n):
            frm = (100 + rng() * 200, 100 + rng() * 200)
            to = (frm[0] + 150 + rng() * 500, frm[1] + (rng() - 0.5) * 400)
            if path_shape(frm, plan_move(frm, to, p, target_w=30, rng=rng))["over"] > 2:
                hits += 1
        assert abs(hits / n - rate) < 0.08, (pid, hits / n, rate)


def test_an_overshoot_returns_onto_the_target():
    for _, f, s, to in aimed_moves(4):
        assert (s[-1][0], s[-1][1]) == to       # lands exactly, whatever the path did before


def test_speed_peaks_before_the_halfway_point():
    shapes = [path_shape(f, s) for _, f, s, _ in aimed_moves()]
    tp = statistics.median(x["t_peak"] for x in shapes)
    assert 0.2 <= tp < 0.45, tp
    assert statistics.median(x["peaks"] for x in shapes) <= 2


def test_fitts_scaled_duration_and_the_cap_are_kept():
    p = make_persona("fitts-shape")
    def mean_dur(w):
        return statistics.mean(path_shape((100, 100), plan_move((100, 100), (600, 100), p, target_w=w,
                                                                    rng=mulberry32(k)))["dur"] for k in range(20))
    assert mean_dur(8) > mean_dur(120)
    for k in range(40):
        st = plan_move((10, 10), (1900, 1000), p, target_w=8, rng=mulberry32(k))
        # Fitts movement time is capped at 1.7 s; the re-planning gaps between submovements add <= 2 x 120 ms
        assert sum(s[2] for s in st) <= 1700 * 1.12 + 240


def test_per_seed_determinism():
    a = plan_move((0, 0), (500, 300), make_persona("det"), target_w=20, rng=mulberry32(9))
    b = plan_move((0, 0), (500, 300), make_persona("det"), target_w=20, rng=mulberry32(9))
    c = plan_move((0, 0), (500, 300), make_persona("det-2"), target_w=20, rng=mulberry32(9))
    assert a == b
    assert a != c


# --- held drags ------------------------------------------------------------------------------------

def test_slider_drags_stay_on_the_track():
    for quantize in (False, True):
        shapes = [path_shape(f, s, quantize) for _, f, s, _ in slider_drags()]
        effs = sorted(x["eff"] for x in shapes)
        assert effs[len(effs) // 10] >= 0.93, (quantize, effs[:5])
        # a 40 px track leaves the handle centre +-20 px; the leg never wanders more than 8
        assert max(x["dev"] for x in shapes) <= 8, max(x["dev"] for x in shapes)


def test_slider_drags_take_half_a_second_to_two_seconds():
    for _, f, s, _ in slider_drags():
        dur = sum(x[2] for x in s)
        assert 500 <= dur <= 2000, dur


def test_slider_drags_sometimes_overshoot_and_pull_back():
    drags = slider_drags(40)
    share = sum(1 for _, f, s, _ in drags if path_shape(f, s)["over"] > 2) / len(drags)
    assert 0.04 <= share <= 0.35, share
    for _, f, s, to in drags:
        assert (s[-1][0], s[-1][1]) == to


def test_a_move_with_the_button_held_is_planned_as_a_drag():
    p = make_persona("held")
    worst = 0.0
    for k in range(40):
        st = plan_move((100, 300), (460, 302), p, held=True, rng=mulberry32(k))
        worst = max(worst, path_shape((100, 300), st)["dev"])
        assert (st[-1][0], st[-1][1]) == (460, 302)   # no seating jiggle unless settle
    assert worst <= 8


# --- shared vectors: Python, Node and .NET plan the same path -------------------------------------

def test_shared_motion_vectors():
    from clearcote._motion import shape_traits
    with open(VECTORS, encoding="utf-8") as f:
        vec = json.load(f)
    for t in vec["shape_traits"]:
        st = shape_traits(make_persona(t["persona"]))
        assert abs(st.overshoot_rate - t["overshoot_rate"]) < 1e-12
        assert abs(st.curve - t["curve"]) < 1e-12
        assert abs(st.drag_tempo - t["drag_tempo"]) < 1e-12
    for c in vec["paths"]:
        steps = plan_move(tuple(c["from"]), tuple(c["to"]), make_persona(c["persona"]), target_w=c["target_w"],
                          settle=c["mode"] == "settle", held=c["mode"] == "held", rng=mulberry32(c["rng"]))
        assert len(steps) == c["n"], c
        assert abs(sum(s[2] for s in steps) - c["total_ms"]) < 1e-6
        for i, x, y, sl in c["samples"]:
            assert abs(steps[i][0] - x) < 1e-6 and abs(steps[i][1] - y) < 1e-6 and abs(steps[i][2] - sl) < 1e-6, (c, i)
