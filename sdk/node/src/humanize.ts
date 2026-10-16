// Humanized input + cursor visualization.
//
// `humanize: true` installs ONE consistent human-input standard covering moving, clicking,
//   dragging, scrolling and typing — dispatched as native trusted input (isTrusted===true,
//   navigator.webdriver stays false). It works whether you drive the page directly or via
//   locators:
//     page level    — page.click / hover / dblclick / type / fill / press / selectOption /
//                      check / uncheck / setChecked,
//                      page.mouse.move / click / dblclick / wheel and held-button drags
//                      (down -> move -> up), page.keyboard.type / press
//     locator level  — locator.click / type / fill / hover / dblclick / press /
//                      pressSequentially / clear / check / uncheck / setChecked / tap /
//                      selectOption / dragTo
//                      (routed through the humanized page methods; main-frame locators only,
//                      STRICT like Playwright, native fallback)
// `showCursor: true` — injects a red cursor dot that follows the real mousemove events.
//
// Mouse moves build an eased, slightly bowed cubic-bezier path SDK-side from the tracked cursor
// position (continuous — no snap back to 0,0) using Playwright's NATIVE mouse.move. Because it is
// native input, the button state is carried: down() -> move() -> up() is a real held-button drag
// (slider captchas work) with no separate CDP channel to desync. Typing goes key-by-key through
// the native keyboard (trusted; shift handled by the engine) with human inter-key timing. No
// special binary command required.
//
// Form input follows sdk/python/clearcote/_humanize.py (the reference); WHAT to press comes from
// ./forms.ts (shared test vectors with Python and .NET), HOW comes from this file. Every DOM read
// goes through the isolated world (./isolated.ts) or Playwright's utility-world queries, never
// page.evaluate / locator.evaluate.

import type { Browser, BrowserContext, Locator, Page } from "playwright-core";
import { errors as pwErrors } from "playwright-core";
import {
  makePersona, planMove, dragDwell, clickHold, keyDwell, clickPoint, planAmbient, gaussFrom, rolloverRate,
  dblclickGap, pauseMs, timingTraits,
  wheelNotches, platformFromNavigator, WHEEL_NOTCH_PX, type Box, type Persona, type Point, type Step, type WheelPlatform,
} from "./motion.js";
import {
  dwellMs, moveOffDelayMs, moveOffRoll, planDwell, planMoveOff,
} from "./motion.js";
import { ambientMode, exploreNotches, idleDrifts, readPauseMs, IDLE_DRIFT_MAX_MS, TEXT_LENGTH as ISO_TEXT_LENGTH } from "./attention.js";
import { keepInView, trimTo, walk } from "./pacing.js";
import { pressAndHold, type PressAndHoldOptions } from "./hold.js";
import {
  COVER_CHECK_MS, COVER_RETRY_MS, ISO_ACTIVE_FIELD, ISO_ACTIVE_VALUE, ISO_CSS_BOX, ISO_DESCRIBE, ISO_FORM_CONTROL,
  ISO_FRAMES, ISO_HIT_IN_BOX, ISO_LABEL_BY_SEL, ISO_LABEL_OF, ISO_LISTBOX_ROW, ISO_PLATFORM, ISO_SCROLL_BY, ISO_VIEWPORT,
  roundHalfEven, worldFor, type DescribedElement,
} from "./isolated.js";
import { NATIVE, frameLocatorOf, frameRoute, humanized as frameHumanized, strictMode } from "./frames.js";

export { reportFallback, strictMode } from "./frames.js";

/** The error a strict-mode tap raises on a touch context (lane F, SC 232). */
export const TAP_NOT_HUMANIZED = "tap is not humanized for touch contexts yet";

// Checkbox clicks on the <label> (setChecked). The persona fixes the TRAIT -- how often this identity clicks a
// visible checkbox's label rather than its box (persona.labelShare when the persona carries one, else 0.4) --;
// each click's choice is an UNSEEDED Math.random draw, like planMove's: a seeded per-identity stream would make
// every session of an identity choose the same way in the same order (a cross-session fingerprint).
export const LABEL_SHARE = 0.4;
/** The persona's trait: the share of visible-checkbox clicks that land on the label text. */
export function labelShare(persona: Persona): number {
  const v = (persona as any).labelShare;
  return typeof v === "number" && v >= 0 && v <= 1 ? v : LABEL_SHARE;
}
/** This click: on the label? An unseeded draw against the persona's trait (`rng`: a test's own). */
export function labelDraw(persona: Persona, rng: () => number = Math.random): boolean {
  return rng() < labelShare(persona);
}
import {
  DEFAULT_TYPO_RATE, NEARBY_LETTERS, classifyField, datetimeEqual, enginePathOk, fitMaxlength, formattedEqual, maskKept,
  matchOption, normalizeClickOptions, numericEqual, planDatetime, planSelect, planTypo, prePressMs, pyFloat,
  rangeAttrs, rangeFraction,
  rangeNudge, rangePoint, rangeSanitize, rangeValueOk, segmentNodesFromTree, typoEligible, typosAllowed,
  type ClickPlan, type FieldClass, type OptionInfo, type SelectPlan, type TypoField, type TypoPlan,
} from "./forms.js";
import {
  applyToPersona, engineModeFor, reportFallback, resolveHumanize, typoRateFor, withCallerApi,
  type HumanizeSetting,
} from "./hzoptions.js";

export { ClearcoteStrictError } from "./hzoptions.js";
export type {
  HumanizeAmbient, HumanizeEngine, HumanizeFallbackEvent, HumanizeOptions, HumanizePreset,
  HumanizeSetting,
} from "./hzoptions.js";

/** The humanize options of launch() / launchPersistentContext() (called HumanizeOptions up to 0.51.0; that
 * name is now the options object itself). */
export interface HumanizeLaunchOptions {
  /** Humanize all input (move/click/drag/scroll/type) as native trusted events: true, "strict" or a
   * HumanizeOptions object ({ preset, strict, engine, ambient, typoRate, handedness, deviceHz, inputDevice,
   * onFallback }). An unknown option throws. */
  humanize?: HumanizeSetting;
  /** Show a red cursor dot that tracks the motion (visualization). */
  showCursor?: boolean;
  /** @internal Fingerprint seed → a stable per-identity motor persona (cadence, tremor, overshoot,
   * handedness). Threaded from `fingerprint` by launch(); unset ⇒ a random persona per session. */
  seed?: string | number;
}

/** Overlay: a red dot that follows real mousemove events. Idempotent IIFE so it works both as
 * an addInitScript source string AND when passed to page.evaluate. */
export const CURSOR_OVERLAY = `(() => {
  if (window.__clearcoteCursor) return; window.__clearcoteCursor = 1;
  const make = () => {
    if (document.getElementById('__clearcote_cursor')) return;
    const d = document.createElement('div'); d.id = '__clearcote_cursor';
    d.style.cssText = 'position:fixed;left:0;top:0;width:20px;height:20px;margin:-10px 0 0 -10px;' +
      'border-radius:50%;border:2px solid #ff3b3b;background:rgba(255,59,59,.22);' +
      'box-shadow:0 0 10px rgba(255,59,59,.6);pointer-events:none;z-index:2147483647';
    (document.body || document.documentElement).appendChild(d);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', make); else make();
  document.addEventListener('mousemove', (e) => {
    const d = document.getElementById('__clearcote_cursor');
    if (d) { d.style.left = e.clientX + 'px'; d.style.top = e.clientY + 'px'; }
  }, true);
})();`;

const rand = (a: number, b: number) => a + Math.random() * (b - a);
const randInt = (a: number, b: number) => a + Math.floor(Math.random() * (b - a + 1)); // inclusive

// Keys a person rolls over (see humanTypeText): lower-case, unshifted, one physical key each.
// Shifted characters, Enter/Backspace and the typo path keep the plain one-press-at-a-time path.
const ROLL_KEYS = new Set("abcdefghijklmnopqrstuvwxyz0123456789 ,.;'/-=");

/** The persona platform when the page cannot say: the SDK's default persona follows the host. */
const hostWheelPlatform = (): WheelPlatform =>
  process.platform === "win32" ? "windows" : process.platform === "darwin" ? "macos" : "linux";

// The engine's pointer-path command. r34 renamed Browser.humanizedClick to Browser.dispatchPointerPath:
// "humanizedClick" is known vocabulary. The served /json/protocol still lists the old name (measured on
// r38, SC 210); r40 keeps the new one out of it. r33 and older only know the old
// name, so the new one goes first and the old one is the fallback for a method-not-found only.
/** @internal exported for tests */
export const POINTER_PATH = "Browser.dispatchPointerPath";
/** @internal exported for tests */
export const POINTER_PATH_LEGACY = "Browser.humanizedClick";

// Which name each browser answered to, so only its first call can pay for a miss. NO_ENGINE_PATH: the
// engine knew neither name -- remembered too, so a new page of that browser does not probe both again.
const engineMethodNames = new WeakMap<object, Map<string, string>>();
const NO_ENGINE_PATH = "";

// A CDP method-not-found: the engine predates the name. A raw CDP client sees code -32601;
// Playwright's CDPSession keeps only the message ("Protocol error (X): 'X' wasn't found").
const isMethodNotFound = (e: any): boolean =>
  e?.code === -32601 || /-32601|wasn't found|method not found/i.test(String(e?.message ?? e));

/** @internal exported for tests. Send `method`, or `legacy` when the engine predates the rename, and
 * remember per `cacheKey` (a Browser, or the context when there is none) which one answered -- or that
 * neither did, after which every call fails at once without a round trip. Any error other than
 * method-not-found propagates as-is: retrying a real failure under the old name would only replay it. */
export async function sendEngineCommand(
  session: { send(method: string, params?: any): Promise<any> }, cacheKey: object,
  method: string, legacy: string, params: Record<string, unknown>
): Promise<any> {
  let names = engineMethodNames.get(cacheKey);
  if (!names) { names = new Map(); engineMethodNames.set(cacheKey, names); }
  const known = names.get(method);
  if (known === NO_ENGINE_PATH) throw Object.assign(new Error(`'${method}' wasn't found`), { code: -32601 });
  if (known) return session.send(known, params);
  try {
    const r = await session.send(method, params);
    names.set(method, method);
    return r;
  } catch (e) {
    if (!isMethodNotFound(e)) throw e;
  }
  let r: any;
  try {
    r = await session.send(legacy, params);
  } catch (e) {
    if (isMethodNotFound(e)) names.set(method, NO_ENGINE_PATH);
    throw e;
  }
  names.set(method, legacy);
  return r;
}

// The longest an engine walk waits for the page to draw (ISO_FRAMES): measured 111-812 ms on a fresh
// headless browser's first page (r40, SC 216). A page that never draws (a hidden tab) walks after it.
const FRAMES_WAIT_MS = 1500;

/** @internal exported for tests. Did this browser's engine already say it has no pointer-path command? */
export function engineHasNoPointerPath(cacheKey: object | null): boolean {
  return !!cacheKey && engineMethodNames.get(cacheKey)?.get(POINTER_PATH) === NO_ENGINE_PATH;
}

/** @internal exported for tests. The page's default action timeout (setDefaultTimeout, else the
 * context's, else Playwright's 30 s). */
export function defaultTimeoutMs(page: any): number {
  try {
    const t = page?._timeoutSettings?.timeout({});
    if (typeof t === "number" && Number.isFinite(t)) return t;
  } catch { /* not this Playwright's shape */ }
  return 30_000;
}

/** @internal exported for tests. A Playwright TimeoutError for a trial whose checks never passed,
 * worded like Playwright's. */
export function trialTimeoutError(selector: string, ms: number, why: string): Error {
  const msg = `Timeout ${Math.round(ms)}ms exceeded.\nCall log:\n  - waiting for locator('${selector}')\n  - ${why}`;
  const E: any = (pwErrors as any)?.TimeoutError;
  if (typeof E === "function") return Object.assign(new E(msg), { [SDK_PW_ERROR]: true });
  return Object.assign(new Error(msg), { name: "TimeoutError", [SDK_PW_ERROR]: true });
}

// The characters a US-layout keyboard (Playwright's, and the engine's keyboard.getLayoutMap())
// types with Shift held: capitals and the shifted symbols.
const SHIFTED_SYMBOLS = new Set('~!@#$%^&*()_+{}|:"<>?');
/** @internal exported for tests */
export const needsShift = (ch: string): boolean => (ch >= "A" && ch <= "Z" && ch.length === 1) || SHIFTED_SYMBOLS.has(ch);
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
// Same unseeded live source as rand() — the seeded persona rng stays inside motion.ts, this is only
// for placing a point in a plausible spread instead of on an exact landmark.
const gauss = () => gaussFrom(Math.random);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// The letter-adjacency map now lives in ./forms.ts (shared with Python and .NET); kept under the old
// name for anything that imported it from here.
/** @internal */
export const NEARBY: Readonly<Record<string, string>> = NEARBY_LETTERS;

/** @internal exported for tests. Per-character fat-finger rate: the persona's own (timingTraits, 0.5–4 %
 * per identity; the global 2 % without a persona). CLEARCOTE_HUMANIZE_TYPO_RATE overrides it when set (0
 * disables typos; the test suites raise it to exercise the correction paths). Read at call time, like
 * Python's typo_rate(). */
export function typoRate(persona?: Persona | null): number {
  const v = process.env.CLEARCOTE_HUMANIZE_TYPO_RATE;
  if (v === undefined || v === "") return persona ? timingTraits(persona).typoRate : DEFAULT_TYPO_RATE;
  const f = pyFloat(v);
  if (f === null || Number.isNaN(f)) return DEFAULT_TYPO_RATE;
  return Math.min(1.0, Math.max(0.0, f));
}

/** @internal exported for tests. A Playwright timeout: the element never became actionable. Falling
 * back to the native call after one would wait the FULL timeout again (a missing element took 2-3x
 * `timeout` to fail), so timeouts propagate instead. */
export function isTimeout(err: unknown): boolean {
  return (err as any)?.name === "TimeoutError";
}

// Playwright words every error after the API the caller used: "page.click: Timeout 800ms exceeded.",
// "locator.check: Error: Not a checkbox or radio button". A humanized call rejected with the error of
// whatever Playwright call it made inside (locator.waitFor, locator.isChecked; verifier B, SC 210) --
// the same class, but a message a caller matching on it would not recognise. Pre-existing in 0.48.0.
const API_PREFIX = /^[A-Za-z_]+\.[A-Za-z_]+: /;
/** Marks an error the SDK raises in Playwright's place (a trial that never passed, a checkbox that would
 * not change): it gets the caller's API prefix too. */
const SDK_PW_ERROR = Symbol.for("clearcote.playwrightError");

/** @internal exported for tests. `err` worded as Playwright words an error of `api` ("page.click",
 * "locator.setChecked"): the inner call's "locator.waitFor: " prefix replaced (or, for an error the SDK
 * raised in Playwright's place, added), in the message and the stack's first line. The object, and so
 * its class, is kept. Anything else is returned as it is. */
export function asApiError<T>(err: T, api: string): T {
  const e: any = err;
  if (!(e instanceof Error) || typeof e.message !== "string") return err;
  const m = API_PREFIX.exec(e.message);
  if (!m && !(err as any)[SDK_PW_ERROR] && e.name !== "TimeoutError") return err;
  const msg = api + ": " + (m ? e.message.slice(m[0].length) : e.message);
  if (msg === e.message) return err;
  const old = e.message;
  try {
    e.message = msg;
    if (typeof e.stack === "string") e.stack = e.stack.replace(old, msg);
  } catch { /* a frozen error keeps its words */ }
  return err;
}

/** @internal `fn`, with every Playwright error it rejects with worded as `api`'s (asApiError). */
export function apiErrors<A extends any[], R>(api: string, fn: (...a: A) => Promise<R>): (...a: A) => Promise<R> {
  return async (...a: A): Promise<R> => withCallerApi(api, async () => {
    try {
      return await fn(...a);
    } catch (e) {
      throw asApiError(e, api);
    }
  });
}

/** @internal An Error the SDK raises where Playwright would, worded by the caller's API (apiErrors). */
export function sdkPlaywrightError(message: string): Error {
  return Object.assign(new Error(message), { [SDK_PW_ERROR]: true });
}

// The form-route boxes are viewport CSS px, like Playwright's boundingBox().
type Rect = Box;
type NativeFn = (selector: string, options?: any) => Promise<any>;

/** Internal: the humane route cannot apply here; run the native Playwright call instead. */
class NativeSignal extends Error {
  constructor(why = "the humane route cannot apply") { super(why); this.name = "NativeSignal"; }
}

/** Why a humanized route handed over: a NativeSignal's reason, else the error it met. */
function whyOf(e: unknown): string {
  return e instanceof NativeSignal ? e.message : "error: " + String((e as any)?.message ?? e);
}

/** @internal exported for tests. A per-page lock: each wrapped public entry point (page.click / fill
 * / type / press / selectOption / check / uncheck / setChecked / dblclick / hover, mouse.click /
 * dblclick, keyboard.type / press) runs one at a time. Without it a `Promise.all` of two fills on one
 * page interleaved their clicks and keys and left each field with part of the other's text: a page has
 * one pointer and one keyboard focus. Internal steps call internal helpers, never a locked entry point,
 * so nothing waits on itself. A failed call releases the lock like a successful one. */
export function createInputLock(): <T>(fn: () => Promise<T>) => Promise<T> {
  let tail: Promise<unknown> = Promise.resolve();
  return <T>(fn: () => Promise<T>): Promise<T> => {
    const run = tail.then(() => fn());
    tail = run.then(() => undefined, () => undefined);
    return run;
  };
}

