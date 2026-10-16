// Pure keyboard planning for humanized typing (lane D of SC 232): a faithful port of the Python SDK's
// clearcote/_keys.py, mirrored in .NET Keys.cs. Nothing here touches a page: given a persona seed and an
// RNG it decides how long a hand waits between two keys (a bigram flight model on the persona's WPM), how
// a chord splits into modifiers and key, how an OS auto-repeats a held key, and the pause before Enter or
// Tab after typing. sdk/testdata/humanize_typing_vectors.json pins the three SDKs to identical answers.
//
// The typing identity comes from a SALTED side stream of the persona seed (mulberry32(seed ^ salt)), like
// rolloverRate and shapeTraits, so makePersona's draw order -- every existing seed's motor identity -- is
// unchanged.
//
// Measured before this existed (SDK 0.51.0): Node's inter-key gap was uniform 45-150 ms with the 6 %
// thinking pause REPLACING the gap, while Python and .NET drew max(25, N(85, 45)) plus the pause -- one seed,
// two cadences; every bigram took the same time; a chord went down as one press (modifier and key within
// ~1 ms); Enter followed the last character after one key gap.

import { gaussFrom, mulberry32, type Rng } from "./motion.js";

const KEYS_SALT = 0xd3a2646c; // own stream (was 0x27d4eb2f = the aim salt: WPM moved with the aim spread)
const NOMINAL_DWELL_MS = 80;
export const MIN_FLIGHT_MS = 25;

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

/** Per-identity typing signature, stable per seed (typingTraits). */
export interface TypingTraits {
  wpm: number;            // 28..75
  sameFinger: number;     // 1.5..1.9
  sameHand: number;       // 1.1..1.3
  alternate: number;      // 0.85..1.0
  tabPref: number;        // 0.60..0.85
  typoRate: number;       // 0.005..0.04
  submitCenterMs: number; // 400..750
  tabCenterMs: number;    // 190..330
  selectStyle: "triple" | "keys";
  blurStyle: "tab" | "click";
  repeatFirstMs: number;  // the OS key auto-repeat DELAY, 252..498 (a machine setting: one per identity)
  repeatGapMs: number;    // the OS key auto-repeat INTERVAL, 26..39 (likewise; planRepeat adds +-2 / +-1 ms)
}

/** The typing identity of persona seed `seedInt` (Persona.seed). Draw order as _keys.typing_traits. */
export function typingTraits(seedInt: number): TypingTraits {
  const r = mulberry32((seedInt ^ KEYS_SALT) >>> 0);
  const wpm = lerp(28, 75, r());
  const sameFinger = lerp(1.5, 1.9, r());
  const sameHand = lerp(1.1, 1.3, r());
  const alternate = lerp(0.85, 1.0, r());
  const tabPref = lerp(0.6, 0.85, r());
  const typoRate = lerp(0.005, 0.04, r());
  const submitCenterMs = lerp(400, 750, r());
  const tabCenterMs = lerp(190, 330, r());
  const selectStyle = r() < 0.55 ? "triple" : "keys";
  const blurStyle = r() < 0.5 ? "tab" : "click";
  const repeatFirstMs = lerp(252, 498, r()); // the two repeat draws came last (R1): every earlier trait is unchanged
  const repeatGapMs = lerp(26, 39, r());
  return { wpm, sameFinger, sameHand, alternate, tabPref, typoRate, submitCenterMs, tabCenterMs, selectStyle, blurStyle,
    repeatFirstMs, repeatGapMs };
}

// US QWERTY touch-typing fingers: 0-3 left pinky..index, 4-7 right index..pinky; rows 0 (digits)..3.
const ROWS = ["`1234567890-=", "qwertyuiop[]\\", "asdfghjkl;'", "zxcvbnm,./"];
const FINGERS = [
  [0, 0, 1, 2, 3, 3, 4, 4, 5, 6, 7, 7, 7],
  [0, 1, 2, 3, 3, 4, 4, 5, 6, 7, 7, 7, 7],
  [0, 1, 2, 3, 3, 4, 4, 5, 6, 7, 7],
  [0, 1, 2, 3, 3, 4, 4, 5, 6, 7],
];
const SHIFTED = '~!@#$%^&*()_+{}|:"<>?';
const SHIFT_BASE = "`1234567890-=[]\\;',./";
const FINGER_MAP = new Map<string, [number, number]>();
ROWS.forEach((keys, row) => Array.from(keys).forEach((k, i) => FINGER_MAP.set(k, [FINGERS[row][i], row])));

/** [finger 0..7, row 0..3] of the key that types `ch`, or null. Capitals / shifted symbols: their base key. */
export function fingerOf(ch: string | null | undefined): [number, number] | null {
  if (!ch || ch.length !== 1) return null;
  let c = ch.toLowerCase();
  const j = SHIFTED.indexOf(c);
  if (j >= 0) c = SHIFT_BASE[j];
  return FINGER_MAP.get(c) ?? null;
}

