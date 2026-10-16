/**
 * SC 232 lane G (mirrors sdk/python/tests/test_motion_vectors_sc232.py): planDrag, planAmbient, the persona
 * timings and the wheel scroll-anchor model are pinned across the three SDKs by
 * sdk/testdata/humanize_motion_vectors.json (drags / ambient / timings / wheel_anchor). The anchor is inside
 * the humanize closures today and differs per SDK; its test checks a pure wheelAnchorPoint(vw, vh, rng) once
 * lane E exports one from motion.ts and is skipped ("API missing") until then.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import * as M from "../src/motion.js";
import { clickHold, dragDwell, keyDwell, makePersona, mulberry32, planAmbient, planDrag, rolloverRate, type Step } from "../src/motion.js";

const VEC = JSON.parse(readFileSync(fileURLToPath(new URL("../../testdata/humanize_motion_vectors.json", import.meta.url)), "utf8"));

function check(steps: Step[], c: any) {
  expect(steps.length).toBe(c.n);
  expect(steps.reduce((a, s) => a + s.sleepMs, 0)).toBeCloseTo(c.total_ms, 6);
  for (const [i, x, y, sl] of c.samples) {
    expect(steps[i].x).toBeCloseTo(x, 6);
    expect(steps[i].y).toBeCloseTo(y, 6);
    expect(steps[i].sleepMs).toBeCloseTo(sl, 6);
  }
}

describe("SC 232 vectors", () => {
  for (const c of VEC.drags) {
    it(`planDrag ${c.persona}`, () => {
      check(planDrag({ x: c.from[0], y: c.from[1] }, { x: c.to[0], y: c.to[1] }, makePersona(c.persona), { settle: c.settle, rng: mulberry32(c.rng) }), c);
    });
  }
  for (const c of VEC.ambient) {
    it(`planAmbient ${c.persona}`, () => {
      check(planAmbient({ x: c.from[0], y: c.from[1] }, c.viewport, makePersona(c.persona), c.ms, mulberry32(c.rng)), c);
    });
  }
  for (const c of VEC.timings) {
    it(`timings ${c.persona}`, () => {
      const p = makePersona(c.persona);
      const r = mulberry32(c.rng);
      const d = dragDwell(p, r);
      expect(d.grabMs).toBeCloseTo(c.grab_ms, 9);
      expect(d.releaseMs).toBeCloseTo(c.release_ms, 9);
      expect(clickHold(p, r)).toBeCloseTo(c.click_hold, 9);
      expect(keyDwell(p, r)).toBeCloseTo(c.key_dwell, 9);
      expect(rolloverRate(p)).toBeCloseTo(c.rollover_rate, 12);
    });
  }
  it("wheel anchor model is the PLAN.md lane E model", () => {
    expect(VEC.wheel_anchor.model).toEqual({ x: { mean: 0.5, sd: 0.1, lo: 0.12, hi: 0.88 }, y: { mean: 0.34, sd: 0.09, lo: 0.1, hi: 0.62 }, target_w: 140 });
  });
  const anchor = M.wheelAnchorPoint as (vw: number, vh: number, rng: () => number) => { x: number; y: number } | [number, number];
  for (const c of VEC.wheel_anchor.cases) {
    it(`wheelAnchorPoint rng ${c.rng} (lane E: one pure model in all three SDKs)`, () => {
      const r = mulberry32(c.rng);
      for (const [x, y] of c.points) {
        const g = anchor!(c.viewport[0], c.viewport[1], r);
        const [gx, gy] = Array.isArray(g) ? g : [g.x, g.y];
        expect(gx).toBeCloseTo(x, 6);
        expect(gy).toBeCloseTo(y, 6);
      }
    });
  }
});
