"""Lane B (SC 232): per-persona timing, persona-seeded aim, box-sized glides, covered-centre aim,
mouse.click errors, press-and-hold press point.

Every test here except the persona pin FAILED on origin/main ceb7983 (0.51.0): one timing window for
every identity, click_point on random.random, target_w=24 for every targeted glide, a covered centre
handed to Playwright, mouse.click swallowing a failed press, and press_and_hold's `position` taken from
the border box while click's is padding-box relative."""

import asyncio
import statistics
import types

import pytest

from clearcote import _hold
from clearcote import _humanize_async as H
from clearcote._motion import (
    click_hold, click_point, drag_dwell, key_dwell, make_persona, mulberry32,
)

from test_humanize_forms_async import Field, Page, attached, kinds, run, fast  # noqa: F401

IDS = ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"]
BOX = {"x": 100.0, "y": 100.0, "width": 160.0, "height": 40.0}


# --------------------------------------------------------------------------- persona pin
# The motor identity of these seeds as 0.51.0 drew it: the new traits come from NEW salted streams,
# so none of these may move (Node and .NET read the same seeds; humanize_motion_vectors.json pins paths).


def _core(p):
    return (p.seed, p.device_hz, p.tremor_hz, p.tremor_amp, p.drift_amp, p.drift_tau, p.jitter, p.fitts_a,
            p.fitts_b, p.primary_frac, p.overshoot, p.max_corrections, p.approach_bias)


def test_existing_persona_fields_are_unchanged():
    import json
    import os
    vec = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "testdata",
                                      "humanize_timing_vectors.json"), encoding="utf-8"))
    for row in vec["persona_core"]:
        got = _core(make_persona(row["persona"]))
        assert list(got) == row["core"], row["persona"]


# --------------------------------------------------------------------------- per-persona timing
def _medians(fn, n=400):
    out = []
    for pid in IDS:
        p = make_persona(pid)
        rng = mulberry32(5)          # the SAME sample stream for every persona: only the persona differs
        out.append(statistics.median(fn(p, rng) for _ in range(n)))
    return out


@pytest.mark.parametrize("name,fn,lo,hi", [
    ("click hold", click_hold, 55, 230),
    ("key dwell", key_dwell, 45, 120),
    ("grab", lambda p, r: drag_dwell(p, r)[0], 130, 360),
    ("release", lambda p, r: drag_dwell(p, r)[1], 90, 230),
])
def test_timing_medians_differ_across_seeds_inside_the_human_band(name, fn, lo, hi):
    med = _medians(fn)
    assert len({round(m) for m in med}) >= 4, (name, med)
    for m in med:
        assert lo <= m <= hi, (name, med)
    # every single draw stays in the band
    p = make_persona("band")
    rng = mulberry32(1)
    for _ in range(2000):
        assert lo <= fn(p, rng) <= hi


def test_typo_rate_is_per_persona_and_the_env_still_overrides(monkeypatch):
    monkeypatch.delenv("CLEARCOTE_HUMANIZE_TYPO_RATE", raising=False)
    rates = [H.typo_rate(make_persona(pid)) for pid in IDS]
    assert len({round(r, 4) for r in rates}) >= 4, rates
    assert all(0.005 <= r <= 0.04 for r in rates), rates
    assert H.typo_rate() == 0.02                       # no persona: the old default
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0.3")
    assert H.typo_rate(make_persona("ha-1")) == 0.3    # the env var wins when set


def test_double_click_gap_and_pauses_are_persona_draws():
    from clearcote._motion import dblclick_gap, pause_ms
    gaps = _medians(dblclick_gap)
    assert len({round(g) for g in gaps}) >= 4 and all(70 <= g <= 170 for g in gaps), gaps
    pauses = [pause_ms(make_persona(pid), 220, 480, mulberry32(3)) for pid in IDS]
    assert len({round(x) for x in pauses}) >= 4, pauses


# --------------------------------------------------------------------------- click_point
def _landings(pid, n=200):
    from clearcote._motion import reset_aim_streams
    reset_aim_streams()                                 # the identity's stream from its start
    p = make_persona(pid)
    return [click_point(BOX, (0.0, 0.0), p) for _ in range(n)]


def test_a_new_page_of_the_same_identity_continues_its_aim_stream():
    """Each page gets its own Persona; the first click of every page landed on one pixel when the stream
    restarted per persona (seen live)."""
    from clearcote._motion import reset_aim_streams
    reset_aim_streams()
    firsts = [click_point(BOX, (0.0, 0.0), make_persona("ha-1")) for _ in range(5)]
    assert len(set(firsts)) == 5, firsts


def test_click_point_is_reproducible_per_seed_and_differs_across_seeds():
    a, b = _landings("ha-1"), _landings("ha-1")
    assert a == b, "same seed, same landing sequence"
    others = [_landings(pid) for pid in IDS[1:]]
    assert all(o != a for o in others)


