// Detector-emulator — extracts the feature vector a commercial behavioral collector
// would compute from a pointer stream and scores how human it looks. This is a VALIDATION tool: it
// lets us prove, offline and deterministically, that the motion core (motion.ts) improves the features
// that matter, instead of eyeballing trajectories or burning scarce clean-IP end-to-end runs.
//
// It is intentionally an APPROXIMATION of what real collectors do (their exact models are private);
// treat the score as a relative signal (old vs new), not an absolute pass/fail.

export interface Sample { x: number; y: number; t: number; buttons?: number; }

export interface MotionFeatures {
  n: number;
  durationMs: number;
  /** Fraction of the movement at which peak speed occurs. Human ≈ 0.3–0.45 (fast ballistic rise, slow
   * homing). A single analytic ease curve is SYMMETRIC (peak ≈ 0.5); pure ease-out peaks ≈ 0.15. */
  peakVelFrac: number;
  /** Time-weighted skewness of the speed profile. Human > 0 (long decelerating homing tail); a
   * symmetric single-curve motion ≈ 0. */
  velSkew: number;
  /** Corrective submovements = prominent interior speed minima (human 1–3; a single curve = 0). */
  submovements: number;
  /** Net displacement / path length (1 = perfectly straight). */
  straightness: number;
  /** Diagnostic only (NOT scored): residual of the whole speed profile vs a SINGLE min-jerk bell.
   * A real multi-submovement reach does not fit one bell, so this is high for human motion too. */
  minJerkResidual: number;
  /** Inter-sample dt mean / std (ms) and a uniformity flag (i.i.d.-uniform dt is a tell). */
  dtMeanMs: number;
  dtStdMs: number;
  dtUniform: boolean;
  /** Lag-1 autocorrelation of the sub-pixel jitter residual. White noise ≈ 0; colored motor noise
   * (1/f drift + 8–12 Hz tremor) ≈ 0.2–0.8. */
  jitterAutocorr: number;
  /** 2/3 power-law fit quality (diagnostic only — not generated): correlation of log speed vs
   * −1/3·log curvature. ~0 for straight paths (undefined curvature); informative on curved ones. */
  powerLawR: number;
  /** Drag endpoint timing (only when a held-button segment is present), else null. */
  grabMs: number | null;
  releaseMs: number | null;
}

function speeds(s: Sample[]): { v: number[]; tMid: number[] } {
  const v: number[] = [], tMid: number[] = [];
  for (let i = 1; i < s.length; i++) {
    const dt = Math.max(1e-3, s[i].t - s[i - 1].t);
    v.push(Math.hypot(s[i].x - s[i - 1].x, s[i].y - s[i - 1].y) / dt);
    tMid.push((s[i].t + s[i - 1].t) / 2);
  }
  return { v, tMid };
}

/** Smooth a series with a small moving average (window w). */
function smooth(a: number[], w = 3): number[] {
  const out: number[] = [];
  for (let i = 0; i < a.length; i++) {
    let s = 0, c = 0;
    for (let j = Math.max(0, i - w); j <= Math.min(a.length - 1, i + w); j++) { s += a[j]; c++; }
    out.push(s / c);
  }
  return out;
}

function mean(a: number[]): number { return a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0; }
function std(a: number[]): number {
  if (a.length < 2) return 0;
  const m = mean(a);
  return Math.sqrt(mean(a.map((x) => (x - m) ** 2)));
}

