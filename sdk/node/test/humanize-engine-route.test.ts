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
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

// R3b (verifier A): lane B's rollover rate forced per test (null = the persona's own)
const forced = vi.hoisted(() => ({ roll: null as number | null }));
vi.mock("../src/motion.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("../src/motion.js")>();
  return { ...real, rolloverRate: (p: any) => (forced.roll === null ? real.rolloverRate(p) : forced.roll) };
});

import { ENGINE_EXT_MISSING, attachHumanize, engineClicksMode, POINTER_PATH, POINTER_PATH_LEGACY } from "../src/humanize.js";
import { ENGINE_KEY_SEQUENCE } from "../src/forms.js";
import { attached as formsAttached } from "./helpers/humanize-fake.js";

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
    keyboard: {
      type: noop, insertText: noop,
      press: async (k: string) => { log.push(["press", k]); },
      down: async (k: string) => { log.push(["kdown", k]); },
      up: async (k: string) => { log.push(["kup", k]); },
    },
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
    forced.roll = null;
  });
  // No typos and NO ROLLOVER: a run the persona rolls over stays per key since verifier A's R3b (its own test below);
  // these tests look at the engine run itself. The rate is read at attach, so it is forced before attachHumanize.
  const noTypos = () => { for (const k of EXT_ENVS) extSaved[k] = process.env[k]; process.env.CLEARCOTE_HUMANIZE_TYPO_RATE = "0"; forced.roll = 0; };

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

// --------------------------------------------------------------- fixes after verifier A (SC 232, R3-R5)
/** A CDP proxy, or a PRO engine before r43, whose EMPTY dispatchKeySequence fails with other wording than -32602's. */
class InvalidTargetEngine extends Engine {
  probes = 0;
  async send(method: string, params?: any): Promise<any> {
    if (method === ENGINE_KEY_SEQUENCE) {
      this.probes++;
      throw new Error("Protocol error (Browser.dispatchKeySequence): Invalid target");
    }
    return super.send(method, params);
  }
}

const typedKeys = (log: any[]) => log.filter((e) => (e[0] === "press" || e[0] === "kdown") && e[1].length === 1).map((e) => e[1]);

