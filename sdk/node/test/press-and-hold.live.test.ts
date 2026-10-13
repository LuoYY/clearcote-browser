/**
 * LIVE: pressAndHold as the PAGE sees it -- one trusted press on the element, held for the asked time
 * with the button reported down on every move, the drift within 2 px, `until` releasing early, frame
 * locators and non-humanized pages working too, and the press-and-hold widget
 * (sdk/testdata/press_and_hold.html) rejecting a plain click but passing a hold.
 *
 * Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary (and a licence is available for a PRO
 * build). Mirrors sdk/python/tests/test_press_and_hold_live.py (+ the async file's `until` check).
 */
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import type { Browser, Page } from "playwright-core";
import { launch, pressAndHold } from "../src/index.js";

const LIVE = process.env.CLEARCOTE_LIVE_ENGINE;
const WIDGET = pathToFileURL(fileURLToPath(new URL("../../testdata/press_and_hold.html", import.meta.url))).href;
const widgetUrl = (ms: number) => `${WIDGET}?ms=${ms}`;
const T = 60_000;

const LOGGER = `<script>
window.__ev = [];
for (const t of ['pointerdown', 'pointerup', 'pointermove', 'click', 'contextmenu'])
  addEventListener(t, e => window.__ev.push({t: e.type, ts: e.timeStamp, x: e.clientX, y: e.clientY,
    trusted: e.isTrusted, buttons: e.buttons, button: e.button, id: e.target.id || e.target.tagName}),
    {capture: true, passive: true});
</script>`;
const HOLD_BTN = `<button id="hold" style="position:absolute;left:@L@px;top:@T@px;width:220px;height:60px">Press and hold</button>
<div id="done" hidden>done</div>
<script>
let timer = null;
const b = document.getElementById('hold');
b.addEventListener('pointerdown', () => { timer = setTimeout(() => document.getElementById('done').hidden = false, 1500); });
b.addEventListener('pointerup', () => clearTimeout(timer));
b.addEventListener('pointerleave', () => clearTimeout(timer));
</script>`;
const PAGE = "<!doctype html><html><head><meta charset='utf-8'><style>html,body{margin:0;height:2400px}</style></head><body>"
  + LOGGER + HOLD_BTN.replace("@L@", "420").replace("@T@", "300")
  + "<div id='far' style='position:absolute;left:300px;top:1900px;width:160px;height:50px;background:#ddd'>far</div>"
  + "</body></html>";
