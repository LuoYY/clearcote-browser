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
  wheelNotches, platformFromNavigator, WHEEL_NOTCH_PX, type Box, type Persona, type Point, type Step, type WheelPlatform,
} from "./motion.js";
import { keepInView, trimTo, walk } from "./pacing.js";
import { pressAndHold, type PressAndHoldOptions } from "./hold.js";
import {
  BLANK_POINT, FOCUSABLE, SEARCH_SLIP_RATE, SELECTION_STATE, TAB_DISTANCE, WORD_FIX_RATE, flightMs, modifierLagMs,
  modifierLeadMs, planGap, planRepeat, splitChord, submitPauseMs, tabPauseMs, typingTraits, type TabInfo,
} from "./keys.js";
import {
  COVER_CHECK_MS, COVER_RETRY_MS, ISO_ACTIVE_FIELD, ISO_ACTIVE_VALUE, ISO_CSS_BOX, ISO_DESCRIBE, ISO_FORM_CONTROL,
  ISO_FRAMES, ISO_HIT_IN_BOX, ISO_LISTBOX_ROW, ISO_PLATFORM, ISO_SCROLL_BY, ISO_VIEWPORT,
  roundHalfEven, worldFor, type DescribedElement,
} from "./isolated.js";
import {
  DEFAULT_TYPO_RATE, NEARBY_LETTERS, classifyField, datetimeEqual, enginePathOk, fitMaxlength, formattedEqual, maskKept,
  matchOption, normalizeClickOptions, numericEqual, planDatetime, planSelect, planTypo, prePressMs, pyFloat,
  rangeAttrs, rangeFraction,
  rangeNudge, rangePoint, rangeSanitize, rangeValueOk, segmentNodesFromTree, typoEligible, typosAllowed,
  type ClickPlan, type FieldClass, type OptionInfo, type SelectPlan, type TypoField, type TypoPlan,
} from "./forms.js";