/** Wrap a single page's input methods + (optionally) inject the cursor overlay. */
/** Which clicks the engine pointer path (Browser.dispatchPointerPath) may walk, from
 * CLEARCOTE_HUMANIZE_ENGINE_CLICKS: "off" (also 0/false/no) never; "all" (also "selector") coordinate
 * clicks AND targeted page.click(selector) / locator.click() clicks; anything else (the default)
 * coordinate clicks only, as before.
 *
 * Targeted clicks stay on the SDK planner by default (SC 210 lane S, measured on r38): the engine
 * presses 9 ms after its last move (the planner's press waits 40-130 ms), and it walks from its own last
 * position, which an SDK glide does not update. Same values as Python `_engine_clicks_mode`. */
export function engineClicksMode(): "off" | "coords" | "all" {
  const v = (process.env.CLEARCOTE_HUMANIZE_ENGINE_CLICKS || "").trim().toLowerCase();
  if (["0", "off", "false", "no", "none"].includes(v)) return "off";
  if (["all", "selector", "selectors"].includes(v)) return "all";
  return "coords";
}

export async function attachHumanize(browser: Browser | null, page: Page, opts: HumanizeLaunchOptions): Promise<void> {
  // IDEMPOTENCY GUARD — do not remove.
  //
  // installHumanize wraps BOTH browser.newPage and browser.newContext. Playwright's newPage()
  // creates a context internally, so the wrapped newContext runs installHumanizeOnContext (which
  // attaches on the "page" event) and then the outer newPage wrapper attaches a second time.
  //
  // Without this guard the mouse is wrapped twice, and the second
  //     const nativeMove = mouse.move.bind(mouse)
  // captures the FIRST WRAPPER rather than the real native move — so every step of the outer
  // planned path triggers an entire inner planned path. Measured on the same ~980px move:
  //     browser.newPage()   51338 ms   2946 samples
  //     context.newPage()    2833 ms    161 samples   <- correct (planner caps at ~180)
  // i.e. an 18x blow-up on the most common entry point in the API.
  if ((page as any).__clearcoteHumanized) return;
  (page as any).__clearcoteHumanized = true;

  if (opts.showCursor) {
    const inject = () => page.evaluate(CURSOR_OVERLAY).catch(() => {});
    inject();
    page.on("load", () => { inject(); });
    page.on("framenavigated", (f) => { if (f === page.mainFrame()) inject(); });
  }
  // humanize: true | "strict" | HumanizeOptions (hzoptions.ts). true is {}: every default reproduces 0.51.0,
  // and no option draws from the persona RNG (same seed, same persona).
  const hz = resolveHumanize(opts.humanize);
  if (!hz) return;
  (page as any)._clearcoteHz = hz;
  const tempo = hz.tempo; // preset: every planned duration x1.4 / x1.0 / x0.7
  // The stock methods, for per-call { humanize: false } (wired at the end of attach).
  const stockPage: Record<string, any> = {}, stockMouse: Record<string, any> = {}, stockKb: Record<string, any> = {};
  for (const n of PAGE_METHODS) if (typeof (page as any)[n] === "function") stockPage[n] = (page as any)[n].bind(page);
  for (const n of MOUSE_METHODS) if (typeof (page.mouse as any)[n] === "function") stockMouse[n] = (page.mouse as any)[n].bind(page.mouse);
  for (const n of KB_METHODS) if (typeof (page.keyboard as any)[n] === "function") stockKb[n] = (page.keyboard as any)[n].bind(page.keyboard);

  // Per-identity motor persona (cadence, tremor, overshoot, handedness) — stable across the session,
  // seeded from the fingerprint so behavior is consistent within an identity and unlinkable across
  // seeds. Stored on the page so the module-level Locator.dragTo patch can reach it too.
  const persona: Persona = applyToPersona(makePersona(opts.seed), hz);
  (page as any)._clearcotePersona = persona;
  // One input action at a time on this page (see createInputLock). Inside the lock the outermost action
  // first runs the lazy after-action step (SC 232 lane C: afterRun). `nextCall` carries what the public
  // wrapper knew about THIS call to the locked() it reaches synchronously: a trial performs no action (so
  // nothing after-action either); a coordinate action names a viewport point (no exploratory scroll).
  const lockRaw = createInputLock();
  let nextCall: { coords: boolean; after: boolean } | null = null;
  const locked = <T>(fn: () => Promise<T>): Promise<T> => {
    const o = nextCall ?? { coords: false, after: true };
    nextCall = null;
    return lockRaw(() => afterRun(fn, o));
  };

  // Tracked cursor position so each move continues from where the last one ended.
  // `held` tracks whether a mouse button is currently down — see mouse.move below.
  // `placed` records whether the engine's cursor has actually been moved yet. A page starts with the
  // pointer at the default origin, so anything that delivers input WITHOUT a target to move to —
  // a wheel, a mouse.down that opens a drag, a bare keyboard.type — otherwise happens from a cursor
  // that has never been anywhere. The guarantees below key off this flag.
  const st = { x: rand(140, 380), y: rand(90, 240), held: false, placed: false };
  // After-action state (SC 232 lane C; see the block at the end of attachHumanize).
  const af = {
    // per-action draws are unseeded, as in planMove: the seed fixes the persona's traits only
    rng: Math.random as () => number, depth: 0, lastEnd: null as number | null, loadAt: null as number | null,
    mo: null as { due: number; x: number; y: number } | null, moHits: 0, moN: 0, trace: [] as unknown[][],
  };

  const mouse: any = page.mouse;
  const nativeMove = mouse.move.bind(mouse);
  const nativeClick = mouse.click.bind(mouse);
  const nativeWheel = mouse.wheel.bind(mouse);
  const nativeDown = mouse.down.bind(mouse);
  const nativeUp = mouse.up.bind(mouse);

  // Walk a planned trajectory (motion.ts) with native (trusted) mouse.move + off-protocol sleeps.
  // Native input carries the button state, so a button pressed via mouse.down() stays held across
  // the whole path — the same code powers a free move AND a held-button drag.
  //
  // Paced against a WALL CLOCK (pacing.ts): each sample goes out at its planned time and late ones
  // are skipped (never the last, and no skip hops more than 40 px, or 20 px once nothing has been
  // dispatched for 40 ms: MAX_JUMP_PX / IDLE_HOP_PX). "Move, then sleep the planned gap" added the
  // ~27 ms mouse.move round trip to every 6-8 ms sample: a 1.05 s planned glide took 3.5-5.4 s on
  // PRO r38 (SC 209), ambient motion 5.7x as long.
  const dispatch = async (steps: Step[]): Promise<void> => {
    const vp = await viewBounds();
    if (tempo !== 1) steps = steps.map((s) => ({ ...s, sleepMs: s.sleepMs * tempo }));
    await walk(vp ? keepInView(steps, vp.width, vp.height) : steps, async (x, y) => { await nativeMove(x, y); st.placed = true; });
  };
  // The viewport a planned path is kept inside (keepInView: the planner's overshoot arcs left the window
  // near its edges, verifier B), read in the isolated world at most every 2 s; null when it cannot be read.
  let vb: { t: number; vp: { width: number; height: number } | null } | null = null;
  const viewBounds = async (): Promise<{ width: number; height: number } | null> => {
    const now = Date.now();
    if (vb === null || now - vb.t > 2000) {
      let vp: { width: number; height: number } | null = null;
      try {
        const wh = await worldFor(page).evaluate<[number, number]>(ISO_VIEWPORT);
        if (wh && wh[0] && wh[1]) vp = { width: Number(wh[0]), height: Number(wh[1]) };
      } catch { /* detached / navigating / no CDP: no clamp */ }
      vb = { t: now, vp };
    }
    return vb.vp;
  };

  // Move to (x, y) as a minimum-jerk sum-of-submovements path (primary ballistic + corrective homing)
  // with colored sub-pixel noise and Fitts-scaled duration. `settle` adds a seating jiggle (drag).
  const glide = async (x: number, y: number, o: { settle?: boolean; targetW?: number; held?: boolean } = {}): Promise<void> => {
    await dispatch(planMove(st, { x, y }, persona, { settle: o.settle, targetW: o.targetW, held: o.held }));
    st.x = x; st.y = y;
  };

  // Engine real-trajectory routing (PRO): send point-to-point moves/clicks through
  // Browser.dispatchPointerPath (Browser.humanizedClick before r34, see sendEngineCommand; real
  // recorded human paths for PRO, engine bezier for free, tiered by the engine license gate).
  // Held-button DRAGS stay native; falls back to the SDK bezier if the method is unavailable
  // (older engine / no browser session).
  const eng: { session: any; key: object | null; tid: string | null; ok: boolean; at: Point | null } =
    { session: null, key: null, tid: null, ok: true, at: null };
  // The engine walks from ITS OWN last position on this page, not from where an SDK glide left the
  // pointer. Measured on r38 (SC 210): an engine click at (300,300), an SDK glide to (800,550), then
  // mouse.click(400,200) through the engine -- its first sample was at (300,300), a 559 px teleport
  // from where the pointer was. So the engine walks only while nothing else has moved the pointer
  // since its own last walk (before its first walk: while nothing has moved it at all).
  const engineInSync = (): boolean => (eng.at === null ? !st.placed : st.x === eng.at.x && st.y === eng.at.y);
  const engineGlide = async (x: number, y: number, noClick: boolean): Promise<boolean> => {
    if (!eng.ok || !engineInSync()) return false;
    try {
      // This browser's engine already said it has neither command: no sessions, no probe.
      const ctx0: any = page.context();
      if (engineHasNoPointerPath((ctx0.browser ? ctx0.browser() : null) ?? ctx0)) { eng.ok = false; return false; }
      if (eng.session === null) {
        const ctx: any = page.context();
        const tsession = await ctx.newCDPSession(page);
        const info: any = await tsession.send("Target.getTargetInfo");
        eng.tid = info.targetInfo.targetId;
        const br: any = ctx.browser ? ctx.browser() : null;
        eng.key = br ?? ctx; // the command name is a property of the engine: learn it once per browser
        eng.session = br ? await br.newBrowserCDPSession() : tsession;
      }
      // The engine does not wait for the page: what it walks into a page that has not drawn yet is lost
      // (a fresh headless browser's first page, SC 216). Wait for two frames first; free once drawn.
      await worldFor(page).evaluate(ISO_FRAMES, FRAMES_WAIT_MS, true);
      const dx = x - st.x, dy = y - st.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const dur = Math.min(1.10, Math.max(0.28, 0.30 + dist / 1700)) * (0.85 + 0.30 * Math.random()) * tempo;
      const tSend = Date.now();
      await sendEngineCommand(eng.session, eng.key!, POINTER_PATH, POINTER_PATH_LEGACY,
        { targetId: eng.tid, x, y, duration: dur, noClick });
      // The engine walks the trajectory in the BROWSER process and answers this command before it
      // has finished. Without waiting, a following mouse.down() fires at the cursor's STALE
      // position — measured: a move to (110,58) then a press landed at (981,629), on <body>
      // instead of the target, so every move-then-press sequence (drag, press-and-hold,
      // drag-and-drop) acted on the wrong element.
      // r38 answers only after the walk AND the click (send returned 772 ms after a 600 ms walk, 2 ms
      // after the release, SC 210): wait only for what is left of `dur`, so an engine that answers
      // late does not add a second, idle `dur` after every click.
      await sleep(Math.max(0, dur * 1000 - (Date.now() - tSend)));
      try { await nativeMove(x, y); } catch { /* keep playwright cursor pos in sync for drags */ }
      st.x = x; st.y = y; st.placed = true;
      eng.at = { x, y };
      return true;
    } catch { eng.ok = false; return false; }
  };

  // The REAL viewport, in CSS px — NOT page.viewportSize().
  //
  // installHeadedViewport (index.ts) forces `viewport: null` on every headed newPage/newContext,
  // and launchPersistentContext does the same, so that innerWidth tracks the real OS window
  // instead of Playwright's emulated 1280x720 (an emulated viewport on a headed window is itself
  // a tell). With viewport:null, viewportSize() returns null for the LIFE of the page — so the
  // `|| {1280, 800}` fallback was taken on every headed launch, and the scroll anchor's
  // "is the pointer inside the viewport" gate was evaluated against a box unrelated to the
  // window. On a maximized display a cursor legitimately at x=1400 read as out-of-bounds and got
  // re-homed on EVERY scroll — precisely the behaviour that gate exists to prevent.
  // innerWidth/innerHeight are correct in both modes; the constant is a last resort only.
  let vpCache: { width: number; height: number } | null = null;
  const viewport = async (): Promise<{ width: number; height: number }> => {
    try {
      const wh = await worldFor(page).evaluate<[number, number]>(ISO_VIEWPORT);
      if (wh && wh[0] && wh[1]) { vpCache = { width: wh[0], height: wh[1] }; return vpCache; }
    } catch { /* detached/navigating — fall through */ }
    return (page.viewportSize && page.viewportSize()) || vpCache || { width: 1280, height: 800 };
  };

  // Non-goal cursor activity (idle drift + a few excursions). It never presses a button and never
  // approaches a caller-named target, so it is also the safe way to give the cursor a position when
  // an action needs one but names no coordinates.
  const ambient = async (ms: number): Promise<void> => {
    try {
      // planAmbient overruns its budget (whole excursions, rests drawn at 0.9-1.4x a frame): cut the
      // plan at a slow sample near ms so ambientMotion(ms) lasts about ms.
      const steps = trimTo(planAmbient(st, await viewport(), persona, ms), ms);
      await dispatch(steps);
      if (steps.length) { st.x = steps[steps.length - 1].x; st.y = steps[steps.length - 1].y; }
    } catch { /* best-effort ambient */ }
  };

  // Give the cursor a position for input that names no coordinates (a bare keyboard.type). It is
  // ambient only: it never presses, so it cannot activate anything it happens to pass over.
  //
  // NOT used before mouse.down — see that wrapper. Before any real move st.x/st.y is the RANDOM
  // SEED assigned at attach, so pressing at the end of this walk would put a TRUSTED click at an
  // arbitrary page coordinate the caller never named, which can land on a link and navigate.
  const ensurePlaced = async (): Promise<void> => {
    if (st.placed) return;
    await ambient(rand(260, 560));
    if (!st.placed) {
      try { await nativeMove(st.x, st.y); st.placed = true; } catch { /* best-effort placement */ }
    }
  };

  // A HELD BUTTON MUST STAY ON THE NATIVE PATH. The engine trajectory builds its own
  // WebMouseEvents and cannot hold a button across them, so routing a drag leg through it emits
  // moves reporting MouseEvent.buttons 0 in the middle of a press. That is both a contradiction no
  // real mouse produces (buttons is derived from physical state) and a functional break — a range
  // thumb, a map or a drop target ignores a move that says nothing is down. Measured before this
  // guard: 672 of 829 held moves reported buttons 0, and a slider dragged 10%->90% never left 0.
  // A BARE MOVE ALWAYS USES THE NATIVE PATH — never the engine trajectory. Two independent
  // reasons, both measured on the Python sync file which shares this design:
  //   * a held button cannot be carried across the engine's own WebMouseEvents, so a drag leg
  //     routed through it reports MouseEvent.buttons 0 mid-press and a range thumb ignores it
  //     (349 of 354 held moves reported buttons 0; the slider moved 0 -> 1 and stopped);
  //   * the engine walks its path ASYNCHRONOUSLY in the browser process and answers before it has
  //     finished, so it keeps moving the cursor AFTER the pre-press pin in mouse.down — a move to
  //     (56,120) then down() pressed on HTML at (856,213) instead of the INPUT, and the slider
  //     stayed at 0. Re-pinning right before the press does not help; the overrun outlives it.
  // mouse.click keeps engine routing: it performs move+click entirely engine-side, where nothing
  // races the trajectory. glide() is still fully humanized (minimum-jerk, colored noise, Fitts).
  mouse.move = async (x: number, y: number) => {
    // A move while the button is held is a DRAG leg: planDrag keeps it on the line between grab and
    // drop (an aimed reach's arc took a slider handle 47 px off a 40 px track, SC 209).
    await glide(x, y, { held: st.held });
  };
  // down/up are wrapped to TRACK the held state mouse.move needs, and — critically — to make the
  // press position DETERMINISTIC. The engine answers its pointer path before the trajectory has
  // finished walking, and it overruns the `duration` it reports, so sleeping that duration is not
  // enough: measured on Node, a move to (110,58) was still mid-flight at (416,504) when the press
  // fired, landing on <body>. Re-issuing a native move to the tracked target immediately before
  // the press pins the cursor to where the caller asked, whatever the trajectory is doing.
  // The pause between the pointer coming to rest and the button going down (forms.prePressMs: 40-250
  // ms, right-skewed, centred per identity). Measured on r38 before it: the focus click before typing
  // pressed ~4 ms after the last pointermove, a drag grab ~21 ms.
  const prePress = async (): Promise<void> => { await sleep(prePressMs(persona.seed) * tempo); };
  // What is left of a button hold once mouse.down() has returned: the hold runs from when the press was
  // SENT. mouse.down() returns only after the page handled the press, and a <select> opens its popup
  // inside that, so a hold slept afterwards reached the page ~30-80 ms longer than planned.
  const holdRest = (holdMs: number, t0: number): number => Math.max(0, holdMs - (Date.now() - t0));
  // Press and release the left button where the last glide left the pointer, after the pre-press
  // pause. Native down/up, so nothing moves: Playwright's mouse.click(x, y) sends one more mouseMoved
  // first, and THAT was the last pointermove the page saw, ~4 ms before the press.
  const pressHere = async (x: number, y: number, holdMs?: number): Promise<void> => {
    const hold = holdMs ?? clickHold(persona);
    await prePress();
    const t0 = Date.now();
    await nativeDown();
    try { await sleep(holdRest(hold, t0)); } finally { await nativeUp(); } // never leave the button down
  };
  mouse.down = async (...a: any[]) => {
    // DELIBERATELY NO PLACEMENT WHEN THE POINTER HAS NEVER MOVED.
    //
    // An earlier revision called ensurePlaced() here and pressed wherever the ambient walk ended.
    // But before any real move st.x/st.y is the RANDOM SEED assigned at attach — not a coordinate
    // the caller named — so that converted a harmless no-op press at the origin into a TRUSTED
    // click at an arbitrary point on the page, which can hit a link and navigate. That is a worse
    // failure than the one it was meant to fix. A bare down() with no preceding move is a caller
    // mistake, and humanize must not invent a target to paper over it; callers who want a
    // humanized drag move first, and that move glides and sets `placed`.
    if (st.placed) {
      // Re-pin only once a real move has happened — that is what makes st.x/st.y correspond to
      // where the engine's cursor actually is. The engine answers its pointer path before its
      // trajectory has finished, so without this the press can fire at a stale position.
      try { await nativeMove(st.x, st.y); } catch { /* best effort: keep the press on target */ }
      // The hand has arrived; it confirms the target before the button goes down. The pin is itself
      // a pointermove, so the pause comes after it. A down() with no move before it stays instant.
      await prePress();
    }
    st.held = true;
    try {
      return await nativeDown(...a);
    } catch (e) {
      st.held = false; // a raising press must not strand held=true and make every later move a drag leg
      throw e;
    }
  };
  mouse.up = async (...a: any[]) => { try { return await nativeUp(...a); } finally { st.held = false; } };
  // The engine path performs ONE plain left click. Anything else -- right/middle button, a double
  // click, a caller-chosen hold -- glides SDK-side and presses as asked (pressAt); the old wrapper
  // dropped those options and always left-clicked once.
  const hclick = async (x: number, y: number, options: any = {}): Promise<void> => {
    const o = normalizeClickOptions(options);
    if (enginePathOk(o) && engineModeFor(page, engineClicksMode) !== "off" && await engineGlide(x, y, false)) return;
    await glide(x, y);
    // A press that rejects REJECTS (it was swallowed here: a click whose press failed reported success).
    // Only the pin before a press (mouse.down) stays best-effort.
    if (o.modifiers.length && !(nativeKbDown && nativeKbUp)) {
      await prePress(); // the hand confirms the target before it presses
      // delay = the mousedown→mouseup HOLD (the persona's); without it Playwright releases in ~2 ms.
      await nativeClick(x, y, { delay: o.delay ?? clickHold(persona), button: o.button, clickCount: o.clickCount });
    } else {
      await pressAt(o);
    }
    // Not after an engine click (returned above): a glide the engine does not see would take the next
    // coordinate click off the engine path (engineInSync).
    scheduleMoveOff();
  };
  mouse.click = (x: number, y: number, options: any = {}) => locked(() => hclick(x, y, options));
  // Stock dblclick teleported and pressed for ~0 ms: route it through the humanized click.
  if (typeof mouse.dblclick === "function") {
    mouse.dblclick = (x: number, y: number, options: any = {}) => {
      const { click_count: _cc, ...rest } = options ?? {};
      return locked(() => hclick(x, y, { ...rest, clickCount: 2 }));
    };
  }

  // Opt-in ambient / pre-challenge cursor activity: idle drift + a few non-goal moves so a behavioral
  // collector sees non-zero pointer entropy BEFORE the first goal action (the biggest reason a
  // challenge/slider is shown to headless automation). `await page.ambientMotion(ms)`.
  (page as any).ambientMotion = async (ms = 1200): Promise<void> => { await ambient(ms); };

  // Held-button drag leg with the seating jiggle (settle), reachable from the module-level
  // Locator.dragTo patch (which can't see this closure) via the page object.
  (page as any)._clearcoteHeldGlide = async (x: number, y: number) => { await glide(x, y, { settle: true }); };

  // OPT-IN: no ambient burst on load by default (it interleaves with caller-driven input,
  // e.g. a slider drag). Call page.ambientMotion(ms) explicitly, or set
  // page._clearcoteAutoAmbient = true to restore the per-load burst.
  (page as any)._clearcoteAutoAmbient = hz.ambient === "load";
  (page as any)._clearcoteAmbient = hz.ambient ?? "off"; // read by the attention/idle code (lane C)
  page.on("load", () => {
    // ambient: "load" (lane A's option, read defensively) is the same per-load burst as the flag.
    if ((page as any)._clearcoteAutoAmbient === true || ambientMode(page) === "load") {
      Promise.resolve((page as any).ambientMotion(rand(450, 950))).catch(() => {});
    }
  });
  // The attention model's read pause counts from the last load (read lazily by the next action).
  page.on("load", () => { af.loadAt = Date.now(); });
  // A wheel event carries the pointer position, and until something moves the cursor that position
  // is the default origin — so a scroll right after a page load reported the mouse parked in the
  // corner, over nothing it was scrolling, while the deltas rolled down the middle of the document.
  // Move to where someone reading this page would have left the cursor first.
  //
  // Only when the current position is unusable (never placed, or outside the viewport). A hand does
  // not lift off the mouse between two scrolls of the same page, so re-homing on every wheel call
  // would replace one tell with another.
  const scrollAnchor = async (): Promise<void> => {
    try {
      const vp = await viewport();
      if (st.placed && st.x > 6 && st.y > 6 && st.x < vp.width - 6 && st.y < vp.height - 6) return;
      // Upper-middle of the reading column, gaussian-jittered: an exact viewport centre is as
      // machine-made as the origin is, so the anchor has to be a distribution, not a landmark.
      const x = clamp(vp.width * 0.48 + gauss() * vp.width * 0.10, vp.width * 0.18, vp.width * 0.82);
      const y = clamp(vp.height * 0.36 + gauss() * vp.height * 0.09, vp.height * 0.14, vp.height * 0.62);
      await glide(x, y);
      await sleep(rand(60, 180)); // eyes land before the finger rolls the wheel
    } catch { /* best-effort: scroll from wherever the cursor is */ }
  };

  // Pixels per wheel notch for the platform this page claims (WHEEL_NOTCH_PX). Read once per page,
  // lazily on the first wheel, in the isolated world like every other humanize read. A page that
  // cannot say (closed, mid-navigation, no CDP) gets the host's platform. Kept for the life of the
  // page either way: a mouse does not change its notch between two scrolls.
  let notchPx: number | null = null;
  const wheelNotchPx = async (): Promise<number> => {
    if (notchPx === null) {
      let plat: WheelPlatform | null = null;
      try { plat = platformFromNavigator(await worldFor(page).evaluate<string>(ISO_PLATFORM)); } catch { /* host below */ }
      notchPx = WHEEL_NOTCH_PX[plat ?? hostWheelPlatform()];
    }
    return notchPx;
  };

  /** Scroll in WHOLE NOTCHES, the only thing a wheel emits. Humanize scrolls whole notches: the
   * delivered total is the requested distance rounded to the nearest notch (at least one), not the
   * exact pixel count.
   *
   * A CDP wheel event always reports wheelDelta ±120 (one notch), whatever its distance. The eased
   * slices this replaced (39/28/20/10/3 px for a 100 px scroll, each at wheelDelta 120, measured on
   * r32/r33) were events no mouse produces: a real notch moves 100 px on Windows, 120 on Linux, 40 on
   * macOS. One axis per event, like a real wheel or tilt, and the notches come in finger-flick bursts. */
  mouse.wheel = async (dx: number, dy: number) => {
    await scrollAnchor();
    const notch = await wheelNotchPx();
    const plan: [number, number][] = [];
    for (let k = wheelNotches(dy, notch); k > 0; k--) plan.push([0, Math.sign(dy) * notch]);
    for (let k = wheelNotches(dx, notch); k > 0; k--) plan.push([Math.sign(dx) * notch, 0]);
    let burst = randInt(2, 5);
    for (let i = 0; i < plan.length; i++) {
      await nativeWheel(plan[i][0], plan[i][1]);
      if (i === plan.length - 1) break;
      // local sleep, NOT page.waitForTimeout: the latter is a CDP round-trip per call, so it
      // emits protocol traffic bot-detectors (e.g. reCAPTCHA) can score — a self-inflicted tell
      // inside the humanize path. setTimeout is off-protocol.
      if (--burst === 0) {
        await sleep(rand(110, 320)); // the finger resets for the next flick
        burst = randInt(2, 5);
      } else {
        await sleep(rand(18, 55));
      }
      if (Math.random() < 0.07) await sleep(rand(150, 450)); // occasional mid-scroll pause (reading)
      // A hand resting on the mouse does not hold it perfectly still through a long scroll. Without
      // this a wheel burst is the one input where the pointer coordinate never changes at all. Never
      // while a button is down, where a move is a drag leg.
      if (plan.length >= 6 && !st.held && Math.random() < 0.06) {
        try { await glide(st.x + gauss() * 3.5, st.y + gauss() * 2.5); } catch { /* drift is cosmetic */ }
      }
    }
  };

  // ----------------------------------------------------------------- keyboard
  const keyboard: any = page.keyboard;
  const nativeKbType = keyboard.type.bind(keyboard);
  const nativeKbPress = keyboard.press.bind(keyboard);
  // A bulk fill's paste (Input.insertText).
  const nativeKbInsert = typeof keyboard.insertText === "function" ? keyboard.insertText.bind(keyboard) : undefined;
  // A page-like keyboard without down/up still gets every other humanization; it just types
  // capitals without the Shift key, as before.
  const nativeKbDown = typeof keyboard.down === "function" ? keyboard.down.bind(keyboard) : undefined;
  const nativeKbUp = typeof keyboard.up === "function" ? keyboard.up.bind(keyboard) : undefined;

  // Emit one character with a human keydown→keyup DWELL. keyboard.press's `delay` IS the hold
  // (keyboard.type's delay is only inter-key flight), so per-char press gives the missing dwell.
  // Fall back to type() for anything press can't map (some symbols / composed chars).
  const emitKey = async (ch: string): Promise<void> => {
    try { await nativeKbPress(ch, { delay: keyDwell(persona) }); }
    catch { try { await nativeKbType(ch); } catch { /* best-effort */ } }
  };

  // Press `k` (n times) with the persona's hold on every press and a human gap between repeats.
  const key = async (k: string, n = 1, gap: [number, number] = [60, 150]): Promise<void> => {
    for (let j = 0; j < n; j++) {
      await nativeKbPress(k, { delay: keyDwell(persona) });
      if (j < n - 1) await sleep(pauseMs(persona, gap[0], gap[1]));
    }
  };

  // Press at the point the last glide ended, the way the options ask: the button, the click count (a
  // double click is two presses with a human gap, not one event), the caller's hold (`delay`) or the
  // persona's, and modifiers held around the whole press with a lead before it (80-360 ms: 40-110 ms
  // per modifier plus the 40-250 ms pre-press pause) and a 20-80 ms lag after, as fingers do. Native
  // down/up, so nothing moves.
  const pressAt = async (o: ClickPlan): Promise<void> => {
    const held: string[] = [];
    try {
      for (const m of o.modifiers) {
        await nativeKbDown!(m);
        held.push(m);
        await sleep(rand(40, 110));
      }
      await prePress(); // the hand confirms the target before it presses
      const n = o.clickCount;
      for (let i = 1; i <= n; i++) {
        const hold = o.delay ?? clickHold(persona);
        const t0 = Date.now();
        await nativeDown({ button: o.button, clickCount: i });
        try {
          await sleep(holdRest(hold, t0));
        } finally {
          await nativeUp({ button: o.button, clickCount: i }); // never leave the button down
        }
        if (i < n) await sleep(dblclickGap(persona)); // the gap inside a double click: the persona's
      }
    } finally {
      for (const m of held.reverse()) {
        await sleep(rand(20, 80));
        try { await nativeKbUp!(m); } catch { /* best-effort */ }
      }
    }
  };

  // The focused field's current value, read in the isolated world (null if unknown).
  const activeValue = async (): Promise<string | null> => {
    try {
      const v = await worldFor(page).evaluate<string | null>(ISO_ACTIVE_VALUE);
      return typeof v === "string" ? v : null;
    } catch { return null; }
  };

  // Node's inter-key flight (the cadence humanTypeText uses between characters).
  const flightMs = () => rand(45, 150);

  const backspaces = async (n: number): Promise<void> => {
    for (let k = 0; k < n; k++) {
      await nativeKbPress("Backspace", { delay: keyDwell(persona) });
      if (k < n - 1) await sleep(rand(70, 150));
    }
  };

  // How many characters arrived since `before` (null = cannot tell).
  const landed = (before: string | null, now: string | null): number | null =>
    before === null || now === null ? null : Array.from(now).length - Array.from(before).length;

  // Make the planned slip at chars[i] and correct it. Returns true when it already typed chars[i]
  // itself (the doubled-key case), so the caller must not type it again.
  //
  // Only what actually LANDED is deleted: an input mask or a maxlength can swallow the wrong key, and
  // a Backspace pressed for a key that never arrived deletes a real character (the
  // letter-typo-in-a-numeric-mask failure). Measured before and after the slip, in the isolated world,
  // one read each -- typos are ~2 % of keys, so this costs nearly nothing.
  const doTypo = async (chars: string[], i: number, tp: TypoPlan): Promise<boolean> => {
    const before = await activeValue();
    if (tp.kind === "sub") {
      await emitKey(tp.wrong);
      let got = landed(before, await activeValue());
      if (got !== null && got <= 0) return false; // swallowed: nothing to correct
      let typed = 1;
      for (const c of chars.slice(i + 1, i + 1 + tp.late)) { // late notice: a key or two more
        await sleep(flightMs());
        await emitKey(c);
        typed++;
      }
      await sleep(rand(140, 340)); // noticing
      got = landed(before, await activeValue());
      await backspaces(got === null ? typed : Math.max(0, Math.min(typed, got)));
      await sleep(rand(80, 200));
      return false;
    }
    if (tp.kind === "double") {
      await emitKey(chars[i]);
      await sleep(rand(45, 95)); // the bounce comes fast
      await emitKey(chars[i]);
      const got = landed(before, await activeValue());
      await sleep(rand(140, 340));
      const extra = got === null ? 1 : Math.max(0, got - 1);
      if (extra) {
        await backspaces(extra);
        await sleep(rand(80, 200));
      }
      return true;
    }
    if (tp.kind === "swap") {
      await emitKey(chars[i + 1]);
      await sleep(flightMs());
      await emitKey(chars[i]);
      const got = landed(before, await activeValue());
      await sleep(rand(140, 340));
      await backspaces(got === null ? 2 : Math.max(0, Math.min(2, got)));
      await sleep(rand(80, 200));
      return false;
    }
    return false;
  };

  // What the typo gate needs about the field keys go to: the caller's description, else the focused
  // element read in the isolated world.
  const typingField = async (field: TypoField | null | undefined): Promise<TypoField | null> => {
    if (field !== null && field !== undefined) return field;
    try { return (await worldFor(page).evaluate<TypoField | null>(ISO_ACTIVE_FIELD)) ?? null; } catch { return null; }
  };

  // Type text key-by-key with human timing. Each char goes through the native keyboard (trusted),
  // so this stays isTrusted===true.
  //
  // Capitals and shifted symbols are typed the way a person types them: ShiftLeft goes down a moment
  // before the key (35-120 ms) and comes up just after it (10-70 ms), held across a run of them.
  // Playwright's press("A") alone sent the key with shiftKey=false and no Shift key at all (measured
  // on r28 with "Ab!") -- input no keyboard produces.
  //
  // Fast pairs ROLL OVER: the next key goes down before the previous one comes up. One press() per
  // character never overlaps (0 of 32 keydowns, measured on r32/r33), a keystroke-dynamics tell;
  // real typists overlap many fast pairs. The share is the persona's rolloverRate, stable per seed.
  //
  // Fat-finger typos are planned by forms.planTypo and never happen where a slip is a failure rather
  // than a slip (passwords, e-mail, numbers, phone numbers, URLs, one-time codes).
  const rollRate = rolloverRate(persona);
  const humanTypeText = async (text: string, field?: TypoField | null, typos = true): Promise<void> => {
    const chars = Array.from(text);
    const n = chars.length;
    let shift = false;
    const rate = typos ? typoRateFor(page, () => typoRate(persona)) : 0;
    const allow = rate > 0 && typosAllowed(await typingField(field));
    try {
      for (let i = 0; i < n; i++) {
        let ch = chars[i];
        const need = needsShift(ch);
        if (need && !shift && nativeKbDown && nativeKbUp) {
          await nativeKbDown("Shift");
          shift = true;
          await sleep(rand(35, 120));
        }
        // occasional fat-finger on unshifted letters and digits, then notice + correct
        let typo = false;
        let consumed = false;
        if (allow && !need && !shift && typoEligible(ch)) {
          const tp = planTypo(chars, i, Math.random, rate);
          if (tp !== null) {
            typo = true;
            try { consumed = await doTypo(chars, i, tp); } catch { /* typo path best-effort */ }
          }
        }
        // Rollover of ch and the next char: unshifted single-key pairs only, never a key with itself,
        // never after a typo. keyboard.down inserts the character exactly as press does, so the typed
        // value is unchanged. The overlap leaves ch down alone for at least 8 ms first.
        const nx = chars[i + 1];
        let rolled = false;
        if (nativeKbDown && nativeKbUp && !shift && i + 1 < n && !typo && ROLL_KEYS.has(ch)
            && ROLL_KEYS.has(nx) && ch !== nx && Math.random() < rollRate) {
          const d1 = keyDwell(persona), d2 = keyDwell(persona);
          if (d1 - 8 >= 12) {
            const overlap = rand(12, Math.min(55, d1 - 8));
            try {
              await nativeKbDown(ch);
              await sleep(d1 - overlap);
              await nativeKbDown(nx);
              await sleep(overlap);
              await nativeKbUp(ch);
              await sleep(Math.max(5, d2 - overlap));
              await nativeKbUp(nx);
            } catch {
              // never strand a key down for the caller's next keystroke
              try { await nativeKbUp(ch); } catch { /* best-effort */ }
              try { await nativeKbUp(nx); } catch { /* best-effort */ }
              break;
            }
            rolled = true;
            i += 1; ch = nx; // nx is typed; the cadence below runs as if after it
          }
        }
        if (!rolled && !consumed) { try { await emitKey(ch); } catch { break; } }
        if (shift && (i === n - 1 || !needsShift(chars[i + 1]))) {
          await sleep(rand(10, 70));
          await nativeKbUp!("Shift");
          shift = false;
        }
        if (i < n - 1) {
          if (Math.random() < 0.06 * tempo) await sleep(rand(180, 450) * tempo); // brief thinking pause
          else await sleep(flightMs() * tempo);
          if (/\s/.test(ch)) await sleep(rand(20, 100) * tempo); // slight extra pause at word boundaries
        }
      }
    } finally {
      // never leave Shift held for the caller's next keystroke
      if (shift) { try { await nativeKbUp!("Shift"); } catch { /* best-effort */ } }
    }
  };

  // Keystrokes from a session in which the mouse never existed. Selector-based typing glides to the
  // field first (focusClick), but page.keyboard.type/press names no target, and on a page focused by
  // autofocus or by Tab it can be the first input of any kind — a keydown stream with zero preceding
  // pointer events is one of the cheapest checks a collector can run.
  //
  // AMBIENT ONLY. Deliberately NOT a move toward the focused field: the caller has already chosen
  // where the text goes, and approaching that element would hover it (and, on a control that reacts
  // to hover, move it) for no behavioral gain. Once per page — after that the cursor has a history,
  // and warming up before every keystroke would itself be the anomaly.
  let kbWarmed = false;
  const keyboardWarmup = async (): Promise<void> => {
    if (kbWarmed || st.placed) return;
    kbWarmed = true;
    await ambient(rand(280, 620));
  };

  keyboard.type = (text: string) => locked(async () => { await keyboardWarmup(); await humanTypeText(text); });

  // keyboard.press with the persona's hold, unless the caller specified their own.
  //
  // WHY THIS EXISTS. keyboard.type was humanised from the start and keyboard.press was not, which
  // left the most common single-key call in any script — Enter, Tab, Escape, arrow keys — emitting
  // a keydown and keyUp in the same instant. Measured with humanize ON, before this wrapper:
  // keyboard.type held keys 58–107ms while keyboard.press held them 1.4–3.8ms. A finger cannot
  // press and release a key in under a millisecond, and a detector reading the SHORTEST dwell in a
  // session sees the press path — so one un-humanised call undid the whole keyboard persona.
  //
  // The caller's own `delay` wins: press(key, {delay}) is the documented way to hold a key for a
  // specific time, and silently overriding it would break that. Only the DEFAULT changes -- and
  // `delay: 0` IS Playwright's default ("no hold"), so it, like any non-positive value, gets the
  // persona's hold too (a 0 ms key hold is the tell this wrapper exists to remove).
  keyboard.press = (key: string, options: any = {}) => locked(async () => {
    await keyboardWarmup();
    const opts = { ...(options && typeof options === "object" ? options : {}) };
    if (!(typeof opts.delay === "number" && opts.delay > 0)) opts.delay = keyDwell(persona);
    return nativeKbPress(key, opts);
  });

  // ------------------------------------------------------- page-level targeted helpers
  // Points tried when resolving a control through the browser's hit test: a floating label or an icon
  // often sits over a field's CENTRE, so the edges get a chance too.
  const probePoints: [number, number][] = [[0.5, 0.5], [0.82, 0.5], [0.18, 0.5], [0.5, 0.8], [0.5, 0.2]];

  // The Fitts target width of a glide onto `box`: its smaller side (floor 6 px). Every targeted glide passed
  // 24 whatever the box, so duration did not follow the target's size.
  const aimW = (box: Rect): number => Math.max(6, Math.min(box.width, box.height));

  // When the persona's point is covered (a badge, a floating label over the centre): the first probe point
  // (edges, then bottom/top) the isolated hit test proves reaches the element, as a whole pixel inside the
  // box; null when none does (the caller falls back).
  const provenClickPoint = async (box: Rect, sel: string | null = null): Promise<Point | null> => {
    for (const [fx, fy] of probePoints.slice(1)) {
      // spread around the probe (3 % of the width, 8 % of the height): a fixed probe pixel would land every
      // covered click on the same spot
      const x = roundHalfEven(clamp(box.x + box.width * fx + gauss() * Math.max(1.5, box.width * 0.03), box.x + 1, box.x + box.width - 1));
      const y = roundHalfEven(clamp(box.y + box.height * fy + gauss() * Math.max(1.0, box.height * 0.08), box.y + 1, box.y + box.height - 1));
      if ((await hitInBox(box, x, y, sel)) === true) return { x, y };
    }
    return null;
  };

  // The control filling `box` (Playwright's resolved box for a locator), read in the isolated world
  // through the browser's own hit test: works for every Playwright selector (getByRole, getByLabel,
  // `>> nth=`, .filter()) where document.querySelector threw and dropped the call to stock input. The
  // element found must fill the SAME box, or it is not the one the locator means; null when no probe
  // point proves that. `probe` records the point that proved it.
  const describe = async (box: Rect | null | undefined, want: string | null = ISO_FORM_CONTROL):
    Promise<DescribedElement | null> => {
    if (!box || box.width <= 0 || box.height <= 0) return null;
    const w = worldFor(page);
    const scroll = await w.scroll(); // once: the hit test wants document coordinates
    for (const [fx, fy] of probePoints) {
      const nid = await w.nodeAt(box.x + box.width * fx, box.y + box.height * fy, scroll);
      if (nid === undefined) continue;
      const info = await w.callOnNode<DescribedElement | null>(nid, ISO_DESCRIBE, { want });
      if (!info || !info.rect) continue;
      const [rx, ry, rw, rh] = info.rect;
      if (Math.abs(rx - box.x) <= 1.5 && Math.abs(ry - box.y) <= 1.5
          && Math.abs(rw - box.width) <= 1.5 && Math.abs(rh - box.height) <= 1.5) {
        info.node = nid;
        // The point that PROVED the control: when the centre is covered (a floating label), this is
        // where a click actually reaches it.
        info.probe = [fx, fy];
        return info;
      }
    }
    return null;
  };

  // A click point the hit test proved reaches the control, when its centre did not; null when the
  // centre itself was fine (then the persona's own point is used, as for any click).
  const provenPoint = (box: Rect, info: DescribedElement | null): [number, number] | null => {
    if (!info || !info.probe || (info.probe[0] === 0.5 && info.probe[1] === 0.5)) return null;
    const [fx, fy] = info.probe;
    const x = box.x + box.width * fx + gauss() * 1.5;
    const y = box.y + box.height * fy + gauss() * 1.0;
    return [clamp(x, box.x + 1, box.x + box.width - 1), clamp(y, box.y + 1, box.y + box.height - 1)];
  };

  // Describe the SAME element again, from the node the first hit test found: no new Playwright handle
  // (see boxOf), so nothing the page can see. null if it is gone.
  const redescribe = async (info: DescribedElement, want: string | null = ISO_FORM_CONTROL): Promise<DescribedElement | null> => {
    if (info.node === undefined) return null;
    const again = await worldFor(page).callOnNode<DescribedElement | null>(info.node, ISO_DESCRIBE, { want });
    if (!again) return null;
    again.node = info.node;
    again.probe = info.probe ?? [0.5, 0.5];
    return again;
  };

  const inView = async (box: Rect): Promise<boolean> => {
    const vp = await viewport();
    return box.x >= 0 && box.y >= 0 && box.x + box.width <= vp.width && box.y + box.height <= vp.height;
  };

  const rectBox = (r: any): Rect | null => {
    if (!r || !Array.isArray(r.rect)) return null;
    const [x, y, width, height] = r.rect as number[];
    return width > 0 && height > 0 ? { x, y, width, height } : null;
  };

  // Does a press at viewport (x, y) reach the element filling `box`? true / false, or null when it
  // cannot be told here (CDP is unavailable). A point inside a child frame is answered by the <iframe>
  // element that holds it (IsolatedWorld.nodeAt).
  // `sel`: a plain-CSS selector for the element, when the caller has one: whether the hit is THAT
  // element (ISO_HIT_IN_BOX), not just one that fills the box.
  const hitInBox = async (box: Rect, x: number, y: number, sel: string | null = null): Promise<boolean | null> => {
    const w = worldFor(page);
    const nid = await w.nodeAt(x, y);
    if (nid === undefined) return null;
    const b: Record<string, unknown> = { x: box.x, y: box.y, width: box.width, height: box.height };
    if (sel) b.sel = sel;
    const res = await w.callOnNode<boolean | null>(nid, ISO_HIT_IN_BOX, b);
    return res === undefined || res === null ? null : Boolean(res);
  };

  // Does the element filling `box` own its centre -- not clipped by a scrolling container, not
  // covered? The browser's own hit test, read in the isolated world.
  const hitOk = async (box: Rect, sel: string | null = null): Promise<boolean> =>
    (await hitInBox(box, box.x + box.width / 2, box.y + box.height / 2, sel)) === true;

  // The box of a plain-CSS selector's first match, read (and scrolled into view) in the isolated
  // world: not even the one boundingBox() handle, and its textContent preview, is made. Used only
  // when Playwright counts exactly the elements the browser's own querySelectorAll finds --
  // Playwright's CSS also pierces open shadow roots, so equal counts mean the same elements in the same
  // order. null otherwise (getByRole, text=, `>>` chains, :has-text()...: then Playwright's boundingBox).
  const cssBox = async (selector: string | null): Promise<Rect | null> => {
    if (typeof selector !== "string" || !selector) return null;
    const w = worldFor(page);
    const r = await w.evaluate<any>(ISO_CSS_BOX, { sel: selector, scroll: false });
    if (!r || !r.n) return null;
    try {
      if ((await page.locator(selector).count()) !== r.n) return null;
    } catch {
      return null;
    }
    let box = rectBox(r);
    if (box && !((await inView(box)) && (await hitOk(box, selector)))) {
      // scrollIntoViewIfNeeded also scrolls a scrolling container that clips it, and does nothing
      // when it already shows (then the cover is an overlay: the click's own check)
      box = rectBox(await w.evaluate<any>(ISO_CSS_BOX, { sel: selector, scroll: true }));
    }
    return box;
  };

  // Scroll the WINDOW so `box` sits centred where it was off-screen, the way Playwright's scroll
  // centres it, and return where the box is now (moved by exactly the scrolled distance) -- or null.
  // Needs no handle: the box is already known.
  const scrollWindowTo = async (box: Rect): Promise<Rect | null> => {
    const vp = await viewport();
    if (box.width > vp.width || box.height > vp.height) return null;
    const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
    const dx = box.x < 0 || box.x + box.width > vp.width ? cx - vp.width / 2 : 0;
    const dy = box.y < 0 || box.y + box.height > vp.height ? cy - vp.height / 2 : 0;
    const moved = await worldFor(page).evaluate<unknown>(ISO_SCROLL_BY, [dx, dy]);
    if (!Array.isArray(moved) || moved.length !== 2) return null;
    return { x: box.x - Number(moved[0]), y: box.y - Number(moved[1]), width: box.width, height: box.height };
  };

  // Wait the way Playwright would, with the CALLER's timeout (undefined = the page default; a
  // hard-coded 30 s used to override setDefaultTimeout both ways), and return the box, in view. With
  // `force` Playwright skips actionability, so nothing is waited for: a hidden target goes native at
  // once instead of timing out where the native call would have acted.
  //
  // Each Playwright call that resolves the element to a handle (waitFor, scrollIntoViewIfNeeded,
  // boundingBox) makes a MAIN-world handle whose preview reads the element's textContent where the
  // page can see it: verifier B counted 4 such reads per humanized click and 7 per textarea fill. So
  // each is made only when needed: the wait only for an element not visible yet (isVisible runs in
  // Playwright's utility world), the scroll only for a box not already inside the viewport, and a
  // plain-CSS `selector` needs no handle at all (cssBox). What remains, for other selectors, is the
  // one boundingBox.
  const boxOf = async (loc: Locator, timeout: number | undefined, force = false, selector: string | null = null):
    Promise<Rect | null> => {
    if (!(await loc.isVisible())) {
      if (force) throw new NativeSignal("forced on a hidden target");
      await loc.waitFor({ state: "visible", timeout });
    }
    const css = await cssBox(selector);
    if (css) return css;
    let box = await loc.boundingBox();
    if (box && !((await inView(box)) && (await hitOk(box, selector)))) {
      // Off-screen: scroll the window to it and prove it shows there (hit test), with no new handle.
      // Clipped by a scrolling container, covered, or the window scroll did not bring it out:
      // Playwright's scroll, which also scrolls containers, and a fresh box.
      const moved = !(await inView(box)) ? await scrollWindowTo(box) : null;
      if (moved && (await inView(moved)) && (await hitOk(moved, selector))) return moved;
      await loc.scrollIntoViewIfNeeded({ timeout });
      box = await loc.boundingBox();
    }
    return box;
  };

  // Does `loc` hold the keyboard focus? Asked through Playwright's utility-world selector engine
  // (`:focus`), so it works for every element and every locator and the page sees nothing. The
  // isolated hit test only knew form controls, so a focused BUTTON read as unfocused and got clicked
  // before a press -- two activations.
  const focused = async (loc: Locator): Promise<boolean> => {
    try { return (await loc.and(page.locator(":focus")).count()) > 0; } catch { return false; }
  };
  const isFocused = (selector: string): Promise<boolean> => focused(page.locator(selector).first());

  // {box, info} when `loc` is a visible text-entry control a person would click into before typing,
  // else null. Anything else -- a button, a link, a hidden helper textarea, a slider -- is left to
  // Playwright, which focuses it without a click (a click on a button activates it; on a slider it
  // moves it).
  const textEntry = async (loc: Locator, timeout: number | undefined, selector: string | null = null):
    Promise<{ box: Rect; info: DescribedElement } | null> => {
    if (!(await loc.isVisible())) return null;
    const box = await boxOf(loc, timeout, false, selector);
    const info = await describe(box);
    if (box && info && classifyField(info.tag, info.type, info.ce) === "text") return { box, info };
    return null;
  };

  // Click into the control filling `box` (resolved, visible and in view). `at`: the point the hit test
  // proved when the centre is covered (a floating label); else the persona's point. Stability is
  // checked in the isolated world -- the control must still fill the same box 50 ms later (an
  // animation in flight goes native) -- not by a second boundingBox(), which the page could see (see
  // boxOf).
  //
  // A control no probe point reaches -- a label or an icon covers all of it -- is clicked anyway, where
  // a person clicks (a floating label passes the focus on), and the caller checks that the caret
  // arrived. Its stability then comes from a fresh box: the isolated CSS read, else boundingBox().
  // Requiring the hit test there sent a fully labelled field to Playwright's insertText (found by the
  // .NET port).
  const focusClick = async (box: Rect | null, at: [number, number] | null = null, loc: Locator | null = null,
    selector: string | null = null): Promise<boolean> => {
    if (!box) return false;
    await sleep(50);
    const info = await describe(box);
    if (!info) {
      let again: Rect | null = selector ? await cssBox(selector) : null;
      if (!again && loc) again = await loc.boundingBox();
      if (!again || Math.abs(again.x - box.x) > 1 || Math.abs(again.y - box.y) > 1) return false;
    } else if (info.disabled) {
      return false;
    }
    const p0 = at ? { x: at[0], y: at[1] } : clickPoint(box, st, persona); // 2D gaussian toward the centre
    const pt = { x: roundHalfEven(p0.x), y: roundHalfEven(p0.y) };      // the pixel the hit test answered for
    await glide(pt.x, pt.y, { targetW: aimW(box) });
    await pressHere(pt.x, pt.y);
    return true;
  };

  // Ctrl+A (Cmd+A on a Mac engine) the way a hand does it: the modifier goes down a moment before the
  // A, the A is held, the modifier comes up just after. Sent as one Playwright chord the modifier and
  // the A went down within ~1 ms of each other. ControlOrMeta lets Playwright pick the chord the
  // ENGINE's editing honours: a Meta+A sent to a Linux engine for a macOS persona selects nothing.
  const selectAll = async (): Promise<void> => {
    if (!nativeKbDown || !nativeKbUp) { await key("ControlOrMeta+a"); return; }
    await nativeKbDown("ControlOrMeta");
    try {
      await sleep(rand(40, 110));
      await nativeKbPress("a", { delay: keyDwell(persona) });
      await sleep(rand(20, 80));
    } finally {
      await nativeKbUp("ControlOrMeta");
    }
  };

  // Select all, then Backspace -- each key HELD like a finger holds it. The old clear sent Control+a
  // and Backspace with ~0 ms holds, which is exactly what a key-dwell check flags. If the field is
  // still not empty (`read` -- the field's value -- says so), it is emptied from the end, one
  // Backspace per character.
  const clearFocused = async (read: () => Promise<string | null> = activeValue): Promise<void> => {
    await selectAll();
    await sleep(rand(60, 160));
    await key("Backspace");
    await sleep(rand(60, 160));
    const left = await read();
    if (left) {
      const len = Array.from(left).length;
      if (len > 64) throw new Error("field would not clear");
      await key("End");
      await sleep(rand(80, 180));
      await key("Backspace", len, [55, 120]);
      await sleep(rand(60, 160));
      if (await read()) throw new Error("field would not clear");
    }
  };

  const nativePageType = (page as any).type ? (page as any).type.bind(page) : null;
  const nativePageFill = page.fill.bind(page);
  const nativePagePress = page.press.bind(page);
  const nativePageDblclick = page.dblclick.bind(page);
  const nativePageClick = page.click.bind(page);
  const nativePageHover = page.hover.bind(page);
  const nativePageSelect = typeof (page as any).selectOption === "function" ? (page as any).selectOption.bind(page) : null;
  const nativeType = (selector: string, text: string, options: any) =>
    nativePageType ? nativePageType(selector, text, options) : nativePageFill(selector, text, options);

  // Make `selector` the focus the way a person does (a click), unless it already is. Only text-entry
  // controls are clicked; for anything else, and when the click did not actually put the caret there
  // (an overlay took it), the caller goes native.
  const clickInto = async (selector: string, timeout: number | undefined): Promise<void> => {
    const loc = page.locator(selector).first();
    if (await focused(loc)) return;
    const te = await textEntry(loc, timeout, selector);
    if (!te) throw new NativeSignal("not a visible text field");
    if (!(await focusClick(te.box, provenPoint(te.box, te.info), loc, selector)) || !(await focused(loc))) {
      throw new NativeSignal("the click did not put the caret in the field");
    }
    await sleep(rand(40, 120));
  };

  const htype = async (selector: string, text: string, options: any): Promise<any> => {
    let why = "the humane route cannot apply";
    try {
      await clickInto(selector, options?.timeout);
      await humanTypeText(text);
      return;
    } catch (e) {
      if (isTimeout(e)) throw e;
      why = whyOf(e);
    }
    reportFallback(page, "page.type", why, selector);
    // outside the try: a native call that raises must raise ONCE, not be retried by the handler
    return nativeType(selector, text, options);
  };
  (page as any).type = apiErrors("page.type", (selector: string, text: string, options: any = {}) =>
    locked(() => htype(selector, text, options)));

  // Type `value` into a text control and PROVE it landed. The value is read through Playwright's
  // utility world (`inputValue`): it sees the control the locator means, wherever focus is, and the page
  // cannot observe it. A field whose value cannot be read, or a click that did not put the caret in it
  // (an overlay took it), is left to Playwright -- "could not check" is never treated as success.
  const fillText = async (loc: Locator, box: Rect | null, value: string, info: DescribedElement | null,
    selector: string | null = null): Promise<boolean> => {
    const isCe = Boolean(info && info.ce)
      || (info === null && ((await loc.getAttribute("contenteditable")) || "false") !== "false");
    // What the field can hold: a maxlength keeps the first N characters (what Playwright's insertText
    // leaves too), and a person types only those -- the field stops taking keys.
    let want = value;
    let limit: [number | string | null, string | null, string | null] = [null, null, null];
    if (!isCe) {
      limit = info
        ? [info.maxlength, info.tag, info.type]
        : [await loc.getAttribute("maxlength"),
          (await loc.locator("xpath=self::textarea").count()) ? "textarea" : "input", (await loc.getAttribute("type")) || ""];
      want = fitMaxlength(value, ...limit);
    }
    // Bulk values (over 200 characters) are still inserted at once -- typing thousands of keys would
    // crawl -- but the way a person pastes: glide to the field, click into it, then insert.
    const bulk = Array.from(want).length > 200; // what the field holds: a 300-character value cut to 5 is typed
    if (bulk && !nativeKbInsert) return false;
    if (!(await focusClick(box, box ? provenPoint(box, info) : null, loc, selector))) return false;
    await sleep(rand(40, 120));
    if (!(await focused(loc))) return false;
    const read = async (): Promise<string | null> => {
      try { return await loc.inputValue({ timeout: COVER_RETRY_MS }); } catch { return null; }
    };
    const put = async (text: string, typos = true): Promise<void> => {
      if (bulk) {
        await sleep(rand(120, 320)); // the paste shortcut, after the click
        await nativeKbInsert!(text);
      } else {
        await humanTypeText(text, info, typos);
      }
    };
    if (isCe) {
      const cur = info ? info.value : null;
      if (cur) await clearFocused();
      await put(want);
      return true; // contenteditable: Playwright's fill sets text the same way; no value to read
    }
    const cur = await read();
    if (cur === null) return false;
    if (cur) await clearFocused(read); // a person does not select-all an empty field
    await put(want);
    let got = await read();
    if (got === null) return false;
    // exact; a mask that only re-punctuated what was typed (a phone format); or one that re-punctuated it
    // and then stopped taking keys (MM/YY on maxlength 5): no wipe and retype (maskKept)
    if (got === want || formattedEqual(got, want) || maskKept(got, want, ...limit)) return true;
    if (bulk) return false; // an inserted value that did not land: Playwright's fill, not a second paste
    // Wrong value (an autocompleter interfered, a key was lost): one clean retry without slips, then
    // give the field to Playwright -- a correct value beats pretty input.
    await clearFocused(read);
    await put(want, false);
    got = await read();
    return got !== null && (got === want || formattedEqual(got, want) || maskKept(got, want, ...limit));
  };

  // A range input: drag the thumb (or click the track) to the value, then arrow-nudge to it exactly.
  // Typing digits into a slider -- what fill() did before -- changes nothing, and the click before it
  // left the thumb wherever it landed, with no error.
  const fillRange = async (loc: Locator, box: Rect, info0: DescribedElement | null, value: string): Promise<boolean> => {
    const info = info0 ?? {
      min: await loc.getAttribute("min"), max: await loc.getAttribute("max"),
      step: await loc.getAttribute("step"), value: await loc.inputValue(), dir: "ltr", wm: "",
    };
    // Off the step, out of range, not a number: Playwright raises "Malformed value" for these, so does
    // this route (by leaving them to it) -- never a silently snapped or clamped value.
    if (!rangeValueOk(value, info.min, info.max, info.step)) return false;
    if ((info.wm || "").startsWith("vertical")) return false;
    const rtl = info.dir === "rtl";
    const [lo, hi, step] = rangeAttrs(info.min, info.max, info.step);
    const target = rangeSanitize(value, info.min, info.max, info.step);
    const now = async (): Promise<number | null> => {
      const f = pyFloat(await loc.inputValue());
      return f === null || Number.isNaN(f) ? null : f;
    };
    const cur = await now();
    if (cur !== null && numericEqual(cur, target)) return true;
    let [tx, ty] = rangePoint(box, rangeFraction(target, lo, hi), rtl);
    ty += gauss() * box.height * 0.08;
    if (cur !== null && Math.random() < 0.65) {
      // Grab the thumb where it is and slide it: what most hands do with a slider.
      let [cx, cy] = rangePoint(box, rangeFraction(cur, lo, hi), rtl);
      cy += gauss() * box.height * 0.08;
      await glide(cx, cy, { targetW: 16 });
      await prePress();
      const { grabMs, releaseMs } = dragDwell(persona);
      await nativeDown();
      st.held = true;
      try {
        await sleep(grabMs);
        await glide(tx, ty, { settle: true });
        await sleep(releaseMs);
      } finally {
        await nativeUp();
        st.held = false;
      }
    } else {
      await glide(tx, ty, { targetW: 16 });
      await pressHere(tx, ty);
    }
    for (let round = 0; round < 3; round++) {
      const v = await now();
      if (v === null) return false;
      if (numericEqual(v, target)) return true;
      let [k, n] = rangeNudge(v, target, step);
      if (k === null || n <= 0 || n > 40) break;
      if (rtl) k = k === "ArrowRight" ? "ArrowLeft" : "ArrowRight";
      await sleep(rand(150, 350));
      await key(k, n);
    }
    const v = await now();
    return v !== null && numericEqual(v, target);
  };

  // A native date / time / month / week / datetime-local editor: click into its first field and type
  // each field in the order the editor itself shows them (read from its UA shadow tree over CDP --
  // the order follows the browser, not the persona, so it is never assumed).
  const fillDatetime = async (loc: Locator, box: Rect, info: DescribedElement | null, value: string): Promise<boolean> => {
    if (!info || !info.node) return false;
    const kind = info.type;
    const w = worldFor(page);
    const tree = await w.send("DOM.describeNode", { backendNodeId: info.node, depth: -1, pierce: true });
    if (!tree || !tree.node) return false;
    const segs = segmentNodesFromTree(tree.node);
    const plan = planDatetime(kind, segs.map(([s]) => s), value, info.value);
    if (!plan || plan.length === 0 || segs.length === 0) return false;
    let px: number | null = null, py = 0, fw = 12.0;
    const bm = segs[0][1] ? await w.send("DOM.getBoxModel", { backendNodeId: segs[0][1] }) : undefined;
    if (bm && bm.model) {
      const c: number[] = bm.model.content;
      const xs = [c[0], c[2], c[4], c[6]], ys = [c[1], c[3], c[5], c[7]];
      const x0 = Math.min(...xs), y0 = Math.min(...ys), x1 = Math.max(...xs), y1 = Math.max(...ys);
      // trust the field box only when it lies inside the input's own box
      if (box.x - 1 <= x0 && x0 < x1 && x1 <= box.x + box.width + 1
          && box.y - 1 <= y0 && y0 < y1 && y1 <= box.y + box.height + 1) {
        px = rand(x0 + (x1 - x0) * 0.25, x0 + (x1 - x0) * 0.75);
        py = (y0 + y1) / 2 + gauss() * Math.max(0.5, (y1 - y0) * 0.1);
        fw = Math.max(6.0, x1 - x0);
      }
    }
    if (px === null) {
      px = box.x + Math.min(14.0, box.width * 0.12);
      py = box.y + box.height / 2;
    }
    await glide(px, py, { targetW: fw });
    await pressHere(px, py);
    await sleep(pauseMs(persona, 150, 350));
    for (const a of plan) {
      if (a[0] === "type") {
        // The same typing model as text (persona dwell, gaussian gaps, thinking pauses, rollover): the
        // old flat 60-150 ms gap had a CV of 0.24 and no rollover. A segment moves on by itself after
        // its last digit and keys go down in order, so a rolled-over digit still lands in the right
        // field (checked on r38, all five types). No typos: a slip in a date is a wrong date.
        await humanTypeText(a[1], null, false);
      } else {
        await key(a[1], a.length > 2 ? (a as [string, string, number])[2] : 1, [70, 170]);
      }
      await sleep(pauseMs(persona, 60, 220)); // between fields: the eye checks what it typed
    }
    return datetimeEqual(kind, await loc.inputValue(), value);
  };

  // Classify through Playwright's utility-world queries when the hit test could not prove the element
  // (it is covered, or inside a frame): attributes only, nothing the page can see.
  const classifyFallback = async (loc: Locator): Promise<FieldClass> => {
    for (const tag of ["textarea", "select"]) {
      if (await loc.locator(`xpath=self::${tag}`).count()) return classifyField(tag);
    }
    if (await loc.locator("xpath=self::input").count()) return classifyField("input", await loc.getAttribute("type"));
    const ce = await loc.getAttribute("contenteditable");
    return classifyField("div", null, ce !== null && ce.toLowerCase() !== "false");
  };

  // fill() classifies the field: text-like is typed (cleared first only when non-empty, then
  // verified), a range is dragged/clicked and nudged, a date-like editor is typed segment by segment.
  // color, unfillable types and anything the humane route cannot prove it did right go to Playwright.
  const hfill = async (selector: string, value: string, options: any): Promise<any> => {
    let why = "the value is not a string";
    try {
      if (typeof value === "string") {
        const timeout = options?.timeout; // undefined: the page's default, as Playwright would use
        const loc = page.locator(selector).first();
        const box = await boxOf(loc, timeout, Boolean(options?.force), selector);
        const info = await describe(box);
        const kind = info ? classifyField(info.tag, info.type, info.ce) : await classifyFallback(loc);
        why = info && (info.disabled || info.readonly) ? "the field is disabled or read-only"
          : ["text", "range", "datetime"].includes(kind) ? `the ${kind} route did not verify` : `no humane route for a ${kind} field`;
        if (!(info && (info.disabled || info.readonly))) {
          if (kind === "text" && await fillText(loc, box, value, info, selector)) return;
          if (kind === "range" && box && await fillRange(loc, box, info, value)) return;
          if (kind === "datetime" && box && await fillDatetime(loc, box, info, value)) return;
        }
      }
    } catch (e) {
      if (isTimeout(e)) throw e;
      why = whyOf(e);
    }
    // Native: color (Playwright sets it -- the OS dialog is no route), unfillable types (Playwright
    // raises, as before humanize), and anything the humane route could not prove it did right.
    reportFallback(page, "page.fill", why, selector);
    return nativePageFill(selector, value, options);
  };
  (page as any).fill = apiErrors("page.fill", (selector: string, value: string, options: any = {}) =>
    locked(() => hfill(selector, value, options)));

  (page as any).dblclick = apiErrors("page.dblclick", (selector: string, options: any = {}) => locked(() => {
    const o = { ...(options ?? {}) };
    if (o.clickCount === undefined) o.clickCount = 2;
    return targetedClick(nativePageDblclick, selector, o, false, options ?? {});
  }));

  const hpress = async (selector: string, key: string, options: any): Promise<any> => {
    let why = "the humane route cannot apply";
    try {
      // Click into text fields only: a press on a button or a link that is not focused yet is
      // Playwright's (it focuses without activating); clicking it first activated it twice.
      await clickInto(selector, options?.timeout);
      // The persona's hold unless the caller set a real one, for the same reason keyboard.press above
      // carries one: this is the path locator.press() delegates to.
      const d = options?.delay;
      await nativeKbPress(key, { delay: typeof d === "number" && d > 0 ? d : keyDwell(persona) });
      return;
    } catch (e) {
      if (isTimeout(e)) throw e;
      why = whyOf(e);
    }
    reportFallback(page, "page.press", why, selector);
    return nativePagePress(selector, key, options);
  };
  (page as any).press = apiErrors("page.press", (selector: string, key: string, options: any = {}) =>
    locked(() => hpress(selector, key, options)));

  // ------------------------------------------------------------------------- selectOption
  // Open the drop-down with a click, move its highlight with the planned keys, commit with Enter: one
  // trusted input+change, the sequence a mouse-and-keyboard user produces. While the popup is open its
  // keys never reach the page (measured on r37: value unchanged until Enter). The opening click is the
  // full humanized click -- including the covered-point check -- at the point the hit test proved when
  // the centre is covered (`at`, a Playwright `position`), so a floating label over the select's centre
  // cannot swallow it and leave the letters to change a closed select one per key.
  const openAndPick = async (selector: string, plan: SelectPlan, at: { x: number; y: number } | null): Promise<void> => {
    // moveOff false: the popup is open now and this sequence owns the pointer until the pick lands.
    await targetedClick(nativePageClick, selector, at ? { position: at } : {}, false, null, undefined, false);
    // Every pause here is the persona's (timingTraits.pauseScale): the windows were one fixed set for
    // every identity.
    await sleep(pauseMs(persona, 220, 480)); // the list opens; the eye finds the option
    const tempo = timingTraits(persona).pauseScale;
    for (const ch of plan.keys) {
      await nativeKbPress(ch, { delay: keyDwell(persona) });
      await sleep(Math.max(40, (100 + gauss() * 35) * tempo)); // well inside type-ahead's 1 s window
    }
    const n = plan.arrows;
    if (n) {
      await sleep(pauseMs(persona, 120, 300));
      await arrowRun(n > 0 ? "ArrowDown" : "ArrowUp", Math.abs(n));
    }
    await sleep(pauseMs(persona, 150, 400));
    await key("Enter");
    await sleep(pauseMs(persona, 80, 160));
  };

  // `n` presses of an arrow key in an OPEN drop-down. A long run is HELD, the way a person crosses a
  // long list: one press, the keyboard's repeat delay, then auto-repeat at its rate -- Playwright sends
  // a key that is already down again as an autoRepeat keydown. Pressed one by one at a human ~180 ms
  // each, 370 rows took 68 s (verifier B). The open popup takes these keys, so the page sees none of
  // them, only how long the pick took.
  const arrowRun = async (k: string, n: number): Promise<void> => {
    if (n < 8 || !nativeKbDown || !nativeKbUp) { await key(k, n); return; }
    await nativeKbDown(k);
    try {
      await sleep(rand(380, 560)); // the repeat delay
      const gap = rand(28, 36);    // ~30 repeats a second
      for (let j = 0; j < n - 1; j++) {
        await sleep(gap);
        await nativeKbDown(k);
      }
    } finally {
      await sleep(rand(20, 60));
      await nativeKbUp(k); // never leave the key down
    }
  };

  // A list box's scroll state and one row's box, read from the node the hit test found.
  type ListboxState = {
    box: [number, number, number, number]; row: [number, number, number, number];
    top: number; bottom: number; st: number; max: number; sel: boolean[];
  };
  const listboxRow = async (info: DescribedElement, i: number): Promise<ListboxState | null> => {
    if (info.node === undefined) return null;
    return (await worldFor(page).callOnNode<ListboxState | null>(info.node, ISO_LISTBOX_ROW, { i })) ?? null;
  };

  // The list box's state once its scroll has come to rest. A wheel notch scrolls the box SMOOTHLY over
  // a few frames, and a read taken mid-animation shows the row where it was, not where it is going:
  // the old loop then wheeled the wrong way and oscillated (verifier B: scrollTop 120 -> 240 -> 323 ->
  // 203 -> 83 -> 0, twice, with the row in view at 120, then a native, untrusted fallback). Two equal
  // reads 50 ms apart = at rest.
  const settledRow = async (info: DescribedElement, i: number): Promise<ListboxState | null> => {
    let prev = await listboxRow(info, i);
    for (let k = 0; k < 30; k++) {
      await sleep(50);
      const cur = await listboxRow(info, i);
      if (!cur || !prev) return cur;
      if (Math.abs(cur.st - prev.st) < 0.5 && Math.abs(cur.row[1] - prev.row[1]) < 0.5) return cur;
      prev = cur;
    }
    return prev;
  };

  // The visible vertical span [top, bottom] of the row, or null when too little of it shows. A row cut
  // off by the box edge is still a target a person clicks when most of it shows.
  const rowSpan = (s: ListboxState): [number, number] | null => {
    const [, y, w, h] = s.row;
    if (w <= 0 || h <= 0) return null;
    const top = Math.max(y, s.top), bottom = Math.min(y + h, s.bottom);
    return bottom - top >= Math.min(h, Math.max(6, h * 0.6)) ? [top, bottom] : null;
  };

  // Wheel the list box until row `i` shows: the pointer goes over the box once, then as many notches
  // as the distance needs -- one at first, to learn how far a notch scrolls this box -- each burst read
  // only once the scroll has settled. {top, bottom, s}, or null when it cannot be brought into view
  // (the wheel does not move the box, or it is at its end).
  const scrollRowIntoView = async (info: DescribedElement, i: number):
    Promise<{ top: number; bottom: number; s: ListboxState } | null> => {
    let s = await settledRow(info, i);
    if (!s) return null;
    let span = rowSpan(s), per: number | null = null, over = false, notches = 0;
    while (span === null && notches < 60) {
      const [, y, , h] = s.row;
      const room = s.bottom - s.top;
      const margin = rand(2, Math.max(3, Math.min(12, (room - h) * 0.4)));
      const need = y + h > s.bottom ? (y + h) - s.bottom + margin : y - s.top - margin;
      if ((need > 0 && s.st >= s.max - 0.5) || (need < 0 && s.st <= 0.5)) return null;
      if (!over) {
        const [bx, , bw] = s.box;
        await glide(bx + bw * rand(0.3, 0.7), s.top + room * rand(0.3, 0.7), { targetW: Math.max(20, room) });
        await sleep(rand(80, 200));
        over = true;
      }
      const n: number = !per ? 1
        : Math.max(1, Math.min(8, Math.floor(Math.abs(need) / per) + (Math.abs(need) % per > per * 0.25 ? 1 : 0)));
      const notch = await wheelNotchPx();
      for (let j = 0; j < n; j++) {
        await nativeWheel(0, need > 0 ? notch : -notch);
        notches++;
        if (j < n - 1) await sleep(rand(30, 70));
      }
      const before = s.st;
      const next = await settledRow(info, i);
      if (!next) return null;
      s = next;
      const moved = Math.abs(s.st - before);
      if (moved < 0.5) return null; // the wheel did not scroll the box
      per = moved / n;
      span = rowSpan(s);
    }
    return span ? { top: span[0], bottom: span[1], s } : null;
  };

  // A list box (multiple or size > 1): click the rows -- the first plainly (which also drops any other
  // selection), the rest with Ctrl/Cmd held, scrolling the box with the wheel to bring a row into view
  // the way a person does.
  const selectListbox = async (info: DescribedElement, idx: number[]): Promise<string[] | null> => {
    const want = [...idx].sort((a, b) => a - b);
    const opts = info.options ?? [];
    const selectedNow = opts.map((o, i) => (o.selected ? i : -1)).filter((i) => i >= 0);
    if (want.length === selectedNow.length && want.every((i, k) => i === selectedNow[k])) {
      return want.map((i) => opts[i].value);
    }
    for (let k = 0; k < want.length; k++) {
      const got = await scrollRowIntoView(info, want[k]);
      if (!got) return null;
      const { top, bottom, s } = got;
      const r = s.row;
      const px = rand(r[0] + r[2] * 0.12, r[0] + Math.min(r[2] * 0.6, 140));
      let py = (top + bottom) / 2 + gauss() * Math.max(0.5, (bottom - top) * 0.12);
      py = Math.min(Math.max(py, top + 1), bottom - 1);
      await glide(px, py, { targetW: bottom - top });
      if (k && nativeKbDown && nativeKbUp) {
        // ControlOrMeta: Playwright sends the add-to-selection key the ENGINE honours
        await pressAt(normalizeClickOptions({ modifiers: ["ControlOrMeta"] }));
      } else {
        await pressHere(px, py);
      }
      await sleep(rand(150, 350));
    }
    const final = await listboxRow(info, want[0]);
    if (!final) return null;
    const got = final.sel.map((v, i) => (v ? i : -1)).filter((i) => i >= 0);
    return got.length === want.length && got.every((i, k) => i === want[k]) ? want.map((i) => opts[i].value) : null;
  };

  // Pick options the way a person does (see openAndPick / selectListbox). Resolves to the selected
  // values like Playwright, or null when the humane route cannot apply or cannot be shown to have
  // worked -- the caller then runs stock selectOption.
  const selectHumanized = async (selector: string, values: any, options: any): Promise<string[] | null> => {
    const wants = selectWants(values);
    if (!wants) return null;
    const loc = page.locator(selector).first();
    const box = await boxOf(loc, options?.timeout, Boolean(options?.force), selector);
    const info = await describe(box, "select");
    if (!box || !info || info.tag !== "select" || info.disabled) return null;
    const opts = info.options ?? [];
    const idx: number[] = [];
    for (const w of wants) {
      const i = wantIndex(opts, w);
      if (i < 0 || opts[i].disabled) return null; // not there (yet) or disabled: Playwright waits / reports it
      if (!idx.includes(i)) idx.push(i);
    }
    if (info.multiple || (info.size ?? 0) > 1) {
      return selectListbox(info, info.multiple ? idx : [Math.min(...idx)]);
    }
    const t = Math.min(...idx); // Playwright: the FIRST option (document order) matching any wanted one
    if (info.selectedIndex === t) return [opts[t].value];
    const labels = opts.map((o) => (o.disabled ? null : (o.label || "")));
    const plan = planSelect(labels, info.selectedIndex ?? -1, t);
    if (plan === null) return null;
    const pt = provenPoint(box, info);
    const at = pt ? { x: pt[0] - box.x - (info.bl || 0), y: pt[1] - box.y - (info.bt || 0) } : null;
    await openAndPick(selector, plan, at);
    if ((await loc.inputValue()) === opts[t].value) return [opts[t].value];
    // One correction from wherever the highlight landed (arrows only), then give up to native.
    const again = await redescribe(info, "select");
    if (!again) return null;
    const fix = planSelect(labels, again.selectedIndex ?? -1, t, 0);
    if (fix === null) return null;
    await openAndPick(selector, fix, at);
    return (await loc.inputValue()) === opts[t].value ? [opts[t].value] : null;
  };

  // page.selectOption, humanized: no more script-assigned value with an untrusted change event
  // (which fails 'interaction-select-change-trust' on clearcotelabs.com/audit).
  if (nativePageSelect) {
    const hselect = async (selector: string, values: any, options: any): Promise<any> => {
      let why: string;
      try {
        const res = await selectHumanized(selector, values, options ?? {});
        if (res !== null) return res;
        why = selectWhy(values, options ?? {});
      } catch (e) {
        if (isTimeout(e)) throw e;
        why = whyOf(e);
      }
      reportFallback(page, "page.selectOption", why, selector);
      return nativePageSelect(selector, values, options);
    };
    (page as any).selectOption = apiErrors("page.selectOption", (selector: string, values: any, options: any = {}) =>
      locked(() => hselect(selector, values, options)));
  }

  // ------------------------------------------------------------------ check / uncheck / setChecked
  // A humanized click when the state must change, then the state verified. `trial` performs no action:
  // the state is read (a non-checkbox raises, as in Playwright) and, when a click would be needed, its
  // checks run here (trial) -- not Playwright's trial, which moves the pointer and presses. Errors propagate as Playwright's own (not a checkbox, timed out): the
  // click owns its native fallback, so nothing here may click a second time -- and when one click did
  // not change the state (a page that cancels it), this raises like Playwright's check instead of
  // clicking again.
  const setChecked = async (orig: NativeFn, selector: string, checked: boolean, options: any): Promise<any> => {
    const opts = options ?? {};
    const loc = page.locator(selector).first();
    if (opts.trial) {
      if ((await loc.isChecked({ timeout: opts.timeout })) === checked) return;
      return trial(selector, opts, false);
    }
    if ((await loc.isChecked({ timeout: opts.timeout })) === checked) return;
    // The click lands on the input's box, or on its <label> (labelAim): always for a hidden custom checkbox
    // (the box click waited for it to show and timed out), 40 % of the time otherwise.
    let clicked = false;
    if (!opts.force && !opts.position) {
      try {
        const lbox = await labelAim(selector, loc, opts.timeout);
        clicked = !!lbox && await clickLabel(lbox);
      } catch (e) {
        if (isTimeout(e)) throw e;
        clicked = false;
      }
    }
    if (!clicked) await targetedClick(nativePageClick, selector, { ...opts }, false, null);
    if ((await loc.isChecked()) === checked) return;
    throw sdkPlaywrightError("Clicking the checkbox did not change its state");
  };
  type LabelRead = { n?: number; hidden?: boolean; label?: { rect: number[]; text: number[] | null } | null } | null;
  // Where a person clicks a checkbox / radio when not on its own box: its <label> (see Python _label_aim).
  const labelAim = async (selector: string, loc: Locator, timeout: number | undefined): Promise<Rect | null> => {
    const w = worldFor(page);
    if (await loc.isVisible()) {
      const box = await boxOf(loc, timeout, false, selector);
      const info = box ? await describe(box) : null;
      if (!info || info.node === undefined || info.tag !== "input" || !["checkbox", "radio"].includes(info.type)) return null;
      const lab = await w.callOnNode<LabelRead>(info.node, ISO_LABEL_OF);
      if (!lab || !lab.label || lab.hidden) return null;
      if (!labelDraw(persona)) return null; // the persona's share; THIS click's choice is unseeded
      const r = lab.label.text ?? lab.label.rect;
      return r[2] >= 2 && r[3] >= 2 ? { x: r[0], y: r[1], width: r[2], height: r[3] } : null;
    }
    if (!selector) return null;
    const lab = await w.evaluate<LabelRead>(ISO_LABEL_BY_SEL, { sel: selector });
    if (!lab || !lab.n || !lab.hidden || !lab.label) return null;
    try { if ((await page.locator(selector).count()) !== lab.n) return null; } catch { return null; }
    const r = lab.label.rect;
    let lbox: Rect | null = { x: r[0], y: r[1], width: r[2], height: r[3] };
    if (lbox.width < 2 || lbox.height < 2) return null;
    if (!(await inView(lbox))) lbox = (await scrollWindowTo(lbox)) ?? lbox;
    return (await inView(lbox)) ? lbox : null;
  };
  // One humanized click inside a label's box where the hit test proves the label owns the point.
  const clickLabel = async (lbox: Rect): Promise<boolean> => {
    const p = clickPoint(lbox, st, persona);
    const x = roundHalfEven(p.x), y = roundHalfEven(p.y);
    await sleep(50);
    if ((await hitInBox(lbox, x, y)) === false) return false;
    await glide(x, y);
    await pressAt(normalizeClickOptions({}));
    return true;
  };
  for (const [name, want] of [["check", true], ["uncheck", false]] as const) {
    if (typeof (page as any)[name] === "function") {
      const orig = (page as any)[name].bind(page);
      (page as any)[name] = apiErrors("page." + name, (selector: string, options: any = {}) =>
        locked(() => setChecked(orig, selector, want, options)));
    }
  }
  if (typeof (page as any).setChecked === "function") {
    const origSc = (page as any).setChecked.bind(page);
    (page as any).setChecked = apiErrors("page.setChecked", (selector: string, checked: boolean, options: any = {}) =>
      locked(() => setChecked((s: string, o: any) => origSc(s, checked, o), selector, Boolean(checked), options)));
  }

  // ------------------------------------------------------------------------- click / hover
  // The covered-point check, through the browser's own hit test read in the isolated world: nothing is
  // dispatched and the pointer does not move. It replaced a Playwright TRIAL action, which moves the
  // REAL pointer onto the element and back and performs a press with its events blocked: verifier B
  // saw, before every humanized click since 0.46.0, a mousemove inside the target, a 450-500 px jump
  // back, then the glide, and a pointerdown/pointerup/click (left, even for a right click) that window
  // capture listeners could see. A covered point is looked at again for COVER_CHECK_MS (an overlay
  // fading out). A point inside a child frame is answered by the <iframe> element that holds it
  // (IsolatedWorld.nodeAt), so no Playwright trial runs for it either. false when covered; null when
  // the hit test cannot run (no CDP): the caller goes native.
  const reachesPoint = async (box: Rect, x: number, y: number, sel: string | null = null): Promise<boolean | null> => {
    const deadline = Date.now() + COVER_CHECK_MS;
    let hit = await hitInBox(box, x, y, sel);
    while (hit === false && Date.now() < deadline) {
      await sleep(80);
      hit = await hitInBox(box, x, y, sel);
    }
    return hit;
  };

  // The viewport point of Playwright's `position` (relative to the PADDING box: the border widths are added).
  const positionPoint = async (box: Rect, pos: { x: number; y: number }): Promise<{ x: number; y: number }> => {
    const info = await describe(box, null);
    const bl = info ? info.bl || 0 : 0, bt = info ? info.bt || 0 : 0;
    return { x: box.x + bl + pos.x, y: box.y + bt + pos.y };
  };

  // `trial: true`, answered by the SDK: Playwright's actionability checks -- visible, enabled (not for
  // hover), stable, and the click point receives the press (the browser's own hit test, read in the
  // isolated world) -- retried until the caller's timeout (or the page default), then a Playwright
  // TimeoutError. NO pointer move and NO press: Playwright's own trial moves the real pointer onto the
  // target and dispatches pointerdown/pointerup/click that page listeners registered before its blocker
  // see (SC 209: 6/6). The point checked is Playwright's: `position`, else the centre. Scrolling the
  // element into view is part of the checks, as in Playwright. With `force` only the element must exist.
  const trial = async (selector: string, options: any, noClick: boolean): Promise<void> => {
    const opts = options ?? {};
    const o = normalizeClickOptions(opts);
    const ms = typeof opts.timeout === "number" ? opts.timeout : defaultTimeoutMs(page);
    const end = ms > 0 ? Date.now() + ms : null;
    const left = () => (end === null ? 0 : Math.max(1, Math.round(end - Date.now())));
    const loc = page.locator(selector).first();
    if (o.force) { await loc.waitFor({ state: "attached", timeout: left() }); return; }
    for (;;) {
      let why = "element is not visible";
      const box = await boxOf(loc, left(), false, selector); // waits for visible; throws Playwright's timeout
      if (box) {
        if (!noClick && !(await loc.isEnabled())) {
          why = "element is not enabled";
        } else {
          const { x, y } = o.position ? await positionPoint(box, o.position)
            : { x: box.x + box.width / 2, y: box.y + box.height / 2 };
          await sleep(50); // stable: 50 ms on, the element still fills `box` (ISO_HIT_IN_BOX)
          if ((await hitInBox(box, x, y, selector)) !== false) return; // null: the hit test cannot run -- nothing to add
          why = `element is not stable, or another element would receive the pointer at (${Math.round(x)}, ${Math.round(y)})`;
        }
      }
      if (end !== null && Date.now() >= end) throw trialTimeoutError(selector, ms, why);
      await sleep(end === null ? 100 : Math.max(0, Math.min(100, end - Date.now())));
    }
  };

  // The humanized form of page.click / dblclick / hover (and the click inside check/uncheck and the
  // drop-down opener).
  //
  // Every Playwright option is honoured: `button`, `clickCount`, `delay` (the hold) and `modifiers`
  // shape the press; `position` replaces the persona's point; `force` skips the covered-point check
  // (and, as in Playwright, every wait); `trial` performs NO action: its checks run here (trial), with
  // no pointer move and no press. The caller's `timeout` is passed through unchanged. No Playwright
  // trial runs before a humanized press either (reachesPoint).
  const targetedClick = async (orig: NativeFn | null, selector: string, options: any, noClick: boolean,
    nativeOpts: any | null, dwell: unknown = undefined, moveOff = true): Promise<any> => {
    const opts = options ?? {};
    const o = normalizeClickOptions(opts);
    let aim: Point | null = null; // where Playwright's own click lands, once the box is known (native)
    const native = async () => {
      if (!orig) throw new Error("humanized click could not run");
      try {
        return await orig(selector, nativeOpts ?? opts);
      } finally {
        // Native click/hover genuinely move the browser pointer to the element. Leaving `placed`
        // false afterwards made the next wheel anchor believe the pointer was still unplaced -- and the
        // engine path walked again from its own stale point (engineInSync; verifier A #6). The next
        // SDK glide starts where Playwright pressed: `position`, else the centre of the box.
        st.placed = true;
        eng.at = null;
        if (aim) { st.x = aim.x; st.y = aim.y; }
      }
    };
    if (o.trial) return trial(selector, opts, noClick);
    let why = "the humane route cannot apply";
    try {
      const timeout = opts.timeout; // undefined: the page's default, as Playwright would use
      const loc = page.locator(selector).first();
      // Playwright skips actionability with force: no wait then -- a target it would click hidden goes
      // native at once instead of timing out first.
      const box = await boxOf(loc, timeout, o.force, selector);
      if (!box) throw new NativeSignal("the element has no box");
      aim = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
      if (!o.force && !(await loc.isEnabled())) throw new NativeSignal("the element is not enabled");
      let x: number, y: number;
      if (o.position) {
        ({ x, y } = await positionPoint(box, o.position));
        aim = { x, y };
      } else {
        ({ x, y } = clickPoint(box, st, persona));
      }
      // Whole pixels: the hit test below answers for the pixel DOM.getNodeForLocation rounds to, so the
      // press goes to that pixel too. A press at 971.6 next to a badge ending at 972 was proven at 972 and
      // landed on the badge, reported as a success (verifier B, SC 210).
      x = roundHalfEven(x); y = roundHalfEven(y);
      if (!o.force && orig) {
        // Stability, then covered-by: 50 ms on, the element must still fill `box` (an animation in
        // flight does not) and own the click point -- no trusted press at a point some overlay owns.
        // Either failing goes native: Playwright waits for it.
        await sleep(50);
        const hit = await reachesPoint(box, x, y, selector);
        if (hit === false && !o.position) {
          // The persona's point is covered (a badge, an icon, a floating label over the centre). Before
          // giving the click to Playwright -- which waited out its timeout on 5-7 of 16 badge-covered
          // buttons (EVAL B) -- aim at the first probe point the hit test proves reaches the element.
          const alt = await provenClickPoint(box, selector);
          if (!alt) throw new NativeSignal("the click point does not reach the element (covered, moving or clipped)");
          ({ x, y } = alt);
          aim = { x, y };
        } else if (!hit) throw new NativeSignal(hit === null ? "the hit test cannot run here"
          : "the click point does not reach the element (covered, moving or clipped)");
      }
      if (o.modifiers.length && !(nativeKbDown && nativeKbUp)) throw new NativeSignal("modifiers need keyboard.down");
      if (!noClick && !o.force && orig && engineModeFor(page, engineClicksMode) === "all" && enginePathOk(o) && await engineGlide(x, y, false)) {
        // Opt-in (CLEARCOTE_HUMANIZE_ENGINE_CLICKS=all): the engine walks the approach and clicks the
        // point the hit test above proved; plain left single clicks only, and only while the engine
        // knows where the pointer is (engineInSync). Otherwise: the planner.
        return;
      }
      if (noClick && o.modifiers.length) {
        // hover with modifiers: Playwright holds them across the move
        const held: string[] = [];
        try {
          for (const m of o.modifiers) {
            await nativeKbDown!(m);
            held.push(m);
            await sleep(rand(40, 110));
          }
          await glide(x, y, { targetW: aimW(box) });
          await dwellOn(x, y, box, dwell);
        } finally {
          for (const m of held.reverse()) {
            await sleep(rand(20, 80));
            try { await nativeKbUp!(m); } catch { /* one failed keyup must not strand the others */ }
          }
        }
        return;
      }
      await glide(x, y, { targetW: aimW(box) });
      if (noClick) {
        await dwellOn(x, y, box, dwell); // the hand rests on what it hovered (SC 232 lane C)
      } else {
        await pressAt(o);
        if (moveOff) scheduleMoveOff();
      }
      return;
    } catch (e) {
      if (isTimeout(e)) throw e;
      why = whyOf(e);
    }
    reportFallback(page, noClick ? "page.hover" : "page.click", why, selector);
    // outside the try, so a native call that raises is not run a second time by the handler
    return native();
  };

  (page as any).click = apiErrors("page.click", (selector: string, options: any = {}) =>
    locked(() => targetedClick(nativePageClick, selector, options, false, null)));
  (page as any).hover = apiErrors("page.hover", (selector: string, options: any = {}) => {
    // `dwell` (ms; 0: none) is the SDK's, not Playwright's: never forwarded to a native hover
    const { dwell, ...rest } = options ?? {};
    return locked(() => targetedClick(nativePageHover, selector, rest, true, null, dwell));
  });
  // Locator.dragTo({ trial: true }) answers its checks here too (see the Locator patch).
  (page as any)._clearcoteTrial = (selector: string, options: any, noClick: boolean) =>
    locked(() => trial(selector, options, noClick));

  // pressAndHold (hold.ts): the hooks it reads off the page. It holds the page lock for the whole hold
  // (one action at a time); inside it uses page.mouse (not locked) and these hooks. _clearcoteNudge is
  // the tremor step: a native move (no glide) that keeps the tracked position where the pointer is.
  (page as any)._clearcoteBoxOf = (loc: Locator, timeout: number | undefined, selector: string | null = null) =>
    boxOf(loc, timeout, false, selector);
  (page as any)._clearcotePointer = (): Point => ({ x: st.x, y: st.y });
  // ... and for its press point: a glide with the box's own Fitts width, the covered-point check (the same
  // isolated hit test as a click, retried COVER_CHECK_MS), the probe-proven point when the chosen one is
  // covered, and the border widths that make `position` padding-box relative like click's.
  (page as any)._clearcoteAimGlide = (x: number, y: number, box: Rect) => glide(x, y, { targetW: aimW(box) });
  (page as any)._clearcoteReachesPoint = reachesPoint;
  (page as any)._clearcoteProvenPoint = provenClickPoint;
  (page as any)._clearcoteBorderOf = async (box: Rect): Promise<[number, number]> => {
    const info = await describe(box, null);
    return info ? [info.bl || 0, info.bt || 0] : [0, 0];
  };
  // the frame route (frames.ts) clears a field with the same held chord as the page routes
  (page as any)._clearcoteSelectAll = () => selectAll();
  (page as any)._clearcoteNudge = async (x: number, y: number): Promise<void> => {
    await nativeMove(x, y);
    st.x = x; st.y = y; st.placed = true;
  };
  (page as any)._clearcoteHold = (loc: Locator, o?: PressAndHoldOptions) => locked(() => pressAndHold(loc, o));
  (page as any).pressAndHold = apiErrors("page.pressAndHold", (selector: string, o?: PressAndHoldOptions) =>
    (page as any)._clearcoteHold(page.locator(selector).first(), o));


  // ------------------------------------------------------------ after-action (SC 232 lane C)
  // What the hand does once an action has landed (a port of the Python humanizer's): it rests on a hovered
  // element (dwellOn), drifts off a clicked one (scheduleMoveOff / runMoveOff), and between actions it
  // reads and idles (attention.ts). All LAZY: run at the start of the next action inside the page lock
  // (afterRun), or by page.settle(), never from a timer. The seed fixes the persona's traits (motion.afterTraits,
  // a salted stream: every existing draw keeps its order); every per-action draw is unseeded.
  // (Function declarations: hoisted, so locked()/targetedClick above can call them.)
  // Hidden switch for the attention model until lane A's `ambient: "idle"` / `attention` option lands.
  (page as any)._clearcoteAttention = false;
  // Test seam: what the after-action model did, newest last (bounded).
  (page as any)._clearcoteAfterTrace = af.trace;
  const wheelInner = mouse.wheel;   // the humanized wheel, before the after-action wrapper below

  // Wrap the UNLOCKED public entry points: mouse.move / wheel run the step first; down / up only mark
  // activity (a press lands where the pointer is). The locked ones run it inside locked() (afterRun).
  const unlocked = (fn: (...a: any[]) => Promise<any>) => async (...a: any[]) => {
    if (af.depth > 0) return fn(...a);
    await preAction(true);
    af.depth++;
    try { return await fn(...a); } finally { af.depth--; af.lastEnd = Date.now(); }
  };
  const touch = (fn: (...a: any[]) => Promise<any>) => async (...a: any[]) => {
    try { return await fn(...a); } finally { af.lastEnd = Date.now(); }
  };
  mouse.move = unlocked(mouse.move);
  mouse.wheel = unlocked(mouse.wheel);
  mouse.down = touch(mouse.down);
  mouse.up = touch(mouse.up);
  // Tell locked() what each public call is (see nextCall): set synchronously, read by the locked() the call
  // reaches before its first await.
  const tag = (obj: any, name: string, info: (a: any[]) => { coords: boolean; after: boolean }) => {
    const fn = obj[name];
    if (typeof fn !== "function") return;
    obj[name] = (...a: any[]) => { nextCall = info(a); try { return fn(...a); } finally { nextCall = null; } };
  };
  tag(mouse, "click", () => ({ coords: true, after: true }));
  tag(mouse, "dblclick", () => ({ coords: true, after: true }));
  for (const [name, at] of [["click", 1], ["hover", 1], ["dblclick", 1], ["check", 1], ["uncheck", 1], ["setChecked", 2]] as const) {
    tag(page as any, name, (a) => ({ coords: false, after: !a[at]?.trial }));
  }
  tag(page as any, "_clearcoteTrial", () => ({ coords: false, after: false }));
  const ambientInner = (page as any).ambientMotion;
  (page as any).ambientMotion = (ms = 1200) => locked(() => ambientInner(ms));
  (page as any).settle = (): Promise<boolean> => { nextCall = { coords: false, after: false }; return locked(settleNow); };

  async function afterRun<T>(fn: () => Promise<T>, o: { coords: boolean; after: boolean }): Promise<T> {
    if (!o.after || af.depth > 0) return fn();
    await preAction(o.coords);
    af.depth++;
    try { return await fn(); } finally { af.depth--; af.lastEnd = Date.now(); }
  }

  function attentionOn(): boolean {
    return (page as any)._clearcoteAttention === true || ambientMode(page) === "idle";
  }

  async function dwellOn(x: number, y: number, box: Box | null, dwell: unknown): Promise<void> {
    let ms: number | null = null;
    if (dwell !== undefined && dwell !== null) {
      const v = Number(dwell);
      if (Number.isFinite(v)) {
        if (v <= 0) return;
        ms = v;
      }
    }
    if (ms === null) ms = dwellMs(persona, af.rng);
    const steps = planDwell({ x, y }, box, persona, ms, af.rng);
    af.trace.push(["dwell", ms, steps.length]);
    trimTrace();
    await dispatch(steps);
    if (steps.length) { st.x = steps[steps.length - 1].x; st.y = steps[steps.length - 1].y; }
  }

  function scheduleMoveOff(): void {
    const hit = moveOffRoll(persona, af.rng, af.moHits, af.moN);
    af.moN++;
    af.mo = null;
    if (hit) {
      af.moHits++;
      af.mo = { due: Date.now() + moveOffDelayMs(persona, af.rng), x: st.x, y: st.y };
    }
  }

  async function runMoveOff(wait: boolean): Promise<boolean> {
    const mo = af.mo;
    af.mo = null;
    if (!mo || st.held || st.x !== mo.x || st.y !== mo.y) return false;
    const left = mo.due - Date.now();
    if (left > 0) {
      if (!wait) return false;
      await sleep(left);
    }
    const vp = (await viewBounds()) ?? (await viewport());
    const to = planMoveOff({ x: st.x, y: st.y }, vp, persona, af.rng);
    af.trace.push(["move_off", [st.x, st.y], [to.x, to.y]]);
    trimTrace();
    await glide(to.x, to.y, { targetW: 140 }); // one coarse glide: nothing to acquire
    return true;
  }

  async function idleDrift(ms: number): Promise<void> {
    af.trace.push(["drift", ms]);
    trimTrace();
    const vp = (await viewBounds()) ?? (await viewport());
    const steps = trimTo(planAmbient({ x: st.x, y: st.y }, vp, persona, ms, af.rng), ms);
    await dispatch(steps);
    if (steps.length) { st.x = steps[steps.length - 1].x; st.y = steps[steps.length - 1].y; }
  }

  async function attend(coords: boolean, now: number): Promise<void> {
    // `now`: when the action started -- a move-off glide just made does not count as idle time
    const la = af.loadAt;
    if (la !== null) {
      af.loadAt = null;
      let n: unknown = 0;
      try { n = await worldFor(page).evaluate(ISO_TEXT_LENGTH); } catch { n = 0; }
      const pause = readPauseMs(n, persona, af.rng);
      const notches = exploreNotches(af.rng);
      af.trace.push(["read", pause, n, coords ? 0 : notches]);
      trimTrace();
      const left = pause - (now - la);
      if (left > 350) await idleDrift(Math.min(left * 0.5, IDLE_DRIFT_MAX_MS)); // the eyes read; the hand drifts once
      const rest = la + pause - Date.now();
      if (rest > 0) await sleep(rest);
      // A coordinate action names a point in the current layout: never scroll that away.
      if (notches && !coords) await wheelInner(0, notches * (await wheelNotchPx()));
      return;
    }
    if (af.lastEnd !== null) {
      for (const ms of idleDrifts((now - af.lastEnd) / 1000, af.rng)) await idleDrift(ms);
    }
  }

  async function preAction(coords: boolean): Promise<void> {
    if (af.depth > 0 || st.held) return;
    af.depth++;
    try {
      const now = Date.now();
      await runMoveOff(false);
      if (attentionOn()) await attend(coords, now);
    } catch { /* the after-action model never stops the action */ } finally { af.depth--; }
  }

  async function settleNow(): Promise<boolean> {
    if (af.depth > 0) return false;
    af.depth++;
    let done = false;
    try { done = await runMoveOff(true); } catch { done = false; } finally { af.depth--; }
    if (done) af.lastEnd = Date.now();
    return done;
  }

  function trimTrace(): void {
    if (af.trace.length > 256) af.trace.splice(0, af.trace.length - 256);
  }

  // Outermost, after the after-action wrappers: a per-call `humanize: false` goes straight to the stock
  // call and skips the after-action step too.
  wirePerCallOff(page, st, stockPage, stockMouse, stockKb);

  // Marker the Locator-class patch keys off: humanize is active for THIS page.
  (page as any)._clearcoteHumanized = true;
  patchLocatorClass(page.locator("html"));
}

