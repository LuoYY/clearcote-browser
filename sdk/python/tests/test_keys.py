"""The keyboard planner (clearcote._keys) against the SHARED vectors in sdk/testdata/humanize_typing_vectors.json
-- the same file the Node (keys.test.ts) and .NET (KeysTests.cs) suites read -- plus the properties the
vectors cannot express. Lane D of SC 232; on 0.51.0 there was no planner (the module does not exist)."""
import dataclasses
import json
import os
import random
import statistics

import pytest

from clearcote import _keys as K
from clearcote._motion import key_dwell, make_persona, mulberry32

VEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "testdata", "humanize_typing_vectors.json")
with open(VEC_PATH, encoding="utf-8") as _f:
    VEC = json.load(_f)


def _traits(p):
    return K.typing_traits(make_persona(p).seed)


@pytest.mark.parametrize("case", VEC["persona_pin"], ids=lambda c: str(c["input"]))
def test_make_persona_fields_are_unchanged(case):
    # The typing identity is a SALTED side stream: every existing persona field stays bit-identical.
    assert dataclasses.asdict(make_persona(case["input"])) == case["persona"]


@pytest.mark.parametrize("case", VEC["typing_traits"], ids=lambda c: c["persona"])
def test_typing_traits_vectors(case):
    got = dataclasses.asdict(_traits(case["persona"]))
    for k, v in got.items():
        assert v == pytest.approx(case[k], abs=1e-12) if isinstance(v, float) else v == case[k], k


def test_typing_traits_are_in_their_bands_and_differ_across_seeds():
    ts = [_traits("seed-%d" % i) for i in range(400)]
    assert all(28 <= t.wpm <= 75 and 1.5 <= t.same_finger <= 1.9 and 1.1 <= t.same_hand <= 1.3
               and 0.85 <= t.alternate <= 1.0 and 0.60 <= t.tab_pref <= 0.85 and 0.005 <= t.typo_rate <= 0.04
               and 252 <= t.repeat_first_ms <= 498 and 26 <= t.repeat_gap_ms <= 39
               for t in ts)
    assert len({round(t.wpm, 3) for t in ts}) > 390
    assert {t.select_style for t in ts} == {"triple", "keys"} and {t.blur_style for t in ts} == {"tab", "click"}


@pytest.mark.parametrize("case", VEC["finger_of"], ids=lambda c: repr(c["ch"]))
def test_finger_of_vectors(case):
    got = K.finger_of(case["ch"])
    assert (list(got) if got else None) == case["want"]


@pytest.mark.parametrize("case", VEC["bigram_class"], ids=lambda c: c["prev"] + c["ch"])
def test_bigram_class_vectors(case):
    assert K.bigram_class(case["prev"], case["ch"]) == (case["cls"], case["jump"])


@pytest.mark.parametrize("case", VEC["gaps"], ids=lambda c: c["persona"])
def test_gap_vectors(case):
    tr, text = _traits(case["persona"]), case["text"]
    rng = mulberry32(case["rng"])
    assert [K.flight_ms(tr, text[i], text[i + 1], rng) for i in range(len(text) - 1)] == \
        pytest.approx(case["flight_ms"], abs=1e-9)
    rng = mulberry32(case["rng"])
    assert [K.plan_gap(tr, text[i], text[i + 1], rng) for i in range(len(text) - 1)] == \
        pytest.approx(case["gap_ms"], abs=1e-9)


@pytest.mark.parametrize("case", VEC["split_chord"], ids=lambda c: c["key"])
def test_split_chord_vectors(case):
    assert K.split_chord(case["key"]) == (case["mods"], case["key_out"])


@pytest.mark.parametrize("case", VEC["timing"], ids=lambda c: c["persona"])
def test_timing_vectors(case):
    tr, rng = _traits(case["persona"]), mulberry32(case["rng"])
    rp = K.plan_repeat(tr, rng)
    assert [rp.first_ms, rp.gap_ms, rp.release_ms] == pytest.approx(case["repeat"], abs=1e-9)
    assert K.modifier_lead_ms(rng) == pytest.approx(case["lead"], abs=1e-9)
    assert K.modifier_lag_ms(rng) == pytest.approx(case["lag"], abs=1e-9)
    assert [K.submit_pause_ms(tr, rng) for _ in range(4)] == pytest.approx(case["submit"], abs=1e-9)
    assert [K.tab_pause_ms(tr, rng) for _ in range(4)] == pytest.approx(case["tab"], abs=1e-9)


def test_ks_reference_is_the_planner():
    tr, rng, t = _traits(VEC["ks_persona"]), mulberry32(VEC["ks_rng"]), VEC["ks_text"]
    assert [K.plan_gap(tr, t[i], t[i + 1], rng) for i in range(len(t) - 1)] == pytest.approx(VEC["ks_reference"], abs=1e-9)
    assert len(VEC["ks_reference"]) == 500


def test_bands_of_the_pauses_and_repeat():
    rng = mulberry32(3)
    for p in ("ha-1", "ha-4", "x", "y"):
        tr = _traits(p)
        for _ in range(300):
            assert 250 <= K.submit_pause_ms(tr, rng) <= 900 and 120 <= K.tab_pause_ms(tr, rng) <= 400
            rp = K.plan_repeat(tr, rng)
            assert 250 <= rp.first_ms <= 500 and 25 <= rp.gap_ms <= 40 and 20 <= rp.release_ms <= 60
            assert 40 <= K.modifier_lead_ms(rng) <= 110 and 20 <= K.modifier_lag_ms(rng) <= 80


