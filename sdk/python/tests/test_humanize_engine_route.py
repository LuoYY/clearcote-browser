"""Which clicks the engine pointer path walks (SC 210 lane S item 7), on a fake page.

Measured on r38: the engine (Browser.dispatchPointerPath / humanizedClick) walks from ITS OWN last
position on the page, which an SDK glide does not update -- an engine click, an SDK glide to (800,550),
then mouse.click(400,200): the engine's first sample was at the previous engine click, a 559 px
teleport. And it answers only after the walk and the click, so sleeping a full `duration` afterwards
added an idle second walk's worth of time to every click.
"""
import pytest

import clearcote._humanize as H


class _Clock:
    """Virtual time for clearcote._humanize: sleep advances it, monotonic reads it."""

    def __init__(self):
        self.now = 0.0

    def sleep(self, s):
        self.now += max(0.0, s)

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def perf_counter(self):
        return self.now


class _Engine:
    """Browser CDP session: answers the pointer-path command after the walk, like r38."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def send(self, method, params=None):
        if method in H.ENGINE_POINTER_PATH:
            self.calls.append((method, params))
            self.clock.sleep(params["duration"] + 0.12)   # walk, then press + release, then answer
            return {}
        return {}


class _Target:
    def send(self, method, params=None):
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "T1"}}
        raise RuntimeError("not in this fake")


class _Mouse:
    def __init__(self, log):
        self.log = log

    def move(self, x, y, **kw):
        self.log.append(("move", x, y))

    def click(self, x, y, **kw):
        self.log.append(("click", x, y))

    def down(self, **kw):
        self.log.append(("down",))

    def up(self, **kw):
        self.log.append(("up",))

    def wheel(self, *a, **kw):
        pass

    def dblclick(self, *a, **kw):
        pass


class _Keyboard:
    def __getattr__(self, name):
        return lambda *a, **k: None


class _Page:
    def __init__(self, engine):
        self.log = []
        self.mouse = _Mouse(self.log)
        self.keyboard = _Keyboard()
        self.main_frame = object()
        browser = type("B", (), {"new_browser_cdp_session": lambda _self: engine})()
        self.context = type("C", (), {"browser": browser,
                                      "new_cdp_session": lambda _self, page: _Target()})()

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture
def page(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(H, "time", clock)
    monkeypatch.delenv("CLEARCOTE_HUMANIZE_ENGINE_CLICKS", raising=False)
    eng = _Engine(clock)
    p = _Page(eng)
    H.attach_humanize(None, p, humanize=True, seed="route")
    p._eng, p._clock = eng, clock
    return p


def test_consecutive_coordinate_clicks_walk_in_the_engine(page):
    page.mouse.click(300, 300)
    page.mouse.click(400, 200)
    assert [c[1]["x"] for c in page._eng.calls] == [300.0, 400.0]


def test_after_an_sdk_glide_the_engine_does_not_walk_from_its_stale_position(page):
    page.mouse.click(300, 300)            # engine: its cursor is now (300, 300)
    page.mouse.move(800, 550)             # SDK planner glide; the engine does not see it
    n_moves = sum(1 for e in page.log if e[0] == "move")
    page.mouse.click(400, 200)
    assert len(page._eng.calls) == 1, "the engine walked from (300,300) while the pointer was at (800,550)"
    glide = [e for e in page.log if e[0] == "move"][n_moves:]
    assert len(glide) > 10                # the planner walked it instead, from where the pointer was
    assert (glide[-1][1], glide[-1][2]) == (400, 200)
    assert ("down",) in page.log and ("up",) in page.log


def test_the_engine_answer_after_its_walk_is_not_followed_by_a_second_wait(page):
    page.mouse.click(300, 300)
    _, params = page._eng.calls[0]
    t0 = page._clock.now
    page.mouse.click(310, 320)
    _, params = page._eng.calls[1]
    spent = page._clock.now - t0
    # the fake engine answers after duration + 0.12 s; nothing more should be added
    assert spent == pytest.approx(params["duration"] + 0.12, abs=1e-9)


def test_engine_clicks_off_keeps_every_click_on_the_planner(page, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_ENGINE_CLICKS", "off")
    page.mouse.click(300, 300)
    page.mouse.click(400, 200)
    assert page._eng.calls == []
    assert sum(1 for e in page.log if e[0] == "down") == 2


def test_engine_clicks_mode_values(monkeypatch):
    for v, want in (("", "coords"), ("0", "off"), ("off", "off"), ("False", "off"), ("all", "all"),
                    ("selector", "all"), ("coords", "coords"), ("whatever", "coords")):
        monkeypatch.setenv("CLEARCOTE_HUMANIZE_ENGINE_CLICKS", v)
        assert H._engine_clicks_mode() == want, v