// ------------------------------------------------------------------- selectOption wants
/** One wanted option. `value` matches value OR label (a bare string, like Playwright's
 * valueOrLabel); `valueOnly` is a `{ value }` option object, which Playwright matches on the value
 * alone; `index` / `label` are `{ index }` / `{ label }`. */
type SelectWant = { kind: "value" | "valueOnly" | "label"; v: string } | { kind: "index"; v: number };

/** @internal exported for tests. Playwright's selectOption values -> the wanted options, or null when
 * the humane route does not apply (nothing asked, an ElementHandle, an option object naming more than
 * one field): the caller then runs stock selectOption. */
export function selectWants(values: any): SelectWant[] | null {
  if (values === null || values === undefined) return null;
  const list: any[] = Array.isArray(values) ? values : [values];
  const out: SelectWant[] = [];
  for (const x of list) {
    if (typeof x === "string") { out.push({ kind: "value", v: x }); continue; }
    if (!x || typeof x !== "object") return null;
    if (typeof x.asElement === "function" || /Handle/.test(x.constructor?.name ?? "")) return null;
    const keys = ["value", "label", "index"].filter((k) => x[k] !== undefined && x[k] !== null);
    if (keys.length !== 1) return null;
    if (keys[0] === "index") {
      if (typeof x.index !== "number" || !Number.isInteger(x.index)) return null;
      out.push({ kind: "index", v: x.index });
    } else if (keys[0] === "label") {
      out.push({ kind: "label", v: String(x.label) });
    } else {
      out.push({ kind: "valueOnly", v: String(x.value) });
    }
  }
  return out.length ? out : null;
}

