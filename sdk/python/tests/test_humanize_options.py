"""humanize= options (lane A, SC 232): the options object, strict mode, the fallback report, per-call
humanize=False, presets, and the seed -> persona guarantee. Sync AND async, against the simulated page of
test_humanize_forms_async.py (the sync humanizer sees it through _hz_sync_sim)."""
import asyncio
import json
import os
import random

import pytest

import test_humanize_forms_async as SIM
from _hz_sync_sim import sync_page
from clearcote import _humanize as HS
from clearcote import _humanize_async as HA
from clearcote._motion import hash_seed, make_persona, mulberry32

PINS = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "testdata", "humanize_persona_pins.json")))


@pytest.fixture
def fast(monkeypatch):
    """No real waiting in either humanizer; no typos (exact key sequences)."""
    async def nosleep(_s=0, *a, **k):
        return None
    monkeypatch.setattr(HA.asyncio, "sleep", nosleep)
    monkeypatch.setattr(HS.time, "sleep", lambda s: None)
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0")
    monkeypatch.delenv("CLEARCOTE_HUMANIZE_ENGINE_CLICKS", raising=False)


def _btn(**kw):
    return ("#b", SIM.Field(tag="button", type="", rect=(100.0, 100.0, 160.0, 40.0), **kw))


def _select():
    return ("#s", SIM.Field(tag="select", type="", options=[("a", "Apple"), ("b", "Banana"), ("c", "Cherry")]))


class Mode:
    """Run one scenario through the sync or the async humanizer."""

    def __init__(self, name):
        self.name = name

    def page(self, *fields, humanize=True):
        if self.name == "sync":
            sim, page = sync_page(*fields)
            HS.attach_humanize(None, page, humanize=humanize, seed="opts")
        else:
            sim = page = SIM.Page(*fields)
            asyncio.run(HA.attach_humanize(None, page, humanize=humanize, seed="opts"))
        return sim, page

    def run(self, fn, *a, **kw):
        if self.name == "sync":
            return fn(*a, **kw)
        return asyncio.run(fn(*a, **kw))

    @property
    def strict_error(self):
        from clearcote import ClearcoteStrictError
        return ClearcoteStrictError


MODES = [Mode("sync"), Mode("async")]


# ------------------------------------------------------------------------------------------ options
def test_humanize_accepts_bool_strict_object_and_dict():
    from clearcote import HumanizeOptions
    from clearcote._hzoptions import resolve
    assert resolve(False) is None and resolve(None) is None
    assert resolve(True) == HumanizeOptions()
    assert resolve("strict") == HumanizeOptions(strict=True)
    o = resolve({"preset": "quick", "typoRate": 0.01, "engine": "off"})
    assert (o.preset, o.typo_rate, o.engine, o.tempo) == ("quick", 0.01, "off", 0.7)
    assert resolve(o) is o
    with pytest.raises(TypeError, match="unknown humanize option 'speed'"):
        resolve({"speed": 2})
    with pytest.raises(TypeError):
        HumanizeOptions(speed=2)
    for bad in ({"preset": "fast"}, {"engine": "maybe"}, {"ambient": "always"}, {"typo_rate": 2},
                {"handedness": "both"}, {"device_hz": 5}, {"input_device": "pen"}, "loud"):
        with pytest.raises((ValueError, TypeError)):
            resolve(bad)
    assert HumanizeOptions().tempo == 1.0 and HumanizeOptions(preset="careful").tempo == 1.4


def test_launch_validates_humanize_before_anything_starts():
    from clearcote._hzoptions import normalize
    assert normalize(False) is False and normalize(True).preset == "default"
    with pytest.raises(TypeError):
        normalize({"nope": 1})


def test_strict_error_is_a_playwright_error():
    from playwright.sync_api import Error as PlaywrightError
    from clearcote import ClearcoteStrictError
    from clearcote.async_api import ClearcoteStrictError as A
    assert A is ClearcoteStrictError and issubclass(ClearcoteStrictError, PlaywrightError)


