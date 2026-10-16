"""Lane E (SC 232): drags and scrolling, on the fake page of _fake_scrollpage.

Measured on r42 / SDK 0.51.0 before these: drag_to pressed and released on the exact box centres
with no hit test and ignored source_position / target_position / force / timeout; page.drag_and_drop
was not humanized at all; the held leg started at full speed on the press; an off-screen target was
scrolled PROGRAMMATICALLY (no wheel event, the target on the exact centre line); a macOS persona
scrolled 40 px notches. Every test here fails on origin/main (ceb7983)."""
import json
import math
import os

import pytest

from clearcote._motion import make_persona, mulberry32
from tests._fake_scrollpage import Doc, make

HESITATION_MAX_PX = 8.0   # the creep stays inside this circle (_motion.HESITATION_MAX_PX)
VECTORS = os.path.join(os.path.dirname(__file__), "..", "..", "testdata", "humanize_scroll_vectors.json")


def _ev(page, kind):
    return [e for e in page.log if e[0] == kind]


def _dnd_doc():
    d = Doc()
    d.els["#src"] = {"rect": [100.0, 200.0, 120.0, 60.0]}
    d.els["#dst"] = {"rect": [700.0, 420.0, 200.0, 140.0]}
    return d


# --------------------------------------------------------------------------- drag options
def test_drag_and_drop_honours_positions_within_2px(monkeypatch):
    page = make(monkeypatch, _dnd_doc())
    page.drag_and_drop("#src", "#dst", source_position={"x": 10, "y": 12}, target_position={"x": 180, "y": 20})
    assert not _ev(page, "native_dnd"), "the humanized path ran"
    downs = [i for i, e in enumerate(page.log) if e[0] == "down"]
    ups = [i for i, e in enumerate(page.log) if e[0] == "up"]
    assert len(downs) == 1 and len(ups) == 1
    press = [e for e in page.log[:downs[0]] if e[0] == "move"][-1]
    release = [e for e in page.log[:ups[0]] if e[0] == "move"][-1]
    assert abs(press[1] - 110) <= 2 and abs(press[2] - 212) <= 2, press
    assert abs(release[1] - 880) <= 2 and abs(release[2] - 440) <= 2, release


def test_drag_and_drop_is_humanized(monkeypatch):
    page = make(monkeypatch, _dnd_doc())
    page.drag_and_drop("#src", "#dst")
    assert not _ev(page, "native_dnd")
    i_down = next(i for i, e in enumerate(page.log) if e[0] == "down")
    i_up = next(i for i, e in enumerate(page.log) if e[0] == "up")
    held = [e for e in page.log[i_down:i_up] if e[0] == "move"]
    assert len(held) >= 10
    t_down = page.log[i_down][1]
    press = [e for e in page.log[:i_down] if e[0] == "move"][-1]
    first_far = next(e for e in held if math.hypot(e[1] - press[1], e[2] - press[2]) >= 10)
    assert first_far[3] - t_down >= 0.130, "grab >= 130 ms before the leg"
    # the drop lands inside the target box, not on its exact centre every time
    last = held[-1]
    assert 700 <= last[1] <= 900 and 420 <= last[2] <= 560


def test_force_skips_the_hit_test(monkeypatch):
    d = _dnd_doc()
    d.covers.append([0.0, 0.0, 1280.0, 800.0])   # an overlay owns every point
    page = make(monkeypatch, d)
    page.drag_and_drop("#src", "#dst")
    assert len(_ev(page, "native_dnd")) == 1, "covered ends go to Playwright's drag"
    assert not _ev(page, "down")
    page.drag_and_drop("#src", "#dst", force=True)
    assert len(_ev(page, "native_dnd")) == 1, "force: no hit test, the humanized drag runs"
    assert len(_ev(page, "down")) == 1


def test_covered_centre_uses_a_probe_point(monkeypatch):
    d = _dnd_doc()
    d.covers.append([130.0, 200.0, 60.0, 60.0])   # an icon over the middle of #src
    page = make(monkeypatch, d)
    page.drag_and_drop("#src", "#dst")
    assert not _ev(page, "native_dnd")
    i_down = next(i for i, e in enumerate(page.log) if e[0] == "down")
    press = [e for e in page.log[:i_down] if e[0] == "move"][-1]
    assert d.at(press[1], press[2]) == "#src"


