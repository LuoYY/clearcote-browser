"""Live: Widevine ON by default for a licensed (PRO) launch, against a real engine.

Skipped unless CLEARCOTE_LIVE_ENGINE points at a PRO chrome binary (r32+, it has --widevine-cdm-path) AND a licence
key resolves (CLEARCOTE_LICENSE_KEY or ~/.clearcote/license.key). Downloads Google's CDM from Google into a temp
cache once, so it needs the network. Headless. These belong in the release gate.
"""
import http.server
import os
import threading

import pytest

import clearcote
from clearcote import _widevine as W

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE or not clearcote.resolve_license_key(),
                                reason="needs CLEARCOTE_LIVE_ENGINE (a PRO engine) and a licence key")

EME = """async () => { try {
  const a = await navigator.requestMediaKeySystemAccess('com.widevine.alpha', [{initDataTypes: ['cenc'],
    videoCapabilities: [{contentType: 'video/mp4; codecs="avc1.42E01E"'}]}]);
  await a.createMediaKeys(); return 'ok ' + a.keySystem;
} catch (e) { return e.name; } }"""


@pytest.fixture(scope="module")
def page_url():
    # EME needs a secure context: http://127.0.0.1 is one, about:blank and data: are not dependable
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<!doctype html><title>eme</title><p>eme"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def cdm_cache(tmp_path_factory):
    """An EMPTY CDM cache for this module: the first default launch must download, verify and install it."""
    root = str(tmp_path_factory.mktemp("wv-live") / "cache")
    old = os.environ.get("CLEARCOTE_WIDEVINE_DIR")
    os.environ["CLEARCOTE_WIDEVINE_DIR"] = root
    W._reset_default_state()
    yield root
    if old is None:
        os.environ.pop("CLEARCOTE_WIDEVINE_DIR", None)
    else:
        os.environ["CLEARCOTE_WIDEVINE_DIR"] = old


@pytest.fixture(autouse=True)
def _no_opt_out(monkeypatch):
    monkeypatch.delenv("CLEARCOTE_WIDEVINE", raising=False)
    monkeypatch.setenv("CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", "120")  # a first download on a slow link


def _eme(context_or_browser, url):
    page = context_or_browser.new_page()
    page.goto(url)
    return page.evaluate(EME)


def test_a_default_licensed_launch_downloads_the_cdm_and_has_widevine(cdm_cache, page_url, tmp_path):
    assert W.cached_cdm(cdm_cache) is None
    ctx = clearcote.launch_persistent_context(str(tmp_path / "udd"), executable_path=LIVE, headless=True, quiet=True,
                                              args=["--no-sandbox"])
    try:
        assert _eme(ctx, page_url) == "ok com.widevine.alpha"
    finally:
        ctx.close()
    assert W.cached_cdm(cdm_cache) is not None and W._stamp_fresh(cdm_cache)
    assert not [n for n in os.listdir(cdm_cache) if n.startswith((".tmp-", ".trash-", ".install-lock"))]
    assert not os.path.exists(tmp_path / "udd" / "WidevineCdm")  # the default never seeds the profile


def test_a_second_launch_uses_the_cache_without_any_network(cdm_cache, page_url, monkeypatch):
    monkeypatch.setattr(W, "fetch_widevine", lambda *a, **k: pytest.fail("network used"))
    browser = clearcote.launch(executable_path=LIVE, headless=True, quiet=True, args=["--no-sandbox"])  # throwaway profile
    try:
        assert _eme(browser, page_url) == "ok com.widevine.alpha"
    finally:
        browser.close()


def test_an_incognito_launch_has_it_too(cdm_cache, page_url):
    browser = clearcote.launch(executable_path=LIVE, headless=True, quiet=True, ephemeral_profile=False,
                               args=["--no-sandbox"])
    try:
        assert _eme(browser, page_url) == "ok com.widevine.alpha"
    finally:
        browser.close()


@pytest.mark.parametrize("how", ["option", "env"])
def test_opting_out_leaves_no_widevine(cdm_cache, page_url, tmp_path, monkeypatch, how):
    kw = {}
    if how == "env":
        monkeypatch.setenv("CLEARCOTE_WIDEVINE", "0")
    else:
        kw["widevine"] = False
    ctx = clearcote.launch_persistent_context(str(tmp_path / "udd"), executable_path=LIVE, headless=True, quiet=True,
                                              args=["--no-sandbox"], **kw)
    try:
        assert _eme(ctx, page_url) == "NotSupportedError"
    finally:
        ctx.close()


def test_the_async_api_follows(cdm_cache, page_url):
    import asyncio
    from clearcote import async_api

    async def run():
        browser = await async_api.launch(executable_path=LIVE, headless=True, quiet=True, args=["--no-sandbox"])
        try:
            page = await browser.new_page()
            await page.goto(page_url)
            return await page.evaluate(EME)
        finally:
            await browser.close()

    # In a thread of its own: the sync tests above left Playwright's sync driver (and its event loop) on this one.
    out = []
    t = threading.Thread(target=lambda: out.append(asyncio.run(run())))
    t.start()
    t.join(120)
    assert out == ["ok com.widevine.alpha"]