# ------------------------------------------------------------------------- fallback report + strict
@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_on_fallback_reports_a_covered_target_then_runs_the_stock_click(fast, mode):
    events = []
    sim, page = mode.page(_btn(covered=True), humanize={"on_fallback": events.append})
    mode.run(page.click, "#b")
    assert [n[0] for n in sim.native] == ["click"], "the stock click still runs in the default mode"
    assert len(events) == 1
    ev = events[0]
    assert ev["api"] == "Page.click" and ev["selector"] == "#b"
    assert "does not reach the element" in ev["reason"]


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_strict_raises_on_a_covered_target_and_runs_nothing(fast, mode):
    sim, page = mode.page(_btn(covered=True), humanize="strict")
    with pytest.raises(mode.strict_error) as ei:
        mode.run(page.click, "#b")
    assert ei.value.api == "Page.click" and ei.value.selector == "#b"
    assert str(ei.value).startswith("Page.click: humanize strict")
    assert sim.native == [] and not [e for e in sim.log if e[0] in ("down", "click")]


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_strict_raises_on_an_element_handle_select(fast, mode):
    # Lane F humanizes option dicts ({value}/{label}/{index}); element handles still go to Playwright.
    sim, page = mode.page(_select(), humanize={"strict": True})
    with pytest.raises(mode.strict_error) as ei:
        mode.run(page.select_option, "#s", element=object())
    assert "element handles" in ei.value.reason and ei.value.api == "Page.select_option"
    assert sim.native == []


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_on_fallback_for_a_hidden_forced_fill_and_a_non_text_press(fast, mode):
    events = []
    sim, page = mode.page(("#t", SIM.Field(visible=False)), _btn(), humanize={"on_fallback": events.append})
    mode.run(page.fill, "#t", "x", force=True)
    mode.run(page.press, "#b", "Enter")
    assert [(e["api"], e["reason"]) for e in events] == [
        ("Page.fill", "forced on a hidden target"), ("Page.press", "not a visible text field")]
    assert [n[0] for n in sim.native] == ["fill", "press"]


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_a_humanized_action_reports_nothing(fast, mode):
    events = []
    sim, page = mode.page(_btn(), ("#t", SIM.Field()), humanize={"on_fallback": events.append, "strict": False})
    mode.run(page.click, "#b")
    mode.run(page.fill, "#t", "hello")
    assert events == [] and sim.native == []


class _FakeImpl:
    def __init__(self, selector, frame):
        self._selector, self._frame = selector, frame


class _Frame:
    def __init__(self, parent):
        self.parent_frame = parent


class _FakeLocator:
    """Just what the Locator patch reads: page, _impl_obj._selector, _impl_obj._frame."""

    def __init__(self, page, selector, child=False, aio=False):
        self.page = page
        self._impl_obj = _FakeImpl(selector, _Frame(object() if child else None))
        self._aio = aio

    def count(self):   # one match: the frame route (lane F) is tried, and cannot run on this stub
        if self._aio:
            async def one():
                return 1
            return one()
        return 1


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_strict_raises_on_a_frame_locator_with_the_locator_api(fast, mode):
    sim, page = mode.page(_btn(), humanize="strict")
    if mode.name == "sync":
        from playwright.sync_api._generated import Locator
    else:
        from playwright.async_api._generated import Locator
    # Lane F: a frame locator takes the frame route; where that route cannot run, strict raises with its reason.
    aio = mode.name != "sync"
    loc = _FakeLocator(page, "iframe >> internal:control=enter-frame >> #b", aio=aio)
    with pytest.raises(mode.strict_error) as ei:
        mode.run(Locator.click, loc)
    assert ei.value.api == "Locator.click" and ei.value.reason.startswith("frame")
    assert str(ei.value).startswith("Locator.click: ")
    child = _FakeLocator(page, "#b", child=True, aio=aio)
    with pytest.raises(mode.strict_error) as ei:
        mode.run(Locator.fill, child, "x")
    assert ei.value.api == "Locator.fill" and ei.value.reason.startswith("frame")


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_on_fallback_can_stop_the_stock_call(fast, mode):
    class Stop(Exception):
        pass

    def stop(ev):
        raise Stop(ev["reason"])
    sim, page = mode.page(_btn(covered=True), humanize={"on_fallback": stop})
    with pytest.raises(Stop):
        mode.run(page.click, "#b")
    assert sim.native == []


