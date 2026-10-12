"""LIVE humanized form input through clearcote.async_api, against a real Clearcote build.

The async mirror of test_humanize_forms_live.py: the same fixture (sdk/testdata/humanize_forms.html) and
the same checks, driven through the async API. Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote
binary (and a licence is available for a PRO build). Every assertion is about what a PAGE sees: trusted
events, key holds, how many change events a selection fires, and the exact value left in the field.
"""
import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")

try:
    import pytest_asyncio
    _PA = tuple(int(p) for p in pytest_asyncio.__version__.split(".")[:2])
except Exception:  # noqa: BLE001
    pytest_asyncio, _PA = None, (0, 0)
_LOOP_SCOPE_OK = _PA >= (0, 24)   # loop_scope= (one event loop, one browser, for the whole module)

pytestmark = [pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)"),
              pytest.mark.skipif(not _LOOP_SCOPE_OK, reason="needs pytest-asyncio >= 0.24 (loop_scope)")]
if _LOOP_SCOPE_OK:
    pytestmark.append(pytest.mark.asyncio(loop_scope="module"))

    def _fixture(**kw):
        return pytest_asyncio.fixture(loop_scope="module", **kw)
else:  # collected but skipped
    def _fixture(**kw):
        return pytest.fixture(**kw)

FIXTURE = (Path(__file__).resolve().parents[2] / "testdata" / "humanize_forms.html").as_uri()
MIN_HOLD_MS = 25   # a finger cannot hold a key or button shorter than this; persona floor is 45


@pytest.fixture(autouse=True, scope="module")
def _no_font_cache_left_behind():
    """A Linux launch points fontconfig at <tmp>/cc-fc-cache, shared by every launch on the machine:
    remove it afterwards only when this module created it (nothing else may be left in TMPDIR)."""
    cache = os.path.join(tempfile.gettempdir(), "cc-fc-cache")
    existed = os.path.exists(cache)
    yield
    if not existed:
        shutil.rmtree(cache, ignore_errors=True)


@_fixture(scope="module")
async def browser(_no_font_cache_left_behind):
    from clearcote import async_api
    b = await async_api.launch(executable_path=LIVE, headless=True, humanize=True,
                               fingerprint="forms-live-async-1", quiet=True)
    yield b
    await b.close()


@_fixture()
async def page(browser):
    p = await browser.new_page()
    await p.goto(FIXTURE)
    yield p
    await p.close()


@pytest.fixture
def typos(monkeypatch):
    def set_rate(r):
        monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", str(r))
    return set_rate


async def ev(page, t=None, id=None):
    out = await page.evaluate("window.__ev")
    return [e for e in out if (t is None or e["t"] == t) and (id is None or e["id"] == id)]


async def reset(page):
    await page.evaluate("window.__ev.length = 0")


def _holds(events, id=None):
    open_, out = {}, []
    for e in events:
        if e["t"] not in ("keydown", "keyup") or (id is not None and e["id"] != id):
            continue
        k = e["code"] or e["key"]
        if e["t"] == "keydown":
            open_.setdefault(k, e["ts"])
        elif k in open_:
            out.append((e["key"], e["ts"] - open_.pop(k)))
    return out


async def key_holds(page, id=None):
    """(key, keydown->keyup ms) for every key that went to element ``id``."""
    return _holds(await ev(page), id)


async def assert_human_keys(page, id):
    events = await ev(page)
    holds = _holds(events, id)
    assert holds, "no keys reached #%s" % id
    short = [(k, round(d, 1)) for k, d in holds if d < MIN_HOLD_MS]
    assert not short, "keys held shorter than a finger can: %s" % short
    assert all(e["tr"] for e in events if e["t"] in ("keydown", "keyup", "input", "change") and e["id"] == id)


async def pressed(page, key, id=None):
    return [e for e in await ev(page, "keydown", id) if e["key"] == key]


# ------------------------------------------------------------------------------------- clicks
async def test_right_click_is_a_right_click(page):
    await reset(page)
    await page.click("#btn", button="right")
    downs = await ev(page, "mousedown", "btn")
    assert [d["button"] for d in downs] == [2] and downs[0]["tr"]
    assert await ev(page, "contextmenu", "btn"), "a right click opens the context menu"
    assert not await ev(page, "click", "btn"), "a right click is not a click"


async def test_middle_click_through_a_locator(page):
    await reset(page)
    await page.locator("#btn").click(button="middle")
    assert [d["button"] for d in await ev(page, "mousedown", "btn")] == [1]
    assert await ev(page, "auxclick", "btn")


async def test_click_count_two_is_a_double_click(page):
    await reset(page)
    await page.click("#btn", click_count=2)
    assert [d["detail"] for d in await ev(page, "mousedown", "btn")] == [1, 2]
    assert (await ev(page, "dblclick", "btn"))[0]["tr"]


