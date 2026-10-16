/**
 * Which clicks the engine pointer path walks (SC 210 lane S item 7), on a fake page -- a port of
 * sdk/python/tests/test_humanize_engine_route.py.
 *
 * Measured on r38: the engine (Browser.dispatchPointerPath / humanizedClick) walks from ITS OWN last
 * position on the page, which an SDK glide does not update -- an engine click, an SDK glide to
 * (800,550), then mouse.click(400,200): the engine's first sample was at the previous engine click, a
 * 559 px teleport. And it answers only after the walk and the click, so sleeping a full `duration`
 * afterwards added an idle second walk's worth of time to every click.
 */
import { afterEach, describe, expect, it } from "vitest";
import { ENGINE_EXT_MISSING, attachHumanize, engineClicksMode, POINTER_PATH, POINTER_PATH_LEGACY } from "../src/humanize.js";
import { ENGINE_KEY_SEQUENCE } from "../src/forms.js";

/** Browser CDP session that answers the pointer-path command after the walk (+120 ms press/release), like r38. */
class Engine {
  calls: any[] = [];
  async send(method: string, params?: any): Promise<any> {
    if (method === POINTER_PATH || method === POINTER_PATH_LEGACY) {
      this.calls.push(params);
      await new Promise((r) => setTimeout(r, params.duration * 1000 + 120));
    }
    return {};
  }
}

const makePage = (engine: Engine) => {
  const log: any[] = [];
  const noop = async () => undefined;
  const target = { send: async (m: string) => (m === "Target.getTargetInfo" ? { targetInfo: { targetId: "T1" } } : undefined) };
  const browser = { newBrowserCDPSession: async () => engine };
  const ctx = { newCDPSession: async () => target, browser: () => browser };
  const locatorProto = {
    fill: noop, click: noop, type: noop, dblclick: noop, hover: noop, press: noop,
    pressSequentially: noop, clear: noop, tap: noop, check: noop, uncheck: noop,
    dragTo: noop, page: () => null,
  };
  const page = {
    mouse: {
      move: async (x: number, y: number) => { log.push(["move", x, y]); },
      click: async (x: number, y: number) => { log.push(["click", x, y]); },
      wheel: noop,
      down: async () => { log.push(["down"]); },
      up: async () => { log.push(["up"]); },
    },
    keyboard: { type: noop, press: noop, down: noop, up: noop, insertText: noop },
    click: noop, hover: noop, dblclick: noop, fill: noop, press: noop, type: noop,
    focus: noop, evaluate: async () => undefined, on: () => undefined,
    mainFrame: () => ({}), waitForTimeout: noop,
    context: () => ctx,
    locator: () => Object.create(locatorProto),
  } as any;
  return { page, log, browser };
};

const ENV = "CLEARCOTE_HUMANIZE_ENGINE_CLICKS";
afterEach(() => { delete process.env[ENV]; });

