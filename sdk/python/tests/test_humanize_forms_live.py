"""LIVE humanized form input against a real Clearcote build (sdk/testdata/humanize_forms.html).

Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary (and a licence is available for a PRO
build). Every assertion is about what a PAGE sees: trusted events, key holds, how many change events a
selection fires, and the exact value left in the field -- the things a behavioural detector and a
form backend read.
"""
import os
import time
from pathlib import Path

import pytest

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")

FIXTURE = (Path(__file__).resolve().parents[2] / "testdata" / "humanize_forms.html").as_uri()
MIN_HOLD_MS = 25   # a finger cannot hold a key or button shorter than this; persona floor is 45


@pytest.fixture(scope="module")
def _no_font_cache_left_behind():
    """A Linux launch points fontconfig at <tmp>/cc-fc-cache, shared by every launch on the machine:
    remove it afterwards only when this module created it (CI fails on anything left in TMPDIR)."""
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
    b = clearcote.launch(executable_path=LIVE, headless=True, humanize=True, fingerprint="forms-live-1",
                         quiet=True)
    yield b
    b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    p.goto(FIXTURE)
    yield p
    p.close()


@pytest.fixture
def typos(monkeypatch):
    def set_rate(r):
        monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", str(r))
    return set_rate


def ev(page, t=None, id=None):
    out = page.evaluate("window.__ev")
    return [e for e in out if (t is None or e["t"] == t) and (id is None or e["id"] == id)]


def reset(page):
    page.evaluate("window.__ev.length = 0")


def key_holds(page, id=None):
    """(key, keydown->keyup ms) for every key that went to element ``id``."""
    open_, out = {}, []
    for e in ev(page):
        if e["t"] not in ("keydown", "keyup") or (id is not None and e["id"] != id):
            continue
        k = e["code"] or e["key"]
        if e["t"] == "keydown":
            open_.setdefault(k, e["ts"])
        elif k in open_:
            out.append((e["key"], e["ts"] - open_.pop(k)))
    return out


def assert_human_keys(page, id):
    holds = key_holds(page, id)
    assert holds, "no keys reached #%s" % id
    short = [(k, round(d, 1)) for k, d in holds if d < MIN_HOLD_MS]
    assert not short, "keys held shorter than a finger can: %s" % short
    assert all(e["tr"] for e in ev(page) if e["t"] in ("keydown", "keyup", "input", "change") and e["id"] == id)


def pressed(page, key, id=None):
    return [e for e in ev(page, "keydown", id) if e["key"] == key]


# ------------------------------------------------------------------------------------- clicks
def test_right_click_is_a_right_click(page):
    reset(page)
    page.click("#btn", button="right")
    downs = ev(page, "mousedown", "btn")
    assert [d["button"] for d in downs] == [2] and downs[0]["tr"]
    assert ev(page, "contextmenu", "btn"), "a right click opens the context menu"
    assert not ev(page, "click", "btn"), "a right click is not a click"


def test_middle_click_through_a_locator(page):
    reset(page)
    page.locator("#btn").click(button="middle")
    assert [d["button"] for d in ev(page, "mousedown", "btn")] == [1]
    assert ev(page, "auxclick", "btn")


def test_click_count_two_is_a_double_click(page):
    reset(page)
    page.click("#btn", click_count=2)
    assert [d["detail"] for d in ev(page, "mousedown", "btn")] == [1, 2]
    assert ev(page, "dblclick", "btn")[0]["tr"]


def test_dblclick_has_human_holds_and_gap(page):
    reset(page)
    page.dblclick("#btn")
    downs, ups = ev(page, "mousedown", "btn"), ev(page, "mouseup", "btn")
    assert len(downs) == 2 and len(ups) == 2
    for d, u in zip(downs, ups):
        assert u["ts"] - d["ts"] >= 40, "button held like a finger"
    gap = downs[1]["ts"] - ups[0]["ts"]
    assert 50 <= gap <= 450, gap
    assert ev(page, "dblclick", "btn")


