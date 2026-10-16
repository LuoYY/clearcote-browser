"""Human motion core (Python) — the trajectory/timing model shared by the sync and async humanizers.

Faithful port of the Node SDK's ``src/motion.ts``. PURE (no Playwright, no I/O): given a start point,
a target and a seeded :class:`Persona`, it returns a list of ``Step``s (absolute cursor coordinates +
a post-step sleep in ms) that the wrapper walks with the engine's NATIVE mouse.move + an off-protocol
sleep. Keeping the math here unifies sync/async (and mirrors Node) so they can't drift, and makes it
unit-testable offline. See ``motion.ts`` for the full modelling rationale (minimum-jerk submovements,
Fitts duration, colored noise, seeded persona, endpoint dwells).

The RNG (mulberry32 + FNV-1a seed hash) is bit-identical to the Node version, so the seed→persona map
is the SAME across both SDKs (a fingerprint gets one motor identity regardless of language). Trajectory
sampling uses transcendental functions (sqrt/log/sin/cos) whose last-ULP results can differ between V8
and CPython, so per-sample paths are statistically — not byte — identical across languages.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, Optional, Union

Rng = Callable[[], float]

_MASK = 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    """Emulate JS Math.imul: low 32 bits of the product (unsigned representation)."""
    return ((a & _MASK) * (b & _MASK)) & _MASK


def mulberry32(seed_int: int) -> Rng:
    """Deterministic 0..1 RNG, bit-identical to the Node mulberry32 (unsigned-32 arithmetic)."""
    state = seed_int & _MASK

    def rng() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & _MASK
        a = state
        t = _imul(a ^ (a >> 15), 1 | a)
        t = ((t + _imul(t ^ (t >> 7), 61 | t)) & _MASK) ^ t
        t &= _MASK
        return ((t ^ (t >> 14)) & _MASK) / 4294967296.0

    return rng


def _js_round(x: float) -> int:
    """JS Math.round: round half toward +infinity (floor(x+0.5)), not Python's banker's rounding."""
    return math.floor(x + 0.5)


def hash_seed(seed: Optional[Union[str, int, float]]) -> int:
    """FNV-1a (string) / knuth-multiplicative (number) hash to a 32-bit int, matching Node hashSeed."""
    if seed is None:
        return int(random.random() * 0xFFFFFFFF) & _MASK
    if isinstance(seed, (int, float)) and not isinstance(seed, bool) and math.isfinite(seed):
        # Multiply in IEEE double (like JS) then ToUint32 — an exact big-int multiply would diverge.
        return int(float(abs(seed)) * 2654435761.0) & _MASK
    s = str(seed)
    h = 0x811C9DC5
    for ch in s:
        h ^= ord(ch) & 0xFFFF   # match JS charCodeAt (full UTF-16 code unit), not just the low byte
        h = _imul(h, 0x01000193)
    return h & _MASK


def gauss_from(rng: Rng) -> float:
    """Standard-normal sample (Box–Muller) from a given RNG."""
    u = 0.0
    v = 0.0
    while u == 0.0:
        u = rng()
    while v == 0.0:
        v = rng()
    return math.sqrt(-2.0 * math.log(u)) * math.cos(2.0 * math.pi * v)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class Persona:
    """The per-identity motor signature (see motion.ts for field semantics/ranges)."""

    seed: int
    device_hz: float
    tremor_hz: float
    tremor_amp: float
    drift_amp: float
    drift_tau: float
    jitter: float
    fitts_a: float
    fitts_b: float
    primary_frac: float
    overshoot: float
    max_corrections: int
    approach_bias: float
    grab_min_ms: float = 130.0
    grab_max_ms: float = 360.0
    release_min_ms: float = 90.0
    release_max_ms: float = 230.0
    # click press-hold (mousedown->mouseup) and per-key dwell (keydown->keyup) windows, ms.
    click_hold_min_ms: float = 60.0
    click_hold_max_ms: float = 150.0
    key_dwell_min_ms: float = 45.0
    key_dwell_max_ms: float = 120.0


def make_persona(seed: Optional[Union[str, int, float]] = None) -> Persona:
    """Derive a stable motor persona from a fingerprint seed (random when unseeded).

    The RNG draw ORDER mirrors motion.ts exactly (including the short-circuit in the max-corrections
    ternary) so the seed→persona mapping is identical across the two SDKs."""
    seed_int = hash_seed(seed)
    r = mulberry32(seed_int)
    device_hz = _js_round(_lerp(110, 155, r()))
    tremor_hz = _lerp(8, 12, r())
    tremor_amp = _lerp(0.12, 0.5, r())
    drift_amp = _lerp(0.1, 0.4, r())
    drift_tau = _lerp(60, 160, r())
    jitter = _lerp(0.25, 0.7, r())
    fitts_a = _lerp(90, 150, r())
    fitts_b = _lerp(120, 190, r())
    primary_frac = _lerp(0.84, 0.94, r())
    overshoot = _lerp(0.03, 0.08, r())
    # short-circuit ternary: r() < 0.15 ? 3 : (r() < 0.6 ? 2 : 1) — second draw only when first fails
    if r() < 0.15:
        max_corrections = 3
    else:
        max_corrections = 2 if r() < 0.6 else 1
    approach_bias = (r() - 0.5) * 0.5
    return Persona(
        seed=seed_int, device_hz=device_hz, tremor_hz=tremor_hz, tremor_amp=tremor_amp,
        drift_amp=drift_amp, drift_tau=drift_tau, jitter=jitter, fitts_a=fitts_a, fitts_b=fitts_b,
        primary_frac=primary_frac, overshoot=overshoot, max_corrections=max_corrections,
        approach_bias=approach_bias,
    )


# A Step is (x, y, sleep_ms): move the cursor to (x, y), then sleep sleep_ms before the next.
Step = "tuple[float, float, float]"


def min_jerk(tau: float) -> float:
    """Minimum-jerk easing s(τ)=10τ³−15τ⁴+6τ⁵ (speed 30τ²(1−τ)²)."""
    t = _clamp(tau, 0.0, 1.0)
    return t * t * t * (10 + t * (-15 + 6 * t))


def _bez(p0: float, c1: float, c2: float, p1: float, e: float) -> float:
    m = 1 - e
    return m * m * m * p0 + 3 * m * m * e * c1 + 3 * m * e * e * c2 + e * e * e * p1


def _log2(x: float) -> float:
    return math.log(x) / math.log(2.0)


# Salt for the path-shape stream. Like the rollover salt, a separate mulberry32(seed ^ salt) stream
# keeps make_persona's draw ORDER (and so every existing seed's motor identity) unchanged.
_SHAPE_SALT = 0x85EBCA6B


@dataclass(frozen=True)
class ShapeTraits:
    """Per-identity path-shape traits, stable per seed (see shape_traits)."""

    overshoot_rate: float   # share of aimed moves that pass the target and come back, 0.15..0.38
    curve: float            # scale on the primary submovement's bow, 0.75..1.3
    drag_tempo: float       # scale on held-drag duration, 0.85..1.2


def shape_traits(p: Persona) -> ShapeTraits:
    """The persona's path-shape traits, drawn from a salted stream of its seed.

    Measured on r38 / SDK 0.48.0 (SC 209): 1 of 36 planned approaches passed the target, against
    15-40 % for real reaches and 14/36 for the engine's recorded trajectories, because the primary
    submovement always aimed at 84-94 % of the distance. Same values in motion.ts shapeTraits and
    Motion.ShapeTraitsOf."""
    r = mulberry32((p.seed ^ _SHAPE_SALT) & _MASK)
    overshoot_rate = _lerp(0.15, 0.38, r())
    curve = _lerp(0.75, 1.3, r())
    drag_tempo = _lerp(0.85, 1.2, r())
    return ShapeTraits(overshoot_rate=overshoot_rate, curve=curve, drag_tempo=drag_tempo)


# Sub-pixel noise scale. The colored noise (OU drift + 8-12 Hz tremor + AR(1) jitter) stays, but
# smaller and carried mostly while the hand is moving: a mouse resting on the pad reports no counts,
# so a slow tail full of +-0.5 px wobble is what a quantizing page turns into 1 px zigzags.
_NOISE = 0.6


def _skewed(tau: float, skew: float) -> float:
    """Time warp 1-(1-tau)^skew: skew > 1 moves the min-jerk speed peak before the midpoint."""
    t = _clamp(tau, 0.0, 1.0)
    return 1.0 - math.pow(1.0 - t, skew) if skew != 1.0 else t


def _sample_submove(out, a, b, dur_ms, p: Persona, rng: Rng, t0, drift, jit, tremor_phase, land,
                    bow_frac, skew):
    """Sample ONE min-jerk submovement a->b along a bowed path with colored noise. Appends
    (x, y, sleep_ms) tuples to ``out``. Returns the absolute clock (ms) at the end.

    ``bow_frac``: signed peak sideways deviation as a share of the submovement's length.
    ``skew``: time warp of the min-jerk profile (1 = symmetric)."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    dist = math.hypot(dx, dy) or 1e-6
    step_ms = 1000.0 / p.device_hz
    # Cap the sample count so a very long move can't emit thousands of steps (defends the Fitts cap).
    n = int(min(180, max(4, _js_round(dur_ms / step_ms))))
    nx, ny = -dy / dist, dx / dist
    # A cubic with both control points offset by c peaks at 0.75 c; the first control point carries
    # more of it, so the widest point of the arc comes before the middle, as in a reach.
    c = bow_frac * dist / 0.75
    c1x, c1y = ax + dx * 0.3 + nx * c * 1.15, ay + dy * 0.3 + ny * c * 1.15
    c2x, c2y = ax + dx * 0.7 + nx * c * 0.85, ay + dy * 0.7 + ny * c * 0.85
    t = t0
    for i in range(1, n + 1):
        tau = i / n
        e = min_jerk(_skewed(tau, skew))
        px = _bez(ax, c1x, c2x, bx, e)
        py = _bez(ay, c1y, c2y, by, e)
        last = i == n
        if not (last and land):
            # noise follows the speed envelope (min-jerk speed 30 tau^2 (1-tau)^2 peaks at 1.875)
            v = 30.0 * tau * tau * (1 - tau) * (1 - tau) / 1.875
            g = _NOISE * (0.35 + 0.65 * v)
            k = step_ms / p.drift_tau
            drift[0] += -k * drift[0] + math.sqrt(2 * k) * p.drift_amp * gauss_from(rng)
            drift[1] += -k * drift[1] + math.sqrt(2 * k) * p.drift_amp * gauss_from(rng)
            # 8-12 Hz tremor: absolute-clock phase (continuous across submovements) + slow amplitude
            # breathing + a slightly DETUNED/phase-offset y axis (not a fixed-amplitude 90-deg-locked tone).
            t_amp = p.tremor_amp * (0.7 + 0.3 * math.sin((2 * math.pi * t) / 850.0 + tremor_phase))
            tr_x = t_amp * math.sin(tremor_phase + (2 * math.pi * p.tremor_hz * t) / 1000.0)
            tr_y = t_amp * math.sin(tremor_phase + 1.0 + (2 * math.pi * (p.tremor_hz * 0.93) * t) / 1000.0)
            # Sub-pixel jitter as a COLORED AR(1) process, not i.i.d. white noise (white noise adds a
            # fresh speed reversal every sample -> spurious "submovements" + flat noise spectrum).
            jit[0] = 0.7 * jit[0] + gauss_from(rng) * p.jitter * 0.22
            jit[1] = 0.7 * jit[1] + gauss_from(rng) * p.jitter * 0.22
            px += (drift[0] + tr_x + jit[0]) * g
            py += (drift[1] + tr_y + jit[1]) * g
        else:
            px, py = bx, by
        sleep_ms = step_ms * _lerp(0.88, 1.12, rng())
        out.append((px, py, sleep_ms))
        t += sleep_ms
    return t


