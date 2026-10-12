import { describe, it, expect, vi, afterEach } from "vitest";

// Wall-clock pacing of planned pointer paths (SC 210 lane P; mirrors tests/test_humanize_pacing.py).
//
// The planner returns ~130 samples ~6.6 ms apart for a ~1 s glide. The old dispatch loop awaited
// mouse.move for each sample and THEN slept the planned gap, so the dispatch round trip (27 ms median
// on PRO r38) was added to every sample: glides ran 3.5-5.4 s, ambientMotion(1500) 5.7x as long
// (SC 209). These tests drive the real attachHumanize wrappers with a fake mouse whose move costs 0 or
// 25 ms of FAKE time (vitest fake timers + performance), so they are exact and fast. On 0.48.0 the
// 25 ms duration checks and the ambient checks fail.

const spy = vi.hoisted(() => ({ plans: [] as { from: { x: number; y: number }; plan: any[] }[] }));
vi.mock("../src/motion.js", async (importOriginal) => {
  const m: any = await importOriginal();
  return {
    ...m,
    planMove: (from: any, to: any, p: any, o: any) => {
      const plan = m.planMove(from, to, p, o);
      spy.plans.push({ from: { x: from.x, y: from.y }, plan });
      return plan;
    },
  };
});

import { attachHumanize } from "../src/humanize.js";
import { ISO_PLATFORM, ISO_VIEWPORT } from "../src/isolated.js";
import { walk, trimTo, schedule, speedAt, STALL_MS, FAST_PX_PER_MS } from "../src/pacing.js";
import { makePersona, planAmbient } from "../src/motion.js";

type Ev = { op: string; t: number; x?: number; y?: number };

const fakePage = (latMs: number) => {
  const log: Ev[] = [];
  const ev = async (op: string, x?: number, y?: number) => {
    await new Promise((r) => setTimeout(r, latMs));
    log.push({ op, t: performance.now(), x, y });
  };
  const noop = async () => undefined;
  const cdp = {
    send: async (method: string, params?: any): Promise<any> => {
      if (method === "Page.getFrameTree") return { frameTree: { frame: { id: "MAIN" } } };
      if (method === "Page.createIsolatedWorld") return { executionContextId: 1 };
      if (method === "Runtime.evaluate") {
        if (params.expression.startsWith(`(${ISO_PLATFORM})`)) return { result: { value: "Win32" } };
        if (params.expression.startsWith(`(${ISO_VIEWPORT})`)) return { result: { value: [1280, 800] } };
        return { result: { value: null } };
      }
      throw new Error(method);
    },
  };
  const locatorProto = {
    fill: noop, click: noop, type: noop, dblclick: noop, hover: noop, press: noop,
    pressSequentially: noop, clear: noop, tap: noop, check: noop, uncheck: noop,
    dragTo: noop, page: () => null,
  };
  const page = {
    mouse: {
      move: (x: number, y: number) => ev("move", x, y),
      down: () => ev("down"), up: () => ev("up"),
      click: noop, wheel: noop, dblclick: noop,
    },
    keyboard: { type: noop, press: noop, down: noop, up: noop, insertText: noop },
    click: noop, hover: noop, dblclick: noop, fill: noop, press: noop, type: noop,
    focus: noop, on: () => undefined, mainFrame: () => ({}), waitForTimeout: noop,
    viewportSize: () => ({ width: 1280, height: 800 }),
    evaluate: () => { throw new Error("humanize must not evaluate in the page's world"); },
    context: () => ({ newCDPSession: async () => cdp }),
    locator: () => Object.create(locatorProto),
  };
  return { page: page as any, log };
};

// Run an async action to completion under fake timers, 1 ms of fake time per tick.
const drive = async <T>(p: Promise<T>): Promise<T> => {
  let done = false;
  const q = p.finally(() => { done = true; });
  for (let i = 0; !done && i < 120_000; i++) await vi.advanceTimersByTimeAsync(1);
  return q;
};

const maxGap = (pts: { x: number; y: number }[]) => {
  let m = 0;
  for (let i = 1; i < pts.length; i++) m = Math.max(m, Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y));
  return m;
};

