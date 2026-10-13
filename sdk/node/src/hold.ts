/**
 * Press and hold: one long, humanized button press on an element. Port of the Python
 * `clearcote/_hold.py` (same plan numbers; Python and .NET follow the same plan).
 *
 * For controls that act only while the button stays down: a hold-to-confirm button, a long-press
 * menu, a "press and hold" verification button. `page.pressAndHold(selector)` /
 * `locator.pressAndHold()` (with `humanize: true`), or `pressAndHold(locator)` on any page.
 *
 * What it does, in order:
 *
 *   1. waits for the element to be visible and brings it into view (the humanized wheel scroll on a
 *      humanized main-frame page, Playwright's scroll otherwise);
 *   2. moves the pointer to a point inside it (a persona-shaped point near the centre, or the
 *      caller's `position`) -- a humanized glide when humanize is on;
 *   3. presses (after the humanized pre-press pause) and holds for `duration` ms, or until
 *      `until()` is true -- then lets go after a human reaction delay (160-350 ms), not on the
 *      same tick the condition flipped;
 *   4. while the button is down, a resting hand still moves the mouse now and then: sparse whole-pixel
 *      drift, never more than 2 px from the press point (`tremor: false` turns it off). Every one of
 *      those moves is a trusted move with the button reported held.
 *
 * The button is always released, also when `until` throws. The plan functions are pure (no I/O).
 */
import type { Locator } from "playwright-core";
import { clickPoint, type Box, type Persona, type Point, type Rng } from "./motion.js";

export const DEFAULT_HOLD_MS: readonly [number, number] = [2800, 3800];
/** `until` with no `duration`: give up after this long. */
export const UNTIL_CAP_MS = 15000;
/** How often `until` is asked. */
export const POLL_MS = 100;
/** Drift never leaves this square around the press point. */
export const TREMOR_RADIUS_PX = 2;
export const REACTION_MS: readonly [number, number] = [160, 350];

export type HoldButton = "left" | "right" | "middle";

export interface PressAndHoldOptions {
  /** How long the button stays down, in ms. Default: 2.8-3.8 s; with `until`, the longest the hold
   * may last (15 s when not given). */
  duration?: number;
  /** The point to press, relative to the element's top-left (as Playwright's `position`). */
  position?: { x: number; y: number };
  button?: HoldButton;
  /** For the element to become visible, in ms (undefined = the page default). */
  timeout?: number;
  /** Polled every 100 ms while held; once it returns truthy the button is let go (after a human
   * reaction delay). May be async. */
  until?: () => boolean | Promise<boolean>;
  /** Sparse 1 px drift while held, within 2 px of the press point. Default true. */
  tremor?: boolean;
}

/** How long the button stays down: the caller's `duration` (ms) exactly; else, with `until`, the cap
 * after which the hold gives up; else a fresh 2.8-3.8 s. */
export function holdMs(duration?: number | string | null, until?: unknown, rng: Rng = Math.random): number {
  if (duration !== undefined && duration !== null) {
    const d = typeof duration === "number" ? duration
      : typeof duration === "string" && duration.trim() !== "" ? Number(duration) : NaN;
    if (Number.isNaN(d) && typeof duration !== "number") {
      throw new TypeError("pressAndHold: duration must be a number of milliseconds");
    }
    if (!Number.isFinite(d) || d <= 0) {
      throw new RangeError("pressAndHold: duration must be a positive number of milliseconds");
    }
    return d;
  }
  if (until !== undefined && until !== null) return UNTIL_CAP_MS;
  const [lo, hi] = DEFAULT_HOLD_MS;
  return lo + rng() * (hi - lo);
}

/** Whole-pixel offsets from the press point, as [tMs, dx, dy], while the button is held. A first move
 * 250-650 ms in, then one every 250-900 ms; each moves one pixel on x and/or y and stays within
 * TREMOR_RADIUS_PX of the press point (a step that would leave it goes back toward it). None in the
 * last 120 ms: the hand is already letting go. */
export function tremorPlan(totalMs: number, rng: Rng = Math.random): [number, number, number][] {
  const out: [number, number, number][] = [];
  let ox = 0, oy = 0;
  let t = 250 + rng() * 400;
  while (t < totalMs - 120 && out.length < 1000) {
    let dx = 0, dy = 0;
    for (;;) {
      dx = Math.floor(rng() * 3) - 1;
      dy = Math.floor(rng() * 3) - 1;
      if (dx || dy) break;
    }
    let nx = ox + dx, ny = oy + dy;
    if (Math.abs(nx) > TREMOR_RADIUS_PX) nx = ox - (nx > 0 ? 1 : -1);
    if (Math.abs(ny) > TREMOR_RADIUS_PX) ny = oy - (ny > 0 ? 1 : -1);
    ox = nx; oy = ny;
    out.push([t, ox, oy]);
    t += 250 + rng() * 650;
  }
  return out;
}

