"""Lane F (SC 232) live: frame routing, option dicts, label checkboxes, popups, persistent contexts, tap.

Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary. Headless only. A local HTTP server serves
the fixture on 127.0.0.1; the "cross" frame loads from localhost, another SITE, so it runs out of
process (no CDP session the SDK holds) and must fall back to Playwright with the fallback reported.
Every document records its input events with isTrusted in window.__ev.
"""
import http.server
import os
import shutil
import socketserver
import tempfile
import threading

import pytest

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")

REC = """<script>
window.__ev = [];
for (const t of ['mousedown','mouseup','click','input','change','keydown','keyup','pointerdown'])
  document.addEventListener(t, (e) => window.__ev.push({t, id: (e.target && e.target.id) || '',
    tr: e.isTrusted, ts: performance.now(), key: e.key || ''}), true);
</script>"""

OPTS = "".join("<option value='v%d'>%s</option>" % (i, n) for i, n in enumerate(
    ["Afghanistan", "Albania", "Germany", "Ghana", "Greece", "Netherlands", "Norway", "Peru", "Spain", "Uruguay"]))

CHILD = """<!doctype html><html><head><meta charset=utf-8>%s<style>body{margin:8px;font:14px sans-serif}
input,select,button{display:block;margin:6px 0}</style></head><body>
<select id=cs>%s</select><input id=ct value='old text'><input id=cc type=checkbox>
<button id=cb>Child button</button></body></html>""" % (REC, OPTS)

OUTER = """<!doctype html><html><head>%s</head><body style='margin:20px'>
<p>outer</p><iframe id=inner src='/child.html' style='width:420px;height:220px;border:3px solid #888;padding:5px'></iframe>
</body></html>""" % REC

MAIN = """<!doctype html><html><head><meta charset=utf-8>%s<style>
body{margin:10px;font:15px sans-serif} iframe{display:block;margin:8px 0}
.vh{position:absolute;opacity:0;width:0;height:0;margin:0}
.box{display:inline-block;width:16px;height:16px;border:2px solid #333;margin-right:6px;vertical-align:middle}
</style></head><body>
<select id=s>%s</select>
<input type=checkbox id=hcb class=vh><label for=hcb id=hlab><span class=box></span>Accept the terms</label><br>
<label id=wrap><input type=checkbox id=hcb2 class=vh><span class=box></span>Subscribe to news</label><br>
<input type=checkbox id=vcb><label for=vcb id=vlab>Visible box with a label</label><br>
<button id=btn>Main button</button>
<a id=pop href='/child.html' target=_blank>open popup</a>
<iframe id=same name=same src='/child.html' style='width:460px;height:230px;border:4px solid #444;padding:6px'></iframe>
<iframe id=nested src='/outer.html' style='width:520px;height:330px;border:2px solid #aaa'></iframe>
<iframe id=cross src='{CROSS}/child.html' style='width:460px;height:230px'></iframe>
</body></html>""" % (REC, OPTS)


class _Handler(http.server.BaseHTTPRequestHandler):
    pages = {}

    def do_GET(self):  # noqa: N802
        body = self.pages.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    _Handler.pages = {"/main.html": MAIN.replace("{CROSS}", "http://localhost:%d" % port),
                      "/child.html": CHILD, "/outer.html": OUTER}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % port
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    import clearcote
    b = clearcote.launch(executable_path=LIVE, headless=True, humanize=True, fingerprint="coverage-live-1",
                         quiet=True)
    yield b
    b.close()


@pytest.fixture
def page(browser, server):
    p = browser.new_page()
    p.goto(server + "/main.html")
    p.wait_for_load_state("load")
    yield p
    p.close()


def ev(frame, t=None, id=None):
    return [e for e in frame.evaluate("window.__ev") if (t is None or e["t"] == t) and (id is None or e["id"] == id)]


def reset(frame):
    frame.evaluate("window.__ev.length = 0")


def child(page, which):
    if which == "same":
        return page.frame(name="same")
    outer = page.frame_locator("#nested")
    for f in page.frames:
        if f.parent_frame is not None and f.parent_frame.parent_frame is not None:
            return f
    raise AssertionError("nested frame not found: %r" % outer)


def floc(page, which):
    return page.frame_locator("#same") if which == "same" else page.frame_locator("#nested").frame_locator("#inner")


# ------------------------------------------------------------------------------- frame select
@pytest.mark.parametrize("run", range(6))
def test_frame_select_is_one_trusted_change(page, run):
    which = "same" if run % 2 == 0 else "nested"
    names = ["Peru", "Ghana", "Spain", "Norway", "Greece", "Uruguay"]
    f = child(page, which)
    reset(f)
    got = floc(page, which).locator("#cs").select_option(label=names[run])
    assert got == ["v%d" % ["Afghanistan", "Albania", "Germany", "Ghana", "Greece", "Netherlands", "Norway",
                            "Peru", "Spain", "Uruguay"].index(names[run])]
    changes = ev(f, "change", "cs")
    assert len(changes) == 1 and changes[0]["tr"], "ONE trusted change (was untrusted: verifier B #4)"
    assert all(e["tr"] for e in ev(f, "input", "cs"))
    downs = ev(f, "mousedown", "cs")
    assert downs and all(d["tr"] for d in downs), "opened with a trusted click"
    assert getattr(page, "_clearcote_last_fallback", None) is None, page._clearcote_last_fallback