def plan_move(frm, to, p: Persona, target_w: float = 24.0, settle: bool = False, rng: Optional[Rng] = None,
              held: bool = False):
    """Plan a free cursor move frm->to as a sum of minimum-jerk submovements (primary + corrections),
    Fitts-scaled duration and colored noise. Returns a list of (x, y, sleep_ms) tuples.

    A move with a button held (``held``) or one that seats a handle (``settle``) is a drag, planned
    by :func:`plan_drag`.

    Shape (SC 209/210): the primary submovement arcs (peak sideways deviation ~6-18 % of the
    distance, per-persona curvature and handedness), reaches its speed peak before the middle of the
    move, and either stops short of the target (a short corrective submovement closes the gap) or,
    in ``shape_traits(p).overshoot_rate`` of moves, passes it by 3-28 px and comes back. Straightness
    (distance / path length) of the planned points lands around 0.85-0.95, the band of real reaches."""
    if settle or held:
        return plan_drag(frm, to, p, settle=settle, rng=rng)
    if rng is None:
        rng = random.random
    fx, fy = frm
    tx, ty = to
    w = max(6.0, target_w)
    d = math.hypot(tx - fx, ty - fy)
    out: list = []
    if d < 1.5:
        out.append((tx, ty, 1000.0 / p.device_hz))
        return out

    idv = _log2(d / w + 1)
    # floor capped at the 1700ms ceiling so a very long move can't invert the clamp (lo > hi).
    mt_lo = min(1700.0, max(70.0, 40.0 + d * 0.6))
    mt = _clamp((p.fitts_a + p.fitts_b * idv) * (0.85 + 0.3 * rng()), mt_lo, 1700.0)
    tr = shape_traits(p)
    ux, uy = (tx - fx) / d, (ty - fy) / d

    targets = []
    over = False
    if d >= 40:
        over = rng() < tr.overshoot_rate
        if over:
            along = d + _clamp(d * _lerp(0.03, 0.08, rng()), 3.0, 28.0)
        else:
            along = d * _clamp(p.primary_frac + 0.05 + gauss_from(rng) * 0.015, 0.88, 0.985)
        lat = gauss_from(rng) * min(6.0, d * 0.02)
        targets.append((fx + ux * along - uy * lat, fy + uy * along + ux * lat))
        if rng() < 0.08 * p.max_corrections:
            cur = targets[0]
            close = _lerp(0.55, 0.8, rng())
            targets.append((cur[0] + (tx - cur[0]) * close + gauss_from(rng) * 1.0,
                            cur[1] + (ty - cur[1]) * close + gauss_from(rng) * 1.0))
    targets.append((tx, ty))

    seg_dur = []
    if len(targets) == 1:
        seg_dur.append(mt)
    else:
        seg_dur.append(mt * 0.78)
        rest = mt * 0.22
        nc = len(targets) - 1
        for _ in range(nc):
            seg_dur.append((rest / nc) * _lerp(0.8, 1.2, rng()))

    # The primary's arc: handedness sign from the persona (1 move in 5 bends the other way), size
    # from the persona's curvature trait. Corrections are short and nearly straight.
    sign = 1.0 if p.approach_bias >= 0 else -1.0
    if rng() < 0.2:
        sign = -sign
    bow = sign * min(0.25, (0.08 + abs(gauss_from(rng)) * 0.11) * tr.curve * (0.75 + abs(p.approach_bias)))
    if d * abs(bow) > 110.0:
        bow = math.copysign(110.0 / d, bow)

    drift = [0.0, 0.0]
    jit = [0.0, 0.0]
    tremor_phase = rng() * 2 * math.pi
    a = frm
    t = 0.0
    for s in range(len(targets)):
        b = targets[s]
        last = s == len(targets) - 1
        first = s == 0
        t = _sample_submove(out, a, b, max(20.0, seg_dur[s]), p, rng, t, drift, jit, tremor_phase, last,
                            bow if first else bow * 0.15, 1.25 if first else 1.0)
        if not last and out:
            x, y, sl = out[-1]
            # motor re-planning gap; a reversal after passing the target comes back quicker
            out[-1] = (x, y, sl + (_lerp(25, 90, rng()) if (over and first) else _lerp(40, 120, rng())))
        a = b
    return out