export function extractFeatures(s: Sample[]): MotionFeatures {
  const n = s.length;
  const durationMs = n > 1 ? s[n - 1].t - s[0].t : 0;
  const { v, tMid } = speeds(s);
  const sv = smooth(v, 2);

  // peak velocity position
  let peakIdx = 0;
  for (let i = 1; i < sv.length; i++) if (sv[i] > sv[peakIdx]) peakIdx = i;
  const peakVelFrac = durationMs > 0 ? (tMid[peakIdx] - s[0].t) / durationMs : 0;

  // velocity skewness (positive for a human's fast-rise/slow-decay profile; ~0 for a symmetric curve)
  const vmean = mean(sv), vstd = std(sv);
  // A constant-speed stream has std 0 up to float noise: the skew of that noise is meaningless and differed
  // between SDKs, so it reads as 0 (SC 232).
  const velSkew = vstd > 1e-9 * Math.max(1, Math.abs(vmean)) ? mean(sv.map((x) => ((x - vmean) / vstd) ** 3)) : 0;

  // corrective submovements: prominent interior local minima (a real dip that recovers), not jitter.
  // Require the min to sit below 0.5·vmax and be a true valley between two higher shoulders, with a
  // minimum index separation so a single correction isn't counted several times.
  const vmax = Math.max(...sv, 1e-9);
  let submovements = 0, lastMin = -10;
  for (let i = 2; i < sv.length - 2; i++) {
    const valley = sv[i] < sv[i - 1] && sv[i] <= sv[i + 1] && sv[i] < sv[i - 2] && sv[i] < sv[i + 2];
    if (valley && sv[i] < 0.5 * vmax && i - lastMin >= 3) { submovements++; lastMin = i; }
  }

  // straightness
  let pathLen = 0;
  for (let i = 1; i < n; i++) pathLen += Math.hypot(s[i].x - s[i - 1].x, s[i].y - s[i - 1].y);
  const netDisp = n > 1 ? Math.hypot(s[n - 1].x - s[0].x, s[n - 1].y - s[0].y) : 0;
  const straightness = pathLen > 1e-6 ? netDisp / pathLen : 1;

  // min-jerk residual: compare the normalized speed profile to 30τ²(1−τ)²
  let minJerkResidual = 1;
  if (sv.length >= 4 && durationMs > 0) {
    const area = sv.reduce((acc, val, i) => acc + val * (i === 0 ? 0 : tMid[i] - tMid[i - 1]), 0) || 1;
    let res = 0, norm = 0;
    for (let i = 0; i < sv.length; i++) {
      const tau = (tMid[i] - s[0].t) / durationMs;
      const ideal = 30 * tau * tau * (1 - tau) * (1 - tau); // ∫ over τ = 1
      const obs = (sv[i] * durationMs) / area; // normalize so ∫ obs dτ ≈ 1
      res += (obs - ideal) ** 2; norm += ideal ** 2;
    }
    minJerkResidual = norm > 0 ? Math.sqrt(res / norm) : 1;
  }

  // dt stats + uniformity (uniform-i.i.d. dt has std/mean near that of U(a,b) and low autocorrelation)
  const dts: number[] = [];
  for (let i = 1; i < n; i++) dts.push(s[i].t - s[i - 1].t);
  const dtMeanMs = mean(dts), dtStdMs = std(dts);
  // A real device stream has a sharp cadence MODE (most samples near the median) with a heavy tail of
  // pauses; a rand(a,b) band has NO mode (roughly flat), so few samples cluster near the median.
  // Measure the fraction within ±20% of the median: high ⇒ has a mode; low ⇒ looks uniform/flat.
  const sorted = [...dts].sort((a, b) => a - b);
  const median = sorted.length ? sorted[Math.floor(sorted.length / 2)] : 0;
  const coreFrac = median > 0 ? dts.filter((d) => d >= 0.8 * median && d <= 1.2 * median).length / dts.length : 0;
  const dtUniform = coreFrac < 0.5;

  // jitter color: lag-1 autocorrelation of the sub-pixel residual (position minus a smoothed trend).
  // White gauss ≈ 0; colored motor noise (slow drift + band-limited tremor) ≈ 0.2–0.8.
  let jitterAutocorr = 0;
  if (n >= 8) {
    const xs = s.map((p) => p.x), ys = s.map((p) => p.y);
    const sx = smooth(xs, 3), sy = smooth(ys, 3);
    const rx = xs.map((x, i) => x - sx[i]), ry = ys.map((y, i) => y - sy[i]);
    const ac = (r: number[]) => {
      let num = 0, den = 0;
      for (let i = 1; i < r.length; i++) num += r[i] * r[i - 1];
      for (let i = 0; i < r.length; i++) den += r[i] * r[i];
      return den > 1e-9 ? num / den : 0;
    };
    jitterAutocorr = (ac(rx) + ac(ry)) / 2;
  }

  // 2/3 power law (diagnostic): correlation between log speed and −(1/3) log curvature
  let powerLawR = 0;
  if (n >= 6) {
    const lv: number[] = [], lc: number[] = [];
    for (let i = 1; i < n - 1; i++) {
      const ax = s[i].x - s[i - 1].x, ay = s[i].y - s[i - 1].y;
      const bx = s[i + 1].x - s[i].x, by = s[i + 1].y - s[i].y;
      const cross = Math.abs(ax * by - ay * bx);
      const segA = Math.hypot(ax, ay), segB = Math.hypot(bx, by);
      const curv = cross / (Math.pow(segA * segB * Math.hypot(bx + ax, by + ay), 1) + 1e-9);
      const sp = (segA + segB) / 2 / Math.max(1e-3, (s[i + 1].t - s[i - 1].t) / 2);
      if (curv > 1e-6 && sp > 1e-6) { lv.push(Math.log(sp)); lc.push(Math.log(curv)); }
    }
    if (lv.length >= 4) {
      const mv = mean(lv), mc = mean(lc);
      let num = 0, dv = 0, dc = 0;
      for (let i = 0; i < lv.length; i++) { num += (lv[i] - mv) * (lc[i] - mc); dv += (lv[i] - mv) ** 2; dc += (lc[i] - mc) ** 2; }
      powerLawR = dv > 0 && dc > 0 ? num / Math.sqrt(dv * dc) : 0;
    }
  }

  // drag endpoint timing: within the buttons==1 (held) segment. The `up` is the first sample AFTER
  // the held span (buttons back to 0); grab = down→first-movement, release = last-movement→up.
  let grabMs: number | null = null, releaseMs: number | null = null;
  const heldIdx: number[] = [];
  for (let i = 0; i < n; i++) if ((s[i].buttons ?? 0) & 1) heldIdx.push(i);
  if (heldIdx.length) {
    const d0 = heldIdx[0];
    let firstMoveT = s[d0].t;
    for (const i of heldIdx) {
      if (Math.hypot(s[i].x - s[d0].x, s[i].y - s[d0].y) > 1) { firstMoveT = s[i].t; break; }
    }
    grabMs = firstMoveT - s[d0].t;
    let lastMoveIdx = d0;
    for (let k = 1; k < heldIdx.length; k++) {
      const i = heldIdx[k], j = heldIdx[k - 1];
      if (Math.hypot(s[i].x - s[j].x, s[i].y - s[j].y) > 1) lastMoveIdx = i;
    }
    const lastHeld = heldIdx[heldIdx.length - 1];
    const upT = lastHeld + 1 < n ? s[lastHeld + 1].t : s[lastHeld].t;
    releaseMs = upT - s[lastMoveIdx].t;
  }

  return {
    n, durationMs, peakVelFrac, velSkew, submovements, straightness, minJerkResidual,
    dtMeanMs, dtStdMs, dtUniform, jitterAutocorr, powerLawR, grabMs, releaseMs,
  };
}

