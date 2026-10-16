"""Lane D of SC 232: the humanized keyboard and focus model, through the real attach_humanize wrappers.

Sync half: a fake keyboard on a FAKE clock (time.sleep / time.monotonic advance it; a press advances
it by its hold), so every keydown time is exact: the cadence against the shared KS reference
(humanize_typing_vectors.json, the same one the Node and .NET suites check), same-finger vs alternating
bigrams, chord order and timing, OS auto-repeat, the submit / Tab pause, the caller's delay as a floor,
caller-held modifiers, and the persona's typo rate with the Ctrl+Backspace word fix.

Async half: the simulated page of test_humanize_forms_async.py, given a Tab order (TAB_DISTANCE),
blank points, selections and a focus that Tab moves: Tab between adjacent fields per persona with
the focus verified, a non-text press target reached by Tab, focus / blur / select_text, page.type's
delay floor and page.press(repeat=n).

On 0.51.0 (origin/main ceb7983) every test here fails (see SESSION-CONTEXT/232-.../lane-D.md)."""
import asyncio
import json
import os
import random
import statistics
import time

import pytest

import clearcote._humanize as hz
import clearcote._humanize_async as H
from clearcote._isolated import ACTIVE_FIELD, ACTIVE_VALUE, PLATFORM, VIEWPORT
from clearcote._keys import BLANK_POINT, SELECTION_STATE, TAB_DISTANCE, typing_traits
from clearcote._motion import make_persona

from test_humanize_forms_async import Field, Mouse, Page, World

VEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "testdata", "humanize_typing_vectors.json")
with open(VEC_PATH, encoding="utf-8") as _f:
    VEC = json.load(_f)


def ks_d(a, b):
    """Two-sample Kolmogorov-Smirnov statistic."""
    a, b = sorted(a), sorted(b)
    i = j = 0
    d = 0.0
    while i < len(a) and j < len(b):
        x = min(a[i], b[j])
        while i < len(a) and a[i] == x:
            i += 1
        while j < len(b) and b[j] == x:
            j += 1
        d = max(d, abs(i / len(a) - j / len(b)))
    return d


def ks_crit(n, m, c=1.95):   # c = 1.95: alpha 0.001
    return c * ((n + m) / (n * m)) ** 0.5


# ============================================================================ sync: fake clock
class Clock:
    def __init__(self):
        self.t = 1000.0

    def sleep(self, s):
        if s and s > 0:
            self.t += s

    def now(self):
        return self.t


class Kb:
    """A keyboard that keeps the field's value the way a browser would and stamps every event."""

    def __init__(self, clock):
        self.c, self.ev, self.held, self.value = clock, [], set(), ""

    def _apply(self, k, mods):
        if k == "Backspace":
            if mods & {"Control", "Alt"}:
                i = len(self.value)
                while i and self.value[i - 1] == " ":
                    i -= 1
                while i and self.value[i - 1] != " ":
                    i -= 1
                self.value = self.value[:i]
            else:
                self.value = self.value[:-1]
        elif len(k) == 1 and not mods & {"Control", "Alt", "Meta", "ControlOrMeta"}:
            self.value += k

    def press(self, key, delay=None, **kw):
        parts = key.split("+") if key not in ("+",) else [key]
        mods, k = set(parts[:-1]), parts[-1]
        self.ev.append(("press", key, self.c.t, delay or 0, frozenset(self.held), kw))
        self._apply(k, mods | self.held)
        self.c.t += (delay or 0) / 1000.0

    def down(self, key):
        rep = key in self.held
        self.ev.append(("down", key, self.c.t, rep, frozenset(self.held)))
        self.held.add(key)
        if len(key) == 1 or key == "Backspace":
            self._apply(key, self.held - {key})

    def up(self, key):
        self.ev.append(("up", key, self.c.t))
        self.held.discard(key)

    def type(self, text, **kw):
        self.ev.append(("type", text, self.c.t))
        for ch in text:
            self._apply(ch, set())

    def insert_text(self, text):
        self.value += text


class SWorld:
    def __init__(self, page):
        self.p = page

    def evaluate(self, src, arg=None, **kw):
        if src == ACTIVE_VALUE:
            return self.p.keyboard.value
        if src == ACTIVE_FIELD:
            return {"type": self.p.field_type, "inputmode": None, "autocomplete": None}
        if src == VIEWPORT:
            return [1280, 800]
        if src == PLATFORM:
            return "Win32"
        return None


class SMouse:
    def __getattr__(self, name):
        return lambda *a, **k: None


