/**
 * A fake page for the drag + scroll humanize tests (SC 232 lane E), the Node twin of Python's
 * tests/_fake_scrollpage.py: a document that scrolls (or not), a nested scrolling container, a slider
 * thumb that follows a held pointer, overlays that own the hit test. The isolated-world reads and the hit
 * test are answered at the CDP level (Runtime.evaluate / DOM.getNodeForLocation / Runtime.callFunctionOn).
 *
 * `log`: ["move", x, y, t] / ["down", t] / ["up", t] / ["wheel", dx, dy, x, y, t] / ["prog", how, t] for
 * every PROGRAMMATIC scroll / ["native_dnd", ...] / ["native_dragTo", ...]. Times are Date.now() ms.
 */
import {
  ISO_CSS_BOX, ISO_DESCRIBE, ISO_FRAMES, ISO_HIT_IN_BOX, ISO_PLATFORM, ISO_SCROLL, ISO_SCROLL_BY, ISO_VIEWPORT,
} from "../../src/isolated.js";

let SCROLLER = "";
try { SCROLLER = (await import("../../src/scroll.js") as any).ISO_SCROLLER ?? ""; } catch { /* before 0.52 */ }

export type Rect = [number, number, number, number];
interface El { rect: Rect; parent?: string }
interface Container { rect: Rect; st: number; inner: number }

export class Doc {
  vw = 1280; vh = 800; sx = 0; sy = 0;
  els = new Map<string, El>();
  containers = new Map<string, Container>();
  ptr: [number, number] = [0, 0];
  held = false;
  grab: [string, number, number] | null = null;
  follow = new Map<string, [number, number]>();
  covers: Rect[] = [];
  log: any[] = [];
  ids: string[] = [];
  constructor(public scrollable = true, public height = 4000, public platform = "Windows") {}

  id(sel: string): number { let i = this.ids.indexOf(sel); if (i < 0) { this.ids.push(sel); i = this.ids.length - 1; } return i + 1; }
  vrect(sel: string): Rect {
    const e = this.els.get(sel)!;
    let [x, y, w, h] = e.rect;
    if (e.parent) { const c = this.containers.get(e.parent)!; x += c.rect[0]; y += c.rect[1] - c.st; }
    return [x - this.sx, y - this.sy, w, h];
  }
  crect(c: string): Rect { const r = this.containers.get(c)!.rect; return [r[0] - this.sx, r[1] - this.sy, r[2], r[3]]; }
  at(x: number, y: number): string {
    for (const c of this.covers) if (c[0] <= x && x < c[0] + c[2] && c[1] <= y && y < c[1] + c[3]) return "#cover";
    for (const sel of [...this.els.keys()].reverse()) {
      const r = this.vrect(sel);
      const p = this.els.get(sel)!.parent;
      if (p) { const cr = this.crect(p); if (!(cr[0] <= x && x < cr[0] + cr[2] && cr[1] <= y && y < cr[1] + cr[3])) continue; }
      if (r[0] <= x && x < r[0] + r[2] && r[1] <= y && y < r[1] + r[3]) return sel;
    }
    return "body";
  }
  centerOn(sel: string, how: string): void {
    this.log.push(["prog", how, Date.now()]);
    const r = this.vrect(sel);
    if (this.scrollable) this.sy = Math.max(0, Math.min(this.height - this.vh, this.sy + r[1] + r[3] / 2 - this.vh / 2));
    const p = this.els.get(sel)!.parent;
    if (p) {
      const c = this.containers.get(p)!;
      const [, y, , h] = this.els.get(sel)!.rect;
      c.st = Math.max(0, Math.min(c.inner - c.rect[3], y + h / 2 - c.rect[3] / 2));
    }
  }
  move(x: number, y: number): void {
    this.ptr = [x, y];
    this.log.push(["move", x, y, Date.now()]);
    if (this.held && this.grab && this.follow.has(this.grab[0])) {
      const [sel, px0, ex0] = this.grab;
      const [gain, q] = this.follow.get(sel)!;
      this.els.get(sel)!.rect[0] = ex0 + Math.round(((x - px0) * gain) / q) * q;
    }
  }
  down(): void {
    this.held = true;
    this.log.push(["down", Date.now()]);
    const hit = this.at(...this.ptr);
    if (this.follow.has(hit)) this.grab = [hit, this.ptr[0], this.els.get(hit)!.rect[0]];
  }
  up(): void { this.held = false; this.grab = null; this.log.push(["up", Date.now()]); }
  wheel(dx: number, dy: number): void {
    this.log.push(["wheel", dx, dy, this.ptr[0], this.ptr[1], Date.now()]);
    for (const [cs, c] of this.containers) {
      const cr = this.crect(cs);
      if (cr[0] <= this.ptr[0] && this.ptr[0] < cr[0] + cr[2] && cr[1] <= this.ptr[1] && this.ptr[1] < cr[1] + cr[3]) {
        const st = Math.max(0, Math.min(c.inner - c.rect[3], c.st + dy));
        if (st !== c.st) { c.st = st; return; }
      }
    }
    if (this.scrollable) this.sy = Math.max(0, Math.min(this.height - this.vh, this.sy + dy));
  }

