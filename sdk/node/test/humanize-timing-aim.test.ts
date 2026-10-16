/**
 * Lane B (SC 232): per-persona timing and persona-seeded aim -- a port of
 * sdk/python/tests/test_humanize_timing_aim.py (the pure-model half; the fake-page half is the
 * "lane B" block in humanize-forms.test.ts). The three SDKs are pinned to the same values by
 * sdk/testdata/humanize_timing_vectors.json.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import * as M from "../src/motion.js";
import { typoRate } from "../src/humanize.js";

const VEC = JSON.parse(readFileSync(fileURLToPath(new URL("../../testdata/humanize_timing_vectors.json", import.meta.url)), "utf8"));
const IDS = ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"];
const BOX = { x: 100, y: 100, width: 160, height: 40 };
const median = (xs: number[]) => { const s = [...xs].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const close = (a: number, b: number, tol = 1e-9) => Math.abs(a - b) <= tol;

describe("persona", () => {
  it("keeps every field 0.51.0 drew (the new traits come from salted side streams)", () => {
    for (const r of VEC.persona_core) {
      const p = M.makePersona(r.persona);
      expect([p.seed, p.deviceHz, p.tremorHz, p.tremorAmp, p.driftAmp, p.driftTau, p.jitter, p.fittsA, p.fittsB,
        p.primaryFrac, p.overshoot, p.maxCorrections, p.approachBias]).toEqual(r.core);
    }
  });
});

describe("per-persona timing", () => {
  const medians = (fn: (p: M.Persona, r: M.Rng) => number) => IDS.map((id) => {
    const p = M.makePersona(id);
    const rng = M.mulberry32(5); // the same sample stream for every persona: only the persona differs
    return median(Array.from({ length: 400 }, () => fn(p, rng)));
  });
  const cases: [string, (p: M.Persona, r: M.Rng) => number, number, number][] = [
    ["click hold", M.clickHold, 55, 230],
    ["key dwell", M.keyDwell, 45, 120],
    ["grab", (p, r) => M.dragDwell(p, r).grabMs, 130, 360],
    ["release", (p, r) => M.dragDwell(p, r).releaseMs, 90, 230],
  ];
  for (const [name, fn, lo, hi] of cases) {
    it(`${name}: medians differ across seeds, inside ${lo}-${hi} ms`, () => {
      const med = medians(fn);
      expect(new Set(med.map(Math.round)).size, JSON.stringify(med)).toBeGreaterThanOrEqual(4);
      for (const m of med) { expect(m).toBeGreaterThanOrEqual(lo); expect(m).toBeLessThanOrEqual(hi); }
      const p = M.makePersona("band"), rng = M.mulberry32(1);
      for (let i = 0; i < 2000; i++) { const v = fn(p, rng); expect(v).toBeGreaterThanOrEqual(lo); expect(v).toBeLessThanOrEqual(hi); }
    });
  }

  const ENV = "CLEARCOTE_HUMANIZE_TYPO_RATE";
  const saved = process.env[ENV];
  afterEach(() => { if (saved === undefined) delete process.env[ENV]; else process.env[ENV] = saved; });

  it("typo rate is per persona (0.5-4 %) and the env var still overrides", () => {
    delete process.env[ENV];
    const rates = IDS.map((id) => typoRate(M.makePersona(id)));
    expect(new Set(rates.map((r) => r.toFixed(4))).size).toBeGreaterThanOrEqual(4);
    for (const r of rates) { expect(r).toBeGreaterThanOrEqual(0.005); expect(r).toBeLessThanOrEqual(0.04); }
    expect(typoRate()).toBe(0.02);
    process.env[ENV] = "0.3";
    expect(typoRate(M.makePersona("ha-1"))).toBe(0.3);
  });
});

describe("clickPoint", () => {
  const landings = (id: string, n = 200) => { const p = M.makePersona(id); return Array.from({ length: n }, () => M.clickPoint(BOX, { x: 0, y: 0 }, p)); };

  it("is reproducible per seed and differs across seeds", () => {
    expect(landings("ha-1")).toEqual(landings("ha-1"));
    for (const id of IDS.slice(1)) expect(landings(id)).not.toEqual(landings("ha-1"));
  });

  it("disperses 0.1-0.2 of the box, per persona", () => {
    const sds = IDS.map((id) => {
      const xs = landings(id, 600).map((p) => p.x);
      const mu = xs.reduce((a, b) => a + b, 0) / xs.length;
      return Math.sqrt(xs.reduce((a, b) => a + (b - mu) ** 2, 0) / xs.length) / BOX.width;
    });
    for (const s of sds) { expect(s).toBeGreaterThanOrEqual(0.08); expect(s).toBeLessThanOrEqual(0.21); }
    expect(Math.max(...sds) - Math.min(...sds)).toBeGreaterThan(0.02);
  });

  it("works without a persona", () => {
    for (let i = 0; i < 50; i++) {
      const pt = M.clickPoint(BOX, { x: 0, y: 0 }, null);
      expect(pt.x).toBeGreaterThanOrEqual(BOX.x + 2); expect(pt.x).toBeLessThanOrEqual(BOX.x + BOX.width - 2);
    }
  });
});

describe("shared timing vectors (Python, Node and .NET agree)", () => {
  it.each((VEC.timing_traits as any[]).map((t) => [t.persona, t]))("timingTraits %s", (_, t: any) => {
    const tt = M.timingTraits(M.makePersona(t.persona));
    for (const [k, py] of [["clickHoldMs", "click_hold_ms"], ["keyDwellMs", "key_dwell_ms"], ["grabMs", "grab_ms"],
      ["releaseMs", "release_ms"], ["dblclickGapMs", "dblclick_gap_ms"], ["pauseScale", "pause_scale"], ["typoRate", "typo_rate"]]) {
      expect(close((tt as any)[k], t[py], 1e-12), k).toBe(true);
    }
  });
  it.each((VEC.aim_traits as any[]).map((t) => [t.persona, t]))("aimTraits %s", (_, t: any) => {
    const a = M.aimTraits(M.makePersona(t.persona));
    expect(close(a.spread, t.spread, 1e-12) && close(a.undershoot, t.undershoot, 1e-12) && close(a.sideBias, t.side_bias, 1e-12)).toBe(true);
  });
  it.each((VEC.draws as any[]).map((d) => [d.persona, d]))("draws %s", (_, d: any) => {
    const p = M.makePersona(d.persona);
    const r = M.mulberry32(d.rng);
    const { grabMs, releaseMs } = M.dragDwell(p, r);
    // drawn in the generator's order: drag_dwell, click_hold, key_dwell -- see gen_timing_vectors.py
    const got = { grab: grabMs, release: releaseMs, click_hold: M.clickHold(p, r), key_dwell: M.keyDwell(p, r),
      dblclick_gap: M.dblclickGap(p, r), pause_220_480: M.pauseMs(p, 220, 480, r) };
    for (const [k, v] of Object.entries(got)) expect(close(v, d[k]), `${k} ${v} vs ${d[k]}`).toBe(true);
    const cp = M.clickPoint(VEC.box, { x: VEC.from[0], y: VEC.from[1] }, p, r);
    expect(close(cp.x, d.click_point_rng[0]) && close(cp.y, d.click_point_rng[1])).toBe(true);
    const fresh = M.makePersona(d.persona);
    for (const want of d.click_point_stream) {
      const pt = M.clickPoint(VEC.box, { x: VEC.from[0], y: VEC.from[1] }, fresh);
      expect(close(pt.x, want[0]) && close(pt.y, want[1]), JSON.stringify([pt, want])).toBe(true);
    }
  });
});