def plan_drag(frm, to, p: Persona, settle: bool = False, rng: Optional[Rng] = None):
    """Plan a held-button drag frm->to (slider handle, drag-and-drop, range thumb).

    Measured on r38 / SDK 0.48.0 (SC 209): the held leg reused the aimed-move planner, whose arc
    put the hand up to 47 px off the line between grab and drop -- off a 40 px slider track. A drag
    is a slower, flatter movement: one skewed min-jerk leg along the line with at most ~4 px of arc
    and +-6 px of total sideways error, 0.5-2 s for 300-400 px (``shape_traits(p).drag_tempo``),
    and now and then (about 0.6 x the persona's overshoot rate) a 2.5-10 px overshoot and pull-back.
    ``settle`` adds a couple of sub-pixel seating jiggles before landing exactly on ``to``."""
    if rng is None:
        rng = random.random
    fx, fy = frm
    tx, ty = to
    d = math.hypot(tx - fx, ty - fy)
    out: list = []
    step_ms = 1000.0 / p.device_hz
    if d < 1.5:
        out.append((tx, ty, step_ms))
        return out
    tr = shape_traits(p)
    ux, uy = (tx - fx) / d, (ty - fy) / d
    mt = _clamp((260.0 + 1.7 * d) * tr.drag_tempo * _lerp(0.9, 1.1, rng()), min(500.0, 150.0 + 1.5 * d), 2000.0)
    over = d >= 60 and rng() < tr.overshoot_rate * 0.6
    o = _clamp(d * _lerp(0.01, 0.03, rng()), 2.5, 10.0) if over else 0.0
    sign = 1.0 if p.approach_bias >= 0 else -1.0
    h = sign * min(5.0, d * 0.015) * _lerp(0.3, 1.0, rng())
    tremor_phase = rng() * 2 * math.pi

    legs = [(0.0, d + o, mt * (0.86 if over else 1.0), h)]
    if over:
        legs.append((d + o, d, mt * 0.14, 0.0))
    drift = 0.0
    jit = 0.0
    t = 0.0
    for li, (s0, s1, dur, hh) in enumerate(legs):
        n = int(min(360, max(4, _js_round(dur / step_ms))))
        dt = dur / n
        last_leg = li == len(legs) - 1
        for i in range(1, n + 1):
            tau = i / n
            e = min_jerk(_skewed(tau, 1.2))
            along = s0 + (s1 - s0) * e
            v = 30.0 * tau * tau * (1 - tau) * (1 - tau) / 1.875
            k = dt / p.drift_tau
            drift += -k * drift + math.sqrt(2 * k) * p.drift_amp * 0.8 * gauss_from(rng)
            jit = 0.7 * jit + gauss_from(rng) * p.jitter * 0.12
            t_amp = p.tremor_amp * (0.7 + 0.3 * math.sin((2 * math.pi * t) / 850.0 + tremor_phase))
            tremor = t_amp * math.sin(tremor_phase + (2 * math.pi * p.tremor_hz * t) / 1000.0)
            along += t_amp * 0.6 * math.sin(tremor_phase + 1.0 + (2 * math.pi * (p.tremor_hz * 0.93) * t) / 1000.0) * v
            side = hh * math.sin(math.pi * e) + (drift + tremor + jit) * (0.6 + 0.4 * v)
            side = _clamp(side, -6.0, 6.0)
            if last_leg and i == n and not settle:
                px, py = tx, ty
            else:
                px, py = fx + ux * along - uy * side, fy + uy * along + ux * side
            sl = dt * _lerp(0.88, 1.12, rng())
            out.append((px, py, sl))
            t += sl
        if not last_leg and out:
            x, y, sl = out[-1]
            out[-1] = (x, y, sl + _lerp(30, 80, rng()))   # the hand notices, stops, pulls back

    if settle:
        # A couple of tiny sub-pixel seating jiggles, then land exactly (handle "clunk" into place).
        jig = 1 + int(rng() * 2)
        for _ in range(jig):
            out.append((tx + _clamp(gauss_from(rng) * 0.8, -1.5, 1.5), ty + _clamp(gauss_from(rng) * 0.8, -1.5, 1.5),
                        _lerp(30, 90, rng())))
        out.append((tx, ty, step_ms))
    return out