/** @internal exported for tests. The option index a want resolves to, Playwright's way, or -1. */
export function wantIndex(opts: readonly OptionInfo[], w: SelectWant): number {
  switch (w.kind) {
    case "value": return matchOption(opts, { value: w.v });
    case "label": return matchOption(opts, { label: w.v });
    case "index": return matchOption(opts, { index: w.v });
    case "valueOnly": return opts.findIndex((o) => o.value === w.v);
  }
}

// --------------------------------------------------------------------------- per-call humanize: false
// Every wrapped page / mouse / keyboard / locator method takes { humanize: false } in its options: the stock
// Playwright call, the keyword stripped. Wired outermost, after attach has wrapped everything.
const PAGE_METHODS = ["click", "hover", "dblclick", "type", "fill", "press", "selectOption", "check", "uncheck", "setChecked"];
const MOUSE_METHODS = ["move", "click", "wheel", "dblclick", "down", "up"];
const KB_METHODS = ["type", "press"];

/** @internal exported for tests. `fn`, except when its last argument is an options object saying
 * humanize: false: then `stock`, with the keyword removed (`after` runs once the stock call settles). */
const SDK_ONLY_KEYS = ["dwell"];

export function perCallOff(stock: (...a: any[]) => any, fn: (...a: any[]) => any, after?: (a: any[]) => void) {
  return function (this: any, ...a: any[]): any {
    const last = a[a.length - 1];
    if (last && typeof last === "object" && !Array.isArray(last) && Object.prototype.hasOwnProperty.call(last, "humanize")) {
      const { humanize, ...rest } = last;
      const args = [...a.slice(0, -1), rest];
      if (humanize === false) {
        // the SDK's own keys (hover dwell): Playwright's call takes none of them
        const stockArgs = [...a.slice(0, -1), Object.fromEntries(Object.entries(rest).filter(([k]) => !SDK_ONLY_KEYS.includes(k)))];
        const r = stock.apply(this, stockArgs);
        if (after) return Promise.resolve(r).finally(() => after(args));
        return r;
      }
      return fn.apply(this, args);
    }
    return fn.apply(this, a);
  };
}

