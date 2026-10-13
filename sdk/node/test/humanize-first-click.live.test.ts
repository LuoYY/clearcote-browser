/**
 * LIVE: the FIRST humanized mouse.click on a fresh headless page, and the first after a navigation,
 * walks and presses. Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary (and a licence is
 * available for a PRO build).
 *
 * WHY. The engine route (Browser.dispatchPointerPath) walks in the browser process and does not wait
 * for the page. A fresh headless page has drawn one or two frames and then stalls until something asks
 * for one, and what the engine sent meanwhile was lost: on r38 and r40 (SC 216, 2026-10-13) 2 of 8 first
 * clicks delivered no mousedown at all (some a mouseup without one), the others 1-9 pointer moves for the
 * whole walk. Waiting for two animation frames before the walk fixed 18 of 18 (25-41 moves each).
 */
import { existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Browser, Page } from "playwright-core";
import { launch } from "../src/index.js";

const LIVE = process.env.CLEARCOTE_LIVE_ENGINE;
const PAGES = 6;

const PAGE = `<!doctype html><html><body style="margin:0;height:1200px">
<button id=b style="position:absolute;left:600px;top:400px;width:120px;height:40px">target</button>
<button id=c style="position:absolute;left:150px;top:120px;width:120px;height:40px">other</button>
<script>window.ev = [];
addEventListener('pointermove', e => {
  const cs = (e.getCoalescedEvents && e.getCoalescedEvents().length) ? e.getCoalescedEvents() : [e];
  for (const c of cs) ev.push({ t: 'move', tr: e.isTrusted });
}, true);
for (const t of ['mousedown', 'mouseup', 'click'])
  addEventListener(t, e => ev.push({ t, tr: e.isTrusted, id: e.target.id }), true);</script></body></html>`;

type E = { t: string; tr: boolean; id?: string };

describe.runIf(LIVE)("live: the first humanized click on a fresh page", () => {
  let browser: Browser;
  const FC_CACHE = join(tmpdir(), "cc-fc-cache");
  let fcCacheBefore = true;
  beforeAll(async () => {
    fcCacheBefore = existsSync(FC_CACHE);
    browser = await launch({ executablePath: LIVE, headless: true, humanize: true, fingerprint: "first-click-live-1",
      quiet: true, args: ["--no-sandbox"] });
  }, 180_000);
  afterAll(async () => {
    try { await browser?.close(); } finally { if (!fcCacheBefore) rmSync(FC_CACHE, { recursive: true, force: true }); }
  }, 60_000);

  // b's centre, or c's: consecutive clicks on one page alternate, so each walk covers ~500 px.
  const clickAndRead = async (page: Page, at: [number, number] = [660, 420]): Promise<E[]> => {
    await page.mouse.click(at[0], at[1]);
    await page.waitForFunction(() => (window as any).ev.some((e: E) => e.t === "mouseup"), null, { timeout: 3_000 })
      .catch(() => {});
    return page.evaluate(() => (window as any).ev.splice(0));
  };
  const summary = (ev: E[]) => ({
    moves: ev.filter((e) => e.t === "move").length,
    down: ev.filter((e) => e.t === "mousedown").length,
    up: ev.filter((e) => e.t === "mouseup").length,
    click: ev.filter((e) => e.t === "click" && (e.id === "b" || e.id === "c")).length,
    trusted: ev.every((e) => e.tr),
  });
  // A walk of 300+ px over >= 280 ms reaches the page as tens of moves once frames flow (22-50 measured);
  // the broken first walks delivered 1-9.
  const WALKED = { down: 1, up: 1, click: 1, trusted: true };

  it(`walks and presses on each of ${PAGES} fresh pages`, async () => {
    const got = [];
    for (let i = 0; i < PAGES; i++) {
      const page = await browser.newPage();
      await page.setContent(PAGE);
      await page.waitForTimeout(200);
      got.push(summary(await clickAndRead(page)));
      await page.close();
    }
    for (const g of got) {
      expect(g, JSON.stringify(got)).toMatchObject(WALKED);
      expect(g.moves, JSON.stringify(got)).toBeGreaterThanOrEqual(10);
    }
  }, 120_000);

  it("walks and presses after a navigation", async () => {
    const page = await browser.newPage();
    await page.setContent(PAGE);
    await page.waitForTimeout(200);
    await clickAndRead(page);
    const got = [];
    for (let i = 0; i < 4; i++) {
      await page.goto("data:text/html," + encodeURIComponent(PAGE + `<!-- ${i} -->`));
      await page.waitForTimeout(200);
      got.push(summary(await clickAndRead(page, i % 2 ? [660, 420] : [210, 140])));
    }
    for (const g of got) {
      expect(g, JSON.stringify(got)).toMatchObject(WALKED);
      expect(g.moves, JSON.stringify(got)).toBeGreaterThanOrEqual(10);
    }
    await page.close();
  }, 120_000);
});
