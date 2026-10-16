from clearcote._humanize import CURSOR_OVERLAY, install_humanize


def _fake_browser():
    b = type("FakeBrowser", (), {})()
    b.new_page = lambda **kw: object()
    b.new_context = lambda **kw: object()
    return b


def test_install_is_noop_when_off():
    b = _fake_browser()
    np, nc = b.new_page, b.new_context
    install_humanize(b, humanize=False, show_cursor=False)
    assert b.new_page is np and b.new_context is nc


def test_install_wraps_when_humanize_on():
    b = _fake_browser()
    np = b.new_page
    install_humanize(b, humanize=True)
    assert b.new_page is not np


def test_install_wraps_when_show_cursor_on():
    b = _fake_browser()
    nc = b.new_context
    install_humanize(b, show_cursor=True)
    assert b.new_context is not nc


def test_cursor_overlay_is_idempotent_iife():
    # runs as both an add_init_script source and via page.evaluate, so it must be a self-calling
    # IIFE guarded against double-injection.
    assert "__clearcoteCursor" in CURSOR_OVERLAY
    body = CURSOR_OVERLAY.strip()
    assert body.startswith("(()") and body.endswith(")();")


class _SyncKeyboard:
    def __init__(self):
        self.events = []

    def press(self, key, **kw):
        self.events.append(("press", key))

    def type(self, text, **kw):
        self.events.append(("type", text))

    def down(self, key):
        self.events.append(("down", key))

    def up(self, key):
        self.events.append(("up", key))


class _SyncPage:
    """Just enough of a sync Page for attach_humanize to wire up; every other method is a no-op."""

    def __init__(self):
        self.keyboard = _SyncKeyboard()
        self.main_frame = object()

    def __getattr__(self, name):
        if name == "mouse":
            m = type("Mouse", (), {})()
            for n in ("move", "click", "wheel", "down", "up", "dblclick"):
                setattr(m, n, lambda *a, **k: None)
            self.__dict__["mouse"] = m
            return m
        return lambda *a, **k: None


def test_sync_typing_holds_shift_for_capitals_and_symbols(monkeypatch):
    from clearcote._humanize import attach_humanize
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0")   # an exact key sequence: no slips
    page = _SyncPage()
    attach_humanize(None, page, humanize=True)
    page.keyboard.type("Hi!")
    ev = [e for e in page.keyboard.events if e[1] in ("Shift", "H", "i", "!")]
    assert ev == [
        ("down", "Shift"), ("press", "H"), ("up", "Shift"),
        ("press", "i"),
        ("down", "Shift"), ("press", "!"), ("up", "Shift"),
    ]


def test_needs_shift_is_the_us_layout():
    from clearcote._humanize import _needs_shift
    assert all(_needs_shift(c) for c in 'AZ~!@#$%^&*()_+{}|:"<>?')
    assert not any(_needs_shift(c) for c in "az09`-=[];',./ \t\\\u00f6")


class _DisabledLocator:
    first = property(lambda self: self)

    def wait_for(self, **kw):
        pass

    def scroll_into_view_if_needed(self, **kw):
        pass

    def is_enabled(self):
        return False


class _ClickPage(_SyncPage):
    """A page whose element is disabled, so the humanized click falls back to Playwright's own."""

    def __init__(self):
        super().__init__()
        self.native_clicks = []

    def click(self, selector, **kw):
        self.native_clicks.append((selector, kw))

    def hover(self, selector, **kw):
        self.native_clicks.append(("hover:" + selector, kw))

    def locator(self, selector):
        return _DisabledLocator()


def test_click_fallback_reaches_playwright_once():
    # 0.21.0-0.31.1: the fallback helper called itself, so every fallback raised RecursionError.
    from clearcote._humanize import attach_humanize
    page = _ClickPage()
    attach_humanize(None, page, humanize=True)
    page.click("#b", timeout=1234)
    page.hover("#h")
    assert page.native_clicks == [("#b", {"timeout": 1234}), ("hover:#h", {})]


# ------------------------------------------------------------- lane F (SC 232): attach on every page
class _SyncCtx:
    """A sync context that emits "page" like Playwright's, with no Browser (a persistent context's)."""

    def __init__(self, pages=()):
        self.pages, self.handlers, self.browser = list(pages), [], None

    def on(self, ev, fn):
        if ev == "page":
            self.handlers.append(fn)

    def add_init_script(self, *_a, **_k):
        return None

    def emit(self, page):
        self.pages.append(page)
        for fn in self.handlers:
            fn(page)


def _humanized(page):
    return page.__dict__.get("_clearcote_humanized") is True


def test_sync_persistent_context_without_a_browser_attaches():
    from clearcote._humanize import install_humanize_on_context
    first = _SyncPage()
    ctx = _SyncCtx([first])
    install_humanize_on_context(ctx, humanize=True, seed="persist")
    assert _humanized(first), "context.browser is None: humanize must still attach"
    later = _SyncPage()
    ctx.emit(later)
    assert _humanized(later)
    install_humanize_on_context(ctx, humanize=True, seed="persist")
    assert len(ctx.handlers) == 1, "a second install adds no second listener"


def test_sync_popup_of_the_implicit_context_of_browser_new_page_is_humanized():
    from clearcote._humanize import install_humanize
    ctx = _SyncCtx()

    class _B:
        def new_page(self, **kw):
            pg = _SyncPage()
            pg.__dict__["context"] = ctx
            ctx.pages.append(pg)
            return pg

        def new_context(self, **kw):
            return _SyncCtx()

    b = _B()
    install_humanize(b, humanize=True, seed="popup")
    page = b.new_page()
    assert _humanized(page)
    move = page.mouse.move
    popup = _SyncPage()
    ctx.emit(popup)                      # window.open / target=_blank in that implicit context
    assert _humanized(popup)
    assert page.mouse.move is move, "the opener is not wrapped twice (idempotent attach)"


def test_sync_native_select_args_translates_option_dicts():
    from clearcote._humanize import native_select_args
    assert native_select_args("a", {"timeout": 5}) == ("a", {"timeout": 5})
    v, o = native_select_args([{"value": "v1"}, {"label": "L"}, {"index": 2}, "plain"], {"index": 0})
    assert v == ["v1", "plain"] and o == {"index": [0, 2], "label": ["L"]}