def test_key_repeat_delay_and_rate_are_persona_traits_with_fresh_per_run_jitter():
    """R1 (verifier A, SC 232): the OS auto-repeat delay and interval are a MACHINE setting -- one per
    identity, like device_hz -- not a per-run draw (two runs in one session repeated at 27 then 39 ms on
    949f5b1). Same seed => the same two traits; every run adds only +-2 / +-1 ms of timer jitter and its
    own release; a different seed has different traits."""
    a, b, c = _traits("ident-1"), _traits("ident-1"), _traits("ident-2")
    assert (a.repeat_first_ms, a.repeat_gap_ms) == (b.repeat_first_ms, b.repeat_gap_ms)
    assert (a.repeat_first_ms, a.repeat_gap_ms) != (c.repeat_first_ms, c.repeat_gap_ms)
    assert 252 <= a.repeat_first_ms <= 498 and 26 <= a.repeat_gap_ms <= 39
    plans = [K.plan_repeat(a, random.random) for _ in range(60)]   # unseeded: a run's own draws
    assert all(abs(p.first_ms - a.repeat_first_ms) <= 2.0 and abs(p.gap_ms - a.repeat_gap_ms) <= 1.0 for p in plans)
    assert max(p.gap_ms for p in plans) - min(p.gap_ms for p in plans) <= 2.0
    assert len({p.release_ms for p in plans}) > 50 and len({p.first_ms for p in plans}) > 50
    # the traits sit at the end of the KEYS stream: every earlier trait is what 949f5b1 drew
    assert dataclasses.asdict(a)["wpm"] == dataclasses.asdict(b)["wpm"]


SAME_FINGER = ["ed", "de", "ju", "ki", "lo", "fr", "sw", "ol"]
ALTERNATE = ["en", "th", "ha", "so", "al", "ka", "fj", "dk"]


def test_same_finger_bigrams_are_slower_than_alternating_ones():
    # keydown-to-keydown (flight + the persona's hold), as a page measures it
    ratios = []
    for p in ("ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"):
        tr, per, rng = _traits(p), make_persona(p), mulberry32(5)
        sf = [K.flight_ms(tr, a, b, rng) + key_dwell(per, rng) for a, b in SAME_FINGER for _ in range(60)]
        al = [K.flight_ms(tr, a, b, rng) + key_dwell(per, rng) for a, b in ALTERNATE for _ in range(60)]
        ratios.append(statistics.median(sf) / statistics.median(al))
    assert 1.2 <= statistics.median(ratios) <= 1.8, ratios
    assert all(1.2 <= r <= 2.0 for r in ratios), ratios


def test_word_initial_and_punctuation_pauses():
    tr = _traits("ha-1")
    mid = statistics.median(K.flight_ms(tr, "a", "l", mulberry32(i)) for i in range(400))
    word = statistics.median(K.flight_ms(tr, " ", "l", mulberry32(i)) for i in range(400))
    punct = statistics.median(K.flight_ms(tr, ".", "l", mulberry32(i)) for i in range(400))
    assert word - mid > 40 and punct - mid > 80
    assert min(K.plan_gap(tr, "a", "b", mulberry32(i)) for i in range(400)) >= K.MIN_FLIGHT_MS


def test_tab_distance_reads_are_plain_functions():
    for src in (K.TAB_DISTANCE, K.BLANK_POINT, K.SELECTION_STATE):
        assert src.lstrip().startswith(("function", "(a) =>", "() =>"))
    assert "'%s'" not in K.TAB_DISTANCE and "a[href]" in K.TAB_DISTANCE


# SC 232 triage (10-16): the calibration of the flight base -- the drawn WPM is the realized speed on neutral English.
NEUTRAL_ENGLISH = ("most people read a page before they act on it: their eyes move over the heading, the first lines and the "
                   "buttons, and the pointer drifts a little while they do. a script that clicks the first control the moment "
                   "the page has loaded never reads anything.")


@pytest.mark.parametrize("seed", ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6", "user-7423"])
def test_the_drawn_wpm_is_the_realized_speed_on_neutral_english(seed):
    # The WPM trait read 28-75 but typed 23-60: the finger factors, the flight noise and the pauses stacked on a base
    # that was already the whole per-character budget (ha-1 at 28.9 WPM planned 529 ms per key = 23 WPM; verifier B:
    # a 72-character field in 29 s). The base is calibrated for neutral English text (_keys.base_flight_ms), so the
    # planned keydown->keydown mean -- the gap plus the persona's own dwell -- is 60000 / (wpm x 5) within 10 %.
    p = make_persona(seed)
    t = K.typing_traits(p.seed)
    rng = mulberry32(7)
    iv = [K.plan_gap(t, a, b, rng) + key_dwell(p, rng) for _ in range(30) for a, b in zip(NEUTRAL_ENGLISH, NEUTRAL_ENGLISH[1:])]
    want = 60000.0 / (t.wpm * 5.0)
    assert abs(statistics.mean(iv) / want - 1.0) <= 0.10, (seed, round(t.wpm, 1), round(statistics.mean(iv)), round(want))