function wirePerCallOff(page: any, st: { x: number; y: number; held: boolean; placed: boolean },
  stockPage: Record<string, any>, stockMouse: Record<string, any>, stockKb: Record<string, any>): void {
  const placed = () => { st.placed = true; };
  const moved = (a: any[]) => {
    st.placed = true;
    if (typeof a[0] === "number" && typeof a[1] === "number") { st.x = a[0]; st.y = a[1]; }
  };
  for (const [n, s] of Object.entries(stockPage)) {
    if (typeof page[n] === "function") page[n] = perCallOff(s, page[n], ["click", "hover", "dblclick"].includes(n) ? placed : undefined);
  }
  const mouse = page.mouse;
  for (const [n, s] of Object.entries(stockMouse)) {
    if (typeof mouse[n] !== "function") continue;
    if (n === "down") {
      // the stock press, but the held state stays tracked (a later move is a drag leg)
      mouse[n] = perCallOff(async (...a: any[]) => { st.held = true; try { return await s(...a); } catch (e) { st.held = false; throw e; } }, mouse[n]);
    } else if (n === "up") {
      mouse[n] = perCallOff(async (...a: any[]) => { try { return await s(...a); } finally { st.held = false; } }, mouse[n]);
    } else {
      mouse[n] = perCallOff(s, mouse[n], ["move", "click", "dblclick"].includes(n) ? moved : undefined);
    }
  }
  for (const [n, s] of Object.entries(stockKb)) {
    if (typeof page.keyboard[n] === "function") page.keyboard[n] = perCallOff(s, page.keyboard[n]);
  }
}