# ----------------------------------------------------------------------------- per-call off
@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_per_call_humanize_false_is_the_stock_call(fast, mode):
    sim, page = mode.page(_btn(), ("#t", SIM.Field()))
    mode.run(page.click, "#b", humanize=False, timeout=500)
    mode.run(page.fill, "#t", "v", humanize=False)
    mode.run(page.press, "#t", "Enter", humanize=False)
    assert sim.native == [("click", "#b", {"timeout": 500}), ("fill", "#t", "v", {}), ("press", "#t", "Enter", {})]
    assert sim.log == [], "no glide, no humanized press"
    mode.run(page.mouse.move, 50, 60, humanize=False)
    mode.run(page.keyboard.type, "ab", humanize=False)
    mode.run(page.keyboard.press, "Tab", humanize=False)
    assert sim.log == [("move", 50, 60), ("type", "ab"), ("press", "Tab", None)]
    # and humanized again without the keyword: a glide of many samples
    sim.log.clear()
    mode.run(page.mouse.move, 600, 400)
    assert len([e for e in sim.log if e[0] == "move"]) > 5


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_per_call_off_keeps_the_held_state(fast, mode):
    sim, page = mode.page(_btn())
    mode.run(page.mouse.move, 300, 300)
    mode.run(page.mouse.down, humanize=False)
    mode.run(page.mouse.up, humanize=False)
    sim.log.clear()
    mode.run(page.mouse.move, 320, 310)   # not a drag leg: the up() was tracked
    assert len([e for e in sim.log if e[0] == "move"]) >= 1


def test_locator_off_strips_the_keyword_and_calls_the_stock_method():
    calls = []

    def stock(self, *a, **kw):
        calls.append(("stock", a, kw))

    def humane(self, *a, **kw):
        calls.append(("humane", a, kw))
    f = HS._locator_off(stock, humane)
    f("L", 1, humanize=False, timeout=3)
    f("L", 2, timeout=4)
    assert calls == [("stock", (1,), {"timeout": 3}), ("humane", (2,), {"timeout": 4})]

    async def astock(self, *a, **kw):
        calls.append(("astock", a, kw))

    async def ahumane(self, *a, **kw):
        calls.append(("ahumane", a, kw))
    g = HA._locator_off(astock, ahumane)
    asyncio.run(g("L", humanize=False))
    asyncio.run(g("L"))
    assert calls[-2:] == [("astock", (), {}), ("ahumane", (), {})]


# ------------------------------------------------------------------------------------- presets
def _planned_ms(mode, monkeypatch, humanize):
    """Total planned time of one glide + one typed word, same RNG stream: the sample gaps the walk is given
    plus every sleep and key hold."""
    total = [0.0]
    if mode.name == "sync":
        def walk(steps, move, *a, **k):
            total[0] += sum(s[2] for s in steps)
            move(steps[-1][0], steps[-1][1])
        monkeypatch.setattr(HS, "_paced_walk", walk)
        monkeypatch.setattr(HS.time, "sleep", lambda s: total.__setitem__(0, total[0] + s * 1000.0))
    else:
        async def walk(steps, move, *a, **k):
            total[0] += sum(s[2] for s in steps)
            await move(steps[-1][0], steps[-1][1])

        async def sleep(s=0, *a, **k):
            total[0] += s * 1000.0
        monkeypatch.setattr(HA, "_paced_walk", walk)
        monkeypatch.setattr(HA.asyncio, "sleep", sleep)
    for i in range(30):   # many RNG streams: a preset changes which pauses are drawn, so compare the means
        sim, page = mode.page(("#t", SIM.Field()), humanize=humanize)
        random.seed(1000 + i)
        mode.run(page.mouse.move, 700, 500)
        mode.run(page.keyboard.type, "the quick brown fox jumps over the lazy dog, twice")
        total[0] += sum(e[2] for e in sim.log if e[0] == "press" and e[2])
    return total[0]


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_presets_scale_the_planned_durations(monkeypatch, mode):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0")
    base = _planned_ms(mode, monkeypatch, True)
    quick = _planned_ms(mode, monkeypatch, {"preset": "quick"})
    careful = _planned_ms(mode, monkeypatch, {"preset": "careful"})
    assert base > 500
    assert abs(quick / base - 0.7) <= 0.7 * 0.15, quick / base
    assert abs(careful / base - 1.4) <= 1.4 * 0.15, careful / base