async def test_dblclick_has_human_holds_and_gap(page):
    await reset(page)
    await page.dblclick("#btn")
    downs, ups = await ev(page, "mousedown", "btn"), await ev(page, "mouseup", "btn")
    assert len(downs) == 2 and len(ups) == 2
    for d, u in zip(downs, ups):
        assert u["ts"] - d["ts"] >= 40, "button held like a finger"
    gap = downs[1]["ts"] - ups[0]["ts"]
    assert 50 <= gap <= 450, gap
    assert await ev(page, "dblclick", "btn")


async def test_locator_dblclick_is_humanized(page):
    await reset(page)
    await page.locator("#btn").dblclick()
    downs, ups = await ev(page, "mousedown", "btn"), await ev(page, "mouseup", "btn")
    assert [d["detail"] for d in downs] == [1, 2]
    assert all(u["ts"] - d["ts"] >= 40 for d, u in zip(downs, ups))


async def test_modifiers_are_held_around_the_press(page):
    await reset(page)
    await page.click("#btn", modifiers=["Shift"])
    click = (await ev(page, "click", "btn"))[0]
    assert click["shift"] and click["tr"]
    shift_down = (await pressed(page, "Shift"))[0]["ts"]
    shift_up = [e for e in await ev(page, "keyup") if e["key"] == "Shift"][0]["ts"]
    assert shift_down < (await ev(page, "mousedown", "btn"))[0]["ts"]
    assert shift_up > (await ev(page, "mouseup", "btn"))[0]["ts"]


async def test_position_is_honoured(page):
    await reset(page)
    await page.click("#btn", position={"x": 10, "y": 12})
    d = (await ev(page, "mousedown", "btn"))[0]
    assert abs(d["ox"] - 10) <= 1.5 and abs(d["oy"] - 12) <= 1.5


async def test_trial_performs_no_action(page):
    await reset(page)
    await page.click("#trialbtn", trial=True)
    await page.locator("#trialbtn").click(trial=True)
    await page.hover("#trialbtn", trial=True)
    assert not await ev(page, "mousedown", "trialbtn") and not await ev(page, "click", "trialbtn")


async def test_check_with_trial_changes_nothing(page):
    await reset(page)
    await page.check("#cb", trial=True)
    await page.locator("#cb").check(trial=True)
    assert not await page.is_checked("#cb")
    assert not await ev(page, "click", "cb") and not await ev(page, "mousedown", "cb")


async def test_delay_sets_the_hold(page):
    await reset(page)
    await page.click("#btn", delay=300)
    d, u = (await ev(page, "mousedown", "btn"))[0], (await ev(page, "mouseup", "btn"))[0]
    assert 270 <= u["ts"] - d["ts"] <= 450


async def test_glide_precedes_the_press(page):
    await reset(page)
    await page.click("#btn")
    moves = [e for e in await ev(page) if e["t"] == "mousemove"]
    assert sum(m["n"] for m in moves) >= 5, "the pointer travels to the target"


async def test_first_click_on_fresh_pages_is_humanized(browser):
    """The covered-point probe used to time out on ~1 in 6 FIRST clicks of a fresh page (slow first
    frames, not an overlay) and send a plain native click instead. Six fresh pages, every one glides."""
    for _ in range(6):
        p = await browser.new_page()
        try:
            await p.goto(FIXTURE)
            await p.click("#btn")
            moves = sum(e["n"] for e in await ev(p, "mousemove"))
            downs, ups = await ev(p, "mousedown", "btn"), await ev(p, "mouseup", "btn")
            assert moves >= 5 and downs, "first click went out unhumanized"
            assert ups[0]["ts"] - downs[0]["ts"] >= 40
        finally:
            await p.close()


async def test_locator_strictness_still_raises(page):
    from playwright.async_api import Error
    with pytest.raises(Error):
        await page.locator("button.dup").click(timeout=2000)


async def test_missing_element_fails_after_one_timeout(page):
    from playwright.async_api import TimeoutError as PWTimeout
    for act in (lambda: page.click("#nope", timeout=1500), lambda: page.fill("#nope", "x", timeout=1500),
                lambda: page.locator("#nope").click(timeout=1500)):
        t0 = time.time()
        with pytest.raises(PWTimeout):
            await act()
        assert time.time() - t0 < 2.8, "a missing element must not wait the timeout twice"


async def _btn_box(page):
    await page.locator("#btn").scroll_into_view_if_needed()   # raw mouse input does not scroll
    return await page.locator("#btn").bounding_box()


async def test_mouse_click_xy_right_button(page):
    box = await _btn_box(page)
    await reset(page)
    await page.mouse.click(box["x"] + 20, box["y"] + 15, button="right")
    assert await ev(page, "contextmenu", "btn")


async def test_mouse_dblclick_xy(page):
    box = await _btn_box(page)
    await reset(page)
    await page.mouse.dblclick(box["x"] + 30, box["y"] + 20)
    dbl = await ev(page, "dblclick", "btn")
    assert dbl and dbl[0]["tr"]
    downs, ups = await ev(page, "mousedown", "btn"), await ev(page, "mouseup", "btn")
    assert all(u["ts"] - d["ts"] >= 40 for d, u in zip(downs, ups))