/** @internal exported for tests. Why selectOption went to Playwright when the humane route returned nothing. */
export function selectWhy(values: any, options: any): string {
  if (options?.element) return "element handles go to Playwright";
  for (const v of Array.isArray(values) ? values : [values]) {
    if (v && typeof v === "object") {
      if (typeof v.asElement === "function" || /Handle/.test(v.constructor?.name ?? "")) return "element handles go to Playwright";
      const keys = ["value", "label", "index"].filter((k) => v[k] !== undefined && v[k] !== null);
      if (keys.length !== 1) return "an option object with several fields goes to Playwright";
    }
  }
  return "no humane route, or the pick could not be shown to have worked";
}

/** Why a locator on a humanized page is not routed: it enters or lives in a child frame. undefined when
 * the page is not humanized (not a fallback) or the locator is routable. */
function frameWhy(self: any): string | undefined {
  try {
    const pg: any = self.page();
    if (!pg || !pg._clearcoteHumanized) return undefined;
    if (String(self._selector ?? "").includes("internal:control=enter-frame")) return "frame locators go to Playwright";
    if (self._frame !== pg.mainFrame()) return "a locator in a child frame goes to Playwright";
  } catch { return undefined; }
  return undefined;
}

// --------------------------------------------------------------------------- locator patch
let locatorPatched = false;

