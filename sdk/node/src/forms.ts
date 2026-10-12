/**
 * Pure planning for humanized form input (port of sdk/python/clearcote/_forms.py).
 *
 * Nothing here touches a page: it decides WHAT a person would press for a form control, and
 * ./humanize.ts decides HOW (glides, holds, cadence). Keeping the decisions pure is what lets one set
 * of test vectors (sdk/testdata/humanize_forms_vectors.json) pin Python (_forms.py), Node (this file)
 * and .NET (Forms.cs) to identical answers.
 *
 * What a person does, measured on r37 headless (SC 197 forms probes, 2026-10-11):
 *   - <select> (a drop-down): a click opens the popup; letters typed while it is open move the
 *     popup's highlight and change NOTHING on the page; Enter commits -> exactly one trusted
 *     input + change. Typing on a focused but CLOSED select instead fires one change per letter, and
 *     stepping it with arrows fires one change per row -- the old route, which picked row 40 with 40
 *     change events.
 *   - list boxes (size > 1 or multiple): a click on a row selects it, Ctrl+click adds a row.
 *   - <input type=range>: a click on the track jumps to that fraction, arrows step by `step`.
 *   - date / time / month / week / datetime-local: the editor's segments come in the BROWSER's
 *     order (month/day/year, hour/minute/ampm on these builds, whatever the persona's locale), so the
 *     order is read from the editor itself, never assumed. Numeric segments take digits and move on
 *     after a full field; the year never moves on by itself (ArrowRight does); a month NAME field
 *     (type=month) ignores digits and steps on arrows; the ampm field takes A / P.
 */

import { gaussFrom, mulberry32 } from "./motion.js";

/** A source of uniform [0, 1) numbers (Math.random, or a seeded generator in tests). */
export type FormsRng = () => number;

// ----------------------------------------------------------------------------- field classes
// The fill() routes. Playwright's own split (kInputTypesToSetValue / kInputTypesToTypeInto):
// text-like types are typed into, value types have their value set, everything else raises.
export const TEXT_TYPES: ReadonlySet<string> = new Set(["", "text", "search", "email", "password", "tel", "url", "number"]);
export const DATETIME_TYPES: ReadonlySet<string> = new Set(["date", "time", "month", "week", "datetime-local"]);
// color: Playwright sets it; the only humane route is the OS colour dialog, which no page sees and
// no script can drive -- so native, exactly as Playwright behaves.
export const NATIVE_VALUE_TYPES: ReadonlySet<string> = new Set(["color"]);

export type FieldClass = "text" | "range" | "datetime" | "native" | "unfillable";

/** 'text' | 'range' | 'datetime' | 'native' | 'unfillable' for one element.
 *
 * 'native' = let Playwright set the value (its semantics, untrusted but correct);
 * 'unfillable' = let Playwright's fill raise exactly as it would have (checkbox, file, ...). */
export function classifyField(tag: string | null | undefined, inputType?: string | null, contenteditable = false): FieldClass {
  const t0 = (tag || "").toLowerCase();
  if (contenteditable && t0 !== "input" && t0 !== "select") return "text";
  if (t0 === "textarea") return "text";
  if (t0 !== "input") return "unfillable";
  const t = (inputType || "").toLowerCase();
  if (TEXT_TYPES.has(t)) return "text";
  if (t === "range") return "range";
  if (DATETIME_TYPES.has(t)) return "datetime";
  if (NATIVE_VALUE_TYPES.has(t)) return "native";
  // An unknown type is a text input to the browser (HTML: invalid type -> text).
  if (!["checkbox", "radio", "file", "submit", "button", "image", "reset", "hidden"].includes(t)) return "text";
  return "unfillable";
}

// ----------------------------------------------------------------------------------- typos
// No fat-finger typos where a wrong character is not a harmless slip but a validation failure or a
// locked field: credentials, numbers, addresses, one-time codes.
export const NO_TYPO_TYPES: ReadonlySet<string> = new Set(["password", "email", "number", "tel", "url"]);
export const NO_TYPO_INPUTMODES: ReadonlySet<string> = new Set(["numeric", "decimal", "tel", "email", "url"]);
export const NO_TYPO_AUTOCOMPLETE: ReadonlySet<string> = new Set(["one-time-code", "cc-number", "cc-csc", "cc-exp",
  "current-password", "new-password"]);