export interface MotionScore { score: number; flags: string[]; features: MotionFeatures; }

/** Score 0..1 (higher = more human) with human-readable flags for the tells that fire. */
export function scoreMotion(s: Sample[], opts: { drag?: boolean } = {}): MotionScore {
  const f = extractFeatures(s);
  const flags: string[] = [];
  let score = 1;
  const penalize = (cond: boolean, amt: number, flag: string) => { if (cond) { score -= amt; flags.push(flag); } };

  // peak velocity should be asymmetric-human: penalize pure ease-out (very early) AND a too-symmetric
  // single-curve peak sitting right at the midpoint.
  penalize(f.peakVelFrac < 0.15, 0.15, `peak-velocity-ease-out(${f.peakVelFrac.toFixed(2)})`);
  penalize(f.peakVelFrac > 0.48, 0.12, `peak-velocity-too-symmetric(${f.peakVelFrac.toFixed(2)})`);
  // velocity profile should have the human right-skew (long homing tail), not a symmetric bell
  penalize(f.velSkew < 0.1, 0.12, `velocity-symmetric(skew=${f.velSkew.toFixed(2)})`);
  // at least one corrective submovement (Fitts homing); a single analytic curve has none
  penalize(f.submovements < 1, 0.2, "no-corrective-submovement");
  // …but not TOO many: an over-corrected, velocity-reversal-heavy reach (many interior speed minima)
  // is as non-human as a single smooth curve — real homing settles in 1–3 corrections.
  penalize(f.submovements > 4, 0.12, `too-many-submovements(${f.submovements})`);
  // dt should NOT look i.i.d.-uniform (real device sampling has a cadence mode)
  penalize(f.dtUniform, 0.15, "uniform-dt-cadence");
  // colored (not white) sub-pixel noise
  penalize(f.jitterAutocorr < 0.1, 0.1, `white-noise-jitter(${f.jitterAutocorr.toFixed(2)})`);
  // path shouldn't be a ruler-straight line
  penalize(f.straightness > 0.9995, 0.08, "perfectly-straight-path");

  if (opts.drag) {
    penalize(f.grabMs != null && f.grabMs < 60, 0.2, `grab-hesitation-too-short(${f.grabMs}ms)`);
    penalize(f.releaseMs != null && f.releaseMs < 40, 0.2, `release-delay-too-short(${f.releaseMs}ms)`);
  }

  return { score: Math.max(0, score), flags, features: f };
}