def test_modifiers_are_held_around_the_press(page):
    reset(page)
    page.click("#btn", modifiers=["Shift"])
    click = ev(page, "click", "btn")[0]
    assert click["shift"] and click["tr"]
    shift_down = pressed(page, "Shift")[0]["ts"]
    shift_up = [e for e in ev(page, "keyup") if e["key"] == "Shift"][0]["ts"]
    assert shift_down < ev(page, "mousedown", "btn")[0]["ts"]
    assert shift_up > ev(page, "mouseup", "btn")[0]["ts"]


def test_position_is_honoured(page):
    reset(page)
    page.click("#btn", position={"x": 10, "y": 12})
    d = ev(page, "mousedown", "btn")[0]
    assert abs(d["ox"] - 10) <= 1.5 and abs(d["oy"] - 12) <= 1.5


def test_trial_performs_no_action(page):
    reset(page)
    page.click("#trialbtn", trial=True)
    page.locator("#trialbtn").click(trial=True)
    page.hover("#trialbtn", trial=True)
    assert not ev(page, "mousedown", "trialbtn") and not ev(page, "click", "trialbtn")


def test_delay_sets_the_hold(page):
    reset(page)
    page.click("#btn", delay=300)
    d, u = ev(page, "mousedown", "btn")[0], ev(page, "mouseup", "btn")[0]
    assert 270 <= u["ts"] - d["ts"] <= 450


def test_glide_precedes_the_press(page):
    reset(page)
    page.click("#btn")
    moves = [e for e in ev(page) if e["t"] == "mousemove"]
    assert sum(m["n"] for m in moves) >= 5, "the pointer travels to the target"


def test_first_click_on_fresh_pages_is_humanized(browser):
    """The covered-point probe used to time out on ~1 in 6 FIRST clicks of a fresh page (slow first
    frames, not an overlay) and send a plain native click instead. Six fresh pages, every one glides."""
    for _ in range(6):
        p = browser.new_page()
        try:
            p.goto(FIXTURE)
            p.click("#btn")
            moves = sum(e["n"] for e in ev(p, "mousemove"))
            assert moves >= 5 and ev(p, "mousedown", "btn"), "first click went out unhumanized"
            d, u = ev(p, "mousedown", "btn")[0], ev(p, "mouseup", "btn")[0]
            assert u["ts"] - d["ts"] >= 40
        finally:
            p.close()


def test_locator_strictness_still_raises(page):
    from playwright.sync_api import Error
    with pytest.raises(Error):
        page.locator("button.dup").click(timeout=2000)


def test_missing_element_fails_after_one_timeout(page):
    from playwright.sync_api import TimeoutError as PWTimeout
    for act in (lambda: page.click("#nope", timeout=1500), lambda: page.fill("#nope", "x", timeout=1500),
                lambda: page.locator("#nope").click(timeout=1500)):
        t0 = time.time()
        with pytest.raises(PWTimeout):
            act()
        assert time.time() - t0 < 2.8, "a missing element must not wait the timeout twice"


def _btn_box(page):
    page.locator("#btn").scroll_into_view_if_needed()   # raw mouse input does not scroll
    return page.locator("#btn").bounding_box()


def test_mouse_click_xy_right_button(page):
    box = _btn_box(page)
    reset(page)
    page.mouse.click(box["x"] + 20, box["y"] + 15, button="right")
    assert ev(page, "contextmenu", "btn")


def test_mouse_dblclick_xy(page):
    box = _btn_box(page)
    reset(page)
    page.mouse.dblclick(box["x"] + 30, box["y"] + 20)
    assert ev(page, "dblclick", "btn") and ev(page, "dblclick", "btn")[0]["tr"]
    downs, ups = ev(page, "mousedown", "btn"), ev(page, "mouseup", "btn")
    assert all(u["ts"] - d["ts"] >= 40 for d, u in zip(downs, ups))


