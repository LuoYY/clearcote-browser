"""LIVE: a planned glide lasts what the planner planned, as the PAGE sees it (SC 210 lane P).

Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary (and a licence is available for a PRO
build). The page records every pointermove's timeStamp; the planner's plan is captured by wrapping
plan_move. On 0.48.0 a ~1 s plan took 3.5-5.4 s on PRO r38 because the ~27 ms move round trip was added
to every 6-8 ms sample (SC 209).
"""
import os
import statistics
import time

import pytest

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;height:100%}</style></head>
<body><script>
window.__pm = [];
addEventListener('pointermove', e => window.__pm.push([e.timeStamp, e.clientX, e.clientY, e.isTrusted, e.buttons]),
                 {capture: true, passive: true});
</script></body></html>"""


@pytest.fixture(scope="module")
def _no_font_cache_left_behind():
    import shutil
    import tempfile
    cache = os.path.join(tempfile.gettempdir(), "cc-fc-cache")
    existed = os.path.exists(cache)
    yield
    if not existed:
        shutil.rmtree(cache, ignore_errors=True)


@pytest.fixture(scope="module")
def browser(_no_font_cache_left_behind):
    import clearcote
    b = clearcote.launch(executable_path=LIVE, headless=True, humanize=True, fingerprint="pace-live-1",
                         viewport={"width": 1180, "height": 760}, quiet=True)
    yield b
    b.close()


@pytest.fixture
def page(browser, tmp_path):
    f = tmp_path / "pace.html"
    f.write_text(PAGE, encoding="utf-8")
    p = browser.new_page()
    p.goto(f.as_uri())
    yield p
    p.close()


@pytest.fixture
def plans(monkeypatch):
    import clearcote._humanize as hz
    out = []
    real = hz.plan_move

    def spy(*a, **k):
        p = real(*a, **k)
        out.append(p)
        return p
    monkeypatch.setattr(hz, "plan_move", spy)
    return out


def test_live_glide_lasts_its_planned_duration(page, plans):
    page.mouse.move(150, 150)
    rows = []
    for (x, y) in [(900, 600), (150, 150), (1000, 200), (200, 650), (900, 600), (150, 150)]:
        page.evaluate("window.__pm.length = 0")
        n0 = len(plans)
        t0 = time.perf_counter()
        page.mouse.move(x, y)
        call_ms = (time.perf_counter() - t0) * 1000.0
        assert len(plans) == n0 + 1, "the glide did not go through the SDK planner"
        plan = plans[-1]
        planned = sum(s[2] for s in plan)
        span_plan = planned - plan[-1][2]          # first sample -> last sample
        pm = page.evaluate("window.__pm")
        assert pm and all(e[3] for e in pm)
        span_page = pm[-1][0] - pm[0][0]
        rows.append((round(planned), round(call_ms), round(span_plan), round(span_page), len(pm)))
        assert (round(pm[-1][1]), round(pm[-1][2])) == (x, y), "the glide must land on its target"
    print("planned_ms call_ms planned_span page_span pointermoves:", rows)
    for planned, call_ms, span_plan, span_page, n in rows:
        assert 0.75 * planned <= call_ms <= 1.25 * planned, rows
        assert 0.75 * span_plan <= span_page <= 1.25 * span_plan, rows


def test_live_ambient_motion_lasts_what_was_asked(page):
    took = []
    for ms in (800, 1500):
        t0 = time.perf_counter()
        page.ambient_motion(ms)
        took.append((ms, round((time.perf_counter() - t0) * 1000.0)))
    print("ambient asked/took:", took)
    for ms, t in took:
        assert 0.75 * ms <= t <= 1.25 * ms, took
    assert statistics.mean(e[3] for e in page.evaluate("window.__pm")) == 1   # all trusted
