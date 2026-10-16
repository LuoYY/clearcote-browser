/**
 * Wheel-driven scrolling for the humanizer: scroll-into-view, the shared wheel anchor, trackpad streams.
 * Port of Python `clearcote/_scroll.py` (same numbers; pinned by sdk/testdata/humanize_scroll_vectors.json).
 *
 * Measured on r42 / SDK 0.51.0 (SC 232 review): before a humanized click, fill or hold on an off-screen
 * target the SDK scrolled PROGRAMMATICALLY -- `scrollIntoViewIfNeeded` in the isolated world or an instant
 * `scrollBy` -- so the page saw a `scroll` event with no wheel event before it, and the target always
 * arrived at the exact centre of the viewport. A hand brings a target in with the wheel: whole notches (a
 * mouse) or a decaying pixel stream (a trackpad), undershooting then correcting, and leaves it somewhere
 * in the upper half of the viewport.
 *
 * Devices. Playwright's `mouse.wheel` sends pixel deltas, so a page sees `deltaMode 0` whatever the
 * persona; what tells a mouse from a trackpad is the SHAPE of the stream: a mouse persona sends whole
 * multiples of the platform notch (100 px Windows, 120 Linux, 40 macOS) in finger-flick bursts, a
 * trackpad persona 2-40 px events at 60-120 events/s on a min-jerk bell, then an inertia tail that decays
 * exponentially for at least 300 ms.
 */
import { gaussFrom, minJerk, wheelNotches, type Rng } from "./motion.js";

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
const jsRound = (x: number) => Math.floor(x + 0.5);

export interface ScrollBox { x: number; y: number; width: number; height: number; }
/** [x, y, width, height] */
export type View = [number, number, number, number];

// ------------------------------------------------------------------ the anchor (one set of numbers)
/** centre frac, sd frac, clamp lo, clamp hi -- the same in Python and .NET. */
export const ANCHOR_X: readonly [number, number, number, number] = [0.5, 0.10, 0.12, 0.88];
export const ANCHOR_Y: readonly [number, number, number, number] = [0.34, 0.09, 0.10, 0.62];
export const ANCHOR_TARGET_W = 140;
export const ANCHOR_MARGIN_PX = 8;
/** A resting hand's drift between wheel events: ONE native move, N(0, 1.5) x N(0, 1.2) px. */
export const DRIFT_SD: readonly [number, number] = [1.5, 1.2];

// ------------------------------------------------------------------ scroll-into-view
export const TARGET_FRAC: readonly [number, number] = [0.25, 0.60];
export const UNDERSHOOT: readonly [number, number] = [0.60, 0.90];
export const NOTCH_BUDGET = 60;
export const SETTLE_MS = 50;
export const SETTLE_CAP_MS = 1200;
export const MIN_MOVE_PX = 0.5;
const NOTCH_GAP_MS: readonly [number, number] = [18, 55];
const FLICK_GAP_MS: readonly [number, number] = [110, 320];
const FLICK_NOTCHES: readonly [number, number] = [2, 5];
const READ_PAUSE_RATE = 0.07;
const READ_PAUSE_MS: readonly [number, number] = [150, 450];

// ------------------------------------------------------------------ trackpad
export const TRACKPAD_HZ: readonly [number, number] = [60, 120];
export const TRACKPAD_PX: readonly [number, number] = [2, 40];
export const INERTIA_MIN_MS = 300;
export const INERTIA_STOP_PX = 1;
export const TRACKPAD_PAUSE_RATE = 0.2;
export const TRACKPAD_PAUSE_MS: readonly [number, number] = [120, 400];
export const TRACKPAD_STROKE_PX = 900;
export const TRACKPAD_PEAK_PX = 38;
export const TRACKPAD_GAP_MS: readonly [number, number] = [80, 200];
export const TRACKPAD_NUDGE_PX = 60;

/** The wheel anchor for a vw x vh viewport (two gaussians from `rng`, x first). */
export function anchorPoint(vw: number, vh: number, rng: Rng = Math.random): [number, number] {
  const [cx, sx, lx, hx] = ANCHOR_X;
  const [cy, sy, ly, hy] = ANCHOR_Y;
  const x = clamp(vw * cx + gaussFrom(rng) * vw * sx, vw * lx, vw * hx);
  const y = clamp(vh * cy + gaussFrom(rng) * vh * sy, vh * ly, vh * hy);
  return [x, y];
}