async def test_check_uncheck_and_set_checked_are_humanized(page):
    await reset(page)
    await page.check("#cb")
    assert await page.is_checked("#cb")
    assert await ev(page, "mousedown", "cb") and all(e["tr"] for e in await ev(page, "click", "cb"))
    await page.locator("#cb").set_checked(False)
    assert not await page.is_checked("#cb")
    await page.uncheck("#cb")          # already unchecked: nothing happens
    assert not await page.is_checked("#cb")
    assert len(await ev(page, "click", "cb")) == 2


# ------------------------------------------------------------------------------------- text
async def test_fill_text_with_held_keys(page):
    await reset(page)
    await page.fill("#name", "Ada Lovelace")
    assert await page.input_value("#name") == "Ada Lovelace"
    await assert_human_keys(page, "name")


async def test_fill_clears_a_prefilled_field_with_held_keys(page):
    await reset(page)
    await page.fill("#prefilled", "new")
    assert await page.input_value("#prefilled") == "new"
    holds = dict((k, d) for k, d in await key_holds(page, "prefilled") if k in ("Control", "Meta", "a", "Backspace"))
    assert "Backspace" in holds and holds["Backspace"] >= MIN_HOLD_MS
    assert any(k in holds for k in ("Control", "Meta"))
    assert all(d >= MIN_HOLD_MS for d in holds.values()), holds


async def test_fill_on_an_empty_field_presses_no_clear_keys(page, typos):
    typos(0)
    await reset(page)
    await page.fill("#bio", "hello")
    assert await page.input_value("#bio") == "hello"
    assert not [e for e in await ev(page, "keydown", "bio") if e["key"] in ("Backspace", "Control", "Meta")]


async def test_clear_empties_the_field(page):
    await page.fill("#prefilled", "abc")
    await reset(page)
    await page.locator("#prefilled").clear()
    assert await page.input_value("#prefilled") == ""
    await assert_human_keys(page, "prefilled")


async def test_get_by_label_and_role_are_humanized(page):
    await reset(page)
    await page.get_by_label("City").fill("Paris")
    assert await page.input_value("#city") == "Paris"
    await assert_human_keys(page, "city")
    await reset(page)
    await page.get_by_role("textbox", name="City").press_sequentially("!")
    assert await page.input_value("#city") == "Paris!"
    await assert_human_keys(page, "city")


async def test_filter_and_first_locators_are_humanized(page):
    await reset(page)
    await page.locator("input[type=text]").filter(has_not_text="zzz").nth(0).fill("first")
    assert await page.input_value("#name") == "first"
    await assert_human_keys(page, "name")


async def test_field_under_a_floating_label(page):
    # The label covers every probe point of the field: a person clicks the label (it passes the focus on)
    # and types; the fill must not fall back to Playwright's insertText.
    await reset(page)
    await page.fill("#float", "abc")
    assert await page.input_value("#float") == "abc"
    assert [e["key"] for e in await ev(page, "keydown", "float")] == ["a", "b", "c"]
    await assert_human_keys(page, "float")


async def test_textarea_newlines_and_contenteditable(page):
    await page.fill("#bio", "two\nlines")
    assert await page.input_value("#bio") == "two\nlines"
    await page.fill("#ce", "hello world")
    assert await page.eval_on_selector("#ce", "e => e.textContent") == "hello world"


async def test_press_holds_the_key(page):
    await page.fill("#name", "x")
    await reset(page)
    await page.press("#name", "Backspace")
    assert await page.input_value("#name") == ""
    await assert_human_keys(page, "name")


async def test_type_into_the_focused_field_does_not_click_it_again(page, typos):
    # The old async page.type clicked the field before every call, even with the caret already in it.
    typos(0)
    await page.fill("#name", "ab")
    await reset(page)
    await page.type("#name", "cd")
    assert await page.input_value("#name") == "abcd"
    assert not await ev(page, "mousedown", "name"), "the caret was already there: no second click"
    await assert_human_keys(page, "name")


# ------------------------------------------------------------------------------------- typos
async def test_forced_typos_are_corrected(page, typos):
    typos(0.35)
    await reset(page)
    text = "the quick brown fox jumps over 2024"
    await page.fill("#name", text)
    assert await page.input_value("#name") == text
    assert await pressed(page, "Backspace", "name"), "with a 35 % rate some slip had to be corrected"
    await assert_human_keys(page, "name")


@pytest.mark.parametrize("sel,value", [
    ("#email", "ada@example.com"), ("#pw", "s3cretPassw0rd"), ("#num", "123456"), ("#tel", "5551234567"),
    ("#url", "https://example.com/a"), ("#otp", "123456")])
async def test_no_typos_where_a_slip_breaks_the_field(page, typos, sel, value):
    typos(0.6)
    await reset(page)
    await page.fill(sel, value)
    assert await page.input_value(sel) == value
    assert not await pressed(page, "Backspace", sel[1:]), "no slips in %s" % sel


async def test_keyboard_type_into_a_focused_password_makes_no_slips(page, typos):
    typos(0.6)
    await page.click("#pw")
    await reset(page)
    await page.keyboard.type("abcdefgh")
    assert await page.input_value("#pw") == "abcdefgh"
    assert not await pressed(page, "Backspace", "pw")