export type BigramClass = "same_finger" | "same_hand" | "alternate" | "none";

/** How the hand gets from `prev` to `ch`, and whether it is a two-row reach on the same hand. */
export function bigramClass(prev: string | null | undefined, ch: string | null | undefined): [BigramClass, boolean] {
  const a = fingerOf(prev), b = fingerOf(ch);
  if (!a || !b) return ["none", false];
  if (a[0] === b[0]) return ["same_finger", Math.abs(a[1] - b[1]) >= 2];
  if ((a[0] < 4) === (b[0] < 4)) return ["same_hand", Math.abs(a[1] - b[1]) >= 2];
  return ["alternate", false];
}

const PUNCT = ".,;:!?";
const isWs = (c: string | null | undefined) => c === " " || c === "\t" || c === "\n";

export function baseFlightMs(t: TypingTraits): number {
  return Math.max(30, 60000 / (t.wpm * 5) - NOMINAL_DWELL_MS);
}

/** Flight (ms) from key `prev` to key `ch`; draw order as _keys.flight_ms. */
export function flightMs(t: TypingTraits, prev: string | null | undefined, ch: string | null | undefined, rng: Rng): number {
  const [cls, jump] = bigramClass(prev, ch);
  let f = cls === "same_finger" ? t.sameFinger : cls === "same_hand" ? t.sameHand : cls === "alternate" ? t.alternate : 1;
  if (jump) f *= 1.15;
  let ms = baseFlightMs(t) * f * Math.exp(0.35 * gaussFrom(rng));
  if (prev != null && isWs(prev) && !isWs(ch)) ms += lerp(40, 120, rng());
  if (prev != null && prev.length === 1 && PUNCT.includes(prev)) ms += lerp(80, 250, rng());
  return Math.max(MIN_FLIGHT_MS, ms);
}

/** The whole sleep between key `prev` coming up and key `ch` going down (_keys.plan_gap). */
export function planGap(t: TypingTraits, prev: string | null | undefined, ch: string | null | undefined, rng: Rng,
  tempo = 1, pauseScale = 1): number {
  let ms = flightMs(t, prev, ch, rng);
  if (prev != null && isWs(prev)) ms += lerp(20, 100, rng());
  if (rng() < 0.06 * pauseScale) ms += lerp(180, 450, rng());
  return ms * tempo; // tempo / pauseScale: lane A's presets (persona.tempo, persona.pauseScale); 1 = as planned
}

export const WORD_FIX_RATE = 0.3;
export const SEARCH_SLIP_RATE = 0.1;

/** Playwright's key string split into [modifiers, key] ("Shift++" -> [["Shift"], "+"]). */
export function splitChord(key: string): [string[], string] {
  if (key === "+") return [[], "+"];
  if (key.endsWith("++")) return [key.slice(0, -2).split("+"), "+"];
  const parts = key.split("+");
  return [parts.slice(0, -1), parts[parts.length - 1]];
}

export const modifierLeadMs = (rng: Rng): number => lerp(40, 110, rng());
export const modifierLagMs = (rng: Rng): number => lerp(20, 80, rng());

export interface RepeatPlan { firstMs: number; gapMs: number; releaseMs: number }

/** OS auto-repeat timing for one held key: the persona's repeat delay and interval (a machine setting,
 * typingTraits) seen through +-2 / +-1 ms of timer jitter per run, and the hand's release. Draw order: first
 * jitter, gap jitter, release (_keys.plan_repeat). */
export function planRepeat(t: TypingTraits, rng: Rng): RepeatPlan {
  const firstMs = t.repeatFirstMs + lerp(-2, 2, rng());
  const gapMs = t.repeatGapMs + lerp(-1, 1, rng());
  const releaseMs = lerp(20, 60, rng());
  return { firstMs, gapMs, releaseMs };
}

export const submitPauseMs = (t: TypingTraits, rng: Rng): number =>
  Math.min(900, Math.max(250, t.submitCenterMs + (rng() - 0.5) * 300));
export const tabPauseMs = (t: TypingTraits, rng: Rng): number =>
  Math.min(400, Math.max(120, t.tabCenterMs + (rng() - 0.5) * 140));

// ------------------------------------------------------------ isolated-world reads (same as _keys.py)
export const FOCUSABLE = "a[href], area[href], button, input, select, textarea, summary, iframe, [tabindex], "
  + "[contenteditable], audio[controls], video[controls]";