def test_check_with_trial_changes_nothing(page):
    reset(page)
    page.check("#cb", trial=True)
    page.locator("#cb").check(trial=True)
    assert not page.is_checked("#cb")
    assert not ev(page, "click", "cb") and not ev(page, "mousedown", "cb")


def test_check_uncheck_and_set_checked_are_humanized(page):
    reset(page)
    page.check("#cb")
    assert page.is_checked("#cb")
    assert ev(page, "mousedown", "cb") and all(e["tr"] for e in ev(page, "click", "cb"))
    page.locator("#cb").set_checked(False)
    assert not page.is_checked("#cb")
    page.uncheck("#cb")          # already unchecked: nothing happens
    assert not page.is_checked("#cb")
    assert len(ev(page, "click", "cb")) == 2


# ------------------------------------------------------------------------------------- text
def test_fill_text_with_held_keys(page):
    reset(page)
    page.fill("#name", "Ada Lovelace")
    assert page.input_value("#name") == "Ada Lovelace"
    assert_human_keys(page, "name")


def test_fill_clears_a_prefilled_field_with_held_keys(page):
    reset(page)
    page.fill("#prefilled", "new")
    assert page.input_value("#prefilled") == "new"
    holds = dict((k, d) for k, d in key_holds(page, "prefilled") if k in ("Control", "Meta", "a", "Backspace"))
    assert "Backspace" in holds and holds["Backspace"] >= MIN_HOLD_MS
    assert any(k in holds for k in ("Control", "Meta"))
    assert all(d >= MIN_HOLD_MS for d in holds.values()), holds


def test_fill_on_an_empty_field_presses_no_clear_keys(page, typos):
    typos(0)
    reset(page)
    page.fill("#bio", "hello")
    assert page.input_value("#bio") == "hello"
    assert not [e for e in ev(page, "keydown", "bio") if e["key"] in ("Backspace", "Control", "Meta")]


def test_clear_empties_the_field(page):
    page.fill("#prefilled", "abc")
    reset(page)
    page.locator("#prefilled").clear()
    assert page.input_value("#prefilled") == ""
    assert_human_keys(page, "prefilled")


def test_get_by_label_and_role_are_humanized(page):
    reset(page)
    page.get_by_label("City").fill("Paris")
    assert page.input_value("#city") == "Paris"
    assert_human_keys(page, "city")
    reset(page)
    page.get_by_role("textbox", name="City").press_sequentially("!")
    assert page.input_value("#city") == "Paris!"
    assert_human_keys(page, "city")


def test_filter_and_first_locators_are_humanized(page):
    reset(page)
    page.locator("input[type=text]").filter(has_not_text="zzz").nth(0).fill("first")
    assert page.input_value("#name") == "first"
    assert_human_keys(page, "name")


def test_field_under_a_floating_label(page):
    # The label covers every probe point of the field: a person clicks the label (it passes the focus on)
    # and types; the fill must not fall back to Playwright's insertText.
    reset(page)
    page.fill("#float", "abc")
    assert page.input_value("#float") == "abc"
    assert [e["key"] for e in ev(page, "keydown", "float")] == ["a", "b", "c"]
    assert_human_keys(page, "float")


def test_textarea_newlines_and_contenteditable(page):
    page.fill("#bio", "two\nlines")
    assert page.input_value("#bio") == "two\nlines"
    page.fill("#ce", "hello world")
    assert page.eval_on_selector("#ce", "e => e.textContent") == "hello world"


def test_press_holds_the_key(page):
    page.fill("#name", "x")
    reset(page)
    page.press("#name", "Backspace")
    assert page.input_value("#name") == ""
    assert_human_keys(page, "name")


# ------------------------------------------------------------------------------------- typos
def test_forced_typos_are_corrected(page, typos):
    typos(0.35)
    reset(page)
    text = "the quick brown fox jumps over 2024"
    page.fill("#name", text)
    assert page.input_value("#name") == text
    assert pressed(page, "Backspace", "name"), "with a 35 % rate some slip had to be corrected"
    assert_human_keys(page, "name")