// No dispatched hop longer than a fast hand covers between the two events (3 px/ms, >= floor).
const hopsOk = (moves: Ev[], floor = 40): boolean => {
  for (let i = 1; i < moves.length; i++) {
    const d = Math.hypot(moves[i].x! - moves[i - 1].x!, moves[i].y! - moves[i - 1].y!);
    if (d > Math.max(floor, FAST_PX_PER_MS * (moves[i].t - moves[i - 1].t)) + 1e-6) return false;
  }
  return true;
};

const checkWalk = (from: { x: number; y: number }, plan: any[], moves: Ev[], dur: number) => {
  const planned = plan.reduce((a, s) => a + s.sleepMs, 0);
  expect(dur, `glide took ${dur.toFixed(0)} ms, planned ${planned.toFixed(0)} ms`).toBeGreaterThanOrEqual(0.8 * planned - 1);
  expect(dur, `glide took ${dur.toFixed(0)} ms, planned ${planned.toFixed(0)} ms`).toBeLessThanOrEqual(1.2 * planned + 1);
  const lastM = moves[moves.length - 1], lastP = plan[plan.length - 1];
  expect([lastM.x, lastM.y]).toEqual([lastP.x, lastP.y]);
  const own = maxGap([from, ...plan]);
  expect(Math.hypot(moves[0].x! - from.x, moves[0].y! - from.y)).toBeLessThanOrEqual(Math.max(40, own) + 1e-6);
  expect(hopsOk(moves, Math.max(40, own)), "a skipped stretch teleported the cursor").toBe(true);
};

const setup = async (lat: number, seed: string) => {
  const f = fakePage(lat);
  await attachHumanize({} as never, f.page, { humanize: true, seed });
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "performance", "Date"] });
  return f;
};

afterEach(() => { vi.useRealTimers(); });

describe("humanize pacing: planned paths keep their planned duration", () => {
  for (const lat of [0, 25]) {
    it(`bare glides at ${lat} ms dispatch latency`, async () => {
      const f = await setup(lat, "pace-1");
      for (const [x, y] of [[900, 600], [150, 150], [640, 420], [1100, 120]]) {
        const n0 = spy.plans.length;
        const t0 = performance.now();
        await drive(f.page.mouse.move(x, y));
        const t1 = performance.now();
        expect(spy.plans.length).toBe(n0 + 1);
        const { from, plan } = spy.plans[spy.plans.length - 1];
        const mv = f.log.filter((e) => e.op === "move" && e.t > t0 && e.t <= t1);
        checkWalk(from, plan, mv, t1 - t0);
        expect([mv[mv.length - 1].x, mv[mv.length - 1].y]).toEqual([x, y]);
      }
    }, 60_000);

    it(`held drag at ${lat} ms: button order kept, lands on target, planned duration`, async () => {
      const f = await setup(lat, "pace-2");
      await drive(f.page.mouse.move(200, 300));
      await drive(f.page.mouse.down());
      const t0 = performance.now();
      await drive(f.page.mouse.move(560, 310));
      const t1 = performance.now();
      await drive(f.page.mouse.up());
      const ops = f.log.map((e) => e.op);
      expect(ops.filter((o) => o === "down").length).toBe(1);
      expect(ops.filter((o) => o === "up").length).toBe(1);
      const d = ops.indexOf("down"), u = ops.indexOf("up");
      const leg = f.log.slice(d + 1, u);
      expect(leg.length).toBeGreaterThan(0);
      expect(leg.every((e) => e.op === "move")).toBe(true);
      expect([leg[leg.length - 1].x, leg[leg.length - 1].y]).toEqual([560, 310]);
      const { from, plan } = spy.plans[spy.plans.length - 1];
      checkWalk(from, plan, leg, t1 - t0);
    }, 60_000);

    for (const ms of [600, 1500, 3000]) {
      it(`ambientMotion(${ms}) at ${lat} ms lasts ${ms} ms +-20 %`, async () => {
        const f = await setup(lat, "pace-3");
        const t0 = performance.now();
        await drive(f.page.ambientMotion(ms));
        const took = performance.now() - t0;
        const mv = f.log.filter((e) => e.op === "move");
        expect(mv.length).toBeGreaterThan(0);
        expect(took, `ambientMotion(${ms}) took ${took.toFixed(0)} ms`).toBeGreaterThanOrEqual(0.8 * ms);
        // With 25 ms latency a late walk stretches instead of catching up with jumps (SC 210 fix 2, no hop
        // over 40 px): ambient's fast flicks run long (Python, 30 plans: worst 1.67x at 600 ms).
        expect(took, `ambientMotion(${ms}) took ${took.toFixed(0)} ms`).toBeLessThanOrEqual((lat === 0 ? 1.2 : 1.8) * ms);
        expect(hopsOk(mv)).toBe(true);
      }, 60_000);
    }
  }
});

