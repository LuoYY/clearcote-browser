"""Humanized form input planning (clearcote._forms) against the SHARED vectors in
sdk/testdata/humanize_forms_vectors.json -- the same file the Node and .NET suites read, so the three
SDKs cannot drift apart -- plus property checks the vectors cannot express."""
import json
import os
import random

import pytest

from clearcote import _forms as F

VEC = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "testdata",
                                  "humanize_forms_vectors.json"), encoding="utf-8"))


def _ids(cases):
    return [str(i) for i in range(len(cases))]


@pytest.mark.parametrize("c", VEC["classify_field"], ids=_ids(VEC["classify_field"]))
def test_classify_field(c):
    assert F.classify_field(c["tag"], c["type"], c["ce"]) == c["want"]


@pytest.mark.parametrize("c", VEC["typos_allowed"], ids=_ids(VEC["typos_allowed"]))
def test_typos_allowed(c):
    assert F.typos_allowed(c["field"]) is c["want"]


@pytest.mark.parametrize("c", VEC["typo_eligible"], ids=_ids(VEC["typo_eligible"]))
def test_typo_eligible(c):
    assert F.typo_eligible(c["ch"]) is c["want"]


@pytest.mark.parametrize("c", VEC["formatted_equal"], ids=_ids(VEC["formatted_equal"]))
def test_formatted_equal(c):
    assert F.formatted_equal(c["got"], c["want"]) is c["result"]


def test_digit_neighbours_are_the_shared_map():
    assert F.NEARBY_DIGITS == VEC["nearby_digits"]


@pytest.mark.parametrize("c", VEC["typeahead_simulate"], ids=_ids(VEC["typeahead_simulate"]))
def test_typeahead_simulate(c):
    assert F.typeahead_simulate(c["labels"], c["start"], c["keys"]) == c["want"]


@pytest.mark.parametrize("c", VEC["plan_select"], ids=_ids(VEC["plan_select"]))
def test_plan_select(c):
    assert F.plan_select(c["labels"], c["current"], c["target"]) == c["want"]


@pytest.mark.parametrize("c", VEC["match_option"], ids=_ids(VEC["match_option"]))
def test_match_option(c):
    kw = {k: c[k] for k in ("value", "index", "label") if k in c}
    assert F.match_option(c["options"], **kw) == c["want"]


@pytest.mark.parametrize("c", VEC["range_sanitize"], ids=_ids(VEC["range_sanitize"]))
def test_range_sanitize(c):
    assert F.range_sanitize(c["value"], c["min"], c["max"], c["step"]) == pytest.approx(c["want"], rel=1e-12, abs=1e-9)


@pytest.mark.parametrize("c", VEC["range_value_ok"], ids=_ids(VEC["range_value_ok"]))
def test_range_value_ok(c):
    assert F.range_value_ok(c["value"], c["min"], c["max"], c["step"]) is c["want"]


def test_range_sanitize_always_returns_quickly_on_hostile_attributes():
    """A page chooses min/max/step: no combination may make the planner spin (it used to walk down in a
    loop that never ended once a step was smaller than the float spacing at the value)."""
    import time
    rng = random.Random(11)
    t0 = time.time()
    for _ in range(5000):
        hi = rng.choice([1e15, 123456789, 1e300, 5, 1e-300])
        st = rng.choice([1e-9, 1e-30, 1e-300, 0.5, 7])
        v = F.range_sanitize(rng.choice([hi, hi * 10, -hi, 0.0, 1e308]), 0, hi, st)
        assert v == v
    assert time.time() - t0 < 5


@pytest.mark.parametrize("c", VEC["range_point"], ids=_ids(VEC["range_point"]))
def test_range_point(c):
    assert F.range_point(c["box"], c["fraction"], c["rtl"]) == pytest.approx(tuple(c["want"]), abs=1e-9)


@pytest.mark.parametrize("c", VEC["range_nudge"], ids=_ids(VEC["range_nudge"]))
def test_range_nudge(c):
    assert list(F.range_nudge(c["current"], c["target"], c["step"])) == c["want"]