@pytest.mark.parametrize("sel,value", [
    ("#email", "ada@example.com"), ("#pw", "s3cretPassw0rd"), ("#num", "123456"), ("#tel", "5551234567"),
    ("#url", "https://example.com/a"), ("#otp", "123456")])
def test_no_typos_where_a_slip_breaks_the_field(page, typos, sel, value):
    typos(0.6)
    reset(page)
    page.fill(sel, value)
    assert page.input_value(sel) == value
    assert not pressed(page, "Backspace", sel[1:]), "no slips in %s" % sel


def test_keyboard_type_into_a_focused_password_makes_no_slips(page, typos):
    typos(0.6)
    page.click("#pw")
    reset(page)
    page.keyboard.type("abcdefgh")
    assert page.input_value("#pw") == "abcdefgh"
    assert not pressed(page, "Backspace", "pw")


def test_typos_respect_maxlength(page, typos):
    typos(1.0)
    for _ in range(3):
        page.fill("#zip", "12345")
        assert page.input_value("#zip") == "12345"


def test_typos_in_a_masked_field_never_delete_real_digits(page, typos):
    typos(1.0)
    for _ in range(3):
        page.fill("#masked", "24680")
        assert page.input_value("#masked") == "24680"


# ------------------------------------------------------------------------------------ select
def changes(page, id):
    return [e for e in ev(page) if e["t"] == "change" and e["id"] == id]


def test_select_by_label_fires_one_trusted_change(page):
    reset(page)
    assert page.select_option("#country", "Germany") == ["ger25"]
    assert page.input_value("#country") == "ger25"
    ch = changes(page, "country")
    assert len(ch) == 1 and ch[0]["tr"]
    assert ev(page, "mousedown", "country"), "the drop-down was opened with the mouse"


@pytest.mark.parametrize("kw,want", [({"value": "swi50"}, "swi50"), ({"index": 3}, "and3"),
                                     ({"label": "Japan"}, "jap35"), ({"value": "United Kingdom"}, "uni53")])
def test_select_value_index_label(page, kw, want):
    reset(page)
    value = kw.pop("value", None)
    assert page.select_option("#country", value, **kw) == [want]
    assert page.input_value("#country") == want
    assert len(changes(page, "country")) == 1


def test_select_through_a_locator(page):
    reset(page)
    assert page.locator("#country").select_option("France") == ["fra23"]
    assert len(changes(page, "country")) == 1


def test_select_far_down_a_long_list_is_quick(page):
    reset(page)
    t0 = time.time()
    page.select_option("#country", "Uruguay")
    assert page.input_value("#country") == "uru55"
    assert len(changes(page, "country")) == 1
    assert time.time() - t0 < 10


def test_select_the_current_option_fires_nothing(page):
    reset(page)
    page.select_option("#country", "afg0")
    assert changes(page, "country") == []


def test_select_duplicate_labels(page):
    reset(page)
    page.select_option("#dup", "p2")
    assert page.input_value("#dup") == "p2" and len(changes(page, "dup")) == 1


def test_select_skips_disabled_rows(page):
    reset(page)
    page.select_option("#dis", "e")
    assert page.input_value("#dis") == "e" and len(changes(page, "dis")) == 1


def test_list_box_scrolls_with_the_wheel(page):
    reset(page)
    page.select_option("#lb", "opt17")
    assert page.input_value("#lb") == "opt17"
    assert ev(page, "wheel", "lb") or ev(page, "wheel"), "the box is scrolled by the wheel, not by script"
    assert changes(page, "lb") and all(c["tr"] for c in changes(page, "lb"))