# Salt for the drag-start hesitation traits (per persona, like _SHAPE_SALT / _ROLLOVER_SALT): drawing
# them from mulberry32(seed ^ salt) keeps make_persona's draw ORDER, so every seed's motor identity is
# what it was, and plan_drag itself is untouched (its vectors still pin the three SDKs).
_HESITATION_SALT = 0xC2B2AE35
# A hand that has just closed on a handle does not set off at once: it holds 100-300 ms, creeping a
# few px (never 8) in the direction it is about to go, before the leg proper. Measured on r38 / SDK
# 0.51.0 (SC 232 review): every drag's first held move was already the full-speed leg start.
HESITATION_MS = (100.0, 300.0)
HESITATION_MAX_PX = 8.0


def drag_hesitation(frm, to, p: Persona, rng: Optional[Rng] = None, dur_ms: Optional[float] = None):
    """Plan the still-but-not-frozen moment between the grab and the drag leg frm->to: (x, y, sleep_ms)
    samples spanning 100-300 ms (centred per persona), the pointer creeping < 8 px toward ``to`` with a
    little sideways wobble. The last sample is where :func:`plan_drag` then starts. The samples are
    drawn from ``rng`` AFTER nothing: a caller plans the leg from the returned end point, so the leg's
    own draws keep their order. ``dur_ms``: the caller's grab window (the persona's grab dwell), clamped
    to 100-300 ms, in place of this persona's own draw (which is still drawn, so the order holds).
    Same numbers in motion.ts dragHesitation and Motion.DragHesitation."""
    if rng is None:
        rng = random.random
    r = mulberry32((p.seed ^ _HESITATION_SALT) & _MASK)
    centre = _lerp(130.0, 260.0, r())          # this hand's typical hesitation
    amp = _lerp(2.0, 6.0, r())                 # how far it creeps (px)
    wobble = _lerp(0.4, 1.2, r())              # sideways (px)
    fx, fy = frm
    tx, ty = to
    d = math.hypot(tx - fx, ty - fy)
    ux, uy = ((tx - fx) / d, (ty - fy) / d) if d >= 1e-6 else (1.0, 0.0)
    dur = _clamp(centre * _lerp(0.8, 1.2, rng()), HESITATION_MS[0], HESITATION_MS[1])
    if dur_ms is not None:
        dur = _clamp(float(dur_ms), HESITATION_MS[0], HESITATION_MS[1])
    reach = amp * _lerp(0.6, 1.0, rng())
    step_ms = 1000.0 / p.device_hz
    n = int(max(3, _js_round(dur / step_ms)))
    dt = dur / n
    phase = rng() * 2 * math.pi
    out: list = []
    side = 0.0
    for i in range(1, n + 1):
        tau = i / n
        along = reach * min_jerk(tau)
        side = 0.8 * side + gauss_from(rng) * wobble * 0.25
        s = _clamp(side + wobble * 0.5 * math.sin(phase + 2 * math.pi * tau), -1.5, 1.5)
        px, py = fx + ux * along - uy * s, fy + uy * along + ux * s
        # the budget is a hard bound: the creep never leaves an 8 px circle around the grab
        if math.hypot(px - fx, py - fy) >= HESITATION_MAX_PX - 0.5:
            px, py = fx + ux * (HESITATION_MAX_PX - 1.0), fy + uy * (HESITATION_MAX_PX - 1.0)
        out.append((px, py, dt * _lerp(0.9, 1.1, rng())))
    return out