@pytest.mark.parametrize("c", VEC["plan_datetime"], ids=_ids(VEC["plan_datetime"]))
def test_plan_datetime(c):
    got = F.plan_datetime(c["kind"], c["segments"], c["value"], c["current"])
    assert (None if got is None else [list(a) for a in got]) == c["want"]


@pytest.mark.parametrize("c", VEC["datetime_equal"], ids=_ids(VEC["datetime_equal"]))
def test_datetime_equal(c):
    assert F.datetime_equal(c["kind"], c["a"], c["b"]) is c["want"]


@pytest.mark.parametrize("c", VEC["normalize_click_options"], ids=_ids(VEC["normalize_click_options"]))
def test_normalize_click_options(c):
    got = F.normalize_click_options(c["options"])
    assert got == c["want"]


@pytest.mark.parametrize("c", VEC["engine_path_ok"], ids=_ids(VEC["engine_path_ok"]))
def test_engine_path_ok(c):
    assert F.engine_path_ok(F.normalize_click_options(c["options"])) is c["want"]


# ------------------------------------------------------------------ properties beyond the vectors
def test_typos_never_touch_ineligible_characters():
    rng = random.Random(1)
    for text in ("ABC!@# ", "ÉÀü", "--__..", "\t\n"):
        for i in range(len(text)):
            for _ in range(200):
                assert F.plan_typo(text, i, rng, rate=1.0) is None


def test_typo_wrong_key_is_a_real_neighbour_of_the_same_class():
    rng = random.Random(2)
    text = "abcdefghijklmnopqrstuvwxyz0123456789"
    seen = set()
    for _ in range(3000):
        i = rng.randrange(len(text))
        tp = F.plan_typo(text, i, rng, rate=1.0)
        assert tp is not None
        seen.add(tp["kind"])
        if tp["kind"] == "sub":
            ch, w = text[i], tp["wrong"]
            assert w != ch
            assert w.isdigit() == ch.isdigit()
            assert w in (F.NEARBY_DIGITS[ch] if ch.isdigit() else F.NEARBY_LETTERS[ch])
            for c in text[i + 1:i + 1 + tp["late"]]:
                assert F.typo_eligible(c) or c == " "
            assert 0 <= tp["late"] <= 2
        if tp["kind"] == "swap":
            assert i + 1 < len(text) and text[i + 1] != text[i] and F.typo_eligible(text[i + 1])
    assert seen == {"sub", "double", "swap"}


def test_typo_rate_is_roughly_honoured():
    rng = random.Random(3)
    hits = sum(F.plan_typo("hello world", 1, rng, rate=0.02) is not None for _ in range(20000))
    assert 250 <= hits <= 550   # 2 % of 20000 = 400


def test_zero_rate_means_no_typos():
    rng = random.Random(4)
    assert all(F.plan_typo("abc", 0, rng, rate=0.0) is None for _ in range(1000))


def test_letters_and_digits_neighbour_maps_never_map_to_themselves():
    for k, v in list(F.NEARBY_LETTERS.items()) + list(F.NEARBY_DIGITS.items()):
        assert k not in v


def _blink_reference(labels, start, keys):
    """An independent restatement of type_ahead.cc, to fuzz the simulator against."""
    n = len(labels)
    if n == 0:
        return start
    buf, rep, cur = "", None, start
    for c in keys.lower():
        buf += c
        if c == rep:
            prefix, off = c, 1
        elif len(buf) > 1:
            prefix, off, rep = buf, 0, None
        else:
            prefix, off, rep = buf, 1, c
        i0 = ((cur if cur >= 0 else 0) + off) % n
        for k in range(n):
            j = (i0 + k) % n
            lab = labels[j]
            if lab and lab.lstrip().lower().startswith(prefix):
                cur = j
                break
    return cur


