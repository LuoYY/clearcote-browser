// pressAndHold's plan (hold.ts): hold length, drift, release reaction, press point -- and the driver's
// release guarantees, against a fake page. Mirrors sdk/python/tests/test_press_and_hold.py.
import { describe, expect, it } from "vitest";
import {
  holdMs, tremorPlan, reactionMs, holdPoint, holdButton, pressAndHold,
  UNTIL_CAP_MS, TREMOR_RADIUS_PX,
} from "../src/hold.js";
import { makePersona, mulberry32 } from "../src/motion.js";
import * as index from "../src/index.js";

describe("holdMs", () => {
  it("takes the duration exactly", () => {
    expect(holdMs(1234)).toBe(1234);
    expect(holdMs("500")).toBe(500);
  });

  for (const bad of [0, -5, Number.NaN, Number.POSITIVE_INFINITY, "soon", ""] as const) {
    it(`rejects ${String(bad)}`, () => { expect(() => holdMs(bad as never)).toThrow(/pressAndHold: duration/); });
  }

  it("draws 2.8-3.8 s by default and caps an until at 15 s", () => {
    for (let i = 0; i < 200; i++) {
      const v = holdMs();
      expect(v).toBeGreaterThanOrEqual(2800);
      expect(v).toBeLessThanOrEqual(3800);
    }
    expect(holdMs(undefined, () => true)).toBe(UNTIL_CAP_MS);
    expect(UNTIL_CAP_MS).toBe(15000);
    expect(holdMs(4000, () => true)).toBe(4000);
  });
});

describe("tremorPlan", () => {
  it("stays near the press point and is sparse", () => {
    const rng = mulberry32(7);
    for (let k = 0; k < 50; k++) {
      const plan = tremorPlan(10000, rng);
      expect(plan.length, "a 10 s hold drifts at least once").toBeGreaterThan(0);
      let prevT = 0, prev: [number, number] = [0, 0];
      for (const [t, dx, dy] of plan) {
        expect(t).toBeGreaterThan(prevT);
        expect(t).toBeLessThan(10000 - 120);
        expect(Math.abs(dx)).toBeLessThanOrEqual(TREMOR_RADIUS_PX);
        expect(Math.abs(dy)).toBeLessThanOrEqual(TREMOR_RADIUS_PX);
        expect(Number.isInteger(dx) && Number.isInteger(dy)).toBe(true);
        expect([dx, dy], "every planned move moves").not.toEqual(prev);
        expect(Math.abs(dx - prev[0]), "one pixel at a time").toBeLessThanOrEqual(1);
        expect(Math.abs(dy - prev[1]), "one pixel at a time").toBeLessThanOrEqual(1);
        prevT = t; prev = [dx, dy];
      }
      for (let i = 1; i < plan.length; i++) {
        const g = plan[i][0] - plan[i - 1][0];
        expect(g).toBeGreaterThanOrEqual(250);
        expect(g).toBeLessThanOrEqual(900);
      }
      expect(plan[0][0]).toBeGreaterThanOrEqual(250);
      expect(plan[0][0]).toBeLessThanOrEqual(650);
    }
  });

  it("plans no drift for a short hold", () => {
    // the first drift comes 250-650 ms in, and none in the last 120 ms
    expect(tremorPlan(300, () => 0)).toEqual([]);
    expect(tremorPlan(700, () => 0.99)).toEqual([]);
  });
});