def test_frame_api_select_and_click_are_humanized(page):
    f = child(page, "same")
    reset(f)
    assert f.select_option("#cs", "Germany") == ["v2"]
    assert [e["tr"] for e in ev(f, "change", "cs")] == [True]
    f.click("#cb")
    downs, ups = ev(f, "mousedown", "cb"), ev(f, "mouseup", "cb")
    assert len(downs) == 1 and downs[0]["tr"] and ups[0]["ts"] - downs[0]["ts"] >= 25, "a finger's hold"


def test_frame_fill_and_check_are_humanized(page):
    fl = floc(page, "nested")
    f = child(page, "nested")
    reset(f)
    fl.locator("#ct").fill("new words")
    assert fl.locator("#ct").input_value() == "new words"
    keys = ev(f, "keydown", "ct")
    assert len(keys) >= len("new words") and all(k["tr"] for k in keys)
    fl.locator("#cc").check()
    assert fl.locator("#cc").is_checked()
    assert [e["tr"] for e in ev(f, "click", "cc")] == [True]


def test_cross_origin_frame_falls_back_and_reports(page):
    page._clearcote_last_fallback = None
    got = page.frame_locator("#cross").locator("#cs").select_option("Peru")
    assert got == ["v7"]
    fb = page._clearcote_last_fallback
    assert fb and fb["api"] == "Locator.select_option" and "cross-origin" in fb["reason"], fb


# ------------------------------------------------------------------------------- option dicts
@pytest.mark.parametrize("arg,want", [({"label": "Greece"}, "v4"), ({"value": "v8"}, "v8"), ({"index": 6}, "v6"),
                                      ([{"label": "Peru"}, {"value": "v9"}], "v7")])
def test_option_dicts_are_one_trusted_change(page, arg, want):
    reset(page)
    assert page.select_option("#s", arg) == [want]
    assert page.locator("#s").input_value() == want
    assert [e["tr"] for e in ev(page, "change", "s")] == [True]


# ------------------------------------------------------------------------------- label checkbox
@pytest.mark.parametrize("run", range(6))
def test_hidden_custom_checkbox_is_checked_through_its_label(page, run):
    sel = "#hcb" if run % 2 == 0 else "#hcb2"
    lab = "hlab" if run % 2 == 0 else "wrap"
    reset(page)
    page.locator(sel).check(timeout=3000)
    assert page.locator(sel).is_checked()
    clicks = [e for e in ev(page, "click") if e["tr"]]
    assert clicks, "a trusted click"
    downs = ev(page, "mousedown")
    assert downs and all(d["tr"] for d in downs)
    assert any(d["id"] in (lab, "") for d in downs), downs   # on the label (or its decorative span)
    page.locator(sel).uncheck(timeout=3000)
    assert not page.locator(sel).is_checked()


def test_visible_checkbox_sometimes_clicks_its_label(browser, server):
    on_label = 0
    for i in range(10):
        p = browser.new_page()
        try:
            p.goto(server + "/main.html")
            reset(p)
            p.check("#vcb")
            assert p.is_checked("#vcb")
            on_label += any(d["id"] == "vlab" for d in ev(p, "mousedown"))
        finally:
            p.close()
    assert 0 < on_label < 10, on_label


# ------------------------------------------------------------------------------- attach
def test_a_popup_is_humanized(page):
    with page.context.expect_page() as pi:
        page.click("#pop")
    popup = pi.value
    popup.wait_for_load_state("load")
    assert getattr(popup, "_clearcote_humanized", False) is True
    reset(popup)
    popup.click("#cb")
    downs, ups = ev(popup, "mousedown", "cb"), ev(popup, "mouseup", "cb")
    assert downs and ups[0]["ts"] - downs[0]["ts"] >= 25
    popup.close()


def test_a_persistent_context_is_humanized(server):
    import clearcote
    d = tempfile.mkdtemp(prefix="cc-cov-")
    try:
        ctx = clearcote.launch_persistent_context(d, executable_path=LIVE, headless=True, humanize=True,
                                                  fingerprint="coverage-live-2", quiet=True)
        try:
            p = ctx.pages[0] if ctx.pages else ctx.new_page()
            assert getattr(p, "_clearcote_humanized", False) is True
            p2 = ctx.new_page()
            assert getattr(p2, "_clearcote_humanized", False) is True
        finally:
            ctx.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------------------- tap
def test_tap_on_a_touch_context_reports_the_fallback(browser, server):
    ctx = browser.new_context(has_touch=True)
    try:
        p = ctx.new_page()
        p.goto(server + "/main.html")
        p.locator("#btn").tap()
        fb = getattr(p, "_clearcote_last_fallback", None)
        assert fb and fb["api"] == "Locator.tap" and "not humanized for touch" in fb["reason"]
        p._clearcote_strict = True     # lane A's strict flag (read defensively)
        from playwright.sync_api import Error
        with pytest.raises(Error, match="tap is not humanized for touch contexts yet"):
            p.locator("#btn").tap()
    finally:
        ctx.close()