/** Turn a Step list (absolute coords + post-step sleep) into timestamped Samples for scoring.
 * `buttonsDuring` marks the held-button (drag) span [startIdx, endIdx) with buttons=1. */
export function stepsToSamples(
  steps: { x: number; y: number; sleepMs: number }[],
  buttonsDuring?: { start: number; end: number }
): Sample[] {
  const out: Sample[] = [];
  let t = 0;
  for (let i = 0; i < steps.length; i++) {
    const held = buttonsDuring && i >= buttonsDuring.start && i < buttonsDuring.end ? 1 : 0;
    out.push({ x: steps[i].x, y: steps[i].y, t, buttons: held });
    t += steps[i].sleepMs;
  }
  return out;
}

// =====================================================================================================
// SC 232 lane G: scorers for the other humanize surfaces (typed runs, wheel streams, drags, hover dwell)
// and one entry point, scoreAction(kind, samples). Port of sdk/python/clearcote/_motionscore.py (same
// thresholds, same penalties, same flag names); sdk/testdata/humanize_motion_vectors.json `scores` pins
// the three SDKs to identical scores.
//
// Sample shapes per kind (missing fields read as 0):
//   glide / ambient : {x, y, t[, buttons]}      drag : {x, y, t, buttons}  (buttons&1 = held)
//   typed           : {key, down, up}           wheel: {t, dy[, dx, mode]}
//   hover           : {x, y, t}  from the ARRIVAL on (first sample = arrival)
// =====================================================================================================

export type FingerPos = readonly [hand: number, finger: number];

// QWERTY finger map: hand 0 left / 1 right / 2 thumb (space); finger 0 pinky .. 3 index.
const FINGER = new Map<string, FingerPos>();
const FINGER_ROWS: [number, string[]][] = [[0, ["qaz1", "wsx2", "edc3", "rfvtgb45"]], [1, ["yhnujm67", "ik,8", "ol.9", "p;/[]'0-="]]];
for (const [hand, rows] of FINGER_ROWS) {
  rows.forEach((keys, f) => { for (const k of keys) FINGER.set(k, [hand, hand === 0 ? f : 3 - f]); });
}
FINGER.set(" ", [2, 4]);

