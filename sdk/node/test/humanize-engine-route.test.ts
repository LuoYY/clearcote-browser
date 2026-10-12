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
import { attachHumanize, engineClicksMode, POINTER_PATH, POINTER_PATH_LEGACY } from "../src/humanize.js";

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
