// Wall-clock pacing for planned pointer paths (mirrors sdk/python/clearcote/_pacing.py).
//
// The motion planner (motion.ts planMove / planAmbient) returns samples {x, y, sleepMs} at the
// persona's device rate (~6-8 ms apart). Walking them as "dispatch, then sleep the planned gap" adds
// the dispatch round trip to EVERY sample: measured on PRO r38, one mouse.move costs 27 ms median, so a
// 1.05 s planned glide took 3.5-5.4 s and the page saw ~25 pointermove/s (SC 209).
//
// walk() schedules each sample at its planned offset from the start (the cumulative sum of the sleeps
// before it) and keeps that clock:
//   on time -> sleep until the sample's planned time, then dispatch it;
//   behind  -> skip the samples whose time has passed and dispatch the most recent due one, so the
//              path keeps the plan's shape and the move keeps the plan's duration;
//   never skipped: the final sample (the move lands exactly where it was aimed) and a sample marked
//              as changing button state (`pinned`);
//   never a teleport: a skip may not put more between two dispatched points than a fast hand covers
//              in the time between them (FAST_PX_PER_MS x dt, at least maxJump = 40 px); a longer hop
//              dispatches the in-between sample instead;
//   a stall is a pause: a single dispatch slower than STALL_MS shifts the schedule by the excess
//              instead of being caught up by skipping.
//
// Sleeps are local timers, never page.waitForTimeout (a CDP round trip a detector can score).
// Nothing here changes what the planner returns; only WHEN each sample goes out and which late ones
// are dropped.

export const MAX_JUMP_PX = 40;
// A single dispatch slower than this is a STALL (the page or the box was busy), not latency: nothing
// reached the page while it lasted, so the plan pauses with it instead of skipping ahead afterwards.
export const STALL_MS = 60;
// A fast hand: ~3 px/ms (a quick flick across a 1080p screen in ~0.6 s). A skip may hop as far as that
// covers in the time since the last dispatch, and never less than MAX_JUMP_PX.
export const FAST_PX_PER_MS = 3;

export interface PacedStep { x: number; y: number; sleepMs: number; }

export interface WalkOpts<S extends PacedStep> {
  now?: () => number;                       // ms, monotonic
  sleep?: (ms: number) => Promise<unknown>;
  maxJump?: number;
  pinned?: (s: S, i: number) => boolean;    // a step that changes button state: never skipped
}

const realNow = (): number => performance.now();
const realSleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, Math.max(0, ms)));

/** Planned dispatch offsets (ms from the start) of every step, and the planned total. */
export function schedule(steps: readonly PacedStep[]): { due: number[]; total: number } {
  const due: number[] = [];
  let t = 0;
  for (const s of steps) { due.push(t); t += Math.max(0, s.sleepMs); }
  return { due, total: t };
}

/** The step to dispatch next: step i is the first not yet sent, `elapsed` ms have passed. */
export function nextIndex<S extends PacedStep>(
  steps: readonly S[], due: readonly number[], i: number, last: { x: number; y: number } | null,
  elapsed: number, maxJump = MAX_JUMP_PX, pinned: (s: S, i: number) => boolean = () => false,
): number {
  const n = steps.length;
  let j = i;
  while (j + 1 < n && due[j + 1] <= elapsed && !pinned(steps[j], j)) j++;
  if (last) {
    while (j > i && Math.hypot(steps[j].x - last.x, steps[j].y - last.y) > maxJump) j--;
  }
  return j;
}

/** Dispatch `steps` through `move` against a wall clock. Stops at the first move that throws.
 *  Resolves to the number of samples dispatched. */
