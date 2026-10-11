"""Humanize's DOM reads run in an isolated world, never the page's (clearcote._isolated)."""
import json

from clearcote._isolated import (
    ACTIVE_VALUE, AsyncIsolatedWorld, DESCRIBE, IsolatedWorld, VIEWPORT, world_for,
)


class _FakeCdp:
    """Page.getFrameTree / Page.createIsolatedWorld / Runtime.evaluate, recorded."""

    def __init__(self, values=None, stale_once=False):
        self.calls, self.values, self.stale_once, self.next_ctx = [], list(values or []), stale_once, 7

    def send(self, method, params=None):
        self.calls.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "MAIN"}}}
        if method == "Page.createIsolatedWorld":
            self.next_ctx += 1
            return {"executionContextId": self.next_ctx}
        if method == "Runtime.evaluate":
            if self.stale_once:
                self.stale_once = False
                raise RuntimeError("Cannot find context with specified id")
            return {"result": {"value": self.values.pop(0) if self.values else None}}
        raise AssertionError(method)


class _FakeContext:
    def __init__(self, cdp):
        self.cdp, self.sessions = cdp, 0

    def new_cdp_session(self, page):
        self.sessions += 1
        return self.cdp


class _FakePage:
    def __init__(self, cdp):
        self.context = _FakeContext(cdp)

    def evaluate(self, *a, **k):
        raise AssertionError("humanize must not evaluate in the page's world")


def test_evaluates_in_an_isolated_world_of_the_main_frame():
    cdp = _FakeCdp(values=[[1280, 720], True])
    world = IsolatedWorld(_FakePage(cdp))
    assert world.evaluate(VIEWPORT) == [1280, 720]
    assert world.evaluate(ACTIVE_VALUE, "#q") is True
    methods = [m for m, _ in cdp.calls]
    assert methods == ["Page.getFrameTree", "Page.createIsolatedWorld", "Runtime.evaluate", "Runtime.evaluate"]
    assert cdp.calls[1][1] == {"frameId": "MAIN"}
    ev = cdp.calls[3][1]
    assert ev["contextId"] == 8 and ev["returnByValue"] is True
    assert ev["expression"] == "(%s)(%s)" % (ACTIVE_VALUE, json.dumps("#q"))
    assert "Runtime.enable" not in methods  # never enables the Runtime domain


def test_recreates_the_world_after_a_navigation():
    cdp = _FakeCdp(values=[[800, 600]], stale_once=True)
    world = IsolatedWorld(_FakePage(cdp))
    assert world.evaluate(VIEWPORT) == [800, 600]
    assert [m for m, _ in cdp.calls].count("Page.createIsolatedWorld") == 2
    assert cdp.calls[-1][1]["contextId"] == 9  # the fresh context


def test_returns_none_instead_of_touching_the_page_world():
    class NoCdp:
        def __init__(self):
            self.context = type("C", (), {"new_cdp_session": lambda s, p: (_ for _ in ()).throw(RuntimeError("x"))})()

        def evaluate(self, *a, **k):
            raise AssertionError("must not fall back to page.evaluate")

    assert IsolatedWorld(NoCdp()).evaluate(VIEWPORT) is None


def test_exceptions_in_the_script_read_as_none():
    class Cdp(_FakeCdp):
        def send(self, method, params=None):
            if method == "Runtime.evaluate":
                return {"exceptionDetails": {"text": "SyntaxError"}}
            return super().send(method, params)

    assert IsolatedWorld(_FakePage(Cdp())).evaluate(ACTIVE_VALUE) is None


def test_world_is_cached_per_page():
    page = _FakePage(_FakeCdp(values=[1, 2]))
    assert world_for(page) is world_for(page)
    world_for(page).evaluate(VIEWPORT)
    world_for(page).evaluate(VIEWPORT)
    assert page.context.sessions == 1


async def test_async_world():
    class ACdp:
        def __init__(self):
            self.sync = _FakeCdp(values=[[1, 2]])

        async def send(self, method, params=None):
            return self.sync.send(method, params)

    class ACtx:
        def __init__(self):
            self.cdp = ACdp()

        async def new_cdp_session(self, page):
            return self.cdp

    page = type("P", (), {})()
    page.context = ACtx()
    assert await AsyncIsolatedWorld(page).evaluate(VIEWPORT) == [1, 2]