async def test_typos_respect_maxlength(page, typos):
    typos(1.0)
    for _ in range(3):
        await page.fill("#zip", "12345")
        assert await page.input_value("#zip") == "12345"


async def test_typos_in_a_masked_field_never_delete_real_digits(page, typos):
    typos(1.0)
    for _ in range(3):
        await page.fill("#masked", "24680")
        assert await page.input_value("#masked") == "24680"


# ------------------------------------------------------------------------------------ select
async def changes(page, id):
    return [e for e in await ev(page) if e["t"] == "change" and e["id"] == id]


async def test_select_by_label_fires_one_trusted_change(page):
    await reset(page)
    assert await page.select_option("#country", "Germany") == ["ger25"]
    assert await page.input_value("#country") == "ger25"
    ch = await changes(page, "country")
    assert len(ch) == 1 and ch[0]["tr"]
    assert await ev(page, "mousedown", "country"), "the drop-down was opened with the mouse"


@pytest.mark.parametrize("kw,want", [({"value": "swi50"}, "swi50"), ({"index": 3}, "and3"),
                                     ({"label": "Japan"}, "jap35"), ({"value": "United Kingdom"}, "uni53")])
async def test_select_value_index_label(page, kw, want):
    kw = dict(kw)
    await reset(page)
    value = kw.pop("value", None)
    assert await page.select_option("#country", value, **kw) == [want]
    assert await page.input_value("#country") == want
    assert len(await changes(page, "country")) == 1


async def test_select_through_a_locator(page):
    await reset(page)
    assert await page.locator("#country").select_option("France") == ["fra23"]
    assert len(await changes(page, "country")) == 1


async def test_select_far_down_a_long_list_is_quick(page):
    await reset(page)
    t0 = time.time()
    await page.select_option("#country", "Uruguay")
    assert await page.input_value("#country") == "uru55"
    assert len(await changes(page, "country")) == 1
    assert time.time() - t0 < 10


async def test_select_the_current_option_fires_nothing(page):
    await reset(page)
    await page.select_option("#country", "afg0")
    assert await changes(page, "country") == []


async def test_select_duplicate_labels(page):
    await reset(page)
    await page.select_option("#dup", "p2")
    assert await page.input_value("#dup") == "p2" and len(await changes(page, "dup")) == 1


async def test_select_skips_disabled_rows(page):
    await reset(page)
    await page.select_option("#dis", "e")
    assert await page.input_value("#dis") == "e" and len(await changes(page, "dis")) == 1


async def test_list_box_scrolls_with_the_wheel(page):
    await reset(page)
    await page.select_option("#lb", "opt17")
    assert await page.input_value("#lb") == "opt17"
    assert await ev(page, "wheel", "lb") or await ev(page, "wheel"), "the box is scrolled by the wheel, not by script"
    ch = await changes(page, "lb")
    assert ch and all(c["tr"] for c in ch)


async def test_multi_select_with_ctrl_click(page):
    await reset(page)
    got = await page.select_option("#ms", ["b", "d", "g"])
    assert sorted(got) == ["b", "d", "g"]
    sel = await page.eval_on_selector("#ms", "e => [...e.selectedOptions].map(o => o.value)")
    assert sel == ["b", "d", "g"]
    assert await pressed(page, "Control") or await pressed(page, "Meta")
    assert all(c["tr"] for c in await changes(page, "ms"))


# ------------------------------------------------------------------------------------- range
async def test_range_reaches_the_exact_value(page):
    await reset(page)
    await page.fill("#rng", "145")
    assert await page.input_value("#rng") == "145"
    inputs = await ev(page, "input", "rng")
    assert inputs and all(e["tr"] for e in inputs)
    assert await ev(page, "pointerdown", "rng"), "moved with the pointer, not set by script"


async def test_range_values_playwright_rejects_are_rejected(page):
    """Off the step, out of range, not a number: Playwright raises "Malformed value"; the humanized
    fill must too, never a silently snapped or clamped value."""
    from playwright.async_api import Error
    for bad in ("13", "250", "nan"):
        with pytest.raises(Error):
            await page.fill("#rng2", bad, timeout=3000)
    await page.fill("#rng2", "15")
    assert await page.input_value("#rng2") == "15"


async def test_range_right_to_left(page):
    await page.fill("#rngrtl", "30")
    assert await page.input_value("#rngrtl") == "30"


# ---------------------------------------------------------------------------- date and time
@pytest.mark.parametrize("sel,value", [("#d", "2026-10-11"), ("#dpre", "2026-10-11"), ("#t", "17:45"),
                                       ("#t", "09:05"), ("#ts", "17:45:30"), ("#m", "2026-10"),
                                       ("#w", "2026-W41"), ("#dtl", "2026-10-11T09:30")])
async def test_date_like_fields_are_typed_by_segment(page, sel, value):
    await reset(page)
    await page.fill(sel, value)
    assert await page.input_value(sel) == value
    ident = sel[1:]
    assert await ev(page, "keydown", ident), "typed with the keyboard, not set by script"
    assert await ev(page, "mousedown", ident), "clicked into the field first"
    assert all(e["tr"] for e in await ev(page) if e["id"] == ident and e["t"] in ("input", "change"))