  evaluate(fn: string, arg: any): any {
    if (fn === ISO_VIEWPORT) return [this.vw, this.vh];
    if (fn === ISO_PLATFORM) return this.platform;
    if (fn === ISO_SCROLL) return [this.sx, this.sy];
    if (fn === ISO_FRAMES) return true;
    if (fn === ISO_CSS_BOX) {
      if (!this.els.has(arg.sel)) return { n: 0 };
      if (arg.scroll) this.centerOn(arg.sel, "css_box");
      return { n: 1, rect: this.vrect(arg.sel) };
    }
    if (SCROLLER && fn === SCROLLER) {
      const p = this.els.get(arg.sel)?.parent;
      return p ? { rect: this.crect(p), x: false, y: true } : null;
    }
    if (fn === ISO_SCROLL_BY) {
      this.log.push(["prog", "scroll_by", Date.now()]);
      const before = this.sy;
      if (this.scrollable) this.sy = Math.max(0, Math.min(this.height - this.vh, this.sy + arg[1]));
      return [0, this.sy - before];
    }
    return null;
  }
  callOn(sel: string, fn: string, arg: any): any {
    if (fn === ISO_HIT_IN_BOX) {
      if (arg.sel) return sel === arg.sel;
      if (!this.els.has(sel)) return false;
      const r = this.vrect(sel);
      return [arg.x, arg.y, arg.width, arg.height].every((v: number, i: number) => Math.abs(v - r[i]) <= 1.5);
    }
    if (fn === ISO_DESCRIBE) {
      if (!this.els.has(sel)) return null;
      return { rect: this.vrect(sel), bl: 0, bt: 0, tag: "div", type: "", ce: false };
    }
    return null;
  }
}

export class FakeLoc {
  constructor(public _page: any, public _selector: string) {}
  get _frame() { return this._page._main; }
  page() { return this._page; }
  first() { return this; }
  async count() { return this._page.doc.els.has(this._selector) ? 1 : 0; }
  async isVisible() { return this._page.doc.els.has(this._selector); }
  async isEnabled() { return true; }
  async waitFor() { /* visible */ }
  async boundingBox() { const [x, y, width, height] = this._page.doc.vrect(this._selector); return { x, y, width, height }; }
  async scrollIntoViewIfNeeded() { this._page.doc.centerOn(this._selector, "playwright"); }
  async dragTo(target: any, kw: any) { this._page.doc.log.push(["native_dragTo", this._selector, target._selector, kw]); }
  async fill() {} async click() {} async type() {} async dblclick() {} async hover() {} async press() {}
  async pressSequentially() {} async clear() {} async tap() {} async check() {} async uncheck() {}
}

export function fakePage(doc: Doc, extra: Record<string, unknown> = {}): any {
  const cdp = {
    send: async (method: string, params?: any): Promise<any> => {
      if (method === "Page.getFrameTree") return { frameTree: { frame: { id: "MAIN" } } };
      if (method === "Page.createIsolatedWorld") return { executionContextId: 1 };
      if (method === "Runtime.evaluate") {
        const ex: string = params.expression;
        const m = /^\(([\s\S]*)\)\(([\s\S]*)\)$/.exec(ex);
        if (!m) return { result: { value: null } };
        // the function is the longest prefix that is a known source
        const fn = m[1], arg = JSON.parse(m[2]);
        return { result: { value: doc.evaluate(fn, arg) } };
      }
      if (method === "DOM.getNodeForLocation") {
        const sel = doc.at(params.x - doc.sx, params.y - doc.sy);
        return { backendNodeId: doc.id(sel), frameId: "MAIN" };
      }
      if (method === "DOM.resolveNode") return { object: { objectId: String(params.backendNodeId) } };
      if (method === "Runtime.callFunctionOn") {
        const sel = doc.ids[Number(params.objectId) - 1];
        return { result: { value: doc.callOn(sel, params.functionDeclaration, params.arguments[0].value) } };
      }
      if (method === "Runtime.releaseObject") return {};
      throw new Error(method);
    },
  };
  const main = {};
  const page: any = {
    doc, _main: main,
    mouse: {
      move: async (x: number, y: number) => doc.move(x, y),
      down: async () => doc.down(),
      up: async () => doc.up(),
      wheel: async (dx: number, dy: number) => doc.wheel(dx, dy),
      click: async (x: number, y: number) => { doc.move(x, y); doc.down(); doc.up(); },
    },
    keyboard: { type: async () => {}, press: async () => {}, down: async () => {}, up: async () => {}, insertText: async () => {} },
    click: async (s: string) => { doc.log.push(["native_click", s]); },
    hover: async (s: string) => { doc.log.push(["native_hover", s]); },
    dblclick: async () => {}, fill: async () => {}, press: async () => {}, type: async () => {}, focus: async () => {},
    dragAndDrop: async (a: string, b: string, o: any) => { doc.log.push(["native_dnd", a, b, o]); },
    on: () => undefined, mainFrame: () => main, waitForTimeout: async () => {},
    viewportSize: () => ({ width: 1280, height: 800 }),
    evaluate: () => { throw new Error("humanize must not evaluate in the page's world"); },
    context: () => ({ newCDPSession: async () => cdp }),
    ...extra,
  };
  page.locator = (sel: string) => new FakeLoc(page, sel);
  return page;
}

export const ev = (doc: Doc, kind: string) => doc.log.filter((e) => e[0] === kind);
