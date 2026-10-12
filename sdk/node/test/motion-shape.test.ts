/**
 * Path SHAPE of the humanize planner (SC 210 lane S), measured on the planned points -- a port of
 * sdk/python/tests/test_motion_shape.py. Metrics are the SC 209 Humanize Lab's (analyze.py
 * seg_metrics). Measured on r38 / SDK 0.48.0 the planned aimed moves were 0.99 straight and passed the
 * target in 1 of 36 approaches; a held drag wandered up to 47 px off its line. The three SDKs are pinned
 * to identical paths by sdk/testdata/humanize_motion_vectors.json.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import * as M from "../src/motion.js";
import { makePersona, mulberry32, planMove, type Point, type Step } from "../src/motion.js";

const VEC = JSON.parse(readFileSync(fileURLToPath(new URL("../../testdata/humanize_motion_vectors.json", import.meta.url)), "utf8"));

const IDS = ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"];
const TARGETS: [number, number, number, number][] = [
  [150, 142, 120, 44], [902, 613, 44, 26], [515, 377, 90, 34], [1040, 138, 160, 56], [198, 632, 36, 24], [675, 205, 70, 30],
];

const median = (xs: number[]) => { const s = [...xs].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };

/** SC 209 seg_metrics on a planned path; `quantize` = what a DPR-1 page sees (whole pixels, no
 * event while the pixel is unchanged). */
function pathShape(from: Point, steps: Step[], quantize = false) {
  const pts: [number, number, number][] = [[from.x, from.y, 0]];
  let t = 0;
  for (const s of steps) {
    let x = s.x, y = s.y;
    if (quantize) {
      x = Math.floor(x); y = Math.floor(y);
      const l = pts[pts.length - 1];
      if (x === l[0] && y === l[1]) { t += s.sleepMs; continue; }
    }
    pts.push([x, y, t]);
    t += s.sleepMs;
  }
  const [x0, y0] = pts[0];
  const [x1, y1, t1] = pts[pts.length - 1];
  const d = Math.hypot(x1 - x0, y1 - y0);
  const cum = [0];
  for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
  const ux = (x1 - x0) / d, uy = (y1 - y0) / d;
  const over = Math.max(0, Math.max(...pts.map((p) => (p[0] - x0) * ux + (p[1] - y0) * uy)) - d);
  const dev = Math.max(...pts.map((p) => Math.abs((p[0] - x0) * uy - (p[1] - y0) * ux)));
  const ts = pts.map((p) => p[2]);
  const distAt = (tt: number) => {
    if (tt <= ts[0]) return 0;
    if (tt >= ts[ts.length - 1]) return cum[cum.length - 1];
    let lo = 0, hi = ts.length - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (ts[mid] <= tt) lo = mid; else hi = mid; }
    if (ts[hi] === ts[lo]) return cum[hi];
    return cum[lo] + ((tt - ts[lo]) / (ts[hi] - ts[lo])) * (cum[hi] - cum[lo]);
  };
  const prof: number[] = [];
  for (let k = 0; k <= Math.floor(t1 / 10); k++) prof.push((distAt(10 * k + 40) - distAt(10 * k - 40)) / 80);
  const vmax = Math.max(...prof);
  let peaks = 0;
  for (let k = 1; k < prof.length - 1; k++) {
    if (!(prof[k] >= prof[k - 1] && prof[k] > prof[k + 1] && prof[k] > 0.2 * vmax)) continue;
    let lo = k; while (lo > 0 && prof[lo - 1] <= prof[k]) lo--;
    let hi = k; while (hi < prof.length - 1 && prof[hi + 1] <= prof[k]) hi++;
    if (prof[k] - Math.max(Math.min(...prof.slice(lo, k + 1)), Math.min(...prof.slice(k, hi + 1))) >= 0.35 * vmax) peaks++;
  }
  return { eff: d / cum[cum.length - 1], over, dev, peaks, tPeak: (prof.indexOf(vmax) + 0.5) / prof.length };
}

