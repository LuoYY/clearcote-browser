"""SC 232 lane G: the detector-emulator scores every humanize surface, and the score hook records them.

* ``score_action(kind, samples)`` for glide / drag / ambient / typed / wheel / hover: a deliberately bad
  synthetic trace of each kind scores < 0.5 (the EVAL.md "G score hook" row), a human-shaped one >= 0.7.
* The QWERTY finger map behind the same-finger / alternating-hand bigram ratio.
* ``CLEARCOTE_HUMANIZE_SCORE=1``: ``_pacing.walk`` / ``walk_async`` append one JSON line per walked path
  (actual dispatch times, not the plan) and warn below 0.7 naming the penalties; off by default; a
  failing tap never breaks the walk.
* sdk/testdata/humanize_motion_vectors.json ``scores``: the three SDKs score the same traces the same.
On origin/main ceb7983 every test here fails: score_action, finger_of and clearcote._trace do not exist.
"""
import asyncio
import json
import os

import pytest

from clearcote import _motionscore as ms
from clearcote import _pacing
from clearcote._motion import make_persona, mulberry32, plan_ambient, plan_move

VECTORS = os.path.join(os.path.dirname(__file__), "..", "..", "testdata", "humanize_motion_vectors.json")


def _vec():
    with open(VECTORS, encoding="utf-8") as f:
        return json.load(f)


def _case(kind, name):
    return next(c for c in _vec()["scores"] if c["kind"] == kind and c["name"] == name)


# ------------------------------------------------------------------ finger map
def test_finger_map_qwerty():
    assert ms.finger_of("a") == (0, 0)          # left pinky
    assert ms.finger_of("f") == (0, 3)          # left index
    assert ms.finger_of("G") == (0, 3)          # case-insensitive, index reaches g
    assert ms.finger_of("j") == (1, 3)          # right index
    assert ms.finger_of("p") == (1, 0)          # right pinky
    assert ms.finger_of(" ") == ms.finger_of("Space") == (2, 4)   # thumb
    assert ms.finger_of("Enter") is None and ms.finger_of(None) is None


# ------------------------------------------------------------------ bad < 0.5, human >= 0.7
BAD = [("glide", "straight"), ("typed", "robot"), ("wheel", "metronome"), ("hover", "none"), ("drag", "teleport")]
GOOD = [("glide", "planned"), ("typed", "human"), ("wheel", "notch-bursts"), ("wheel", "trackpad"),
        ("hover", "dwell"), ("drag", "hesitant"), ("ambient", "planned")]


@pytest.mark.parametrize("kind,name", BAD)
def test_bad_synthetic_trace_scores_below_half(kind, name):
    r = ms.score_action(kind, _case(kind, name)["samples"])
    assert r.score <= 0.5, (kind, name, r.score, r.flags)
    assert r.flags


@pytest.mark.parametrize("kind,name", GOOD)
def test_human_shaped_trace_scores_at_least_0_7(kind, name):
    r = ms.score_action(kind, _case(kind, name)["samples"])
    assert r.score >= 0.7, (kind, name, r.score, r.flags)


def test_typed_bigram_ratio_reads_same_finger_slower():
    f = ms.typed_features(_case("typed", "human")["samples"])
    assert f["same_finger_n"] >= 3 and f["alternating_n"] >= 3
    assert f["same_finger_ratio"] > 1.05


def test_wheel_eased_slices_flagged():
    # The pre-0.4x wheel: 39/28/20/10/3 px per event. Both notches and trackpad inertia are accepted, this is neither.
    r = ms.score_action("wheel", _case("wheel", "eased-slices")["samples"])
    assert "wheel-eased-slices" in r.flags


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        ms.score_action("teleport", [])


@pytest.mark.parametrize("case", _vec()["scores"], ids=lambda c: c["kind"] + "-" + c["name"])
def test_scores_match_shared_vectors(case):
    r = ms.score_action(case["kind"], case["samples"])
    assert r.score == pytest.approx(case["score"], abs=1e-9)
    assert [f.split("(")[0] for f in r.flags] == [f.split("(")[0] for f in case["flags"]]


# ------------------------------------------------------------------ score hook
class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)

    async def asleep(self, s):
        self.t += max(0.0, s)