/** Should the pointer be re-homed before a wheel? Never with a button down; not when placed and inside. */
export function anchorNeeded(pos: [number, number], vw: number, vh: number, placed: boolean, held: boolean): boolean {
  if (held) return false;
  const [x, y] = pos;
  const m = ANCHOR_MARGIN_PX;
  return !(placed && m <= x && x <= vw - m && m <= y && y <= vh - m);
}

/** (dx, dy) of the hand's drift between wheel events, x first. */
export function wheelDrift(rng: Rng = Math.random): [number, number] {
  return [gaussFrom(rng) * DRIFT_SD[0], gaussFrom(rng) * DRIFT_SD[1]];
}

/** "trackpad" or "mouse": the caller's option when it names one, else a trackpad for a macOS persona. */
export function inputDevice(platform: string | null | undefined, option?: unknown): "trackpad" | "mouse" {
  const o = option !== undefined && option !== null ? String(option).trim().toLowerCase() : "";
  if (o === "trackpad" || o === "touchpad") return "trackpad";
  if (o === "mouse") return "mouse";
  return (platform || "").toLowerCase() === "macos" ? "trackpad" : "mouse";
}

export function targetFrac(rng: Rng = Math.random): number { return lerp(TARGET_FRAC[0], TARGET_FRAC[1], rng()); }

export function inView(box: ScrollBox, view: View): boolean {
  const [vx, vy, vw, vh] = view;
  return box.x >= vx && box.y >= vy && box.x + box.width <= vx + vw && box.y + box.height <= vy + vh;
}

/** (dx, dy) the content must move so `box` shows inside `view` (0 on an axis already in view). */
export function scrollNeed(box: ScrollBox, view: View, frac: number): [number, number] {
  const [vx, vy, vw, vh] = view;
  let dx = 0, dy = 0;
  if (box.y < vy || box.y + box.height > vy + vh)
    dy = box.height > vh * 0.9 ? box.y - (vy + vh * 0.05) : (box.y + box.height / 2) - (vy + vh * frac);
  if (box.x < vx || box.x + box.width > vx + vw)
    dx = box.width > vw * 0.9 ? box.x - (vx + vw * 0.05) : (box.x + box.width / 2) - (vx + vw / 2);
  return [dx, dy];
}

/** (dx, dy) still to scroll to where scrollNeed aimed, on the axes that were out of view at the start. */
export function aimNeed(box: ScrollBox, view: View, frac: number, ax: boolean, ay: boolean): [number, number] {
  const [vx, vy, vw, vh] = view;
  let dx = 0, dy = 0;
  if (ay) dy = box.height > vh * 0.9 ? box.y - (vy + vh * 0.05) : (box.y + box.height / 2) - (vy + vh * frac);
  if (ax) dx = box.width > vw * 0.9 ? box.x - (vx + vw * 0.05) : (box.x + box.width / 2) - (vx + vw / 2);
  return [dx, dy];
}

export function undershoot(needPx: number, rng: Rng = Math.random): number {
  return needPx * lerp(UNDERSHOOT[0], UNDERSHOOT[1], rng());
}

const sum = (a: number[]) => a.reduce((s, v) => s + v, 0);

function swipe(dist: number, dt: number, tauR: number, decayMs: number): [number[], number[]] {
  const build = (nfull: number, amp: number): [number[], number[]] => {
    const k = Math.max(2, Math.floor(nfull * tauR));
    const finger: number[] = [];
    let prev = 0;
    for (let i = 1; i <= k; i++) { const e = minJerk(i / nfull); finger.push(amp * (e - prev)); prev = e; }
    const vr = Math.max(finger[finger.length - 1], INERTIA_STOP_PX * 1.5);
    const tau = dist < TRACKPAD_NUDGE_PX ? decayMs
      : Math.max(decayMs, (INERTIA_MIN_MS * 1.15) / Math.log(vr / INERTIA_STOP_PX));
    const tail: number[] = [];
    let t = dt;
    while (tail.length < 400) {
      const v = vr * Math.exp(-t / tau);
      if (v < INERTIA_STOP_PX) break;
      tail.push(v);
      t += dt;
    }
    return [finger, tail];
  };
  let nfull = Math.max(4, jsRound(dist / 12));
  let finger: number[] = [], tail: number[] = [];
  for (let it = 0; it < 40; it++) {
    let amp = dist;
    for (let j = 0; j < 6; j++) {
      [finger, tail] = build(nfull, amp);
      const tot = sum(finger) + sum(tail);
      if (tot <= 0) break;
      amp *= dist / tot;
    }
    [finger, tail] = build(nfull, amp);
    if (Math.max(...finger) <= TRACKPAD_PEAK_PX || nfull >= 600) break;
    nfull = Math.ceil(nfull * 1.15);
  }
  const folded: number[] = [];
  let carry = 0;
  for (let v of finger) {
    v += carry;
    carry = 0;
    if (v < TRACKPAD_PX[0]) carry = v; else folded.push(v);
  }
  if (carry) { if (folded.length) folded[folded.length - 1] += carry; else folded.push(carry); }
  const rest = dist - sum(folded) - sum(tail);
  folded[folded.length - 1] += rest;
  if (folded[folded.length - 1] < TRACKPAD_PX[0]) {
    const k = Math.max(1, Math.floor(dist / 3));
    return [new Array(k).fill(dist / k), []];
  }
  return [folded, tail];
}