describe("reactionMs / holdPoint / holdButton", () => {
  it("reacts in 160-350 ms", () => {
    const rng = mulberry32(3);
    const vals = Array.from({ length: 500 }, () => reactionMs(rng));
    expect(Math.min(...vals)).toBeGreaterThanOrEqual(160);
    expect(Math.max(...vals)).toBeLessThanOrEqual(350);
  });

  it("presses at the centre, the position, or the persona's point", () => {
    const box = { x: 100, y: 50, width: 200, height: 40 };
    expect(holdPoint(box)).toEqual([200, 70]);
    expect(holdPoint(box, null, null, { x: 10, y: 5 })).toEqual([110, 55]);
    const [x, y] = holdPoint(box, { x: 0, y: 0 }, makePersona("hold-1"));
    expect(x).toBeGreaterThanOrEqual(102); expect(x).toBeLessThanOrEqual(298);
    expect(y).toBeGreaterThanOrEqual(52); expect(y).toBeLessThanOrEqual(88);
  });

  it("takes left/right/middle only", () => {
    expect(() => holdButton("both")).toThrow(/button must be/);
    expect(holdButton(undefined)).toBe("left");
    expect(holdButton("right")).toBe("right");
  });

  it("is exported from the package index", () => {
    expect(index.pressAndHold).toBe(pressAndHold);
  });
});

type Ev = [string, ...unknown[]];
const fakeLocator = (log: Ev[]) => {
  const page = {
    mainFrame: () => ({}),
    mouse: {
      move: async (x: number, y: number) => { log.push(["move", x, y]); },
      down: async (o: any = {}) => { log.push(["down", o.button]); },
      up: async (o: any = {}) => { log.push(["up", o.button]); },
    },
  };
  return {
    page: () => page,
    waitFor: async () => {},
    scrollIntoViewIfNeeded: async () => {},
    boundingBox: async () => ({ x: 10, y: 10, width: 100, height: 30 }),
  } as never;
};

describe("pressAndHold driver", () => {
  it("throws on a bad duration or button before anything moves", async () => {
    const log: Ev[] = [];
    await expect(pressAndHold(fakeLocator(log), { duration: -1 })).rejects.toThrow(/duration/);
    await expect(pressAndHold(fakeLocator(log), { button: "both" as never })).rejects.toThrow(/button/);
    expect(log).toEqual([]);
  });

  it("releases when until throws", async () => {
    const log: Ev[] = [];
    await expect(pressAndHold(fakeLocator(log), {
      duration: 2000, until: () => { throw new Error("until failed"); },
    })).rejects.toThrow("until failed");
    expect(log[log.length - 1]).toEqual(["up", "left"]);
  });

  it("releases early once until is true, after a reaction, and reports it", async () => {
    const log: Ev[] = [];
    let calls = 0;
    const t0 = performance.now();
    const ok = await pressAndHold(fakeLocator(log), {
      duration: 5000, tremor: false, until: async () => ++calls >= 3,
    });
    const took = performance.now() - t0;
    expect(ok).toBe(true);
    expect(took).toBeGreaterThanOrEqual(300);
    expect(took).toBeLessThanOrEqual(1000);
    expect(log.map((e) => e[0])).toEqual(["move", "down", "up"]);
    expect(log[0]).toEqual(["move", 60, 25]); // the centre of the box, no persona on a plain page
  });

  it("returns false when until never becomes true", async () => {
    const log: Ev[] = [];
    expect(await pressAndHold(fakeLocator(log), { duration: 400, until: () => false, tremor: false })).toBe(false);
    expect(log[log.length - 1][0]).toBe("up");
  });

  it("drifts within 2 px of the press point while held, and presses the asked button", async () => {
    const log: Ev[] = [];
    expect(await pressAndHold(fakeLocator(log), { duration: 1600, button: "right", position: { x: 20, y: 15 } })).toBe(true);
    expect(log[0]).toEqual(["move", 30, 25]);
    expect(log[1]).toEqual(["down", "right"]);
    expect(log[log.length - 1]).toEqual(["up", "right"]);
    const drift = log.slice(2, -1);
    expect(drift.length).toBeGreaterThan(0);
    for (const [k, x, y] of drift) {
      expect(k).toBe("move");
      expect(Math.abs((x as number) - 30)).toBeLessThanOrEqual(2);
      expect(Math.abs((y as number) - 25)).toBeLessThanOrEqual(2);
    }
  });
});