def _lines(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


@pytest.fixture
def hook(tmp_path, monkeypatch):
    out = tmp_path / "score.jsonl"
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_SCORE", "1")
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_SCORE_FILE", str(out))
    monkeypatch.delenv("CLEARCOTE_NO_WARN", raising=False)
    from clearcote import _warnings
    monkeypatch.setattr(_warnings, "_seen_notes", set())
    return out


def test_walk_records_one_line_per_walked_path(hook):
    c = _Clock()
    steps = plan_move((100, 100), (800, 500), make_persona("ha-1"), target_w=40, rng=mulberry32(5))
    _pacing.walk(steps, lambda x, y: None, clock=c.now, sleep=c.sleep)
    _pacing.walk(plan_ambient((300, 300), {"width": 1180, "height": 760}, make_persona("ha-2"), 800, rng=mulberry32(2)),
                 lambda x, y: None, clock=c.now, sleep=c.sleep, kind="ambient")
    lines = _lines(hook)
    assert [ln["kind"] for ln in lines] == ["glide", "ambient"]
    first = lines[0]
    assert first["n"] == len(steps) and first["meta"]["sent"] == len(steps)
    assert 0.0 <= first["score"] <= 1.0 and isinstance(first["flags"], list)
    assert first["features"]["duration_ms"] > 0


def test_walk_records_actual_dispatch_times_not_the_plan(hook):
    # A 25 ms dispatch: the pacer skips samples, and the recorded trace is what went out.
    c = _Clock()

    def slow(x, y):
        c.t += 0.025

    steps = plan_move((100, 100), (800, 500), make_persona("ha-1"), target_w=40, rng=mulberry32(5))
    sent = _pacing.walk(steps, slow, clock=c.now, sleep=c.sleep)
    ln = _lines(hook)[0]
    assert sent < len(steps) and ln["n"] == sent == ln["meta"]["sent"]


def test_bad_walk_warns_naming_the_penalties(hook, capsys):
    c = _Clock()
    _pacing.walk([(100 + i * 5, 100, 8) for i in range(60)], lambda x, y: None, clock=c.now, sleep=c.sleep)
    ln = _lines(hook)[0]
    assert ln["score"] < 0.5
    err = capsys.readouterr().err
    assert "clearcote: warning: humanize glide scored" in err
    assert "no-corrective-submovement" in err and "perfectly-straight-path" in err


def test_async_walk_records(hook):
    c = _Clock()

    async def mv(x, y):
        return None

    steps = plan_move((100, 100), (600, 300), make_persona("ha-3"), target_w=30, rng=mulberry32(9))
    asyncio.run(_pacing.walk_async(steps, mv, clock=c.now, sleep=c.asleep))
    assert [ln["kind"] for ln in _lines(hook)] == ["glide"]


def test_hook_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CLEARCOTE_HUMANIZE_SCORE", raising=False)
    monkeypatch.chdir(tmp_path)
    c = _Clock()
    _pacing.walk([(1, 1, 8), (5, 5, 8)], lambda x, y: None, clock=c.now, sleep=c.sleep)
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_SCORE", "0")
    _pacing.walk([(1, 1, 8), (5, 5, 8)], lambda x, y: None, clock=c.now, sleep=c.sleep)
    assert os.listdir(tmp_path) == []


def test_default_file_is_cwd_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_SCORE", "1")
    monkeypatch.delenv("CLEARCOTE_HUMANIZE_SCORE_FILE", raising=False)
    monkeypatch.setenv("CLEARCOTE_NO_WARN", "1")
    monkeypatch.chdir(tmp_path)
    c = _Clock()
    _pacing.walk([(1, 1, 8), (50, 5, 8), (90, 9, 8)], lambda x, y: None, clock=c.now, sleep=c.sleep)
    assert len(_lines(tmp_path / "clearcote-humanize-score.jsonl")) == 1


def test_a_failing_tap_never_breaks_the_walk(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_SCORE", "1")
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_SCORE_FILE", str(tmp_path / "no-such-dir" / "x.jsonl"))
    c = _Clock()
    assert _pacing.walk([(1, 1, 8), (50, 5, 8)], lambda x, y: None, clock=c.now, sleep=c.sleep) == 2


def test_record_is_the_lane_tap(hook):
    from clearcote import _trace
    r = _trace.record("typed", _case("typed", "robot")["samples"], {"field": "#name"})
    assert r is not None and r.kind == "typed"
    ln = _lines(hook)[0]
    assert ln["kind"] == "typed" and ln["meta"] == {"field": "#name"} and ln["score"] < 0.5
    assert _trace.record("wheel", []) is None