/** (hand, finger) for a typed key on a QWERTY layout, or null for keys off the letter block. */
export function fingerOf(key: unknown): FingerPos | null {
  if (typeof key !== "string" || !key) return null;
  let k = key.toLowerCase();
  if (k === "space") k = " ";
  if (k.length !== 1) return null;
  return FINGER.get(k) ?? null;
}

const cv = (a: number[]): number => { const m = mean(a); return m > 1e-9 ? std(a) / m : 0; };
const median = (a: number[]): number => {
  if (!a.length) return 0;
  const s = [...a].sort((x, y) => x - y); const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const num = (o: any, k: string, d = 0): number => (o && o[k] != null ? Number(o[k]) : d);

export type ActionKind = "glide" | "move" | "click" | "pointer" | "drag" | "ambient" | "typed" | "type" | "keys" | "wheel" | "hover";
export interface ActionScore { kind: string; score: number; flags: string[]; features: Record<string, unknown>; }
export interface KeySample { key: string; down: number; up?: number; code?: string; }
export interface WheelSample { t: number; dy?: number; dx?: number; mode?: number; }

const done = (kind: string, score: number, flags: string[], features: Record<string, unknown>): ActionScore =>
  ({ kind, score: Math.max(0, Math.round(score * 1e4) / 1e4), flags, features });

const penalties = () => {
  const st = { score: 1, flags: [] as string[] };
  const pen = (cond: boolean, amt: number, flag: string) => { if (cond) { st.score -= amt; st.flags.push(flag); } };
  return { st, pen };
};

export function typedFeatures(s: KeySample[]) {
  const ev = s.filter((x) => x && typeof x === "object")
    .map((x) => ({ key: x.key, down: num(x, "down"), up: num(x, "up", num(x, "down")) }))
    .sort((a, b) => a.down - b.down);
  const n = ev.length;
  const gaps: number[] = [];
  for (let i = 1; i < n; i++) gaps.push(ev[i].down - ev[i - 1].down);
  const dwells = ev.map((e) => Math.max(0, e.up - e.down));
  let rollover = 0;
  for (let i = 1; i < n; i++) if (ev[i].down < ev[i - 1].up) rollover++;
  const same: number[] = [], alt: number[] = [];
  for (let i = 1; i < n; i++) {
    const a = fingerOf(ev[i - 1].key), b = fingerOf(ev[i].key);
    if (!a || !b || a[0] === 2 || b[0] === 2) continue;
    if (a[0] === b[0] && a[1] === b[1]) same.push(gaps[i - 1]);
    else if (a[0] !== b[0]) alt.push(gaps[i - 1]);
  }
  const ratio = same.length >= 3 && alt.length >= 3 && median(alt) > 0 ? median(same) / median(alt) : null;
  return {
    n,
    gapMeanMs: mean(gaps),
    gapCv: gaps.length >= 2 ? cv(gaps) : 0,
    gapMinMs: gaps.length ? Math.min(...gaps) : 0,
    dwellMeanMs: mean(dwells),
    dwellCv: dwells.length >= 2 ? cv(dwells) : 0,
    dwellBandFrac: dwells.length ? dwells.filter((d) => d >= 30 && d <= 200).length / dwells.length : 0,
    rolloverRate: n > 1 ? rollover / (n - 1) : 0,
    sameFingerN: same.length,
    alternatingN: alt.length,
    sameFingerRatio: ratio,
  };
}

/** A typed run: varied gaps (CV 0.15-1.6), dwell 30-200 ms and varied, some rollover on longer runs,
 *  same-finger bigrams slower than alternating-hand ones. */
export function scoreTyped(s: KeySample[]): ActionScore {
  const f = typedFeatures(s);
  const { st, pen } = penalties();
  const n = f.n;
  pen(n >= 3 && f.gapCv < 0.15, 0.25, `key-gap-too-regular(cv=${f.gapCv.toFixed(2)})`);
  pen(n >= 3 && f.gapCv > 1.6, 0.10, `key-gap-erratic(cv=${f.gapCv.toFixed(2)})`);
  pen(n >= 3 && f.gapMinMs < 8, 0.15, `key-gap-near-zero(${f.gapMinMs.toFixed(0)}ms)`);
  pen(n >= 2 && f.dwellBandFrac < 0.7, 0.15, `dwell-out-of-band(${f.dwellBandFrac.toFixed(2)} in 30-200ms)`);
  pen(n >= 3 && f.dwellCv < 0.08, 0.15, `dwell-constant(cv=${f.dwellCv.toFixed(2)})`);
  pen(f.sameFingerRatio != null && f.sameFingerRatio < 1.05, 0.15, `no-bigram-structure(same/alt=${(f.sameFingerRatio ?? 0).toFixed(2)})`);
  pen(n >= 15 && f.rolloverRate === 0, 0.10, "no-rollover");
  pen(n >= 5 && f.rolloverRate > 0.6, 0.10, `too-much-rollover(${f.rolloverRate.toFixed(2)})`);
  return done("typed", st.score, st.flags, f);
}

const NOTCH_PX = [40, 100, 120];

export function wheelFeatures(s: WheelSample[]) {
  const ev = s.filter((x) => x && typeof x === "object")
    .map((x) => ({ t: num(x, "t"), dy: num(x, "dy"), dx: num(x, "dx"), mode: Math.trunc(num(x, "mode")) }))
    .sort((a, b) => a.t - b.t);
  const n = ev.length;
  const mags = ev.map((e) => (e.dy ? Math.abs(e.dy) : Math.abs(e.dx)));
  const gaps: number[] = [];
  for (let i = 1; i < n; i++) gaps.push(ev[i].t - ev[i - 1].t);
  const distinct = new Set(mags.map((m) => Math.round(m * 1000) / 1000)).size;
  const lineMode = n > 0 && ev.every((e) => e.mode === 1);
  const notch = n > 0 && (lineMode || (distinct === 1 && NOTCH_PX.includes(mags[0])));
  const bursts: number[] = [], intra: number[] = [];
  if (n) {
    let size = 1;
    for (const g of gaps) { if (g >= 90) { bursts.push(size); size = 1; } else { intra.push(g); size++; } }
    bursts.push(size);
  }
  const tail = n >= 6 ? mags.slice(Math.max(1, Math.floor((2 * n) / 3))) : [];
  let dec = 0;
  for (let i = 1; i < tail.length; i++) if (tail[i] <= tail[i - 1]) dec++;
  const tailDec = tail.length >= 3 ? dec / (tail.length - 1) : 0;
  const gm = median(gaps);
  return {
    n, notch, lineMode, distinctDeltas: distinct,
    deltaMedianPx: median(mags), deltaMaxPx: mags.length ? Math.max(...mags) : 0,
    gapMedianMs: gm, gapCv: gaps.length >= 2 ? cv(gaps) : 0,
    intraBurstMedianMs: median(intra), burstSizes: bursts, flickResets: Math.max(0, bursts.length - 1),
    rateHz: gaps.length && gm > 0 ? 1000 / gm : 0, tailDecreasingFrac: tailDec,
  };
}

/** Wheel stream: notch cadence (bursts of 2-5 notches 10-80 ms apart, flick resets >= 90 ms) or trackpad
 *  pixel inertia (varied deltas <= 60 px at 45-150 Hz with a decaying tail). Both accepted. */
export function scoreWheel(s: WheelSample[]): ActionScore {
  const f = wheelFeatures(s);
  const { st, pen } = penalties();
  const n = f.n;
  if (n < 2) return done("wheel", st.score, st.flags, f);
  if (f.notch) {
    pen(n >= 4 && f.gapCv < 0.10, 0.25, `wheel-metronome(cv=${f.gapCv.toFixed(2)})`);
    pen(n >= 8 && f.flickResets === 0, 0.15, "no-flick-bursts");
    const mb = f.burstSizes.length ? Math.max(...f.burstSizes) : 0;
    pen(f.burstSizes.length > 0 && mb > 7, 0.10, `burst-too-long(${mb})`);
    const im = f.intraBurstMedianMs;
    pen(im > 0 && !(im >= 10 && im <= 80), 0.15, `wheel-cadence-off(${im.toFixed(0)}ms)`);
    pen(f.gapMedianMs < 4, 0.25, "wheel-same-tick");
  } else {
    pen(f.distinctDeltas <= 1, 0.20, "trackpad-constant-delta");
    pen(f.deltaMaxPx > 60, 0.15, `trackpad-delta-too-large(${f.deltaMaxPx.toFixed(0)}px)`);
    pen(n >= 6 && !(f.rateHz >= 45 && f.rateHz <= 150), 0.15, `trackpad-rate-off(${f.rateHz.toFixed(0)}Hz)`);
    pen(n >= 6 && f.tailDecreasingFrac < 0.6, 0.20, `no-inertia-tail(${f.tailDecreasingFrac.toFixed(2)})`);
    pen(n <= 8 && f.distinctDeltas === n && f.gapMedianMs >= 25, 0.25, "wheel-eased-slices");
  }
  return done("wheel", st.score, st.flags, f);
}

const btn = (s: Sample): number => s.buttons ?? 0;

export interface DragFeatures { heldN: number; startHesitationMs?: number; heldStraightness?: number; settleMs?: number; heldMs?: number; }

/** Start hesitation (ms until the pointer is >= 8 px from the press), held-path straightness, settle
 *  before release. */
export function dragFeatures(s: Sample[]): DragFeatures {
  const held: number[] = [];
  for (let i = 0; i < s.length; i++) if (btn(s[i]) & 1) held.push(i);
  if (!held.length) return { heldN: 0 };
  const p0 = s[held[0]];
  let hes: number | null = null;
  for (const i of held) if (Math.hypot(s[i].x - p0.x, s[i].y - p0.y) >= 8) { hes = s[i].t - p0.t; break; }
  if (hes == null) hes = s[held[held.length - 1]].t - p0.t;
  let path = 0;
  for (let k = 1; k < held.length; k++) path += Math.hypot(s[held[k]].x - s[held[k - 1]].x, s[held[k]].y - s[held[k - 1]].y);
  const last = s[held[held.length - 1]];
  const net = Math.hypot(last.x - p0.x, last.y - p0.y);
  let lastSig = held[0];
  for (let k = 1; k < held.length; k++) {
    if (Math.hypot(s[held[k]].x - s[held[k - 1]].x, s[held[k]].y - s[held[k - 1]].y) > 1) lastSig = held[k];
  }
  const li = held[held.length - 1];
  const upT = li + 1 < s.length ? s[li + 1].t : s[li].t;
  return {
    heldN: held.length,
    startHesitationMs: hes,
    heldStraightness: path > 1e-6 ? net / path : 1,
    settleMs: upT - s[lastSig].t,
    heldMs: last.t - p0.t,
  };
}

/** scoreMotion({drag: true}) plus the drag-specific tells. */
export function scoreDrag(s: Sample[]): ActionScore {
  const m = scoreMotion(s, { drag: true });
  const f = dragFeatures(s);
  const feats: Record<string, unknown> = { ...m.features, ...f };
  const st = { score: m.score, flags: [...m.flags] };
  const pen = (cond: boolean, amt: number, flag: string) => { if (cond) { st.score -= amt; st.flags.push(flag); } };
  if (!f.heldN) {
    pen(true, 0.5, "drag-no-held-span");
    return done("drag", st.score, st.flags, feats);
  }
  const g = f as Required<DragFeatures>;
  pen(g.startHesitationMs < 60, 0.20, `drag-start-no-hesitation(${g.startHesitationMs.toFixed(0)}ms)`);
  pen(g.startHesitationMs > 800, 0.05, `drag-start-too-slow(${g.startHesitationMs.toFixed(0)}ms)`);
  pen(g.heldN >= 6 && g.heldStraightness > 0.9995, 0.10, "drag-ruler-straight");
  pen(g.settleMs < 40, 0.15, `drag-no-settle(${g.settleMs.toFixed(0)}ms)`);
  pen(g.heldN < 10 && g.heldMs > 100, 0.15, `drag-too-few-held-moves(${g.heldN})`);
  return done("drag", st.score, st.flags, feats);
}

/** Samples from the arrival on: dwell, samples after arrival, excursion from the arrival point, drift. */
export function hoverFeatures(s: Sample[]) {
  const n = s.length;
  if (!n) return { n: 0, dwellMs: 0, afterN: 0, excursionPx: 0, driftPx: 0 };
  const a = s[0];
  let exc = 0;
  for (const p of s) exc = Math.max(exc, Math.hypot(p.x - a.x, p.y - a.y));
  return { n, dwellMs: s[n - 1].t - a.t, afterN: n - 1, excursionPx: exc, driftPx: Math.hypot(s[n - 1].x - a.x, s[n - 1].y - a.y) };
}

export function scoreHover(s: Sample[]): ActionScore {
  const f = hoverFeatures(s);
  const { st, pen } = penalties();
  pen(f.afterN < 3, 0.30, `hover-no-dwell-samples(${f.afterN})`);
  pen(f.dwellMs < 150, 0.20, `hover-dwell-short(${f.dwellMs.toFixed(0)}ms)`);
  pen(f.dwellMs > 1500, 0.05, `hover-dwell-long(${f.dwellMs.toFixed(0)}ms)`);
  pen(f.excursionPx > 3, 0.15, `hover-drift-large(${f.excursionPx.toFixed(1)}px)`);
  pen(f.afterN >= 3 && f.excursionPx === 0, 0.15, "hover-frozen");
  return done("hover", st.score, st.flags, f);
}

/** Idle / non-goal motion: only the device-stream tells apply (no Fitts homing to judge). */
export function scoreAmbient(s: Sample[]): ActionScore {
  const { st, pen } = penalties();
  if (s.length < 2) { pen(true, 0.5, "ambient-too-short"); return done("ambient", st.score, st.flags, { n: s.length }); }
  const f = extractFeatures(s);
  pen(f.durationMs < 200, 0.20, `ambient-too-short(${f.durationMs.toFixed(0)}ms)`);
  pen(f.dtUniform, 0.15, "uniform-dt-cadence");
  pen(f.n >= 8 && f.jitterAutocorr < 0.1, 0.10, `white-noise-jitter(${f.jitterAutocorr.toFixed(2)})`);
  pen(f.n >= 4 && f.straightness > 0.9995, 0.15, "perfectly-straight-path");
  return done("ambient", st.score, st.flags, { ...f });
}

/** One entry point for the score hook: kind is glide | move | drag | ambient | typed | wheel | hover. */
export function scoreAction(kind: ActionKind | string, samples: any[]): ActionScore {
  const k = (kind || "").toLowerCase();
  if (k === "glide" || k === "move" || k === "click" || k === "pointer") {
    const m = scoreMotion(samples as Sample[]);
    return done("glide", m.score, [...m.flags], { ...m.features });
  }
  if (k === "drag") return scoreDrag(samples as Sample[]);
  if (k === "ambient") return scoreAmbient(samples as Sample[]);
  if (k === "typed" || k === "type" || k === "keys") return scoreTyped(samples as KeySample[]);
  if (k === "wheel") return scoreWheel(samples as WheelSample[]);
  if (k === "hover") return scoreHover(samples as Sample[]);
  throw new Error(`unknown action kind: ${JSON.stringify(kind)}`);
}