export const TAB_DISTANCE = `function (b) {
  const up = n => n.parentElement || (n.parentNode && n.parentNode.nodeType === 11 && n.parentNode.host) || null;
  const FOC = '${FOCUSABLE}';
  const same = r => Math.abs(r.x - b.x) <= 1.5 && Math.abs(r.y - b.y) <= 1.5 &&
    Math.abs(r.width - b.width) <= 1.5 && Math.abs(r.height - b.height) <= 1.5;
  let t = null;
  if (b.sel) { try { t = document.querySelector(b.sel); } catch (e) { t = null; } }
  if (!t) {
    for (let e = this.nodeType === 1 ? this : this.parentElement, n = 0; e && n < 64; e = up(e), n++)
      if (e.matches && e.matches(FOC)) { t = e; break; }
  }
  if (!t || !t.matches(FOC) || !same(t.getBoundingClientRect())) return null;
  const ti = e => { const v = parseInt(e.getAttribute('tabindex'), 10); return isNaN(v) ? 0 : v; };
  const vis = e => { const s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none' && e.getClientRects().length > 0; };
  const tabbable = e => !e.disabled && !(e.tagName === 'INPUT' && e.type === 'hidden') && ti(e) >= 0 &&
    !(e.tagName === 'A' && !e.hasAttribute('href')) && vis(e);
  const all = Array.from(document.querySelectorAll(FOC)).filter(tabbable);
  const pos = all.filter(e => ti(e) > 0).sort((x, y) => ti(x) - ti(y));
  const order = pos.concat(all.filter(e => ti(e) <= 0));
  let a = document.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  const ai = a ? order.indexOf(a) : -1, tix = order.indexOf(t);
  const VALUE_TYPES = ['checkbox', 'radio', 'button', 'submit', 'reset', 'file', 'image', 'range', 'color', 'hidden'];
  const text = e => !!e && ((e.tagName === 'INPUT' && VALUE_TYPES.indexOf(e.type) < 0) || e.tagName === 'TEXTAREA' || !!e.isContentEditable);
  const role = t.getAttribute('role') || '';
  const act = ['BUTTON', 'A', 'SUMMARY', 'SELECT'].indexOf(t.tagName) >= 0 ||
    (t.tagName === 'INPUT' && VALUE_TYPES.indexOf(t.type) >= 0) ||
    ['button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'option'].indexOf(role) >= 0;
  let near = null;
  if (tix >= 0) for (const off of [1, 2, 3, -1]) {
    const c = order[tix - off];
    if (!c || c === a || !text(c) || c.disabled) continue;
    const q = c.getBoundingClientRect();
    if (q.width >= 2 && q.height >= 2 && q.left >= 0 && q.top >= 0 && q.right <= innerWidth && q.bottom <= innerHeight) {
      near = { off, rect: [q.x, q.y, q.width, q.height] }; break; }
  }
  return { dist: ai >= 0 && tix >= 0 ? tix - ai : null, fromText: text(a), focused: a === t,
    activatable: act, text: text(t), tag: t.tagName.toLowerCase(), type: t.tagName === 'INPUT' ? t.type : '',
    n: order.length, noAnchor: ai < 0, near }; }`;

export const BLANK_POINT = `(a) => { const b = a.box, FOC = '${FOCUSABLE}, label, [onclick], [role]';
  const cands = [];
  for (const dy of [28, 56, 96]) cands.push([b.x + b.width * 0.5, b.y + b.height + dy]);
  for (const dx of [32, 72, 128]) cands.push([b.x + b.width + dx, b.y + b.height * 0.5]);
  for (const dx of [32, 72]) cands.push([b.x - dx, b.y + b.height * 0.5]);
  for (const dy of [40, 80]) cands.push([b.x + b.width * 0.5, b.y - dy]);
  const PLAIN = ['HTML', 'BODY', 'DIV', 'SECTION', 'MAIN', 'FORM', 'ARTICLE', 'P', 'SPAN', 'FIELDSET', 'UL', 'LI', 'HEADER', 'FOOTER', 'NAV', 'ASIDE', 'TD', 'TR', 'TABLE'];
  for (const [x, y] of cands) {
    if (x < 4 || y < 4 || x > a.vw - 4 || y > a.vh - 4) continue;
    const e = document.elementFromPoint(x, y);
    if (!e) continue;
    if (PLAIN.indexOf(e.tagName) < 0 || e.isContentEditable) continue;
    if (e.closest(FOC)) continue;
    return [x, y];
  }
  return null; }`;

export const SELECTION_STATE = `() => { let e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e) return null;
  if (typeof e.selectionStart === 'number' && typeof e.value === 'string')
    return [e.selectionStart, e.selectionEnd, e.value.length];
  if (e.isContentEditable) { const s = getSelection(); return [0, s ? s.toString().length : 0, (e.textContent || '').length]; }
  return null; }`;

export interface TabInfo {
  dist: number | null; fromText: boolean; focused: boolean; activatable: boolean; text: boolean;
  tag: string; type: string; n: number;
  noAnchor?: boolean;                                  // nothing (in the Tab order) has the focus
  near?: { off: number; rect: number[] } | null;       // the text stop 1-3 before the target (or 1 after) and its presses away
}