export interface WheelEv { dx: number; dy: number; sleepMs: number; }
export interface Swipe { finger: WheelEv[]; tail: WheelEv[]; gapMs: number; }

/** The swipes of a trackpad scroll of (dx, dy); see planTrackpad. Draw order as Python's. */
export function planTrackpadStrokes(dx: number, dy: number, rng: Rng = Math.random): Swipe[] {
  const d = Math.hypot(dx, dy);
  if (d < TRACKPAD_PX[0]) return dx || dy ? [{ finger: [{ dx, dy, sleepMs: 0 }], tail: [], gapMs: 0 }] : [];
  const ux = dx / d, uy = dy / d;
  const hz = lerp(TRACKPAD_HZ[0], TRACKPAD_HZ[1], rng());
  const dt = 1000 / hz;
  const n = Math.max(1, Math.ceil(d / TRACKPAD_STROKE_PX));
  const pause = rng() < TRACKPAD_PAUSE_RATE;
  const pauseMs = lerp(TRACKPAD_PAUSE_MS[0], TRACKPAD_PAUSE_MS[1], rng());
  const pauseSwipe = Math.min(n - 1, Math.floor(rng() * n));
  const pauseAt = lerp(0.25, 0.75, rng());
  const specs: [number, number, number, number][] = [];
  for (let i = 0; i < n; i++)
    specs.push([lerp(0.9, 1.1, rng()), lerp(0.55, 0.75, rng()), lerp(140, 220, rng()), lerp(TRACKPAD_GAP_MS[0], TRACKPAD_GAP_MS[1], rng())]);
  const totalShare = specs.reduce((a, s) => a + s[0], 0);
  const out: Swipe[] = [];
  let done = 0;
  for (let si = 0; si < n; si++) {
    const [share, tauR, decay, gap] = specs[si];
    const dist = si === n - 1 ? d - done : (d * share) / totalShare;
    done += dist;
    const [finger, tail] = swipe(dist, dt, tauR, decay);
    const stall = pause && si === pauseSwipe && finger.length > 2 ? Math.floor(pauseAt * (finger.length - 1)) : -1;
    const fEv: WheelEv[] = finger.map((v, i) => {
      let sl = dt * lerp(0.92, 1.08, rng());
      if (i === stall) sl += pauseMs;
      return { dx: ux * v, dy: uy * v, sleepMs: sl };
    });
    const tEv: WheelEv[] = tail.map((v) => ({ dx: ux * v, dy: uy * v, sleepMs: dt * lerp(0.92, 1.08, rng()) }));
    out.push({ finger: fEv, tail: tEv, gapMs: si < n - 1 ? gap : 0 });
  }
  return out;
}

/** A two-finger trackpad scroll of (dx, dy) px as wheel events in order (`sleepMs` = wait AFTER the
 * event). Per swipe (<= 900 px): 2-40 px events at 60-120/s on a min-jerk bell, lifted while moving,
 * then an inertia tail decaying for >= 300 ms until |delta| < 1; 20 % of scrolls stall once 120-400 ms. */
export function planTrackpad(dx: number, dy: number, rng: Rng = Math.random): WheelEv[] {
  const out: WheelEv[] = [];
  for (const sw of planTrackpadStrokes(dx, dy, rng)) {
    const evs = sw.finger.concat(sw.tail);
    if (evs.length && sw.gapMs) evs[evs.length - 1] = { ...evs[evs.length - 1], sleepMs: evs[evs.length - 1].sleepMs + sw.gapMs };
    out.push(...evs);
  }
  return out;
}