describe("pacing.walk", () => {
  const fake = (lat: number) => {
    let t = 0;
    const sent: { x: number; y: number; t: number }[] = [];
    return {
      now: () => t,
      sleep: async (ms: number) => { t += Math.max(0, ms); },
      move: async (x: number, y: number) => { t += lat; sent.push({ x, y, t }); },
      sent, get t() { return t; },
    };
  };

  it("never drops a pinned (button-state) step or the last one", async () => {
    const steps = Array.from({ length: 200 }, (_, i) => ({ x: i, y: 0, sleepMs: 2, btn: [37, 38, 120].includes(i) }));
    const c = fake(25);
    await walk(steps, c.move, { now: c.now, sleep: c.sleep, pinned: (s) => s.btn });
    const xs = c.sent.map((s) => s.x);
    expect(xs.length).toBeLessThan(100);
    for (const k of [37, 38, 120, 199]) expect(xs).toContain(k);
    expect([...xs].sort((a, b) => a - b)).toEqual(xs);
  });

  it("on time: every sample at its planned time", async () => {
    const steps = Array.from({ length: 50 }, (_, i) => ({ x: i * 3, y: 0, sleepMs: 7 }));
    const c = fake(0);
    expect(await walk(steps, c.move, { now: c.now, sleep: c.sleep })).toBe(50);
    c.sent.forEach((s, k) => expect(s.t).toBeCloseTo(7 * k, 6));
    expect(c.t).toBeCloseTo(350, 6);
  });

  it("dispatches the in-between point instead of a long jump", async () => {
    const steps = Array.from({ length: 40 }, (_, i) => ({ x: i * 30, y: 0, sleepMs: 1 }));
    const c = fake(25);
    await walk(steps, c.move, { now: c.now, sleep: c.sleep });
    // 30 px/ms is ten times a fast hand: every hop stays within what one covers in a 25 ms dispatch.
    expect(maxGap([{ x: 0, y: 0 }, ...c.sent])).toBeLessThanOrEqual(FAST_PX_PER_MS * 25 + 1e-6);
    expect(c.sent.length).toBeGreaterThan(10);
    expect(c.sent[c.sent.length - 1].x).toBe(39 * 30);
  });

  for (const stall of [200, 350, 500]) {
    it(`after a ${stall} ms stall it stretches instead of jumping (SC 210 lab v2)`, async () => {
      // 35 hops of 60-103 px a few ms apart on r38 under load: the old cap (3 px/ms x the time since the
      // last dispatch) let a slow dispatch turn into one big catch-up hop. Now <= 40 px, <= 20 px after it.
      const steps = Array.from({ length: 300 }, (_, i) => ({ x: i * 3, y: 0, sleepMs: 2 }));
      let t = 0;
      const sent: { x: number; t: number }[] = [];
      await walk(steps, async (x) => { t += [3, 9, 15].includes(sent.length) ? stall : 25; sent.push({ x, t }); },
        { now: () => t, sleep: async (ms) => { t += Math.max(0, ms); } });
      const hops = sent.slice(1).map((s, k) => s.x - sent[k].x);
      expect(Math.max(...hops)).toBeLessThanOrEqual(40 + 1e-6);
      for (const k of [3, 9, 15]) expect(hops[k], `the hop after stall ${k}`).toBeLessThanOrEqual(20 + 1e-6);
      expect(sent[sent.length - 1].x).toBe(299 * 3);
    });
  }

  it("pauses through a stall instead of skipping after it", async () => {
    const steps = Array.from({ length: 200 }, (_, i) => ({ x: i * 2, y: 0, sleepMs: 5 }));
    let t = 0;
    const sent: number[] = [];
    await walk(steps, async (x) => { t += sent.length === 10 ? 500 : 2; sent.push(x); },
      { now: () => t, sleep: async (ms) => { t += Math.max(0, ms); } });
    expect(sent.length).toBeGreaterThanOrEqual(185);
    expect(Math.abs(t - (1000 + 500 - STALL_MS))).toBeLessThan(30);
    expect(sent[sent.length - 1]).toBe(398);
  });

  it("a slow first sample is a stall too", async () => {
    const steps = Array.from({ length: 40 }, (_, i) => ({ x: i * 2, y: 0, sleepMs: 5 }));
    let t = 0;
    const sent: number[] = [];
    await walk(steps, async (x) => { t += sent.length ? 0 : 300; sent.push(x); },
      { now: () => t, sleep: async (ms) => { t += Math.max(0, ms); } });
    expect(sent.length).toBeGreaterThanOrEqual(40 - STALL_MS / 5 - 1);
  });

  it("stops at a failing move", async () => {
    let calls = 0;
    const c = fake(0);
    const n = await walk(Array.from({ length: 10 }, (_, i) => ({ x: i, y: 0, sleepMs: 5 })),
      async () => { if (++calls === 3) throw new Error("Target closed"); }, { now: c.now, sleep: c.sleep });
    expect(n).toBe(2);
    expect(calls).toBe(3);
  });

  it("trimTo cuts an ambient plan near the budget, dropping only a tail", () => {
    const p = makePersona("trim");
    for (const ms of [600, 1200, 1500, 4000]) {
      const plan = planAmbient({ x: 300, y: 300 }, { width: 1280, height: 800 }, p, ms);
      const cut = trimTo(plan, ms);
      const { total } = schedule(cut);
      expect(total).toBeGreaterThanOrEqual(0.85 * ms);
      expect(total).toBeLessThanOrEqual(1.1 * ms);
      expect(cut).toEqual(plan.slice(0, cut.length));
    }
  });

  it("trimTo on a short ask does not stop the pointer mid-flight", () => {
    for (let seed = 0; seed < 60; seed++) {
      const plan = planAmbient({ x: 300, y: 300 }, { width: 1280, height: 800 }, makePersona(seed), 300);
      const cut = trimTo(plan, 300);
      let peak = 0;
      for (let k = 0; k < plan.length; k++) peak = Math.max(peak, speedAt(plan, k));
      const { total } = schedule(cut);
      expect(cut).toEqual(plan.slice(0, cut.length));
      expect(total).toBeLessThanOrEqual(600 + 1e-6);
      if (total > 1.1 * 300) expect(speedAt(plan, cut.length - 1)).toBeLessThanOrEqual(0.3 * peak);
    }
  });
});

describe("keepInView (SC 210 fix 2)", () => {
  it("moves samples inside but never the target", async () => {
    const { keepInView } = await import("../src/pacing.js");
    const steps = [{ x: 10, y: 5, sleepMs: 8 }, { x: -12, y: -3, sleepMs: 8 }, { x: 1290, y: 400, sleepMs: 8 }, { x: 1279, y: 799, sleepMs: 8 }];
    expect(keepInView(steps, 1280, 800).map((s) => [s.x, s.y])).toEqual([[10, 5], [2, 2], [1279, 400], [1279, 799]]);
    const aimedOut = [{ x: 10, y: 5, sleepMs: 8 }, { x: 1300, y: 900, sleepMs: 8 }];
    expect(keepInView(aimedOut, 1280, 800)).toBe(aimedOut);
  });
});
