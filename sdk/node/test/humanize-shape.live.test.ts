/**
 * LIVE path shape and engine routing against a real Clearcote build (SC 210 lane S) -- a port of
 * sdk/python/tests/test_humanize_shape_live.py. Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote
 * binary (and a licence is available for a PRO build). What a PAGE sees: trusted pointer events, where
 * a press lands, how far a held drag strays from its line, and whether the pointer ever jumps.
 */
import { existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import type { Browser, Page } from "playwright-core";
import { launch } from "../src/index.js";

const LIVE = process.env.CLEARCOTE_LIVE_ENGINE;
const T = 90_000;

const PAGE = `<!doctype html><html><head><style>
body { margin: 0; height: 900px; font: 14px sans-serif; }
#track { position: absolute; left: 60px; top: 300px; width: 420px; height: 44px; background: #ddd; }
#knob { position: absolute; left: 2px; top: 2px; width: 40px; height: 40px; border-radius: 20px;
        background: #16a34a; touch-action: none; }
.b { position: absolute; width: 90px; height: 34px; }
</style></head><body>
<div id="track"><div id="knob"></div></div>
<button class="b" id="t1" style="left:600px;top:120px">one</button>
<button class="b" id="t2" style="left:180px;top:520px">two</button>
<button class="b" id="t3" style="left:820px;top:560px">three</button>
<script>
window.__ev = [];
addEventListener('pointermove', e => {
  const cs = (e.getCoalescedEvents && e.getCoalescedEvents().length) ? e.getCoalescedEvents() : [e];
  for (const c of cs) window.__ev.push({t: 'move', ts: c.timeStamp, x: c.clientX, y: c.clientY, b: c.buttons, tr: e.isTrusted});
}, true);
for (const ty of ['pointerdown', 'pointerup', 'click'])
  addEventListener(ty, e => window.__ev.push({t: ty, ts: e.timeStamp, x: e.clientX, y: e.clientY, tr: e.isTrusted,
                                             id: e.target.id || e.target.tagName}), true);
(() => { const k = document.getElementById('knob'); let sx = null, x0 = 2;
  k.addEventListener('pointerdown', e => { sx = e.clientX; x0 = k.offsetLeft; k.setPointerCapture(e.pointerId); });
  k.addEventListener('pointermove', e => { if (sx === null) return;
    k.style.left = Math.max(2, Math.min(378, x0 + e.clientX - sx)) + 'px'; });
  k.addEventListener('pointerup', () => { sx = null; }); })();
</script></body></html>`;

type E = { t: string; ts: number; x: number; y: number; b?: number; tr: boolean; id?: string };

describe.runIf(LIVE)("live path shape and engine routing", () => {
  let browser: Browser;
  let page: Page;
  const ENV = "CLEARCOTE_HUMANIZE_ENGINE_CLICKS";
  const FC_CACHE = join(tmpdir(), "cc-fc-cache");
  let fcCacheBefore = true;
  beforeAll(async () => {
    fcCacheBefore = existsSync(FC_CACHE);
    browser = await launch({ executablePath: LIVE, headless: true, humanize: true, fingerprint: "shape-live-1", quiet: true,
      args: ["--no-sandbox"] });
  }, 180_000);
  afterAll(async () => {
    try { await browser?.close(); } finally { if (!fcCacheBefore) rmSync(FC_CACHE, { recursive: true, force: true }); }
  }, 60_000);
  beforeEach(async () => {
    page = await browser.newPage();
    await page.setContent(PAGE);
    await page.waitForTimeout(200);
  }, 60_000);
  afterEach(async () => { delete process.env[ENV]; await page?.close(); });

  const take = (): Promise<E[]> => page.evaluate(() => (window as any).__ev.splice(0));
  const centre = async (sel: string) => {
    const b = (await page.locator(sel).boundingBox())!;
    return { x: b.x + b.width / 2, y: b.y + b.height / 2, b };
  };
  /** Records the pointer-path walks the engine accepted on the browser CDP session humanize opens. */
  const spyEngine = () => {
    const log: any[] = [];
    const br: any = page.context().browser();
    const orig = br.newBrowserCDPSession.bind(br);
    br.newBrowserCDPSession = async () => {
      const s = await orig();
      const send = s.send.bind(s);
      s.send = async (method: string, params?: any) => {
        const out = await send(method, params);
        if (method === "Browser.dispatchPointerPath" || method === "Browser.humanizedClick") log.push(params);
        return out;
      };
      return s;
    };
    return log;
  };

  it("a slider drag stays on its track and lands", async () => {
    const { x, y } = await centre("#knob");
    await page.mouse.move(x, y);
    await take();
    await page.mouse.down();
    await page.mouse.move(x + 330, y + 2);
    await page.mouse.up();
    const ev = await take();
    const held = ev.filter((e) => e.t === "move" && e.b);
    // Enough held samples to measure the leg, not a rate check: the glide is paced against a wall clock
    // (lane P), so a loaded box sends fewer, later samples of the same leg than the unpaced loop did.
    expect(held.length).toBeGreaterThanOrEqual(10);
    expect(ev.every((e) => e.tr)).toBe(true);
    const x1 = x + 330, y1 = y + 2, d = Math.hypot(x1 - x, y1 - y);
    const side = Math.max(...held.map((e) => Math.abs(((e.x - x) * (y1 - y)) / d - ((e.y - y) * (x1 - x)) / d)));
    expect(side).toBeLessThanOrEqual(9);
    const left = await page.evaluate(() => document.getElementById("knob")!.offsetLeft);
    expect(Math.abs(left - 332)).toBeLessThanOrEqual(3);
  }, T);

  it("a coordinate click after an SDK glide never teleports", async () => {
    const t1 = await centre("#t1");
    await page.mouse.click(t1.x, t1.y);
    await take();
    await page.mouse.move(850, 600);
    await take();
    const t2 = await centre("#t2");
    await page.mouse.click(t2.x, t2.y);
    const ev = await take();
    const pts = [[850, 600], ...ev.filter((e) => e.t === "move").map((e) => [e.x, e.y])];
    let worst = 0;
    for (let i = 1; i < pts.length; i++) worst = Math.max(worst, Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
    expect(worst).toBeLessThan(60);
    const downs = ev.filter((e) => e.t === "pointerdown");
    expect(downs.map((e) => e.id)).toEqual(["t2"]);
    expect(downs[0].tr).toBe(true);
  }, T);

  it("opt-in: selector clicks walk in the engine, trusted, inside the target", async () => {
    process.env[ENV] = "all";
    const log = spyEngine();
    for (const sel of ["#t1", "#t3", "#t2"]) await page.click(sel);
    const ev = await take();
    expect(log.length).toBe(3);
    for (const [i, sel] of ["#t1", "#t3", "#t2"].entries()) {
      const { b } = await centre(sel);
      expect(log[i].x).toBeGreaterThanOrEqual(b.x);
      expect(log[i].x).toBeLessThanOrEqual(b.x + b.width);
      expect(log[i].y).toBeGreaterThanOrEqual(b.y);
      expect(log[i].y).toBeLessThanOrEqual(b.y + b.height);
    }
    expect(ev.filter((e) => e.t === "click").map((e) => e.id)).toEqual(["t1", "t3", "t2"]);
    expect(ev.every((e) => e.tr)).toBe(true);
    await page.mouse.move(300, 200);
    await take();
    await page.click("#t1");
    expect(log.length).toBe(3);
    expect((await take()).filter((e) => e.t === "click").map((e) => e.id)).toEqual(["t1"]);
  }, T);

  it("by default a selector click stays on the planner", async () => {
    const log = spyEngine();
    await page.click("#t3");
    const ev = await take();
    expect(log).toEqual([]);
    expect(ev.filter((e) => e.t === "click").map((e) => e.id)).toEqual(["t3"]);
    expect(ev.every((e) => e.tr)).toBe(true);
  }, T);
});
