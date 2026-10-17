"""SC 232 integration (lane G's score hook in lanes C/D/E): with ``CLEARCOTE_HUMANIZE_SCORE=1`` every humanized
surface records a scored line -- a hover dwell as ``hover``, a held leg as ``drag`` (the held button on every
sample), ambient motion as ``ambient``, a typed run as one ``typed`` line of the characters' down/up, and one
``wheel`` line per mouse.wheel call -- in the sync and the async humanizer, on the forms simulator. The hook is
off by default: nothing is written. Mirrored by node/test/humanize-trace-taps.test.ts and HumanizeTraceTapsTests.cs.
"""
import asyncio
import json
import os

import pytest

import test_humanize_forms_async as SIM
from _hz_sync_sim import sync_page
from clearcote import _humanize as HS
from clearcote import _humanize_async as HA
from clearcote import _pacing
from clearcote._motion import make_persona, mulberry32, plan_drag


def _field():
    return ("#t", SIM.Field(rect=(100.0, 100.0, 200.0, 24.0)))


class Mode:
    """Run one scenario through the sync or the async humanizer."""

    def __init__(self, name):
        self.name = name

    def page(self, *fields):
        if self.name == "sync":
            sim, page = sync_page(*fields)
            HS.attach_humanize(None, page, humanize=True, seed="taps")
        else:
            sim = page = SIM.Page(*fields)
            asyncio.run(HA.attach_humanize(None, page, humanize=True, seed="taps"))
        return sim, page

    def run(self, fn, *a, **kw):
        if self.name == "sync":
            return fn(*a, **kw)
        return asyncio.run(fn(*a, **kw))


MODES = [Mode("sync"), Mode("async")]


def _lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


@pytest.fixture
def fast(monkeypatch):
    """No real waiting in either humanizer (the hook records what was dispatched, not wall time); no typos."""
    async def nosleep(_s=0, *a, **k):
        return None
    monkeypatch.setattr(HA.asyncio, "sleep", nosleep)
    monkeypatch.setattr(HS.time, "sleep", lambda s: None)
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0")
    monkeypatch.setenv("CLEARCOTE_NO_WARN", "1")
    monkeypatch.delenv("CLEARCOTE_HUMANIZE_ENGINE_CLICKS", raising=False)


@pytest.fixture
def hook(tmp_path, monkeypatch):
    out = tmp_path / "score.jsonl"
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_SCORE", "1")
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_SCORE_FILE", str(out))
    return out


def _drive(mode, page):
    mode.run(page.hover, "#t")                 # a glide, then the hover dwell (lane C)
    mode.run(page.mouse.down)                  # a held leg = a drag (lane E)
    mode.run(page.mouse.move, 420, 300)
    mode.run(page.mouse.up)
    mode.run(page.ambient_motion, 300)         # ambient (lane C's planner)
    mode.run(page.keyboard.type, "hello world")   # one typed run (lane D)
    mode.run(page.mouse.wheel, 0, 300)         # one wheel call (lane E)


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_every_humanized_surface_records_its_kind(fast, hook, mode):
    sim, page = mode.page(_field())
    _drive(mode, page)
    lines = _lines(hook)
    kinds = [ln["kind"] for ln in lines]
    for k in ("glide", "hover", "drag", "ambient", "typed", "wheel"):
        assert k in kinds, (k, kinds)
    typed = [ln for ln in lines if ln["kind"] == "typed"]
    assert len(typed) == 1, kinds
    assert typed[0]["n"] == len("hello world")
    assert typed[0]["meta"] == {"n": 11, "typos": False, "floor_ms": 0.0}
    wheel = [ln for ln in lines if ln["kind"] == "wheel"]
    assert len(wheel) == 1 and wheel[0]["n"] >= 1 and wheel[0]["meta"]["dy"] == 300
    drag = [ln for ln in lines if ln["kind"] == "drag"]
    assert drag and drag[0]["features"]["held_n"] == drag[0]["n"], "the held button is on every drag sample"
    hover = [ln for ln in lines if ln["kind"] == "hover"]
    assert hover[0]["n"] >= 3
    for ln in lines:
        assert 0.0 <= ln["score"] <= 1.0 and isinstance(ln["flags"], list)


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_hook_off_by_default_records_nothing(fast, tmp_path, monkeypatch, mode):
    out = tmp_path / "score.jsonl"
    monkeypatch.delenv("CLEARCOTE_HUMANIZE_SCORE", raising=False)
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_SCORE_FILE", str(out))
    sim, page = mode.page(_field())
    _drive(mode, page)
    assert not out.exists()


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


def test_walk_carries_the_held_button_on_every_drag_sample(hook):
    c = _Clock()
    steps = plan_drag((100, 100), (400, 300), make_persona("ha-1"), rng=mulberry32(3))
    _pacing.walk(steps, lambda x, y: None, clock=c.now, sleep=c.sleep, kind="drag", buttons=1)
    (line,) = _lines(hook)
    assert line["kind"] == "drag" and line["features"]["held_n"] == line["n"] == len(steps)