// Compact QWERTY adjacency for letter typos; digits slip to a NEIGHBOURING DIGIT on the number row
// (a digit never turns into a letter, and never into itself).
export const NEARBY_LETTERS: Readonly<Record<string, string>> = {
  a: "sqwz", b: "vghn", c: "xdfv", d: "sfecx", e: "wrsdf", f: "dgrtcv",
  g: "fhtyb", h: "gjybn", i: "ujko", j: "hkunm", k: "jloi", l: "kop",
  m: "njk", n: "bhjm", o: "iklp", p: "ol", q: "wa", r: "edft",
  s: "awedxz", t: "rfgy", u: "yhji", v: "cfgb", w: "qase", x: "zsdc",
  y: "tghu", z: "asx",
};
export const NEARBY_DIGITS: Readonly<Record<string, string>> = {
  "1": "2", "2": "13", "3": "24", "4": "35", "5": "46", "6": "57", "7": "68",
  "8": "79", "9": "80", "0": "9",
};

export const DEFAULT_TYPO_RATE = 0.02;

/** What the typo gate reads about a field (any member may be missing). */
export interface TypoField {
  type?: string | null; inputmode?: string | null; autocomplete?: string | null;
  /** Focus is inside a frame the isolated world cannot read (ISO_ACTIVE_FIELD's marker). */
  frame?: boolean;
}

/** May a fat-finger typo happen in this field? Unknown fields allow typos, as the old behaviour did;
 * a field inside a frame (unreadable from here) fails CLOSED. */
export function typosAllowed(field: TypoField | null | undefined): boolean {
  if (!field || Object.keys(field).length === 0) return true;
  if (field.frame) return false; // focus is inside a frame we cannot read: fail closed
  const t = (field.type || "").toLowerCase();
  if (NO_TYPO_TYPES.has(t)) return false;
  if (NO_TYPO_INPUTMODES.has((field.inputmode || "").toLowerCase())) return false;
  const ac = (field.autocomplete || "").toLowerCase().split(/\s+/).filter(Boolean);
  return !ac.some((tok) => NO_TYPO_AUTOCOMPLETE.has(tok));
}

/** Characters a typo may replace: unshifted ASCII letters and digits. */
export function typoEligible(ch: string | undefined | null): boolean {
  return typeof ch === "string" && ch.length === 1 && ((ch >= "a" && ch <= "z") || (ch >= "0" && ch <= "9"));
}

const choice = (s: string, rng: FormsRng): string => s[Math.floor(rng() * s.length)];

/** A neighbouring key for `ch` (same class: letter -> letter, digit -> digit), or null. */
export function nearbyKey(ch: string, rng: FormsRng = Math.random): string | null {
  if (ch.length === 1 && ch >= "a" && ch <= "z") return choice(NEARBY_LETTERS[ch], rng);
  if (ch.length === 1 && ch >= "0" && ch <= "9") return choice(NEARBY_DIGITS[ch], rng);
  return null;
}

export type TypoPlan =
  | { kind: "sub"; wrong: string; late: number }
  | { kind: "double" }
  | { kind: "swap" };

/** Decide whether character `i` of `text` gets a typo, and which kind.
 *
 * null, or:
 *   {kind: "sub", wrong, late}  -- a neighbouring key instead of text[i]; noticed after `late` (0-2)
 *       more correct characters, then Backspace late+1 times and retype.
 *   {kind: "double"}            -- text[i] pressed twice; one Backspace.
 *   {kind: "swap"}              -- text[i+1] typed before text[i]; two Backspaces.
 * `text` is indexed by code point (pass a string or an Array.from(text) array). The humanize layer
 * still checks that each wrong key actually landed in the field before it deletes anything (an input
 * mask or a maxlength can swallow it). */
export function planTypo(text: string | readonly string[], i: number, rng: FormsRng = Math.random,
  rate: number = DEFAULT_TYPO_RATE): TypoPlan | null {
  const t = typeof text === "string" ? Array.from(text) : text;
  const ch = t[i];
  if (!typoEligible(ch) || rate <= 0 || rng() >= rate) return null;
  const r = rng();
  const nxt = i + 1 < t.length ? t[i + 1] : null;
  if (r < 0.15 && nxt !== null && typoEligible(nxt) && nxt !== ch) return { kind: "swap" };
  if (r < 0.30) return { kind: "double" };
  let late = 0;
  if (rng() < 0.3) {
    // Late notice: only over characters that are themselves plain unshifted keys, so the retyping
    // after the Backspaces stays on the simple path.
    const k = rng() < 0.7 ? 1 : 2;
    const tail = t.slice(i + 1, i + 1 + k);
    if (tail.length === k && tail.every((c) => typoEligible(c) || c === " ")) late = k;
  }
  return { kind: "sub", wrong: nearbyKey(ch, rng) as string, late };
}