// The options each page-level humanized method understands. Everything the caller passed that a page
// method accepts travels with it: the old fwd sent ONLY `timeout`, which turned
// click({button: "right"}) into a left click and click({trial: true}) into a real one.
const CLICK_OPTS = ["button", "clickCount", "delay", "force", "modifiers", "noWaitAfter", "position", "timeout", "trial"];
const HOVER_OPTS = ["force", "modifiers", "noWaitAfter", "position", "timeout", "trial", "dwell"];
const DBL_OPTS = ["button", "delay", "force", "modifiers", "noWaitAfter", "position", "timeout", "trial"];
const FILL_OPTS = ["force", "noWaitAfter", "timeout"];
const TYPE_OPTS = ["timeout"];
const PRESS_OPTS = ["delay", "noWaitAfter", "timeout"];
const SELECT_OPTS = ["force", "noWaitAfter", "timeout"];
const CHECK_OPTS = ["force", "noWaitAfter", "position", "timeout", "trial"];

/** @internal exported for tests: the subset of `kw` named in `names` (null/undefined dropped). */
export function pickOptions(kw: any, names: readonly string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (kw && typeof kw === "object") for (const k of names) if (kw[k] !== undefined && kw[k] !== null) out[k] = kw[k];
  return out;
}

/** Patch the Locator prototype once so locator.* interactions route through the humanized page.*
 * methods. Each is a no-op unless the locator's page has humanize active AND the locator targets
 * the main frame; anything else (frame locators, another page) uses the original Playwright method.
 * Locator actions stay STRICT: more than one match goes to the native method, which raises
 * Playwright's own strict-mode error. Once routed, errors propagate as they are (the page method
 * already ran its own native fallback, so nothing is retried). Composes if another library already
 * patched the prototype. */
