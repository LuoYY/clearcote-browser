// The fake page every humanize unit test shares (SC 232 integration): humanize-forms.test.ts (lanes B-F),
// humanize-options.test.ts (lane A) and humanize-trace-taps.test.ts. It was duplicated here from the forms test by
// lane A and drifted (lanes D/E/F extended the forms copy); now the forms test imports this one. No vitest imports:
// the test files own their mocks (humanize-forms.test.ts mocks ../src/motion.js, which reaches this module's
// attachHumanize too).
import { attachHumanize } from "../../src/humanize.js";
import * as ISO from "../../src/isolated.js";
import {
  ISO_ACTIVE_FIELD, ISO_ACTIVE_VALUE, ISO_CSS_BOX, ISO_DESCRIBE, ISO_HIT_IN_BOX, ISO_LISTBOX_ROW, ISO_PLATFORM, ISO_SCROLL, ISO_SCROLL_BY, ISO_VIEWPORT,
} from "../../src/isolated.js";
// lane F names, read through the module so this file still loads against an SDK without them
const ISO_LABEL_OF: string = (ISO as any).ISO_LABEL_OF ?? "<no ISO_LABEL_OF>";
const ISO_LABEL_BY_SEL: string = (ISO as any).ISO_LABEL_BY_SEL ?? "<no ISO_LABEL_BY_SEL>";
import { typeaheadSimulate } from "../../src/forms.js";
import { BLANK_POINT, FOCUSABLE, SELECTION_STATE, TAB_DISTANCE } from "../../src/keys.js";

export const SLOW = 30_000; // glides and holds use real sleeps

export type Ev = { t: string; a?: any; ts: number };
export type Box = { x: number; y: number; width: number; height: number };

export interface Field {
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
  control?: string;                    // a <label>: the selector of the checkbox its click is passed on to
  textRect?: Box;                      // a <label>: its TEXT's rect (ISO_LABEL_OF `text`), when it is not the whole box
  wraps?: boolean;                     // a <label> that CONTAINS its control (ISO_LABEL_OF `contains`)
}