// ------------------------------------------------------------------ isolated-world read
/** The nearest scrollable ancestor of a plain-CSS selector's first match: its visible client rect and the
 * axes it scrolls, or null when the window scrolls it (same source as Python `_scroll.SCROLLER`). */
export const ISO_SCROLLER = `(a) => { let e;
  try { e = document.querySelector(a.sel); } catch (x) { return null; }
  if (!e) return null;
  const top = document.scrollingElement || document.documentElement;
  for (let p = e.parentElement; p && p !== top && p !== document.body; p = p.parentElement) {
    const s = getComputedStyle(p), sc = v => v === 'auto' || v === 'scroll' || v === 'overlay';
    const y = sc(s.overflowY) && p.scrollHeight > p.clientHeight + 1;
    const x = sc(s.overflowX) && p.scrollWidth > p.clientWidth + 1;
    if (x || y) { const r = p.getBoundingClientRect();
      return { rect: [r.x + p.clientLeft, r.y + p.clientTop, p.clientWidth, p.clientHeight], x: x, y: y }; }
  }
  return null; }`;

// ------------------------------------------------------------------ the I/O loop
export interface Scroller { rect: [number, number, number, number]; x: boolean; y: boolean; }

/** What bringIntoView needs from the humanizer. */
export interface ScrollIO {
  rng?: Rng;
  viewport(): Promise<{ width: number; height: number }>;
  pointer(): [number, number];
  placed(): boolean;
  held(): boolean;
  glide(x: number, y: number, targetW: number): Promise<void>;
  /** ONE native wheel event */
  wheel(dx: number, dy: number): Promise<void>;
  sleep(ms: number): Promise<void>;
  notchPx(): Promise<number>;
  device(): Promise<"mouse" | "trackpad">;
}

async function readSettled(boxFn: () => Promise<ScrollBox | null>, io: ScrollIO): Promise<ScrollBox | null> {
  let a = await boxFn();
  const t0 = Date.now();
  for (;;) {
    await io.sleep(SETTLE_MS);
    const b = await boxFn();
    if (a === null || b === null) return b;
    if (Math.abs(a.x - b.x) < 0.01 && Math.abs(a.y - b.y) < 0.01) return b;
    if (Date.now() - t0 > SETTLE_CAP_MS) return b;
    a = b;
  }
}

async function sendNotches(io: ScrollIO, n: number, axis: "x" | "y", sign: number, rng: Rng): Promise<void> {
  const notch = await io.notchPx();
  let burst = Math.floor(lerp(FLICK_NOTCHES[0], FLICK_NOTCHES[1] + 1, rng()));
  for (let k = 0; k < n; k++) {
    await io.wheel(axis === "x" ? sign * notch : 0, axis === "y" ? sign * notch : 0);
    if (k === n - 1) break;
    if (--burst === 0) {
      await io.sleep(lerp(FLICK_GAP_MS[0], FLICK_GAP_MS[1], rng()));
      burst = Math.floor(lerp(FLICK_NOTCHES[0], FLICK_NOTCHES[1] + 1, rng()));
    } else await io.sleep(lerp(NOTCH_GAP_MS[0], NOTCH_GAP_MS[1], rng()));
    if (rng() < READ_PAUSE_RATE) await io.sleep(lerp(READ_PAUSE_MS[0], READ_PAUSE_MS[1], rng()));
  }
}

async function sendTrackpad(io: ScrollIO, dx: number, dy: number, rng: Rng): Promise<void> {
  for (const e of planTrackpad(dx, dy, rng)) {
    await io.wheel(e.dx, e.dy);
    if (e.sleepMs) await io.sleep(e.sleepMs);
  }
}

async function viewOf(io: ScrollIO, scroller: Scroller | null): Promise<View> {
  const vp = await io.viewport();
  if (!scroller) return [0, 0, vp.width, vp.height];
  const [x, y, w, h] = scroller.rect;
  const x0 = Math.max(0, x), y0 = Math.max(0, y);
  const x1 = Math.min(vp.width, x + w), y1 = Math.min(vp.height, y + h);
  return [x0, y0, Math.max(0, x1 - x0), Math.max(0, y1 - y0)];
}

async function placeOver(io: ScrollIO, view: View, rng: Rng): Promise<void> {
  const [vx, vy, vw, vh] = view;
  if (vw <= 0 || vh <= 0) return;
  const [px, py] = io.pointer();
  if (!anchorNeeded([px - vx, py - vy], vw, vh, io.placed(), io.held())) return;
  const [ax, ay] = anchorPoint(vw, vh, rng);
  await io.glide(vx + ax, vy + ay, ANCHOR_TARGET_W);
  await io.sleep(lerp(60, 180, rng()));   // eyes land before the finger rolls the wheel
}