def test_multi_select_with_ctrl_click(page):
    reset(page)
    got = page.select_option("#ms", ["b", "d", "g"])
    assert sorted(got) == ["b", "d", "g"]
    sel = page.eval_on_selector("#ms", "e => [...e.selectedOptions].map(o => o.value)")
    assert sel == ["b", "d", "g"]
    assert pressed(page, "Control") or pressed(page, "Meta")
    assert all(c["tr"] for c in changes(page, "ms"))


# ------------------------------------------------------------------------------------- range
def test_range_reaches_the_exact_value(page):
    reset(page)
    page.fill("#rng", "145")
    assert page.input_value("#rng") == "145"
    inputs = [e for e in ev(page, "input", "rng")]
    assert inputs and all(e["tr"] for e in inputs)
    assert ev(page, "pointerdown", "rng"), "moved with the pointer, not set by script"


def test_range_values_playwright_rejects_are_rejected(page):
    """Off the step, out of range, not a number: Playwright raises "Malformed value"; the humanized
    fill must too, never a silently snapped or clamped value."""
    from playwright.sync_api import Error
    for bad in ("13", "250", "nan"):
        with pytest.raises(Error):
            page.fill("#rng2", bad, timeout=3000)
    page.fill("#rng2", "15")
    assert page.input_value("#rng2") == "15"


def test_range_right_to_left(page):
    page.fill("#rngrtl", "30")
    assert page.input_value("#rngrtl") == "30"


# ---------------------------------------------------------------------------- date and time
@pytest.mark.parametrize("sel,value", [("#d", "2026-10-11"), ("#dpre", "2026-10-11"), ("#t", "17:45"),
                                       ("#t", "09:05"), ("#ts", "17:45:30"), ("#m", "2026-10"),
                                       ("#w", "2026-W41"), ("#dtl", "2026-10-11T09:30")])
def test_date_like_fields_are_typed_by_segment(page, sel, value):
    reset(page)
    page.fill(sel, value)
    assert page.input_value(sel) == value
    ident = sel[1:]
    assert ev(page, "keydown", ident), "typed with the keyboard, not set by script"
    assert ev(page, "mousedown", ident), "clicked into the field first"
    assert all(e["tr"] for e in ev(page) if e["id"] == ident and e["t"] in ("input", "change"))


def test_color_goes_through_playwright(page):
    page.fill("#col", "#ff0000")
    assert page.input_value("#col") == "#ff0000"


def test_fill_a_checkbox_raises_like_playwright(page):
    from playwright.sync_api import Error
    with pytest.raises(Error):
        page.fill("#cb", "x", timeout=2000)


# ------------------------------------------------- cases the first verification found missing
def test_frame_locator_fill_replaces_the_value(page):
    page.frame_locator("#fr").locator("#fi").fill("new")
    assert page.frame_locator("#fr").locator("#fi").input_value() == "new"


def test_a_covered_field_still_ends_with_the_value(page):
    reset(page)
    page.fill("#cov", "new", timeout=5000)
    assert page.input_value("#cov") == "new"
    stray = [e for e in ev(page, "keydown") if e["id"] in ("", "BODY", "covlay")]
    assert not stray, "keys went to the page instead of the field"


def test_a_reformatting_mask_is_typed_once(page):
    reset(page)
    page.fill("#phone", "5551234567")
    assert page.input_value("#phone") == "(555) 123-4567"
    digits = [e for e in ev(page, "keydown", "phone") if e["key"].isdigit()]
    assert len(digits) == 10, "typed %d digits for a 10-digit number" % len(digits)


def test_force_on_hidden_targets_does_not_wait(page):
    t0 = time.time()
    page.click("#hiddenbtn", force=True, timeout=3000)
    page.fill("#hiddendate", "2026-10-11", force=True, timeout=3000)
    assert time.time() - t0 < 2.5
    assert page.input_value("#hiddendate") == "2026-10-11"


def test_type_into_a_hidden_helper_textarea(page):
    t0 = time.time()
    page.type("#helper", "abc", timeout=3000)
    assert page.input_value("#helper") == "abc"
    assert time.time() - t0 < 2.5


