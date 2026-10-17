/**
 * SC 232 integration (lane G's score hook in lanes C/D/E; mirrors python/tests/test_humanize_trace_taps.py): with
 * CLEARCOTE_HUMANIZE_SCORE=1 every humanized surface records a scored line -- a hover dwell as "hover", a held leg
 * as "drag" (the held button on every sample), ambient motion as "ambient", a typed run as one "typed" line of the
 * characters' down/up, and one "wheel" line per mouse.wheel call -- on the forms fake page. Off by default: nothing
 * is written.
 */
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { attached } from "./helpers/humanize-fake.js";
import { walk } from "../src/pacing.js";
import { makePersona, mulberry32, planDrag } from "../src/motion.js";

const ENVS = ["CLEARCOTE_HUMANIZE_SCORE", "CLEARCOTE_HUMANIZE_SCORE_FILE", "CLEARCOTE_HUMANIZE_TYPO_RATE", "CLEARCOTE_NO_WARN",
  "CLEARCOTE_HUMANIZE_ENGINE_CLICKS"];
const saved: Record<string, string | undefined> = {};
let dir = "";
let file = "";
beforeEach(() => {
  for (const k of ENVS) saved[k] = process.env[k];
  dir = mkdtempSync(join(tmpdir(), "cc-taps-"));
  file = join(dir, "score.jsonl");
  process.env.CLEARCOTE_HUMANIZE_SCORE_FILE = file;
  process.env.CLEARCOTE_HUMANIZE_TYPO_RATE = "0";
  process.env.CLEARCOTE_NO_WARN = "1";
  delete process.env.CLEARCOTE_HUMANIZE_ENGINE_CLICKS;
});
afterEach(() => {
  for (const k of ENVS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
  rmSync(dir, { recursive: true, force: true });
});
const lines = (): any[] => (existsSync(file) ? readFileSync(file, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : []);

async function drive(page: any): Promise<void> {
  await page.hover("#t");                 // a glide, then the hover dwell (lane C)
  await page.mouse.down();                // a held leg = a drag (lane E)
  await page.mouse.move(420, 300);
  await page.mouse.up();
  await page.ambientMotion(300);          // ambient (lane C's planner)
  await page.keyboard.type("hello world");   // one typed run (lane D)
  await page.mouse.wheel(0, 300);         // one wheel call (lane E)
}

describe("score hook taps (lane G in lanes C/D/E)", () => {
  it("every humanized surface records its kind", async () => {
    process.env.CLEARCOTE_HUMANIZE_SCORE = "1";
    const f = await attached({ field: { tag: "input", type: "text" } });
    await drive(f.page);
    const ls = lines();
    const kinds = ls.map((l) => l.kind);
    for (const k of ["glide", "hover", "drag", "ambient", "typed", "wheel"]) expect(kinds, k).toContain(k);
    const typed = ls.filter((l) => l.kind === "typed");
    expect(typed).toHaveLength(1);
    expect(typed[0].n).toBe("hello world".length);
    expect(typed[0].meta).toEqual({ n: 11, typos: false, floor_ms: 0 });
    const wheel = ls.filter((l) => l.kind === "wheel");
    expect(wheel).toHaveLength(1);
    expect(wheel[0].n).toBeGreaterThanOrEqual(1);
    expect(wheel[0].meta.dy).toBe(300);
    const drag = ls.filter((l) => l.kind === "drag");
    expect(drag.length).toBeGreaterThan(0);
    expect(drag[0].features.heldN, "the held button is on every drag sample").toBe(drag[0].n);
    const hover = ls.filter((l) => l.kind === "hover");
    expect(hover[0].n).toBeGreaterThanOrEqual(3);
    for (const l of ls) { expect(l.score).toBeGreaterThanOrEqual(0); expect(l.score).toBeLessThanOrEqual(1); expect(Array.isArray(l.flags)).toBe(true); }
  }, 60_000);

  it("off by default: nothing is written", async () => {
    delete process.env.CLEARCOTE_HUMANIZE_SCORE;
    const f = await attached({ field: { tag: "input", type: "text" } });
    await drive(f.page);
    expect(existsSync(file)).toBe(false);
  }, 60_000);

  it("walk carries the held button on every drag sample", async () => {
    process.env.CLEARCOTE_HUMANIZE_SCORE = "1";
    let t = 0;
    const steps = planDrag({ x: 100, y: 100 }, { x: 400, y: 300 }, makePersona("ha-1"), { rng: mulberry32(3) });
    await walk(steps, async () => undefined, { now: () => t, sleep: async (ms) => { t += Math.max(0, ms); }, kind: "drag", buttons: 1 });
    const ls = lines();
    expect(ls).toHaveLength(1);
    expect(ls[0].kind).toBe("drag");
    expect(ls[0].features.heldN).toBe(ls[0].n);
    expect(ls[0].n).toBe(steps.length);
  });
});
