/**
 * The persona browser window: maximized, snapped, or a restored rectangle, derived from a seed and the
 * final screen / work area.
 *
 * A bit-exact port of the engine's `components/ungoogled/persona_window_geometry.cc` (r39), which the
 * engine applies natively to every persona launch: the headless display becomes the persona's display
 * and the first window is created with the persona's bounds and show state, so `outerWidth` /
 * `outerHeight` / `screenX` / `screenY` are the real window's. The SDK uses this port in its tests
 * (the parity vector is the engine's own output) and to predict a persona's window.
 *
 * 64-bit arithmetic in BigInt, masked; every other value is a small integer, so Python, Node and .NET
 * agree on every rectangle.
 */

const MASK64 = (1n << 64n) - 1n;

export const WINDOW_ALGORITHM_VERSION = 1;
export const MIN_AVAIL_WIDTH = 800;
export const MIN_AVAIL_HEIGHT = 560;

const WINDOW_SALT = 0x5b1e7a3c9d24f681n;
const WINDOW_KEY_SALT = 0xc2b2ae3d27d4eb4fn;
const MIN_WIDTH = 900;
const MIN_HEIGHT = 600;
const MIN_SNAP_WIDTH = 900;
const MAX_TOP_OFFSET = 65;
const MAX_NEAR_OFFSET = 60;

export const WINDOW_PLATFORMS: Readonly<Record<string, number>> = { windows: 0, linux: 1, macos: 2 };

// [maximized, near, snap, restored] per-mille by work-area width: <= 1440, <= 1680, <= 1920, <= 2560,
// wider. Each row sums to 1000. Same table as the engine.
const WEIGHTS: ReadonlyArray<readonly [number, number, number, number]> = [
  [700, 50, 0, 250],
  [670, 50, 0, 280],
  [640, 50, 10, 300],
  [530, 50, 10, 410],
  [450, 50, 10, 490],
];

export type WindowKind = "maximized" | "near" | "snap-left" | "snap-right" | "restored";

export interface PersonaWindow {
  valid: boolean;
  state?: "maximized" | "normal";
  kind?: WindowKind;
  /** The normal (restored) rectangle in work-area coordinates; a maximized window restores here. */
  x: number;
  y: number;
  width: number;
  height: number;
}

function mix(z: bigint): bigint {
  z = ((z ^ (z >> 30n)) * 0xbf58476d1ce4e5b9n) & MASK64;
  z = ((z ^ (z >> 27n)) * 0x94d049bb133111ebn) & MASK64;
  return z ^ (z >> 31n);
}

class Stream {
  private state: bigint;
  constructor(state: bigint) { this.state = state & MASK64; }
  next(): bigint {
    this.state = (this.state + 0x9e3779b97f4a7c15n) & MASK64;
    return mix(this.state);
  }
  below(n: number): number { return Number(this.next() % BigInt(n)); }
  range(lo: number, hi: number): number { return lo + this.below(hi - lo + 1); }
}

const u32 = (v: number) => BigInt(v >>> 0);

function inputHash(seed: bigint, sw: number, sh: number, aw: number, ah: number, platform: number, salt: bigint): bigint {
  let h = mix((seed & MASK64) ^ salt);
  h = mix(h ^ ((u32(sw) << 32n) | u32(sh)));
  h = mix(h ^ ((u32(aw) << 32n) | u32(ah)));
  h = mix(h ^ ((BigInt(platform) << 8n) | BigInt(WINDOW_ALGORITHM_VERSION)));
  return h;
}

const permille = (total: number, pm: number) => Math.floor((total * pm) / 1000);
const clamp = (v: number, lo: number, hi: number) => (v < lo ? lo : v > hi ? hi : v);

function skewed(s: Stream): number {
  const a = s.range(0, 1000);
  const b = s.range(0, 1000);
  return Math.min(a, b);
}

function span(s: Stream, total: number, loPm: number, hiPm: number, floor: number): number {
  const lo = Math.min(total, Math.max(floor, permille(total, loPm)));
  const hi = Math.min(total, Math.max(lo, permille(total, hiPm)));
  return s.range(lo, hi);
}

