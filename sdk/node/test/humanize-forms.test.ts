/**
 * The humanized form wiring (src/humanize.ts) against a FAKE page: Playwright's mouse/keyboard/page
 * methods and a CDP session that answers the isolated-world reads and the hit test. A small model of
 * the page's fields (values, focus, selection, a mask, a maxlength, a reformatting mask, a drop-down
 * popup, a list box, an overlay) stands in for the browser, so the tests can check what the wiring
 * presses and in what order without an engine. The live suite (humanize-forms.live.test.ts) checks the
 * same contract on a real build.
 *
 * Mirrors the behaviour contract of SC 197's PORTING-SPEC-humanize-forms.md and FIXES-after-verify-A.md:
 * click options, mouse click/dblclick, the fill routes and their clear, typos (gating, mask-aware
 * correction, the env rate), selectOption, check/uncheck/setChecked, the Locator patch (strictness,
 * option forwarding, error propagation), the caller's timeout, `force`, focus through `:focus`, and the
 * per-page lock. page.evaluate throws throughout: no read may use the page's world.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  apiErrors, asApiError, attachHumanize, createInputLock, isTimeout, pickOptions, sdkPlaywrightError, selectWants, typoRate,
  wantIndex,
} from "../src/humanize.js";
import { errors as pwErrors } from "playwright-core";
import {
  ISO_ACTIVE_FIELD, ISO_ACTIVE_VALUE, ISO_CSS_BOX, ISO_DESCRIBE, ISO_HIT_IN_BOX, ISO_LISTBOX_ROW, ISO_PLATFORM, ISO_SCROLL,
  ISO_SCROLL_BY, ISO_VIEWPORT,
} from "../src/isolated.js";
import { typeaheadSimulate } from "../src/forms.js";
import { BLANK_POINT, FOCUSABLE, SELECTION_STATE, TAB_DISTANCE, typingTraits } from "../src/keys.js";
import { makePersona } from "../src/motion.js";

const SLOW = 30_000; // glides and holds use real sleeps

type Ev = { t: string; a?: any; ts: number };
type Box = { x: number; y: number; width: number; height: number };

interface Field {
  tag: string;
  type: string;
  value: string;
  box: Box;
  accept?: (ch: string) => boolean;   // an input mask: false = the key is swallowed
  format?: (v: string) => string;     // a REFORMATTING mask, applied after every key
  maxlength?: number;
  inputmode?: string | null;
  autocomplete?: string | null;
  checked?: boolean;
  stubborn?: boolean;                  // a checkbox that cancels its click
  noSelectAll?: boolean;               // select-all does nothing here (the clear must fall back)
  inFrame?: boolean;                   // focus inside it reads as {frame: true}
  visible?: boolean;
  min?: string; max?: string; step?: string;   // a range input
  // <select>
  options?: { value: string; label: string; disabled?: boolean; selected?: boolean }[];
  selectedIndex?: number;
  multiple?: boolean;
  size?: number;
  rowH?: number;                       // a list box: each row is rowH px, scrolled by scrollTop
  scrollTop?: number;
  appearAfterWait?: boolean;           // not visible until something waited for it
}

interface FakeOpts {
  field?: Partial<Field>;                      // the single field every selector means
  fields?: Record<string, Partial<Field>>;     // several fields, by selector
  box?: Box;
  engine?: boolean;          // the engine pointer path answers
  count?: number;            // how many elements a locator matches
  waitForError?: Error;      // locator.waitFor / isChecked reject with this
  enabled?: boolean;
  covered?: boolean;         // an overlay owns every point of the field: the hit test and clicks hit it
  coveredCentre?: boolean;   // an overlay over the middle half of the field only
  hitTestBlind?: boolean;    // the hit test finds nothing (an iframe), but clicks reach the field
  labelPassesFocus?: boolean; // the overlay is a floating label: a click on it focuses the field
  wheelPx?: number;          // how far one wheel notch scrolls a list box (default 60)
  nativeThrows?: Record<string, Error>;
}

const timeoutError = (msg = "Timeout 1500ms exceeded.") => Object.assign(new Error(msg), { name: "TimeoutError" });

// ONE locator prototype for this file: attachHumanize patches the Locator prototype once per module.
const protoCalls: { m: string; args: any[] }[] = [];
const ORIG = ["fill", "click", "type", "dblclick", "hover", "press", "pressSequentially", "clear", "tap", "check",
  "uncheck", "setChecked", "selectOption", "dragTo"];
const locatorProto: Record<string, any> = {};
for (const m of ORIG) locatorProto[m] = async function (...args: any[]) { protoCalls.push({ m, args }); return `proto-${m}`; };

const MAIN_FRAME = { name: "main" };
const allExpressions: string[] = [];
const FOCUS_QUERY = { focusQuery: true };

function makeFake(opts: FakeOpts = {}) {
  const defBox: Box = opts.box ?? { x: 100, y: 200, width: 160, height: 40 };
  const mk = (p: Partial<Field>, box: Box): Field => ({ tag: "input", type: "text", value: "", box, ...p } as Field);
  const fields: Record<string, Field> = {};
  let single: Field | null = null;
  if (opts.fields) {
    let y = 100;
    for (const [sel, p] of Object.entries(opts.fields)) { fields[sel] = mk(p, p.box ?? { x: 100, y, width: 160, height: 30 }); y += 60; }
  } else {
    single = mk(opts.field ?? {}, opts.field?.box ?? defBox);
  }
  const fieldFor = (selector: string): Field => single ?? fields[selector];
  const allFields = (): Field[] => (single ? [single] : Object.values(fields));
  const events: Ev[] = [];
  let handles = 0;           // main-world element handles Playwright would make (each = a textContent preview)
  let waited = false;
  const engineCalls: any[] = [];
  const waits: any[] = [];
  const t0 = performance.now();
  const rec = (t: string, a?: any) => events.push({ t, a, ts: performance.now() - t0 });
  let focus: Field | null = null;
  let selAll = false;
  const mods = new Set<string>();
  let pos = { x: 0, y: 0 };
  let popup: { field: Field; buf: string; highlight: number } | null = null;

  const inBox = (b: Box, x: number, y: number) => x >= b.x && x <= b.x + b.width && y >= b.y && y <= b.y + b.height;
  const overlayAt = (f: Field, x: number, y: number) =>
    opts.covered || (opts.coveredCentre && x > f.box.x + f.box.width * 0.25 && x < f.box.x + f.box.width * 0.75);
  const fieldAt = (x: number, y: number) => allFields().find((f) => inBox(f.box, x, y)) ?? null;
  const labels = (f: Field) => (f.options ?? []).map((o) => (o.disabled ? null : o.label));
  const isSelect = (f: Field) => f.tag === "select";
  const listbox = (f: Field) => isSelect(f) && (f.multiple || (f.size ?? 0) > 1);

  const typeChar = (ch: string) => {
    const f = focus;
    if (!f) { rec("stray", ch); return; }
    if (f.accept && !f.accept(ch)) return;
    if (selAll) { f.value = ""; selAll = false; }
    if (f.maxlength !== undefined && Array.from(f.value).length >= f.maxlength) return;
    f.value += ch;
    if (f.format) f.value = f.format(f.value);
  };
  const stepEnabled = (f: Field, from: number, dir: number) => {
    const ls = labels(f);
    let i = from;
    do { i += dir; } while (i >= 0 && i < ls.length && ls[i] === null);
    return i >= 0 && i < ls.length ? i : from;
  };
  const applyKey = (k: string) => {
    if (popup) {
      const f = popup.field;
      if (k.length === 1) {
        popup.buf += k;
        popup.highlight = typeaheadSimulate(labels(f), f.selectedIndex ?? -1, popup.buf);
      } else if (k === "ArrowDown") popup.highlight = stepEnabled(f, popup.highlight, 1);
      else if (k === "ArrowUp") popup.highlight = stepEnabled(f, popup.highlight, -1);
      else if (k === "Enter") {
        if (popup.highlight !== f.selectedIndex) {
          f.selectedIndex = popup.highlight;
          f.value = f.options![popup.highlight].value;
          rec("change", f.value);
        }
        popup = null;
      }
      return;
    }
    if (k === "ControlOrMeta+a" || (k === "a" && (mods.has("ControlOrMeta") || mods.has("Control") || mods.has("Meta")))) {
      if (!focus?.noSelectAll) selAll = true;
      return;
    }
    if (k === "Backspace") {
      if (!focus) return;
      if (selAll) { focus.value = ""; selAll = false; } else focus.value = Array.from(focus.value).slice(0, -1).join("");
      return;
    }
    if (k === "End") { selAll = false; return; }
    if (k === "Tab") {   // lane D: Tab moves the focus along the fields (Shift: back) and selects a field's text
      const order = allFields();
      const i = focus ? order.indexOf(focus) + (mods.has("Shift") ? -1 : 1) : -1;
      focus = i >= 0 && i < order.length ? order[i] : null;
      selAll = !!focus && focus.tag === "input" && focus.value !== "";
      return;
    }
    if (k === "Home" && mods.has("Shift")) { selAll = true; return; }
    if (Array.from(k).length === 1) typeChar(k);
  };
  // a list box: rows of rowH px inside the box's client area, scrolled by scrollTop
  const rowH = (f: Field) => f.rowH ?? 20;
  const maxScroll = (f: Field) => Math.max(0, (f.options ?? []).length * rowH(f) - f.box.height);
  const rowRect = (f: Field, i: number): [number, number, number, number] =>
    [f.box.x, f.box.y + i * rowH(f) - (f.scrollTop ?? 0), f.box.width, rowH(f)];
  const rowAt = (f: Field, y: number) => {
    if (y < f.box.y || y > f.box.y + f.box.height) return -1;
    const i = Math.floor((y - f.box.y + (f.scrollTop ?? 0)) / rowH(f));
    return i >= 0 && i < (f.options ?? []).length ? i : -1;
  };
  const listboxState = (f: Field, i: number) => (!f.options || !f.options[i]) ? null : {
    box: [f.box.x, f.box.y, f.box.width, f.box.height], row: rowRect(f, i),
    top: f.box.y, bottom: f.box.y + f.box.height, st: f.scrollTop ?? 0, max: maxScroll(f),
    sel: f.options.map((o) => !!o.selected),
  };
  const argOf = (expr: string, fn: string) => JSON.parse(expr.slice(fn.length + 3, -1)); // `(${fn})(${json})`
  const pressed = (x: number, y: number, ctrl: boolean) => {
    const f = fieldAt(x, y);
    if (f && overlayAt(f, x, y)) { focus = opts.labelPassesFocus ? f : null; rec("overlay-click", { x, y }); return; }
    if (!f) return;
    focus = f;
    if (f.type === "checkbox") {
      if (f.stubborn) { rec("toggle-cancelled"); return; }
      f.checked = !f.checked; rec("toggle", f.checked); return;
    }
    if (listbox(f)) {
      const r = rowAt(f, y);
      if (r < 0) return;
      for (const [i, o] of f.options!.entries()) if (!ctrl) o.selected = i === r;
      if (ctrl) f.options![r].selected = !f.options![r].selected;
      rec("change", r);
      return;
    }
    if (isSelect(f)) popup = { field: f, highlight: f.selectedIndex ?? 0, buf: "" };
  };

  const mouse = {
    move: async (x: number, y: number) => { pos = { x, y }; rec("move", { x, y }); },
    click: async (x: number, y: number, o: any = {}) => { pos = { x, y }; rec("click", { x, y, ...o }); pressed(x, y, false); },
    down: async (o: any = {}) => { rec("down", o); },
    up: async (o: any = {}) => {
      rec("up", o);
      if (o.clickCount === 3 && focus && fieldAt(pos.x, pos.y) === focus) { selAll = true; return; } // a triple click selects
      if (o.clickCount > 1) return;
      pressed(pos.x, pos.y, mods.has("ControlOrMeta") || mods.has("Control"));
    },
    wheel: async (dx: number, dy: number) => {
      rec("wheel", { dx, dy });
      const f = fieldAt(pos.x, pos.y);
      if (f && listbox(f)) f.scrollTop = Math.min(maxScroll(f), Math.max(0, (f.scrollTop ?? 0) + Math.sign(dy) * (opts.wheelPx ?? 60)));
    },
    dblclick: async (x: number, y: number) => { rec("stock-dblclick", { x, y }); },
  };
  const keyboard = {
    type: async (t: string) => { rec("ktype", t); for (const c of Array.from(t)) applyKey(c); },
    press: async (k: string, o: any = {}) => { rec("press", { k, delay: o.delay }); applyKey(k); },
    down: async (k: string) => {
      rec("kdown", k);
      if (["Shift", "Control", "Meta", "Alt", "ControlOrMeta"].includes(k)) mods.add(k); else applyKey(k);
    },
    up: async (k: string) => { rec("kup", k); mods.delete(k); },
    insertText: async (t: string) => {
      rec("insert", t);
      const fo = focus;
      if (!fo) return;
      if (selAll) { fo.value = ""; selAll = false; }
      const room = fo.maxlength === undefined ? Infinity : Math.max(0, fo.maxlength - Array.from(fo.value).length);
      fo.value += Array.from(t).slice(0, room).join("");
    },
  };

  const describeInfo = (f: Field, want: string | null) => {
    if (want === "select" && !isSelect(f)) return null;
    if (want && want !== "select" && want !== FOCUSABLE && !["input", "textarea", "select"].includes(f.tag)) return null;
    const o: any = {
      tag: f.tag, type: f.tag === "input" ? f.type : "", ce: false,
      rect: [f.box.x, f.box.y, f.box.width, f.box.height], focused: focus === f, value: f.value, min: f.min ?? null, max: f.max ?? null,
      step: f.step ?? null, maxlength: f.maxlength ?? -1, inputmode: f.inputmode ?? null, autocomplete: f.autocomplete ?? null,
      readonly: false, disabled: false, dir: "ltr", wm: "horizontal-tb", bl: 2, bt: 3,
    };
    if (isSelect(f)) {
      o.multiple = !!f.multiple; o.size = f.size ?? 0; o.selectedIndex = f.selectedIndex ?? -1;
      o.options = f.options!.map((x) => ({ value: x.value, label: x.label, disabled: !!x.disabled, selected: !!x.selected }));
      if (listbox(f)) o.optRects = f.options!.map((_, i) => rowRect(f, i));
    }
    return o;
  };

  const cdp = {
    send: async (method: string, params: any = {}): Promise<any> => {
      switch (method) {
        case "Page.getFrameTree": return { frameTree: { frame: { id: "MAIN" } } };
        case "Page.createIsolatedWorld": return { executionContextId: 1 };
        case "Runtime.evaluate": {
          const e: string = params.expression;
          allExpressions.push(e);
          if (e.startsWith(`(${ISO_ACTIVE_VALUE})`)) return { result: { value: focus && !focus.inFrame ? focus.value : null } };
          if (e.startsWith(`(${ISO_ACTIVE_FIELD})`)) {
            if (!focus) return { result: { value: null } };
            if (focus.inFrame) return { result: { value: { frame: true } } };
            return { result: { value: { type: focus.tag === "input" ? focus.type : "", inputmode: focus.inputmode ?? null,
              autocomplete: focus.autocomplete ?? null } } };
          }
          if (e.startsWith(`(${ISO_SCROLL})`)) return { result: { value: [0, 0] } };
          if (e.startsWith(`(${ISO_PLATFORM})`)) return { result: { value: "Win32" } };
          if (e.startsWith(`(${ISO_VIEWPORT})`)) return { result: { value: [1280, 800] } };
          if (e.startsWith(`(${ISO_SCROLL_BY})`)) return { result: { value: [0, 0] } };
          if (e.startsWith(`(${SELECTION_STATE})`)) {   // lane D: select-all is the selection the fake knows
            if (!focus) return { result: { value: null } };
            const n = Array.from(focus.value).length;
            return { result: { value: selAll ? [0, n, n] : [n, n, n] } };
          }
          if (e.startsWith(`(${BLANK_POINT})`)) {      // lane D: a blank point under the field
            const a = argOf(e, BLANK_POINT);
            return { result: { value: [a.box.x + a.box.width / 2, a.box.y + a.box.height + 200] } };
          }
          if (e.startsWith(`(${ISO_CSS_BOX})`)) {
            const a = argOf(e, ISO_CSS_BOX);
            if (/internal:|>>/.test(a.sel)) return { result: { value: null } }; // not CSS the browser accepts
            const f = fieldFor(a.sel);
            if (!f) return { result: { value: { n: 0 } } };
            return { result: { value: { n: 1, rect: f.visible === false ? [0, 0, 0, 0] : [f.box.x, f.box.y, f.box.width, f.box.height] } } };
          }
          return { exceptionDetails: { text: "unknown read" } };
        }
        case "DOM.getNodeForLocation": {
          if (opts.hitTestBlind) return {};
          const f = fieldAt(params.x, params.y);
          if (!f) return {};
          if (overlayAt(f, params.x, params.y)) return { backendNodeId: 999, frameId: "MAIN" };
          return { backendNodeId: 100 + allFields().indexOf(f), frameId: "MAIN" };
        }
        case "DOM.resolveNode": return { object: { objectId: `o${params.backendNodeId}` } };
        case "Runtime.callFunctionOn": {
          allExpressions.push(params.functionDeclaration);
          const id = Number(String(params.objectId).slice(1));
          const f = allFields()[id - 100];
          const arg = params.arguments[0].value;
          if (params.functionDeclaration === ISO_HIT_IN_BOX) {
            const b = f?.box; // the overlay (999) owns no field's box
            return { result: { value: !!b && Math.abs(b.x - arg.x) <= 1.5 && Math.abs(b.y - arg.y) <= 1.5
              && Math.abs(b.width - arg.width) <= 1.5 && Math.abs(b.height - arg.height) <= 1.5 } };
          }
          if (params.functionDeclaration === ISO_LISTBOX_ROW) return { result: { value: f ? listboxState(f, arg.i) : null } };
          if (params.functionDeclaration === TAB_DISTANCE) {
            // lane D: the Tab order is the fields' order; text = an input of a text type or a textarea
            if (!f) return { result: { value: null } };
            const order = allFields();
            const text = (g: Field | null) => !!g && (g.tag === "textarea" || (g.tag === "input" && ["text", "search", ""].includes(g.type)));
            const ai = focus ? order.indexOf(focus) : -1;
            return { result: { value: { dist: ai >= 0 ? order.indexOf(f) - ai : null, fromText: text(focus), focused: focus === f,
              activatable: f.tag === "button" || f.tag === "select" || f.type === "checkbox", text: text(f), tag: f.tag,
              type: f.type, n: order.length } } };
          }
          if (params.functionDeclaration !== ISO_DESCRIBE) return { exceptionDetails: { text: "unknown call" } };
          return { result: { value: f ? describeInfo(f, arg?.want ?? null) : null } };
        }
        case "Runtime.releaseObject": return {};
        case "Target.getTargetInfo": return { targetInfo: { targetId: "T1" } };
        case "Browser.dispatchPointerPath":
          engineCalls.push(params);
          if (opts.engine) return {};
          throw new Error("engine path unavailable here");
        default: throw new Error(`unexpected CDP ${method}`);
      }
    },
  };

  const natives: { m: string; args: any[] }[] = [];
  const nativeFn = (m: string) => async (...args: any[]) => {
    natives.push({ m, args });
    if (opts.nativeThrows?.[m]) throw opts.nativeThrows[m];
    const f = fieldFor(args[0]);
    if (m === "fill" && f) {
      if (f.tag === "input" && f.type === "checkbox") throw new Error('Input of type "checkbox" cannot be filled');
      if (!args[2]?.trial) f.value = args[1];
    }
    return `native-${m}`;
  };

  const page: any = {
    mouse, keyboard,
    click: nativeFn("click"), hover: nativeFn("hover"), dblclick: nativeFn("dblclick"), fill: nativeFn("fill"),
    press: nativeFn("press"), type: nativeFn("type"), selectOption: nativeFn("selectOption"),
    check: nativeFn("check"), uncheck: nativeFn("uncheck"), setChecked: nativeFn("setChecked"),
    // Playwright's focus (what a press on an activatable target out of Tab reach falls back to): recorded
    focus: async (sel: string, ...args: any[]) => { natives.push({ m: "focus", args: [sel, ...args] }); focus = fields[sel] ?? single; },
    on: () => undefined, mainFrame: () => MAIN_FRAME, waitForTimeout: async () => undefined,
    viewportSize: () => ({ width: 1280, height: 800 }),
    evaluate: () => { throw new Error("humanize must not evaluate in the page's world"); },
    context: () => ({ newCDPSession: async () => cdp, browser: () => null }),
    locator: (selector: string) => {
      if (selector === ":focus") return FOCUS_QUERY;
      const f = fieldFor(selector);
      const loc: any = Object.create(locatorProto);
      Object.assign(loc, {
        _selector: selector, _frame: MAIN_FRAME, page: () => page,
        first: () => loc,
        waitFor: async (o: any = {}) => {
          handles++;
          waits.push({ m: "waitFor", timeout: o.timeout });
          waited = true;
          if (opts.waitForError) throw opts.waitForError;
          if (f && f.visible === false) throw new Error("waited for a hidden element");
        },
        scrollIntoViewIfNeeded: async (o: any = {}) => { handles++; waits.push({ m: "scroll", timeout: o.timeout }); },
        // a missing element (waitForError) is not visible; appearAfterWait: visible once waited for
        isVisible: async () => !opts.waitForError && (!f || (f.visible !== false && (!f.appearAfterWait || waited))),
        isEnabled: async () => opts.enabled ?? true,
        boundingBox: async (o: any = {}) => {
          handles++;
          if (o.timeout !== undefined) waits.push({ m: "box", timeout: o.timeout });
          return f ? { ...f.box } : null;
        },
        inputValue: async () => f?.value ?? "",
        getAttribute: async (n: string) => (n === "type" ? f?.type ?? null : null),
        count: async () => opts.count ?? 1,
        and: (other: any) => ({ count: async () => (other === FOCUS_QUERY && focus === f ? 1 : 0) }),
        isChecked: async (o: any = {}) => {
          waits.push({ m: "isChecked", timeout: o.timeout });
          if (opts.waitForError) throw opts.waitForError;
          return !!f?.checked;
        },
        locator: (s: string) => ({ count: async () => (f && s === `xpath=self::${f.tag}` ? 1 : 0) }),
        evaluate: () => { throw new Error("humanize must not evaluate in the page's world"); },
      });
      return loc;
    },
  };
  return {
    page, events, natives, engineCalls, waits, box: defBox, handles: () => handles,
    field: single as Field, fields,
    setFocused: (v: boolean | string) => { focus = v === false ? null : typeof v === "string" ? fields[v] : single; },
    of: (t: string) => events.filter((e) => e.t === t),
  };
}

type Fake = ReturnType<typeof makeFake>;
const attached = async (o: FakeOpts = {}): Promise<Fake> => {
  const f = makeFake(o);
  await attachHumanize({} as never, f.page, { humanize: true, seed: "forms-unit" });
  return f;
};

const ENV = "CLEARCOTE_HUMANIZE_TYPO_RATE";
const savedRate = process.env[ENV];
afterEach(() => {
  if (savedRate === undefined) delete process.env[ENV]; else process.env[ENV] = savedRate;
  protoCalls.length = 0;
});

// ------------------------------------------------------------------------------- mouse.click
describe("mouse.click / dblclick options", () => {
  it("a plain left click takes the engine path; any option presses SDK-side as asked", async () => {
    const f = await attached({ engine: true });
    await f.page.mouse.click(300, 200);
    expect(f.engineCalls.length).toBe(1);
    expect(f.of("down")).toEqual([]);
    await f.page.mouse.click(300, 200, { button: "right" });
    expect(f.engineCalls.length).toBe(1);
    expect(f.of("down").map((e) => e.a)).toEqual([{ button: "right", clickCount: 1 }]);
    expect(f.of("up").map((e) => e.a)).toEqual([{ button: "right", clickCount: 1 }]);
  }, SLOW);

  it("clickCount 2 is two presses with a human gap and persona holds", async () => {
    const f = await attached();
    await f.page.mouse.click(300, 200, { clickCount: 2 });
    const downs = f.of("down"), ups = f.of("up");
    expect(downs.map((e) => e.a)).toEqual([{ button: "left", clickCount: 1 }, { button: "left", clickCount: 2 }]);
    expect(ups.map((e) => e.a)).toEqual([{ button: "left", clickCount: 1 }, { button: "left", clickCount: 2 }]);
    for (let i = 0; i < 2; i++) expect(ups[i].ts - downs[i].ts).toBeGreaterThanOrEqual(40);
    const gap = downs[1].ts - ups[0].ts;
    expect(gap).toBeGreaterThanOrEqual(65);
    expect(gap).toBeLessThan(400);
  }, SLOW);

  it("delay sets the hold; delay 0 (Playwright's default) or a negative one gets the persona's hold", async () => {
    const f = await attached();
    await f.page.mouse.click(300, 200, { delay: 250, button: "right" });
    expect(f.of("up")[0].ts - f.of("down")[0].ts).toBeGreaterThanOrEqual(240);
    for (const delay of [0, -5]) {
      const g = await attached();
      await g.page.click("#btn", { delay });
      expect(g.of("up")[0].ts - g.of("down")[0].ts, `delay ${delay}`).toBeGreaterThanOrEqual(55);
    }
  }, SLOW);

  it("modifiers are held around the press with a lead and a lag; ControlOrMeta is passed through", async () => {
    const f = await attached();
    await f.page.mouse.click(300, 200, { modifiers: ["ControlOrMeta", "Shift", "Shift", "Bogus"] });
    const seq = f.events.filter((e) => ["kdown", "kup", "down", "up"].includes(e.t)).map((e) => [e.t, e.t.startsWith("k") ? e.a : e.a.button]);
    expect(seq).toEqual([["kdown", "ControlOrMeta"], ["kdown", "Shift"], ["down", "left"], ["up", "left"],
      ["kup", "Shift"], ["kup", "ControlOrMeta"]]);
    const kd = f.of("kdown"), ku = f.of("kup");
    expect(f.of("down")[0].ts - kd[1].ts).toBeGreaterThanOrEqual(70);   // 40-110 lead + 40-130 dwell
    expect(ku[0].ts - f.of("up")[0].ts).toBeGreaterThanOrEqual(15);     // 20-80 lag
  }, SLOW);

  it("mouse.dblclick is humanized: two presses, never the stock teleport", async () => {
    const f = await attached({ engine: true });
    await f.page.mouse.dblclick(150, 220);
    expect(f.of("stock-dblclick")).toEqual([]);
    expect(f.engineCalls).toEqual([]);
    expect(f.of("down").map((e) => e.a.clickCount)).toEqual([1, 2]);
    expect(f.of("move").length).toBeGreaterThan(0);
  }, SLOW);
});

// ------------------------------------------------------------------------------- page.click
describe("page.click / hover / dblclick", () => {
  it("trial performs no action and runs no Playwright trial (it moves the pointer and presses)", async () => {
    const f = await attached();
    await f.page.click("#btn", { trial: true });
    await f.page.hover("#btn", { trial: true });
    await f.page.dblclick("#btn", { trial: true });
    await f.page.click("#btn", { trial: true, position: { x: 5, y: 5 } });
    expect(f.natives).toEqual([]);
    expect(f.of("down")).toEqual([]);
    expect(f.of("move")).toEqual([]);
  }, SLOW);

  it("a trial whose checks never pass raises Playwright's TimeoutError, still with no action", async () => {
    const covered = await attached({ covered: true });
    await expect(covered.page.click("#btn", { trial: true, timeout: 300 })).rejects.toMatchObject({ name: "TimeoutError" });
    const disabled = await attached({ enabled: false });
    const err = await disabled.page.click("#btn", { trial: true, timeout: 300 }).catch((e: any) => e);
    expect(err).toMatchObject({ name: "TimeoutError" });
    expect(String(err.message)).toMatch(/300ms[\s\S]*not enabled/);
    await disabled.page.hover("#btn", { trial: true, timeout: 300 });   // hover does not need enabled
    for (const f of [covered, disabled]) {
      expect(f.natives).toEqual([]);
      expect(f.of("down")).toEqual([]);
      expect(f.of("move")).toEqual([]);
    }
  }, SLOW);

  it("a forced trial only needs the element", async () => {
    const f = await attached({ covered: true });
    await f.page.click("#btn", { trial: true, force: true, timeout: 300 });
    expect(f.natives).toEqual([]);
    expect(f.of("move")).toEqual([]);
  }, SLOW);

  it("a TimeoutError propagates and is never followed by a second, native wait", async () => {
    const f = await attached({ waitForError: timeoutError() });
    for (const act of [() => f.page.click("#nope", { timeout: 1500 }), () => f.page.fill("#nope", "x", { timeout: 1500 }),
      () => f.page.hover("#nope"), () => f.page.dblclick("#nope"), () => f.page.selectOption("#nope", "a"),
      () => f.page.check("#nope")]) {
      await expect(act()).rejects.toMatchObject({ name: "TimeoutError" });
    }
    expect(f.natives).toEqual([]);
    // type / press: a target that is not visible is no text field to click into, so Playwright's own
    // type / press runs straight away, and its single wait is the one that raises
    await f.page.press("#nope", "a");
    await f.page.type("#nope", "a");
    expect(f.natives.map((n) => n.m)).toEqual(["press", "type"]);
    expect(isTimeout(timeoutError())).toBe(true);
    expect(isTimeout(new Error("strict mode violation"))).toBe(false);
  }, SLOW);

  it("the caller's timeout is passed through unchanged; none means Playwright's page default", async () => {
    const timeoutsOf = async (run: (f: Fake) => Promise<unknown>) => {
      const f = await attached({ field: { type: "checkbox", appearAfterWait: true } });
      await run(f).catch(() => undefined);
      return f.waits.filter((w) => w.m !== "box").map((w) => w.timeout);
    };
    expect(await timeoutsOf((f) => f.page.click("#btn"))).toEqual([undefined]);
    expect(await timeoutsOf((f) => f.page.click("#btn", { timeout: 1234 }))).toEqual([1234]);
    expect(await timeoutsOf((f) => f.page.fill("#f", "v", { timeout: 777 }))).toEqual([777]);
    expect((await timeoutsOf((f) => f.page.check("#cb", { timeout: 555 })))[0]).toBe(555);
  }, SLOW);

  it("position is measured from the padding box; the press lands there, with no Playwright trial", async () => {
    const f = await attached();
    await f.page.click("#btn", { position: { x: 10, y: 12 } });
    const lastMove = f.of("move").at(-1)!.a;
    expect(lastMove.x).toBeCloseTo(100 + 2 + 10, 5);
    expect(lastMove.y).toBeCloseTo(200 + 3 + 12, 5);
    expect(f.natives).toEqual([]);
    expect(f.of("down").length).toBe(1);
  }, SLOW);

  it("the covered-point check is the hit test, not a Playwright trial: a covered target goes native", async () => {
    const free = await attached();
    await free.page.click("#btn");
    expect(free.natives).toEqual([]);       // no trial, which moved the real pointer and pressed
    expect(free.of("down").length).toBe(1);
    const covered = await attached({ covered: true });
    const t0 = Date.now();
    await covered.page.click("#btn");
    expect(Date.now() - t0).toBeGreaterThanOrEqual(350);   // an overlay is looked at again for 400 ms
    expect(covered.natives).toEqual([{ m: "click", args: ["#btn", {}] }]);
    expect(covered.of("down")).toEqual([]);
  }, SLOW);

  it("a point the hit test cannot answer goes native, with no Playwright trial first", async () => {
    // (a point inside a child frame is answered by its <iframe> element: IsolatedWorld.nodeAt)
    const f = await attached({ hitTestBlind: true });
    await f.page.click("#btn");
    expect(f.natives).toEqual([{ m: "click", args: ["#btn", {}] }]);
    expect(f.of("down")).toEqual([]);
  }, SLOW);

  it("a CSS selector in view needs no main-world handle; any other selector one boundingBox", async () => {
    process.env[ENV] = "0";
    const f = await attached();
    await f.page.click("#btn");
    await f.page.fill("#bio", "x");
    await f.page.hover("#btn");
    expect(f.handles()).toBe(0);
    const g = await attached();
    await g.page.click('internal:role=button[name="Target"i]');
    expect(g.handles()).toBe(1);
  }, SLOW);

  it("force skips the hit test and the enabled check", async () => {
    const f = await attached({ enabled: false });
    await f.page.click("#btn", { force: true, button: "middle" });
    expect(f.natives).toEqual([]);
    expect(f.of("down").map((e) => e.a.button)).toEqual(["middle"]);
  }, SLOW);

  it("force on a hidden target goes native at once: no wait Playwright would not make", async () => {
    for (const run of [
      (f: Fake) => f.page.click("#h", { force: true, timeout: 3000 }),
      (f: Fake) => f.page.fill("#h", "2026-10-11", { force: true, timeout: 3000 }),
      (f: Fake) => f.page.selectOption("#h", "x", { force: true, timeout: 3000 }),
    ]) {
      const f = await attached({ field: { visible: false } });
      await run(f);
      expect(f.waits.filter((w) => w.m === "waitFor")).toEqual([]);
      expect(f.natives.length).toBe(1);
      expect(f.natives[0].args.at(-1)).toMatchObject({ force: true });
      expect(f.of("down")).toEqual([]);
    }
  }, SLOW);

  it("a disabled element goes to native (which waits for it), with the caller's options", async () => {
    const f = await attached({ enabled: false });
    await f.page.click("#btn", { button: "right", timeout: 5 });
    expect(f.natives).toEqual([{ m: "click", args: ["#btn", { button: "right", timeout: 5 }] }]);
  }, SLOW);

  it("page.dblclick is two humanized presses and nothing native (not even a trial)", async () => {
    const f = await attached();
    await f.page.dblclick("#btn");
    expect(f.of("down").map((e) => e.a.clickCount)).toEqual([1, 2]);
    expect(f.natives).toEqual([]);
  }, SLOW);

  it("hover with modifiers holds them across the move and presses nothing", async () => {
    const f = await attached();
    await f.page.hover("#btn", { modifiers: ["Shift"] });
    expect(f.of("down")).toEqual([]);
    const kd = f.of("kdown")[0], ku = f.of("kup")[0];
    expect(kd.a).toBe("Shift");
    const moves = f.of("move");
    expect(moves[0].ts).toBeGreaterThan(kd.ts);
    expect(ku.ts).toBeGreaterThan(moves.at(-1)!.ts);
  }, SLOW);
});

// ------------------------------------------------------------------------------- fill / type
describe("fill: text", () => {
  it("clears a prefilled field with a held ControlOrMeta, a held A and a held Backspace", async () => {
    process.env[ENV] = "0";
    const f = await attached({ field: { value: "old value here" } });
    await f.page.fill("#prefilled", "new");
    expect(f.field.value).toBe("new");
    const kd = f.of("kdown")[0], ku = f.of("kup")[0];
    const a = f.of("press").find((e) => e.a.k === "a")!;
    expect(kd.a).toBe("ControlOrMeta");
    expect(ku.a).toBe("ControlOrMeta");
    expect(a.ts - kd.ts, "the modifier leads the A").toBeGreaterThanOrEqual(40);
    expect(ku.ts - a.ts, "and comes up just after it").toBeGreaterThanOrEqual(20);
    const keys = f.of("press").map((e) => e.a.k);
    expect(keys.slice(0, 2)).toEqual(["a", "Backspace"]);
    for (const p of f.of("press")) expect(p.a.delay).toBeGreaterThanOrEqual(45);
    expect(keys).not.toContain("Control+a");
    expect(keys).not.toContain("ControlOrMeta+a");
    expect(f.natives).toEqual([]);
  }, SLOW);

  it("an empty field gets no clear keys at all", async () => {
    process.env[ENV] = "0";
    const f = await attached();
    await f.page.fill("#bio", "hello");
    expect(f.field.value).toBe("hello");
    expect(f.of("press").map((e) => e.a.k).filter((k) => k.length > 1)).toEqual([]);
    expect(f.of("kdown").filter((e) => e.a === "ControlOrMeta")).toEqual([]);
  }, SLOW);

  it("a field that will not clear by select-all is emptied from the end, one held Backspace per character", async () => {
    process.env[ENV] = "0";
    const f = await attached({ field: { value: "abc", noSelectAll: true } });
    await f.page.fill("#f", "xyz");
    expect(f.field.value).toBe("xyz");
    const keys = f.of("press").map((e) => e.a.k);
    expect(keys.slice(0, 5)).toEqual(["a", "Backspace", "End", "Backspace", "Backspace"]);
    for (const e of f.of("press")) expect(e.a.delay).toBeGreaterThanOrEqual(45);
  }, SLOW);

  it("the value is verified; a field that never takes it goes to Playwright", async () => {
    process.env[ENV] = "0";
    const f = await attached({ field: { accept: () => false } });
    await f.page.fill("#f", "abc");
    expect(f.natives.map((n) => n.m)).toEqual(["fill"]);
    expect(f.natives[0].args.slice(0, 2)).toEqual(["#f", "abc"]);
  }, SLOW);

  it("a reformatting mask is accepted as typed: the digits go in once, never a second time", async () => {
    process.env[ENV] = "0";
    const phone = (v: string) => {
      const d = v.replace(/\D/g, "").slice(0, 10);
      return d.length > 6 ? `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}` : d.length > 3 ? `(${d.slice(0, 3)}) ${d.slice(3)}` : d;
    };
    const f = await attached({ field: { type: "tel", format: phone } });
    await f.page.fill("#phone", "5551234567");
    expect(f.field.value).toBe("(555) 123-4567");
    const digits = [...f.of("press").map((e) => e.a.k), ...f.of("kdown").map((e) => e.a)].filter((k) => /^\d$/.test(k));
    expect(digits.length).toBe(10);
    expect(f.natives).toEqual([]);
  }, SLOW);

  // SC 210 fix 2 (verifier B): a mask that re-punctuates and then stops taking keys was typed, wiped
  // with Ctrl+A Backspace, typed again and handed to Playwright -- for the same visible value.
  const expiry = (v: string) => { const d = v.replace(/\D/g, "").slice(0, 4); return d.length > 2 ? `${d.slice(0, 2)}/${d.slice(2)}` : d; };
  const telDashes = (v: string) => { const d = v.replace(/\D/g, "").slice(0, 10); return [d.slice(0, 3), d.slice(3, 6), d.slice(6)].filter(Boolean).join("-"); };
  const postcode = (v: string) => { const d = v.toUpperCase().replace(/[^0-9A-Z]/g, "").slice(0, 6); return d.length > 4 ? `${d.slice(0, 4)} ${d.slice(4)}` : d; };
  for (const [name, format, maxlength, value, want, keys] of [
    ["MM/YY on maxlength 5", expiry, 5, "123456", "12/34", "12345"],
    ["3-3-4 dashes on maxlength 12", telDashes, 12, "5551234567890", "555-123-4567", "555123456789"],
    ["a script that formats and cuts, no maxlength", postcode, undefined, "1234abx", "1234 AB", "1234abx"],
  ] as const) {
    it(`a mask that stops taking keys is typed once: ${name}`, async () => {
      process.env[ENV] = "0";
      const f = await attached({ field: { format, maxlength } });
      await f.page.fill("#m", value);
      expect(f.field.value).toBe(want);
      expect(f.natives).toEqual([]);
      const typed: string[] = f.events.filter((e) => e.t === "press" || e.t === "kdown").map((e) => (e.t === "press" ? e.a.k : e.a));
      expect(typed.filter((k) => k.length === 1).join("")).toBe(keys);
      expect(typed.filter((k) => ["ControlOrMeta", "ControlOrMeta+a", "Backspace"].includes(k)), "wiped and retyped").toEqual([]);
    }, SLOW);
  }

  // SC 210 round 3: maskKept read a change of case or a stripped separator as a mask that cuts, and
  // accepted a value missing its last key ("abc12" for "ABC126"). Only punctuation the page ADDED marks
  // such a mask; here the lost key is noticed and the value gets its one clean retry.
  const alnumOnly = (v: string) => v.replace(/[^0-9A-Za-z]/g, "");
  for (const [name, format, value, want, lost] of [
    ["a field that lower-cases", (v: string) => v.toLowerCase(), "ABC126", "abc126", "6"],
    ["a field that strips separators", alnumOnly, "555-123-4567", "5551234567", "7"],
    ["a plain field (as before)", undefined, "123456", "123456", "6"],
  ] as const) {
    it(`a lost key is typed again: ${name}`, async () => {
      process.env[ENV] = "0";
      let dropped = 0;
      const accept = (ch: string) => { if (ch === lost && !dropped) { dropped = 1; return false; } return true; };
      const f = await attached({ field: { format, accept } });
      await f.page.fill("#m", value);
      expect(dropped).toBe(1);
      expect(f.field.value).toBe(want);
      expect(f.natives).toEqual([]);
      const typed: string[] = f.events.filter((e) => e.t === "press" || e.t === "kdown").map((e) => (e.t === "press" ? e.a.k : e.a));
      expect(typed.filter((k) => ["ControlOrMeta", "ControlOrMeta+a"].includes(k)).length, "retyped once").toBeGreaterThan(0);
    }, SLOW);
  }

  it("a covered field is clicked where a person clicks; when the overlay keeps the caret, Playwright fills it", async () => {
    process.env[ENV] = "0";
    const f = await attached({ field: { value: "old" }, covered: true });
    await f.page.fill("#cov", "new");
    expect(f.field.value).toBe("new");
    expect(f.natives.map((n) => n.m)).toEqual(["fill"]);
    expect(f.of("overlay-click").length).toBe(1);   // one click, then the focus check said no
    expect(f.of("press")).toEqual([]);
    expect(f.of("stray")).toEqual([]);
  }, SLOW);

  it("a field a floating label covers completely is clicked through the label and TYPED", async () => {
    // No probe point reaches it, so the hit test proves nothing; the label passes the focus on, as on
    // #float. Requiring the hit test sent this to Playwright's insertText (found by the .NET port).
    process.env[ENV] = "0";
    const f = await attached({ field: { value: "" }, covered: true, labelPassesFocus: true });
    await f.page.fill("#float", "abc");
    expect(f.field.value).toBe("abc");
    expect(f.natives).toEqual([]);
    expect(f.of("overlay-click").length).toBe(1);
    expect(f.of("press").map((e) => e.a.k).filter((k: string) => k.length === 1).join("")
      + f.of("kdown").map((e) => e.a).join("")).toMatch(/^[abc]{3}$/);
  }, SLOW);

  it("a field the hit test cannot see (an iframe) is clicked, cleared and verified through the locator", async () => {
    process.env[ENV] = "0";
    const f = await attached({ field: { value: "old" }, hitTestBlind: true });
    await f.page.fill("#fi", "new");
    expect(f.field.value).toBe("new");   // "oldnew" once: nothing read, nothing cleared, nothing checked
    expect(f.natives).toEqual([]);
  }, SLOW);

  it("a target that moved since its box was read is not clicked blind", async () => {
    const f = await attached({ field: { value: "" }, covered: true, labelPassesFocus: true });
    const loc = f.page.locator("internal:label=\"x\"i");   // not CSS: the fresh box is boundingBox()
    const realBox = loc.boundingBox;
    let n = 0;
    // boxOf reads it twice (covered: no hit-test proof, so a fresh read after the scroll); then it moves
    loc.boundingBox = async () => { n++; const b = await realBox(); return n <= 2 ? b : { ...b, y: b.y + 30 }; };
    f.page.locator = ((orig) => (s: string) => (s === "internal:label=\"x\"i" ? loc : orig(s)))(f.page.locator);
    await f.page.fill("internal:label=\"x\"i", "abc");
    expect(f.of("overlay-click")).toEqual([]);
    expect(f.natives.map((n2) => n2.m)).toEqual(["fill"]);
  }, SLOW);

  it("color and unfillable types go to Playwright (which raises for a checkbox)", async () => {
    const color = await attached({ field: { type: "color", value: "#000000" } });
    await color.page.fill("#col", "#ff0000");
    expect(color.natives.map((n) => n.m)).toEqual(["fill"]);
    expect(color.of("press")).toEqual([]);
    const cb = await attached({ field: { type: "checkbox" } });
    await expect(cb.page.fill("#cb", "x")).rejects.toThrow(/checkbox/);
    expect(cb.of("press")).toEqual([]);
  }, SLOW);

  it("a value over 200 characters is clicked into, then inserted at once (no Playwright fill)", async () => {
    const f = await attached({ field: { value: "old" } });
    await f.page.fill("#f", "x".repeat(201));
    expect(f.natives).toEqual([]);
    expect(f.field.value).toBe("x".repeat(201));
    const seq = f.events.map((e) => e.t).filter((t) => ["move", "down", "up", "insert"].includes(t));
    expect(seq.indexOf("down")).toBeGreaterThan(seq.indexOf("move"));
    expect(seq.indexOf("down")).toBeLessThan(seq.indexOf("insert"));
    expect(f.of("insert").map((e) => e.a)).toEqual(["x".repeat(201)]);
    expect(f.of("press").filter((e) => e.a.k === "x")).toEqual([]);
  }, SLOW);

  it("a bulk value respects maxlength and never falls back", async () => {
    const f = await attached({ field: { maxlength: 250 } });
    await f.page.fill("#f", "q".repeat(300));
    expect(f.natives).toEqual([]);
    expect(f.field.value).toBe("q".repeat(250));
    expect(f.of("insert").map((e) => e.a)).toEqual(["q".repeat(250)]);
  }, SLOW);

  it("maxlength: only what fits is typed, no wipe and retype, no Playwright fill", async () => {
    // r38: fill('1234567') on maxlength=5 typed seven, saw 12345, wiped it, typed seven again and then
    // handed the field to Playwright. Playwright's own fill leaves 12345 too.
    process.env[ENV] = "0";
    const f = await attached({ field: { maxlength: 5 } });
    await f.page.fill("#zip", "1234567");
    expect(f.field.value).toBe("12345");
    expect(f.natives).toEqual([]);
    const typed = [...f.of("press").map((e) => e.a.k), ...f.of("kdown").map((e) => e.a)].filter((k) => /^[0-9]$/.test(k));
    expect(typed.sort()).toEqual(["1", "2", "3", "4", "5"]);
    expect(f.of("press").filter((e) => e.a.k === "Backspace")).toEqual([]);
    expect(f.of("insert")).toEqual([]);
  }, SLOW);

  it("every SDK press waits 40+ ms after the last pointer move (focus click, click, drag grab)", async () => {
    // r38: the focus click before typing pressed ~4 ms after the last pointermove, a drag grab ~21 ms.
    process.env[ENV] = "0";
    const gapBeforeDown = (f: Fake) => {
      const out: number[] = [];
      let last: number | null = null;
      for (const e of f.events) {
        if (e.t === "move") last = e.ts;
        else if (e.t === "down" && last !== null) out.push(e.ts - last);
      }
      return out;
    };
    const a = await attached();
    await a.page.fill("#f", "abc");
    const b = await attached({ field: { tag: "button", type: "" } });
    await b.page.click("#btn");
    const c = await attached();
    await c.page.mouse.move(150, 220);
    await c.page.mouse.down();
    await c.page.mouse.up();
    for (const [name, f] of [["fill", a], ["click", b], ["drag", c]] as const) {
      const gaps = gapBeforeDown(f);
      expect(gaps.length, name).toBeGreaterThan(0);
      for (const g of gaps) expect(g, name).toBeGreaterThanOrEqual(38);   // timer granularity
      expect(f.of("click"), name).toEqual([]);
    }
  }, SLOW);

  it("a bare mouse.down with no move before it stays instant", async () => {
    const f = await attached();
    const t0 = performance.now();
    await f.page.mouse.down();
    await f.page.mouse.up();
    expect(performance.now() - t0).toBeLessThan(30);
  }, SLOW);

  it("a range value Playwright would reject goes to Playwright (which raises), never a snapped one", async () => {
    for (const bad of ["13", "250", "nan", "abc"]) {
      const f = await attached({ field: { type: "range", value: "3", min: "3", max: "20", step: "4" } });
      await f.page.fill("#rng2", bad);
      expect(f.natives.map((n) => n.m), bad).toEqual(["fill"]);
      expect(f.of("down")).toEqual([]);
      expect(f.of("click")).toEqual([]);
    }
  }, SLOW);
});

describe("one input action at a time per page", () => {
  it("a Promise.all of two fills gives each field its own value", async () => {
    process.env[ENV] = "0";
    const f = await attached({ fields: { "#a": {}, "#b": { value: "prefilled" } } });
    await Promise.all([f.page.fill("#a", "alpha one"), f.page.fill("#b", "beta two"), f.page.click("#a")]);
    expect(f.fields["#a"].value).toBe("alpha one");
    expect(f.fields["#b"].value).toBe("beta two");
    expect(f.of("stray")).toEqual([]);
    expect(f.natives).toEqual([]);
  }, 60_000);

  it("createInputLock runs one at a time, in order, and a failure releases it", async () => {
    const lock = createInputLock();
    const log: string[] = [];
    const job = (name: string, ms: number, fail = false) => lock(async () => {
      log.push(`${name}+`);
      await new Promise((r) => setTimeout(r, ms));
      log.push(`${name}-`);
      if (fail) throw new Error(name);
      return name;
    });
    const results = await Promise.allSettled([job("a", 30), job("b", 5, true), job("c", 1)]);
    expect(log).toEqual(["a+", "a-", "b+", "b-", "c+", "c-"]);
    expect(results.map((r) => r.status)).toEqual(["fulfilled", "rejected", "fulfilled"]);
  });
});

describe("typos", () => {
  it("the rate comes from CLEARCOTE_HUMANIZE_TYPO_RATE at call time (default 0.02, 0 disables, clamped)", () => {
    delete process.env[ENV];
    expect(typoRate()).toBe(0.02);
    for (const [v, want] of [["0", 0], ["0.35", 0.35], [" 0.5 ", 0.5], ["5", 1], ["-1", 0], ["junk", 0.02], ["", 0.02],
      ["nan", 0.02], ["NaN", 0.02]] as const) {
      process.env[ENV] = v;
      expect(typoRate(), v).toBe(want);
    }
  });

  it("forced typos in a text field are corrected and the value comes out right", async () => {
    process.env[ENV] = "1";
    const f = await attached();
    await f.page.fill("#name", "hello world 2024");
    expect(f.field.value).toBe("hello world 2024");
    expect(f.of("press").some((e) => e.a.k === "Backspace")).toBe(true);
    for (const e of f.of("press")) expect(e.a.delay).toBeGreaterThanOrEqual(45);
  }, SLOW);

  it.each([
    ["password", {}], ["email", {}], ["number", {}], ["tel", {}], ["url", {}],
    ["text", { autocomplete: "one-time-code" }], ["text", { inputmode: "numeric" }],
  ] as const)("no slips where a slip breaks the field: %s %o", async (type, extra) => {
    process.env[ENV] = "1";
    const f = await attached({ field: { type, ...extra } });
    await f.page.fill("#f", "abc123");
    expect(f.field.value).toBe("abc123");
    expect(f.of("press").some((e) => e.a.k === "Backspace")).toBe(false);
  }, SLOW);

  it("keyboard.type into a focused password makes no slips (the focused field is read in the isolated world)", async () => {
    process.env[ENV] = "1";
    const f = await attached({ field: { type: "password" } });
    f.setFocused(true);
    await f.page.keyboard.type("abcdefgh");
    expect(f.field.value).toBe("abcdefgh");
    expect(f.of("press").some((e) => e.a.k === "Backspace")).toBe(false);
  }, SLOW);

  it("focus inside a frame fails closed: no slips where the field cannot be read", async () => {
    process.env[ENV] = "1";
    const f = await attached({ field: { inFrame: true } });
    f.setFocused(true);
    await f.page.keyboard.type("abcdef");
    expect(f.field.value).toBe("abcdef");
    expect(f.of("press").some((e) => e.a.k === "Backspace")).toBe(false);
  }, SLOW);

  it("mask-aware: a swallowed slip is not 'corrected' by deleting a real character", async () => {
    // The field accepts only a, b and c: every letter slip (a neighbour key) is swallowed, while a
    // doubled or swapped key lands. Without the before/after reads, each swallowed slip cost a real
    // character. keyboard.type has no verify-and-retry behind it, so the first pass must be right.
    process.env[ENV] = "1";
    for (let n = 0; n < 3; n++) {
      const f = await attached({ field: { accept: (c) => "abc".includes(c) } });
      f.setFocused(true);
      await f.page.keyboard.type("abcabc");
      expect(f.field.value).toBe("abcabc");
    }
  }, 60_000);

  it("a maxlength that swallows the doubled key costs no real digit", async () => {
    process.env[ENV] = "1";
    for (let n = 0; n < 3; n++) {
      const f = await attached({ field: { maxlength: 5 } });
      f.setFocused(true);
      await f.page.keyboard.type("12345");
      expect(f.field.value).toBe("12345");
    }
  }, 60_000);

  it("digits slip only to neighbouring digits", async () => {
    process.env[ENV] = "1";
    const f = await attached();
    f.setFocused(true);
    await f.page.keyboard.type("5555");
    const typed = f.of("press").map((e) => e.a.k).filter((k: string) => k.length === 1);
    expect(typed.length).toBeGreaterThan(4);   // at rate 1 every key slips
    for (const k of typed) expect(["4", "5", "6"]).toContain(k);
    expect(f.field.value).toBe("5555");
  }, SLOW);
});

// ------------------------------------------------------------------------------- page.type / press
describe("type / press", () => {
  it("a focused field (asked through :focus, any selector) is typed into without another click", async () => {
    process.env[ENV] = "0";
    const f = await attached();
    f.setFocused(true);
    await f.page.press('internal:role=textbox[name="City"i]', "Enter");
    await f.page.type('internal:label="City"i >> nth=0', "ab");
    expect(f.of("click")).toEqual([]);
    expect(f.of("down")).toEqual([]);
    expect(f.natives).toEqual([]);
    expect(f.of("press")[0].a).toMatchObject({ k: "Enter" });
    expect(f.of("press")[0].a.delay).toBeGreaterThanOrEqual(45);
    expect(f.field.value).toBe("ab");
  }, SLOW);

  it("an unfocused text field is clicked first; the caller's press delay wins, 0 gets the persona's", async () => {
    const f = await attached();
    await f.page.press("#f", "x", { delay: 222 });
    expect(f.of("down").length).toBe(1);
    expect(f.of("press").at(-1)!.a).toEqual({ k: "x", delay: 222 });
    await f.page.press("#f", "y", { delay: 0 });
    expect(f.of("press").at(-1)!.a.delay).toBeGreaterThanOrEqual(45);
    expect(f.of("down").length).toBe(1);   // already focused: no second click
  }, SLOW);

  it("a press on a button is never clicked first: the pointer goes to it, the focus is given, the key is held", async () => {
    // Lane D (SC 232): no Tab order in this fake, so the button is out of Tab reach -- Playwright's focus,
    // reported -- and the key is a HELD press; never a click (it activated the button twice) and never
    // Playwright's 0 ms focus-and-press.
    const f = await attached({ field: { tag: "button", type: "" } });
    const seen: any[] = [];
    (f.page as any)._clearcoteOnFallback = (e: any) => seen.push(e);
    await f.page.press("#counter", "Enter");
    expect(f.natives.map((n) => n.m)).toEqual(["focus"]);
    expect(f.of("click")).toEqual([]);
    expect(f.of("down")).toEqual([]);
    expect(f.of("press").map((e) => e.a.k)).toEqual(["Enter"]);
    expect(f.of("press")[0].a.delay).toBeGreaterThanOrEqual(45);
    expect(seen[0]?.api).toBe("page.press");
  }, SLOW);

  it("typing into a hidden helper textarea is Playwright's at once, with no wait", async () => {
    const f = await attached({ field: { tag: "textarea", type: "", visible: false } });
    await f.page.type("#helper", "abc", { timeout: 3000 });
    expect(f.natives.map((n) => n.m)).toEqual(["type"]);
    expect(f.waits.filter((w) => w.m === "waitFor")).toEqual([]);
  }, SLOW);

  it("keyboard.press: delay 0 is Playwright's default and gets the persona's hold; a real delay wins", async () => {
    const f = await attached();
    await f.page.keyboard.press("Enter", { delay: 0 });
    await f.page.keyboard.press("Tab");
    await f.page.keyboard.press("Escape", { delay: 300 });
    const [a, b, c] = f.of("press").map((e) => e.a.delay);
    expect(a).toBeGreaterThanOrEqual(45);
    expect(b).toBeGreaterThanOrEqual(45);
    expect(c).toBe(300);
  }, SLOW);

  it("no read anywhere in this file resolved a target by querySelector, nor ran in the page's world", () => {
    // (page.evaluate throws in every fake.) The one querySelectorAll is ISO_CSS_BOX: a fast path for a
    // plain-CSS selector, used only when Playwright counts the same elements, never the way a target
    // is resolved (the hit test is). ISO_HIT_IN_BOX (SC 210 fix 2) reads the caller's plain-CSS selector
    // only to tell whether the element the hit test found IS that element (its first match must still
    // fill the box, else the box rule answers), and asks whether a twin holds a control. TAB_DISTANCE
    // (lane D) walks the document's focusables for the Tab ORDER -- what a Tab press visits --, and reads
    // the caller's plain-CSS selector the same way HIT_IN_BOX does (it must still fill the box).
    expect(allExpressions.length).toBeGreaterThan(0);
    for (const e of allExpressions) {
      if (!e.startsWith(`(${ISO_CSS_BOX})`) && !e.includes(ISO_HIT_IN_BOX) && !e.includes(TAB_DISTANCE)) expect(e).not.toContain("querySelector");
    }
    expect(allExpressions.some((e) => e.startsWith(`(${ISO_CSS_BOX})`))).toBe(true);
  });
});

// ------------------------------------------------------------------------------- selectOption
const COUNTRIES = ["Afghanistan", "Albania", "Algeria", "Germany", "Ghana", "Greece", "Spain", "Sweden", "Switzerland"];
const countryField = (selected = 0): Partial<Field> => ({
  tag: "select", type: "", value: COUNTRIES[selected].slice(0, 3).toLowerCase(), selectedIndex: selected,
  options: COUNTRIES.map((c, i) => ({ value: c.slice(0, 3).toLowerCase(), label: c, selected: i === selected })),
});

describe("selectOption", () => {
  it("a drop-down is opened with a humanized click, type-ahead moves the highlight, Enter commits ONE change", async () => {
    const f = await attached({ field: countryField() });
    expect(await f.page.selectOption("#country", "Ghana")).toEqual(["gha"]);
    expect(f.field.value).toBe("gha");
    expect(f.of("change").length).toBe(1);
    expect(f.of("down").length).toBe(1);
    const keys = f.of("press").map((e) => e.a.k);
    expect(keys.at(-1)).toBe("Enter");
    expect(keys.slice(0, -1).join("")).toBe("gh");
    for (const e of f.of("press")) expect(e.a.delay).toBeGreaterThanOrEqual(45);
    expect(f.natives).toEqual([]);   // the opening click's covered check was the hit test, no trial
  }, SLOW);

  it("under an overlay over its centre, the drop-down is opened at the point the hit test proved", async () => {
    const f = await attached({ field: countryField(), coveredCentre: true });
    expect(await f.page.selectOption("#country", "Sweden")).toEqual(["swe"]);
    expect(f.of("change").length).toBe(1);
    expect(f.of("overlay-click")).toEqual([]);
    const down = f.of("move").at(-1)!.a;   // where the press went: outside the overlay's middle half
    expect(down.x < 100 + 160 * 0.25 || down.x > 100 + 160 * 0.75).toBe(true);
    expect(f.natives).toEqual([]);
  }, SLOW);

  it("an untypeable long list is crossed with a HELD arrow key: one keydown, auto-repeats, one keyup", async () => {
    const opts = Array.from({ length: 30 }, (_, i) => ({ value: `s${String(i).padStart(3, "0")}`, label: "Same", selected: i === 0 }));
    const f = await attached({ field: { tag: "select", type: "", value: "s000", selectedIndex: 0, options: opts } });
    expect(await f.page.selectOption("#same", "s020")).toEqual(["s020"]);
    expect(f.of("change").length).toBe(1);
    const downs = f.of("kdown").filter((e) => e.a === "ArrowDown");
    expect(downs.length).toBe(20);
    expect(f.of("kup").filter((e) => e.a === "ArrowDown").length).toBe(1);
    expect(f.of("press").filter((e) => e.a.k === "ArrowDown")).toEqual([]);
    expect(downs[1].ts - downs[0].ts, "the keyboard's repeat delay").toBeGreaterThanOrEqual(350);
    const repeats = downs.slice(2).map((d, k) => d.ts - downs[k + 1].ts);
    expect(repeats.reduce((a, b) => a + b, 0) / repeats.length).toBeLessThan(60);   // ~30 a second
    // a short run stays single presses
    const g = await attached({ field: { tag: "select", type: "", value: "s000", selectedIndex: 0, options: opts.map((o) => ({ ...o })) } });
    expect(await g.page.selectOption("#same", "s003")).toEqual(["s003"]);
    expect(g.of("press").filter((e) => e.a.k === "ArrowDown").length).toBe(3);
    expect(g.of("kdown").filter((e) => e.a === "ArrowDown")).toEqual([]);
  }, 60_000);

  it("index, label and value-only option objects resolve Playwright's way", async () => {
    for (const [values, want] of [[{ index: 5 }, "gre"], [{ label: "Sweden" }, "swe"], [{ value: "spa" }, "spa"],
      [["Algeria"], "alg"]] as const) {
      const f = await attached({ field: countryField() });
      expect(await f.page.selectOption("#country", values)).toEqual([want]);
      expect(f.of("change").length).toBe(1);
      expect(f.natives).toEqual([]);
    }
  }, 60_000);

  it("the option already selected is returned and nothing fires", async () => {
    const f = await attached({ field: countryField(3) });
    expect(await f.page.selectOption("#country", "ger")).toEqual(["ger"]);
    expect(f.events.filter((e) => e.t !== "move")).toEqual([]);
  }, SLOW);

  it("element handles, multi-field option objects and unknown options go to Playwright", async () => {
    for (const values of [{ value: "ger", label: "Germany" }, { asElement: () => null }, "Atlantis", null]) {
      const f = await attached({ field: countryField() });
      expect(await f.page.selectOption("#country", values as any)).toBe("native-selectOption");
      expect(f.of("change")).toEqual([]);
    }
  }, SLOW);

  it("a list box: the first row is a plain click, the rest are pressed with ControlOrMeta held", async () => {
    const opts = "abcdef".split("").map((c) => ({ value: c, label: `Item ${c}`, selected: false }));
    const f = await attached({
      field: { tag: "select", type: "", multiple: true, size: 6, selectedIndex: -1, value: "", options: opts,
        box: { x: 100, y: 200, width: 200, height: 120 } },
    });
    expect(await f.page.selectOption("#ms", ["b", "d", "e"])).toEqual(["b", "d", "e"]);
    expect(f.field.options!.filter((o) => o.selected).map((o) => o.value)).toEqual(["b", "d", "e"]);
    expect(f.of("click")).toEqual([]);   // a press, not mouse.click (which moves again right before it)
    expect(f.of("down").length).toBe(3);
    expect(f.of("kdown").map((e) => e.a)).toEqual(["ControlOrMeta", "ControlOrMeta"]);
    expect(f.of("wheel")).toEqual([]);
    expect(f.natives).toEqual([]);
  }, SLOW);

  it("a list-box row that needs a mid-list scroll: one notch to learn the step, then the rest, then the row", async () => {
    const opts = Array.from({ length: 25 }, (_, i) => ({ value: `m${String(i).padStart(2, "0")}`, label: `Multi ${i}`, selected: false }));
    for (const [want, most] of [["m14", 6], ["m10", 6], ["m24", 8]] as const) {
      const f = await attached({
        field: { tag: "select", type: "", size: 6, selectedIndex: -1, value: "", options: opts.map((o) => ({ ...o })),
          box: { x: 100, y: 200, width: 200, height: 120 } },
      });
      expect(await f.page.selectOption("#lbmid", want), want).toEqual([want]);
      const wheels = f.of("wheel");
      expect(wheels.length, want).toBeGreaterThan(0);
      expect(wheels.length, want).toBeLessThanOrEqual(most);
      expect(wheels.every((w) => w.a.dy > 0), "never wheeled the wrong way").toBe(true);
      expect(f.natives).toEqual([]);
      expect(f.field.options!.filter((o) => o.selected).map((o) => o.value)).toEqual([want]);
    }
  }, 60_000);

  it("selectWants / wantIndex: a string is value-or-label, an object names one field", () => {
    const opts = [{ value: "x", label: "Same" }, { value: "y", label: "Same" }, { value: "Same", label: "z" }];
    expect(selectWants("Same")).toEqual([{ kind: "value", v: "Same" }]);
    expect(wantIndex(opts, selectWants("Same")![0])).toBe(0);           // value OR label: first match
    expect(wantIndex(opts, selectWants({ value: "Same" })![0])).toBe(2); // value only
    expect(wantIndex(opts, selectWants({ label: "z" })![0])).toBe(2);
    expect(wantIndex(opts, selectWants({ index: 1 })![0])).toBe(1);
    expect(selectWants([])).toBeNull();
    expect(selectWants({ index: 1.5 })).toBeNull();
    expect(selectWants([{ value: "x" }, 3])).toBeNull();
  });
});

// ------------------------------------------------------------------------------- check
describe("check / uncheck / setChecked", () => {
  it("a humanized click only when the state must change, then verified", async () => {
    const f = await attached({ field: { type: "checkbox", checked: false } });
    await f.page.check("#cb");
    expect(f.field.checked).toBe(true);
    expect(f.of("down").length).toBe(1);
    await f.page.check("#cb");        // already checked: nothing happens
    expect(f.of("down").length).toBe(1);
    await f.page.setChecked("#cb", false);
    expect(f.field.checked).toBe(false);
    await f.page.uncheck("#cb");
    expect(f.of("down").length).toBe(2);
    expect(f.natives).toEqual([]); // no Playwright call at all, not even a trial
  }, SLOW);

  it("a checkbox that cancels its click is clicked ONCE, then raises like Playwright", async () => {
    const f = await attached({ field: { type: "checkbox", checked: false, stubborn: true } });
    await expect(f.page.check("#stubborn")).rejects.toThrow(/did not change its state/);
    expect(f.of("down").length).toBe(1);
    expect(f.natives).toEqual([]);
    // and through a locator: the error propagates, the original locator method is not run again
    await expect(f.page.locator("#stubborn").check()).rejects.toThrow(/did not change its state/);
    expect(f.of("down").length).toBe(2);
    expect(protoCalls).toEqual([]);
  }, SLOW);

  it("trial changes nothing and runs no Playwright trial", async () => {
    const f = await attached({ field: { type: "checkbox", checked: false } });
    await f.page.check("#cb", { trial: true });
    await f.page.setChecked("#cb", true, { trial: true });
    await f.page.locator("#cb").check({ trial: true });
    expect(f.field.checked).toBe(false);
    expect(f.natives).toEqual([]);
    expect(f.of("down")).toEqual([]);
    expect(f.of("move")).toEqual([]);
  }, SLOW);
});

// ------------------------------------------------------------------------------- Locator patch
describe("Locator patch", () => {
  it("stays STRICT: more than one match goes to the native method, which raises Playwright's error", async () => {
    const f = await attached({ count: 2 });
    expect(await f.page.locator("button.dup").click({ timeout: 2000 })).toBe("proto-click");
    expect(protoCalls.map((c) => c.m)).toEqual(["click"]);
    expect(f.natives).toEqual([]);
    expect(f.of("move")).toEqual([]);
  }, SLOW);

  it("frame locators stay native (their selector enters the frame)", async () => {
    const f = await attached();
    const loc = f.page.locator("#fr >> internal:control=enter-frame >> #fi");
    expect(await loc.fill("new")).toBe("proto-fill");
    expect(protoCalls.map((c) => c.m)).toEqual(["fill"]);
    expect(f.natives).toEqual([]);
  }, SLOW);

  it("a TimeoutError propagates from the humanized route, with no native retry", async () => {
    const f = await attached({ waitForError: timeoutError() });
    await expect(f.page.locator("#nope").click({ timeout: 1500 })).rejects.toMatchObject({ name: "TimeoutError" });
    await expect(f.page.locator("#nope").fill("x", { timeout: 1500 })).rejects.toMatchObject({ name: "TimeoutError" });
    expect(protoCalls).toEqual([]);
  }, SLOW);

  it("EVERY error propagates once routed: the page method already ran its native fallback", async () => {
    const boom = new Error("boom from native click");
    const f = await attached({ enabled: false, nativeThrows: { click: boom } });
    await expect(f.page.locator("#btn").click()).rejects.toBe(boom);
    expect(f.natives.map((n) => n.m)).toEqual(["click"]);   // once
    expect(protoCalls).toEqual([]);                        // and not the locator method on top
  }, SLOW);

  it("forwards every option the page method understands (and only those)", async () => {
    const f = await attached({ enabled: false });   // a disabled target: the native click gets the options
    await f.page.locator("#btn").click({ button: "right", clickCount: 1, noWaitAfter: true, timeout: 1234, bogus: 1 });
    expect(f.natives).toEqual([{ m: "click", args: ["#btn", { button: "right", clickCount: 1, noWaitAfter: true, timeout: 1234 }] }]);
    expect(pickOptions({ delay: 5, timeout: null, noWaitAfter: true }, ["delay", "timeout", "noWaitAfter"]))
      .toEqual({ delay: 5, noWaitAfter: true });
  }, SLOW);

  it("check / setChecked / selectOption / tap route through the humanized page methods", async () => {
    const f = await attached({ field: { type: "checkbox", checked: false } });
    await f.page.locator("#cb").check();
    expect(f.field.checked).toBe(true);
    await f.page.locator("#cb").setChecked(false);
    expect(f.field.checked).toBe(false);
    await f.page.locator("#cb").tap({ trial: true });
    expect(protoCalls).toEqual([]);
    const s = await attached({ field: countryField() });
    expect(await s.page.locator("#country").selectOption("Germany")).toEqual(["ger"]);
    expect(s.of("change").length).toBe(1);
  }, SLOW);
});

describe("Playwright's error wording (SC 210 fix 2)", () => {
  it("an error is worded as the API the caller used, class kept", () => {
    const e = new pwErrors.TimeoutError("locator.waitFor: Timeout 800ms exceeded.\nCall log:\n  - waiting for locator('#x')");
    const n = asApiError(e, "page.click");
    expect(n).toBe(e);
    expect(n).toBeInstanceOf(pwErrors.TimeoutError);
    expect(n.message.split("\n")[0]).toBe("page.click: Timeout 800ms exceeded.");
    expect(n.message.endsWith("waiting for locator('#x')")).toBe(true);
    expect(asApiError(sdkPlaywrightError("Clicking the checkbox did not change its state"), "locator.check").message)
      .toBe("locator.check: Clicking the checkbox did not change its state");
    const plain = new TypeError("cannot read x");
    expect(asApiError(plain, "page.click").message).toBe("cannot read x");
  });

  it("a humanized call rejects with the caller's API name", async () => {
    const f = apiErrors("page.check", async () => { throw new pwErrors.TimeoutError("locator.isChecked: Timeout 500ms exceeded."); });
    await expect(f()).rejects.toThrow(/^page\.check: Timeout 500ms exceeded\.$/);
  });
});

describe("paths stay in the viewport; whole-pixel presses (SC 210 fix 2)", () => {
  it("glides near the edges stay inside the viewport", async () => {
    // The planner's overshoot arcs left the window near its edges (verifier B). This fake's viewport is 1280 x 800.
    const f = await attached();
    const pts: [number, number][] = [[1276, 400], [640, 3], [4, 790], [1277, 797], [100, 2], [1270, 6], [3, 3]];
    for (let r = 0; r < 3; r++) for (const [x, y] of pts) await f.page.mouse.move(x, y);
    const out = f.of("move").filter((e) => !(e.a.x >= 0 && e.a.x <= 1280 && e.a.y >= 0 && e.a.y <= 800));
    expect(out.length, JSON.stringify(out.slice(0, 3))).toBe(0);
  }, 60_000);

  it("a humanized click presses the whole pixel the hit test proved", async () => {
    // DOM.getNodeForLocation answers for a whole pixel: a press at 971.6 was proven at 972 and landed on a
    // badge next to the target (verifier B).
    const f = await attached();
    for (let i = 0; i < 6; i++) {
      await f.page.click("#btn");
      const m = f.of("move");
      const last = m[m.length - 1].a;
      expect(Number.isInteger(last.x) && Number.isInteger(last.y), JSON.stringify(last)).toBe(true);
    }
  }, 60_000);
});

// ------------------------------------------------------------------- lane D (SC 232): Tab and focus
describe("lane D: Tab between fields, presses on buttons, focus / blur / selectText", () => {
  const seeded = async (seed: string, o: FakeOpts) => {
    const f = makeFake(o);
    await attachHumanize({} as never, f.page, { humanize: true, seed });
    return f;
  };
  // fake timers: every planned sleep resolves at once, the order of events is what is checked
  const drive = async <T>(p: Promise<T>): Promise<T> => { await vi.runAllTimersAsync(); return p; };
  const three = () => ({ fields: { "#a": {}, "#b": {}, "#c": {} } });

  it("fill Tabs from field to adjacent field in the persona's share, focus verified", async () => {
    process.env[ENV] = "0";
    vi.useFakeTimers({ toFake: ["setTimeout", "Date"] });
    try {
      const out: Record<string, [number, number]> = {};
      for (const seed of ["ha-1", "ha-2", "ha-4", "ha-5"]) {
        let tabs = 0, n = 0;
        for (let k = 0; k < 25; k++) {
          const f = await seeded(seed, three());
          for (const [sel, v] of [["#a", "alpha"], ["#b", "bravo"], ["#c", "charlie"]] as const) {
            const before = f.events.length;
            await drive(f.page.fill(sel, v));
            if (sel !== "#a") { n++; if (f.events.slice(before).some((e) => e.t === "press" && e.a.k === "Tab")) tabs++; }
          }
          expect(f.natives).toEqual([]);
        }
        const want = typingTraits(makePersona(seed).seed).tabPref;
        out[seed] = [tabs / n, want];
        expect(Math.abs(tabs / n - want), JSON.stringify(out)).toBeLessThanOrEqual(0.2);
      }
    } finally { vi.useRealTimers(); }
  }, 120_000);

  it("a press on a button within Tab reach Tabs to it: no click, no native", async () => {
    process.env[ENV] = "0";
    vi.useFakeTimers({ toFake: ["setTimeout", "Date"] });
    try {
      const f = await seeded("ha-1", { fields: { "#q": {}, "#go": { tag: "button", type: "" } } });
      await drive(f.page.fill("#q", "hello"));
      const downs = f.of("down").length;
      await drive(f.page.press("#go", "Enter"));
      expect(f.natives).toEqual([]);
      expect(f.of("down").length).toBe(downs);
      expect(f.of("press").map((e) => e.a.k).slice(-2)).toEqual(["Tab", "Enter"]);
    } finally { vi.useRealTimers(); }
  }, 60_000);

  it("focus / blur / selectText go the humane way, with nothing native", async () => {
    process.env[ENV] = "0";
    vi.useFakeTimers({ toFake: ["setTimeout", "Date"] });
    try {
      for (const seed of ["ha-1", "ha-2", "ha-4", "ha-5"]) {   // blur click/tab, select triple/keys
        const f = await seeded(seed, three());
        await drive(f.page.fill("#a", "some text"));
        await drive(f.page.focus("#b"));
        await drive(f.page.focus("#a"));
        await drive((f.page as any)._clearcoteSelectText("#a"));
        expect(f.of("press").some((e) => e.a.k === "Home") || f.of("up").some((e) => e.a.clickCount === 3), seed).toBe(true);
        await drive((f.page as any)._clearcoteBlur("#a"));
        expect(f.natives, seed).toEqual([]);
      }
    } finally { vi.useRealTimers(); }
  }, 60_000);

  it("page.type keeps the caller's delay as a floor; page.press repeat holds the key", async () => {
    process.env[ENV] = "0";
    const f = await seeded("ha-2", { field: {} });
    const t0 = Date.now();
    await f.page.type("#f", "abcd", { delay: 300 });
    expect(Date.now() - t0).toBeGreaterThanOrEqual(900);
    expect(f.field.value).toBe("abcd");
    await f.page.press("#f", "ArrowRight", { repeat: 6 });
    expect(f.of("kdown").filter((e) => e.a === "ArrowRight").length).toBe(6);
    expect(f.of("kup").filter((e) => e.a === "ArrowRight").length).toBe(1);
  }, SLOW);
});