# ------------------------------------------------------------------------------ the other options
@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_options_land_on_the_page_and_persona(fast, mode):
    sim, page = mode.page(_btn(), humanize={"ambient": "load", "handedness": "left", "device_hz": 250,
                                           "input_device": "trackpad", "typo_rate": 0.25, "engine": "off"})
    p = page._clearcote_persona
    assert (p.handedness, p.input_device, p.device_hz, p.tempo) == ("left", "trackpad", 250, 1.0)
    assert page._clearcote_auto_ambient is True and page._clearcote_ambient == "load"
    from clearcote._hzoptions import engine_mode, typo_rate_for
    os.environ["CLEARCOTE_HUMANIZE_ENGINE_CLICKS"] = "all"
    try:
        assert engine_mode(page, HS._engine_clicks_mode) == "off"
    finally:
        del os.environ["CLEARCOTE_HUMANIZE_ENGINE_CLICKS"]
    assert typo_rate_for(page, HS.typo_rate) == 0.25
    sim2, page2 = mode.page(_btn())
    assert page2._clearcote_auto_ambient is False and page2._clearcote_ambient == "off"
    assert typo_rate_for(page2, HS.typo_rate) == 0.0   # the env (fast sets 0)


# ------------------------------------------------------------------- same seed, same persona
_BASE = ("seed", "device_hz", "tremor_hz", "tremor_amp", "drift_amp", "drift_tau", "jitter", "fitts_a", "fitts_b",
         "primary_frac", "overshoot", "max_corrections", "approach_bias")


def test_the_first_20_rng_draws_are_pinned():
    r = mulberry32(hash_seed(PINS["rng_seed"]))
    assert hash_seed(PINS["rng_seed"]) == PINS["hash"]
    assert [r() for _ in range(20)] == PINS["draws"]


@pytest.mark.parametrize("pin", PINS["personas"], ids=lambda p: str(p["seed_input"]))
def test_the_seed_to_persona_map_is_pinned(pin):
    p = make_persona(pin["seed_input"])
    assert {k: getattr(p, k) for k in _BASE} == pin["fields"]


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
@pytest.mark.parametrize("humanize", [True, "strict", {"preset": "careful", "handedness": "right", "typo_rate": 0.1,
                                                         "ambient": "idle", "engine": "all"}])
def test_options_never_change_the_persona_draws(fast, mode, humanize):
    sim, page = mode.page(_btn(), humanize=humanize)
    want = make_persona("opts")
    assert {k: getattr(page._clearcote_persona, k) for k in _BASE} == {k: getattr(want, k) for k in _BASE}


# --------------------------------------------------------------------------------- cloud payload
def test_cloud_launch_forwards_the_options_object():
    from clearcote.cloud import _prepare_launch
    sdk, body = _prepare_launch({"humanize": True, "country": "us"})
    assert body == {"country": "us", "humanize": False}, "humanize=True: the 0.51.0 body"
    sdk, body = _prepare_launch({"country": "us"})
    assert body == {"country": "us", "humanize": False}
    sdk, body = _prepare_launch({"humanize": {"preset": "quick", "typo_rate": 0.01, "on_fallback": print}})
    assert body == {"humanize": False, "humanizeOptions": {"preset": "quick", "strict": False, "engine": "auto",
                                                           "typoRate": 0.01}}
    sdk, body = _prepare_launch({"humanize": "strict"})
    assert body["humanizeOptions"]["strict"] is True
    sdk, body = _prepare_launch({"humanize": False})
    assert body == {"humanize": False}
    with pytest.raises(TypeError):
        _prepare_launch({"humanize": {"bogus": 1}})