def drag_dwell(p: Persona, rng: Optional[Rng] = None):
    """(grab_ms, release_ms) for a held-button drag, from the persona window."""
    if rng is None:
        rng = random.random
    return (_lerp(p.grab_min_ms, p.grab_max_ms, rng()), _lerp(p.release_min_ms, p.release_max_ms, rng()))


def click_hold(p: Persona, rng: Optional[Rng] = None) -> float:
    """A human mousedown->mouseup click hold (ms), right-skewed. Fixes the ~2 ms instant-press tell."""
    if rng is None:
        rng = random.random
    return p.click_hold_min_ms + min(1.0, abs(gauss_from(rng)) * 0.5) * (p.click_hold_max_ms - p.click_hold_min_ms)


def key_dwell(p: Persona, rng: Optional[Rng] = None) -> float:
    """A human per-key keydown->keyup dwell (ms), right-skewed. Fixes the ~2-3 ms instant-keystroke tell."""
    if rng is None:
        rng = random.random
    return p.key_dwell_min_ms + min(1.0, abs(gauss_from(rng)) * 0.45) * (p.key_dwell_max_ms - p.key_dwell_min_ms)


# Salt for the rollover stream. Drawing from mulberry32(seed ^ salt) instead of adding a field to
# make_persona keeps the persona draw ORDER (and so every existing seed's motor identity) unchanged.
_ROLLOVER_SALT = 0x9E3779B9