def test_press_on_a_button_activates_it_once(page):
    page.press("#counter", "Enter")
    assert page.evaluate("window.__clicks || 0") == 1
    page.locator("#counter").press("Enter")
    assert page.evaluate("window.__clicks || 0") == 2


def test_the_callers_default_timeout_is_honoured(page):
    from playwright.sync_api import TimeoutError as PWTimeout
    page.set_default_timeout(1500)
    t0 = time.time()
    with pytest.raises(PWTimeout):
        page.click("#nope")
    with pytest.raises(PWTimeout):
        page.fill("#nope", "x")
    with pytest.raises(PWTimeout):
        page.select_option("#nope", "x")
    assert time.time() - t0 < 6.5, "each wait must be the page's 1.5 s, not a hard-coded 30 s"


def test_check_on_a_checkbox_that_cancels_its_click_clicks_once_and_raises(page):
    from playwright.sync_api import Error
    reset(page)
    with pytest.raises(Error):
        page.locator("#stubborn").check(timeout=3000)
    assert len(ev(page, "click", "stubborn")) == 1


def test_delay_zero_still_holds_the_button(page):
    reset(page)
    page.click("#btn", delay=0)
    d, u = ev(page, "mousedown", "btn")[0], ev(page, "mouseup", "btn")[0]
    assert u["ts"] - d["ts"] >= 40


def test_select_all_holds_the_modifier_before_the_a(page):
    reset(page)
    page.fill("#prefilled", "x")
    ks = [e for e in ev(page) if e["t"] in ("keydown", "keyup") and e["id"] == "prefilled"]
    mod_down = next(e for e in ks if e["t"] == "keydown" and e["key"] in ("Control", "Meta"))
    a_down = next(e for e in ks if e["t"] == "keydown" and e["key"] == "a")
    a_up = next(e for e in ks if e["t"] == "keyup" and e["key"] == "a")
    mod_up = next(e for e in ks if e["t"] == "keyup" and e["key"] in ("Control", "Meta"))
    assert a_down["ts"] - mod_down["ts"] >= 30, "the modifier leads the A like a finger"
    assert mod_up["ts"] - a_up["ts"] >= 15, "and comes up just after it"


def test_a_select_under_a_floating_overlay_opens_and_picks_once(page):
    reset(page)
    assert page.select_option("#selfloat", "e") == ["e"]
    assert page.input_value("#selfloat") == "e"
    ch = changes(page, "selfloat")
    assert len(ch) == 1 and ch[0]["tr"]


def test_typo_gate_reads_through_shadow_roots_and_fails_closed_in_frames(page):
    from clearcote._isolated import ACTIVE_FIELD, world_for
    page.click("#spw #inner")
    assert (world_for(page).evaluate(ACTIVE_FIELD) or {}).get("type") == "password"
    page.frame_locator("#fr").locator("#fpw").click()
    assert world_for(page).evaluate(ACTIVE_FIELD) == {"frame": True}


def test_no_slips_in_a_password_inside_a_shadow_root(page, typos):
    typos(1.0)
    reset(page)
    page.fill("#spw #inner", "abcdef")
    assert page.input_value("#spw #inner") == "abcdef"
    assert not pressed(page, "Backspace", "spw")


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


def test_long_list_is_typed_through_its_spaced_label(page):
    # "Item 371" of 400 "Item NNN": the space is typed too. Without it the prefix stopped at "item" and
    # the other 370 rows were single ArrowDowns -- 68 s.
    reset(page)
    t0 = time.time()
    assert page.select_option("#long", "Item 371") == ["v371"]
    assert time.time() - t0 < 15
    ch = changes(page, "long")
    assert len(ch) == 1 and ch[0]["tr"]


