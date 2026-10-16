"""LIVE drag + scroll (SC 232 lane E) against a real Clearcote build, headless.

Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary. What a PAGE sees on
sdk/testdata/humanize_dragscroll.html: wheel events before every scroll, where the target ends up, where
a drag presses and releases, the hesitation after the press, a knob dragged by a delta, a trackpad stream.
"""
import math
import os

import pytest

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")
HTML = open(os.path.join(os.path.dirname(__file__), "..", "..", "testdata", "humanize_dragscroll.html"),
            encoding="utf-8").read()


@pytest.fixture(scope="module")
def browser():
    import clearcote
    b = clearcote.launch(executable_path=LIVE, headless=True, humanize=True, fingerprint="dragscroll-live-1",
                         quiet=True)
    yield b
    b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    pg.set_content(HTML)
    yield pg
    pg.close()


def _ev(page, kind):
    return [e for e in page.evaluate("window.__ev") if e["t"] == kind]


def test_click_far_target_is_wheeled_in(page):
    page.click("#far")
    wheels, scrolls = _ev(page, "wheel"), _ev(page, "scroll")
    assert wheels and all(w["tr"] for w in wheels)
    assert scrolls and scrolls[0]["ts"] >= wheels[0]["ts"], "no scroll before the first wheel"
    assert getattr(page, "_clearcote_last_fallback", None) is None
    r = page.evaluate("(() => { const r = document.getElementById('far').getBoundingClientRect(); "
                      "return [r.y, r.height, innerHeight]; })()")
    frac = (r[0] + r[1] / 2) / r[2]
    assert 0.15 <= frac <= 0.70, frac   # 25-60 % +- half a notch
    clicks = _ev(page, "click")
    assert clicks and clicks[-1]["id"] == "far" and clicks[-1]["tr"]


def test_nested_container_is_wheeled(page):
    page.click("#deep")
    st = page.evaluate("document.getElementById('box').scrollTop")
    assert st > 0
    assert getattr(page, "_clearcote_last_fallback", None) is None
    ids = {s["id"] for s in _ev(page, "scroll")}
    assert "box" in ids
    assert _ev(page, "click")[-1]["id"] == "deep"


def test_non_scrollable_panel_falls_back_and_reports(page):
    page.click("#hid")
    assert page._clearcote_last_fallback == {"api": "scroll_into_view", "reason": "not_scrollable"}
    assert _ev(page, "click")[-1]["id"] == "hid"


def test_drag_and_drop_positions_and_hesitation(page):
    page.drag_and_drop("#src", "#dst", source_position={"x": 10, "y": 12}, target_position={"x": 180, "y": 20})
    down = _ev(page, "pointerdown")[-1]
    up = _ev(page, "pointerup")[-1]
    assert abs(down["x"] - 110) <= 2 and abs(down["y"] - 212) <= 2, down
    assert abs(up["x"] - 880) <= 2 and abs(up["y"] - 440) <= 2, up
    held = [m for m in _ev(page, "move") if m["b"] == 1 and down["ts"] <= m["ts"] <= up["ts"]]
    assert len(held) >= 10
    leg = next(m for m in held if math.hypot(m["x"] - down["x"], m["y"] - down["y"]) >= 8)
    assert 90 <= leg["ts"] - down["ts"] <= 380, leg["ts"] - down["ts"]


def test_drag_by_knob(page):
    got = page.drag_by("#knob", 150, 0)
    left = page.evaluate("document.getElementById('knob').offsetLeft")
    assert abs((left - 2) - 150) <= 2, left
    assert abs(got[0] - 150) <= 2
    assert 1 <= page._clearcote_drag_legs <= 3


def test_trackpad_stream(page):
    page._clearcote_input_device = "trackpad"
    page.mouse.move(600, 300)
    page.mouse.wheel(0, 700)
    w = _ev(page, "wheel")
    assert w and all(e["mode"] == 0 for e in w)
    dy = [e["dy"] for e in w]
    assert all(abs(d) <= 41 for d in dy)
    span = (w[-1]["ts"] - w[0]["ts"]) / 1000.0
    rate = (len(w) - 1) / span
    # The plan is 60-120 events/s, sent on a wall clock (_scroll.paced_wheel); what the PAGE sees is bound
    # by the renderer's wheel ack (a CDP wheel returns after it, ~26 ms on r42 headless, and Chromium
    # coalesces wheels awaiting an ack), so headless shows ~35-40/s. Pinned loosely here; see lane-E.md.
    assert 25 <= rate <= 130, rate
    # the tail: the last events decay
    tail = dy[-6:]
    assert all(a >= b for a, b in zip(tail, tail[1:])) and tail[-1] < 2