export interface BringResult { box: ScrollBox | null; ok: boolean; notches: number; }

/** Wheel the target whose current box `boxFn()` returns into view (see Python `bring_into_view`):
 * anchor (or over the nearest scrollable ancestor `scrollerFn()` names; an off-screen container is wheeled
 * in first), whole notches or a trackpad swipe for 60-90 % of the need, settle (two equal reads 50 ms
 * apart), re-read, correct; at most 60 notches. `ok` false: the wheel did not move it or the budget ran out. */
export async function bringIntoView(io: ScrollIO, boxFn: () => Promise<ScrollBox | null>, frac?: number | null,
  rng?: Rng, scrollerFn?: (() => Promise<Scroller | null>) | null): Promise<BringResult> {
  const r: Rng = rng ?? io.rng ?? Math.random;
  let box = await boxFn();
  if (!box) return { box: null, ok: false, notches: 0 };
  let scroller = scrollerFn ? await scrollerFn() : null;
  if (scroller) {
    const vp = await io.viewport();
    const sr = scroller.rect;
    if (!inView({ x: sr[0], y: sr[1], width: sr[2], height: sr[3] }, [0, 0, vp.width, vp.height])) {
      const srect = async (): Promise<ScrollBox | null> => {
        const s = await scrollerFn!();
        return s ? { x: s.rect[0], y: s.rect[1], width: s.rect[2], height: s.rect[3] } : null;
      };
      const first = await bringIntoView(io, srect, frac, r, null);
      if (!first.ok) return { box: await boxFn(), ok: false, notches: 0 };
      scroller = await scrollerFn!();
      box = await boxFn();
      if (!box) return { box: null, ok: false, notches: 0 };
    }
  }
  let view = await viewOf(io, scroller);
  if (inView(box, view)) return { box, ok: true, notches: 0 };
  const f = frac ?? targetFrac(r);
  let notches = 0;
  let first = true;
  const device = await io.device();
  const notch = await io.notchPx();
  let placed = false;
  const need0 = scrollNeed(box, view, f);
  let ax = need0[0] !== 0, ay = need0[1] !== 0;
  if (scroller) { ax = ax && !!scroller.x; ay = ay && !!scroller.y; }
  const tol = device !== "trackpad" ? notch * 0.5 : 6;
  let shownRounds = 0;
  while (notches < NOTCH_BUDGET) {
    const [dx, dy] = aimNeed(box!, view, f, ax, ay);
    if (inView(box!, view)) {
      if ((Math.abs(dx) < tol && Math.abs(dy) < tol) || shownRounds >= 2) return { box, ok: true, notches };
      shownRounds++;
    } else if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return { box, ok: false, notches };
    if (!placed) { await placeOver(io, view, r); placed = true; }
    const askX = first ? undershoot(dx, r) : dx;
    const askY = first ? undershoot(dy, r) : dy;
    first = false;
    if (device === "trackpad") {
      await sendTrackpad(io, askX, askY, r);
      notches += Math.max(1, jsRound(Math.hypot(askX, askY) / notch));
    } else {
      let ny = Math.abs(askY) >= tol ? wheelNotches(askY, notch) : 0;
      let nx = Math.abs(askX) >= tol ? wheelNotches(askX, notch) : 0;
      ny = Math.min(ny, NOTCH_BUDGET - notches);
      if (ny) { await sendNotches(io, ny, "y", askY > 0 ? 1 : -1, r); notches += ny; }
      nx = Math.min(nx, NOTCH_BUDGET - notches);
      if (nx) { await sendNotches(io, nx, "x", askX > 0 ? 1 : -1, r); notches += nx; }
      if (!(nx || ny)) return { box, ok: inView(box!, view), notches };
    }
    const before: ScrollBox = box!;
    box = await readSettled(boxFn, io);
    if (!box) return { box: null, ok: false, notches };
    if (scroller) {
      scroller = scrollerFn ? await scrollerFn() : scroller;
      view = await viewOf(io, scroller);
    }
    if (Math.abs(box.x - before.x) < MIN_MOVE_PX && Math.abs(box.y - before.y) < MIN_MOVE_PX)
      return { box, ok: inView(box, view), notches };
  }
  return { box, ok: false, notches };
}
