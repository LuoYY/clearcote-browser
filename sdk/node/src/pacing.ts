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
//   never a teleport: a skip that would put more than maxJump px between two dispatched points
//              dispatches the in-between sample instead (a fast hand covers ~40 px per 25 ms).
//
// Sleeps are local timers, never page.waitForTimeout (a CDP round trip a detector can score).
// Nothing here changes what the planner returns; only WHEN each sample goes out and which late ones
// are dropped.

export const MAX_JUMP_PX = 40;

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
  const t0 = now();
  let i = 0, sent = 0;
  let last: { x: number; y: number } | null = null;
  while (i < steps.length) {
    const el = now() - t0;
    if (el < due[i]) await sleep(due[i] - el);
    else i = nextIndex(steps, due, i, last, el, maxJump, pinned);
    const s = steps[i];
    try { await move(s.x, s.y); } catch { return sent; }
    sent++;
    last = { x: s.x, y: s.y };
    i++;
  }
  const el = now() - t0;
  if (el < total) await sleep(total - el);   // the last sample's own dwell (a settle hold, a frame)
  return sent;
}

/**
 * Cut an ambient plan so it lasts about `ms`. planAmbient keeps adding whole excursions + rests until
 * its budget is spent and draws each rest sample's gap from 0.9-1.4x the frame time it counts, so its
 * plan runs well past what was asked. Of the samples whose end falls in [lo*ms, hi*ms], keep up to the
 * slowest one (inside a rest or the tail of a move, so the cursor is not frozen mid-flight). Only a
 * tail is dropped; no sample is changed.
 */
export function trimTo<S extends PacedStep>(steps: S[], ms: number, lo = 0.85, hi = 1.10): S[] {
  if (!steps.length || ms <= 0) return steps;
  const { due, total } = schedule(steps);
  if (total <= hi * ms) return steps;
  let best = -1, bestV = Infinity;
  for (let k = 0; k < steps.length; k++) {
    const end = due[k] + Math.max(0, steps[k].sleepMs);
    if (end < lo * ms) continue;
    if (end > hi * ms) break;
    const v = k === 0 ? 0
      : Math.hypot(steps[k].x - steps[k - 1].x, steps[k].y - steps[k - 1].y) / Math.max(1e-3, steps[k - 1].sleepMs);
    if (v < bestV) { best = k; bestV = v; }
  }
  if (best < 0) {
    best = 0;
    for (let k = 0; k < steps.length; k++) if (due[k] + Math.max(0, steps[k].sleepMs) <= hi * ms) best = k;
  }
  return steps.slice(0, best + 1);
}