/** Python's str.isalnum() for one code point (Unicode letters and numbers). */
const isAlnum = (c: string): boolean => /^[\p{L}\p{N}]$/u.test(c);

/** Is `got` just `want` re-punctuated by an input mask -- the same letters and digits in the same
 * order, with separators, spaces or case changed ("(555) 123-4567" for "5551234567")? A value only a
 * formatter changed counts as typed right; a lost or an extra key does not. Typing it again would not
 * change what the formatter does, and no person types their phone number twice. */
export function formattedEqual(got: string | null | undefined, want: string | null | undefined): boolean {
  if (got === null || got === undefined || want === null || want === undefined) return false;
  const a = Array.from(got).filter(isAlnum).map(casefold);
  const b = Array.from(want).filter(isAlnum).map(casefold);
  return b.length > 0 && a.length === b.length && a.every((c, i) => c === b[i]);
}

// ------------------------------------------------------------------------------- maxlength
/** The input types whose maxlength the browser enforces on what is typed or inserted, plus textarea. */
export const MAXLENGTH_TYPES: ReadonlySet<string> = new Set(["", "text", "search", "url", "tel", "email", "password"]);

/**
 * The part of `value` an EMPTY control with `maxlength` ends up holding: what Playwright's fill
 * (Input.insertText) leaves there, and what a person types before the field stops taking keys.
 * maxlength counts UTF-16 code units, as Blink does; a character that would be cut in half (beyond the
 * BMP) is dropped whole. Negative / missing maxlength, a contenteditable and a type that ignores
 * maxlength keep the whole value. Port of _forms.fit_maxlength (r38: fill('1234567') on maxlength=5
 * typed seven digits, wiped the field, typed seven again, then fell back to Playwright).
 */
export function fitMaxlength(value: string, maxlength: number | string | null | undefined,
  tag: string | null | undefined = "input", inputType: string | null | undefined = ""): string {
  let n = -1;
  if (maxlength !== null && maxlength !== undefined && maxlength !== "") {
    const v = typeof maxlength === "number" ? maxlength : Number(maxlength);
    n = Number.isFinite(v) ? Math.trunc(v) : -1;
  }
  const t = (tag || "").toLowerCase();
  if (n < 0 || (t !== "input" && t !== "textarea")) return value;
  if (t === "input" && !MAXLENGTH_TYPES.has((inputType || "").toLowerCase())) return value;
  let units = 0, out = "";
  for (const ch of value) {          // code points, like Python's str
    units += ch.length;               // 2 for a surrogate pair
    if (units > n) return out;
    out += ch;
  }
  return value;
}

// ------------------------------------------------------------------------------ pre-press pause
/** Bounds of the pause between the pointer coming to rest and the button going down (ms). */
export const PRE_PRESS_MIN_MS = 40;
export const PRE_PRESS_MAX_MS = 250;
// Salt for the per-identity centre (same value in _forms.py and Forms.cs).
const PRE_PRESS_SALT = 0x5bd1e995;

/** The identity's typical pre-press pause (ms), 70..135, stable per persona seed. */
export function prePressCenter(seedInt: number): number {
  const r = mulberry32((seedInt ^ PRE_PRESS_SALT) >>> 0)();
  return 70 + (135 - 70) * r;
}

/**
 * One pre-press pause (ms): a log-normal above the 40 ms floor with its median at the identity's
 * centre (sigma 0.55, right-skewed); a draw past 250 ms is drawn again (up to four times, then capped).
 * Port of _forms.pre_press_ms. Measured on r38 before it existed: the focus click before typing pressed
 * ~4 ms after the last pointermove, a drag grab ~21 ms.
 */
export function prePressMs(seedInt: number, rng: FormsRng = Math.random): number {
  const c = prePressCenter(seedInt) - PRE_PRESS_MIN_MS;
  for (let k = 0; k < 4; k++) {
    const ms = PRE_PRESS_MIN_MS + c * Math.exp(0.55 * gaussFrom(rng));
    if (ms <= PRE_PRESS_MAX_MS) return ms;
  }
  return PRE_PRESS_MAX_MS;
}

// ------------------------------------------------------------------------------ select-all
/** The select-all chord a person on `platform` uses: Cmd+A on a Mac, Ctrl+A elsewhere. */
export function selectAllCombo(platform: string | null | undefined): string {
  return ["macos", "mac", "darwin"].includes((platform || "").toLowerCase()) ? "Meta+a" : "Control+a";
}