describe("fixes after verifier A (R3-R5)", () => {
  const ENVS = ["CLEARCOTE_HUMANIZE_TYPO_RATE", "CLEARCOTE_HUMANIZE_SCORE", "CLEARCOTE_HUMANIZE_SCORE_FILE", "CLEARCOTE_NO_WARN"];
  const saved: Record<string, string | undefined> = {};
  let dir = "";
  afterEach(() => {
    for (const k of ENVS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
    forced.roll = null;
    if (dir) rmSync(dir, { recursive: true, force: true });
    dir = "";
  });
  const setup = () => { for (const k of ENVS) saved[k] = process.env[k]; process.env.CLEARCOTE_HUMANIZE_TYPO_RATE = "0"; };

  it("R3a: engine 'off' keeps a typed run per key on an r43 engine (no probe, no dispatchKeySequence)", async () => {
    setup();
    const eng = new R43Engine();
    const { page, log, browser } = makePage(eng);
    await attachHumanize(browser as never, page, { humanize: { engine: "off" }, seed: "route" });
    await page.keyboard.type("hello");
    expect(eng.keys.length).toBe(0);
    expect(eng.probes).toBe(0);
    expect(typedKeys(log)).toEqual(Array.from("hello"));
  }, 30_000);

  it("R3b: a run the persona rolls over stays per key (with its overlaps); a run with none goes engine-side", async () => {
    setup();
    forced.roll = 1;   // the rate is read at attach (lane B's rolloverRate(persona))
    const eng = new R43Engine();
    const { page, log, browser } = makePage(eng);
    await attachHumanize(browser as never, page, { humanize: true, seed: "route" });
    await page.keyboard.type("hello world");
    expect(eng.keys.length, "a rolled run stays per key").toBe(0);
    expect(typedKeys(log)).toEqual(Array.from("hello world"));
    expect(log.filter((e) => e[0] === "kdown" && e[1].length === 1).length).toBeGreaterThanOrEqual(6);
    forced.roll = 0;
    const eng2 = new R43Engine();
    const p2 = makePage(eng2);
    await attachHumanize(p2.browser as never, p2.page, { humanize: true, seed: "route" });
    await p2.page.keyboard.type("hello world");
    expect(eng2.keys.length, "a run with no rollover: the engine").toBe(1);
    expect(eng2.keys[0].keys.length).toBe(11);
  }, 30_000);

  it("R3c: an engine typed run records one 'typed' trace line with the planned times", async () => {
    setup();
    dir = mkdtempSync(join(tmpdir(), "cc-r3c-"));
    const file = join(dir, "score.jsonl");
    process.env.CLEARCOTE_HUMANIZE_SCORE = "1";
    process.env.CLEARCOTE_HUMANIZE_SCORE_FILE = file;
    process.env.CLEARCOTE_NO_WARN = "1";
    forced.roll = 0;
    const eng = new R43Engine();
    const { page, browser } = makePage(eng);
    await attachHumanize(browser as never, page, { humanize: true, seed: "route" });
    await page.keyboard.type("hello world");
    expect(eng.keys.length).toBe(1);
    const lines: any[] = existsSync(file) ? readFileSync(file, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
    const typed = lines.filter((l) => l.kind === "typed");
    expect(typed, lines.map((l) => l.kind).join(",")).toHaveLength(1);
    expect(typed[0].n).toBe(11);
    expect(typed[0].meta).toEqual({ n: 11, typos: false, floor_ms: 0, engine: true });
  }, 30_000);

  it("R4: a probe that fails with other wording than -32602's is not an r43 engine (no fromX teleport later)", async () => {
    setup();
    const eng = new InvalidTargetEngine();
    const { page, log, browser } = makePage(eng);
    await attachHumanize(browser as never, page, { humanize: true, seed: "route" });
    await page.mouse.click(300, 200);                       // plain and in sync: the r42 route
    await page.mouse.click(300, 200, { button: "right" });  // the probe fails with "Invalid target": not r43 -> SDK-side
    await page.mouse.move(500, 400);                        // an SDK glide: the engine is out of sync now
    await page.mouse.click(520, 410);                       // declined (no fromX re-sync on a non-r43 engine): planner
    expect(eng.probes).toBe(1);
    expect(eng.calls.length).toBe(1);
    expect(eng.calls[0].fromX).toBeUndefined();
    expect(eng.calls[0].button).toBeUndefined();
    expect(log.filter((e) => e[0] === "down").length).toBe(2);
  }, 30_000);

  it("R5: with engine 'all' selector clicks with a button, a click count or modifiers take the r43 extensions", async () => {
    setup();
    const f = await formsAttached({ field: { tag: "button", type: "" }, engine: true, engineExt: true }, { engine: "all" });
    await f.page.click("#b");
    await f.page.click("#b", { button: "right" });
    await f.page.dblclick("#b");
    await f.page.click("#b", { modifiers: ["Shift"] });
    expect(f.engineCalls.length).toBe(4);
    expect(f.engineCalls[0].noClick).toBe(false);
    expect(f.engineCalls[0].button).toBeUndefined();
    expect(f.engineCalls[1].button).toBe("right");
    expect(f.engineCalls[2].clickCount).toBe(2);
    expect(f.engineCalls[3].modifiers).toBe(8);
    expect(f.of("down")).toEqual([]);
    expect(f.of("kdown").map((e) => e.a)).toEqual(["Shift"]);
    expect(f.of("kup").map((e) => e.a)).toEqual(["Shift"]);
    expect(f.natives.filter((n) => !n.args[1]?.trial)).toEqual([]);
  }, 30_000);
});