def test_click_point_dispersion_is_a_persona_trait_of_0_1_to_0_2_of_the_box():
    sds = []
    for pid in IDS:
        xs = [x for x, _ in _landings(pid, 600)]
        sds.append(statistics.pstdev(xs) / BOX["width"])
    assert all(0.08 <= s <= 0.21 for s in sds), sds    # clamping trims the tails a little
    assert max(sds) - min(sds) > 0.02, sds              # not one dispersion for everyone


def test_click_point_without_a_persona_still_works():
    for _ in range(50):
        x, y = click_point(BOX, (0.0, 0.0), None)
        assert BOX["x"] + 2 <= x <= BOX["x"] + BOX["width"] - 2
        assert BOX["y"] + 2 <= y <= BOX["y"] + BOX["height"] - 2


# --------------------------------------------------------------------------- target_w = box
def test_targeted_glides_use_the_box_size_as_the_fitts_width(fast, monkeypatch):
    seen = []
    real = H.plan_move

    def spy(frm, to, p, target_w=24.0, **kw):
        seen.append(target_w)
        return real(frm, to, p, target_w=target_w, **kw)
    monkeypatch.setattr(H, "plan_move", spy)

    async def go():
        p = await attached(("#b", Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0))),
                           ("#w", Field(tag="button", type="", rect=(100.0, 300.0, 300.0, 90.0))))
        await p.click("#b")
        await p.hover("#w")
        return p
    run(go())
    assert seen[-2:] == [40.0, 90.0], seen


# --------------------------------------------------------------------------- covered centre
def test_a_badge_over_the_centre_is_clicked_at_a_proven_probe_point(fast):
    async def go():
        for _ in range(16):
            b = Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0), cover_center=True)
            p = await attached(("#b", b))
            await p.click("#b")
            assert p.native == [], "no Playwright fallback"
            downs = kinds(p, "down")
            assert len(downs) == 1
            x, y = downs[0][-1]
            assert b.inside(x, y) and not b.covers(x, y), (x, y)
            assert getattr(b, "clicks", 0) == 1
    run(go())


def test_a_fully_covered_target_still_goes_native(fast):
    async def go():
        b = Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0), covered=True)
        p = await attached(("#b", b))
        await p.click("#b")
        assert [n[0] for n in p.native] == ["click"]
        assert not kinds(p, "down")
    run(go())


# --------------------------------------------------------------------------- mouse.click errors
def test_mouse_click_raises_when_the_press_fails(fast):
    async def go():
        p = Page(("#b", Field(tag="button", type="")))

        async def boom(*a, **k):
            raise RuntimeError("press failed: target closed")
        p.mouse.down = boom
        await H.attach_humanize(None, p, humanize=True, seed="raise")
        with pytest.raises(RuntimeError, match="press failed"):
            await p.mouse.click(150, 110, button="right")
    run(go())


def test_sync_mouse_click_raises_when_the_press_fails(monkeypatch):
    from clearcote import _humanize
    from test_humanize import _SyncPage
    monkeypatch.setattr(_humanize.time, "sleep", lambda *_: None)
    page = _SyncPage()
    m = page.mouse

    def boom(*a, **k):
        raise RuntimeError("press failed: target closed")
    m.down = boom
    _humanize.attach_humanize(None, page, humanize=True, seed="raise")
    with pytest.raises(RuntimeError, match="press failed"):
        page.mouse.click(150, 110, button="right")


# --------------------------------------------------------------------------- press_and_hold point
def _hold_loc(p, sel):
    loc = p.locator(sel)
    loc.page = p
    loc._impl_obj = types.SimpleNamespace(_selector=sel, _frame=types.SimpleNamespace(parent_frame=None))
    return loc


def test_hold_position_is_padding_box_relative_like_click(fast):
    async def go():
        b = Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0), bl=3.0, bt=2.0)
        p = await attached(("#b", b))
        await _hold.press_and_hold_async(_hold_loc(p, "#b"), 150, position={"x": 10, "y": 5}, tremor=False)
        down = kinds(p, "down")[0]
        assert down[-1] == (113.0, 107.0), down     # box + border + position; 0.51.0 pressed (110, 105)
        # and the same point a humanized click with that position presses
        await p.click("#b", position={"x": 10, "y": 5})
        assert kinds(p, "down")[-1][-1] == (113.0, 107.0)
    run(go())


def test_hold_point_plan_takes_the_border():
    box = {"x": 100, "y": 50, "width": 200, "height": 40}
    assert _hold.hold_point(box, position={"x": 10, "y": 5}, border=(2, 1)) == (112.0, 56.0)


def test_hold_on_a_covered_centre_presses_a_proven_point(fast):
    async def go():
        for _ in range(8):
            b = Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0), cover_center=True)
            p = await attached(("#b", b))
            await _hold.press_and_hold_async(_hold_loc(p, "#b"), 120, tremor=False)
            x, y = kinds(p, "down")[0][-1]
            assert b.inside(x, y) and not b.covers(x, y), (x, y)
    run(go())
