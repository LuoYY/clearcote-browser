/**
 * SC 232 lane G (mirrors sdk/python/tests/test_motionscore_actions.py): scoreAction scores every humanize
 * surface, CLEARCOTE_HUMANIZE_SCORE records each walked path, and the three SDKs score the shared traces in
 * sdk/testdata/humanize_motion_vectors.json `scores` identically. On origin/main ceb7983 these fail:
 * scoreAction / fingerOf / trace.ts do not exist and nothing is exported from the package index.
 */
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as idx from "../src/index.js";
import { fingerOf, scoreAction, typedFeatures } from "../src/motionscore.js";
import { makePersona, mulberry32, planAmbient, planMove } from "../src/motion.js";
import { walk } from "../src/pacing.js";
import { record } from "../src/trace.js";
import { resetSeenWarnings } from "../src/warnings.js";

const VEC = JSON.parse(readFileSync(fileURLToPath(new URL("../../testdata/humanize_motion_vectors.json", import.meta.url)), "utf8"));
const kase = (kind: string, name: string) => VEC.scores.find((c: any) => c.kind === kind && c.name === name);
const names = (fl: string[]) => fl.map((f) => f.split("(")[0]);

describe("finger map", () => {
  it("QWERTY hands and fingers", () => {
    expect(fingerOf("a")).toEqual([0, 0]);
    expect(fingerOf("f")).toEqual([0, 3]);
    expect(fingerOf("G")).toEqual([0, 3]);
    expect(fingerOf("j")).toEqual([1, 3]);
    expect(fingerOf("p")).toEqual([1, 0]);
    expect(fingerOf(" ")).toEqual([2, 4]);
    expect(fingerOf("Space")).toEqual([2, 4]);
    expect(fingerOf("Enter")).toBeNull();
  });
});

describe("scoreAction", () => {
  for (const [kind, name] of [["glide", "straight"], ["typed", "robot"], ["wheel", "metronome"], ["hover", "none"], ["drag", "teleport"]]) {
    it(`bad ${kind}/${name} scores <= 0.5`, () => {
      const r = scoreAction(kind, kase(kind, name).samples);
      expect(r.score).toBeLessThanOrEqual(0.5);
      expect(r.flags.length).toBeGreaterThan(0);
    });
  }
  for (const [kind, name] of [["glide", "planned"], ["typed", "human"], ["wheel", "notch-bursts"], ["wheel", "trackpad"],
    ["hover", "dwell"], ["drag", "hesitant"], ["ambient", "planned"]]) {
    it(`human ${kind}/${name} scores >= 0.7`, () => {
      expect(scoreAction(kind, kase(kind, name).samples).score).toBeGreaterThanOrEqual(0.7);
    });
  }
  it("same-finger bigrams read slower than alternating-hand ones", () => {
    const f = typedFeatures(kase("typed", "human").samples);
    expect(f.sameFingerRatio).toBeGreaterThan(1.05);
  });
  it("unknown kind throws", () => { expect(() => scoreAction("teleport", [])).toThrow(); });
  for (const c of VEC.scores) {
    it(`shared vector ${c.kind}/${c.name}`, () => {
      const r = scoreAction(c.kind, c.samples);
      expect(r.score).toBeCloseTo(c.score, 9);
      expect(names(r.flags)).toEqual(names(c.flags));
    });
  }
  it("is exported from the package index", () => {
    expect(typeof (idx as any).scoreAction).toBe("function");
    expect(typeof (idx as any).scoreMotion).toBe("function");
    expect(typeof (idx as any).recordHumanizeAction).toBe("function");
  });
});

describe("CLEARCOTE_HUMANIZE_SCORE hook", () => {
  let dir = "";
  let file = "";
  const env = { ...process.env };
  let clock = 0;
  const now = () => clock;
  const sleep = async (ms: number) => { clock += Math.max(0, ms); };
  const lines = () => readFileSync(file, "utf8").trim().split("\n").map((l) => JSON.parse(l));

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "cc-score-"));
    file = join(dir, "score.jsonl");
    process.env.CLEARCOTE_HUMANIZE_SCORE = "1";
    process.env.CLEARCOTE_HUMANIZE_SCORE_FILE = file;
    delete process.env.CLEARCOTE_NO_WARN;
    resetSeenWarnings();
    clock = 0;
  });
  afterEach(() => {
    process.env = { ...env };
    vi.restoreAllMocks();
    rmSync(dir, { recursive: true, force: true });
  });

  it("walk writes one line per walked path with the actual dispatch times", async () => {
    const steps = planMove({ x: 100, y: 100 }, { x: 800, y: 500 }, makePersona("ha-1"), { targetW: 40, rng: mulberry32(5) });
    await walk(steps, async () => undefined, { now, sleep });
    await walk(planAmbient({ x: 300, y: 300 }, { width: 1180, height: 760 }, makePersona("ha-2"), 800, mulberry32(2)),
      async () => undefined, { now, sleep, kind: "ambient" });
    const ls = lines();
    expect(ls.map((l) => l.kind)).toEqual(["glide", "ambient"]);
    expect(ls[0].n).toBe(steps.length);
    expect(ls[0].meta.sent).toBe(steps.length);
    expect(ls[0].features.durationMs).toBeGreaterThan(0);
  });

  it("a slow dispatch: the line holds what went out, not the plan", async () => {
    const steps = planMove({ x: 100, y: 100 }, { x: 800, y: 500 }, makePersona("ha-1"), { targetW: 40, rng: mulberry32(5) });
    const sent = await walk(steps, async () => { clock += 25; }, { now, sleep });
    const l = lines()[0];
    expect(sent).toBeLessThan(steps.length);
    expect(l.n).toBe(sent);
  });

  it("a bad path warns naming the penalties", async () => {
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    const bad = Array.from({ length: 60 }, (_, i) => ({ x: 100 + i * 5, y: 100, sleepMs: 8 }));
    await walk(bad, async () => undefined, { now, sleep });
    expect(lines()[0].score).toBeLessThan(0.5);
    const out = err.mock.calls.map((c) => String(c[0])).join("");
    expect(out).toContain("clearcote: warning: humanize glide scored");
    expect(out).toContain("no-corrective-submovement");
  });

  it("off by default and with =0", async () => {
    delete process.env.CLEARCOTE_HUMANIZE_SCORE;
    await walk([{ x: 1, y: 1, sleepMs: 8 }, { x: 5, y: 5, sleepMs: 8 }], async () => undefined, { now, sleep });
    process.env.CLEARCOTE_HUMANIZE_SCORE = "0";
    await walk([{ x: 1, y: 1, sleepMs: 8 }, { x: 5, y: 5, sleepMs: 8 }], async () => undefined, { now, sleep });
    expect(existsSync(file)).toBe(false);
    expect(readdirSync(dir)).toEqual([]);
  });

  it("a failing tap never breaks the walk", async () => {
    process.env.CLEARCOTE_HUMANIZE_SCORE_FILE = join(dir, "no-such-dir", "x.jsonl");
    expect(await walk([{ x: 1, y: 1, sleepMs: 8 }, { x: 50, y: 5, sleepMs: 8 }], async () => undefined, { now, sleep })).toBe(2);
  });

  it("record() is the tap the typing / wheel / hover loops call", () => {
    process.env.CLEARCOTE_NO_WARN = "1";
    const r = record("typed", kase("typed", "robot").samples, { field: "#name" });
    expect(r?.kind).toBe("typed");
    const l = lines()[0];
    expect(l.meta).toEqual({ field: "#name" });
    expect(l.score).toBeLessThan(0.5);
    expect(record("wheel", [])).toBeNull();
  });
});