async def test_async_world_opens_one_session_and_one_world_under_concurrent_reads():
    # Two coroutines reading at once (a wheel's viewport read during a fill's hit test) each saw "no
    # session yet" and opened their own, then created their own isolated world.
    import asyncio

    class ACdp:
        def __init__(self):
            self.sync = _FakeCdp(values=[[1, 2]] * 8)

        async def send(self, method, params=None):
            await asyncio.sleep(0)   # a real round trip: the other coroutine runs meanwhile
            return self.sync.send(method, params)

    class ACtx:
        def __init__(self):
            self.cdp, self.sessions = ACdp(), 0

        async def new_cdp_session(self, page):
            self.sessions += 1
            await asyncio.sleep(0)
            return self.cdp

    page = type("P", (), {})()
    page.context = ACtx()
    world = AsyncIsolatedWorld(page)
    got = await asyncio.gather(*(world.evaluate(VIEWPORT) for _ in range(4)),
                               world.send("Page.getFrameTree"))
    assert got[:4] == [[1, 2]] * 4
    assert page.context.sessions == 1
    assert [m for m, _ in page.context.cdp.sync.calls].count("Page.createIsolatedWorld") == 1


class _HitCdp(_FakeCdp):
    """Adds the hit test + node calls the form routes use."""

    def __init__(self, hit=None, call_value=None):
        super().__init__()
        self.hit, self.call_value = hit, call_value

    def send(self, method, params=None):
        if method == "DOM.getNodeForLocation":
            self.calls.append((method, params))
            return self.hit
        if method == "DOM.resolveNode":
            self.calls.append((method, params))
            return {"object": {"objectId": "obj-1"}}
        if method == "Runtime.callFunctionOn":
            self.calls.append((method, params))
            return {"result": {"value": self.call_value}}
        if method == "Runtime.releaseObject":
            self.calls.append((method, params))
            return {}
        return super().send(method, params)


def test_node_at_uses_the_renderer_hit_test_and_rounds_to_pixels():
    cdp = _HitCdp(hit={"backendNodeId": 42, "frameId": "MAIN"})
    w = IsolatedWorld(_FakePage(cdp))
    assert w.node_at(10.6, 20.4) == 42
    hit = [p for m, p in cdp.calls if m == "DOM.getNodeForLocation"][0]
    assert hit == {"x": 11, "y": 20, "includeUserAgentShadowDOM": False, "ignorePointerEventsNone": True}


def test_node_at_refuses_a_node_inside_another_frame():
    cdp = _HitCdp(hit={"backendNodeId": 42, "frameId": "IFRAME-7"})
    assert IsolatedWorld(_FakePage(cdp)).node_at(1, 1) is None


def test_node_at_is_none_when_nothing_is_there():
    assert IsolatedWorld(_FakePage(_HitCdp(hit={}))).node_at(1, 1) is None


def test_call_on_node_resolves_into_the_isolated_world_and_releases_the_handle():
    cdp = _HitCdp(call_value={"tag": "select"})
    w = IsolatedWorld(_FakePage(cdp))
    assert w.call_on_node(42, DESCRIBE, {"want": "select"}) == {"tag": "select"}
    methods = [m for m, _ in cdp.calls]
    res = [p for m, p in cdp.calls if m == "DOM.resolveNode"][0]
    assert res["backendNodeId"] == 42 and res["executionContextId"] == 8   # the isolated world
    call = [p for m, p in cdp.calls if m == "Runtime.callFunctionOn"][0]
    assert call["objectId"] == "obj-1" and call["functionDeclaration"] == DESCRIBE
    assert call["arguments"] == [{"value": {"want": "select"}}] and call["returnByValue"] is True
    assert methods.index("Runtime.releaseObject") > methods.index("Runtime.callFunctionOn")
    assert "Runtime.enable" not in methods and "DOM.enable" not in methods


def test_describe_and_active_value_are_functions_not_page_globals():
    # They run in the isolated world: no reference to anything a page could have planted.
    for src in (DESCRIBE, ACTIVE_VALUE):
        assert "__" not in src and "window." not in src
    assert DESCRIBE.lstrip().startswith("function (a)")