def test_typeahead_matches_an_independent_restatement():
    rng = random.Random(5)
    alphabet = "aabbcd"
    for _ in range(3000):
        labels = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 4))) or None
                  for _ in range(rng.randint(1, 8))]
        start = rng.randint(-1, len(labels) - 1)
        keys = "".join(rng.choice("abcd") for _ in range(rng.randint(1, 4)))
        assert F.typeahead_simulate(labels, start, keys) == _blink_reference(labels, start, keys)


def test_plan_select_always_lands_on_the_target():
    """Whatever the plan, simulating it (type-ahead, then arrows over enabled rows) must end on the
    target -- the property the live Enter relies on."""
    rng = random.Random(6)
    words = ["alpha", "alpine", "beta", "bet", "gamma", "Gamma", "delta", "  delta", "zeta", "", "eta",
             "eta", "Ünï", "item 1", "item 2", "x", "xx", "xxx"]
    for _ in range(4000):
        labels = [rng.choice(words) if rng.random() > 0.15 else None for _ in range(rng.randint(1, 12))]
        enabled = [i for i, l in enumerate(labels) if l is not None]
        if not enabled:
            continue
        target = rng.choice(enabled)
        # any current row: an enabled one, none (-1), or a DISABLED one (a page can preselect it)
        current = rng.choice(enabled + [-1] + [i for i, l in enumerate(labels) if l is None])
        plan = F.plan_select(labels, current, target)
        assert plan is not None
        land = F.typeahead_simulate(labels, current if current >= 0 else -1, plan["keys"]) if plan["keys"] \
            else (current if current >= 0 else enabled[0])
        # walk the arrows over enabled rows only
        pos = land
        step = 1 if plan["arrows"] > 0 else -1
        for _ in range(abs(plan["arrows"])):
            pos += step
            while labels[pos] is None:
                pos += step
        assert pos == target, (labels, current, target, plan)
        assert all(ch in F.TYPEABLE for ch in plan["keys"])


def test_plan_select_is_never_worse_than_arrows_alone():
    rng = random.Random(7)
    for _ in range(2000):
        labels = ["opt%d" % rng.randint(0, 30) for _ in range(rng.randint(2, 30))]
        cur, tgt = rng.randrange(len(labels)), rng.randrange(len(labels))
        plan = F.plan_select(labels, cur, tgt)
        assert len(plan["keys"]) + abs(plan["arrows"]) <= abs(tgt - cur)


def test_range_sanitize_lands_on_the_step_grid_inside_the_bounds():
    rng = random.Random(8)
    for _ in range(3000):
        lo = rng.choice([0, -10, 3, 0.5])
        hi = lo + rng.choice([1, 7, 100, 250.5])
        step = rng.choice([1, 2, 5, 0.1, 0.25, 7])
        v = F.range_sanitize(rng.uniform(lo - 20, hi + 20), lo, hi, step)
        assert lo - 1e-9 <= v <= hi + 1e-9
        k = (v - lo) / step
        assert abs(k - round(k)) < 1e-6


def test_range_point_stays_on_the_track():
    rng = random.Random(9)
    for _ in range(1000):
        box = {"x": rng.uniform(0, 500), "y": rng.uniform(0, 500), "width": rng.uniform(1, 600), "height": 16}
        f = rng.random()
        x, y = F.range_point(box, f, rng.random() < 0.5)
        assert box["x"] <= x <= box["x"] + box["width"]
        assert y == box["y"] + 8


def test_every_datetime_plan_types_only_digits_and_ampm_letters():
    kinds = {"date": ["month-field", "day-field", "year-field"], "time": ["hour-field", "minute-field", "ampm-field"],
             "datetime-local": ["month-field", "day-field", "year-field", "hour-field", "minute-field", "ampm-field"],
             "week": ["week-field", "year-field"]}
    values = {"date": "2031-02-28", "time": "23:59", "datetime-local": "1999-12-31T00:00", "week": "2020-W01"}
    for k, segs in kinds.items():
        plan = F.plan_datetime(k, segs, values[k])
        assert plan
        for a in plan:
            if a[0] == "type":
                assert a[1].isdigit() or a[1] in ("a", "p")
            else:
                assert a[1] in ("ArrowRight", "ArrowUp", "ArrowDown")