class SPage:
    def __init__(self, clock, field_type="text"):
        self.keyboard = Kb(clock)
        self.mouse = SMouse()
        self.main_frame = object()
        self.viewport_size = {"width": 1280, "height": 800}
        self.field_type = field_type
        self.world = SWorld(self)

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(time, "sleep", c.sleep)
    monkeypatch.setattr(time, "monotonic", c.now)
    monkeypatch.setattr(time, "perf_counter", c.now)
    monkeypatch.setattr(hz, "world_for", lambda page: page.world)
    return c


@pytest.fixture
def no_roll(monkeypatch):
    monkeypatch.setattr(hz, "rollover_rate", lambda persona: 0.0)
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0")


def spage(seed, clock, **kw):
    p = SPage(clock, **kw)
    hz.attach_humanize(None, p, humanize=True, seed=seed)
    return p


def presses(p, keys=None):
    return [e for e in p.keyboard.ev if e[0] == "press" and (keys is None or e[1] in keys)]


def char_gaps(p):
    """keyup -> next keydown, ms, for the plain one-key-per-press typing (no rollover, no typos)."""
    ev = [e for e in presses(p) if len(e[1]) == 1]
    return [(b[2] - (a[2] + a[3] / 1000.0)) * 1000.0 for a, b in zip(ev, ev[1:])]


def test_cadence_matches_the_shared_reference_distribution(clock, no_roll):
    # The same planned distribution in Python, Node and .NET (the vectors pin the planner; this pins the
    # wrapper to it). On 0.51.0 Python drew max(25, N(85, 45)) whatever the persona: D ~0.5.
    random.seed(1)
    p = spage(VEC["ks_persona"], clock)
    p.keyboard.type(VEC["ks_text"])
    gaps = char_gaps(p)
    assert len(gaps) == 500
    d = ks_d(gaps, VEC["ks_reference"])
    assert d < ks_crit(500, 500), "KS D=%.3f" % d


SAME_FINGER = ["ed", "de", "ju", "ki", "lo", "fr", "sw", "ol"]
ALTERNATE = ["en", "th", "ha", "so", "al", "ka", "fj", "dk"]


def test_same_finger_bigrams_are_slower_than_alternating(clock, no_roll):
    ratios = []
    for seed in ("ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"):
        random.seed(seed)
        words = (SAME_FINGER + ALTERNATE) * 25
        random.shuffle(words)
        p = spage(seed, clock)
        p.keyboard.type(" ".join(words))
        ev = [e for e in presses(p) if len(e[1]) == 1]
        iv = {"sf": [], "alt": []}
        for a, b in zip(ev, ev[1:]):
            pair = a[1] + b[1]
            if pair in SAME_FINGER:
                iv["sf"].append(b[2] - a[2])     # keydown to keydown, as a page measures it
            elif pair in ALTERNATE:
                iv["alt"].append(b[2] - a[2])
        ratios.append(statistics.median(iv["sf"]) / statistics.median(iv["alt"]))
    assert 1.2 <= statistics.median(ratios) <= 1.8, ratios
    assert all(r >= 1.15 for r in ratios), ratios


def test_chords_press_the_modifier_first_and_release_it_last(clock):
    p = spage("ha-1", clock)
    for _ in range(20):
        p.keyboard.ev.clear()
        p.keyboard.press("Control+a")
        ev = p.keyboard.ev
        assert [e[:2] for e in ev] == [("down", "Control"), ("press", "a"), ("up", "Control")]
        lead = (ev[1][2] - ev[0][2]) * 1000
        lag = (ev[2][2] - (ev[1][2] + ev[1][3] / 1000)) * 1000
        assert 40 <= lead <= 110 and 20 <= lag <= 80 and ev[1][3] >= 45
        assert p.keyboard.value == ""
    p.keyboard.ev.clear()
    p.keyboard.press("ControlOrMeta+Shift+ArrowLeft")
    assert [e[:2] for e in p.keyboard.ev] == [("down", "ControlOrMeta"), ("down", "Shift"), ("press", "ArrowLeft"),
                                              ("up", "Shift"), ("up", "ControlOrMeta")]


def test_a_modifier_the_caller_holds_is_not_pressed_again(clock, no_roll):
    p = spage("ha-1", clock)
    p.keyboard.down("Shift")
    p.keyboard.press("Shift+a")
    p.keyboard.type("bc")
    p.keyboard.up("Shift")
    downs = [e for e in p.keyboard.ev if e[0] == "down"]
    assert [e[1] for e in downs] == ["Shift"], "no second Shift under the caller's"
    assert [e[1] for e in presses(p)] == ["a", "b", "c"] and all("Shift" in e[4] for e in presses(p))
    # a bare down/up pair still holds the key like a finger
    p.keyboard.ev.clear()
    p.keyboard.down("x")
    p.keyboard.up("x")
    (d, u) = p.keyboard.ev
    assert (u[2] - d[2]) * 1000 >= 45


