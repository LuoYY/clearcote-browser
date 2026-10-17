"""Persona window geometry (engine r39+): the engine opens each persona's own browser window.

WHY THIS FILE EXISTS. Until r38 every seed opened the SAME window while its screen varied: 780x580
headless (the 800x600 headless display minus Chrome's padding) for any launch that skipped the SDK's
fit -- a caller's own ``no_viewport=True``, raw CLI/CDP, Docker -- and the SDK's maximize for every
other launch (SC 202/203: 16 seeds, one outer size; SC 206: 1,000 seeds, the same). Coherent on any
one page, but one window shape across a whole fleet of identities.

From r39 the engine derives the window from the persona (``components/ungoogled/
persona_window_geometry.cc``; ``clearcote._windowgeom`` is its bit-exact port) and applies it to the
real window. These tests lock down:

* the port: the parity vector (``tests/data/window_vectors.txt``) is the engine's own output, generated
  by the standalone C++ test (``persona_window_geometry_test vectors``); Node and .NET assert the same
  file;
* the distribution: maximized common but not universal, no restored size on more than 1 % of seeds,
  every window inside its work area;
* the SDK's side: on an r39 engine a persona launch gets NO ``--screen-info`` and NO CDP fit (the one
  step a page could see), keeps ``no_viewport`` as its default, and an explicit viewport only moves a
  window that hangs over the work area; on older engines nothing changes;
* live (``CLEARCOTE_LIVE_ENGINE``): the page sees exactly the derived window, with zero resize events,
  the same in fresh processes and after a persistent-profile restart.
"""
import collections
import os
import sys

import pytest

import clearcote
from clearcote import _windowgeom as wg
import importlib

_download = importlib.import_module("clearcote.download")  # the module; clearcote.download is a function
from clearcote._geometry import (
    HEADLESS_NORMAL_FRAME,
    _reconcile_plan,
    _reconcile_steps,
    apply_headless_geometry,
    engine_places_persona_window,
    fit_window_over_cdp,
    geometry_is_coherent,
    served_geometry,
)

HERE = os.path.dirname(os.path.abspath(__file__))
VECTORS = os.path.join(HERE, "data", "window_vectors.txt")


