/**
 * Lane E (SC 232): drags and scrolling on a fake page (test/helpers/fake-scroll-page.ts) -- the Node twin
 * of sdk/python/tests/test_humanize_dragscroll.py. Measured on r42 / SDK 0.51.0 before these: dragTo
 * pressed and released on the exact box centres with no hit test and ignored sourcePosition /
 * targetPosition / force / timeout; page.dragAndDrop was not humanized; the held leg started at full
 * speed on the press; an off-screen target was scrolled PROGRAMMATICALLY; a macOS persona scrolled 40 px
 * notches; the wheel anchor used other numbers than Python and .NET. Every test fails on origin/main.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { attachHumanize } from "../src/humanize.js";
import * as M from "../src/motion.js";
import { Doc, ev, fakePage } from "./helpers/fake-scroll-page.js";

const SLOW = 60_000;
const VEC = JSON.parse(readFileSync(fileURLToPath(new URL("../../testdata/humanize_scroll_vectors.json", import.meta.url)), "utf8"));

const dndDoc = () => {
  const d = new Doc();
  d.els.set("#src", { rect: [100, 200, 120, 60] });
  d.els.set("#dst", { rect: [700, 420, 200, 140] });
  return d;
};
const attached = async (d: Doc, seed = "lane-e", extra: Record<string, unknown> = {}) => {
  const p = fakePage(d, extra);
  await attachHumanize({} as never, p, { humanize: true, seed });
  return p;
};
const idx = (d: Doc, kind: string) => d.log.findIndex((e) => e[0] === kind);
const lastMoveBefore = (d: Doc, i: number) => d.log.slice(0, i).filter((e) => e[0] === "move").pop();

describe("drag options", () => {
  it("dragAndDrop honours sourcePosition / targetPosition within 2 px", async () => {
    const d = dndDoc();
    const p = await attached(d);
    await p.dragAndDrop("#src", "#dst", { sourcePosition: { x: 10, y: 12 }, targetPosition: { x: 180, y: 20 } });
    expect(ev(d, "native_dnd")).toEqual([]);
    const press = lastMoveBefore(d, idx(d, "down"));
    const release = lastMoveBefore(d, idx(d, "up"));
    expect(Math.abs(press[1] - 110)).toBeLessThanOrEqual(2);
    expect(Math.abs(press[2] - 212)).toBeLessThanOrEqual(2);
    expect(Math.abs(release[1] - 880)).toBeLessThanOrEqual(2);
    expect(Math.abs(release[2] - 440)).toBeLessThanOrEqual(2);
  }, SLOW);

  it("locator.dragTo honours positions too, and is humanized (>= 10 held moves, grab >= 130 ms)", async () => {
    const d = dndDoc();
    const p = await attached(d);
    await p.locator("#src").dragTo(p.locator("#dst"), { sourcePosition: { x: 100, y: 30 } });
    expect(ev(d, "native_dragTo")).toEqual([]);
    const iDown = idx(d, "down"), iUp = idx(d, "up");
    const press = lastMoveBefore(d, iDown);
    expect(Math.abs(press[1] - 200)).toBeLessThanOrEqual(2);
    const held = d.log.slice(iDown, iUp).filter((e) => e[0] === "move");
    expect(held.length).toBeGreaterThanOrEqual(10);
    const far = held.find((e) => Math.hypot(e[1] - press[1], e[2] - press[2]) >= 10)!;
    expect(far[3] - d.log[iDown][1]).toBeGreaterThanOrEqual(120);
  }, SLOW);

  it("a covered end goes native; force skips the hit test", async () => {
    const d = dndDoc();
    d.covers.push([0, 0, 1280, 800]);
    const p = await attached(d);
    await p.dragAndDrop("#src", "#dst");
    expect(ev(d, "native_dnd").length).toBe(1);
    expect(ev(d, "down").length).toBe(0);
    await p.dragAndDrop("#src", "#dst", { force: true });
    expect(ev(d, "native_dnd").length).toBe(1);
    expect(ev(d, "down").length).toBe(1);
  }, SLOW);

  it("drag-start hesitation: inside 8 px for 100-300 ms after the press", async () => {
    for (const seed of ["ha-1", "ha-4"]) {
      const d = dndDoc();
      const p = await attached(d, seed);
      await p.dragAndDrop("#src", "#dst");
      const iDown = idx(d, "down");
      const press = lastMoveBefore(d, iDown);
      const held = d.log.slice(iDown).filter((e) => e[0] === "move");
      const t0 = d.log[iDown][1];
      const early = held.filter((e) => e[3] - t0 < 90);
      expect(early.length).toBeGreaterThan(0);
      for (const e of early) expect(Math.hypot(e[1] - press[1], e[2] - press[2])).toBeLessThan(8);
      const leg = held.find((e) => Math.hypot(e[1] - press[1], e[2] - press[2]) >= 8)!;
      expect(leg[3] - t0).toBeGreaterThanOrEqual(90);
      expect(leg[3] - t0).toBeLessThanOrEqual(380);
    }
  }, SLOW);
});

describe("dragBy", () => {
  it("converges on a lagging, stepped slider in <= 3 legs", async () => {
    const d = new Doc();
    d.els.set("#thumb", { rect: [200, 300, 20, 20] });
    d.follow.set("#thumb", [0.93, 10]);
    const p = await attached(d);
    const got = await p.dragBy("#thumb", 120, 0);
    expect(Math.abs(got.x - 120)).toBeLessThanOrEqual(2);
    expect(p._clearcoteDragLegs).toBeGreaterThanOrEqual(1);
    expect(p._clearcoteDragLegs).toBeLessThanOrEqual(3);
    expect(d.els.get("#thumb")!.rect[0]).toBe(320);
  }, SLOW);
});

describe("wheel scroll-into-view", () => {
  it("a click on an off-screen target wheels it in (whole notches, no programmatic scroll, 25-60 %)", async () => {
    const d = new Doc();
    d.els.set("#far", { rect: [500, 3000, 160, 40] });
    const p = await attached(d);
    await p.click("#far");
    const w = ev(d, "wheel");
    expect(w.length).toBeGreaterThan(0);
    expect(ev(d, "prog")).toEqual([]);
    for (const e of w) expect(e[1] === 0 && e[2] % 100 === 0).toBe(true);
    const r = d.vrect("#far");
    const frac = (r[1] + r[3] / 2) / d.vh;
    expect(frac).toBeGreaterThanOrEqual(0.18);
    expect(frac).toBeLessThanOrEqual(0.67);
    expect(ev(d, "down").length).toBe(1);
  }, SLOW);

  it("a nested scrolling container is wheeled over", async () => {
    const d = new Doc();
    d.containers.set("#box", { rect: [100, 200, 400, 200], st: 0, inner: 2000 });
    d.els.set("#deep", { rect: [20, 1500, 200, 30], parent: "#box" });
    const p = await attached(d);
    await p.click("#deep");
    const w = ev(d, "wheel");
    expect(w.length).toBeGreaterThan(0);
    expect(ev(d, "prog")).toEqual([]);
    for (const e of w) expect(e[3] >= 100 && e[3] <= 500 && e[4] >= 200 && e[4] <= 400).toBe(true);
    expect(d.containers.get("#box")!.st).toBeGreaterThan(0);
    expect(d.sy).toBe(0);
  }, SLOW);

  it("a non-scrollable page falls back to the programmatic scroll and reports it", async () => {
    const d = new Doc(false);
    d.els.set("#far", { rect: [500, 3000, 160, 40] });
    const reported: any[] = [];
    const p = await attached(d, "lane-e", { _clearcoteReportFallback: (e: any) => reported.push(e) });
    await p.click("#far");
    expect(ev(d, "prog").length).toBeGreaterThan(0);
    expect(p._clearcoteLastFallback).toEqual({ api: "scroll_into_view", reason: "not_scrollable" });
    expect(reported).toEqual([{ api: "scroll_into_view", reason: "not_scrollable" }]);
  }, SLOW);
});

describe("trackpad persona", () => {
  it("a macOS persona scrolls pixel swipes with an inertia tail; a mouse option keeps notches", async () => {
    const d = new Doc(true, 4000, "MacIntel");
    const p = await attached(d);
    await p.mouse.wheel(0, 600);
    const deltas = ev(d, "wheel").map((e) => e[2]);
    expect(Math.abs(deltas.reduce((a, b) => a + b, 0) - 600)).toBeLessThan(1e-6);
    expect(deltas.some((x) => Math.abs(x - Math.round(x)) > 1e-3)).toBe(true);
    expect(deltas.every((x) => Math.abs(x) <= 40)).toBe(true);
    const d2 = new Doc(true, 4000, "MacIntel");
    const p2 = await attached(d2, "lane-e", { _clearcoteInputDevice: "mouse" });
    await p2.mouse.wheel(0, 120);
    expect(ev(d2, "wheel").map((e) => e[2])).toEqual([40, 40, 40]);
  }, SLOW);
});

describe("shared vectors (humanize_scroll_vectors.json)", () => {
  it("anchor, drift, trackpad, hesitation, held drag, need and device match Python", async () => {
    const S: any = await import("../src/scroll.js");
    const close = (a: number, b: number, tol = 1e-6) => expect(Math.abs(a - b)).toBeLessThan(tol);
    for (const v of VEC.anchor) {
      const [x, y] = S.anchorPoint(v.vw, v.vh, M.mulberry32(v.rng));
      close(x, v.x, 1e-9); close(y, v.y, 1e-9);
    }
    for (const v of VEC.drift) {
      const [dx, dy] = S.wheelDrift(M.mulberry32(v.rng));
      close(dx, v.dx, 1e-9); close(dy, v.dy, 1e-9);
    }
    for (const v of VEC.trackpad) {
      const evs = S.planTrackpad(v.dx, v.dy, M.mulberry32(v.rng));
      expect(evs.length).toBe(v.n);
      for (const [i, ex, ey, sl] of v.samples) { close(evs[i].dx, ex); close(evs[i].dy, ey); close(evs[i].sleepMs, sl); }
    }
    for (const v of VEC.hesitation) {
      const st = (M as any).dragHesitation({ x: v.from[0], y: v.from[1] }, { x: v.to[0], y: v.to[1] },
        M.makePersona(v.persona), M.mulberry32(v.rng), v.dur_ms);
      expect(st.length).toBe(v.n);
      for (const [i, x, y, sl] of v.samples) { close(st[i].x, x); close(st[i].y, y); close(st[i].sleepMs, sl); }
    }
    for (const v of VEC.held) {
      const r = (M as any).hesitateThenDrag({ x: v.from[0], y: v.from[1] }, { x: v.to[0], y: v.to[1] },
        M.makePersona(v.persona), v.window_ms, v.settle, M.mulberry32(v.rng));
      expect(r.steps.length).toBe(v.n);
      expect(r.creep).toBe(v.creep);
      for (const [i, x, y, sl] of v.samples) { close(r.steps[i].x, x); close(r.steps[i].y, y); close(r.steps[i].sleepMs, sl); }
    }
    for (const v of VEC.need) {
      const [dx, dy] = S.scrollNeed(v.box, v.view, v.frac);
      close(dx, v.dx, 1e-9); close(dy, v.dy, 1e-9);
    }
    for (const v of VEC.device) expect(S.inputDevice(v.platform, v.option)).toBe(v.device);
  });
});