async def test_color_goes_through_playwright(page):
    await page.fill("#col", "#ff0000")
    assert await page.input_value("#col") == "#ff0000"


async def test_fill_a_checkbox_raises_like_playwright(page):
    from playwright.async_api import Error
    with pytest.raises(Error):
        await page.fill("#cb", "x", timeout=2000)


# ------------------------------------------------- cases the first verification found missing
async def test_frame_locator_fill_replaces_the_value(page):
    await page.frame_locator("#fr").locator("#fi").fill("new")
    assert await page.frame_locator("#fr").locator("#fi").input_value() == "new"


async def test_a_covered_field_still_ends_with_the_value(page):
    await reset(page)
    await page.fill("#cov", "new", timeout=5000)
    assert await page.input_value("#cov") == "new"
    stray = [e for e in await ev(page, "keydown") if e["id"] in ("", "BODY", "covlay")]
    assert not stray, "keys went to the page instead of the field"


async def test_a_reformatting_mask_is_typed_once(page):
    await reset(page)
    await page.fill("#phone", "5551234567")
    assert await page.input_value("#phone") == "(555) 123-4567"
    digits = [e for e in await ev(page, "keydown", "phone") if e["key"].isdigit()]
    assert len(digits) == 10, "typed %d digits for a 10-digit number" % len(digits)


async def test_force_on_hidden_targets_does_not_wait(page):
    t0 = time.time()
    await page.click("#hiddenbtn", force=True, timeout=3000)
    await page.fill("#hiddendate", "2026-10-11", force=True, timeout=3000)
    assert time.time() - t0 < 2.5
    assert await page.input_value("#hiddendate") == "2026-10-11"


async def test_type_into_a_hidden_helper_textarea(page):
    t0 = time.time()
    await page.type("#helper", "abc", timeout=3000)
    assert await page.input_value("#helper") == "abc"
    assert time.time() - t0 < 2.5


async def test_press_on_a_button_activates_it_once(page):
    await page.press("#counter", "Enter")
    assert await page.evaluate("window.__clicks || 0") == 1
    await page.locator("#counter").press("Enter")
    assert await page.evaluate("window.__clicks || 0") == 2


async def test_the_callers_default_timeout_is_honoured(page):
    from playwright.async_api import TimeoutError as PWTimeout
    page.set_default_timeout(1500)
    t0 = time.time()
    with pytest.raises(PWTimeout):
        await page.click("#nope")
    with pytest.raises(PWTimeout):
        await page.fill("#nope", "x")
    with pytest.raises(PWTimeout):
        await page.select_option("#nope", "x")
    assert time.time() - t0 < 6.5, "each wait must be the page's 1.5 s, not a hard-coded 30 s"


async def test_check_on_a_checkbox_that_cancels_its_click_clicks_once_and_raises(page):
    from playwright.async_api import Error
    await reset(page)
    with pytest.raises(Error):
        await page.locator("#stubborn").check(timeout=3000)
    assert len(await ev(page, "click", "stubborn")) == 1


async def test_delay_zero_still_holds_the_button(page):
    await reset(page)
    await page.click("#btn", delay=0)
    d, u = (await ev(page, "mousedown", "btn"))[0], (await ev(page, "mouseup", "btn"))[0]
    assert u["ts"] - d["ts"] >= 40


async def test_select_all_holds_the_modifier_before_the_a(page):
    await reset(page)
    await page.fill("#prefilled", "x")
    ks = [e for e in await ev(page) if e["t"] in ("keydown", "keyup") and e["id"] == "prefilled"]
    mod_down = next(e for e in ks if e["t"] == "keydown" and e["key"] in ("Control", "Meta"))
    a_down = next(e for e in ks if e["t"] == "keydown" and e["key"] == "a")
    a_up = next(e for e in ks if e["t"] == "keyup" and e["key"] == "a")
    mod_up = next(e for e in ks if e["t"] == "keyup" and e["key"] in ("Control", "Meta"))
    assert a_down["ts"] - mod_down["ts"] >= 30, "the modifier leads the A like a finger"
    assert mod_up["ts"] - a_up["ts"] >= 15, "and comes up just after it"


async def test_a_select_under_a_floating_overlay_opens_and_picks_once(page):
    await reset(page)
    assert await page.select_option("#selfloat", "e") == ["e"]
    assert await page.input_value("#selfloat") == "e"
    ch = await changes(page, "selfloat")
    assert len(ch) == 1 and ch[0]["tr"]


async def test_typo_gate_reads_through_shadow_roots_and_fails_closed_in_frames(page):
    from clearcote._isolated import ACTIVE_FIELD, async_world_for
    await page.click("#spw #inner")
    assert ((await async_world_for(page).evaluate(ACTIVE_FIELD)) or {}).get("type") == "password"
    await page.frame_locator("#fr").locator("#fpw").click()
    assert await async_world_for(page).evaluate(ACTIVE_FIELD) == {"frame": True}