// ------------------------------------------------------------------------------ click options
export type ClickModifier = "Alt" | "Control" | "ControlOrMeta" | "Meta" | "Shift";
const MODIFIERS: readonly string[] = ["Alt", "Control", "ControlOrMeta", "Meta", "Shift"];

/** What the humanized press needs from Playwright's click/dblclick/hover options. */
export interface ClickPlan {
  button: "left" | "right" | "middle";
  clickCount: number;
  modifiers: ClickModifier[];
  delay: number | null;
  position: { x: number; y: number } | null;
  force: boolean;
  trial: boolean;
}

/** Playwright click/dblclick/hover options -> what the humanized press needs.
 *
 * The old wrappers forwarded only `timeout`, so `button: "right"` became a left click, modifiers and
 * `position` were dropped, and `trial: true` -- documented to perform NO action -- clicked.
 *
 * "ControlOrMeta" is kept as is: Playwright resolves it to the key the ENGINE's platform uses, which is
 * what makes Ctrl+click open a link in a new tab on a Windows or Linux build whatever the persona
 * claims. (`platform` is accepted for API symmetry and unused.) Both `clickCount` and the Python
 * spelling `click_count` are read. */
export function normalizeClickOptions(options?: Record<string, any> | null, _platform?: string | null): ClickPlan {
  const o = options || {};
  let button = o.button || "left";
  if (button !== "left" && button !== "right" && button !== "middle") button = "left";
  const raw = "click_count" in o ? o.click_count : o.clickCount;
  let count = raw ? Math.trunc(Number(raw)) : 1;
  if (!Number.isFinite(count)) count = 1;
  const mods: ClickModifier[] = [];
  for (const m of (o.modifiers || []) as string[]) {
    if (MODIFIERS.includes(m) && !mods.includes(m as ClickModifier)) mods.push(m as ClickModifier);
  }
  const pos = o.position;
  const position = pos && typeof pos === "object" && "x" in pos && "y" in pos
    ? { x: Number(pos.x), y: Number(pos.y) } : null;
  let delay: number | null = o.delay !== undefined && o.delay !== null ? pyFloat(o.delay) : null;
  // delay 0 is Playwright's DEFAULT ("no hold"), not a request for a 1 ms press: it, and anything
  // non-positive or non-finite, means "use the persona's hold" (a 1-4 ms button hold is a tell, and a
  // negative hold would leave the button down).
  if (delay !== null && (!Number.isFinite(delay) || delay <= 0)) delay = null;
  return {
    button, clickCount: Math.max(1, count), modifiers: mods,
    delay, position,
    force: Boolean(o.force), trial: Boolean(o.trial),
  };
}

/** The engine pointer path performs one plain left click and nothing else. */
export function enginePathOk(o: ClickPlan): boolean {
  return o.button === "left" && o.clickCount === 1 && o.modifiers.length === 0 && o.delay === null;
}

// ------------------------------------------------------------------- <select> type-ahead plan
/** Python's str.casefold(), closely enough for option labels: full upper-casing then lower-casing
 * folds what lower-casing alone misses (sharp s -> ss, ligatures, final sigma). */
function casefold(s: string): string {
  return s.toUpperCase().toLowerCase();
}

/** Blink compares FoldCase()d labels with leading whitespace stripped (type_ahead.cc). */
function fold(s: string | null | undefined): string {
  return casefold((s || "").replace(/^\s+/, ""));
}

// A space counts: inside a running type-ahead search Blink appends it like any other character, so
// "item 371" lands on "Item 371" in a 400-row list (measured on r37 by verifier B: 1 trusted change,
// 0.99 s). Without it the prefix stopped at "item" and the other 370 rows were ArrowDowns (68 s). It
// is never the FIRST key: labels are compared with leading whitespace stripped, and a space that
// starts a search can open or close the popup instead.
export const TYPEABLE: ReadonlySet<string> = new Set("abcdefghijklmnopqrstuvwxyz0123456789 ");

/** Where Blink's TypeAhead (kMatchPrefix | kCycleFirstChar) lands after `keys`.
 *
 * `labels`: one entry per option, null for a disabled option (Blink's OptionAtIndex returns an empty
 * string for those, so they never match). `start`: the highlighted index when typing begins (the
 * current selection; -1 = none). Returns the landing index (unchanged if no match).
 *
 * Mirrors type_ahead.cc: a first key, or the same key again, searches for that ONE character starting
 * AFTER the current item (so a repeated letter cycles through the items that start with it); a mixed
 * buffer searches for the whole buffer starting AT the current item. */