# --------------------------------------------------------------------------- hesitation
@pytest.mark.parametrize("seed", ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"])
def test_drag_start_hesitation(monkeypatch, seed):
    page = make(monkeypatch, _dnd_doc(), seed=seed)
    page.drag_and_drop("#src", "#dst")
    i_down = next(i for i, e in enumerate(page.log) if e[0] == "down")
    t_down = page.log[i_down][1]
    press = [e for e in page.log[:i_down] if e[0] == "move"][-1]
    held = [e for e in page.log[i_down:] if e[0] == "move"]
    still = [e for e in held if e[3] - t_down < 0.100]
    assert still, "the hand moves a little while it hesitates"
    assert all(math.hypot(e[1] - press[1], e[2] - press[2]) < HESITATION_MAX_PX for e in still)
    leg = next(e for e in held if math.hypot(e[1] - press[1], e[2] - press[2]) >= HESITATION_MAX_PX)
    assert 0.100 <= leg[3] - t_down <= 0.300 + 0.05, leg[3] - t_down


# --------------------------------------------------------------------------- drag_by
@pytest.mark.parametrize("seed", ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"])
def test_drag_by_converges_in_at_most_three_legs(monkeypatch, seed):
    d = Doc()
    d.els["#thumb"] = {"rect": [200.0, 300.0, 20.0, 20.0]}
    d.follow["#thumb"] = (0.93, 10.0)   # a lagging slider that snaps to 10 px steps
    page = make(monkeypatch, d, seed=seed)
    got = page.drag_by("#thumb", 120, 0)
    assert got is not None and abs(got[0] - 120) <= 2 and abs(got[1]) <= 2, got
    assert 1 <= page._clearcote_drag_legs <= 3
    assert d.els["#thumb"]["rect"][0] == 320.0
    assert len(_ev(page, "down")) == 1 and len(_ev(page, "up")) == 1


# --------------------------------------------------------------------------- wheel scroll-into-view
def _far_doc(scrollable=True, platform="Windows"):
    d = Doc(scrollable=scrollable, platform=platform)
    d.els["#far"] = {"rect": [500.0, 3000.0, 160.0, 40.0]}
    return d


def test_click_offscreen_scrolls_with_the_wheel_not_programmatically(monkeypatch):
    d = _far_doc()
    page = make(monkeypatch, d)
    page.click("#far")
    wheels = _ev(page, "wheel")
    assert wheels, "wheel events brought the target in"
    assert not _ev(page, "prog"), "no programmatic scroll"
    assert all(w[1] == 0 and w[2] % 100 == 0 for w in wheels), "whole Windows notches"
    r = d.vrect("#far")
    frac = (r[1] + r[3] / 2) / d.vh
    assert 0.0 <= r[1] and r[1] + r[3] <= d.vh
    assert 0.25 - 0.07 <= frac <= 0.60 + 0.07, frac    # 25-60 % asked, +- half a 100 px notch
    assert _ev(page, "down"), "then it clicked"
    # the first flick undershoots: fewer notches than the full distance needs
    first_burst = 0
    for e in page.log:
        if e[0] == "wheel":
            first_burst += 1
        elif first_burst and e[0] in ("move",):
            break
    assert first_burst < 30


def test_nested_container_is_wheeled_over(monkeypatch):
    d = Doc()
    d.containers["#box"] = {"rect": [100.0, 200.0, 400.0, 200.0], "st": 0.0, "inner": 2000.0}
    d.els["#deep"] = {"rect": [20.0, 1500.0, 200.0, 30.0], "parent": "#box"}
    page = make(monkeypatch, d)
    page.click("#deep")
    wheels = _ev(page, "wheel")
    assert wheels and not _ev(page, "prog")
    assert all(100 <= w[3] <= 500 and 200 <= w[4] <= 400 for w in wheels), "the pointer is over the container"
    assert d.containers["#box"]["st"] > 0 and d.sy == 0
    r = d.vrect("#deep")
    assert 200 <= r[1] and r[1] + r[3] <= 400


def test_non_scrollable_page_falls_back_and_reports(monkeypatch):
    d = _far_doc(scrollable=False)
    page = make(monkeypatch, d)
    reported = []
    page._clearcote_report_fallback = reported.append
    page.click("#far")
    assert _ev(page, "prog"), "the programmatic scroll is the fallback"
    assert page._clearcote_last_fallback == {"api": "scroll_into_view", "reason": "not_scrollable"}
    assert reported == [{"api": "scroll_into_view", "reason": "not_scrollable"}]


def test_press_and_hold_uses_the_wheel(monkeypatch):
    d = _far_doc()
    page = make(monkeypatch, d)
    page.press_and_hold("#far", 300, tremor=False)
    assert _ev(page, "wheel") and not _ev(page, "prog")


# --------------------------------------------------------------------------- trackpad
def test_mac_persona_scrolls_like_a_trackpad(monkeypatch):
    d = Doc(platform="MacIntel")
    page = make(monkeypatch, d)
    page.mouse.wheel(0, 600)
    w = _ev(page, "wheel")
    deltas = [e[2] for e in w]
    assert abs(sum(deltas) - 600) < 1e-6
    assert any(abs(x - round(x)) > 1e-3 for x in deltas), "pixel deltas, not whole notches"
    assert all(abs(x) <= 40 for x in deltas)
    peak = max(range(len(deltas)), key=lambda i: deltas[i])
    tail = deltas[peak:]
    k = len(tail) - 1
    while k > 0 and tail[k - 1] > tail[k]:
        k -= 1
    decay = tail[k:]
    t_tail = w[-1][5] - w[peak + k][5]
    assert len(decay) >= 10 and decay[-1] >= 1.0 and t_tail >= 0.25, (len(decay), t_tail)
    span = w[-1][5] - w[0][5]
    rate = (len(w) - 1) / span
    assert 55 <= rate <= 125 or any(  # a 120-400 ms mid-swipe pause lowers the mean rate
        (b[5] - a[5]) >= 0.12 for a, b in zip(w, w[1:])), rate


def test_mouse_persona_keeps_notches_and_trackpad_option_overrides(monkeypatch):
    page = make(monkeypatch, Doc(platform="MacIntel"), device="mouse")
    page.mouse.wheel(0, 120)
    assert [e[2] for e in _ev(page, "wheel")] == [40, 40, 40]
    page2 = make(monkeypatch, Doc(platform="Windows"), device="trackpad")
    page2.mouse.wheel(0, 300)
    assert len(_ev(page2, "wheel")) > 5


# --------------------------------------------------------------------------- shared vectors
def test_scroll_vectors_match_the_python_planner():
    from clearcote import _scroll as S
    from clearcote._motion import drag_hesitation
    with open(VECTORS, encoding="utf-8") as f:
        doc = json.load(f)
    for v in doc["anchor"]:
        x, y = S.anchor_point(v["vw"], v["vh"], mulberry32(v["rng"]))
        assert abs(x - v["x"]) < 1e-9 and abs(y - v["y"]) < 1e-9
        assert v["vw"] * 0.12 <= x <= v["vw"] * 0.88 and v["vh"] * 0.10 <= y <= v["vh"] * 0.62
    for v in doc["drift"]:
        dx, dy = S.wheel_drift(mulberry32(v["rng"]))
        assert abs(dx - v["dx"]) < 1e-9 and abs(dy - v["dy"]) < 1e-9
    for v in doc["trackpad"]:
        ev = S.plan_trackpad(v["dx"], v["dy"], mulberry32(v["rng"]))
        assert len(ev) == v["n"]
        for i, ex, ey, sl in v["samples"]:
            assert abs(ev[i][0] - ex) < 1e-6 and abs(ev[i][1] - ey) < 1e-6 and abs(ev[i][2] - sl) < 1e-6
    for v in doc["hesitation"]:
        steps = drag_hesitation(tuple(v["from"]), tuple(v["to"]), make_persona(v["persona"]),
                                mulberry32(v["rng"]), v.get("dur_ms"))
        assert len(steps) == v["n"]
        for i, x, y, sl in v["samples"]:
            assert abs(steps[i][0] - x) < 1e-6 and abs(steps[i][1] - y) < 1e-6 and abs(steps[i][2] - sl) < 1e-6
    from clearcote._motion import hesitate_then_drag
    for v in doc["held"]:
        steps, nc = hesitate_then_drag(tuple(v["from"]), tuple(v["to"]), make_persona(v["persona"]), v["window_ms"],
                                       v["settle"], mulberry32(v["rng"]))
        assert len(steps) == v["n"] and nc == v["creep"]
        for i, x, y, sl in v["samples"]:
            assert abs(steps[i][0] - x) < 1e-6 and abs(steps[i][1] - y) < 1e-6 and abs(steps[i][2] - sl) < 1e-6
    for v in doc["need"]:
        got = S.scroll_need(v["box"], v["view"], v["frac"])
        assert abs(got[0] - v["dx"]) < 1e-9 and abs(got[1] - v["dy"]) < 1e-9
    for v in doc["device"]:
        assert S.input_device(v["platform"], v["option"]) == v["device"]