export interface FakeOpts {
  field?: Partial<Field>;                      // the single field every selector means
  fields?: Record<string, Partial<Field>>;     // several fields, by selector
  box?: Box;
  engine?: boolean;          // the engine pointer path answers
  engineExt?: boolean;       // ... and takes the r43 extensions (an EMPTY dispatchKeySequence is -32602)
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

export const timeoutError = (msg = "Timeout 1500ms exceeded.") => Object.assign(new Error(msg), { name: "TimeoutError" });

// ONE locator prototype for this file: attachHumanize patches the Locator prototype once per module.
export const protoCalls: { m: string; args: any[] }[] = [];
const ORIG = ["fill", "click", "type", "dblclick", "hover", "press", "pressSequentially", "clear", "tap", "check",
  "uncheck", "setChecked", "selectOption", "dragTo"];
export const locatorProto: Record<string, any> = {};
for (const m of ORIG) locatorProto[m] = async function (...args: any[]) { protoCalls.push({ m, args }); return `proto-${m}`; };

export const MAIN_FRAME = { name: "main" };
export const allExpressions: string[] = [];
export const FOCUS_QUERY = { focusQuery: true };

export function makeFake(opts: FakeOpts = {}) {
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
  const hits: { node: number; box: Box }[] = [];   // ISO_HIT_IN_BOX asked: the hit node, the box asked about (a read, not an event)
  let handles = 0;           // main-world element handles Playwright would make (each = a textContent preview)
  let waited = false;
  const engineCalls: any[] = [];
  const keySequences: any[] = [];
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
  const fieldAt = (x: number, y: number) => allFields().find((f) => f.visible !== false && inBox(f.box, x, y)) ?? null;
  // what ISO_LABEL_OF reads from a checkbox: its label is the field whose `control` names it
  const labelOf = (f: Field) => {
    if (f.tag !== "input" || !["checkbox", "radio"].includes(f.type)) return null;
    const sel = Object.keys(fields).find((k) => fields[k] === f);
    const lab = allFields().find((g) => g.tag === "label" && g.control === sel);
    return { tag: "input", type: f.type, rect: [f.box.x, f.box.y, f.box.width, f.box.height],
      hidden: f.visible === false || f.box.width <= 1, checked: !!f.checked, disabled: false,
      label: lab ? { rect: [lab.box.x, lab.box.y, lab.box.width, lab.box.height],
        text: lab.textRect ? [lab.textRect.x, lab.textRect.y, lab.textRect.width, lab.textRect.height] : null, contains: !!lab.wraps } : null };
  };
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
      if (selAll) { focus.value = ""; selAll = false; }
      else if (mods.has("Control") || mods.has("Alt")) {   // the browser's word delete: Backspace under Control (Alt on macOS)
        const v = Array.from(focus.value);
        let i = v.length;
        while (i && v[i - 1] === " ") i--;
        while (i && v[i - 1] !== " ") i--;
        focus.value = v.slice(0, i).join("");
      } else focus.value = Array.from(focus.value).slice(0, -1).join("");
      return;
    }
    if (k === "End") { selAll = false; return; }
    if (k === "Tab") {   // lane D: Tab moves the focus along the fields (Shift: back) and selects a field's text
      const order = allFields();
      // nothing focused: Tab enters the page at its first stop (Shift+Tab at the last), as the browser does
      const i = focus ? order.indexOf(focus) + (mods.has("Shift") ? -1 : 1) : (mods.has("Shift") ? order.length - 1 : 0);
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
    if (f.tag === "label" && f.control) {
      const c = fieldFor(f.control);
      rec("label-click", f.control);
      if (c && !c.stubborn) { c.checked = !c.checked; rec("toggle", c.checked); }
      return;
    }
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
          if (e.startsWith(`(${ISO_LABEL_BY_SEL})`)) {
            const a = argOf(e, ISO_LABEL_BY_SEL);
            const f = fieldFor(a.sel);
            if (!f) return { result: { value: { n: 0 } } };
            const o = labelOf(f);
            return { result: { value: o ? { ...o, n: 1 } : { n: 1 } } };
          }
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
            hits.push({ node: id, box: { x: arg.x, y: arg.y, width: arg.width, height: arg.height } });   // the box asked about
            return { result: { value: !!b && Math.abs(b.x - arg.x) <= 1.5 && Math.abs(b.y - arg.y) <= 1.5
              && Math.abs(b.width - arg.width) <= 1.5 && Math.abs(b.height - arg.height) <= 1.5 } };
          }
          if (params.functionDeclaration === ISO_LISTBOX_ROW) return { result: { value: f ? listboxState(f, arg.i) : null } };
          if (params.functionDeclaration === ISO_LABEL_OF) return { result: { value: f ? labelOf(f) : null } };
          if (params.functionDeclaration === TAB_DISTANCE) {
            // lane D: the Tab order is the fields' order; text = an input of a text type or a textarea
            if (!f) return { result: { value: null } };
            const order = allFields();
            const text = (g: Field | null) => !!g && (g.tag === "textarea" || (g.tag === "input" && ["text", "search", ""].includes(g.type)));
            const ai = focus ? order.indexOf(focus) : -1;
            const ti = order.indexOf(f);
            let near: { off: number; rect: number[] } | null = null;   // the text stop 1-3 before f (or 1 after) a hand clicks into and Tabs from
            for (const off of [1, 2, 3, -1]) {
              const c = order[ti - off];
              if (c && c !== focus && text(c)) { near = { off, rect: [c.box.x, c.box.y, c.box.width, c.box.height] }; break; }
            }
            return { result: { value: { dist: ai >= 0 ? ti - ai : null, fromText: text(focus), focused: focus === f,
              activatable: f.tag === "button" || f.tag === "select" || f.type === "checkbox", text: text(f), tag: f.tag,
              type: f.type, n: order.length, noAnchor: ai < 0, near } } };
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
        case "Browser.dispatchKeySequence":
          if (!opts.engineExt) throw new Error("Protocol error (Browser.dispatchKeySequence): 'Browser.dispatchKeySequence' wasn't found");
          if (!params?.keys?.length) throw Object.assign(new Error("Protocol error (Browser.dispatchKeySequence): Invalid parameters"), { code: -32602 });
          keySequences.push(params);
          return {};
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
    page, events, hits, natives, engineCalls, keySequences, waits, box: defBox, handles: () => handles,
    field: single as Field, fields,
    setFocused: (v: boolean | string) => { focus = v === false ? null : typeof v === "string" ? fields[v] : single; },
    of: (t: string) => events.filter((e) => e.t === t),
  };
}

export type Fake = ReturnType<typeof makeFake>;
export const attached = async (o: FakeOpts = {}, humanize: unknown = true): Promise<Fake> => {
  const f = makeFake(o);
  await attachHumanize({} as never, f.page, { humanize: humanize as any, seed: "forms-unit" });
  return f;
};