# --------------------------------------------------------------- the port
def _vectors():
    with open(VECTORS, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield line.split()


def test_vector_file_is_the_full_engine_table():
    rows = list(_vectors())
    assert len(rows) == 1536, "regenerate with persona_window_geometry_test vectors"
    assert {r[5] for r in rows} == {"0", "1", "2"}


def test_port_matches_the_engine_bit_for_bit():
    """Every rectangle and every saved-placement key equals the engine's (C++ standalone build)."""
    bad = []
    for raw, sw, sh, aw, ah, plat, valid, kind, state, x, y, w, h, key in _vectors():
        seed = wg.mixed_seed(raw)
        g = wg.derive_window(seed, sw, sh, aw, ah, int(plat))
        got = (g["valid"], g.get("kind", "-"), g.get("state", "normal"),
               g.get("x", 0), g.get("y", 0), g.get("width", 0), g.get("height", 0))
        want = (valid == "1", kind, state, int(x), int(y), int(w), int(h))
        if got != want or "%016x" % wg.window_key(seed, sw, sh, aw, ah, int(plat)) != key:
            bad.append((raw, sw, sh, aw, ah, plat, got, want))
    assert not bad, bad[:5]


def test_seed_parse_matches_the_engine():
    """PersonaSeedFromString: an unsigned decimal is the seed, anything else FNV-1a 64."""
    assert wg.fnv1a64("a") == 0xAF63DC4C8601EC8C  # the published FNV-1a 64 test value
    assert wg.fnv1a64("") == 0xCBF29CE484222325
    # MixedPersonaSeed(0) is SplitMix64's first output for state 0
    assert wg.mixed_seed("0") == 0xE220A8397B1DCDAF
    assert wg.mixed_seed("geo-1") == wg.mixed_seed("geo-1")
    assert wg.mixed_seed("1") != wg.mixed_seed("01x")


def test_derivation_is_deterministic_and_seeds_differ():
    a = wg.derive_window(wg.mixed_seed("7"), 1920, 1080, 1920, 1032)
    assert a == wg.derive_window(wg.mixed_seed("7"), 1920, 1080, 1920, 1032)
    seen = {tuple(sorted(wg.derive_window(wg.mixed_seed(str(i)), 1920, 1080, 1920, 1032).items()))
            for i in range(200)}
    assert len(seen) > 70  # ~36 % restored windows, almost all distinct, plus distinct restore rects


def test_too_small_or_inconsistent_work_areas_get_no_window():
    assert wg.derive_window(1, 800, 600, 800, 500)["valid"] is False   # shorter than a desktop's
    assert wg.derive_window(1, 1024, 768, 640, 768)["valid"] is False  # narrower than a desktop's
    assert wg.derive_window(1, 1366, 768, 1400, 728)["valid"] is False  # avail wider than screen


@pytest.mark.parametrize("screen", [(1366, 768, 1366, 728), (1920, 1080, 1920, 1032),
                                    (1920, 1080, 1920, 1080), (2560, 1440, 2560, 1392),
                                    (3440, 1440, 3440, 1392), (3840, 2160, 3840, 2112)])
@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_distribution_over_2000_seeds(screen, platform):
    """The release-gate shape, per screen: every window inside the work area, maximized the common
    state (40-75 %), no restored size on more than 1 % of identities, snapped halves Windows-only."""
    sw, sh, aw, ah = screen
    n = 2000
    states, sizes = collections.Counter(), collections.Counter()
    for i in range(1, n + 1):
        g = wg.derive_window(wg.mixed_seed(str(i)), sw, sh, aw, ah, platform)
        assert g["valid"]
        assert 0 <= g["x"] and 0 <= g["y"] and g["x"] + g["width"] <= aw and g["y"] + g["height"] <= ah, g
        assert g["kind"] not in ("snap-left", "snap-right") or platform == "windows"
        states[g["state"]] += 1
        if g["state"] == "normal" and not g["kind"].startswith("snap"):
            sizes[(g["width"], g["height"])] += 1
            assert g["width"] >= min(aw, 900) and g["height"] >= min(ah, 600), g
    share = states["maximized"] / n
    assert 0.40 <= share <= 0.75, share
    assert max(sizes.values()) <= n // 100, sizes.most_common(3)
    # Chrome's own fresh-profile box (10,10 + half the work area minus 15) is a fleet tell, never drawn
    assert not any(k == (aw // 2 - 15, ah - 20) for k in sizes)


# --------------------------------------------------------------- the engine probe
def _engine(tmp_path, with_switch=True):
    """A stand-in engine binary: the probe looks for the NUL-delimited switch literal (in chrome.dll
    next to the executable on Windows)."""
    body = b"\x00junk\x00" + (b"\x00fingerprint-window\x00" if with_switch else b"\x00fingerprint\x00")
    exe = tmp_path / "fake-chrome"  # not "chrome.exe": the install check would want a full browser dir
    exe.write_bytes(body)
    if sys.platform.startswith("win"):
        (tmp_path / "chrome.dll").write_bytes(body)
    return str(exe)


def test_engine_places_persona_window_needs_a_persona_and_the_switch(tmp_path):
    new_dir = tmp_path / "new"
    new_dir.mkdir()
    new = _engine(new_dir)
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old = _engine(old_dir, with_switch=False)
    assert engine_places_persona_window(new, ["--fingerprint=7"]) is True
    assert engine_places_persona_window(new, ["--persona-from-env"]) is True
    assert engine_places_persona_window(old, ["--fingerprint=7"]) is False      # r38 and older
    assert engine_places_persona_window(new, []) is False                       # no persona
    assert engine_places_persona_window(None, ["--fingerprint=7"]) is False
    assert engine_places_persona_window(new, ["--fingerprint=7", "--fingerprint-window=off"]) is False
    assert engine_places_persona_window(new, ["--fingerprint=7", "--fingerprint-platform=android"]) is False
    # a schema 1-3 pin keeps the old window unless the caller turns it on
    assert engine_places_persona_window(new, ["--fingerprint=7", "--fingerprint-schema=3"]) is False
    assert engine_places_persona_window(
        new, ["--fingerprint=7", "--fingerprint-schema=3", "--fingerprint-window=on"]) is True
    assert engine_places_persona_window(new, ["--fingerprint=7", "--fingerprint-schema=4"]) is True


# --------------------------------------------------------------- the launch plan
def test_native_persona_plan_sends_no_display_and_no_fit(tmp_path):
    exe = _engine(tmp_path)
    kw = {}
    geom = apply_headless_geometry(kw, "7", ["--fingerprint=7"], exe=exe)
    assert geom == {"mode": "persona-native", "args": [], "fit": False, "reconcile": False, "viewport": None}
    assert kw["no_viewport"] is True


@pytest.mark.parametrize("explicit,reconcile", [
    ({"no_viewport": True}, False),            # the SC 203 configuration: the engine does it all
    ({"viewport": None}, False),
    ({"viewport": {"width": 1280, "height": 720}}, True),
    ({"screen": {"width": 1280, "height": 720}}, False),
])
def test_native_persona_plan_keeps_explicit_choices(tmp_path, explicit, reconcile):
    exe = _engine(tmp_path)
    kw = dict(explicit)
    geom = apply_headless_geometry(kw, "7", ["--fingerprint=7"], exe=exe)
    assert geom["mode"] == "persona-native" and geom["args"] == [] and geom["reconcile"] is reconcile
    assert kw == explicit, "an explicit geometry option must reach Playwright untouched"


def test_older_engine_keeps_the_old_plan(tmp_path):
    exe = _engine(tmp_path, with_switch=False)
    kw = {}
    geom = apply_headless_geometry(kw, "7", ["--fingerprint=7"], exe=exe)
    assert geom["mode"] == "persona" and any(a.startswith("--screen-info=") for a in geom["args"])
    assert apply_headless_geometry({"no_viewport": True}, "7", ["--fingerprint=7"], exe=exe) is None


def test_headed_is_left_alone(tmp_path):
    assert apply_headless_geometry({"headless": False}, "7", ["--fingerprint=7"], exe=_engine(tmp_path)) is None


@pytest.mark.parametrize("bounds,avail,want", [
    ({"left": 0, "top": 0, "width": 1296, "height": 854, "windowState": "normal"}, (1920, 1032), None),
    ({"left": 900, "top": 300, "width": 1296, "height": 854, "windowState": "normal"}, (1920, 1032), (624, 178)),
    ({"left": 40, "top": 20, "width": 2000, "height": 900, "windowState": "normal"}, (1920, 1032), (0, 20)),
    ({"left": 0, "top": 0, "width": 1920, "height": 1032, "windowState": "maximized"}, (1920, 1032), None),
])
def test_reconcile_only_moves_a_window_back_onto_the_work_area(bounds, avail, want):
    assert _reconcile_plan(bounds, avail) == want


FW, FH = HEADLESS_NORMAL_FRAME


def test_reconcile_restores_a_maximized_window_and_frames_the_viewport():
    """Measured on the r39 test build: Playwright's viewport-sized request only RESTORES a maximized
    persona window (Chromium drops the size), leaving outer 1493x830 around inner 1280x720. The
    reconcile asks for the normal state, then viewport + the engine's normal frame, then a move."""
    win = {"windowId": 3, "bounds": {"left": 0, "top": 22, "width": 1493, "height": 830, "windowState": "normal"}}
    steps, final = _reconcile_steps(win, (1920, 1032), {"width": 1280, "height": 720})
    assert steps == [{"width": 1280 + FW, "height": 720 + FH}]
    assert (final["width"], final["height"], final["left"], final["top"]) == (1280 + FW, 720 + FH, 0, 22)
    maxed = {"windowId": 3, "bounds": {"left": 0, "top": 0, "width": 1920, "height": 1032, "windowState": "maximized"}}
    steps, _ = _reconcile_steps(maxed, (1920, 1032), {"width": 1280, "height": 720})
    assert steps[:2] == [{"windowState": "normal"}, {"width": 1280 + FW, "height": 720 + FH}]


def test_reconcile_leaves_a_window_that_already_agrees_alone():
    win = {"windowId": 3, "bounds": {"left": 100, "top": 20, "width": 1280 + FW, "height": 720 + FH,
                                     "windowState": "normal"}}
    assert _reconcile_steps(win, (1920, 1032), {"width": 1280, "height": 720})[0] == []


def test_reconcile_never_resizes_without_a_viewport():
    win = {"windowId": 3, "bounds": {"left": 900, "top": 300, "width": 1296, "height": 854, "windowState": "normal"}}
    assert _reconcile_steps(win, (1920, 1032), None)[0] == [{"left": 624, "top": 178}]


def test_headless_normal_frame_is_the_host_engines():
    assert HEADLESS_NORMAL_FRAME == ((16, 129) if sys.platform.startswith("win") else (0, 121))


class _Cdp:
    def __init__(self, bounds):
        self.calls = []
        self.bounds = bounds

    def send(self, method, params=None, session_id=None):
        self.calls.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "T", "type": "page"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "S"}
        if method == "Runtime.evaluate":
            e = params["expression"]
            if "availLeft" in e:
                return {"result": {"value": [1920, 1080, 0, 0, 1920, 1032]}}
            return {"result": {"value": [self.bounds["width"], self.bounds["height"]]}}
        if method == "Browser.getWindowForTarget":
            return {"windowId": 3, "bounds": dict(self.bounds)}
        if method == "Browser.setWindowBounds":
            b = params["bounds"]
            self.bounds.update({k: b[k] for k in ("left", "top", "width", "height") if k in b})
            self.bounds["windowState"] = b.get("windowState", "normal")
        return {}


def test_served_native_persona_gets_nothing_without_a_window_size(tmp_path):
    exe = _engine(tmp_path)
    g = served_geometry(["--fingerprint=7"], "7", exe=exe)
    assert g == {"persona": True, "native": True, "display": None, "args": [], "fit": False}
    cdp = _Cdp({"left": 120, "top": 30, "width": 1400, "height": 900, "windowState": "normal"})
    assert fit_window_over_cdp(cdp, True, None, native=True) is None
    assert not [c for c in cdp.calls if c[0] in ("Emulation.updateScreen", "Browser.setWindowBounds")]


def test_served_native_persona_window_size_keeps_the_persona_position(tmp_path):
    exe = _engine(tmp_path)
    assert served_geometry(["--fingerprint=7"], "7", exe=exe, window_size=(1200, 800))["fit"] is True
    cdp = _Cdp({"left": 900, "top": 30, "width": 1400, "height": 900, "windowState": "normal"})
    out = fit_window_over_cdp(cdp, True, (1200, 800), settle_timeout=0.05, native=True)
    assert out["outer"] == (1200, 800)
    assert not [c for c in cdp.calls if c[0] == "Emulation.updateScreen"], "the display already is the persona's"
    sets = [c[1]["bounds"] for c in cdp.calls if c[0] == "Browser.setWindowBounds"]
    assert sets[-1] == {"left": 720, "top": 30, "width": 1200, "height": 800}  # moved just enough


def test_served_native_maximized_window_is_restored_before_resizing(tmp_path):
    cdp = _Cdp({"left": 0, "top": 0, "width": 1920, "height": 1032, "windowState": "maximized"})
    fit_window_over_cdp(cdp, True, (1200, 800), settle_timeout=0.05, native=True)
    sets = [c[1]["bounds"] for c in cdp.calls if c[0] == "Browser.setWindowBounds"]
    assert sets[0] == {"windowState": "normal"}


class _Recorder:
    """A fake Playwright that records launch options and every CDP call the SDK makes."""

    def __init__(self, bounds=None):
        self.context_kwargs = None
        self.launch_kwargs = None
        self.cdp = _Cdp(bounds or {"left": 0, "top": 0, "width": 1920, "height": 1032, "windowState": "normal"})

    def install(self, monkeypatch):
        rec = self

        class _Page:
            def __init__(self):
                class _Ctx:
                    def new_cdp_session(self, page):
                        return rec.cdp
                self.context = _Ctx()

            def evaluate(self, js):
                return [1920, 1032] if "availWidth" in js else [rec.cdp.bounds["width"], rec.cdp.bounds["height"]]

        class _Context:
            def __init__(self, with_page=True):
                self.pages = [_Page()] if with_page else []

            def on(self, *a, **k):
                pass

            def new_page(self, **kw):
                return _Page()

        class _Browser(_Context):
            def __init__(self):
                super().__init__(with_page=False)

            def new_context(self, **kw):
                return _Context(with_page=False)

        class _Chromium:
            def launch(self, **kw):
                rec.launch_kwargs = kw
                return _Browser()

            def launch_persistent_context(self, user_data_dir, **kw):
                rec.context_kwargs = kw
                return _Context()

        class _PW:
            chromium = _Chromium()

        monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
        monkeypatch.setattr(clearcote, "install_humanize", lambda *a, **k: None)
        monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)
        # The stand-in engine's chrome.dll (the switch probe reads it on Windows) would make the
        # install check expect a complete browser directory around it.
        monkeypatch.setattr(_download, "check_install", lambda exe: None)
        return self

    def window_calls(self):
        return [c for c in self.cdp.calls if c[0].startswith("Browser.setWindowBounds")]


def test_persistent_context_on_r39_sends_no_screen_info_and_never_resizes(monkeypatch, tmp_path):
    rec = _Recorder().install(monkeypatch)
    clearcote.launch_persistent_context(str(tmp_path / "p"), executable_path=_engine(tmp_path),
                                        fingerprint="7", quiet=True)
    assert rec.context_kwargs["no_viewport"] is True
    assert not [a for a in rec.context_kwargs["args"] if a.startswith(("--screen-info", "--window-"))]
    assert rec.window_calls() == []


def test_persistent_context_on_r39_with_no_viewport_is_the_engines_alone(monkeypatch, tmp_path):
    rec = _Recorder().install(monkeypatch)
    clearcote.launch_persistent_context(str(tmp_path / "p"), executable_path=_engine(tmp_path),
                                        fingerprint="7", no_viewport=True, quiet=True)
    assert rec.cdp.calls == []


def test_persistent_context_on_r39_with_a_viewport_frames_it_and_keeps_it_on_the_work_area(monkeypatch, tmp_path):
    rec = _Recorder({"left": 900, "top": 300, "width": 1296, "height": 854, "windowState": "normal"})
    rec.install(monkeypatch)
    clearcote.launch_persistent_context(str(tmp_path / "p"), executable_path=_engine(tmp_path),
                                        fingerprint="7", viewport={"width": 1280, "height": 720}, quiet=True)
    assert rec.context_kwargs["viewport"] == {"width": 1280, "height": 720}
    w, h = 1280 + FW, 720 + FH
    assert rec.window_calls() == [
        ("Browser.setWindowBounds", {"windowId": 3, "bounds": {"width": w, "height": h}}),
        ("Browser.setWindowBounds", {"windowId": 3, "bounds": {"left": min(900, 1920 - w), "top": min(300, 1032 - h)}}),
    ]


def test_launch_on_r39_new_pages_get_no_fit(monkeypatch, tmp_path):
    rec = _Recorder().install(monkeypatch)
    browser = clearcote.launch(executable_path=_engine(tmp_path), fingerprint="7", quiet=True,
                               ephemeral_profile=False)
    browser.new_page()
    browser.new_context().new_page()
    assert rec.window_calls() == []
    assert not [a for a in rec.launch_kwargs["args"] if a.startswith(("--screen-info", "--window-"))]


def test_launch_on_an_older_engine_still_fits(monkeypatch, tmp_path):
    rec = _Recorder().install(monkeypatch)
    browser = clearcote.launch(executable_path=_engine(tmp_path, with_switch=False), fingerprint="7",
                               quiet=True, ephemeral_profile=False)
    browser.new_page()
    assert rec.window_calls(), "r38 and older engines keep the work-area fit"


def test_headed_linux_fit_is_skipped_for_an_r39_persona(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    exe = _engine(tmp_path)
    assert clearcote._headed_linux_fit(["--fingerprint=7"], exe) is False
    assert clearcote._headed_linux_fit([], exe) is True


# --------------------------------------------------------------- live engine (r39+)
LIVE_EXE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
live_r39 = pytest.mark.skipif(
    not LIVE_EXE or not engine_places_persona_window(LIVE_EXE, ["--fingerprint=1"]),
    reason="set CLEARCOTE_LIVE_ENGINE to an r39+ engine (plus CLEARCOTE_LICENSE_KEY for PRO)")

_RECORDER = ("window.__resizes = 0; "
             "addEventListener('resize', () => { window.__resizes++; }, true);")
_READ = """() => ({screen: [screen.width, screen.height], avail: [screen.availWidth, screen.availHeight],
  outer: [outerWidth, outerHeight], inner: [innerWidth, innerHeight], pos: [screenX, screenY],
  vv: [visualViewport.width, visualViewport.height], client: [document.documentElement.clientWidth,
  document.documentElement.clientHeight], dpr: devicePixelRatio, resizes: window.__resizes,
  media: matchMedia(`(device-width: ${screen.width}px) and (device-height: ${screen.height}px)`).matches})"""
_PAGE = "data:text/html,<!doctype html><body style='margin:0'>geo</body>"
_HOST_PLATFORM = "windows" if sys.platform.startswith("win") else "linux"


def _live(udd, seed, **kw):
    ctx = clearcote.launch_persistent_context(str(udd), executable_path=LIVE_EXE, args=["--no-sandbox"],
                                              quiet=True, fingerprint=seed, **kw)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.add_init_script(_RECORDER)
        page.goto(_PAGE)
        page.wait_for_timeout(500)
        return page.evaluate(_READ)
    finally:
        ctx.close()


def _expect(seed, m, platform=_HOST_PLATFORM):
    return wg.derive_window(wg.mixed_seed(seed), *m["screen"], *m["avail"], platform)


def _assert_is_the_derived_window(seed, m):
    g = _expect(seed, m)
    assert g["valid"], m
    if g["state"] == "maximized":
        assert m["outer"] == m["avail"] and m["pos"] == [0, 0], (g, m)
    else:
        assert m["outer"] == [g["width"], g["height"]] and m["pos"] == [g["x"], g["y"]], (g, m)
    assert geometry_is_coherent(m["screen"], m["avail"], m["inner"], m["outer"]), m
    assert m["vv"] == m["inner"] == m["client"] or m["client"][0] < m["inner"][0], m  # scrollbar aside
    assert m["media"] is True, m
    assert m["resizes"] == 0, f"a page saw the window being resized: {m}"


@live_r39
@pytest.mark.parametrize("seed", ["1", "2", "3", "7", "16", "live-window-a"])
def test_live_page_sees_exactly_the_derived_window(tmp_path, seed):
    _assert_is_the_derived_window(seed, _live(tmp_path / ("w" + seed), seed))


@live_r39
def test_live_no_viewport_is_the_derived_window_too(tmp_path):
    """The SC 203 configuration (``no_viewport=True``) was the 780x580 case."""
    m = _live(tmp_path / "nv", "3", no_viewport=True)
    assert m["outer"] != [780, 580]
    _assert_is_the_derived_window("3", m)


@live_r39
def test_live_same_seed_same_window_in_fresh_processes(tmp_path):
    a = _live(tmp_path / "a", "42")
    b = _live(tmp_path / "b", "42")
    assert (a["screen"], a["avail"], a["outer"], a["pos"]) == (b["screen"], b["avail"], b["outer"], b["pos"])


@live_r39
def test_live_persistent_profile_keeps_its_window_across_restarts(tmp_path):
    udd = tmp_path / "keep"
    first = _live(udd, "11")
    second = _live(udd, "11")
    assert (first["outer"], first["pos"]) == (second["outer"], second["pos"])
    _assert_is_the_derived_window("11", second)


@live_r39
def test_live_a_profile_reused_with_another_seed_takes_the_new_window(tmp_path):
    udd = tmp_path / "reuse"
    _live(udd, "11")
    m = _live(udd, "12")
    _assert_is_the_derived_window("12", m)


@live_r39
def test_live_explicit_window_size_wins(tmp_path):
    ctx = clearcote.launch_persistent_context(str(tmp_path / "ws"), executable_path=LIVE_EXE, quiet=True,
                                              args=["--no-sandbox", "--window-size=1200,800"], fingerprint="5")
    try:
        page = ctx.pages[0]
        page.goto(_PAGE)
        m = page.evaluate(_READ)
        assert m["outer"] == [1200, 800], m
        assert m["pos"][0] + 1200 <= m["avail"][0] and m["pos"][1] + 800 <= m["avail"][1], m
    finally:
        ctx.close()


@live_r39
def test_live_start_maximized_wins(tmp_path):
    ctx = clearcote.launch_persistent_context(str(tmp_path / "mx"), executable_path=LIVE_EXE, quiet=True,
                                              args=["--no-sandbox", "--start-maximized"], fingerprint="5")
    try:
        page = ctx.pages[0]
        page.goto(_PAGE)
        m = page.evaluate(_READ)
        assert m["outer"] == m["avail"] and m["pos"] == [0, 0], m
    finally:
        ctx.close()


@live_r39
@pytest.mark.parametrize("seed", ["1", "2", "3", "9"])
def test_live_explicit_viewport_is_kept_framed_and_on_the_work_area(tmp_path, seed):
    """Seeds 1 and 2 have maximized persona windows: before the reconcile change they reported outer
    1493x830 / an outer height equal to the inner one around a 1280x720 viewport (r39 test build)."""
    m = _live(tmp_path / ("vp" + seed), seed, viewport={"width": 1280, "height": 720})
    assert m["inner"] == [1280, 720], m
    assert m["outer"] == [1280 + FW, 720 + FH], f"outer and inner must differ by the engine's frame: {m}"
    assert m["pos"][0] + m["outer"][0] <= m["avail"][0] and m["pos"][1] + m["outer"][1] <= m["avail"][1], m
    assert m["resizes"] == 0, m


@live_r39
def test_live_fingerprint_window_off_restores_r38(tmp_path):
    ctx = clearcote.launch_persistent_context(str(tmp_path / "off"), executable_path=LIVE_EXE, quiet=True,
                                              args=["--no-sandbox", "--fingerprint-window=off"],
                                              fingerprint="3", no_viewport=True)
    try:
        page = ctx.pages[0]
        page.goto(_PAGE)
        m = page.evaluate(_READ)
        assert m["outer"] == [780, 580], f"--fingerprint-window=off must keep the r38 window: {m}"
    finally:
        ctx.close()