function patchLocatorClass(sample: Locator): void {
  if (locatorPatched) return;
  locatorPatched = true;
  const proto: any = Object.getPrototypeOf(sample);
  if (!proto) return;

  const oFill = proto.fill, oClick = proto.click, oType = proto.type, oDbl = proto.dblclick,
    oHover = proto.hover, oPress = proto.press, oPressSeq = proto.pressSequentially,
    oClear = proto.clear, oTap = proto.tap, oCheck = proto.check, oUncheck = proto.uncheck,
    oDragTo = proto.dragTo, oSelectOption = proto.selectOption, oSetChecked = proto.setChecked;

  // Main-frame locators only: a FrameLocator's child keeps the MAIN frame as its _frame and enters the
  // iframe through its selector, so the selector is what tells. Routed through page.fill, its
  // prefilled field was never cleared ("old" + "new" = "oldnew"); frame locators stay native.
  const on = (self: any): boolean => {
    const pg: any = self.page();
    if (!pg || !pg._clearcoteHumanized) return false;
    try {
      if (String(self._selector ?? "").includes("internal:control=enter-frame")) return false;
      return self._frame === pg.mainFrame();
    } catch { return false; }
  };
  // Locator actions are STRICT in Playwright: more than one match is an error. The page-level methods
  // they route through take the first match, so check the count here and let the native call raise
  // its own strict-mode error when it is not at most one.
  const strict = async (self: any): Promise<boolean> => {
    try { return (await self.count()) <= 1; } catch { return false; }
  };
  const sel = (self: any): string => self._selector;
  // Once routed, every error propagates: the page-level wrappers own the native fallback, so retrying
  // the original locator method here ran the action a second (or third) time -- a checkbox whose click
  // is cancelled was clicked three times.
  const route = async (self: any, fn: () => Promise<any>, frameRouted = true): Promise<[boolean, any]> => {
    if (on(self) && await strict(self)) return [true, await fn()];
    // A frame locator: the frame route (froute, lane F) runs next and reports its own fallbacks; only an action
    // without a frame route (tap) reports here.
    const why = frameRouted ? null : frameWhy(self);
    if (why) reportFallback(self.page(), "locator", why, sel(self));
    return [false, undefined];
  };
  // A locator inside a child frame (FrameLocator, frame.locator) on a humanized page: the frame route (frames.ts:
  // the frame's own isolated world for the hit test, the page's humanized mouse and keyboard for the input).
  // [false] when it does not apply -- the native method runs, and the route has reported why.
  const froute = async (self: any, action: string, args: any[], kw: any): Promise<[boolean, any]> => {
    if (!(frameHumanized(self.page()) && frameLocatorOf(self)) || !(await strict(self))) return [false, undefined];
    const r = await frameRoute(self, action, args, kw);
    return r === NATIVE ? [false, undefined] : [true, r];
  };
  // Every routed method words its errors as Playwright words the Locator method (asApiError): through
  // page.*, an error read "page.click: ..." or "locator.waitFor: ...".
  const apiMethod = (api: string, fn: (this: any, ...a: any[]) => Promise<any>) =>
    async function (this: any, ...a: any[]): Promise<any> {
      const self = this;
      return withCallerApi(api, async () => {
        try { return await fn.apply(self, a); } catch (e) { throw asApiError(e, api); }
      });
    };
  // Playwright's tap rejects at once in a context without hasTouch ("The page does not support tap");
  // routed as a click it clicked instead (SC 210 error parity). A context it cannot read is routed.
  const noTouch = (self: any): boolean => {
    try { return self.page().context()._options.hasTouch !== true; } catch { return false; }
  };

  proto.fill = apiMethod("locator.fill", async function (this: any, value: string, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().fill(sel(this), value, pickOptions(kw, FILL_OPTS)));
    if (done) return r;
    const [fdone, fr] = await froute(this, 'fill', [value], kw);
    return fdone ? fr : oFill.call(this, value, kw);
  });
  proto.click = apiMethod("locator.click", async function (this: any, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().click(sel(this), pickOptions(kw, CLICK_OPTS)));
    if (done) return r;
    const [fdone, fr] = await froute(this, 'click', [], kw);
    return fdone ? fr : oClick.call(this, kw);
  });
  proto.type = apiMethod("locator.type", async function (this: any, text: string, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().type(sel(this), text, pickOptions(kw, TYPE_OPTS)));
    if (done) return r;
    const [fdone, fr] = await froute(this, 'type', [text], kw);
    return fdone ? fr : oType.call(this, text, kw);
  });
  proto.dblclick = apiMethod("locator.dblclick", async function (this: any, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().dblclick(sel(this), pickOptions(kw, DBL_OPTS)));
    if (done) return r;
    const [fdone, fr] = await froute(this, 'dblclick', [], kw);
    return fdone ? fr : oDbl.call(this, kw);
  });
  proto.hover = apiMethod("locator.hover", async function (this: any, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().hover(sel(this), pickOptions(kw, HOVER_OPTS)));
    if (done) return r;
    const [fdone, fr] = await froute(this, 'hover', [], kw);
    if (fdone) return fr;
    const { dwell: _dwell, ...pw } = kw ?? {}; // `dwell` is the SDK's: Playwright's own hover does not take it
    return oHover.call(this, pw);
  });
  proto.press = apiMethod("locator.press", async function (this: any, key: string, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().press(sel(this), key, pickOptions(kw, PRESS_OPTS)));
    if (done) return r;
    const [fdone, fr] = await froute(this, 'press', [key], kw);
    return fdone ? fr : oPress.call(this, key, kw);
  });
  proto.pressSequentially = apiMethod("locator.pressSequentially", async function (this: any, text: string, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().type(sel(this), text, pickOptions(kw, TYPE_OPTS)));
    if (done) return r;
    const [fdone, fr] = await froute(this, 'pressSequentially', [text], kw);
    return fdone ? fr : oPressSeq.call(this, text, kw);
  });
  proto.clear = apiMethod("locator.clear", async function (this: any, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().fill(sel(this), "", pickOptions(kw, FILL_OPTS)));
    if (done) return r;
    const [fdone, fr] = await froute(this, 'clear', [], kw);
    return fdone ? fr : oClear.call(this, kw);
  });
  proto.tap = apiMethod("locator.tap", async function (this: any, kw: any = {}) {
    if (noTouch(this)) return oTap.call(this, kw);
    // A touch context: no humanized touch sequence yet, so the tap goes out as the humanized MOUSE click. Lane A's
    // report: strict mode throws ClearcoteStrictError and nothing is sent; otherwise onFallback hears it.
    if (on(this)) reportFallback(this.page(), "locator.tap", TAP_NOT_HUMANIZED + ": sent as a humanized mouse click", sel(this));
    const [done, r] = await route(this, () => this.page().click(sel(this), pickOptions(kw, HOVER_OPTS)), false);
    return done ? r : oTap.call(this, kw);
  });
  proto.check = apiMethod("locator.check", async function (this: any, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().check(sel(this), pickOptions(kw, CHECK_OPTS)));
    if (done) return r;
    const [fdone, fr] = await froute(this, 'check', [], kw);
    return fdone ? fr : oCheck.call(this, kw);
  });
  proto.uncheck = apiMethod("locator.uncheck", async function (this: any, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().uncheck(sel(this), pickOptions(kw, CHECK_OPTS)));
    if (done) return r;
    const [fdone, fr] = await froute(this, 'uncheck', [], kw);
    return fdone ? fr : oUncheck.call(this, kw);
  });
  if (typeof oSetChecked === "function") {
    proto.setChecked = apiMethod("locator.setChecked", async function (this: any, checked: boolean, kw: any = {}) {
      const [done, r] = await route(this, () => this.page().setChecked(sel(this), checked, pickOptions(kw, CHECK_OPTS)));
      if (done) return r;
      const [fdone, fr] = await froute(this, 'setChecked', [checked], kw);
      return fdone ? fr : oSetChecked.call(this, checked, kw);
    });
  }
  // Delegate to the humanized page.selectOption, like every other method here: one implementation
  // behind the page method is what stops the two entry points drifting apart.
  if (typeof oSelectOption === "function") {
    proto.selectOption = apiMethod("locator.selectOption", async function (this: any, values: any, kw: any = {}) {
      const [done, r] = await route(this, () => this.page().selectOption(sel(this), values, pickOptions(kw, SELECT_OPTS)));
      if (done) return r;
      const [fdone, fr] = await froute(this, 'selectOption', [values], kw);
      return fdone ? fr : oSelectOption.call(this, values, kw);
    });
  }

  // New, not a Playwright method: press and hold this element (hold.ts), under the page lock when the
  // page is humanized. Works on frame locators and non-humanized pages too: it presses at viewport
  // coordinates through page.mouse.
  proto.pressAndHold = apiMethod("locator.pressAndHold", async function (this: any, kw?: PressAndHoldOptions) {
    const hold = this.page()._clearcoteHold;
    return typeof hold === "function" ? hold(this, kw) : pressAndHold(this, kw);
  });

  proto.dragTo = apiMethod("locator.dragTo", async function (this: any, target: any, kw: any = {}) {
    if (kw?.trial && on(this) && on(target) && await strict(this) && await strict(target)) {
      // Playwright's drag trial moves the pointer onto the source and presses there (SC 209); the SDK
      // runs the checks of both ends instead: visible, stable, receives the pointer.
      const t = this.page()._clearcoteTrial;
      if (typeof t === "function") {
        for (const [loc, pos] of [[this, kw.sourcePosition], [target, kw.targetPosition]] as const) {
          const o: any = pickOptions(kw, ["force", "timeout"]);
          if (pos) o.position = pos;
          await t(sel(loc), o, true);
        }
        return;
      }
    }
    if (!on(this) && !kw?.trial && frameWhy(this)) reportFallback(this.page(), "locator.dragTo", frameWhy(this)!, sel(this));
    if (on(this) && !kw?.trial) {
      let why = "the source or the target has no box";
      try {
        const pg: any = this.page();
        const sb = await this.boundingBox();
        const tb = await target.boundingBox();
        if (sb && tb) {
          const sx = sb.x + sb.width / 2, sy = sb.y + sb.height / 2;
          const tx = tb.x + tb.width / 2, ty = tb.y + tb.height / 2;
          // Human endpoint dynamics (the two worst slider tells): a grab hesitation AFTER pressing and
          // a settle dwell BEFORE releasing, drawn from the page's motor persona.
          const persona: Persona | undefined = pg._clearcotePersona;
          const { grabMs, releaseMs } = persona ? dragDwell(persona) : { grabMs: rand(130, 360), releaseMs: rand(90, 230) };
          await pg.mouse.move(sx, sy);
          await pg.mouse.down(); await sleep(grabMs);   // pre-press pause inside down(); then the grab hesitation
          const heldGlide = pg._clearcoteHeldGlide;     // humanized held-button drag + seating jiggle (settle)
          if (heldGlide) await heldGlide(tx, ty); else await pg.mouse.move(tx, ty);
          await sleep(releaseMs);                       // pre-release settle before letting go
          await pg.mouse.up();
          return;
        }
      } catch (e) { why = "error: " + String((e as any)?.message ?? e); /* fall back */ }
      reportFallback(this.page(), "locator.dragTo", why, sel(this));
    }
    return oDragTo.call(this, target, kw);
  });

  // { humanize: false } on any of these: the stock Playwright method, the keyword stripped.
  const stockOf: Record<string, any> = {
    fill: oFill, click: oClick, type: oType, dblclick: oDbl, hover: oHover, press: oPress,
    pressSequentially: oPressSeq, clear: oClear, tap: oTap, check: oCheck, uncheck: oUncheck, dragTo: oDragTo,
    selectOption: oSelectOption, setChecked: oSetChecked,
  };
  for (const [name, stock] of Object.entries(stockOf)) {
    if (typeof stock === "function" && typeof proto[name] === "function") proto[name] = perCallOff(stock, proto[name]);
  }
}

/** Install humanize/showCursor on a browser: wraps newPage/newContext so every page is covered. */
export function installHumanize(browser: Browser, opts0: HumanizeLaunchOptions): void {
  // true | "strict" | HumanizeOptions: validated now (an unknown option throws), resolved once
  const opts: HumanizeLaunchOptions = { ...opts0, humanize: resolveHumanize(opts0.humanize) ?? false };
  if (!opts.humanize && !opts.showCursor) return;
  const origNewPage = browser.newPage.bind(browser);
  (browser as any).newPage = async (options?: any) => {
    const page = await origNewPage(options);
    await attachHumanize(browser, page, opts);
    // browser.newPage makes its context inside Playwright (not through the wrapped newContext below): hook
    // that implicit context too, so a popup it opens (window.open, target=_blank) is humanized.
    try { installHumanizeOnContext(page.context(), opts, browser); } catch { /* a page-like object */ }
    return page;
  };
  const origNewContext = browser.newContext.bind(browser);
  (browser as any).newContext = async (options?: any) => {
    const ctx = await origNewContext(options);
    installHumanizeOnContext(ctx, opts, browser);
    return ctx;
  };
}

/** Install humanize/showCursor on a single context (used for launchPersistentContext).
 *
 * Attaches whether or not the context has a Browser. A persistent context has none on older
 * playwright-core (context.browser() is null on 1.49.x), and launch() is a persistent context by
 * default: this used to attach only when a Browser was found, so on those versions humanize was
 * silently off. attachHumanize needs only the page; the engine route takes the browser from the
 * page's context when there is one and uses the page's own CDP session when there is not (there the
 * engine answers only to the pre-r34 name, POINTER_PATH_LEGACY: one miss per context, measured on r38). */
export function installHumanizeOnContext(
  context: BrowserContext, opts0: HumanizeLaunchOptions, browser?: Browser | null
): void {
  const opts: HumanizeLaunchOptions = { ...opts0, humanize: resolveHumanize(opts0.humanize) ?? false };
  if (!opts.humanize && !opts.showCursor) return;
  const b = browser ?? context.browser();
  if (opts.showCursor) { context.addInitScript(CURSOR_OVERLAY).catch(() => {}); }
  // One listener per context: a second install (browser.newPage's implicit context, hooked again) adds none.
  if ((context as any)._clearcoteHooked !== true) {
    (context as any)._clearcoteHooked = true;
    context.on("page", (p) => { attachHumanize(b, p, opts).catch(() => {}); });
  }
  for (const p of context.pages()) attachHumanize(b, p, opts).catch(() => {});
}