def test_repeat_is_an_os_auto_repeat(clock):
    p = spage("ha-2", clock)
    p.keyboard.press("ArrowRight", repeat=12)
    ev = p.keyboard.ev
    downs = [e for e in ev if e[0] == "down"]
    assert len(downs) == 12 and [e[3] for e in downs] == [False] + [True] * 11
    assert [e[:2] for e in ev if e[0] == "up"] == [("up", "ArrowRight")] and ev[-1][0] == "up"
    gaps = [(b[2] - a[2]) * 1000 for a, b in zip(downs, downs[1:])]
    assert 250 <= gaps[0] <= 500 and all(25 <= g <= 40 for g in gaps[1:]) and len(set(round(g, 6) for g in gaps[1:])) == 1


def test_enter_and_tab_after_typing_wait_the_persona_pause(clock, no_roll):
    for key, lo, hi in (("Enter", 250, 900), ("Tab", 120, 400)):
        for seed in ("ha-1", "ha-4"):
            p = spage(seed, clock)
            p.keyboard.type("hello")
            p.keyboard.press(key)
            ev = presses(p)
            last, k = ev[-2], ev[-1]
            assert k[1] == key
            gap = (k[2] - (last[2] + last[3] / 1000)) * 1000
            assert lo <= gap <= hi, (key, seed, gap)
            # once per typed run: a second Enter is not delayed again
            t0 = clock.t
            p.keyboard.press(key)
            assert (presses(p)[-1][2] - t0) * 1000 < 1


def test_the_callers_delay_is_a_floor(clock):
    p = spage("ha-5", clock)
    p.keyboard.type("abcdefgh ijkl", delay=300)
    gaps = char_gaps(p)
    assert len(gaps) == 12 and min(gaps) >= 300 - 1e-6
    assert not [e for e in p.keyboard.ev if e[0] == "down"], "no rollover under a floor"


def _slips(p):
    """Corrections in the event log: runs of Backspace presses, and word deletes."""
    n, prev_bs = 0, False
    for e in p.keyboard.ev:
        bs = e[0] == "press" and e[1] == "Backspace"
        if bs and not prev_bs:
            n += 1
        if e[0] in ("press", "down", "up"):
            prev_bs = bs if e[0] == "press" else prev_bs
    return n


TYPO_TEXT = ("we keep a quiet mind and type plain words into the form until the day ends then rest "
             "slowly between lines ") * 12


def test_typo_rate_is_the_personas(clock, monkeypatch):
    monkeypatch.delenv("CLEARCOTE_HUMANIZE_TYPO_RATE", raising=False)
    monkeypatch.setattr(hz, "rollover_rate", lambda persona: 0.0)
    eligible = sum(1 for c in TYPO_TEXT if c.isalnum())
    rates = {}
    for seed in ("ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"):
        random.seed(seed)
        p = spage(seed, clock)
        p.keyboard.type(TYPO_TEXT)
        assert p.keyboard.value == TYPO_TEXT
        want = typing_traits(make_persona(seed).seed).typo_rate
        n = 0
        for _ in range(3):   # three runs: ~30-300 slips per seed
            p.keyboard.ev.clear()
            p.keyboard.value = ""
            p.keyboard.type(TYPO_TEXT)
            n += sum(1 for e in p.keyboard.ev if e[0] == "press" and e[1] == "Backspace") > 0 and _slips(p)
        got = n / (3 * eligible)
        rates[seed] = (round(got, 4), round(want, 4))
        assert 0.003 <= got <= 0.05 and 0.4 * want <= got <= 1.9 * want + 0.002, rates
    assert len({r[0] for r in rates.values()}) >= 5