async def test_no_slips_in_a_password_inside_a_shadow_root(page, typos):
    typos(1.0)
    await reset(page)
    await page.fill("#spw #inner", "abcdef")
    assert await page.input_value("#spw #inner") == "abcdef"
    assert not await pressed(page, "Backspace", "spw")


# ------------------------------------------------- async only: concurrency and cancellation
async def test_concurrent_fills_on_one_page_land_in_their_own_fields(page, typos):
    """asyncio.gather of two fills on one page: the page's lock serialises them (unlocked, a ended up
    with part of its text and b with a's -- verify-final-A S5a)."""
    typos(0.3)
    await asyncio.gather(page.fill("#name", "the quick brown fox"), page.fill("#city", "jumps over it"),
                         page.select_option("#country", "Japan"))
    assert await page.input_value("#name") == "the quick brown fox"
    assert await page.input_value("#city") == "jumps over it"
    assert await page.input_value("#country") == "jap35"


async def _buttons_after(page):
    """MouseEvent.buttons on the next move: anything but 0 means a button was left down."""
    await reset(page)
    await page.mouse.move(400, 30)
    await page.mouse.move(420, 40)
    moves = await ev(page, "mousemove")
    return [m["buttons"] for m in moves]


async def test_a_click_cancelled_mid_press_leaves_no_button_or_key_down(page):
    await reset(page)
    with pytest.raises(asyncio.TimeoutError):
        # the hold is 6 s, so the cancellation lands while the button (and Shift) are down
        await asyncio.wait_for(page.click("#btn", delay=6000, modifiers=["Shift"]), 4.0)
    assert await ev(page, "mousedown", "btn"), "the press had started"
    assert await ev(page, "mouseup", "btn"), "the button came up when the click was cancelled"
    shift_up = [e for e in await ev(page, "keyup") if e["key"] == "Shift"]
    assert shift_up, "Shift came up too"
    assert set(await _buttons_after(page)) == {0}
    await reset(page)
    await page.click("#btn")    # the page lock was released: the next action runs normally
    assert [d["detail"] for d in await ev(page, "mousedown", "btn")] == [1]


async def test_typing_cancelled_mid_word_leaves_no_key_down(page, typos, monkeypatch):
    """Every pair rolls over (the next key goes down before this one comes up), so a cancellation
    almost always lands with a key held; each attempt cancels at a different moment."""
    from clearcote import _humanize_async
    monkeypatch.setattr(_humanize_async, "rollover_rate", lambda persona: 1.0)
    typos(0)
    await page.click("#bio")
    for wait in (0.9, 1.33, 1.71, 2.05):
        await reset(page)
        task = asyncio.ensure_future(page.keyboard.type("asdf jkl; qwer uiop " * 6))
        await asyncio.sleep(wait)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.3)
        held = {}
        for e in await ev(page):
            if e["t"] == "keydown":
                held[e["code"] or e["key"]] = True
            elif e["t"] == "keyup":
                held.pop(e["code"] or e["key"], None)
        assert not held, "keys left down after a cancel at %.2f s: %s" % (wait, sorted(held))


# ------------------------------------------------- cases verifier B found (SC 197 verify-final-B)
# Counts every read of Node.prototype.textContent made in the PAGE's world after it is installed (the
# fixture's own recorder uses a getter saved before this runs). Playwright computes a preview for every
# main-world element handle it makes, and that preview reads textContent where the page can see it.
COUNT_TEXT_READS = """() => { const d = Object.getOwnPropertyDescriptor(Node.prototype, 'textContent');
  window.__reads = 0;
  Object.defineProperty(Node.prototype, 'textContent', { configurable: true, enumerable: d.enumerable,
    get() { window.__reads++; return d.get.call(this); }, set(v) { d.set.call(this, v); } }); }"""
# Where the pointer went, move by move, and every pointerdown a WINDOW capture listener sees (it runs
# before anything Playwright installs during an action, so a press Playwright blocks is still seen). A busy
# renderer merges the moves of one frame into one event, so each pointermove's coalesced moves are recorded:
# a glide never steps far between two of them, the old trial's jump onto the target did.
TRACK_POINTER = """() => { window.__mv = []; window.__downs = [];
  addEventListener('pointermove', e => { const l = e.getCoalescedEvents ? e.getCoalescedEvents() : [];
    for (const c of (l.length ? l : [e])) __mv.push([c.clientX, c.clientY]); }, true);
  addEventListener('pointerdown', e => __downs.push([e.target.id, e.button, e.isTrusted]), true); }"""


async def test_long_list_is_typed_through_its_spaced_label(page):
    # "Item 371" of 400 "Item NNN": the space is typed too. Without it the prefix stopped at "item" and
    # the other 370 rows were single ArrowDowns -- 68 s.
    await reset(page)
    t0 = time.time()
    assert await page.select_option("#long", "Item 371") == ["v371"]
    assert time.time() - t0 < 15
    ch = await changes(page, "long")
    assert len(ch) == 1 and ch[0]["tr"]


