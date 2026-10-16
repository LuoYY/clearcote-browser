/**
 * The attention model (SC 232 lane C): what a reader does BETWEEN the actions a script asks for. A port of
 * sdk/python/clearcote/_attention.py (same values; pinned by sdk/testdata/humanize_after_vectors.json).
 *
 * Pure planning, no Playwright: humanize.ts calls these at the start of the NEXT action -- never from a
 * timer -- and sleeps / dispatches what they return.
 *  - After a `load` the first action waits a READ PAUSE of 0.8–4 s, scaled by how much text the page
 *    shows (measured in the isolated world, capped at READ_CAP_CHARS), and in EXPLORE_RATE of loads rolls
 *    the wheel 1–3 notches first.
 *  - When more than IDLE_GAP_S pass between two actions, the hand idles: 1–2 ambient drifts of 300–900 ms.
 * OFF by default: on with lane A's `ambient: "idle"` / `attention: true`, or `page._clearcoteAttention = true`.
 */
import { optionsOf } from "./hzoptions.js";
import { afterTraits, type Persona, type Rng } from "./motion.js";

export const READ_MIN_MS = 800;
export const READ_MAX_MS = 4000;
export const READ_CAP_CHARS = 20000;
export const EXPLORE_RATE = 0.3;
export const IDLE_GAP_S = 1.5;
export const IDLE_DRIFT_MIN_MS = 300;
export const IDLE_DRIFT_MAX_MS = 900;

/** Visible text length of the document, capped (run in the isolated world). Same source as Python's. */
export const TEXT_LENGTH = "() => { const b = document.body; if (!b) return 0; let t = ''; try { t = b.innerText || ''; }"
  + ` catch (e) { t = b.textContent || ''; } return Math.min(${READ_CAP_CHARS}, t.length); }`;

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** The read pause before the first action on a freshly loaded page (ms): READ_MIN_MS for an empty page,
 * READ_MAX_MS from READ_CAP_CHARS of text (square-root growth between), × the persona's read tempo ×
 * 0.85–1.15, kept in the band. */
export function readPauseMs(textLen: unknown, p: Persona, rng: Rng = Math.random): number {
  let n = Number(textLen ?? 0);
  if (!Number.isFinite(n)) n = 0;
  const frac = Math.sqrt(clamp(n / READ_CAP_CHARS, 0, 1));
  const ms = (READ_MIN_MS + (READ_MAX_MS - READ_MIN_MS) * frac) * afterTraits(p).readTempo * lerp(0.85, 1.15, rng());
  return clamp(ms, READ_MIN_MS, READ_MAX_MS);
}

/** 0 (most loads) or 1–3 exploratory wheel notches down, in EXPLORE_RATE of loads. */
export function exploreNotches(rng: Rng = Math.random): number {
  if (rng() >= EXPLORE_RATE) return 0;
  return 1 + Math.floor(rng() * 3);
}

/** The ambient drifts (ms each) the hand makes after `gapS` seconds without an action: none up to
 * IDLE_GAP_S, else 1–2 of IDLE_DRIFT_MIN_MS–IDLE_DRIFT_MAX_MS. */
export function idleDrifts(gapS: number | null | undefined, rng: Rng = Math.random): number[] {
  if (gapS === null || gapS === undefined || gapS <= IDLE_GAP_S) return [];
  const n = 1 + (rng() < 0.5 ? 1 : 0);
  const out: number[] = [];
  for (let i = 0; i < n; i++) out.push(lerp(IDLE_DRIFT_MIN_MS, IDLE_DRIFT_MAX_MS, rng()));
  return out;
}

/** A humanize option read off the page: the page's resolved HumanizeOptions (`optionsOf`), else a stand-in
 * `page._clearcoteOptions` object (the tests' seam), else `dflt`. Never throws. */
export function option(page: any, name: string, dflt: unknown = undefined): unknown {
  try {
    const o: any = optionsOf(page) ?? page?._clearcoteOptions;
    if (o === null || o === undefined || typeof o !== "object") return dflt;
    return name in o ? o[name] : dflt;
  } catch { return dflt; }
}

/** The page's ambient mode: lane A's `ambient` option ("off" | "load" | "idle"), else "off". A truthy
 * `attention` option maps to "idle". */
export function ambientMode(page: any): "off" | "load" | "idle" {
  const v = option(page, "ambient");
  if (typeof v === "string") {
    const m = v.trim().toLowerCase();
    if (m === "off" || m === "load" || m === "idle") return m;
  }
  if (option(page, "attention", false)) return "idle";
  return "off";
}