describe("engine pointer path routing", () => {
  it("consecutive coordinate clicks walk in the engine", async () => {
    const eng = new Engine();
    const { page, browser } = makePage(eng);
    await attachHumanize(browser as never, page, { humanize: true, seed: "route" });
    await page.mouse.click(300, 300);
    await page.mouse.click(400, 200);
    expect(eng.calls.map((c) => c.x)).toEqual([300, 400]);
  }, 20_000);

  it("after an SDK glide the engine does not walk from its stale position", async () => {
    const eng = new Engine();
    const { page, log, browser } = makePage(eng);
    await attachHumanize(browser as never, page, { humanize: true, seed: "route" });
    await page.mouse.click(300, 300);   // engine: its cursor is now (300, 300)
    await page.mouse.move(800, 550);    // SDK planner glide; the engine does not see it
    const before = log.filter((e) => e[0] === "move").length;
    await page.mouse.click(400, 200);
    expect(eng.calls.length).toBe(1);
    const glide = log.filter((e) => e[0] === "move").slice(before);
    expect(glide.length).toBeGreaterThan(10);
    expect(glide[glide.length - 1].slice(1)).toEqual([400, 200]);
    expect(log.some((e) => e[0] === "down")).toBe(true);
  }, 20_000);

  it("after a native fallback click the engine does not walk from its stale position", async () => {
    // A selector click the SDK hands to Playwright moves the real pointer to the element; the engine does
    // not see it either (verifier A #6: only `placed` was recorded, so the engine walked again).
    const eng = new Engine();
    const { page, log, browser } = makePage(eng);
    await attachHumanize(browser as never, page, { humanize: true, seed: "route" });
    await page.mouse.click(300, 300);   // engine: its cursor is now (300, 300)
    await page.click("#elsewhere");     // this fake cannot resolve it: Playwright's own click
    await page.mouse.click(400, 200);
    expect(eng.calls.length).toBe(1);
    const moves = log.filter((e) => e[0] === "move");
    expect(moves[moves.length - 1].slice(1)).toEqual([400, 200]);
  }, 20_000);

  it("an engine answer after its walk is not followed by a second wait", async () => {
    const eng = new Engine();
    const { page, browser } = makePage(eng);
    await attachHumanize(browser as never, page, { humanize: true, seed: "route" });
    await page.mouse.click(300, 300);
    const t0 = Date.now();
    await page.mouse.click(310, 320);
    const spent = Date.now() - t0;
    const walk = eng.calls[1].duration * 1000 + 120;
    expect(spent).toBeGreaterThanOrEqual(walk - 5);
    expect(spent).toBeLessThan(walk + 150);
  }, 20_000);

  it("CLEARCOTE_HUMANIZE_ENGINE_CLICKS=off keeps every click on the planner", async () => {
    process.env[ENV] = "off";
    const eng = new Engine();
    const { page, log, browser } = makePage(eng);
    await attachHumanize(browser as never, page, { humanize: true, seed: "route" });
    await page.mouse.click(300, 300);
    await page.mouse.click(400, 200);
    expect(eng.calls).toEqual([]);
    expect(log.filter((e) => e[0] === "down").length).toBe(2);
  }, 20_000);

  it("engineClicksMode values match Python", () => {
    for (const [v, want] of [["", "coords"], ["0", "off"], ["off", "off"], ["False", "off"], ["all", "all"],
      ["selector", "all"], ["coords", "coords"], ["whatever", "coords"]]) {
      process.env[ENV] = v;
      expect(engineClicksMode()).toBe(want);
    }
  });
});

// ------------------------------------------------------------------ r43 engine extensions (SC 232 lane H)
// A port of the Python fake-session tests (test_humanize_forms_async.py: _R43Session / _R42Session): the SDK side of
// pro-r43-07 (dispatchPointerPath fromX/fromY, button, clickCount, modifiers, buttons, settle; answers after its last
// event) and pro-r43-09 (Browser.dispatchKeySequence), feature-detected once per browser by an EMPTY key sequence.

/** An r43 engine: the pointer path takes the extensions; an EMPTY dispatchKeySequence is invalid params (-32602). */
class R43Engine extends Engine {
  keys: any[] = [];
  probes = 0;
  async send(method: string, params?: any): Promise<any> {
    if (method === ENGINE_KEY_SEQUENCE) {
      if (!params?.keys?.length) {
        this.probes++;
        throw Object.assign(new Error("Protocol error (Browser.dispatchKeySequence): Invalid parameters"), { code: -32602 });
      }
      this.keys.push(params);
      return {};
    }
    return super.send(method, params);
  }
}

/** A PRO engine before r43: the pointer path answers, Browser.dispatchKeySequence does not exist (-32601). */
class R42Engine extends Engine {
  probes = 0;
  async send(method: string, params?: any): Promise<any> {
    if (method === ENGINE_KEY_SEQUENCE) {
      this.probes++;
      throw Object.assign(new Error("Protocol error (Browser.dispatchKeySequence): 'Browser.dispatchKeySequence' wasn't found"), { code: -32601 });
    }
    return super.send(method, params);
  }
}

const EXT_ENVS = ["CLEARCOTE_HUMANIZE_TYPO_RATE", "CLEARCOTE_HUMANIZE_ENGINE_EXT"];
const extSaved: Record<string, string | undefined> = {};