export function typeaheadSimulate(labels: readonly (string | null)[], start: number, keys: string): number {
  const n = labels.length;
  if (n === 0) return start;
  const folded = labels.map((l) => (l !== null && l !== undefined ? fold(l) : null));
  let buf = "";
  let repeating: string | null = null;
  let cur = start;
  for (const c0 of Array.from(keys)) {
    const c = casefold(c0);
    buf += c;
    let offset = 1;
    let prefix: string;
    if (c === repeating) {
      prefix = c;
    } else {
      prefix = buf;
      if (Array.from(buf).length > 1) {
        repeating = null;
        offset = 0;
      } else {
        repeating = c;
      }
    }
    let idx = ((cur >= 0 ? cur : 0) + offset) % n;
    for (let k = 0; k < n; k++) {
      const lab = folded[idx];
      if (lab !== null && lab !== "" && lab.startsWith(prefix)) {
        cur = idx;
        break;
      }
      idx = (idx + 1) % n;
    }
  }
  return cur;
}

/** Arrow presses from row `frm` to row `to` in the popup, which skips disabled rows (signed). Each
 * press lands on the next enabled row, so the count is the enabled rows passed through INCLUDING the
 * target: (frm, to] going down, [to, frm) going up -- the start row itself never counts, which is what
 * makes a move away from a disabled current row come out right. */
function enabledSteps(labels: readonly (string | null)[], frm: number, to: number): number | null {
  if (frm === to) return 0;
  const on = (i: number) => labels[i] !== null && labels[i] !== undefined;
  if (!on(to)) return null;
  let steps = 0;
  if (to > frm) {
    for (let i = frm + 1; i <= to; i++) if (on(i)) steps++;
    return steps;
  }
  for (let i = to; i < frm; i++) if (on(i)) steps++;
  return -steps; // the target counts, so never -0
}

export interface SelectPlan { keys: string; arrows: number }

/** Keys that move an OPEN drop-down's highlight from `current` to `target`.
 *
 * Returns {keys: "<letters to type>", arrows: <signed ArrowDown count>} with the fewest presses: the
 * shortest typeable prefix of the target's label that type-ahead lands on exactly, else the cheapest
 * (prefix, then arrows) combination, else arrows alone. null when the target is disabled (no route
 * selects it). */
export function planSelect(labels: readonly (string | null)[], current: number, target: number,
  maxPrefix = 24): SelectPlan | null {
  if (!(target >= 0 && target < labels.length) || labels[target] === null || labels[target] === undefined) return null;
  let best: SelectPlan | null = null;
  const start = current >= 0 && current < labels.length ? current : -1;
  const base = start >= 0 ? enabledSteps(labels, start, target) : null;
  if (base !== null) {
    best = { keys: "", arrows: base };
  } else if (start < 0) {
    // No selection: the popup highlights the first row; arrows count from there.
    const first = labels.findIndex((l) => l !== null && l !== undefined);
    if (first >= 0) {
      const st = enabledSteps(labels, first, target);
      if (st !== null) best = { keys: "", arrows: st };
    }
  }
  const label = Array.from(fold(labels[target]));
  let prefix = "";
  for (const ch of label.slice(0, Math.max(0, maxPrefix))) {
    if (!TYPEABLE.has(ch) || (ch === " " && !prefix)) break;
    prefix += ch;
    const land = typeaheadSimulate(labels, start, prefix);
    if (land < 0 || labels[land] === null || labels[land] === undefined) continue;
    const st = enabledSteps(labels, land, target);
    if (st === null) continue;
    const cost = prefix.length + Math.abs(st);
    const bestCost = best === null ? null : best.keys.length + Math.abs(best.arrows);
    // fewest presses; on a tie, typing the name outright beats typing and then arrowing
    if (best === null || cost < (bestCost as number) || (cost === bestCost && st === 0 && best.arrows !== 0)) {
      best = { keys: prefix, arrows: st };
    }
    if (st === 0) break;
  }
  return best;
}

/** One <option> as the humanize reads it. */
export interface OptionInfo { value?: string | null; label?: string | null; disabled?: boolean; selected?: boolean }

/** Playwright's option matching for ONE wanted option: a string `value` matches an option's value OR
 * its label; `label` matches the label; `index` the position. Returns the index or -1. */