function aimedMoves(rounds = 12) {
  const out: { from: Point; steps: Step[]; to: Point }[] = [];
  IDS.forEach((pid, pi) => {
    const p = makePersona(pid);
    for (let r = 0; r < rounds; r++) {
      const rng = mulberry32(1000 * pi + r);
      let cur = { x: 260, y: 165 };
      for (const [cx, cy, w, h] of TARGETS) {
        const to = { x: cx, y: cy };
        out.push({ from: cur, steps: planMove(cur, to, p, { targetW: Math.min(w, h), rng }), to });
        cur = to;
      }
    }
  });
  return out;
}

function sliderDrags(rounds = 16) {
  const out: { from: Point; steps: Step[]; to: Point }[] = [];
  IDS.forEach((pid, pi) => {
    const p = makePersona(pid);
    for (let r = 0; r < rounds; r++) {
      const rng = mulberry32(7000 + 100 * pi + r);
      const fx = 40 + rng() * 60;
      const fy = 300 + rng() * 40;
      const tx = fx + 300 + rng() * 100;
      const ty = fy + (rng() - 0.5) * 6;
      const from = { x: fx, y: fy }, to = { x: tx, y: ty };
      out.push({ from, steps: planMove(from, to, p, { settle: true, rng }), to });
    }
  });
  return out;
}

describe("aimed moves", () => {
  it("are as straight as real reaches, not ruler lines", () => {
    for (const q of [false, true]) {
      const effs = aimedMoves().map((m) => pathShape(m.from, m.steps, q).eff);
      const med = median(effs);
      expect(med).toBeGreaterThanOrEqual(0.85);
      expect(med).toBeLessThanOrEqual(0.95);
      expect(effs.filter((e) => e > 0.995).length / effs.length).toBeLessThan(0.1);
    }
  });

  it("overshoot and come back in 15-40 % of moves", () => {
    const moves = aimedMoves();
    const share = moves.filter((m) => pathShape(m.from, m.steps).over > 2).length / moves.length;
    expect(share).toBeGreaterThanOrEqual(0.15);
    expect(share).toBeLessThanOrEqual(0.4);
  });

  it("overshoot rate is a per-persona trait", () => {
    for (const pid of IDS) {
      const p = makePersona(pid);
      const rate = M.shapeTraits(p).overshootRate;
      expect(rate).toBeGreaterThanOrEqual(0.15);
      expect(rate).toBeLessThanOrEqual(0.38);
      const rng = mulberry32(55);
      const n = 300;
      let hits = 0;
      for (let i = 0; i < n; i++) {
        const fx = 100 + rng() * 200, fy = 100 + rng() * 200;
        const tx = fx + 150 + rng() * 500, ty = fy + (rng() - 0.5) * 400;
        const from = { x: fx, y: fy };
        if (pathShape(from, planMove(from, { x: tx, y: ty }, p, { targetW: 30, rng })).over > 2) hits++;
      }
      expect(Math.abs(hits / n - rate)).toBeLessThan(0.08);
    }
  });

  it("lands exactly on the target", () => {
    for (const m of aimedMoves(4)) {
      const l = m.steps[m.steps.length - 1];
      expect([l.x, l.y]).toEqual([m.to.x, m.to.y]);
    }
  });

  it("reach peak speed before the halfway point", () => {
    const shapes = aimedMoves().map((m) => pathShape(m.from, m.steps));
    const tp = median(shapes.map((s) => s.tPeak));
    expect(tp).toBeGreaterThanOrEqual(0.2);
    expect(tp).toBeLessThan(0.45);
    expect(median(shapes.map((s) => s.peaks))).toBeLessThanOrEqual(2);
  });

  it("keep the Fitts duration cap", () => {
    const p = makePersona("fitts-shape");
    for (let k = 0; k < 40; k++) {
      const st = planMove({ x: 10, y: 10 }, { x: 1900, y: 1000 }, p, { targetW: 8, rng: mulberry32(k) });
      expect(st.reduce((a, s) => a + s.sleepMs, 0)).toBeLessThanOrEqual(1700 * 1.12 + 240);
    }
  });

  it("are deterministic per seed", () => {
    const a = planMove({ x: 0, y: 0 }, { x: 500, y: 300 }, makePersona("det"), { targetW: 20, rng: mulberry32(9) });
    const b = planMove({ x: 0, y: 0 }, { x: 500, y: 300 }, makePersona("det"), { targetW: 20, rng: mulberry32(9) });
    const c = planMove({ x: 0, y: 0 }, { x: 500, y: 300 }, makePersona("det-2"), { targetW: 20, rng: mulberry32(9) });
    expect(a).toEqual(b);
    expect(a).not.toEqual(c);
  });
});