function drawRestored(s: Stream, aw: number, ah: number, g: PersonaWindow): void {
  const minW = Math.min(aw, MIN_WIDTH);
  const minH = Math.min(ah, MIN_HEIGHT);
  let w = span(s, aw, 420, 900, minW);
  let h = s.below(10) < 8 ? span(s, ah, 780, 1000, minH) : span(s, ah, 450, 780, minH);
  const loW = Math.max(minW, Math.floor((11 * h + 9) / 10));
  const hiW = Math.min(aw, 2 * h);
  if (loW <= hiW) {
    w = clamp(w, loW, hiW);
  } else {
    h = clamp(h, Math.max(minH, Math.floor((w + 1) / 2)), Math.min(ah, Math.max(minH, Math.floor((10 * w) / 11))));
  }
  g.width = w;
  g.height = h;
  const slackX = aw - w;
  const slackY = ah - h;
  g.y = permille(Math.min(slackY, MAX_TOP_OFFSET), skewed(s));
  const where = s.below(100);
  if (where < 25) g.x = 0;
  else if (where < 35) g.x = permille(slackX, s.range(1, 100));
  else {
    const a = s.range(0, 1000);
    const b = s.range(0, 1000);
    g.x = permille(slackX, Math.floor((a + b) / 2));
  }
}

function platformCode(p: string | number | undefined): number {
  if (typeof p === "number") return p;
  return WINDOW_PLATFORMS[String(p ?? "windows").toLowerCase()] ?? 0;
}

/** The window for an identity seed on a work area; `{ valid: false }` when it is too small for a desktop. */
export function deriveWindow(
  seed: bigint, screenWidth: number, screenHeight: number, availWidth: number, availHeight: number,
  platform: string | number = "windows",
): PersonaWindow {
  const sw = Math.trunc(screenWidth), sh = Math.trunc(screenHeight);
  const aw = Math.trunc(availWidth), ah = Math.trunc(availHeight);
  const g: PersonaWindow = { valid: false, x: 0, y: 0, width: 0, height: 0 };
  if (aw < MIN_AVAIL_WIDTH || ah < MIN_AVAIL_HEIGHT || sw < aw || sh < ah) return g;
  const plat = platformCode(platform);
  const s = new Stream(inputHash(seed, sw, sh, aw, ah, plat, WINDOW_SALT));
  const [mx, near, snapW] = WEIGHTS[aw <= 1440 ? 0 : aw <= 1680 ? 1 : aw <= 1920 ? 2 : aw <= 2560 ? 3 : 4];
  const snap = plat === WINDOW_PLATFORMS.windows && Math.floor(aw / 2) >= MIN_SNAP_WIDTH ? snapW : 0;
  let pick = s.below(1000);
  g.valid = true;
  if ((pick -= mx) < 0) {
    g.state = "maximized";
    g.kind = "maximized";
    drawRestored(s, aw, ah, g);
    return g;
  }
  g.state = "normal";
  if ((pick -= near) < 0) {
    g.kind = "near";
    g.width = span(s, aw, 900, 990, Math.min(aw, MIN_WIDTH));
    g.height = span(s, ah, 900, 990, Math.min(ah, MIN_HEIGHT));
    g.x = permille(Math.min(aw - g.width, MAX_NEAR_OFFSET), s.range(0, 1000));
    g.y = permille(Math.min(ah - g.height, MAX_NEAR_OFFSET), s.range(0, 1000));
    return g;
  }
  if ((pick -= snap) < 0) {
    const right = s.below(5) < 2;
    g.kind = right ? "snap-right" : "snap-left";
    g.width = Math.floor(aw / 2);
    g.height = ah;
    g.x = right ? aw - g.width : 0;
    g.y = 0;
    return g;
  }
  g.kind = "restored";
  drawRestored(s, aw, ah, g);
  return g;
}

/** The engine's saved-placement tag value (PersonaWindowKey) for these inputs. */
export function windowKey(
  seed: bigint, screenWidth: number, screenHeight: number, availWidth: number, availHeight: number,
  platform: string | number = "windows",
): bigint {
  return inputHash(seed, Math.trunc(screenWidth), Math.trunc(screenHeight), Math.trunc(availWidth),
    Math.trunc(availHeight), platformCode(platform), WINDOW_KEY_SALT);
}

/** FNV-1a 64 of a UTF-8 string: the engine's PersonaSeedFromString for non-numeric values. */
export function fnv1a64(text: string): bigint {
  let h = 0xcbf29ce484222325n;
  for (const b of Buffer.from(String(text), "utf8")) {
    h ^= BigInt(b);
    h = (h * 0x100000001b3n) & MASK64;
  }
  return h;
}

/**
 * The engine's persona_seed for a `fingerprint` value: the value itself when it is an unsigned 64-bit
 * decimal, else FNV-1a 64, then one SplitMix64 step (MixedPersonaSeed).
 */
export function mixedSeed(raw: string | number | bigint): bigint {
  const s = String(raw);
  const seed = /^[0-9]+$/.test(s) && BigInt(s) <= MASK64 ? BigInt(s) : fnv1a64(s);
  return mix((seed + 0x9e3779b97f4a7c15n) & MASK64);
}
