/**
 * Lane F (SC 232) live, Node: select inside a frame through frameLocator is ONE trusted change (6/6, a direct
 * and a nested frame); option objects on the page; a hidden custom checkbox is checked through its label; a
 * cross-origin (out-of-process) frame falls back to Playwright with the fallback reported. Headless only;
 * skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary. Mirrors test_humanize_coverage_live.py.
 */
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Browser, Frame, Page } from "playwright-core";
import { launch } from "../src/index.js";

const LIVE = process.env.CLEARCOTE_LIVE_ENGINE;
const T = 90_000;

const REC = `<script>
window.__ev = [];
for (const t of ['mousedown','mouseup','click','input','change','keydown'])
  document.addEventListener(t, (e) => window.__ev.push({ t, id: (e.target && e.target.id) || '', tr: e.isTrusted }), true);
</script>`;
const NAMES = ["Afghanistan", "Albania", "Germany", "Ghana", "Greece", "Netherlands", "Norway", "Peru", "Spain", "Uruguay"];
const OPTS = NAMES.map((n, i) => `<option value='v${i}'>${n}</option>`).join("");
const CHILD = `<!doctype html><html><head><meta charset=utf-8>${REC}</head><body style='margin:8px'>
<select id=cs>${OPTS}</select><input id=ct value='old'><input id=cc type=checkbox></body></html>`;
const OUTER = `<!doctype html><html><head>${REC}</head><body style='margin:20px'>
<iframe id=inner src='/child.html' style='width:420px;height:200px;border:3px solid #888;padding:5px'></iframe></body></html>`;
const MAIN = (cross: string) => `<!doctype html><html><head><meta charset=utf-8>${REC}<style>
.vh{position:absolute;opacity:0;width:0;height:0;margin:0} iframe{display:block;margin:8px 0}</style></head>
<body style='margin:10px'><select id=s>${OPTS}</select>
<input type=checkbox id=hcb class=vh><label for=hcb id=hlab>Accept the terms</label><br>
<label id=wrap><input type=checkbox id=hcb2 class=vh>Subscribe to news</label><br>
<iframe id=same name=same src='/child.html' style='width:460px;height:200px;border:4px solid #444;padding:6px'></iframe>
<iframe id=nested src='/outer.html' style='width:520px;height:300px'></iframe>
<iframe id=cross src='${cross}/child.html' style='width:460px;height:200px'></iframe></body></html>`;

type E = { t: string; id: string; tr: boolean };
const ev = async (f: Page | Frame, t: string, id: string): Promise<E[]> =>
  ((await f.evaluate("window.__ev")) as E[]).filter((e) => e.t === t && e.id === id);
const reset = (f: Page | Frame) => f.evaluate("window.__ev.length = 0");

describe.runIf(LIVE)("live lane F coverage (node)", () => {
  let server: Server;
  let base = "";
  let browser: Browser;
  let page: Page;

  beforeAll(async () => {
    let pages: Record<string, string> = {};
    server = createServer((req, res) => {
      const body = pages[(req.url ?? "").split("?")[0]];
      if (body === undefined) { res.writeHead(404); res.end(); return; }
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(body);
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
    const port = (server.address() as AddressInfo).port;
    base = `http://127.0.0.1:${port}`;
    pages = { "/main.html": MAIN(`http://localhost:${port}`), "/child.html": CHILD, "/outer.html": OUTER };
    browser = await launch({ executablePath: LIVE, headless: true, humanize: true, fingerprint: "coverage-live-node",
      quiet: true, args: ["--no-sandbox"] });
    page = await browser.newPage();
    await page.goto(base + "/main.html");
    await page.waitForLoadState("load");
  }, 180_000);
  afterAll(async () => {
    try { await browser?.close(); } finally { await new Promise<void>((r) => server.close(() => r())); }
  });

  const nested = (): Frame => page.frames().find((f) => f.parentFrame()?.parentFrame())!;

  for (let run = 0; run < 6; run++) {
    it(`frame select is one trusted change (${run})`, async () => {
      const which = run % 2 === 0 ? "same" : "nested";
      const f = which === "same" ? page.frame({ name: "same" })! : nested();
      const fl = which === "same" ? page.frameLocator("#same") : page.frameLocator("#nested").frameLocator("#inner");
      const name = ["Peru", "Ghana", "Spain", "Norway", "Greece", "Uruguay"][run];
      await reset(f);
      expect(await fl.locator("#cs").selectOption({ label: name })).toEqual([`v${NAMES.indexOf(name)}`]);
      const ch = await ev(f, "change", "cs");
      expect(ch.length).toBe(1);
      expect(ch[0].tr).toBe(true);
      expect((page as any)._clearcoteLastFallback).toBeUndefined();
    }, T);
  }

  it("frame fill types trusted keys", async () => {
    const f = nested();
    await reset(f);
    await page.frameLocator("#nested").frameLocator("#inner").locator("#ct").fill("new words");
    expect(await f.locator("#ct").inputValue()).toBe("new words");
    const keys = await ev(f, "keydown", "ct");
    expect(keys.length).toBeGreaterThanOrEqual(9);
    expect(keys.every((k) => k.tr)).toBe(true);
  }, T);

  it("option objects on the page are one trusted change", async () => {
    await reset(page);
    expect(await page.selectOption("#s", [{ label: "Peru" }, { value: "v9" }])).toEqual(["v7"]);
    expect((await ev(page, "change", "s")).map((e) => e.tr)).toEqual([true]);
  }, T);

  for (const sel of ["#hcb", "#hcb2"]) {
    it(`a hidden custom checkbox ${sel} is checked through its label`, async () => {
      await reset(page);
      await page.locator(sel).check({ timeout: 3000 });
      expect(await page.locator(sel).isChecked()).toBe(true);
      const downs = (await page.evaluate("window.__ev") as E[]).filter((e) => e.t === "mousedown");
      expect(downs.length).toBe(1);
      expect(downs[0].tr).toBe(true);
    }, T);
  }

  it("a cross-origin frame falls back to Playwright, reported", async () => {
    (page as any)._clearcoteLastFallback = undefined;
    expect(await page.frameLocator("#cross").locator("#cs").selectOption("Peru")).toEqual(["v7"]);
    const fb = (page as any)._clearcoteLastFallback;
    expect(fb?.api).toBe("locator.selectOption");
    expect(String(fb?.reason)).toContain("cross-origin");
  }, T);
});
