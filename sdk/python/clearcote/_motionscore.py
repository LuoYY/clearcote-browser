"""Detector-emulator (Python) — extracts the feature vector a behavioral collector would compute from a
pointer stream and scores how human it looks. Faithful port of the Node SDK's ``src/motionscore.ts``.

This is a VALIDATION tool: it lets us prove, offline and deterministically, that the motion core
(``_motion.py``) improves the features that matter, instead of eyeballing trajectories or burning scarce
clean-IP end-to-end runs. It is intentionally an APPROXIMATION of what real collectors do (their exact
models are private); treat the score as a RELATIVE signal (old vs new), not an absolute pass/fail.

PURE (no Playwright, no I/O, standard library only). A ``Sample`` is a dict ``{"x","y","t","buttons"}`` or
a tuple ``(x, y, t[, buttons])``; ``buttons`` defaults to 0. ``steps_to_samples`` emits the dict form and
consumes the ``(x, y, sleep_ms)`` steps produced by ``_motion.plan_move``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple, Union

# A Sample is a dict {"x","y","t","buttons"?} or a tuple (x, y, t[, buttons]).
Sample = Union[dict, Sequence[float]]


def _sx(s: Sample) -> float:
    return s["x"] if isinstance(s, dict) else s[0]


def _sy(s: Sample) -> float:
    return s["y"] if isinstance(s, dict) else s[1]


def _st(s: Sample) -> float:
    """time in ms (0 when absent), mirroring the guarded ``_sb`` accessor so a 2-tuple (x, y) — or a
    dict without ``t`` — never raises IndexError/KeyError in ``extract_features``."""
    if isinstance(s, dict):
        return s.get("t") or 0
    return s[2] if len(s) > 2 else 0


def _sb(s: Sample) -> int:
    """buttons bitmask (0 when absent), matching TS ``s.buttons ?? 0``."""
    if isinstance(s, dict):
        return int(s.get("buttons") or 0)
    return int(s[3]) if len(s) > 3 else 0


@dataclass
class MotionFeatures:
    """The feature vector a behavioral collector would compute (see motionscore.ts for field semantics)."""

    n: int
    duration_ms: float
    #: Fraction of the movement at which peak speed occurs. Human ~0.3-0.45; pure ease-out ~0.15.
    peak_vel_frac: float
    #: Time-weighted skewness of the speed profile. Human > 0 (long decelerating homing tail); ~0 symmetric.
    vel_skew: float
    #: Corrective submovements = prominent interior speed minima (human 1-3; a single curve = 0).
    submovements: int
    #: Net displacement / path length (1 = perfectly straight).
    straightness: float
    #: Diagnostic only (NOT scored): residual of the whole speed profile vs a SINGLE min-jerk bell.
    min_jerk_residual: float
    dt_mean_ms: float
    dt_std_ms: float
    #: True when inter-sample dt looks i.i.d.-uniform (no cadence mode) — a tell.
    dt_uniform: bool
    #: Lag-1 autocorrelation of the sub-pixel jitter residual. White noise ~0; colored motor noise ~0.2-0.8.
    jitter_autocorr: float
    #: 2/3 power-law fit quality (diagnostic only).
    power_law_r: float
    #: Drag endpoint timing (only when a held-button segment is present), else None.
    grab_ms: Optional[float]
    release_ms: Optional[float]


def _speeds(s: Sequence[Sample]) -> Tuple[List[float], List[float]]:
    v: List[float] = []
    t_mid: List[float] = []
    for i in range(1, len(s)):
        dt = max(1e-3, _st(s[i]) - _st(s[i - 1]))
        v.append(math.hypot(_sx(s[i]) - _sx(s[i - 1]), _sy(s[i]) - _sy(s[i - 1])) / dt)
        t_mid.append((_st(s[i]) + _st(s[i - 1])) / 2)
    return v, t_mid


def _smooth(a: Sequence[float], w: int = 3) -> List[float]:
    """Smooth a series with a small moving average (window w)."""
    out: List[float] = []
    for i in range(len(a)):
        acc = 0.0
        c = 0
        for j in range(max(0, i - w), min(len(a) - 1, i + w) + 1):
            acc += a[j]
            c += 1
        out.append(acc / c)
    return out


def _mean(a: Sequence[float]) -> float:
    return sum(a) / len(a) if a else 0.0


def _std(a: Sequence[float]) -> float:
    if len(a) < 2:
        return 0.0
    m = _mean(a)
    return math.sqrt(_mean([(x - m) ** 2 for x in a]))


def extract_features(s: Sequence[Sample]) -> MotionFeatures:
    n = len(s)
    duration_ms = _st(s[n - 1]) - _st(s[0]) if n > 1 else 0.0
    v, t_mid = _speeds(s)
    sv = _smooth(v, 2)

    # peak velocity position
    peak_idx = 0
    for i in range(1, len(sv)):
        if sv[i] > sv[peak_idx]:
            peak_idx = i
    peak_vel_frac = (t_mid[peak_idx] - _st(s[0])) / duration_ms if duration_ms > 0 else 0.0

    # velocity skewness (positive for a human's fast-rise/slow-decay profile; ~0 for a symmetric curve)
    vmean = _mean(sv)
    vstd = _std(sv) or 1e-9
    vel_skew = _mean([((x - vmean) / vstd) ** 3 for x in sv])

    # corrective submovements: prominent interior local minima below 0.5*vmax, a true valley between two
    # higher shoulders, with a minimum index separation so one correction isn't counted several times.
    vmax = max([*sv, 1e-9])
    submovements = 0
    last_min = -10
    for i in range(2, len(sv) - 2):
        valley = sv[i] < sv[i - 1] and sv[i] <= sv[i + 1] and sv[i] < sv[i - 2] and sv[i] < sv[i + 2]
        if valley and sv[i] < 0.5 * vmax and i - last_min >= 3:
            submovements += 1
            last_min = i

    # straightness
    path_len = 0.0
    for i in range(1, n):
        path_len += math.hypot(_sx(s[i]) - _sx(s[i - 1]), _sy(s[i]) - _sy(s[i - 1]))
    net_disp = math.hypot(_sx(s[n - 1]) - _sx(s[0]), _sy(s[n - 1]) - _sy(s[0])) if n > 1 else 0.0
    straightness = net_disp / path_len if path_len > 1e-6 else 1.0

    # min-jerk residual: compare the normalized speed profile to 30*tau^2*(1-tau)^2
    min_jerk_residual = 1.0
    if len(sv) >= 4 and duration_ms > 0:
        area = 0.0
        for i in range(len(sv)):
            area += sv[i] * (0 if i == 0 else t_mid[i] - t_mid[i - 1])
        area = area or 1.0
        res = 0.0
        norm = 0.0
        for i in range(len(sv)):
            tau = (t_mid[i] - _st(s[0])) / duration_ms
            ideal = 30 * tau * tau * (1 - tau) * (1 - tau)  # integral over tau = 1
            obs = (sv[i] * duration_ms) / area  # normalize so integral obs d(tau) ~ 1
            res += (obs - ideal) ** 2
            norm += ideal ** 2
        min_jerk_residual = math.sqrt(res / norm) if norm > 0 else 1.0

    # dt stats + uniformity: a real device stream has a sharp cadence MODE (most samples near the median)
    # with a heavy tail of pauses; a rand(a,b) band has NO mode, so few samples cluster near the median.
    dts: List[float] = []
    for i in range(1, n):
        dts.append(_st(s[i]) - _st(s[i - 1]))
    dt_mean_ms = _mean(dts)
    dt_std_ms = _std(dts)
    sorted_dts = sorted(dts)
    median = sorted_dts[len(sorted_dts) // 2] if sorted_dts else 0.0
    core_frac = (
        len([d for d in dts if 0.8 * median <= d <= 1.2 * median]) / len(dts) if median > 0 else 0.0
    )
    dt_uniform = core_frac < 0.5

    # jitter color: lag-1 autocorrelation of the sub-pixel residual (position minus a smoothed trend).
    jitter_autocorr = 0.0
    if n >= 8:
        xs = [_sx(p) for p in s]
        ys = [_sy(p) for p in s]
        sx = _smooth(xs, 3)
        sy = _smooth(ys, 3)
        rx = [xs[i] - sx[i] for i in range(len(xs))]
        ry = [ys[i] - sy[i] for i in range(len(ys))]

        def _ac(r: List[float]) -> float:
            num = 0.0
            den = 0.0
            for i in range(1, len(r)):
                num += r[i] * r[i - 1]
            for i in range(len(r)):
                den += r[i] * r[i]
            return num / den if den > 1e-9 else 0.0

        jitter_autocorr = (_ac(rx) + _ac(ry)) / 2

    # 2/3 power law (diagnostic): correlation between log speed and -(1/3) log curvature
    power_law_r = 0.0
    if n >= 6:
        lv: List[float] = []
        lc: List[float] = []
        for i in range(1, n - 1):
            ax = _sx(s[i]) - _sx(s[i - 1])
            ay = _sy(s[i]) - _sy(s[i - 1])
            bx = _sx(s[i + 1]) - _sx(s[i])
            by = _sy(s[i + 1]) - _sy(s[i])
            cross = abs(ax * by - ay * bx)
            seg_a = math.hypot(ax, ay)
            seg_b = math.hypot(bx, by)
            curv = cross / (math.pow(seg_a * seg_b * math.hypot(bx + ax, by + ay), 1) + 1e-9)
            sp = (seg_a + seg_b) / 2 / max(1e-3, (_st(s[i + 1]) - _st(s[i - 1])) / 2)
            if curv > 1e-6 and sp > 1e-6:
                lv.append(math.log(sp))
                lc.append(math.log(curv))
        if len(lv) >= 4:
            mv = _mean(lv)
            mc = _mean(lc)
            num = 0.0
            dv = 0.0
            dc = 0.0
            for i in range(len(lv)):
                num += (lv[i] - mv) * (lc[i] - mc)
                dv += (lv[i] - mv) ** 2
                dc += (lc[i] - mc) ** 2
            power_law_r = num / math.sqrt(dv * dc) if (dv > 0 and dc > 0) else 0.0

    # drag endpoint timing: within the buttons==1 (held) segment. The `up` is the first sample AFTER the
    # held span (buttons back to 0); grab = down->first-movement, release = last-movement->up.
    grab_ms: Optional[float] = None
    release_ms: Optional[float] = None
    held_idx: List[int] = []
    for i in range(n):
        if _sb(s[i]) & 1:
            held_idx.append(i)
    if held_idx:
        d0 = held_idx[0]
        first_move_t = _st(s[d0])
        for i in held_idx:
            if math.hypot(_sx(s[i]) - _sx(s[d0]), _sy(s[i]) - _sy(s[d0])) > 1:
                first_move_t = _st(s[i])
                break
        grab_ms = first_move_t - _st(s[d0])
        last_move_idx = d0
        for k in range(1, len(held_idx)):
            i = held_idx[k]
            j = held_idx[k - 1]
            if math.hypot(_sx(s[i]) - _sx(s[j]), _sy(s[i]) - _sy(s[j])) > 1:
                last_move_idx = i
        last_held = held_idx[-1]
        up_t = _st(s[last_held + 1]) if last_held + 1 < n else _st(s[last_held])
        release_ms = up_t - _st(s[last_move_idx])

    return MotionFeatures(
        n=n,
        duration_ms=duration_ms,
        peak_vel_frac=peak_vel_frac,
        vel_skew=vel_skew,
        submovements=submovements,
        straightness=straightness,
        min_jerk_residual=min_jerk_residual,
        dt_mean_ms=dt_mean_ms,
        dt_std_ms=dt_std_ms,
        dt_uniform=dt_uniform,
        jitter_autocorr=jitter_autocorr,
        power_law_r=power_law_r,
        grab_ms=grab_ms,
        release_ms=release_ms,
    )


@dataclass
class MotionScore:
    score: float
    flags: List[str]
    features: MotionFeatures


def score_motion(s: Sequence[Sample], drag: bool = False) -> MotionScore:
    """Score 0..1 (higher = more human) with human-readable flags for the tells that fire."""
    f = extract_features(s)
    flags: List[str] = []
    score = 1.0

    def penalize(cond: bool, amt: float, flag: str) -> None:
        nonlocal score
        if cond:
            score -= amt
            flags.append(flag)

    # peak velocity should be asymmetric-human: penalize pure ease-out (very early) AND a too-symmetric
    # single-curve peak sitting right at the midpoint.
    penalize(f.peak_vel_frac < 0.15, 0.15, f"peak-velocity-ease-out({f.peak_vel_frac:.2f})")
    penalize(f.peak_vel_frac > 0.48, 0.12, f"peak-velocity-too-symmetric({f.peak_vel_frac:.2f})")
    # velocity profile should have the human right-skew (long homing tail), not a symmetric bell
    penalize(f.vel_skew < 0.1, 0.12, f"velocity-symmetric(skew={f.vel_skew:.2f})")
    # at least one corrective submovement (Fitts homing); a single analytic curve has none
    penalize(f.submovements < 1, 0.2, "no-corrective-submovement")
    # ...but not TOO many: an over-corrected, velocity-reversal-heavy reach (many interior speed minima)
    # is as non-human as a single smooth curve — real homing settles in 1-3 corrections.
    penalize(f.submovements > 4, 0.12, f"too-many-submovements({f.submovements})")
    # dt should NOT look i.i.d.-uniform (real device sampling has a cadence mode)
    penalize(f.dt_uniform, 0.15, "uniform-dt-cadence")
    # colored (not white) sub-pixel noise
    penalize(f.jitter_autocorr < 0.1, 0.1, f"white-noise-jitter({f.jitter_autocorr:.2f})")
    # path shouldn't be a ruler-straight line
    penalize(f.straightness > 0.9995, 0.08, "perfectly-straight-path")

    if drag:
        penalize(f.grab_ms is not None and f.grab_ms < 60, 0.2, f"grab-hesitation-too-short({f.grab_ms}ms)")
        penalize(
            f.release_ms is not None and f.release_ms < 40, 0.2, f"release-delay-too-short({f.release_ms}ms)"
        )

    return MotionScore(score=max(0.0, score), flags=flags, features=f)


def steps_to_samples(
    steps: Sequence[Tuple[float, float, float]],
    buttons_during: Optional[dict] = None,
) -> List[dict]:
    """Turn a Step list (absolute coords + post-step sleep) into timestamped Samples for scoring.

    A ``step`` is an ``(x, y, sleep_ms)`` tuple (matches ``_motion.plan_move`` output). ``buttons_during``
    marks the held-button (drag) span ``[start, end)`` with buttons=1 (dict ``{"start": i, "end": j}``).
    """
    out: List[dict] = []
    t = 0.0
    for i in range(len(steps)):
        held = (
            1
            if buttons_during is not None
            and buttons_during["start"] <= i < buttons_during["end"]
            else 0
        )
        x, y, sleep_ms = steps[i][0], steps[i][1], steps[i][2]
        out.append({"x": x, "y": y, "t": t, "buttons": held})
        t += sleep_ms
    return out


# =====================================================================================================
# SC 232 lane G: scorers for the other humanize surfaces (typed runs, wheel streams, drags, hover
# dwell) and one entry point, ``score_action(kind, samples)``. Same contract as ``score_motion``:
# pure, deterministic, standard library only, a RELATIVE signal. Ported to Node (motionscore.ts) and
# .NET (MotionScore.cs); sdk/testdata/humanize_motion_vectors.json ``scores`` pins all three.
#
# Sample shapes per kind (dicts; missing fields read as 0):
#   glide / ambient : {"x","y","t"[,"buttons"]}           t in ms
#   drag            : {"x","y","t","buttons"}              buttons&1 marks the held span
#   typed           : {"key","down","up"}                  down/up in ms; "code" optional
#   wheel           : {"t","dy"[,"dx","mode"]}             dy/dx in px (mode 0) or lines (mode 1)
#   hover           : {"x","y","t"}                        samples from ARRIVAL on (first = arrival)
# =====================================================================================================

# QWERTY finger map: key -> (hand, finger). hand: 0 left, 1 right; finger: 0 pinky .. 3 index.
# Space is the thumb: (2, 4) so it is neither same-finger nor alternating with anything.
_FINGER: dict = {}
for _hand, _rows in ((0, ("qaz1", "wsx2", "edc3", "rfvtgb45")), (1, ("yhnujm67", "ik,8", "ol.9", "p;/[]'0-="))):
    for _f, _keys in enumerate(_rows):
        for _k in _keys:
            _FINGER[_k] = (_hand, _f if _hand == 0 else 3 - _f)
_FINGER[" "] = (2, 4)


def finger_of(key: Any) -> Optional[Tuple[int, int]]:
    """(hand, finger) for a typed key on a QWERTY layout, or None for keys off the letter block."""
    if not isinstance(key, str) or not key:
        return None
    k = key.lower()
    if k == "space":
        k = " "
    if len(k) != 1:
        return None
    return _FINGER.get(k)


def _cv(a: Sequence[float]) -> float:
    m = _mean(a)
    return _std(a) / m if m > 1e-9 else 0.0


def _median(a: Sequence[float]) -> float:
    s = sorted(a)
    if not s:
        return 0.0
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def _g(s: Any, k: str, default: float = 0.0) -> float:
    v = s.get(k) if isinstance(s, dict) else None
    return float(v) if v is not None else default


@dataclass
class ActionScore:
    """``score`` 0..1 (higher = more human), ``flags`` = the penalties that fired, ``features`` = what
    they were computed from (plain dict so it serialises as-is to the score file)."""

    kind: str
    score: float
    flags: List[str]
    features: dict


def _done(kind: str, score: float, flags: List[str], feats: dict) -> ActionScore:
    return ActionScore(kind=kind, score=max(0.0, round(score, 4)), flags=flags, features=feats)


def typed_features(s: Sequence[Any]) -> dict:
    """Key-gap CV, dwell band, rollover rate and the same-finger / alternating-hand bigram ratio."""
    ev = sorted(({"key": x.get("key"), "down": _g(x, "down"), "up": _g(x, "up", _g(x, "down"))}
                 for x in s if isinstance(x, dict)), key=lambda e: e["down"])
    n = len(ev)
    gaps = [ev[i]["down"] - ev[i - 1]["down"] for i in range(1, n)]
    dwells = [max(0.0, e["up"] - e["down"]) for e in ev]
    rollover = sum(1 for i in range(1, n) if ev[i]["down"] < ev[i - 1]["up"])
    same: List[float] = []
    alt: List[float] = []
    for i in range(1, n):
        a, b = finger_of(ev[i - 1]["key"]), finger_of(ev[i]["key"])
        if a is None or b is None or a[0] == 2 or b[0] == 2:
            continue
        if a == b:
            same.append(gaps[i - 1])
        elif a[0] != b[0]:
            alt.append(gaps[i - 1])
    ratio = (_median(same) / _median(alt)) if (len(same) >= 3 and len(alt) >= 3 and _median(alt) > 0) else None
    return {
        "n": n,
        "gap_mean_ms": _mean(gaps),
        "gap_cv": _cv(gaps) if len(gaps) >= 2 else 0.0,
        "gap_min_ms": min(gaps) if gaps else 0.0,
        "dwell_mean_ms": _mean(dwells),
        "dwell_cv": _cv(dwells) if len(dwells) >= 2 else 0.0,
        "dwell_band_frac": (len([d for d in dwells if 30 <= d <= 200]) / len(dwells)) if dwells else 0.0,
        "rollover_rate": rollover / (n - 1) if n > 1 else 0.0,
        "same_finger_n": len(same),
        "alternating_n": len(alt),
        "same_finger_ratio": ratio,
    }


def score_typed(s: Sequence[Any]) -> ActionScore:
    """A typed run: Gaussian-ish gaps (CV 0.25-1.0), dwell 30-200 ms and varied, some rollover on
    longer runs, same-finger bigrams slower than alternating-hand ones."""
    f = typed_features(s)
    flags: List[str] = []
    score = 1.0

    def pen(cond: bool, amt: float, flag: str) -> None:
        nonlocal score
        if cond:
            score -= amt
            flags.append(flag)

    n = f["n"]
    pen(n >= 3 and f["gap_cv"] < 0.15, 0.25, "key-gap-too-regular(cv=%.2f)" % f["gap_cv"])
    pen(n >= 3 and f["gap_cv"] > 1.6, 0.10, "key-gap-erratic(cv=%.2f)" % f["gap_cv"])
    pen(n >= 3 and f["gap_min_ms"] < 8, 0.15, "key-gap-near-zero(%.0fms)" % f["gap_min_ms"])
    pen(n >= 2 and f["dwell_band_frac"] < 0.7, 0.15, "dwell-out-of-band(%.2f in 30-200ms)" % f["dwell_band_frac"])
    pen(n >= 3 and f["dwell_cv"] < 0.08, 0.15, "dwell-constant(cv=%.2f)" % f["dwell_cv"])
    pen(f["same_finger_ratio"] is not None and f["same_finger_ratio"] < 1.05, 0.15,
        "no-bigram-structure(same/alt=%.2f)" % (f["same_finger_ratio"] or 0.0))
    pen(n >= 15 and f["rollover_rate"] == 0.0, 0.10, "no-rollover")
    pen(n >= 5 and f["rollover_rate"] > 0.6, 0.10, "too-much-rollover(%.2f)" % f["rollover_rate"])
    return _done("typed", score, flags, f)


_NOTCH_PX = (40.0, 100.0, 120.0)


def wheel_features(s: Sequence[Any]) -> dict:
    """Notch cadence (bursts of 2-5 notches 15-60 ms apart, flick resets >= 90 ms) or trackpad pixel
    inertia (2-40 px at 60-120 Hz with a decaying tail). Both are accepted; mixtures are not."""
    ev = sorted(({"t": _g(x, "t"), "dy": _g(x, "dy"), "dx": _g(x, "dx"), "mode": int(_g(x, "mode"))}
                 for x in s if isinstance(x, dict)), key=lambda e: e["t"])
    n = len(ev)
    mags = [abs(e["dy"]) if e["dy"] else abs(e["dx"]) for e in ev]
    gaps = [ev[i]["t"] - ev[i - 1]["t"] for i in range(1, n)]
    distinct = len({round(m, 3) for m in mags})
    line_mode = n > 0 and all(e["mode"] == 1 for e in ev)
    notch = n > 0 and (line_mode or (distinct == 1 and mags[0] in _NOTCH_PX))
    bursts: List[int] = []
    intra: List[float] = []
    if n:
        size = 1
        for g in gaps:
            if g >= 90:
                bursts.append(size)
                size = 1
            else:
                intra.append(g)
                size += 1
        bursts.append(size)
    # inertia: over the last third, fraction of consecutive steps that do not grow
    tail = mags[max(1, (2 * n) // 3):] if n >= 6 else []
    tail_dec = (sum(1 for i in range(1, len(tail)) if tail[i] <= tail[i - 1]) / (len(tail) - 1)) if len(tail) >= 3 else 0.0
    return {
        "n": n,
        "notch": notch,
        "line_mode": line_mode,
        "distinct_deltas": distinct,
        "delta_median_px": _median(mags),
        "delta_max_px": max(mags) if mags else 0.0,
        "gap_median_ms": _median(gaps),
        "gap_cv": _cv(gaps) if len(gaps) >= 2 else 0.0,
        "intra_burst_median_ms": _median(intra),
        "burst_sizes": bursts,
        "flick_resets": max(0, len(bursts) - 1),
        "rate_hz": (1000.0 / _median(gaps)) if gaps and _median(gaps) > 0 else 0.0,
        "tail_decreasing_frac": tail_dec,
    }


def score_wheel(s: Sequence[Any]) -> ActionScore:
    f = wheel_features(s)
    flags: List[str] = []
    score = 1.0

    def pen(cond: bool, amt: float, flag: str) -> None:
        nonlocal score
        if cond:
            score -= amt
            flags.append(flag)

    n = f["n"]
    if n < 2:
        return _done("wheel", score, flags, f)
    if f["notch"]:
        pen(n >= 4 and f["gap_cv"] < 0.10, 0.25, "wheel-metronome(cv=%.2f)" % f["gap_cv"])
        pen(n >= 8 and f["flick_resets"] == 0, 0.15, "no-flick-bursts")
        pen(bool(f["burst_sizes"]) and max(f["burst_sizes"]) > 7, 0.10, "burst-too-long(%d)" % max(f["burst_sizes"]))
        im = f["intra_burst_median_ms"]
        pen(im > 0 and not (10 <= im <= 80), 0.15, "wheel-cadence-off(%.0fms)" % im)
        pen(f["gap_median_ms"] < 4, 0.25, "wheel-same-tick")
    else:
        pen(f["distinct_deltas"] <= 1, 0.20, "trackpad-constant-delta")
        pen(f["delta_max_px"] > 60, 0.15, "trackpad-delta-too-large(%.0fpx)" % f["delta_max_px"])
        pen(n >= 6 and not (45 <= f["rate_hz"] <= 150), 0.15, "trackpad-rate-off(%.0fHz)" % f["rate_hz"])
        pen(n >= 6 and f["tail_decreasing_frac"] < 0.6, 0.20, "no-inertia-tail(%.2f)" % f["tail_decreasing_frac"])
        pen(n <= 8 and f["distinct_deltas"] == n and f["gap_median_ms"] >= 25, 0.25, "wheel-eased-slices")
    return _done("wheel", score, flags, f)


def drag_features(s: Sequence[Sample]) -> dict:
    """Start hesitation (ms still, < 8 px, after the press), held-path straightness, settle before
    release; on top of ``extract_features`` (grab / release timing)."""
    held = [i for i in range(len(s)) if _sb(s[i]) & 1]
    if not held:
        return {"held_n": 0}
    d0 = held[0]
    px, py, pt = _sx(s[d0]), _sy(s[d0]), _st(s[d0])
    hes = None
    for i in held:
        if math.hypot(_sx(s[i]) - px, _sy(s[i]) - py) >= 8:
            hes = _st(s[i]) - pt
            break
    if hes is None:
        hes = _st(s[held[-1]]) - pt
    pts = [s[i] for i in held]
    path = sum(math.hypot(_sx(pts[i]) - _sx(pts[i - 1]), _sy(pts[i]) - _sy(pts[i - 1])) for i in range(1, len(pts)))
    net = math.hypot(_sx(pts[-1]) - px, _sy(pts[-1]) - py)
    last_sig = held[0]
    for k in range(1, len(held)):
        if math.hypot(_sx(s[held[k]]) - _sx(s[held[k - 1]]), _sy(s[held[k]]) - _sy(s[held[k - 1]])) > 1:
            last_sig = held[k]
    up_t = _st(s[held[-1] + 1]) if held[-1] + 1 < len(s) else _st(s[held[-1]])
    return {
        "held_n": len(held),
        "start_hesitation_ms": hes,
        "held_straightness": net / path if path > 1e-6 else 1.0,
        "settle_ms": up_t - _st(s[last_sig]),
        "held_ms": _st(s[held[-1]]) - pt,
    }


def score_drag(s: Sequence[Sample]) -> ActionScore:
    """``score_motion(drag=True)`` plus the drag-specific tells."""
    m = score_motion(s, drag=True)
    f = drag_features(s)
    feats = dict(vars(m.features))
    feats.update(f)
    flags = list(m.flags)
    score = m.score

    def pen(cond: bool, amt: float, flag: str) -> None:
        nonlocal score
        if cond:
            score -= amt
            flags.append(flag)

    if f["held_n"] == 0:
        pen(True, 0.5, "drag-no-held-span")
        return _done("drag", score, flags, feats)
    pen(f["start_hesitation_ms"] < 60, 0.20, "drag-start-no-hesitation(%.0fms)" % f["start_hesitation_ms"])
    pen(f["start_hesitation_ms"] > 800, 0.05, "drag-start-too-slow(%.0fms)" % f["start_hesitation_ms"])
    pen(f["held_n"] >= 6 and f["held_straightness"] > 0.9995, 0.10, "drag-ruler-straight")
    pen(f["settle_ms"] < 40, 0.15, "drag-no-settle(%.0fms)" % f["settle_ms"])
    pen(f["held_n"] < 10 and f["held_ms"] > 100, 0.15, "drag-too-few-held-moves(%d)" % f["held_n"])
    return _done("drag", score, flags, feats)


def hover_features(s: Sequence[Sample]) -> dict:
    """Samples from the arrival on: dwell length, samples after arrival, excursion from the arrival
    point, net drift."""
    n = len(s)
    if n == 0:
        return {"n": 0, "dwell_ms": 0.0, "after_n": 0, "excursion_px": 0.0, "drift_px": 0.0}
    x0, y0, t0 = _sx(s[0]), _sy(s[0]), _st(s[0])
    exc = max(math.hypot(_sx(p) - x0, _sy(p) - y0) for p in s)
    return {
        "n": n,
        "dwell_ms": _st(s[-1]) - t0,
        "after_n": n - 1,
        "excursion_px": exc,
        "drift_px": math.hypot(_sx(s[-1]) - x0, _sy(s[-1]) - y0),
    }


def score_hover(s: Sequence[Sample]) -> ActionScore:
    f = hover_features(s)
    flags: List[str] = []
    score = 1.0

    def pen(cond: bool, amt: float, flag: str) -> None:
        nonlocal score
        if cond:
            score -= amt
            flags.append(flag)

    pen(f["after_n"] < 3, 0.30, "hover-no-dwell-samples(%d)" % f["after_n"])
    pen(f["dwell_ms"] < 150, 0.20, "hover-dwell-short(%.0fms)" % f["dwell_ms"])
    pen(f["dwell_ms"] > 1500, 0.05, "hover-dwell-long(%.0fms)" % f["dwell_ms"])
    pen(f["excursion_px"] > 3, 0.15, "hover-drift-large(%.1fpx)" % f["excursion_px"])
    pen(f["after_n"] >= 3 and f["excursion_px"] == 0, 0.15, "hover-frozen")
    return _done("hover", score, flags, f)


def score_ambient(s: Sequence[Sample]) -> ActionScore:
    """Idle / non-goal motion: only the device-stream tells apply (no Fitts homing to judge)."""
    f = extract_features(s) if len(s) >= 2 else None
    flags: List[str] = []
    score = 1.0

    def pen(cond: bool, amt: float, flag: str) -> None:
        nonlocal score
        if cond:
            score -= amt
            flags.append(flag)

    if f is None:
        pen(True, 0.5, "ambient-too-short")
        return _done("ambient", score, flags, {"n": len(s)})
    pen(f.duration_ms < 200, 0.20, "ambient-too-short(%.0fms)" % f.duration_ms)
    pen(f.dt_uniform, 0.15, "uniform-dt-cadence")
    pen(f.n >= 8 and f.jitter_autocorr < 0.1, 0.10, "white-noise-jitter(%.2f)" % f.jitter_autocorr)
    pen(f.n >= 4 and f.straightness > 0.9995, 0.15, "perfectly-straight-path")
    return _done("ambient", score, flags, dict(vars(f)))


def score_action(kind: str, samples: Sequence[Any]) -> ActionScore:
    """One entry point for the score hook: ``kind`` is glide | move | drag | ambient | typed | wheel | hover."""
    k = (kind or "").lower()
    if k in ("glide", "move", "click", "pointer"):
        m = score_motion(samples, drag=False)
        return _done("glide", m.score, list(m.flags), dict(vars(m.features)))
    if k == "drag":
        return score_drag(samples)
    if k == "ambient":
        return score_ambient(samples)
    if k in ("typed", "type", "keys"):
        return score_typed(samples)
    if k == "wheel":
        return score_wheel(samples)
    if k == "hover":
        return score_hover(samples)
    raise ValueError("unknown action kind: %r" % (kind,))