def test_word_slips_are_sometimes_fixed_with_ctrl_backspace_and_the_value_stays_exact(clock, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0.25")
    monkeypatch.setattr(hz, "rollover_rate", lambda persona: 0.0)
    random.seed(7)
    p = spage("ha-3", clock)
    text = "several ordinary words typed into a plain text field by somebody careful " * 3
    p.keyboard.type(text)
    assert p.keyboard.value == text
    word = [e for e in p.keyboard.ev if e[0] == "press" and e[1] == "Backspace" and "Control" in e[4]]
    plain = [e for e in p.keyboard.ev if e[0] == "press" and e[1] == "Backspace" and "Control" not in e[4]]
    assert word and plain


def test_a_search_field_sometimes_keeps_a_slip_and_a_text_field_never(clock, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0.3")
    monkeypatch.setattr(hz, "rollover_rate", lambda persona: 0.0)
    random.seed(11)
    text = "query words for the search box"
    kept = 0
    for _ in range(30):
        p = spage("ha-2", clock, field_type="search")
        p.keyboard.type(text)
        kept += p.keyboard.value != text
    assert kept >= 1
    for _ in range(10):
        p = spage("ha-2", clock, field_type="text")
        p.keyboard.type(text)
        assert p.keyboard.value == text


# ============================================================================ async: simulated page
class KWorld(World):
    async def evaluate(self, src, arg=None, **kw):
        if src == SELECTION_STATE:
            f = self.p.focused
            if f is None:
                return None
            n = len(f.value)
            return [0, n, n] if f.all else [n, n, n]
        if src == BLANK_POINT:
            b = arg["box"]
            return [b["x"] + b["width"] / 2, b["y"] + b["height"] + 40]
        return await super().evaluate(src, arg)

    async def call_on_node(self, nid, src, arg=None):
        if src == TAB_DISTANCE:
            f = self.p.fields[nid - 1]
            order = self.p.fields
            a = self.p.focused
            text = lambda g: g is not None and (g.tag == "textarea" or (g.tag == "input" and g.type in ("text", "search", "")))  # noqa: E731
            return {"dist": order.index(f) - order.index(a) if a in order else None, "fromText": text(a),
                    "focused": a is f, "activatable": f.tag in ("button", "select") or f.type == "checkbox",
                    "text": text(f), "tag": f.tag, "type": f.type, "n": len(order)}
        return await super().call_on_node(nid, src, arg)


class KMouse(Mouse):
    async def up(self, button="left", click_count=1):
        await super().up(button, click_count)
        f = self.p.focused
        if click_count == 3 and f is not None and f.inside(*self.p.pos):
            f.all = True   # a triple click selects an input's text


class KPage(Page):
    def __init__(self, *fields):
        super().__init__(*fields)
        self.mouse = KMouse(self)
        self._cc_isolated_world = KWorld(self)

    def key(self, k):
        if k == "Tab":
            if self.focused in self.fields:
                i = self.fields.index(self.focused) + (-1 if "Shift" in self.mods else 1)
                self.focused = self.fields[i] if 0 <= i < len(self.fields) else None
                if self.focused is not None and self.focused.tag == "input":
                    self.focused.all = True   # Tab into a field selects its text
            return
        if k == "Home" and "Shift" in self.mods and self.focused is not None:
            self.focused.all = True
            return
        super().key(k)

    async def focus(self, sel, **kw):
        self.native.append(("focus", sel, kw))
        self.focused = self.by_sel[sel]


@pytest.fixture
def fast(monkeypatch):
    async def nosleep(_s=0, *a, **k):
        return None
    monkeypatch.setattr(H.asyncio, "sleep", nosleep)
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0")


def _three(y0=100.0):
    return [("#%s" % c, Field(rect=(100.0, y0 + 50.0 * i, 200.0, 24.0))) for i, c in enumerate("abc")]


async def kattached(seed, *fields):
    p = KPage(*fields)
    await H.attach_humanize(None, p, humanize=True, seed=seed)
    return p


def run(coro):
    return asyncio.run(coro)


def test_fill_tabs_between_adjacent_fields_in_the_personas_share(fast):
    async def go():
        out = {}
        for seed in ("ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"):
            random.seed(seed)
            tabs = n = 0
            for _ in range(30):
                p = await kattached(seed, *_three())
                for sel, v in (("#a", "alpha"), ("#b", "bravo"), ("#c", "charlie")):
                    before = len(p.log)
                    await p.fill(sel, v)
                    if sel != "#a":
                        n += 1
                        tabs += any(e[0] == "press" and e[1] == "Tab" for e in p.log[before:])
                assert [f.value for f in p.fields] == ["alpha", "bravo", "charlie"] and not p.native
            want = typing_traits(make_persona(seed).seed).tab_pref
            out[seed] = (tabs / n, want)
            assert abs(tabs / n - want) <= 0.17, out
        allr = [r for r, _ in out.values()]
        assert 0.55 <= statistics.mean(allr) <= 0.9, out
    run(go())


def test_tab_only_lands_where_verified_else_a_click(fast, monkeypatch):
    # A page whose Tab order skips to somewhere else (a hidden stop): the focus check fails, so the
    # field is clicked and still filled.
    async def go():
        p = await kattached("ha-5", *_three())
        await p.fill("#a", "x")
        orig = p.key

        def key(k):
            if k == "Tab":
                p.focused = None   # focus went to something outside the simulated fields
                return
            orig(k)
        p.key = key
        monkeypatch.setattr(H.random, "random", lambda: 0.01)   # always prefer Tab (never 0: gauss loops on it)
        n = len(p.log)
        await p.fill("#b", "y")
        new = p.log[n:]
        tab = next(i for i, e in enumerate(new) if e[0] == "press" and e[1] == "Tab")
        assert any(e[0] == "down" for e in new[tab:]), "the Tab missed: the field is clicked"
        assert p.by_sel["#b"].value == "y" and not p.native
    run(go())


def kinds(p, *names):
    return [e for e in p.log if e[0] in names]


def test_press_on_a_button_within_tab_reach_tabs_to_it(fast):
    async def go():
        btn = Field(tag="button", type="", rect=(100.0, 200.0, 160.0, 40.0))
        p = await kattached("ha-1", ("#q", Field(rect=(100.0, 100.0, 200.0, 24.0))), ("#go", btn))
        await p.fill("#q", "hello")
        downs_before = len(kinds(p, "down"))
        await p.press("#go", "Enter")
        assert not p.native, p.native
        assert len(kinds(p, "down")) == downs_before, "no click on the button"
        keys = [e[1] for e in kinds(p, "press")]
        assert keys[-2:] == ["Tab", "Enter"] and p.focused is btn
    run(go())


def test_type_honours_delay_as_a_floor_and_press_repeats(fast, monkeypatch):
    async def go():
        slept = []

        async def rec(s=0, *a, **k):
            slept.append(s)
        monkeypatch.setattr(H.asyncio, "sleep", rec)
        p = await kattached("ha-2", ("#a", Field()))
        await p.type("#a", "abcdef", delay=300)
        assert p.by_sel["#a"].value == "abcdef"
        assert sum(1 for s in slept if s >= 0.3) >= 5
        p.log.clear()
        await p.press("#a", "ArrowRight", repeat=6)
        assert [e for e in p.log if e[0] == "kdown"] == [("kdown", "ArrowRight")] * 6
        assert [e for e in p.log if e[0] == "kup"] == [("kup", "ArrowRight")]
    run(go())


def test_focus_tabs_or_clicks_and_never_goes_native(fast):
    async def go():
        p = await kattached("ha-5", *_three())
        await p.fill("#a", "x")
        await p.focus("#b")
        await p.focus("#c")
        await p.focus("#a")
        assert p.focused is p.by_sel["#a"] and not p.native
    run(go())


@pytest.mark.parametrize("seed", ["ha-1", "ha-4"])   # blur_style click / tab
def test_blur_leaves_the_field_the_persona_way(fast, seed):
    async def go():
        p = await kattached(seed, *_three())
        await p.fill("#b", "x")
        n = len(p.log)
        await p._clearcote_blur("#b")
        assert p.focused is not p.by_sel["#b"] and not p.native
        new = p.log[n:]
        style = typing_traits(make_persona(seed).seed).blur_style
        assert (any(e[0] == "down" for e in new) if style == "click"
                else any(e[0] == "press" and e[1] == "Tab" for e in new))
    run(go())


@pytest.mark.parametrize("seed", ["ha-2", "ha-1"])   # select_style triple / keys
def test_select_text_selects_the_whole_value(fast, seed):
    async def go():
        p = await kattached(seed, ("#a", Field(value="some text")))
        await p._clearcote_select_text("#a")
        assert p.by_sel["#a"].all and not p.native
        style = typing_traits(make_persona(seed).seed).select_style
        if style == "triple":
            assert [e[2] for e in kinds(p, "down")][-3:] == [1, 2, 3]
        else:
            assert [e[1] for e in kinds(p, "press")][-2:] == ["End", "Home"]
    run(go())


def test_locator_focus_blur_and_select_text_are_wrapped():
    from playwright.async_api._generated import Locator as ALocator
    from playwright.sync_api._generated import Locator as SLocator
    run(H.attach_humanize(None, KPage(), humanize=True, seed="x"))
    hz.attach_humanize(None, SPage(Clock()), humanize=True, seed="x")
    for L in (ALocator, SLocator):
        for name in ("focus", "blur", "select_text"):
            assert getattr(L, name).__module__.startswith("clearcote."), (L, name)