describe("r43 engine extensions (lane H)", () => {
  afterEach(() => {
    for (const k of EXT_ENVS) { if (extSaved[k] === undefined) delete process.env[k]; else process.env[k] = extSaved[k]; }
  });
  const noTypos = () => { for (const k of EXT_ENVS) extSaved[k] = process.env[k]; process.env.CLEARCOTE_HUMANIZE_TYPO_RATE = "0"; };

  it("walks right, double and modified clicks, held legs and slip-free typed runs engine-side", async () => {
    noTypos();
    const eng = new R43Engine();
    const { page, log, browser } = makePage(eng);
    await attachHumanize(browser as never, page, { humanize: true, seed: "route" });
    await page.mouse.click(300, 200);                       // plain and in sync: the r42 route, no probe
    await page.mouse.click(300, 200, { button: "right" });  // the probe, then the engine walks it
    await page.mouse.click(310, 200, { clickCount: 2 });
    await page.mouse.click(320, 200, { modifiers: ["Shift"] });
    await page.mouse.click(300, 200, { delay: 250 });       // a caller-chosen hold stays SDK-side
    expect(eng.calls.length).toBe(4);
    expect(eng.probes).toBe(1);
    expect(eng.calls[0].fromX).toBeUndefined();
    expect(eng.calls[0].button).toBeUndefined();
    expect(eng.calls[0].noClick).toBe(false);
    expect(eng.calls[1].button).toBe("right");
    expect(eng.calls[2].clickCount).toBe(2);
    expect(eng.calls[3].modifiers).toBe(8);
    expect(log.filter((e) => e[0] === "down").length).toBe(1);   // only the held-delay click presses natively
    // a held leg: dispatchPointerPath {buttons: 1, noClick}, starting where the SDK's own glide left the pointer
    await page.mouse.down();
    await page.mouse.move(360, 240);
    await page.mouse.up();
    const held = eng.calls.filter((c) => c.buttons);
    expect(held.length).toBe(1);
    expect(held[0]).toMatchObject({ buttons: 1, noClick: true, fromX: 300, fromY: 200, x: 360, y: 240 });
    // a typed run with no slip to plan: ONE dispatchKeySequence, US codes, Shift as modifiers 8
    await page.keyboard.type("hello, World!");
    expect(eng.keys.length).toBe(1);
    const keys = eng.keys[0].keys;
    expect(keys.map((k: any) => k.key).join("")).toBe("hello, World!");
    expect(keys[5].code).toBe("Comma");
    expect(keys[7].modifiers).toBe(8);
    expect(keys[0].modifiers).toBeUndefined();
    expect(keys[12]).toMatchObject({ code: "Digit1", modifiers: 8, gapMs: 0 });
    for (const k of keys) { expect(k.dwellMs).toBeGreaterThanOrEqual(45); expect(k.dwellMs).toBeLessThanOrEqual(120); }
    for (const k of keys.slice(0, -1)) expect(k.gapMs).toBeGreaterThanOrEqual(25);
  }, 30_000);

  it("a PRO engine without the extensions is reported once through onFallback; presses stay SDK-side", async () => {
    noTypos();
    const eng = new R42Engine();
    const events: any[] = [];
    const { page, log, browser } = makePage(eng);
    await attachHumanize(browser as never, page, { humanize: { onFallback: (e: any) => events.push(e), strict: true }, seed: "route" });
    await page.mouse.click(300, 200);                       // the pointer path answers: a PRO engine
    await page.mouse.click(300, 200, { button: "right" });  // the probe says no: reported once, pressed SDK-side
    await page.mouse.click(310, 200, { clickCount: 2 });    // no second probe, no second report
    expect(eng.calls.length).toBe(1);
    expect(eng.probes).toBe(1);
    expect(events).toEqual([{ api: "engine_ext", reason: ENGINE_EXT_MISSING }]);
    expect(log.filter((e) => e[0] === "down").length).toBe(3);
  }, 30_000);

  it("CLEARCOTE_HUMANIZE_ENGINE_EXT=off keeps the r42 route on an r43 engine", async () => {
    noTypos();
    process.env.CLEARCOTE_HUMANIZE_ENGINE_EXT = "off";
    const eng = new R43Engine();
    const { page, log, browser } = makePage(eng);
    await attachHumanize(browser as never, page, { humanize: true, seed: "route" });
    await page.mouse.click(300, 200);
    await page.mouse.click(300, 200, { button: "right" });
    expect(eng.calls.length).toBe(1);
    expect(eng.probes).toBe(0);
    expect(log.filter((e) => e[0] === "down").length).toBe(1);
  }, 30_000);
});