export interface HumanizeOptions {
  /** Humanize all input (move/click/drag/scroll/type) as native trusted events. */
  humanize?: boolean;
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

/** @internal exported for tests. Per-character fat-finger rate. CLEARCOTE_HUMANIZE_TYPO_RATE
 * overrides it (0 disables typos; the test suites raise it to exercise the correction paths). Read at
 * call time, like Python's typo_rate(). */
export function typoRate(): number {
  const v = process.env.CLEARCOTE_HUMANIZE_TYPO_RATE;
  if (v === undefined || v === "") return DEFAULT_TYPO_RATE;
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
  return async (...a: A): Promise<R> => {
    try {
      return await fn(...a);
    } catch (e) {
      throw asApiError(e, api);
    }
  };
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
  constructor() { super("clearcote humanize: native route"); this.name = "NativeSignal"; }
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
/** Lane A's ClearcoteStrictError (strict mode): never swallowed by a best-effort path. */
const isStrict = (e: unknown): boolean => !!e && ((e as any).name === "ClearcoteStrictError" || (e as any).constructor?.name === "ClearcoteStrictError");

export function engineClicksMode(): "off" | "coords" | "all" {
  const v = (process.env.CLEARCOTE_HUMANIZE_ENGINE_CLICKS || "").trim().toLowerCase();
  if (["0", "off", "false", "no", "none"].includes(v)) return "off";
  if (["all", "selector", "selectors"].includes(v)) return "all";
  return "coords";
}

export async function attachHumanize(browser: Browser | null, page: Page, opts: HumanizeOptions): Promise<void> {
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
  if (!opts.humanize) return;

  // Per-identity motor persona (cadence, tremor, overshoot, handedness) — stable across the session,
  // seeded from the fingerprint so behavior is consistent within an identity and unlinkable across
  // seeds. Stored on the page so the module-level Locator.dragTo patch can reach it too.
  const persona: Persona = makePersona(opts.seed);
  (page as any)._clearcotePersona = persona;
  // The typing identity (WPM, bigram factors, Tab preference, typo rate, pauses) from a salted side stream
  // of the persona seed (keys.typingTraits): the persona's own draws are unchanged.
  const traits = typingTraits(persona.seed);
  // Keys the CALLER holds through keyboard.down: key -> [when it went down, the hold it is owed].
  const heldKeys = new Map<string, [number, number]>();
  // A keydown->keyup hold: lane B's per-persona key_dwell_window when present, else motion.keyDwell.
  const dwell = (): number => {
    const w = (persona as any).keyDwellWindow;
    if (Array.isArray(w) && w.length === 2) {
      return w[0] + Math.min(1, Math.abs(gaussFrom(Math.random)) * 0.45) * (w[1] - w[0]);
    }
    return keyDwell(persona);
  };
  const tabPref = (): number => (typeof (persona as any).tabPref === "number" ? (persona as any).tabPref : traits.tabPref);
  // CLEARCOTE_HUMANIZE_TYPO_RATE when set, else the persona's slip rate (lane B's field, else 0.5-4 %).
  const personaTypoRate = (): number => {
    const v = process.env.CLEARCOTE_HUMANIZE_TYPO_RATE;
    if (v !== undefined && v !== "") return typoRate();
    const pr = (persona as any).typoRate;
    return typeof pr === "number" ? pr : traits.typoRate;
  };
  // One input action at a time on this page (see createInputLock).
  const locked = createInputLock();

  // Tracked cursor position so each move continues from where the last one ended.
  // `held` tracks whether a mouse button is currently down — see mouse.move below.
  // `placed` records whether the engine's cursor has actually been moved yet. A page starts with the
  // pointer at the default origin, so anything that delivers input WITHOUT a target to move to —
  // a wheel, a mouse.down that opens a drag, a bare keyboard.type — otherwise happens from a cursor
  // that has never been anywhere. The guarantees below key off this flag.
  // `typed`: when the last typed run ended (the submit / Tab pause owed by afterTyping), null after a glide.
  const st = { x: rand(140, 380), y: rand(90, 240), held: false, placed: false, typed: null as number | null };

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
    st.typed = null; // the hand left the keyboard: no submit / Tab pause owed
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
      const dur = Math.min(1.10, Math.max(0.28, 0.30 + dist / 1700)) * (0.85 + 0.30 * Math.random());
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
  const prePress = async (): Promise<void> => { await sleep(prePressMs(persona.seed)); };
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
    if (enginePathOk(o) && engineClicksMode() !== "off" && await engineGlide(x, y, false)) return;
    await glide(x, y);
    try {
      if (o.modifiers.length && !(nativeKbDown && nativeKbUp)) {
        await prePress(); // the hand confirms the target before it presses
        // delay = the mousedown→mouseup HOLD (human ~60–150 ms); without it Playwright releases in ~2 ms.
        await nativeClick(x, y, { delay: o.delay ?? clickHold(persona), button: o.button, clickCount: o.clickCount });
        return;
      }
      await pressAt(o);
    } catch { /* best-effort */ }
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
  (page as any)._clearcoteAutoAmbient = false;
  page.on("load", () => {
    if ((page as any)._clearcoteAutoAmbient) {
      Promise.resolve((page as any).ambientMotion(rand(450, 950))).catch(() => {});
    }
  });
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
    try { await nativeKbPress(ch, { delay: dwell() }); return; } catch { /* below */ }
    // Outside the catch: in strict mode lane A's report throws, and that must reach the caller.
    fallback("keyboard.press", "insertText: a character keyboard.press cannot map");
    try { await nativeKbType(ch); } catch { /* best-effort */ }
  };

  // Press `k` (n times) with the persona's hold on every press and a human gap between repeats.
  const key = async (k: string, n = 1, gap: [number, number] = [60, 150]): Promise<void> => {
    for (let j = 0; j < n; j++) {
      await nativeKbPress(k, { delay: dwell() });
      if (j < n - 1) await sleep(rand(gap[0], gap[1]));
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
        if (i < n) await sleep(rand(70, 170)); // the gap inside a double click
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

  // One keyup->keydown flight of the persona's cadence (keys.flightMs, the planner Python and .NET use).
  const flight = (prev: string | null = null, ch: string | null = null): number => flightMs(traits, prev, ch, Math.random);

  const backspaces = async (n: number): Promise<void> => {
    for (let k = 0; k < n; k++) {
      await nativeKbPress("Backspace", { delay: dwell() });
      if (k < n - 1) await sleep(rand(70, 150));
    }
  };

  // How many characters arrived since `before` (null = cannot tell).
  const landed = (before: string | null, now: string | null): number | null =>
    before === null || now === null ? null : Array.from(now).length - Array.from(before).length;

  // Bring the focused field back to `target` (what it held before a slip) from wherever a word delete
  // left it: Backspace to the common prefix, then type the rest. Nothing when unreadable.
  const restore = async (target: string): Promise<void> => {
    const got = await activeValue();
    if (got === null || got === target) return;
    const g = Array.from(got), t = Array.from(target);
    let k = 0;
    while (k < Math.min(g.length, t.length) && g[k] === t[k]) k++;
    if (g.length - k > 64 || t.length - k > 64) return;
    if (g.length > k) { await backspaces(g.length - k); await sleep(rand(80, 200)); }
    let prev: string | null = null;
    for (const c of t.slice(k)) {
      if (prev !== null) await sleep(flight(prev, c));
      await emitKey(c);
      prev = c;
    }
  };

  // The word-delete chord the ENGINE's editing honours: Alt+Backspace on macOS, else Ctrl.
  const wordDelete = (): Promise<void> => pressKey((hostWheelPlatform() === "macos" ? "Alt" : "Control") + "+Backspace");

  // Make the planned slip at chars[i] and correct it. Returns true when it already typed chars[i] itself
  // (the doubled-key case) -- or left a substitution uncorrected (`leave`: a search field) --, so the
  // caller must not type it again.
  //
  // Only what actually LANDED is deleted: an input mask or a maxlength can swallow the wrong key, and
  // a Backspace pressed for a key that never arrived deletes a real character (the
  // letter-typo-in-a-numeric-mask failure). Measured before and after the slip, in the isolated world.
  // A substitution noticed inside a word is fixed with the word-delete chord and the word typed again in
  // WORD_FIX_RATE of the cases (when the field can be read to put it back exactly).
  const doTypo = async (chars: string[], i: number, tp: TypoPlan, leave = false): Promise<boolean> => {
    const before = await activeValue();
    if (tp.kind === "sub") {
      await emitKey(tp.wrong);
      let got = landed(before, await activeValue());
      if (got !== null && got <= 0) return false; // swallowed: nothing to correct
      if (leave) return true;                     // the slip stays: the wrong key took this place
      let typed = 1;
      let prev = tp.wrong;
      for (const c of chars.slice(i + 1, i + 1 + tp.late)) { // late notice: a key or two more
        await sleep(flight(prev, c));
        await emitKey(c);
        prev = c;
        typed++;
      }
      await sleep(rand(140, 340)); // noticing
      let ws = 0;
      for (let j = i - 1; j >= 0; j--) if (chars[j] === " " || chars[j] === "\n") { ws = j + 1; break; }
      if (before !== null && i - ws + typed >= 2 && nativeKbDown && Math.random() < WORD_FIX_RATE) {
        await wordDelete();
        await sleep(rand(120, 260));
        await restore(before);
        await sleep(rand(80, 200));
        return false;
      }
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
      await sleep(flight(chars[i + 1], chars[i]));
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
  // Cadence: keys.planGap, the planner Python and .NET use (was uniform 45-150 ms here, with the
  // thinking pause REPLACING the gap): the persona's WPM sets a base flight, scaled by how the hand
  // travels between the two keys (same finger 1.5-1.9x, same hand 1.1-1.3x, the other hand 0.85-1.0x,
  // a two-row reach 1.15x) and log-normal noise, plus a pause before a new word and after punctuation,
  // the whitespace pause and a 6 % thinking pause, ADDED. `floorMs` (the caller's `delay`) is the
  // shortest gap between two keys.
  //
  // Capitals and shifted symbols are typed the way a person types them: ShiftLeft goes down a moment
  // before the key (35-120 ms) and comes up just after it (10-70 ms), held across a run of them (none
  // under a Shift the caller holds through keyboard.down). Fast pairs ROLL OVER at the persona's
  // rolloverRate. Fat-finger typos are planned by forms.planTypo at the persona's rate and never happen
  // where a slip is a failure rather than a slip; `exact` false (type / pressSequentially /
  // keyboard.type, never fill) lets a search-field substitution stay uncorrected (SEARCH_SLIP_RATE).
  const rollRate = rolloverRate(persona);
  const humanTypeText = async (text: string, field?: TypoField | null, typos = true, floorMs = 0, exact = true): Promise<void> => {
    const chars = Array.from(text);
    const n = chars.length;
    let shift = false;
    const extShift = heldKeys.has("Shift");
    const rate = typos ? personaTypoRate() : 0;
    const fld = rate > 0 ? await typingField(field) : null;
    const allow = rate > 0 && typosAllowed(fld);
    const search = allow && !exact && !!fld && String((fld as any).type || "").toLowerCase() === "search";
    const floor = Math.max(0, Number(floorMs) || 0);
    try {
      for (let i = 0; i < n; i++) {
        let ch = chars[i];
        const need = needsShift(ch) && !extShift;
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
            const leave = search && tp.kind === "sub" && Math.random() < SEARCH_SLIP_RATE;
            try { consumed = await doTypo(chars, i, tp, leave); } catch (e) { if (isStrict(e)) throw e; /* typo path best-effort */ }
          }
        }
        // Rollover of ch and the next char: unshifted single-key pairs only, never a key with itself,
        // never after a typo, never under a caller's delay floor. keyboard.down inserts the character
        // exactly as press does, so the typed value is unchanged.
        const nx = chars[i + 1];
        let rolled = false;
        if (nativeKbDown && nativeKbUp && !shift && i + 1 < n && !typo && floor <= 0 && ROLL_KEYS.has(ch)
            && ROLL_KEYS.has(nx) && ch !== nx && Math.random() < rollRate) {
          const d1 = dwell(), d2 = dwell();
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
        if (!rolled && !consumed) { try { await emitKey(ch); } catch (e) { if (isStrict(e)) throw e; break; } }
        if (shift && (i === n - 1 || !needsShift(chars[i + 1]))) {
          await sleep(rand(10, 70));
          await nativeKbUp!("Shift");
          shift = false;
        }
        if (i < n - 1) await sleep(Math.max(floor, planGap(traits, ch, chars[i + 1], Math.random,
          (persona as any).tempo ?? 1, (persona as any).pauseScale ?? 1)));
      }
    } finally {
      // never leave Shift held for the caller's next keystroke
      if (shift) { try { await nativeKbUp!("Shift"); } catch { /* best-effort */ } }
      if (n) st.typed = Date.now(); // a following Enter / Tab waits (afterTyping)
    }
  };

  // The pause between the last typed character and Enter (250-900 ms) or Tab (120-400 ms), centred per
  // persona, less whatever the caller already spent. Once per typed run; a glide in between cancels it.
  const afterTyping = async (k: string): Promise<void> => {
    const t = st.typed;
    st.typed = null;
    if (t === null) return;
    const [mods, base] = splitChord(k);
    let ms: number;
    if (base === "Enter" && !mods.length) ms = submitPauseMs(traits, Math.random);
    else if (base === "Tab" && mods.every((m) => m === "Shift")) ms = tabPauseMs(traits, Math.random);
    else return;
    const rest = ms - (Date.now() - t);
    if (rest > 0) await sleep(rest);
  };

  // One key: pressed with the persona's hold (or the caller's `delay`); with `repeat` > 1 HELD the way an
  // OS auto-repeats it -- one keydown, the repeat delay (250-500 ms), then a keydown with repeat=true every
  // 25-40 ms (Playwright sends a key that is already down as an autoRepeat keydown), `repeat` keydowns in
  // all, then one keyup.
  const holdKey = async (k: string, delay: number | null = null, repeat = 1): Promise<void> => {
    const n = Math.max(1, Math.floor(Number(repeat) || 1));
    if (n === 1 || !nativeKbDown || !nativeKbUp) {
      for (let j = 0; j < n; j++) {
        await nativeKbPress(k, { delay: delay && delay > 0 ? delay : dwell() });
        if (j < n - 1) await sleep(rand(60, 150));
      }
      return;
    }
    const rp = planRepeat(Math.random);
    await nativeKbDown(k);
    try {
      await sleep(rp.firstMs);
      for (let j = 0; j < n - 1; j++) {
        if (j) await sleep(rp.gapMs);
        await nativeKbDown(k);
      }
      await sleep(rp.releaseMs);
    } finally {
      await nativeKbUp(k); // never leave the key held
    }
  };

  // keyboard.press, humanized: a chord ("Control+a", "ControlOrMeta+Shift+ArrowLeft") is split into its
  // modifiers and its key -- each modifier goes down in order, 40-110 ms before the next, the key is
  // held, 20-80 ms later the modifiers come up in reverse order (as one Playwright press the modifier and
  // the key went down within ~1 ms). A modifier the caller holds (keyboard.down) is not pressed again.
  const pressKey = async (k: string, delay: number | null = null, repeat = 1): Promise<void> => {
    const [all, base] = splitChord(k);
    const mods = all.filter((m) => !heldKeys.has(m));
    if (!mods.length) return holdKey(base, delay, repeat);
    if (!nativeKbDown || !nativeKbUp) { await nativeKbPress(k, { delay: delay && delay > 0 ? delay : dwell() }); return; }
    const down: string[] = [];
    try {
      for (const m of mods) {
        await nativeKbDown(m);
        down.push(m);
        await sleep(modifierLeadMs(Math.random));
      }
      await holdKey(base, delay, repeat);
      await sleep(modifierLagMs(Math.random));
    } finally {
      let j = 0;
      for (const m of down.reverse()) {
        if (j++) await sleep(rand(10, 40));
        try { await nativeKbUp(m); } catch { /* one failed keyup must not strand the others */ }
      }
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

  keyboard.type = (text: string, options: any = {}) => locked(async () => {
    await keyboardWarmup();
    await humanTypeText(text, undefined, true, Number(options?.delay) || 0, false);
  });

  // keyboard.down / up, tracked: a chord does not press a modifier the caller holds, typing does not
  // press Shift under the caller's Shift, and a down() straight followed by up() still holds the key
  // for a persona dwell (no finger lifts a key in 0 ms).
  if (nativeKbDown && nativeKbUp) {
    keyboard.down = (k: string) => locked(async () => {
      await keyboardWarmup();
      if (!heldKeys.has(k)) heldKeys.set(k, [Date.now(), dwell()]);
      return nativeKbDown(k);
    });
    keyboard.up = (k: string) => locked(async () => {
      const t = heldKeys.get(k);
      heldKeys.delete(k);
      if (t) { const rest = t[1] - (Date.now() - t[0]); if (rest > 0) await sleep(rest); }
      return nativeKbUp(k);
    });
  }

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
  // specific time. `delay: 0` IS Playwright's default ("no hold"), so it gets the persona's hold. A
  // chord is pressed modifier first (pressKey); `repeat: n` (the SDK's) holds the key through n OS
  // auto-repeat keydowns; Enter or Tab right after typing waits the persona's pause (afterTyping).
  keyboard.press = (k: string, options: any = {}) => locked(async () => {
    await keyboardWarmup();
    const o = options && typeof options === "object" ? options : {};
    await afterTyping(k);
    return pressKey(k, typeof o.delay === "number" && o.delay > 0 ? o.delay : null, o.repeat ?? 1);
  });

  // ------------------------------------------------------- page-level targeted helpers
  // Points tried when resolving a control through the browser's hit test: a floating label or an icon
  // often sits over a field's CENTRE, so the edges get a chance too.
  const probePoints: [number, number][] = [[0.5, 0.5], [0.82, 0.5], [0.18, 0.5], [0.5, 0.8], [0.5, 0.2]];

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
      if (force) throw new NativeSignal();
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
    await glide(pt.x, pt.y);
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
      await nativeKbPress("a", { delay: dwell() });
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

  // Where the control `info` (described, with its hit-test node) sits in the page's Tab order relative to
  // the focused element (keys.TAB_DISTANCE, read in the isolated world), or null.
  const tabDistance = async (info: DescribedElement | null, selector: string | null = null): Promise<TabInfo | null> => {
    if (!info || info.node === undefined || info.node === null || !info.rect) return null;
    const [x, y, width, height] = info.rect;
    const arg: Record<string, unknown> = { x, y, width, height };
    if (typeof selector === "string" && selector) arg.sel = selector;
    try { return (await worldFor(page).callOnNode<TabInfo | null>(info.node as number, TAB_DISTANCE, arg)) ?? null; } catch { return null; }
  };

  // Press Tab `d` times (Shift+Tab when d < 0): the first after the persona's Tab pause when it follows
  // typing, the rest 110-260 ms apart (the eye follows the focus ring).
  const tabSteps = async (d: number): Promise<void> => {
    const k = d > 0 ? "Tab" : "Shift+Tab";
    for (let j = 0; j < Math.abs(d); j++) {
      if (j === 0) await afterTyping(k); else await sleep(rand(110, 260));
      await pressKey(k);
    }
  };

  // Tab into the control `info` instead of clicking it, the way a person moves between adjacent fields:
  // when focus sits in a TEXT field 1-3 stops before it (or 1 after: Shift+Tab), in the persona's share
  // (60-85 %, keys.typingTraits tabPref). `force`: any focused element, up to 3 stops either way, no roll
  // (a button a press goes to). `needEmpty`: only an empty field (Tab selects a field's text; type()
  // appends at the caret). True once focus is verified there; false = click.
  const tabInto = async (loc: Locator, info: DescribedElement | null, selector: string | null = null,
    needEmpty = false, force = false): Promise<boolean> => {
    if (needEmpty && info && info.value) return false;
    const td = await tabDistance(info, selector);
    if (!td || td.dist === null || td.focused) return false;
    const d = td.dist;
    if (force) {
      if (!(Math.abs(d) >= 1 && Math.abs(d) <= 3)) return false;
    } else if (!td.fromText || !((d >= 1 && d <= 3) || d === -1) || Math.random() >= tabPref()) {
      return false;
    }
    await tabSteps(d);
    await sleep(rand(40, 120));
    return focused(loc);
  };

  // Make `selector` the focus the way a person does -- Tab from the field before it (tabInto) or a click
  // --, unless it already is. Only text-entry controls are clicked; for anything else, and when the click
  // did not actually put the caret there (an overlay took it), the caller goes native. Tab only into an
  // EMPTY field.
  const clickInto = async (selector: string, timeout: number | undefined): Promise<void> => {
    const loc = page.locator(selector).first();
    if (await focused(loc)) return;
    const te = await textEntry(loc, timeout, selector);
    if (!te) throw new NativeSignal();
    if (await tabInto(loc, te.info, selector, true)) return;
    if (!(await focusClick(te.box, provenPoint(te.box, te.info), loc, selector)) || !(await focused(loc))) {
      throw new NativeSignal();
    }
    await sleep(rand(40, 120));
  };

  // Give `selector` -- any focusable element -- the keyboard focus the way a person does: Tab when it is
  // 1-3 stops from the focused element (always for what a click would ACTIVATE: a button, a link, a
  // checkbox), else a humanized click for what a click only focuses. True when focus is verified there;
  // false for an activatable target out of Tab reach (the pointer has glided onto it; the caller gives
  // the focus natively, reported) -- never Playwright's 0 ms focus-and-press.
  const focusTarget = async (selector: string, timeout: number | undefined): Promise<boolean> => {
    const loc = page.locator(selector).first();
    if (await focused(loc)) return true;
    const box = await boxOf(loc, timeout, false, selector);
    const info = box ? await describe(box, FOCUSABLE) : null;
    if (!box || !info) throw new NativeSignal();
    const td = await tabDistance(info, selector);
    const act = td === null || td.activatable; // unknown: never risk an activating click
    if (td && td.dist !== null && Math.abs(td.dist) >= 1 && Math.abs(td.dist) <= 3) {
      if ((act || Math.random() < tabPref()) && await tabInto(loc, info, selector, false, true)) return true;
    }
    if (!act && !info.disabled) {
      if (await focusClick(box, provenPoint(box, info), loc, selector) && await focused(loc)) {
        await sleep(rand(40, 120));
        return true;
      }
      throw new NativeSignal();
    }
    const p0 = clickPoint(box, st, persona);
    await glide(roundHalfEven(p0.x), roundHalfEven(p0.y)); // the hand goes to it; the focus has to be given
    return false;
  };

  const htype = async (selector: string, text: string, options: any): Promise<any> => {
    try {
      await clickInto(selector, options?.timeout);
      await humanTypeText(text, undefined, true, Number(options?.delay) || 0, false);
      return;
    } catch (e) {
      if (isTimeout(e)) throw e;
    }
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
    // From the field before it, a person Tabs (persona share, focus verified); else a click.
    if (!(info && await tabInto(loc, info, selector))) {
      if (!(await focusClick(box, box ? provenPoint(box, info) : null, loc, selector))) return false;
      await sleep(rand(40, 120));
      if (!(await focused(loc))) return false;
    }
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
    await sleep(rand(150, 350));
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
      await sleep(rand(60, 220)); // between fields: the eye checks what it typed
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
    try {
      if (typeof value === "string") {
        const timeout = options?.timeout; // undefined: the page's default, as Playwright would use
        const loc = page.locator(selector).first();
        const box = await boxOf(loc, timeout, Boolean(options?.force), selector);
        const info = await describe(box);
        const kind = info ? classifyField(info.tag, info.type, info.ce) : await classifyFallback(loc);
        if (!(info && (info.disabled || info.readonly))) {
          if (kind === "text" && await fillText(loc, box, value, info, selector)) return;
          if (kind === "range" && box && await fillRange(loc, box, info, value)) return;
          if (kind === "datetime" && box && await fillDatetime(loc, box, info, value)) return;
        }
      }
    } catch (e) {
      if (isTimeout(e)) throw e;
    }
    // Native: color (Playwright sets it -- the OS dialog is no route), unfillable types (Playwright
    // raises, as before humanize), and anything the humane route could not prove it did right.
    return nativePageFill(selector, value, options);
  };
  (page as any).fill = apiErrors("page.fill", (selector: string, value: string, options: any = {}) =>
    locked(() => hfill(selector, value, options)));

  (page as any).dblclick = apiErrors("page.dblclick", (selector: string, options: any = {}) => locked(() => {
    const o = { ...(options ?? {}) };
    if (o.clickCount === undefined) o.clickCount = 2;
    return targetedClick(nativePageDblclick, selector, o, false, options ?? {});
  }));

  const nativePageFocus = typeof (page as any).focus === "function" ? (page as any).focus.bind(page) : null;

  // A humanized route handed this call to Playwright: told to lane A's on_fallback hook when the page
  // carries one (page._clearcoteOnFallback), else nothing (as before).
  const fallback = (api: string, reason: string): void => {
    // lane A's reporter when the merged build installs one on the page (it throws in strict mode: not caught)
    const report = (page as any)._clearcoteReportFallback;
    if (typeof report === "function") { report(api, reason); return; }
    const cb = (page as any)._clearcoteOnFallback;
    if (typeof cb === "function") { try { cb({ api, reason }); } catch { /* the hook's own problem */ } }
  };

  // Give `selector` the focus before a key goes to it: a text field by Tab from the field before it
  // (only when empty) or a click; anything else through focusTarget; an activatable target out of Tab
  // reach gets the pointer over it and Playwright's focus (reported), and the key is still held.
  const focusForPress = async (selector: string, timeout: number | undefined): Promise<void> => {
    const loc = page.locator(selector).first();
    if (await focused(loc)) return;
    if (!(await loc.isVisible())) throw new NativeSignal(); // Playwright waits for it
    const box = await boxOf(loc, timeout, false, selector);
    const info = await describe(box);
    if (box && info && classifyField(info.tag, info.type, info.ce) === "text") {
      if (await tabInto(loc, info, selector, true)) return;
      if (!(await focusClick(box, provenPoint(box, info), loc, selector)) || !(await focused(loc))) throw new NativeSignal();
      await sleep(rand(40, 120));
      return;
    }
    if (await focusTarget(selector, timeout)) return;
    if (!nativePageFocus) throw new NativeSignal();
    fallback("page.press", "focus: an activatable target out of Tab reach");
    await nativePageFocus(selector, timeout !== undefined ? { timeout } : {});
    await sleep(rand(40, 120));
  };

  // page.press (and locator.press): the target gets the focus the way a person gives it (never
  // Playwright's focus-and-press in 0 ms), then the key goes through the keyboard model -- a chord's
  // modifier first, the persona's hold (or the caller's `delay`), `repeat: n` as OS auto-repeat, and the
  // submit / Tab pause after typing.
  const hpress = async (selector: string, key: string, options: any): Promise<any> => {
    const { repeat, ...opts } = options ?? {};
    try {
      await focusForPress(selector, opts.timeout);
      await afterTyping(key);
      const d = opts.delay;
      await pressKey(key, typeof d === "number" && d > 0 ? d : null, repeat ?? 1);
      return;
    } catch (e) {
      if (isTimeout(e)) throw e;
    }
    fallback("page.press", "native press");
    let r: any;
    for (let j = 0; j < Math.max(1, Math.floor(Number(repeat) || 1)); j++) r = await nativePagePress(selector, key, opts);
    return r;
  };
  (page as any).press = apiErrors("page.press", (selector: string, key: string, options: any = {}) =>
    locked(() => hpress(selector, key, options)));

  // page.focus / locator.focus: Tab or a click (focusTarget), verified; Playwright's focus for an
  // activatable target out of Tab reach (reported) and for anything the humane route cannot do.
  if (nativePageFocus) {
    (page as any).focus = apiErrors("page.focus", (selector: string, options: any = {}) => locked(async () => {
      try {
        if (await focusTarget(selector, options?.timeout)) return;
        fallback("page.focus", "an activatable target out of Tab reach");
      } catch (e) {
        if (isTimeout(e)) throw e;
        fallback("page.focus", "native focus");
      }
      return nativePageFocus(selector, options);
    }));
  }

  // locator.blur, humanized: a focused element is left the way the persona leaves fields -- Tab to the
  // next stop, or a click on a blank point beside it (keys.BLANK_POINT; Tab when there is none). True when
  // focus is verified gone; false = Playwright's blur.
  const blurHuman = async (selector: string, options: any): Promise<boolean> => {
    const loc = page.locator(selector).first();
    if (!(await focused(loc))) return false;
    if (traits.blurStyle === "click") {
      const box = await boxOf(loc, options?.timeout, false, selector);
      const vp = await viewport();
      const pt = box ? await worldFor(page).evaluate<[number, number] | null>(BLANK_POINT, { box, vw: vp.width, vh: vp.height }) : null;
      if (Array.isArray(pt) && pt.length === 2) {
        const x = roundHalfEven(pt[0] + gauss() * 3), y = roundHalfEven(pt[1] + gauss() * 3);
        await glide(x, y, { targetW: 60 });
        await pressHere(x, y);
        await sleep(rand(40, 120));
        if (!(await focused(loc))) return true;
      }
    }
    await afterTyping("Tab");
    await pressKey("Tab");
    await sleep(rand(40, 120));
    return !(await focused(loc));
  };

  const selectedAll = async (): Promise<boolean> => {
    const s = await worldFor(page).evaluate<number[] | null>(SELECTION_STATE);
    return Array.isArray(s) && s.length === 3 && Math.min(s[0], s[1]) === 0 && Math.max(s[0], s[1]) === s[2];
  };

  // locator.selectText, humanized: a triple click (an <input>) or the keys -- End then Shift+Home
  // (Ctrl+End, Ctrl+Shift+Home in a textarea; Cmd+A on macOS) --, per persona; verified in the isolated
  // world (SELECTION_STATE), one select-all retry; false = Playwright's selectText.
  const selectTextHuman = async (selector: string, options: any): Promise<boolean> => {
    const loc = page.locator(selector).first();
    const box = await boxOf(loc, options?.timeout, !!options?.force, selector);
    const info = box ? await describe(box) : null;
    if (!box || !info || info.disabled || classifyField(info.tag, info.type, info.ce) !== "text") return false;
    if (traits.selectStyle === "triple" && info.tag === "input") {
      const at = provenPoint(box, info);
      const p0 = at ? { x: at[0], y: at[1] } : clickPoint(box, st, persona);
      await glide(roundHalfEven(p0.x), roundHalfEven(p0.y));
      await pressAt(normalizeClickOptions({ clickCount: 3 }));
    } else {
      if (!(await focused(loc))) {
        if (!(await focusClick(box, provenPoint(box, info), loc, selector)) || !(await focused(loc))) return false;
        await sleep(rand(120, 300));
      }
      if (hostWheelPlatform() === "macos") await pressKey("ControlOrMeta+a");
      else if (info.tag === "input") { await pressKey("End"); await sleep(rand(80, 200)); await pressKey("Shift+Home"); }
      else { await pressKey("ControlOrMeta+End"); await sleep(rand(80, 200)); await pressKey("ControlOrMeta+Shift+Home"); }
    }
    await sleep(rand(60, 160));
    if (await selectedAll()) return true;
    await selectAll();
    await sleep(rand(60, 160));
    return selectedAll();
  };

  // Run the humane `fn` (true = done); Playwright's own locator method when it declines or fails (a
  // timeout propagates).
  const guarded = (api: string, fn: (s: string, o: any) => Promise<boolean>, name: string) =>
    (selector: string, options: any = {}) => locked(async () => {
      try {
        if (await fn(selector, options)) return;
      } catch (e) {
        if (isTimeout(e)) throw e;
      }
      fallback(api, "native");
      return LOCATOR_NATIVE[name].call(page.locator(selector).first(), options);
    });
  (page as any)._clearcoteBlur = guarded("locator.blur", blurHuman, "blur");
  (page as any)._clearcoteSelectText = guarded("locator.selectText", selectTextHuman, "selectText");

  // ------------------------------------------------------------------------- selectOption
  // Open the drop-down with a click, move its highlight with the planned keys, commit with Enter: one
  // trusted input+change, the sequence a mouse-and-keyboard user produces. While the popup is open its
  // keys never reach the page (measured on r37: value unchanged until Enter). The opening click is the
  // full humanized click -- including the covered-point check -- at the point the hit test proved when
  // the centre is covered (`at`, a Playwright `position`), so a floating label over the select's centre
  // cannot swallow it and leave the letters to change a closed select one per key.
  const openAndPick = async (selector: string, plan: SelectPlan, at: { x: number; y: number } | null): Promise<void> => {
    await targetedClick(nativePageClick, selector, at ? { position: at } : {}, false, null);
    await sleep(rand(220, 480)); // the list opens; the eye finds the option
    for (const ch of plan.keys) {
      await nativeKbPress(ch, { delay: dwell() });
      await sleep(Math.max(40, 100 + gauss() * 35)); // well inside type-ahead's 1 s window
    }
    const n = plan.arrows;
    if (n) {
      await sleep(rand(120, 300));
      await arrowRun(n > 0 ? "ArrowDown" : "ArrowUp", Math.abs(n));
    }
    await sleep(rand(150, 400));
    await key("Enter");
    await sleep(rand(80, 160));
  };

  // `n` presses of an arrow key in an OPEN drop-down. A long run is HELD, the way a person crosses a
  // long list: one press, the keyboard's repeat delay, then auto-repeat at its rate -- Playwright sends
  // a key that is already down again as an autoRepeat keydown. Pressed one by one at a human ~180 ms
  // each, 370 rows took 68 s (verifier B). The open popup takes these keys, so the page sees none of
  // them, only how long the pick took.
  const arrowRun = async (k: string, n: number): Promise<void> => {
    if (n < 8) { await key(k, n); return; }
    await holdKey(k, null, n); // OS auto-repeat (keys.planRepeat)
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
      try {
        const res = await selectHumanized(selector, values, options ?? {});
        if (res !== null) return res;
      } catch (e) {
        if (isTimeout(e)) throw e;
      }
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
    await targetedClick(nativePageClick, selector, { ...opts }, false, null);
    if ((await loc.isChecked()) === checked) return;
    throw sdkPlaywrightError("Clicking the checkbox did not change its state");
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
    nativeOpts: any | null): Promise<any> => {
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
    try {
      const timeout = opts.timeout; // undefined: the page's default, as Playwright would use
      const loc = page.locator(selector).first();
      // Playwright skips actionability with force: no wait then -- a target it would click hidden goes
      // native at once instead of timing out first.
      const box = await boxOf(loc, timeout, o.force, selector);
      if (!box) throw new NativeSignal();
      aim = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
      if (!o.force && !(await loc.isEnabled())) throw new NativeSignal();
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
        if (!(await reachesPoint(box, x, y, selector))) throw new NativeSignal();
      }
      if (o.modifiers.length && !(nativeKbDown && nativeKbUp)) throw new NativeSignal();
      if (!noClick && !o.force && orig && engineClicksMode() === "all" && enginePathOk(o) && await engineGlide(x, y, false)) {
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
          await glide(x, y);
        } finally {
          for (const m of held.reverse()) {
            await sleep(rand(20, 80));
            try { await nativeKbUp!(m); } catch { /* one failed keyup must not strand the others */ }
          }
        }
        return;
      }
      await glide(x, y);
      if (!noClick) await pressAt(o);
      return;
    } catch (e) {
      if (isTimeout(e)) throw e;
    }
    // outside the try, so a native call that raises is not run a second time by the handler
    return native();
  };

  (page as any).click = apiErrors("page.click", (selector: string, options: any = {}) =>
    locked(() => targetedClick(nativePageClick, selector, options, false, null)));
  (page as any).hover = apiErrors("page.hover", (selector: string, options: any = {}) =>
    locked(() => targetedClick(nativePageHover, selector, options, true, null)));
  // Locator.dragTo({ trial: true }) answers its checks here too (see the Locator patch).
  (page as any)._clearcoteTrial = (selector: string, options: any, noClick: boolean) =>
    locked(() => trial(selector, options, noClick));

  // pressAndHold (hold.ts): the hooks it reads off the page. It holds the page lock for the whole hold
  // (one action at a time); inside it uses page.mouse (not locked) and these hooks. _clearcoteNudge is
  // the tremor step: a native move (no glide) that keeps the tracked position where the pointer is.
  (page as any)._clearcoteBoxOf = (loc: Locator, timeout: number | undefined, selector: string | null = null) =>
    boxOf(loc, timeout, false, selector);
  (page as any)._clearcotePointer = (): Point => ({ x: st.x, y: st.y });
  (page as any)._clearcoteNudge = async (x: number, y: number): Promise<void> => {
    await nativeMove(x, y);
    st.x = x; st.y = y; st.placed = true;
  };
  (page as any)._clearcoteHold = (loc: Locator, o?: PressAndHoldOptions) => locked(() => pressAndHold(loc, o));
  (page as any).pressAndHold = apiErrors("page.pressAndHold", (selector: string, o?: PressAndHoldOptions) =>
    (page as any)._clearcoteHold(page.locator(selector).first(), o));

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

// --------------------------------------------------------------------------- locator patch
let locatorPatched = false;

// The options each page-level humanized method understands. Everything the caller passed that a page
// method accepts travels with it: the old fwd sent ONLY `timeout`, which turned
// click({button: "right"}) into a left click and click({trial: true}) into a real one.
const CLICK_OPTS = ["button", "clickCount", "delay", "force", "modifiers", "noWaitAfter", "position", "timeout", "trial"];
const HOVER_OPTS = ["force", "modifiers", "noWaitAfter", "position", "timeout", "trial"];
const DBL_OPTS = ["button", "delay", "force", "modifiers", "noWaitAfter", "position", "timeout", "trial"];
const FILL_OPTS = ["force", "noWaitAfter", "timeout"];
const TYPE_OPTS = ["delay", "timeout"];   // delay: the shortest gap between two keys (humanized typing)
const PRESS_OPTS = ["delay", "noWaitAfter", "timeout", "repeat"];   // repeat: the SDK's OS auto-repeat
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
// Playwright's own Locator methods the humanized blur / selectText fall back to.
const LOCATOR_NATIVE: Record<string, any> = {};

function patchLocatorClass(sample: Locator): void {
  if (locatorPatched) return;
  locatorPatched = true;
  const proto: any = Object.getPrototypeOf(sample);
  if (!proto) return;

  const oFill = proto.fill, oClick = proto.click, oType = proto.type, oDbl = proto.dblclick,
    oHover = proto.hover, oPress = proto.press, oPressSeq = proto.pressSequentially,
    oClear = proto.clear, oTap = proto.tap, oCheck = proto.check, oUncheck = proto.uncheck,
    oDragTo = proto.dragTo, oSelectOption = proto.selectOption, oSetChecked = proto.setChecked,
    oFocus = proto.focus, oBlur = proto.blur, oSelectText = proto.selectText;
  LOCATOR_NATIVE.blur = oBlur;
  LOCATOR_NATIVE.selectText = oSelectText;

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
  const route = async (self: any, fn: () => Promise<any>): Promise<[boolean, any]> => {
    if (on(self) && await strict(self)) return [true, await fn()];
    return [false, undefined];
  };
  // Every routed method words its errors as Playwright words the Locator method (asApiError): through
  // page.*, an error read "page.click: ..." or "locator.waitFor: ...".
  const apiMethod = (api: string, fn: (this: any, ...a: any[]) => Promise<any>) =>
    async function (this: any, ...a: any[]): Promise<any> {
      try { return await fn.apply(this, a); } catch (e) { throw asApiError(e, api); }
    };
  // Playwright's tap rejects at once in a context without hasTouch ("The page does not support tap");
  // routed as a click it clicked instead (SC 210 error parity). A context it cannot read is routed.
  const noTouch = (self: any): boolean => {
    try { return self.page().context()._options.hasTouch !== true; } catch { return false; }
  };

  proto.fill = apiMethod("locator.fill", async function (this: any, value: string, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().fill(sel(this), value, pickOptions(kw, FILL_OPTS)));
    return done ? r : oFill.call(this, value, kw);
  });
  proto.click = apiMethod("locator.click", async function (this: any, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().click(sel(this), pickOptions(kw, CLICK_OPTS)));
    return done ? r : oClick.call(this, kw);
  });
  proto.type = apiMethod("locator.type", async function (this: any, text: string, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().type(sel(this), text, pickOptions(kw, TYPE_OPTS)));
    return done ? r : oType.call(this, text, kw);
  });
  proto.dblclick = apiMethod("locator.dblclick", async function (this: any, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().dblclick(sel(this), pickOptions(kw, DBL_OPTS)));
    return done ? r : oDbl.call(this, kw);
  });
  proto.hover = apiMethod("locator.hover", async function (this: any, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().hover(sel(this), pickOptions(kw, HOVER_OPTS)));
    return done ? r : oHover.call(this, kw);
  });
  proto.press = apiMethod("locator.press", async function (this: any, key: string, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().press(sel(this), key, pickOptions(kw, PRESS_OPTS)));
    if (done) return r;
    const { repeat, ...rest } = kw ?? {};   // not humanized: `repeat` presses, Playwright's own each
    let out: any;
    for (let j = 0; j < Math.max(1, Math.floor(Number(repeat) || 1)); j++) out = await oPress.call(this, key, rest);
    return out;
  });
  if (typeof oFocus === "function") {
    proto.focus = apiMethod("locator.focus", async function (this: any, kw: any = {}) {
      const [done, r] = await route(this, () => this.page().focus(sel(this), pickOptions(kw, ["timeout"])));
      return done ? r : oFocus.call(this, kw);
    });
  }
  if (typeof oBlur === "function") {
    proto.blur = apiMethod("locator.blur", async function (this: any, kw: any = {}) {
      const fn = this.page()?._clearcoteBlur;
      if (typeof fn === "function") {
        const [done, r] = await route(this, () => fn(sel(this), pickOptions(kw, ["timeout"])));
        if (done) return r;
      }
      return oBlur.call(this, kw);
    });
  }
  if (typeof oSelectText === "function") {
    proto.selectText = apiMethod("locator.selectText", async function (this: any, kw: any = {}) {
      const fn = this.page()?._clearcoteSelectText;
      if (typeof fn === "function") {
        const [done, r] = await route(this, () => fn(sel(this), pickOptions(kw, ["force", "timeout"])));
        if (done) return r;
      }
      return oSelectText.call(this, kw);
    });
  }
  proto.pressSequentially = apiMethod("locator.pressSequentially", async function (this: any, text: string, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().type(sel(this), text, pickOptions(kw, TYPE_OPTS)));
    return done ? r : oPressSeq.call(this, text, kw);
  });
  proto.clear = apiMethod("locator.clear", async function (this: any, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().fill(sel(this), "", pickOptions(kw, FILL_OPTS)));
    return done ? r : oClear.call(this, kw);
  });
  proto.tap = apiMethod("locator.tap", async function (this: any, kw: any = {}) {
    if (noTouch(this)) return oTap.call(this, kw);
    const [done, r] = await route(this, () => this.page().click(sel(this), pickOptions(kw, HOVER_OPTS)));
    return done ? r : oTap.call(this, kw);
  });
  proto.check = apiMethod("locator.check", async function (this: any, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().check(sel(this), pickOptions(kw, CHECK_OPTS)));
    return done ? r : oCheck.call(this, kw);
  });
  proto.uncheck = apiMethod("locator.uncheck", async function (this: any, kw: any = {}) {
    const [done, r] = await route(this, () => this.page().uncheck(sel(this), pickOptions(kw, CHECK_OPTS)));
    return done ? r : oUncheck.call(this, kw);
  });
  if (typeof oSetChecked === "function") {
    proto.setChecked = apiMethod("locator.setChecked", async function (this: any, checked: boolean, kw: any = {}) {
      const [done, r] = await route(this, () => this.page().setChecked(sel(this), checked, pickOptions(kw, CHECK_OPTS)));
      return done ? r : oSetChecked.call(this, checked, kw);
    });
  }
  // Delegate to the humanized page.selectOption, like every other method here: one implementation
  // behind the page method is what stops the two entry points drifting apart.
  if (typeof oSelectOption === "function") {
    proto.selectOption = apiMethod("locator.selectOption", async function (this: any, values: any, kw: any = {}) {
      const [done, r] = await route(this, () => this.page().selectOption(sel(this), values, pickOptions(kw, SELECT_OPTS)));
      return done ? r : oSelectOption.call(this, values, kw);
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
    if (on(this) && !kw?.trial) {
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
      } catch { /* fall back */ }
    }
    return oDragTo.call(this, target, kw);
  });
}

/** Install humanize/showCursor on a browser: wraps newPage/newContext so every page is covered. */
export function installHumanize(browser: Browser, opts: HumanizeOptions): void {
  if (!opts.humanize && !opts.showCursor) return;
  const origNewPage = browser.newPage.bind(browser);
  (browser as any).newPage = async (options?: any) => {
    const page = await origNewPage(options);
    await attachHumanize(browser, page, opts);
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
  context: BrowserContext, opts: HumanizeOptions, browser?: Browser | null
): void {
  if (!opts.humanize && !opts.showCursor) return;
  const b = browser ?? context.browser();
  if (opts.showCursor) { context.addInitScript(CURSOR_OVERLAY).catch(() => {}); }
  context.on("page", (p) => { attachHumanize(b, p, opts).catch(() => {}); });
  for (const p of context.pages()) attachHumanize(b, p, opts).catch(() => {});
}