export async function walk<S extends PacedStep>(
  steps: readonly S[], move: (x: number, y: number) => Promise<unknown>, o: WalkOpts<S> = {},
): Promise<number> {
  const now = o.now ?? realNow;
  const sleep = o.sleep ?? realSleep;
  const maxJump = o.maxJump ?? MAX_JUMP_PX;
  const pinned = o.pinned ?? (() => false);
  if (!steps.length) return 0;
  const { due, total } = schedule(steps);
  let t0 = now();
  let tLast = t0;
  let i = 0, sent = 0;
  let last: { x: number; y: number } | null = null;
  while (i < steps.length) {
    const el = now() - t0;
    if (el < due[i]) await sleep(due[i] - el);
    else i = nextIndex(steps, due, i, last, el, jumpCap(maxJump, now() - tLast), pinned);
    const s = steps[i];
    const tb = now();
    try { await move(s.x, s.y); } catch { return sent; }
    sent++;
    t0 += stallMs(now() - tb);
    last = { x: s.x, y: s.y };
    tLast = tb;
    i++;
  }
  const el = now() - t0;
  if (el < total) await sleep(total - el);   // the last sample's own dwell (a settle hold, a frame)
  return sent;
}

/** The largest hop allowed between two dispatched points `dtMs` apart: what a fast hand covers in that
 *  time, never less than maxJump. */
export const jumpCap = (maxJump: number, dtMs: number): number => Math.max(maxJump, FAST_PX_PER_MS * Math.max(0, dtMs));

// Measured on PRO r38 under load: one move answered after 523 ms. Catching that up by skipping coarsened
// the rest of the glide to maximum hops, and across the fixture's iframe the page saw a 110 px jump. A
// single dispatch slower than STALL_MS is a stall: nothing reached the page while it lasted, so the plan
// pauses with it and only STALL_MS is caught up.
const stallMs = (took: number): number => (took > STALL_MS ? took - STALL_MS : 0);

/** px/ms arriving at sample k (0 for the first). */
export function speedAt(steps: readonly PacedStep[], k: number): number {
  if (k === 0) return 0;
  const a = steps[k - 1], b = steps[k];
  return Math.hypot(b.x - a.x, b.y - a.y) / Math.max(1e-3, a.sleepMs);
}

/**
 * Cut an ambient plan so it lasts about `ms`. planAmbient keeps adding whole excursions + rests until
 * its budget is spent and draws each rest sample's gap from 0.9-1.4x the frame time it counts, so its
 * plan runs well past what was asked. Of the samples whose end falls in [lo*ms, hi*ms], keep up to the
 * slowest one (inside a rest or the tail of a move). Only a tail is dropped; no sample is changed.
 *
 * The one exception to "about ms": when the whole window lies inside the first excursion (a short ask,
 * ~300 ms, against a ~0.5-1 s glide) every candidate is mid-flight, and a pointer that stops dead at
 * speed is a kinematic no hand produces. Then the cut moves forward to where that move has slowed down
 * (<= 10 % of the plan's peak speed), as long as that is within `stretch` x ms.
 */
export function trimTo<S extends PacedStep>(steps: S[], ms: number, lo = 0.85, hi = 1.10, stretch = 2): S[] {
  if (!steps.length || ms <= 0) return steps;
  const { due, total } = schedule(steps);
  if (total <= hi * ms) return steps;
  const end = steps.map((s, k) => due[k] + Math.max(0, s.sleepMs));
  let best = -1, bestV = Infinity;
  for (let k = 0; k < steps.length; k++) {
    if (end[k] < lo * ms) continue;
    if (end[k] > hi * ms) break;
    const v = speedAt(steps, k);
    if (v < bestV) { best = k; bestV = v; }
  }
  if (best < 0) {
    best = 0;
    for (let k = 0; k < steps.length; k++) if (end[k] <= hi * ms) best = k;
  }
  let peak = 0;
  for (let k = 0; k < steps.length; k++) peak = Math.max(peak, speedAt(steps, k));
  if (peak > 0 && speedAt(steps, best) > 0.3 * peak) {
    // First the rest of the +-20 % the caller was promised, then forward.
    let wk = -1, wv = Infinity;
    for (let k = 0; k < steps.length; k++) {
      if (end[k] < 0.8 * ms || end[k] > 1.15 * ms) continue;
      const v = speedAt(steps, k);
      if (v < wv) { wk = k; wv = v; }
    }
    if (wk >= 0 && wv <= 0.3 * peak) return steps.slice(0, wk + 1);
    for (let k = best + 1; k < steps.length; k++) {
      if (end[k] > stretch * ms) break;
      if (speedAt(steps, k) <= 0.1 * peak) { best = k; break; }
    }
  }
  return steps.slice(0, best + 1);
}
