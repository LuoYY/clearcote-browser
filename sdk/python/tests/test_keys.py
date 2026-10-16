"""The keyboard planner (clearcote._keys) against the SHARED vectors in sdk/testdata/humanize_typing_vectors.json
-- the same file the Node (keys.test.ts) and .NET (KeysTests.cs) suites read -- plus the properties the
vectors cannot express. Lane D of SC 232; on 0.51.0 there was no planner (the module does not exist)."""
import dataclasses
import json
import os
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
    rp = K.plan_repeat(rng)
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
            rp = K.plan_repeat(rng)
            assert 250 <= rp.first_ms <= 500 and 25 <= rp.gap_ms <= 40 and 20 <= rp.release_ms <= 60
            assert 40 <= K.modifier_lead_ms(rng) <= 110 and 20 <= K.modifier_lag_ms(rng) <= 80


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
