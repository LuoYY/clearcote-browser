"""LIVE: press_and_hold as the PAGE sees it -- one trusted press on the element, held for the asked
time with the button reported down on every move, the drift within 2 px, `until` releasing early, and
frame locators and non-humanized pages working too.

Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary (and a licence is available for a PRO build).
"""
import asyncio
import os
import time

import pytest

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")

LOGGER = """<script>
window.__ev = [];
for (const t of ['pointerdown', 'pointerup', 'pointermove', 'click', 'contextmenu'])
  addEventListener(t, e => window.__ev.push({t: e.type, ts: e.timeStamp, x: e.clientX, y: e.clientY,
    trusted: e.isTrusted, buttons: e.buttons, button: e.button, id: e.target.id || e.target.tagName}),
    {capture: true, passive: true});
</script>"""

HOLD_BTN = """<button id="hold" style="position:absolute;left:@L@px;top:@T@px;width:220px;height:60px">Press and hold</button>
<div id="done" hidden>done</div>
<script>
let timer = null;
const b = document.getElementById('hold');
b.addEventListener('pointerdown', () => { timer = setTimeout(() => document.getElementById('done').hidden = false, 1500); });
b.addEventListener('pointerup', () => clearTimeout(timer));
b.addEventListener('pointerleave', () => clearTimeout(timer));
</script>"""

PAGE = ("<!doctype html><html><head><meta charset='utf-8'><style>html,body{margin:0;height:2400px}</style></head><body>"
        + LOGGER + HOLD_BTN.replace("@L@", "420").replace("@T@", "300")
        + "<div id='far' style='position:absolute;left:300px;top:1900px;width:160px;height:50px;background:#ddd'>far</div>"
        + "</body></html>")

FRAME_PAGE = ("<!doctype html><html><head><meta charset='utf-8'></head><body style='margin:0'>"
              "<iframe id='f' style='position:absolute;left:200px;top:150px;width:600px;height:300px;border:0' "
              "srcdoc=\"" + ("<body style='margin:0'>" + LOGGER + HOLD_BTN.replace("@L@", "80").replace("@T@", "90")).replace('"', "&quot;")
              + "\"></iframe></body></html>")


@pytest.fixture(scope="module")
def _no_font_cache_left_behind():
    import shutil
    import tempfile
    cache = os.path.join(tempfile.gettempdir(), "cc-fc-cache")
    existed = os.path.exists(cache)
    yield
    if not existed:
        shutil.rmtree(cache, ignore_errors=True)


def _launch(humanize):
    import clearcote
    return clearcote.launch(executable_path=LIVE, headless=True, humanize=humanize, fingerprint="hold-live-1",
                            viewport={"width": 1180, "height": 760}, quiet=True)


@pytest.fixture(scope="module")
def browser(_no_font_cache_left_behind):
    b = _launch(True)
    yield b
    b.close()


@pytest.fixture
def page(browser, tmp_path):
    f = tmp_path / "hold.html"
    f.write_text(PAGE, encoding="utf-8")
    p = browser.new_page()
    p.goto(f.as_uri())
    yield p
    p.close()


def _held(ev, target="hold"):
    downs = [e for e in ev if e["t"] == "pointerdown"]
    ups = [e for e in ev if e["t"] == "pointerup"]
    assert len(downs) == 1 and len(ups) == 1, [e["t"] for e in ev if e["t"] != "pointermove"]
    d, u = downs[0], ups[0]
    assert d["trusted"] and u["trusted"]
    assert d["id"] == target and u["id"] == target, (d, u)
    during = [e for e in ev if e["t"] == "pointermove" and d["ts"] < e["ts"] < u["ts"]]
    return d, u, during


def test_hold_for_duration(page):
    t0 = time.monotonic()
    assert page.press_and_hold("#hold", 2000) is True
    took = time.monotonic() - t0
    ev = page.evaluate("window.__ev")
    d, u, during = _held(ev)
    held = u["ts"] - d["ts"]
    assert 1950 <= held <= 2250, held
    assert d["button"] == 0 and d["buttons"] == 1
    assert during, "a 2 s hold drifts at least once"
    for m in during:
        assert m["trusted"] and m["buttons"] == 1, m
        assert abs(m["x"] - d["x"]) <= 2 and abs(m["y"] - d["y"]) <= 2, (m, d)
    # the approach was a glide (many moves before the press), not a teleport
    before = [e for e in ev if e["t"] == "pointermove" and e["ts"] < d["ts"]]
    assert len(before) >= 10
    assert took < 6.0
    assert page.locator("#done").is_visible()