export function reactionMs(rng: Rng = Math.random): number {
  return REACTION_MS[0] + rng() * (REACTION_MS[1] - REACTION_MS[0]);
}

/** The viewport point to press: `position` (relative to the element's top-left, as Playwright's
 * `position`), else the persona's click point when there is one, else the centre. */
export function holdPoint(box: Box, from?: Point | null, persona?: Persona | null,
  position?: { x: number; y: number } | null): [number, number] {
  if (position) return [box.x + Number(position.x), box.y + Number(position.y)];
  if (persona && from) {
    const p = clickPoint(box, from, persona);
    return [p.x, p.y];
  }
  return [box.x + box.width / 2, box.y + box.height / 2];
}

/** @internal exported for tests. */
export function holdButton(button?: string | null): HoldButton {
  if (button !== undefined && button !== null && button !== "left" && button !== "right" && button !== "middle") {
    throw new TypeError("pressAndHold: button must be 'left', 'right' or 'middle'");
  }
  return (button || "left") as HoldButton;
}

/** Does `loc` target the page's main frame (so the humanized box/scroll helpers apply)? */
function mainFrameLocator(loc: any): boolean {
  try {
    if (String(loc._selector ?? "").includes("internal:control=enter-frame")) return false;
    return loc._frame === loc.page().mainFrame();
  } catch { return false; }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Press and hold the element `locator` for `opts.duration` ms (default 2.8-3.8 s). Works on any page,
 * humanized or not, and on frame locators: it presses at viewport coordinates through `page.mouse`
 * (a humanized glide and pre-press pause when the page has `humanize: true`).
 *
 * Resolves true when the hold ran its full `duration` or `until` became true, false when `until`
 * never did. The button is always released. A bad `duration` or `button` throws before anything moves.
 */
export async function pressAndHold(locator: Locator, opts: PressAndHoldOptions = {}): Promise<boolean> {
  const o = opts ?? {};
  const btn = holdButton(o.button);
  const until = o.until;
  if (until !== undefined && until !== null && typeof until !== "function") {
    throw new TypeError("pressAndHold: until must be a function");
  }
  const total = holdMs(o.duration, until);
  const loc: any = locator;
  const page: any = loc.page();
  let box: Box | null = null;
  const boxOf = page._clearcoteBoxOf;
  if (typeof boxOf === "function" && mainFrameLocator(loc)) {
    try {
      box = await boxOf(loc, o.timeout, loc._selector ?? null);
    } catch (e) {
      if ((e as any)?.name === "TimeoutError") throw e;
      box = null;
    }
  }
  if (!box) {
    await loc.waitFor({ state: "visible", timeout: o.timeout });
    await loc.scrollIntoViewIfNeeded({ timeout: o.timeout });
    box = await loc.boundingBox();
  }
  if (!box) throw new Error("pressAndHold: the element has no box (not rendered)");
  const persona: Persona | undefined = page._clearcotePersona;
  const posOf = page._clearcotePointer;
  let [x, y] = holdPoint(box, typeof posOf === "function" ? posOf() : null, persona, o.position);
  x = Math.round(x); y = Math.round(y);
  const nudge: (x: number, y: number) => Promise<void> =
    typeof page._clearcoteNudge === "function" ? page._clearcoteNudge : (nx, ny) => page.mouse.move(nx, ny);

  await page.mouse.move(x, y);             // a humanized glide when humanize is on
  await page.mouse.down({ button: btn });  // humanized: the pre-press pause first
  const t0 = performance.now();
  const elapsed = () => performance.now() - t0;
  let done = !until;
  try {
    const drift = o.tremor === false ? [] : tremorPlan(total);
    let i = 0;
    let nextPoll = 0;
    for (;;) {
      const now = elapsed();
      if (now >= total) break;
      if (until && now >= nextPoll) {
        nextPoll = now + POLL_MS;
        if (await until()) {
          done = true;
          await sleep(reactionMs());
          break;
        }
      }
      while (i < drift.length && drift[i][0] <= elapsed()) {
        await nudge(x + drift[i][1], y + drift[i][2]);
        i++;
      }
      let wake = total;
      if (i < drift.length) wake = Math.min(wake, drift[i][0]);
      if (until) wake = Math.min(wake, nextPoll);
      await sleep(Math.max(0, Math.min(wake - elapsed(), 50)));
    }
  } finally {
    await page.mouse.up({ button: btn });  // never leave the button down
  }
  return done;
}