export function matchOption(options: readonly OptionInfo[],
  want: { value?: string | null; index?: number | null; label?: string | null }): number {
  const { value, index, label } = want;
  if (index !== undefined && index !== null) return index >= 0 && index < options.length ? index : -1;
  for (let i = 0; i < options.length; i++) {
    const o = options[i];
    if (label !== undefined && label !== null) {
      if (o.label === label) return i;
    } else if (value !== undefined && value !== null && (o.value === value || o.label === value)) {
      return i;
    }
  }
  return -1;
}

// --------------------------------------------------------------------------------- range
const FLOAT_RE = /^[+-]?(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?$/;

/** Python's float(v) for a string or number: null where float() would raise. */
export function pyFloat(v: unknown): number | null {
  if (typeof v === "number") return v;
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v !== "string") return null;
  const s = v.trim();
  const low = s.toLowerCase().replace(/^[+-]/, "");
  if (low === "inf" || low === "infinity") return s.startsWith("-") ? -Infinity : Infinity;
  if (low === "nan") return NaN;
  if (!FLOAT_RE.test(s) || /__|_\.|\._|_$|_[eE]|[eE]_/.test(s)) return null;
  return Number(s.replace(/_/g, ""));
}

function num(v: unknown, dflt: number): number {
  const f = pyFloat(v);
  return f === null || Number.isNaN(f) ? dflt : f;
}

/** HTML defaults: min 0, max 100, step 1 ('any' = no snapping, null); max below min -> max = min. */
export function rangeAttrs(min?: unknown, max?: unknown, step?: unknown): [number, number, number | null] {
  const lo = num(min, 0.0);
  let hi = num(max, 100.0);
  if (hi < lo) hi = lo;
  let st: number | null;
  if (typeof step === "string" && step.trim().toLowerCase() === "any") {
    st = null;
  } else {
    st = num(step, 1.0);
    if (st <= 0) st = 1.0;
  }
  return [lo, hi, st];
}

/** The value the browser will hold after being asked for `value` (clamped, step-aligned from min,
 * rounded to the nearest step; a tie goes up, as Blink's StepRange does). */
export function rangeSanitize(value: unknown, min?: unknown, max?: unknown, step?: unknown): number {
  const [lo, hi, st] = rangeAttrs(min, max, step);
  let v = Math.min(Math.max(num(value, (lo + hi) / 2), lo), hi);
  if (st) {
    // Blink steps in Decimal; the epsilon keeps binary floats from rounding 0.35/0.1 =
    // 3.4999999999999996 down to 3 where the browser gets 4. The step count is capped at the last
    // step inside max -- computed, never walked down in a loop: a page can serve max=123456789
    // step=1e-9, where subtracting one step leaves the float unchanged and the loop never ends
    // (which froze the whole event loop).
    const q = (v - lo) / st, last = (hi - lo) / st;
    if (!(Number.isFinite(q) && Number.isFinite(last))) return v; // a step too small to count in floats
    const n = Math.min(Math.floor(q + 0.5 + 1e-9), Math.floor(last + 1e-9));
    v = lo + n * st;
  }
  return v;
}

/** Would the browser keep `value` exactly? Playwright's fill sets the value and raises "Malformed
 * value" when the browser changed it (off the step, out of range, not a number), so the humane route
 * only takes values that pass and leaves the rest to Playwright to reject. */
export function rangeValueOk(value: unknown, min?: unknown, max?: unknown, step?: unknown): boolean {
  const f = pyFloat(value);
  if (f === null || !Number.isFinite(f)) return false;
  return numericEqual(rangeSanitize(f, min, max, step), f);
}

export function rangeFraction(value: number, min?: unknown, max?: unknown): number {
  const [lo, hi] = rangeAttrs(min, max, null);
  return hi === lo ? 0.0 : (value - lo) / (hi - lo);
}

// Chromium's default slider thumb is ~16 px wide; the track's usable span is inset by half of it.
export const RANGE_THUMB_INSET = 8.0;

/** Where on a horizontal range input the thumb sits at `fraction` (CSS px): [x, y]. */
export function rangePoint(box: { x: number; y: number; width: number; height: number }, fraction: number,
  rtl = false, inset = RANGE_THUMB_INSET): [number, number] {
  const w = box.width;
  const ins = Math.min(inset, w / 2.0);
  const f = rtl ? 1.0 - fraction : fraction;
  return [box.x + ins + f * (w - 2 * ins), box.y + box.height / 2.0];
}