describe("held drags", () => {
  it("stay on a 40 px slider track", () => {
    for (const q of [false, true]) {
      const shapes = sliderDrags().map((m) => pathShape(m.from, m.steps, q));
      const effs = shapes.map((s) => s.eff).sort((a, b) => a - b);
      expect(effs[Math.floor(effs.length / 10)]).toBeGreaterThanOrEqual(0.93);
      expect(Math.max(...shapes.map((s) => s.dev))).toBeLessThanOrEqual(8);
    }
  });

  it("take 0.5-2 s for 300-400 px", () => {
    for (const m of sliderDrags()) {
      const dur = m.steps.reduce((a, s) => a + s.sleepMs, 0);
      expect(dur).toBeGreaterThanOrEqual(500);
      expect(dur).toBeLessThanOrEqual(2000);
    }
  });

  it("sometimes overshoot and pull back, then land exactly", () => {
    const drags = sliderDrags(40);
    const share = drags.filter((m) => pathShape(m.from, m.steps).over > 2).length / drags.length;
    expect(share).toBeGreaterThanOrEqual(0.04);
    expect(share).toBeLessThanOrEqual(0.35);
    for (const m of drags) {
      const l = m.steps[m.steps.length - 1];
      expect([l.x, l.y]).toEqual([m.to.x, m.to.y]);
    }
  });

  it("a move with the button held is planned as a drag", () => {
    const p = makePersona("held");
    let worst = 0;
    for (let k = 0; k < 40; k++) {
      const st = planMove({ x: 100, y: 300 }, { x: 460, y: 302 }, p, { held: true, rng: mulberry32(k) });
      worst = Math.max(worst, pathShape({ x: 100, y: 300 }, st).dev);
      const l = st[st.length - 1];
      expect([l.x, l.y]).toEqual([460, 302]);
    }
    expect(worst).toBeLessThanOrEqual(8);
  });
});

describe("shared motion vectors (Python, Node and .NET plan the same path)", () => {
  it.each((VEC.shape_traits as any[]).map((t) => [t.persona, t]))("shapeTraits %s", (_, t: any) => {
    const st = M.shapeTraits(makePersona(t.persona));
    expect(Math.abs(st.overshootRate - t.overshoot_rate)).toBeLessThan(1e-12);
    expect(Math.abs(st.curve - t.curve)).toBeLessThan(1e-12);
    expect(Math.abs(st.dragTempo - t.drag_tempo)).toBeLessThan(1e-12);
  });

  it.each((VEC.paths as any[]).map((c, i) => [i, c]))("path #%i", (_, c: any) => {
    const steps = planMove({ x: c.from[0], y: c.from[1] }, { x: c.to[0], y: c.to[1] }, makePersona(c.persona), {
      targetW: c.target_w, settle: c.mode === "settle", held: c.mode === "held", rng: mulberry32(c.rng),
    });
    expect(steps.length).toBe(c.n);
    expect(Math.abs(steps.reduce((a, s) => a + s.sleepMs, 0) - c.total_ms)).toBeLessThan(1e-6);
    for (const [i, x, y, sl] of c.samples) {
      expect(Math.abs(steps[i].x - x)).toBeLessThan(1e-6);
      expect(Math.abs(steps[i].y - y)).toBeLessThan(1e-6);
      expect(Math.abs(steps[i].sleepMs - sl)).toBeLessThan(1e-6);
    }
  });
});