def rollover_rate(p: Persona) -> float:
    """Share of eligible key pairs this persona types with ROLLOVER: the next key goes down before
    the previous one comes up, the way people type fast common pairs. Stable per seed, 0.12..0.38.

    Measured on r32/r33 before this existed: 0 overlapping keydowns in 30-32 typed keys, because
    every character was one press() (down, dwell, up) before the next began.
    Same value in motion.ts rolloverRate and Motion.RolloverRate."""
    return _lerp(0.12, 0.38, mulberry32((p.seed ^ _ROLLOVER_SALT) & _MASK)())


# Pixels one wheel notch scrolls, by the platform the persona claims. A wheel event sent over CDP
# always carries wheelDelta +-120 (input_handler.cc sets wheel_ticks to +-1 whatever the delta), and
# a real notch is the only event whose distance goes with a 120 wheelDelta:
#   windows 100 = 3 lines x 100/3 px (web_input_event_builders_win.cc, default 3 lines per notch)
#   linux   120 = ui::MouseWheelEvent::kWheelDelta (event.cc); measured 120/-120 per X11 notch
#   macos    40 = kScrollbarPixelsPerCocoaTick (cocoa_event_utils.h)
# android is not measured; it gets the windows value.
WHEEL_NOTCH_PX = {"windows": 100, "linux": 120, "macos": 40, "android": 100}