async def test_untypeable_long_list_holds_the_arrow_key(page):
    await reset(page)
    t0 = time.time()
    assert await page.select_option("#same", "s100") == ["s100"]
    assert time.time() - t0 < 20, "100 rows are crossed with a held key, not 100 separate presses"
    ch = await changes(page, "same")
    assert len(ch) == 1 and ch[0]["tr"]


@pytest.mark.parametrize("value", ["m10", "m14", "m03"])
async def test_list_box_row_that_needs_a_mid_list_scroll(page, value):
    # The old loop read the box mid-animation, wheeled the wrong way and oscillated, then fell back to an
    # untrusted native change.
    await page.evaluate("document.getElementById('lbmid').scrollTop = 0")
    await reset(page)
    assert await page.select_option("#lbmid", value) == [value]
    assert await page.input_value("#lbmid") == value
    assert not [e for e in await ev(page) if not e["tr"]], "every event trusted: no native fallback"
    assert len(await ev(page, "wheel")) <= 6
    assert await ev(page, "mousedown", "OPTION"), "the row itself was clicked"


async def test_a_click_does_not_jump_the_pointer_or_press_twice(page):
    # The covered-point check used to be a Playwright TRIAL click, which moved the real pointer onto the
    # target and back and fired a pointerdown that only its own listener blocked.
    await page.evaluate(TRACK_POINTER)
    await page.mouse.move(60, 60)
    for sel, button in (("#btn", "left"), ("#btn", "right"), ("#counter", "left")):
        await page.evaluate("__mv.length = 0; __downs.length = 0")
        await page.click(sel, button=button)
        mv = await page.evaluate("__mv")
        jumps = [((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(mv, mv[1:])]
        assert mv and max(jumps or [0]) < 80, "a jump of %.0f px" % max(jumps or [0])
        assert await page.evaluate("__downs") == [[sel[1:], 0 if button == "left" else 2, True]]


async def test_dblclick_presses_twice_not_three_times(page):
    await page.evaluate(TRACK_POINTER)
    await page.evaluate("__downs.length = 0")
    await page.dblclick("#btn")
    assert await page.evaluate("__downs") == [["btn", 0, True], ["btn", 0, True]]


async def test_css_targets_cause_no_main_world_reads(page):
    await page.evaluate(COUNT_TEXT_READS)
    await page.click("#btn")
    await page.locator("#btn").click()
    await page.dblclick("#btn")
    await page.hover("#btn")
    await page.fill("#bio", "two words")
    await page.type("#bio", " more")
    await page.fill("#ce", "edited")
    await page.click("#far")
    assert await page.evaluate("__reads") == 0
    assert await page.input_value("#bio") == "two words more"


async def test_role_targets_cause_at_most_one_main_world_read(page):
    # get_by_role cannot be resolved without Playwright's bounding_box(), whose handle preview reads
    # textContent once; scrolling the window to an off-screen one makes no further handle.
    await page.evaluate(COUNT_TEXT_READS)
    await page.get_by_role("button", name="Target").click()
    assert await page.evaluate("__reads") <= 1
    await page.evaluate("scrollTo(0, 0); __reads = 0")
    await reset(page)
    await page.get_by_role("button", name="Far away").click()
    assert await page.evaluate("__reads") <= 1
    assert [e["tr"] for e in await ev(page, "click", "far")] == [True]


@pytest.mark.parametrize("how", ["css", "role"])
async def test_click_a_button_clipped_by_a_scrolling_container(page, how):
    # In the viewport, but cut off by its overflow:auto parent: the hit test sees that, and the container
    # is scrolled before the humanized click (it is not handed to a native click).
    await page.evaluate("document.getElementById('scroller').scrollIntoView({block: 'center'});"
                        "document.getElementById('scroller').scrollTop = 0")
    await reset(page)
    if how == "css":
        await page.click("#innerbtn")
    else:
        await page.get_by_role("button", name="Inner one").click()
    assert [e["tr"] for e in await ev(page, "click", "innerbtn")] == [True]
    assert await page.evaluate("document.getElementById('scroller').scrollTop") > 0
    assert await ev(page, "mousemove"), "the pointer travelled to it"


# ------------------------------------------------- SC 210 lane F: maxlength, pre-press, bulk, date timing
LONG_VALUE = ("A long note pasted into a text area: it runs well past the two hundred character limit so the "
              "SDK inserts it in one go instead of typing it key by key, but a person still has to click into "
              "the field before pasting anything into it, which is what this checks. The end.")


def press_gaps(events, id=None):
    """ms from the last mousemove (end of its collapsed run) to each pointerdown on ``id``."""
    last, out = None, []
    for e in events:
        if e["t"] == "mousemove":
            last = e.get("te", e["ts"])
        elif e["t"] == "pointerdown" and (id is None or e["id"] == id):
            out.append(None if last is None else e["ts"] - last)
    return out


async def test_maxlength_types_only_what_fits(page, typos):
    typos(0)
    await reset(page)
    await page.fill("#zip", "1234567")
    assert await page.input_value("#zip") == "12345", "Playwright's fill leaves the same five digits"
    keys = [e["key"] for e in await ev(page, "keydown", "zip")]
    assert keys == list("12345"), "only what fits is typed, no wipe and retype: %s" % keys
    inputs = await ev(page, "input", "zip")
    assert [e["value"] for e in inputs] == ["1", "12", "123", "1234", "12345"], "one key each, no insertText"
    assert all(e["tr"] for e in inputs)


@pytest.mark.parametrize("act", ["fill_text", "type", "slider_drag", "range_fill", "select", "listbox",
                                 "date", "check", "click"])
async def test_every_sdk_press_waits_after_the_pointer_arrives(page, act):
    await reset(page)
    target = {"fill_text": "name", "type": "city", "slider_drag": "rng", "range_fill": "rng2",
              "select": "country", "listbox": "OPTION", "date": "d", "check": "cb", "click": "btn"}[act]
    if act == "fill_text":
        await page.fill("#name", "Ada Lovelace")
    elif act == "type":
        await page.type("#city", "Leiden")
    elif act == "slider_drag":
        b = await page.locator("#rng").bounding_box()
        y = b["y"] + b["height"] / 2
        await page.mouse.move(b["x"] + 30, y)
        await page.mouse.down()
        await page.mouse.move(b["x"] + 200, y)
        await page.mouse.up()
    elif act == "range_fill":
        await page.fill("#rng2", "15")
    elif act == "select":
        await page.select_option("#country", label="Iceland")
    elif act == "listbox":
        await page.select_option("#lb", "opt3")
    elif act == "date":
        await page.fill("#d", "1990-04-12")
    elif act == "check":
        await page.check("#cb")
    else:
        await page.click("#btn")
    gaps = press_gaps(await ev(page), target)
    assert gaps, "a press reached #%s" % target
    short = [g if g is None else round(g, 1) for g in gaps if g is None or g < 40]
    assert not short, "pressed too soon after the pointer arrived: %s ms" % short


async def test_a_bulk_fill_clicks_into_the_field_before_inserting(page):
    assert len(LONG_VALUE) > 200
    await reset(page)
    await page.fill("#bio", LONG_VALUE)
    assert await page.input_value("#bio") == LONG_VALUE
    events = await ev(page)
    downs = [e for e in events if e["t"] == "pointerdown" and e["id"] == "bio"]
    inputs = [e for e in events if e["t"] == "input" and e["id"] == "bio"]
    assert downs and downs[0]["tr"], "a trusted press into the field"
    assert inputs and downs[0]["ts"] < inputs[0]["ts"], "the press comes before the insert"
    assert len(inputs) == 1 and inputs[0]["it"] == "insertText", "one insert, not typed key by key"


DATE_VALUES = {"#d": ["1990-04-12", "2031-11-03"], "#t": ["14:35", "07:58"], "#m": ["2026-10", "1999-03"],
               "#w": ["2026-W41", "2019-W07"], "#dtl": ["2026-01-15T09:30", "1987-06-24T21:12"]}


async def test_date_segments_use_the_typing_model_and_survive_rollover(page, monkeypatch):
    import statistics
    from clearcote import _humanize_async
    gaps, rolls = [], 0
    for forced in (False, True):
        if forced:   # every eligible digit pair rolled over: auto-advancing segments still get their digits
            monkeypatch.setattr(_humanize_async, "rollover_rate", lambda p: 1.0)
        for sel, values in DATE_VALUES.items():
            for value in values:
                await reset(page)
                await page.fill(sel, value)
                assert await page.input_value(sel) == value, (sel, value, forced)
                events = await ev(page)
                kd = [e for e in events if e["t"] == "keydown" and e["id"] == sel[1:]]
                ku = [e for e in events if e["t"] == "keyup" and e["id"] == sel[1:]]
                if not forced:
                    gaps += [b["ts"] - a["ts"] for a, b in zip(kd, kd[1:]) if b["ts"] - a["ts"] < 2000]
                for i, d in enumerate(kd[:-1]):
                    up = next((u for u in ku if u["code"] == d["code"] and u["ts"] >= d["ts"]), None)
                    if forced and up is not None and kd[i + 1]["ts"] < up["ts"]:
                        rolls += 1
    cv = statistics.pstdev(gaps) / statistics.mean(gaps)
    assert 0.3 <= cv <= 1.0, "inter-key variation %.2f over %d gaps" % (cv, len(gaps))
    assert rolls, "rollover happened"


async def test_select_open_click_is_held_as_planned(page, monkeypatch):
    import statistics
    from clearcote import _humanize_async
    monkeypatch.setattr(_humanize_async, "click_hold", lambda p, rng=None: 120.0)
    holds = []
    for label in ("Iceland", "Norway", "Chile", "Kenya", "Malta", "Spain"):
        await reset(page)
        await page.select_option("#country", label=label)
        d, u = await ev(page, "mousedown", "country"), await ev(page, "mouseup", "country")
        assert d and u
        holds.append(u[0]["ts"] - d[0]["ts"])
    assert statistics.median(holds) <= 135, "held %s ms for a planned 120" % [round(h) for h in holds]