def test_until_releases_after_a_reaction(page):
    loc = page.locator("#hold")
    assert loc.press_and_hold(8000, until=lambda: page.locator("#done").is_visible()) is True
    ev = page.evaluate("window.__ev")
    d, u, _ = _held(ev)
    held = u["ts"] - d["ts"]
    # done shows 1500 ms in; polled every 100 ms, then 160-350 ms of reaction
    assert 1600 <= held <= 2050, held


def test_until_never_true(page):
    assert page.press_and_hold("#hold", 700, until=lambda: False, tremor=False) is False
    d, u, during = _held(page.evaluate("window.__ev"))
    assert 650 <= u["ts"] - d["ts"] <= 950
    assert not during, "tremor=False: no moves while held"


def test_position_and_right_button(page):
    page.press_and_hold("#hold", 400, position={"x": 20, "y": 15}, button="right", tremor=False)
    ev = page.evaluate("window.__ev")
    d = [e for e in ev if e["t"] == "pointerdown"][0]
    assert (d["x"], d["y"]) == (440, 315) and d["button"] == 2


def test_scrolls_into_view(page):
    page.press_and_hold("#far", 300, tremor=False)
    ev = page.evaluate("window.__ev")
    assert [e for e in ev if e["t"] == "pointerdown"][0]["id"] == "far"
    assert page.evaluate("scrollY") > 1000


def test_frame_locator(browser, tmp_path):
    f = tmp_path / "frame.html"
    f.write_text(FRAME_PAGE, encoding="utf-8")
    p = browser.new_page()
    try:
        p.goto(f.as_uri())
        btn = p.frame_locator("#f").locator("#hold")
        btn.wait_for()
        assert btn.press_and_hold(1800) is True
        ev = p.frames[1].evaluate("window.__ev")
        d, u, during = _held(ev)
        assert u["ts"] - d["ts"] >= 1750
        assert all(m["buttons"] == 1 for m in during)
        assert p.frames[1].locator("#done").is_visible()
    finally:
        p.close()


def test_without_humanize(_no_font_cache_left_behind, tmp_path):
    import clearcote
    f = tmp_path / "plain.html"
    f.write_text(PAGE, encoding="utf-8")
    b = _launch(False)
    try:
        p = b.new_page()
        p.goto(f.as_uri())
        assert not hasattr(p, "press_and_hold")
        assert clearcote.press_and_hold(p.locator("#hold"), 1700) is True
        d, u, during = _held(p.evaluate("window.__ev"))
        assert u["ts"] - d["ts"] >= 1650
        assert all(m["buttons"] == 1 for m in during)
        assert p.locator("#done").is_visible()
    finally:
        b.close()


# --- the press-and-hold widget (sdk/testdata/press_and_hold.html) --------------------------------------
WIDGET = os.path.join(os.path.dirname(__file__), "..", "..", "testdata", "press_and_hold.html")


def _widget_url(ms):
    from pathlib import Path
    return Path(os.path.abspath(WIDGET)).as_uri() + "?ms=%d" % ms


def _done(state):
    return state["state"] == "done"


def test_widget_rejects_a_plain_click_and_a_short_hold(page):
    """The widget is a real gate: a stock click, a too-short hold and a synthetic press all fail."""
    page.goto(_widget_url(1500))
    page.locator("#hold").click()
    page.evaluate("document.getElementById('hold').dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}))")
    page.press_and_hold("#hold", 600)
    s = page.evaluate("window.__hold")
    assert s["state"] == "idle", s
    assert s["attempts"] == 2 and [r["why"] for r in s["resets"]] == ["released", "released"], s
    assert s["rejected"] == ["untrusted"], s


def test_widget_held_until_done(page):
    page.goto(_widget_url(2500))
    ok = page.press_and_hold("#hold", until=lambda: page.locator("#hold[data-state=done]").count() > 0)
    s = page.evaluate("window.__hold")
    assert ok is True and _done(s), s
    assert s["attempts"] == 1 and not s["resets"] and not s["rejected"], s
    assert s["moves"] >= 1 and s["movesNoButton"] == 0, s
    assert page.locator("#status").inner_text() == "Verified"


def test_widget_in_a_frame(browser, tmp_path):
    f = tmp_path / "widget-frame.html"
    f.write_text("<!doctype html><body style='margin:0'><iframe id='f' src='%s' "
                 "style='position:absolute;left:120px;top:60px;width:640px;height:420px;border:0'></iframe>"
                 % _widget_url(2000), encoding="utf-8")
    p = browser.new_page()
    try:
        p.goto(f.as_uri())
        fl = p.frame_locator("#f")
        btn = fl.locator("#hold")
        btn.wait_for()
        assert btn.press_and_hold(until=lambda: fl.locator("#hold[data-state=done]").count() > 0) is True
        s = p.frames[1].evaluate("window.__hold")
        assert _done(s) and s["attempts"] == 1 and s["movesNoButton"] == 0, s
    finally:
        p.close()