/** Python's round(): half to even. */
function roundHalfEven(x: number): number {
  const f = Math.floor(x);
  const d = x - f;
  if (d > 0.5) return f + 1;
  if (d < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

/** Arrow presses from `current` to `target`: ['ArrowRight'|'ArrowLeft', count] or [null, 0].
 * ArrowRight raises a horizontal LTR range by one step (and lowers an RTL one; the caller swaps). */
export function rangeNudge(current: number, target: number, step: number | null | undefined):
  [("ArrowRight" | "ArrowLeft") | null, number] {
  if (step === null || step === undefined || step <= 0 || Math.abs(target - current) < 1e-9) return [null, 0];
  const n = roundHalfEven(Math.abs(target - current) / step);
  return [target > current ? "ArrowRight" : "ArrowLeft", n];
}

export function numericEqual(a: unknown, b: unknown): boolean {
  const fa = pyFloat(a), fb = pyFloat(b);
  if (fa === null || fb === null) return false;
  return Math.abs(fa - fb) < 1e-6;
}

// ----------------------------------------------------------------- date / time / month / week
export type DatetimeParts = Partial<Record<"year" | "month" | "day" | "week" | "hour" | "minute" | "second"
  | "millisecond", number>>;

/** Parse an HTML date-like value into its parts, or null if it is not valid for `kind`. */
export function parseDatetimeValue(kind: string, value: string | null | undefined): DatetimeParts | null {
  const v = (value || "").trim();
  if (kind === "date") {
    const m = /^(\d{4,6})-(\d{2})-(\d{2})$/.exec(v);
    if (!m) return null;
    const y = parseInt(m[1], 10), mo = parseInt(m[2], 10), d = parseInt(m[3], 10);
    return mo >= 1 && mo <= 12 && d >= 1 && d <= 31 ? { year: y, month: mo, day: d } : null;
  }
  if (kind === "month") {
    const m = /^(\d{4,6})-(\d{2})$/.exec(v);
    if (!m) return null;
    const y = parseInt(m[1], 10), mo = parseInt(m[2], 10);
    return mo >= 1 && mo <= 12 ? { year: y, month: mo } : null;
  }
  if (kind === "week") {
    const m = /^(\d{4,6})-W(\d{2})$/.exec(v);
    if (!m) return null;
    const y = parseInt(m[1], 10), w = parseInt(m[2], 10);
    return w >= 1 && w <= 53 ? { year: y, week: w } : null;
  }
  if (kind === "time") return parseTime(v);
  if (kind === "datetime-local") {
    const m = /^(\d{4,6})-(\d{2})-(\d{2})[T ](.+)$/.exec(v);
    if (!m) return null;
    const t = parseTime(m[4]);
    const y = parseInt(m[1], 10), mo = parseInt(m[2], 10), d = parseInt(m[3], 10);
    if (t === null || !(mo >= 1 && mo <= 12 && d >= 1 && d <= 31)) return null;
    return { ...t, year: y, month: mo, day: d };
  }
  return null;
}

function parseTime(v: string): DatetimeParts | null {
  const m = /^(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/.exec(v);
  if (!m) return null;
  const h = parseInt(m[1], 10), mi = parseInt(m[2], 10);
  const s = m[3] ? parseInt(m[3], 10) : 0;
  const ms = m[4] ? parseInt(m[4].padEnd(3, "0"), 10) : 0;
  if (h > 23 || mi > 59 || s > 59) return null;
  return { hour: h, minute: mi, second: s, millisecond: ms };
}

function sameParts(a: DatetimeParts, b: DatetimeParts): boolean {
  const ka = Object.keys(a).sort(), kb = Object.keys(b).sort();
  return ka.length === kb.length && ka.every((k, i) => k === kb[i] && (a as any)[k] === (b as any)[k]);
}

/** Same moment for `kind`, whatever the formatting (17:45 == 17:45:00). */
export function datetimeEqual(kind: string, a: string | null | undefined, b: string | null | undefined): boolean {
  const pa = parseDatetimeValue(kind, a), pb = parseDatetimeValue(kind, b);
  return pa !== null && pb !== null && sameParts(pa, pb);
}

/** One key action for a date-like editor: ["type", "digits"] | ["key", "ArrowRight"] |
 * ["key", "ArrowUp" | "ArrowDown", n]. */
export type DatetimeAction = ["type", string] | ["key", string] | ["key", string, number];

const pad = (n: number, w: number) => String(n).padStart(w, "0");

/** Key actions that type `value` into a native date-like editor whose fields come in `segments`
 * order (names as the editor's pseudo elements give them, '-field' optional: month, day, year, hour,
 * minute, second, millisecond, ampm, week).
 *
 * null when the editor cannot represent the value (a seconds value with no seconds field, a year
 * beyond four digits, an unknown segment). */
export function planDatetime(kind: string, segments: readonly string[], value: string,
  current?: string | null): DatetimeAction[] | null {
  const parts = parseDatetimeValue(kind, value);
  if (parts === null) return null;
  const segs = segments.map((s) => s.split("-field").join(""));
  if (segs.length === 0) return null;
  const haveAmpm = segs.includes("ampm");
  if (kind === "time" || kind === "datetime-local") {
    if (parts.second && !segs.includes("second")) return null;
    if (parts.millisecond && !segs.includes("millisecond")) return null;
  }
  const cur = current ? parseDatetimeValue(kind, current) : null;
  const out: DatetimeAction[] = [];
  for (let k = 0; k < segs.length; k++) {
    const seg = segs[k];
    const last = k === segs.length - 1;
    if (seg === "year") {
      const y = parts.year;
      if (y === undefined || y > 9999) return null;
      out.push(["type", pad(y, 4)]);
      if (!last) out.push(["key", "ArrowRight"]);      // the year never moves on by itself
    } else if (seg === "month") {
      const m = parts.month;
      if (m === undefined) return null;
      if (kind === "month") {
        // A month NAME field: digits are ignored; it steps on arrows. From empty the first ArrowUp
        // shows January.
        const c = cur ? cur.month : undefined;
        if (c === undefined) {
          out.push(["key", "ArrowUp", m]);
        } else {
          const d = (((m - c) % 12) + 12) % 12;
          out.push(d <= 6 ? ["key", "ArrowUp", d] : ["key", "ArrowDown", 12 - d]);
        }
        if (!last) out.push(["key", "ArrowRight"]);
      } else {
        out.push(["type", pad(m, 2)]);
      }
    } else if (seg === "day") {
      const d = parts.day;
      if (d === undefined) return null;
      out.push(["type", pad(d, 2)]);
    } else if (seg === "week") {
      const w = parts.week;
      if (w === undefined) return null;
      out.push(["type", pad(w, 2)]);
    } else if (seg === "hour") {
      const h = parts.hour;
      if (h === undefined) return null;
      out.push(["type", pad(haveAmpm ? (h % 12 || 12) : h, 2)]);
    } else if (seg === "minute") {
      out.push(["type", pad(parts.minute ?? 0, 2)]);
    } else if (seg === "second") {
      out.push(["type", pad(parts.second ?? 0, 2)]);
    } else if (seg === "millisecond") {
      out.push(["type", pad(parts.millisecond ?? 0, 3)]);
    } else if (seg === "ampm") {
      // lower case: a capital with no Shift held is input no keyboard produces
      out.push(["type", (parts.hour ?? 0) < 12 ? "a" : "p"]);
    } else {
      return null;
    }
  }
  // drop no-op arrow steps (a month already right)
  return out.filter((a) => !(a[0] === "key" && a.length === 3 && a[2] === 0));
}

/** A node of a CDP DOM.describeNode tree (only what the walkers read). */
export interface DescribedNode {
  backendNodeId?: number;
  attributes?: string[];
  children?: DescribedNode[];
  shadowRoots?: DescribedNode[];
  [k: string]: unknown;
}

function walkSegments(node: DescribedNode, out: [string, number | undefined][]): void {
  const attrs = node.attributes || [];
  for (let j = 0; j + 1 < attrs.length; j += 2) {
    if (attrs[j] === "pseudo") {
      const ps = attrs[j + 1];
      if (ps.startsWith("-webkit-datetime-edit-") && ps.endsWith("-field")) {
        out.push([ps.slice("-webkit-datetime-edit-".length), node.backendNodeId]);
      }
    }
  }
  for (const c of [...(node.shadowRoots || []), ...(node.children || [])]) walkSegments(c, out);
}

/** The date-like editor's field names, in order, from a CDP DOM.describeNode(pierce) tree. */
export function segmentNamesFromTree(node: DescribedNode): string[] {
  const out: [string, number | undefined][] = [];
  walkSegments(node, out);
  return out.map(([name]) => name);
}

/** Like segmentNamesFromTree but with each field's backendNodeId: [[name, backendNodeId]]. */
export function segmentNodesFromTree(node: DescribedNode): [string, number | undefined][] {
  const out: [string, number | undefined][] = [];
  walkSegments(node, out);
  return out;
}
