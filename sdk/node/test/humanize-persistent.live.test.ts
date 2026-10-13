/**
 * LIVE: launch({ humanize: true }) -- a persistent context by default -- really humanizes, on whatever
 * playwright-core is installed. Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary (and a
 * licence is available for a PRO build).
 *
 * WHY. On playwright-core 1.49.x a persistent context has no Browser (context.browser() is null), and
 * humanize attached only when it found one: 0.48.0 and 0.49.0 silently did not humanize there, while
 * package.json allows ^1.49.0. Run this file on the oldest playwright-core package.json allows as well
 * as on the lockfile's (the "node SDK tests (oldest playwright-core)" CI job installs it; on a box with
 * an engine: `npm i --no-save playwright-core@<floor>` before `npx vitest run` on this file).
 */
import { existsSync, readFileSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Browser, Page } from "playwright-core";
import { launch } from "../src/index.js";

const LIVE = process.env.CLEARCOTE_LIVE_ENGINE;
const require = createRequire(import.meta.url);
const PW = JSON.parse(readFileSync(join(dirname(require.resolve("playwright-core")), "package.json"), "utf8")).version;

const PAGE = `<!doctype html><html><body style="margin:0;height:1200px">
<button id=b style="position:absolute;left:600px;top:400px;width:120px;height:40px">target</button>
<select id=s style="position:absolute;left:600px;top:560px;width:120px;height:30px">
<option value=one>one</option><option value=two>two</option></select>
<script>window.ev = [];
// Coalesced pointer moves: the engine walks the path in the browser, and a frame folds its moves into one
// mousemove event, so counting events would undercount the path.
addEventListener('pointermove', e => {
  const cs = (e.getCoalescedEvents && e.getCoalescedEvents().length) ? e.getCoalescedEvents() : [e];
  for (const c of cs) ev.push({ t: 'move', ts: c.timeStamp, tr: e.isTrusted });
}, true);
for (const t of ['mousedown', 'mouseup', 'click', 'change'])
  addEventListener(t, e => ev.push({ t, ts: e.timeStamp, tr: e.isTrusted }), true);</script></body></html>`;

type E = { t: string; ts: number; tr: boolean };

describe.runIf(LIVE)(`live humanize on a persistent launch (playwright-core ${PW})`, () => {
  let browser: Browser;
  let page: Page;
  // The Linux font config's cache: left as found (the temp directory must end the run as it began).
  const FC_CACHE = join(tmpdir(), "cc-fc-cache");
  let fcCacheBefore = true;
  beforeAll(async () => {
    fcCacheBefore = existsSync(FC_CACHE);
    browser = await launch({ executablePath: LIVE, headless: true, humanize: true, fingerprint: "persist-live-1",
      quiet: true, args: ["--no-sandbox"] });
    page = await browser.newPage();
    await page.setContent(PAGE);
    await page.waitForTimeout(200);
  }, 180_000);
  afterAll(async () => {
    try { await browser?.close(); } finally { if (!fcCacheBefore) rmSync(FC_CACHE, { recursive: true, force: true }); }
  }, 60_000);

  const take = (): Promise<E[]> => page.evaluate(() => (window as any).ev.splice(0));

  it("attaches to the page", () => {
    expect((page as any).__clearcoteHumanized).toBe(true);
  });

  it("mouse.click glides to the point and holds the button", async () => {
    // What tells the two apart is TIME, not the number of move events: stock Playwright presses as soon as
    // it is asked (one move, a 0.3 ms hold, measured on 0.49.0 + 1.49.1), while the humanized click walks
    // for at least ~280 ms and holds the button like a finger. The engine walks in the browser, and under
    // headless a walk can reach the page as only 1-3 pointer events (on main with the lockfile's
    // playwright-core too), so a move count would be flaky.
    //
    // One click first, and only the second is measured: on a fresh headless page the FIRST engine click
    // sometimes delivers a move and no press at all (2 of 6 on main with playwright-core 1.61 and r38,
    // 2026-10-13) -- a separate bug, not what this file is about.
    await page.mouse.click(200, 200);
    await page.waitForFunction(() => (window as any).ev.some((e: E) => e.t === "mouseup"), null, { timeout: 3_000 })
      .catch(() => {});
    await take();
    const t0 = await page.evaluate(() => performance.now());
    await page.mouse.click(660, 420);
    // The engine can answer before its walk has reached the page: wait for the release (or time out on
    // the stock path, whose release is already there).
    await page.waitForFunction(() => (window as any).ev.some((e: E) => e.t === "mouseup"), null, { timeout: 5_000 })
      .catch(() => {});
    const ev = await take();
    const down = ev.find((e) => e.t === "mousedown")!;
    const up = ev.find((e) => e.t === "mouseup")!;
    expect(down && up).toBeTruthy();
    expect(down.ts - t0).toBeGreaterThanOrEqual(150);
    expect(up.ts - down.ts).toBeGreaterThanOrEqual(20);
    expect(ev.every((e) => e.tr)).toBe(true);
  }, 60_000);

  it("selectOption picks through trusted input", async () => {
    await take();
    await page.selectOption("#s", "two");
    expect(await page.locator("#s").inputValue()).toBe("two");
    const change = (await take()).filter((e) => e.t === "change");
    expect(change.length).toBe(1);
    expect(change[0].tr).toBe(true);
  }, 60_000);
});
