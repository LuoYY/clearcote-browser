"""SC 232 lab triage (2026-10-16): the SYNC humanizer -- what the lab drives -- on the simulators of
test_humanize_forms_async / test_humanize_keyboard through _hz_sync_sim, for the two gaps the lab and verifier B found:

* a visible checkbox's label click (the hit test is asked about the LABEL ELEMENT's box, not its text rect; before,
  every visible-label click failed it and went to the input: 0 of 20 / 0 of 18);
* a press on a button out of Tab reach (a click into the text stop next to it + Tab; Tab in from the top when nothing
  is focused; before, the pointer glided onto it and Playwright's focus was used: 0 of 4 / 6 of 12).

Twins of the async tests in test_humanize_forms_async.py / test_humanize_keyboard.py; all three fail on d2b9ab6."""
import time

import pytest

import clearcote._humanize as HS
from _hz_sync_sim import Sync
from test_humanize_forms_async import Field, Page, kinds, non_trial
from test_humanize_keyboard import KPage


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_TYPO_RATE", "0")


def _attached(seed, page_cls, *fields):
    sim = page_cls(*fields)
    page = Sync.of(sim)
    HS.attach_humanize(None, page, humanize=True, seed=seed)
    return sim, page


def test_sync_visible_label_click_is_hit_tested_on_the_label_element(fast, monkeypatch):
    monkeypatch.setattr(HS, "label_draw", lambda *a, **k: True)   # this click: the label (the trait is not the question)
    cb = Field(type="checkbox", rect=(100.0, 100.0, 16.0, 16.0))
    lab = Field(tag="label", type="", rect=(96.0, 96.0, 420.0, 24.0), control=cb, text_rect=(124.0, 100.0, 110.0, 16.0))
    sim, page = _attached("label-hit", Page, ("#cb", cb), ("#lab", lab))
    page.check("#cb")
    assert cb.checked and not non_trial(sim)
    downs = kinds(sim, "down")
    assert len(downs) == 1
    x, y = downs[0][4]
    assert lab.inside(x, y) and not cb.inside(x, y) and 124.0 <= x <= 234.0 and 100.0 <= y <= 116.0, (x, y)


# SC 239 box probe (10-17): a label WRAPPING its visible checkbox -- LABEL_OF's text rect was the input's own box
# (see test_humanize_forms_async.WRAP_LABEL); the same two cases through the SYNC humanizer (what the lab drives).
def test_sync_wrapping_labels_text_rect_over_the_input_is_no_label_aim(fast, monkeypatch):
    from test_humanize_forms_async import WRAP_INPUT, WRAP_LABEL
    monkeypatch.setattr(HS, "label_draw", lambda *a, **k: True)
    cb = Field(type="checkbox", rect=WRAP_INPUT)
    lab = Field(tag="label", type="", rect=WRAP_LABEL, control=cb, wraps=True, text_rect=WRAP_INPUT)
    sim, page = _attached("label-wrap", Page, ("#cb", cb), ("#lab", lab))
    page.check("#cb")
    assert cb.checked and not non_trial(sim)
    downs = kinds(sim, "down")
    assert len(downs) == 1 and cb.inside(*downs[0][4]), downs
    assert not [h for h in sim.hits if h[1] == WRAP_LABEL], "a label aim was tried on the input's own box"


def test_sync_wrapping_labels_text_beside_the_input_is_aimed_and_hit_tested_on_the_label(fast, monkeypatch):
    from test_humanize_forms_async import WRAP_INPUT, WRAP_LABEL
    monkeypatch.setattr(HS, "label_draw", lambda *a, **k: True)
    cb = Field(type="checkbox", rect=WRAP_INPUT)
    lab = Field(tag="label", type="", rect=WRAP_LABEL, control=cb, wraps=True, text_rect=(61.0, 75.0, 193.8, 15.0))
    sim, page = _attached("label-wrap", Page, ("#cb", cb), ("#lab", lab))
    page.check("#cb")
    assert cb.checked and not non_trial(sim)
    downs = kinds(sim, "down")
    assert len(downs) == 1
    x, y = downs[0][4]
    assert lab.inside(x, y) and not cb.inside(x, y) and 61.0 <= x <= 254.8 and 75.0 <= y <= 90.0, (x, y)
    assert [h for h in sim.hits if h[1] == WRAP_LABEL and h[0] is lab], "hit-tested on the label element's box"


def test_sync_press_on_a_button_out_of_tab_reach_clicks_the_field_before_it_and_tabs(fast):
    btn = Field(tag="button", type="", rect=(100.0, 400.0, 160.0, 40.0))
    q = Field(rect=(100.0, 300.0, 200.0, 24.0))
    sim, page = _attached("ha-1", KPage, ("#far", Field(rect=(100.0, 50.0, 200.0, 24.0))),
                          ("#b1", Field(tag="button", type="", rect=(100.0, 100.0, 60.0, 30.0))),
                          ("#b2", Field(tag="button", type="", rect=(100.0, 150.0, 60.0, 30.0))),
                          ("#b3", Field(tag="button", type="", rect=(100.0, 200.0, 60.0, 30.0))),
                          ("#q", q), ("#go", btn))
    page.fill("#far", "x")          # the focus sits 5 stops before #go
    downs = len(kinds(sim, "down"))
    page.press("#go", "Enter")
    assert not sim.native, sim.native
    ds = kinds(sim, "down")[downs:]
    assert len(ds) == 1 and q.inside(*ds[0][4]), ("one click, into the field before the button", ds)
    assert [e[1] for e in kinds(sim, "press")][-2:] == ["Tab", "Enter"] and sim.focused is btn


def test_sync_press_on_a_button_with_nothing_focused_tabs_in_from_the_top(fast):
    btn = Field(tag="button", type="", rect=(100.0, 200.0, 160.0, 40.0))
    sim, page = _attached("ha-2", KPage, ("#b1", Field(tag="button", type="", rect=(100.0, 100.0, 60.0, 30.0))), ("#go", btn))
    assert sim.focused is None
    page.press("#go", "Enter")
    assert not sim.native and not kinds(sim, "down"), (sim.native, kinds(sim, "down"))
    assert [e[1] for e in kinds(sim, "press")] == ["Tab", "Tab", "Enter"] and sim.focused is btn