def wheel_notches(delta: float, notch_px: float) -> int:
    """Whole notches for a requested scroll distance: the nearest count, at least one when the
    caller asked for any scroll at all. A wheel cannot move 39 px; it moves notches."""
    if not delta:
        return 0
    return max(1, _js_round(abs(delta) / notch_px))


def platform_from_navigator(value: Optional[str]) -> Optional[str]:
    """Map navigator.userAgentData.platform / navigator.platform to a WHEEL_NOTCH_PX key."""
    v = (value or "").lower()
    if not v:
        return None
    if "android" in v:   # before linux: Android's navigator.platform is "Linux armv8l"
        return "android"
    if "win" in v:
        return "windows"
    if "mac" in v:
        return "macos"
    if "linux" in v or "x11" in v or "cros" in v:
        return "linux"
    return None


def click_point(box, frm, p: Persona, rng: Optional[Rng] = None):
    """A human click point inside ``box`` (dict with x/y/width/height): 2D gaussian toward center,
    nudged toward the approach side (undershoot), clamped a couple px inside the edges."""
    if rng is None:
        rng = random.random
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    dx, dy = cx - frm[0], cy - frm[1]
    d = math.hypot(dx, dy) or 1.0
    ux, uy = -dx / d, -dy / d
    x = cx + ux * box["width"] * 0.12 + gauss_from(rng) * box["width"] * 0.16
    y = cy + uy * box["height"] * 0.12 + gauss_from(rng) * box["height"] * 0.16
    # For a box narrower/shorter than the 4px inset the clamp bounds invert (lo > hi) and would return
    # a point OUTSIDE the element — use the center for such tiny targets.
    return (_clamp(x, box["x"] + 2, box["x"] + box["width"] - 2) if box["width"] >= 4 else cx,
            _clamp(y, box["y"] + 2, box["y"] + box["height"] - 2) if box["height"] >= 4 else cy)


def plan_ambient(frm, viewport, p: Persona, ms: float, rng: Optional[Rng] = None):
    """Plan ambient / pre-challenge cursor activity (idle drift + non-goal moves within the viewport)
    so a behavioral collector sees non-zero pointer entropy before the first goal action. ~``ms`` long."""
    if rng is None:
        rng = random.random
    out: list = []
    cur = frm
    elapsed = 0.0
    budget = max(200.0, ms)
    vw, vh = viewport["width"], viewport["height"]
    bx0, bx1 = vw * 0.12, vw * 0.88
    by0, by1 = vh * 0.14, vh * 0.8
    while elapsed < budget and len(out) < 4000:
        target = (_lerp(bx0, bx1, rng()), _lerp(by0, by1, rng()))
        for s in plan_move(cur, target, p, target_w=80, rng=rng):
            out.append(s)
            elapsed += s[2]
        rest_ms = _lerp(120, 600, rng())
        drift = [0.0, 0.0]
        step_ms = 1000.0 / p.device_hz
        held = 0.0
        tphase = rng() * 2 * math.pi
        while held < rest_ms:
            k = step_ms / p.drift_tau
            drift[0] += -k * drift[0] + math.sqrt(2 * k) * p.drift_amp * gauss_from(rng)
            drift[1] += -k * drift[1] + math.sqrt(2 * k) * p.drift_amp * gauss_from(rng)
            ph = tphase + (2 * math.pi * p.tremor_hz * held) / 1000.0
            out.append((target[0] + drift[0] + p.tremor_amp * math.sin(ph),
                        target[1] + drift[1] + p.tremor_amp * math.sin(ph + math.pi / 2),
                        step_ms * _lerp(0.9, 1.4, rng())))
            held += step_ms
            elapsed += step_ms
        cur = target
    return out