def test_segment_names_and_nodes_from_a_pierced_describe_tree():
    def el(pseudo=None, bid=None, children=(), shadow=()):
        n = {"nodeName": "DIV", "attributes": ["pseudo", pseudo] if pseudo else [], "backendNodeId": bid}
        if children:
            n["children"] = list(children)
        if shadow:
            n["shadowRoots"] = list(shadow)
        return n
    tree = el(bid=1, shadow=[el(bid=2, children=[
        el("-webkit-datetime-edit", 3, children=[
            el("-webkit-datetime-edit-month-field", 4), el("-webkit-datetime-edit-text", 5),
            el("-webkit-datetime-edit-day-field", 6), el("-webkit-datetime-edit-text", 7),
            el("-webkit-datetime-edit-year-field", 8)]),
        el("-webkit-calendar-picker-indicator", 9)])])
    assert F.segment_names_from_tree(tree) == ["month-field", "day-field", "year-field"]
    assert F.segment_nodes_from_tree(tree) == [("month-field", 4), ("day-field", 6), ("year-field", 8)]


# ------------------------------------------------------------------- maxlength + pre-press pause
@pytest.mark.parametrize("c", VEC["mask_kept"], ids=_ids(VEC["mask_kept"]))
def test_mask_kept(c):
    assert F.mask_kept(c["got"], c["want"], c["maxlength"], c["tag"], c["type"]) is c["result"]


@pytest.mark.parametrize("c", VEC["fit_maxlength"], ids=_ids(VEC["fit_maxlength"]))
def test_fit_maxlength(c):
    assert F.fit_maxlength(c["value"], c["maxlength"], c["tag"], c["type"]) == c["want"]


@pytest.mark.parametrize("c", VEC["pre_press_center"], ids=_ids(VEC["pre_press_center"]))
def test_pre_press_center(c):
    assert F.pre_press_center(c["seed"]) == pytest.approx(c["center"], abs=1e-6)


@pytest.mark.parametrize("c", VEC["pre_press_ms"], ids=_ids(VEC["pre_press_ms"]))
def test_pre_press_ms(c):
    from clearcote._motion import mulberry32
    r = mulberry32(c["rng_seed"])
    assert [F.pre_press_ms(c["seed"], r) for _ in c["ms"]] == pytest.approx(c["ms"], abs=1e-6)


def test_pre_press_pause_is_40_to_250_ms_right_skewed_and_per_identity():
    from clearcote._motion import hash_seed, mulberry32
    centres = set()
    for name in ("ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"):
        seed = hash_seed(name)
        r = mulberry32(seed)
        xs = sorted(F.pre_press_ms(seed, r) for _ in range(4000))
        assert 40.0 <= xs[0] and xs[-1] <= 250.0
        med, mean = xs[len(xs) // 2], sum(xs) / len(xs)
        assert mean > med, "right-skewed: the long tail is on the slow side"
        assert sum(1 for x in xs if x == 40.0 or x == 250.0) <= 4, "no pile-up at the bounds"
        assert 70.0 <= F.pre_press_center(seed) <= 135.0
        centres.add(round(F.pre_press_center(seed), 3))
    assert len(centres) == 6, "each identity has its own typical pause"


def test_fit_maxlength_matches_what_insert_text_leaves():
    # Playwright's fill on an empty field leaves exactly this prefix; typing only it is the parity.
    rnd = random.Random(5)
    for _ in range(500):
        n = rnd.randint(-1, 12)
        v = "".join(rnd.choice("ab1\U0001F600") for _ in range(rnd.randint(0, 10)))
        got = F.fit_maxlength(v, n, "input", "text")
        units = sum(2 if ord(ch) > 0xFFFF else 1 for ch in got)
        assert v.startswith(got)
        assert n < 0 and got == v or units <= n
        if n >= 0 and got != v:
            nxt = v[len(got)]
            assert units + (2 if ord(nxt) > 0xFFFF else 1) > n, "the longest prefix that fits"
