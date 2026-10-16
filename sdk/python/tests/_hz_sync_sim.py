"""A SYNC view of the async page simulator in test_humanize_forms_async.py, so the sync humanizer
(clearcote._humanize) runs against the same simulated DOM. Every coroutine the simulator returns is run to
completion on the spot; simulator objects are wrapped again, and attributes set by the humanizer (the
wrapped page.click, mouse.move, ...) live on the proxy, which is cached per object so page.mouse is
always the same proxy."""
import inspect

import test_humanize_forms_async as SIM

_SIM_TYPES = tuple(v for v in vars(SIM).values() if inspect.isclass(v) and v.__module__ == SIM.__name__)


def _wrap(v):
    if isinstance(v, Sync):
        return v
    if inspect.iscoroutine(v):
        return _wrap(_drain(v))
    if isinstance(v, _SIM_TYPES) or (v is not None and type(v).__module__ == SIM.__name__):
        return Sync.of(v)
    return v


def _drain(c):
    """Run a simulator coroutine to its end without an event loop: it only ever suspends on
    asyncio.sleep(0), a bare yield. So the sync scenarios can run inside asyncio.run too."""
    try:
        while True:
            c.send(None)
    except StopIteration as e:
        return e.value


class Sync:
    _cache = {}

    @classmethod
    def of(cls, obj):
        p = cls._cache.get(id(obj))
        if p is None or object.__getattribute__(p, "_o") is not obj:
            p = cls(obj)
            cls._cache[id(obj)] = p
        return p

    def __init__(self, obj):
        object.__setattr__(self, "_o", obj)

    def __getattr__(self, name):
        v = getattr(object.__getattribute__(self, "_o"), name)
        if callable(v) and not inspect.isclass(v):
            def call(*a, **kw):
                return _wrap(v(*a, **kw))
            return call
        return _wrap(v)

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)


def sync_page(*fields):
    """A simulated page (async simulator underneath) seen through the sync API."""
    p = SIM.Page(*fields)
    sp = Sync.of(p)
    return p, sp