const FRAME_PAGE = "<!doctype html><html><head><meta charset='utf-8'></head><body style='margin:0'>"
  + "<iframe id='f' style='position:absolute;left:200px;top:150px;width:600px;height:300px;border:0' srcdoc=\""
  + ("<body style='margin:0'>" + LOGGER + HOLD_BTN.replace("@L@", "80").replace("@T@", "90")).replace(/"/g, "&quot;")
  + "\"></iframe></body></html>";

type Ev = { t: string; ts: number; x: number; y: number; trusted: boolean; buttons: number; button: number; id: string };
type HoldState = { state: string; attempts: number; moves: number; movesNoButton: number; rejected: string[]; resets: { why: string }[] };

function held(ev: Ev[], target = "hold"): { d: Ev; u: Ev; during: Ev[] } {
  const downs = ev.filter((e) => e.t === "pointerdown");
  const ups = ev.filter((e) => e.t === "pointerup");
  expect(downs.length === 1 && ups.length === 1, JSON.stringify(ev.filter((e) => e.t !== "pointermove").map((e) => e.t))).toBe(true);
  const d = downs[0], u = ups[0];
  expect(d.trusted && u.trusted).toBe(true);
  expect([d.id, u.id]).toEqual([target, target]);
  return { d, u, during: ev.filter((e) => e.t === "pointermove" && d.ts < e.ts && e.ts < u.ts) };
}

describe.runIf(LIVE)("live pressAndHold", () => {
  let browser: Browser;
  let page: Page;
  let dir: string;
  let pageUrl: string;
  let frameUrl: string;
  // Leave TMPDIR as we found it (see humanize-forms.live.test.ts).
  const FC_CACHE = join(tmpdir(), "cc-fc-cache");
  let fcCacheBefore = true;
  const open = (humanize: boolean) => launch({
    executablePath: LIVE, headless: true, humanize, fingerprint: "hold-live-1", quiet: true, args: ["--no-sandbox"],
  });

  beforeAll(async () => {
    fcCacheBefore = existsSync(FC_CACHE);
    dir = mkdtempSync(join(tmpdir(), "cc-hold-live-"));
    writeFileSync(join(dir, "hold.html"), PAGE, "utf8");
    writeFileSync(join(dir, "frame.html"), FRAME_PAGE, "utf8");
    pageUrl = pathToFileURL(join(dir, "hold.html")).href;
    frameUrl = pathToFileURL(join(dir, "frame.html")).href;
    browser = await open(true);
  }, 180_000);
  afterAll(async () => {
    try {
      await browser?.close();
    } finally {
      rmSync(dir, { recursive: true, force: true });
      if (!fcCacheBefore) rmSync(FC_CACHE, { recursive: true, force: true });
    }
  }, 60_000);
  beforeEach(async () => {
    page = await browser.newPage({ viewport: { width: 1180, height: 760 } });
    await page.goto(pageUrl);
  }, 60_000);
  afterEach(async () => { await page?.close(); }, 60_000);

  const P = () => page as any;
  const ev = (): Promise<Ev[]> => page.evaluate("window.__ev") as Promise<Ev[]>;

  it("holds for the duration, drifting within 2 px with the button down", async () => {
    const t0 = performance.now();
    expect(await P().pressAndHold("#hold", { duration: 2000 })).toBe(true);
    const took = performance.now() - t0;
    const all = await ev();
    const { d, u, during } = held(all);
    const h = u.ts - d.ts;
    expect(h).toBeGreaterThanOrEqual(1950); expect(h).toBeLessThanOrEqual(2250);
    expect([d.button, d.buttons]).toEqual([0, 1]);
    expect(during.length, "a 2 s hold drifts at least once").toBeGreaterThan(0);
    for (const m of during) {
      expect(m.trusted && m.buttons === 1, JSON.stringify(m)).toBe(true);
      expect(Math.abs(m.x - d.x)).toBeLessThanOrEqual(2);
      expect(Math.abs(m.y - d.y)).toBeLessThanOrEqual(2);
    }
    // the approach was a glide (many moves before the press), not a teleport
    expect(all.filter((e) => e.t === "pointermove" && e.ts < d.ts).length).toBeGreaterThanOrEqual(10);
    expect(took).toBeLessThan(6000);
    expect(await page.locator("#done").isVisible()).toBe(true);
  }, T);

  it("until releases after a reaction (locator.pressAndHold, async until)", async () => {
    const loc: any = page.locator("#hold");
    expect(await loc.pressAndHold({ duration: 8000, until: () => page.locator("#done").isVisible() })).toBe(true);
    const { d, u } = held(await ev());
    const h = u.ts - d.ts;
    // done shows 1500 ms in; polled every 100 ms, then 160-350 ms of reaction
    expect(h).toBeGreaterThanOrEqual(1600); expect(h).toBeLessThanOrEqual(2050);
  }, T);

  it("returns false when until never holds; tremor off means no moves", async () => {
    expect(await P().pressAndHold("#hold", { duration: 700, until: () => false, tremor: false })).toBe(false);
    const { d, u, during } = held(await ev());
    expect(u.ts - d.ts).toBeGreaterThanOrEqual(650); expect(u.ts - d.ts).toBeLessThanOrEqual(950);
    expect(during).toEqual([]);
  }, T);

  it("presses at position with the right button", async () => {
    await P().pressAndHold("#hold", { duration: 400, position: { x: 20, y: 15 }, button: "right", tremor: false });
    const d = (await ev()).filter((e) => e.t === "pointerdown")[0];
    expect([d.x, d.y, d.button]).toEqual([440, 315, 2]);
  }, T);

  it("scrolls the element into view", async () => {
    await P().pressAndHold("#far", { duration: 300, tremor: false });
    expect((await ev()).filter((e) => e.t === "pointerdown")[0].id).toBe("far");
    expect(await page.evaluate("scrollY")).toBeGreaterThan(1000);
  }, T);

  it("throws on a bad duration / button", async () => {
    await expect(P().pressAndHold("#hold", { duration: 0 })).rejects.toThrow(/duration/);
    await expect(P().pressAndHold("#hold", { button: "both" })).rejects.toThrow(/button/);
    expect((await ev()).filter((e) => e.t === "pointerdown")).toEqual([]);
  }, T);

  it("works on a frame locator", async () => {
    await page.goto(frameUrl);
    const btn: any = page.frameLocator("#f").locator("#hold");
    await btn.waitFor();
    expect(await btn.pressAndHold({ duration: 1800 })).toBe(true);
    const f = page.frames()[1];
    const { d, u, during } = held(await f.evaluate("window.__ev") as Ev[]);
    expect(u.ts - d.ts).toBeGreaterThanOrEqual(1750);
    expect(during.every((m) => m.buttons === 1)).toBe(true);
    expect(await f.locator("#done").isVisible()).toBe(true);
  }, T);

  it("widget: rejects a plain click, a synthetic press and a short hold", async () => {
    await page.goto(widgetUrl(1500));
    await page.locator("#hold").click();
    await page.evaluate("document.getElementById('hold').dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}))");
    await P().pressAndHold("#hold", { duration: 600 });
    const s = await page.evaluate("window.__hold") as HoldState;
    expect(s.state, JSON.stringify(s)).toBe("idle");
    expect(s.attempts, JSON.stringify(s)).toBe(2);
    expect(s.resets.map((r) => r.why)).toEqual(["released", "released"]);
    expect(s.rejected).toEqual(["untrusted"]);
  }, T);

  it("widget: held until done", async () => {
    await page.goto(widgetUrl(2500));
    const ok = await P().pressAndHold("#hold", { until: async () => (await page.locator("#hold[data-state=done]").count()) > 0 });
    const s = await page.evaluate("window.__hold") as HoldState;
    expect(ok).toBe(true);
    expect(s.state, JSON.stringify(s)).toBe("done");
    expect(s.attempts).toBe(1); expect(s.resets).toEqual([]); expect(s.rejected).toEqual([]);
    expect(s.moves).toBeGreaterThanOrEqual(1);
    expect(s.movesNoButton).toBe(0);
    expect(await page.locator("#status").innerText()).toBe("Verified");
  }, T);

  it("widget: in an iframe", async () => {
    const f = join(dir, "widget-frame.html");
    writeFileSync(f, `<!doctype html><body style='margin:0'><iframe id='f' src='${widgetUrl(2000)}' `
      + "style='position:absolute;left:120px;top:60px;width:640px;height:420px;border:0'></iframe>", "utf8");
    await page.goto(pathToFileURL(f).href);
    const fl = page.frameLocator("#f");
    const btn: any = fl.locator("#hold");
    await btn.waitFor();
    expect(await btn.pressAndHold({ until: async () => (await fl.locator("#hold[data-state=done]").count()) > 0 })).toBe(true);
    const s = await page.frames()[1].evaluate("window.__hold") as HoldState;
    expect(s.state, JSON.stringify(s)).toBe("done");
    expect(s.attempts).toBe(1);
    expect(s.movesNoButton).toBe(0);
  }, T);

  it("without humanize: the exported function", async () => {
    const b = await open(false);
    try {
      const p = await b.newPage({ viewport: { width: 1180, height: 760 } });
      await p.goto(pageUrl);
      expect((p as any).pressAndHold).toBeUndefined();
      expect(await pressAndHold(p.locator("#hold"), { duration: 1700 })).toBe(true);
      const { d, u, during } = held(await p.evaluate("window.__ev") as Ev[]);
      expect(u.ts - d.ts).toBeGreaterThanOrEqual(1650);
      expect(during.every((m) => m.buttons === 1)).toBe(true);
      expect(await p.locator("#done").isVisible()).toBe(true);
      // and the widget, on the plain page
      await p.goto(widgetUrl(1800));
      expect(await pressAndHold(p.locator("#hold"))).toBe(true);
      const s = await p.evaluate("window.__hold") as HoldState;
      expect(s.state, JSON.stringify(s)).toBe("done");
      expect(s.movesNoButton).toBe(0);
    } finally {
      await b.close();
    }
  }, 120_000);
});