def test_untypeable_long_list_holds_the_arrow_key(page):
    reset(page)
    t0 = time.time()
    assert page.select_option("#same", "s100") == ["s100"]
    assert time.time() - t0 < 20, "100 rows are crossed with a held key, not 100 separate presses"
    ch = changes(page, "same")
    assert len(ch) == 1 and ch[0]["tr"]


@pytest.mark.parametrize("value", ["m10", "m14", "m03"])
def test_list_box_row_that_needs_a_mid_list_scroll(page, value):
    # The old loop read the box mid-animation, wheeled the wrong way and oscillated, then fell back to an
    # untrusted native change.
    page.evaluate("document.getElementById('lbmid').scrollTop = 0")
    reset(page)
    assert page.select_option("#lbmid", value) == [value]
    assert page.input_value("#lbmid") == value
    assert not [e for e in ev(page) if not e["tr"]], "every event trusted: no native fallback"
    assert len(ev(page, "wheel")) <= 6
    assert ev(page, "mousedown", "OPTION"), "the row itself was clicked"


def test_a_click_does_not_jump_the_pointer_or_press_twice(page):
    # The covered-point check used to be a Playwright TRIAL click, which moved the real pointer onto the
    # target and back and fired a pointerdown that only its own listener blocked.
    page.evaluate(TRACK_POINTER)
    page.mouse.move(60, 60)
    for sel, button in (("#btn", "left"), ("#btn", "right"), ("#counter", "left")):
        page.evaluate("__mv.length = 0; __downs.length = 0")
        page.click(sel, button=button)
        mv = page.evaluate("__mv")
        jumps = [((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(mv, mv[1:])]
        assert mv and max(jumps or [0]) < 80, "a jump of %.0f px" % max(jumps or [0])
        assert page.evaluate("__downs") == [[sel[1:], 0 if button == "left" else 2, True]]


def test_dblclick_presses_twice_not_three_times(page):
    page.evaluate(TRACK_POINTER)
    page.evaluate("__downs.length = 0")
    page.dblclick("#btn")
    assert page.evaluate("__downs") == [["btn", 0, True], ["btn", 0, True]]


def test_css_targets_cause_no_main_world_reads(page):
    page.evaluate(COUNT_TEXT_READS)
    page.click("#btn")
    page.locator("#btn").click()
    page.dblclick("#btn")
    page.hover("#btn")
    page.fill("#bio", "two words")
    page.type("#bio", " more")
    page.fill("#ce", "edited")
    page.click("#far")
    assert page.evaluate("__reads") == 0
    assert page.input_value("#bio") == "two words more"


def test_role_targets_cause_at_most_one_main_world_read(page):
    # get_by_role cannot be resolved without Playwright's bounding_box(), whose handle preview reads
    # textContent once; scrolling the window to an off-screen one makes no further handle.
    page.evaluate(COUNT_TEXT_READS)
    page.get_by_role("button", name="Target").click()
    assert page.evaluate("__reads") <= 1
    page.evaluate("scrollTo(0, 0); __reads = 0")
    reset(page)
    page.get_by_role("button", name="Far away").click()
    assert page.evaluate("__reads") <= 1
    assert [e["tr"] for e in ev(page, "click", "far")] == [True]


@pytest.mark.parametrize("how", ["css", "role"])
def test_click_a_button_clipped_by_a_scrolling_container(page, how):
    # In the viewport, but cut off by its overflow:auto parent: the hit test sees that, and the container
    # is scrolled before the humanized click (it is not handed to a native click).
    page.evaluate("document.getElementById('scroller').scrollIntoView({block: 'center'});"
                  "document.getElementById('scroller').scrollTop = 0")
    reset(page)
    if how == "css":
        page.click("#innerbtn")
    else:
        page.get_by_role("button", name="Inner one").click()
    assert [e["tr"] for e in ev(page, "click", "innerbtn")] == [True]
    assert page.evaluate("document.getElementById('scroller').scrollTop") > 0
    assert ev(page, "mousemove"), "the pointer travelled to it"
