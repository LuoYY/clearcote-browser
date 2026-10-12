/**
 * LIVE humanized form input against a real Clearcote build (sdk/testdata/humanize_forms.html).
 *
 * Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary (and a licence is available for a PRO
 * build). Every assertion is about what a PAGE sees: trusted events, key holds, how many change
 * events a selection fires, and the exact value left in the field -- the things a behavioural detector
 * and a form backend read. Mirrors sdk/python/tests/test_humanize_forms_live.py check for check.
 */
import { existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { errors, type Browser, type Page } from "playwright-core";
import { launch } from "../src/index.js";
import { ISO_ACTIVE_FIELD, worldFor } from "../src/isolated.js";

const LIVE = process.env.CLEARCOTE_LIVE_ENGINE;
const FIXTURE = pathToFileURL(fileURLToPath(new URL("../../testdata/humanize_forms.html", import.meta.url))).href;
const MIN_HOLD_MS = 25; // a finger cannot hold a key or button shorter than this; persona floor is 45
const T = 90_000;

type E = {
  t: string; id: string; tr: boolean; ts: number; key: string; code: string; button: number | null;
  buttons: number | null; detail: number; shift: boolean; ctrl: boolean; alt: boolean; meta: boolean;
  ox: number | null; oy: number | null; value: string | null; it: string; n: number; te?: number;
};

describe.runIf(LIVE)("live humanized form input", () => {
  let browser: Browser;
  let page: Page;
  const ENV = "CLEARCOTE_HUMANIZE_TYPO_RATE";
  const savedRate = process.env[ENV];
  const typos = (r: number) => { process.env[ENV] = String(r); };

  // Leave TMPDIR as we found it (SDK CI fails on leftovers, and a vitest worker never runs exit
  // hooks). launch() (not persistent) makes no profile dir there; a Linux launch does create the
  // shared fontconfig cache (fonts.ts), removed here when this suite was the one that made it. Only
  // that name: other live files may be running in parallel workers with their own temp dirs.
  const FC_CACHE = join(tmpdir(), "cc-fc-cache");
  let fcCacheBefore = true;
  beforeAll(async () => {
    fcCacheBefore = existsSync(FC_CACHE);
    browser = await launch({
      executablePath: LIVE, headless: true, humanize: true, fingerprint: "forms-live-1", quiet: true,
      args: ["--no-sandbox"],
    });
  }, 180_000);
  afterAll(async () => {
    try {
      await browser?.close();
    } finally {
      if (!fcCacheBefore) rmSync(FC_CACHE, { recursive: true, force: true });
    }
  }, 60_000);
  beforeEach(async () => {
    page = await browser.newPage();
    await page.goto(FIXTURE);
  }, 60_000);
  afterEach(async () => {
    if (savedRate === undefined) delete process.env[ENV]; else process.env[ENV] = savedRate;
    await page?.close();
  }, 60_000);

  const ev = async (t?: string, id?: string): Promise<E[]> =>
    ((await page.evaluate("window.__ev")) as E[]).filter((e) => (t === undefined || e.t === t) && (id === undefined || e.id === id));
  const reset = () => page.evaluate("window.__ev.length = 0");

  /** [key, keydown->keyup ms] for every key that went to element `id`. */
  const keyHolds = async (id?: string): Promise<[string, number][]> => {
    const open = new Map<string, number>();
    const out: [string, number][] = [];
    for (const e of await ev()) {
      if ((e.t !== "keydown" && e.t !== "keyup") || (id !== undefined && e.id !== id)) continue;
      const k = e.code || e.key;
      if (e.t === "keydown") { if (!open.has(k)) open.set(k, e.ts); }
      else if (open.has(k)) { out.push([e.key, e.ts - open.get(k)!]); open.delete(k); }
    }
    return out;
  };

  const assertHumanKeys = async (id: string) => {
    const holds = await keyHolds(id);
    expect(holds.length, `no keys reached #${id}`).toBeGreaterThan(0);
    const short = holds.filter(([, d]) => d < MIN_HOLD_MS);
    expect(short, "keys held shorter than a finger can").toEqual([]);
    const evs = (await ev()).filter((e) => ["keydown", "keyup", "input", "change"].includes(e.t) && e.id === id);
    expect(evs.every((e) => e.tr)).toBe(true);
  };

  const pressed = async (key: string, id?: string) => (await ev("keydown", id)).filter((e) => e.key === key);
  const changes = async (id: string) => (await ev()).filter((e) => e.t === "change" && e.id === id);

  // ------------------------------------------------------------------------------------- clicks
  it("a right click is a right click", async () => {
    await reset();
    await page.click("#btn", { button: "right" });
    const downs = await ev("mousedown", "btn");
    expect(downs.map((d) => d.button)).toEqual([2]);
    expect(downs[0].tr).toBe(true);
    expect((await ev("contextmenu", "btn")).length, "a right click opens the context menu").toBeGreaterThan(0);
    expect(await ev("click", "btn"), "a right click is not a click").toEqual([]);
  }, T);

  it("a middle click through a locator", async () => {
    await reset();
    await page.locator("#btn").click({ button: "middle" });
    expect((await ev("mousedown", "btn")).map((d) => d.button)).toEqual([1]);
    expect((await ev("auxclick", "btn")).length).toBeGreaterThan(0);
  }, T);

  it("clickCount 2 is a double click", async () => {
    await reset();
    await page.click("#btn", { clickCount: 2 });
    expect((await ev("mousedown", "btn")).map((d) => d.detail)).toEqual([1, 2]);
    expect((await ev("dblclick", "btn"))[0].tr).toBe(true);
  }, T);

  it("dblclick has human holds and a human gap", async () => {
    await reset();
    await page.dblclick("#btn");
    const downs = await ev("mousedown", "btn"), ups = await ev("mouseup", "btn");
    expect(downs.length).toBe(2);
    expect(ups.length).toBe(2);
    for (let i = 0; i < 2; i++) expect(ups[i].ts - downs[i].ts, "button held like a finger").toBeGreaterThanOrEqual(40);
    const gap = downs[1].ts - ups[0].ts;
    expect(gap).toBeGreaterThanOrEqual(50);
    expect(gap).toBeLessThanOrEqual(450);
    expect((await ev("dblclick", "btn")).length).toBeGreaterThan(0);
  }, T);

  it("modifiers are held around the press", async () => {
    await reset();
    await page.click("#btn", { modifiers: ["Shift"] });
    const click = (await ev("click", "btn"))[0];
    expect(click.shift && click.tr).toBe(true);
    const shiftDown = (await pressed("Shift"))[0].ts;
    const shiftUp = (await ev("keyup")).filter((e) => e.key === "Shift")[0].ts;
    expect(shiftDown).toBeLessThan((await ev("mousedown", "btn"))[0].ts);
    expect(shiftUp).toBeGreaterThan((await ev("mouseup", "btn"))[0].ts);
  }, T);

  it("position is honoured", async () => {
    await reset();
    await page.click("#btn", { position: { x: 10, y: 12 } });
    const d = (await ev("mousedown", "btn"))[0];
    expect(Math.abs(d.ox! - 10)).toBeLessThanOrEqual(1.5);
    expect(Math.abs(d.oy! - 12)).toBeLessThanOrEqual(1.5);
  }, T);

  it("trial performs no action", async () => {
    await reset();
    await page.click("#trialbtn", { trial: true });
    await page.locator("#trialbtn").click({ trial: true });
    await page.hover("#trialbtn", { trial: true });
    expect(await ev("mousedown", "trialbtn")).toEqual([]);
    expect(await ev("click", "trialbtn")).toEqual([]);
  }, T);

  it("delay sets the hold", async () => {
    await reset();
    await page.click("#btn", { delay: 300 });
    const d = (await ev("mousedown", "btn"))[0], u = (await ev("mouseup", "btn"))[0];
    expect(u.ts - d.ts).toBeGreaterThanOrEqual(270);
    expect(u.ts - d.ts).toBeLessThanOrEqual(450);
  }, T);

  it("a glide precedes the press", async () => {
    await reset();
    await page.click("#btn");
    const moves = (await ev()).filter((e) => e.t === "mousemove");
    expect(moves.reduce((s, m) => s + m.n, 0), "the pointer travels to the target").toBeGreaterThanOrEqual(5);
  }, T);

  it("the first click on six fresh pages is humanized every time", async () => {
    // The covered-point probe used to time out on ~1 in 6 FIRST clicks of a fresh page (slow first
    // frames, not an overlay) and send a plain native click instead.
    for (let k = 0; k < 6; k++) {
      const p = await browser.newPage();
      try {
        await p.goto(FIXTURE);
        await p.click("#btn");
        const all = (await p.evaluate("window.__ev")) as E[];
        const moves = all.filter((e) => e.t === "mousemove").reduce((s, m) => s + m.n, 0);
        const d = all.find((e) => e.t === "mousedown" && e.id === "btn");
        const u = all.find((e) => e.t === "mouseup" && e.id === "btn");
        expect(moves, "first click went out unhumanized").toBeGreaterThanOrEqual(5);
        expect(d && u && u.ts - d.ts >= 40).toBe(true);
      } finally {
        await p.close();
      }
    }
  }, 6 * T);

  it("locator strictness still raises", async () => {
    await expect(page.locator("button.dup").click({ timeout: 2000 })).rejects.toThrow(/strict mode violation/i);
  }, T);

  it("a missing element fails after ONE timeout", async () => {
    for (const act of [() => page.click("#nope", { timeout: 1500 }), () => page.fill("#nope", "x", { timeout: 1500 }),
      () => page.locator("#nope").click({ timeout: 1500 })]) {
      const t0 = Date.now();
      await expect(act()).rejects.toBeInstanceOf(errors.TimeoutError);
      expect(Date.now() - t0, "a missing element must not wait the timeout twice").toBeLessThan(2800);
    }
  }, T);

  const btnBox = async () => {
    await page.locator("#btn").scrollIntoViewIfNeeded(); // raw mouse input does not scroll
    return (await page.locator("#btn").boundingBox())!;
  };

  it("mouse.click(x, y) with the right button", async () => {
    const box = await btnBox();
    await reset();
    await page.mouse.click(box.x + 20, box.y + 15, { button: "right" });
    expect((await ev("contextmenu", "btn")).length).toBeGreaterThan(0);
  }, T);

  it("mouse.dblclick(x, y)", async () => {
    const box = await btnBox();
    await reset();
    await page.mouse.dblclick(box.x + 30, box.y + 20);
    const dbl = await ev("dblclick", "btn");
    expect(dbl.length).toBeGreaterThan(0);
    expect(dbl[0].tr).toBe(true);
    const downs = await ev("mousedown", "btn"), ups = await ev("mouseup", "btn");
    for (let i = 0; i < Math.min(downs.length, ups.length); i++) expect(ups[i].ts - downs[i].ts).toBeGreaterThanOrEqual(40);
  }, T);

  it("check with trial changes nothing", async () => {
    await reset();
    await page.check("#cb", { trial: true });
    await page.locator("#cb").check({ trial: true });
    expect(await page.isChecked("#cb")).toBe(false);
    expect(await ev("click", "cb")).toEqual([]);
    expect(await ev("mousedown", "cb")).toEqual([]);
  }, T);

  it("check, uncheck and setChecked are humanized", async () => {
    await reset();
    await page.check("#cb");
    expect(await page.isChecked("#cb")).toBe(true);
    expect((await ev("mousedown", "cb")).length).toBeGreaterThan(0);
    expect((await ev("click", "cb")).every((e) => e.tr)).toBe(true);
    await page.locator("#cb").setChecked(false);
    expect(await page.isChecked("#cb")).toBe(false);
    await page.uncheck("#cb"); // already unchecked: nothing happens
    expect(await page.isChecked("#cb")).toBe(false);
    expect((await ev("click", "cb")).length).toBe(2);
  }, T);

  // ------------------------------------------------------------------------------------- text
  it("fill types text with held keys", async () => {
    await reset();
    await page.fill("#name", "Ada Lovelace");
    expect(await page.inputValue("#name")).toBe("Ada Lovelace");
    await assertHumanKeys("name");
  }, T);

  it("fill clears a prefilled field with held keys", async () => {
    await reset();
    await page.fill("#prefilled", "new");
    expect(await page.inputValue("#prefilled")).toBe("new");
    const holds = new Map((await keyHolds("prefilled")).filter(([k]) => ["Control", "Meta", "a", "Backspace"].includes(k)));
    expect(holds.has("Backspace")).toBe(true);
    expect(holds.get("Backspace")!).toBeGreaterThanOrEqual(MIN_HOLD_MS);
    expect(holds.has("Control") || holds.has("Meta")).toBe(true);
    for (const d of holds.values()) expect(d).toBeGreaterThanOrEqual(MIN_HOLD_MS);
  }, T);

  it("fill on an empty field presses no clear keys", async () => {
    typos(0);
    await reset();
    await page.fill("#bio", "hello");
    expect(await page.inputValue("#bio")).toBe("hello");
    expect((await ev("keydown", "bio")).filter((e) => ["Backspace", "Control", "Meta"].includes(e.key))).toEqual([]);
  }, T);

  it("clear() empties the field", async () => {
    await page.fill("#prefilled", "abc");
    await reset();
    await page.locator("#prefilled").clear();
    expect(await page.inputValue("#prefilled")).toBe("");
    await assertHumanKeys("prefilled");
  }, T);

  it("getByLabel and getByRole are humanized", async () => {
    await reset();
    await page.getByLabel("City").fill("Paris");
    expect(await page.inputValue("#city")).toBe("Paris");
    await assertHumanKeys("city");
    await reset();
    await page.getByRole("textbox", { name: "City" }).pressSequentially("!");
    expect(await page.inputValue("#city")).toBe("Paris!");
    await assertHumanKeys("city");
  }, T);

  it(".filter() and .nth() locators are humanized", async () => {
    await reset();
    await page.locator("input[type=text]").filter({ hasNotText: "zzz" }).nth(0).fill("first");
    expect(await page.inputValue("#name")).toBe("first");
    await assertHumanKeys("name");
  }, T);

  it("a field under a floating label is typed, not filled by script", async () => {
    // The label covers every probe point of the field: a person clicks the label (it passes the focus
    // on) and types; the fill must not fall back to Playwright's insertText.
    typos(0);
    await reset();
    await page.fill("#float", "abc");
    expect(await page.inputValue("#float")).toBe("abc");
    expect((await ev("keydown", "float")).map((e) => e.key)).toEqual(["a", "b", "c"]);
    await assertHumanKeys("float");
  }, T);

  it("textarea newlines and contenteditable", async () => {
    await page.fill("#bio", "two\nlines");
    expect(await page.inputValue("#bio")).toBe("two\nlines");
    await page.fill("#ce", "hello world");
    expect(await page.locator("#ce").textContent()).toBe("hello world");
  }, T);

  it("press holds the key", async () => {
    await page.fill("#name", "x");
    await reset();
    await page.press("#name", "Backspace");
    expect(await page.inputValue("#name")).toBe("");
    await assertHumanKeys("name");
  }, T);

  // ------------------------------------------------------------------------------------- typos
  it("forced typos are corrected", async () => {
    typos(0.35);
    await reset();
    const text = "the quick brown fox jumps over 2024";
    await page.fill("#name", text);
    expect(await page.inputValue("#name")).toBe(text);
    expect((await pressed("Backspace", "name")).length, "with a 35 % rate some slip had to be corrected").toBeGreaterThan(0);
    await assertHumanKeys("name");
  }, T);

  it.each([
    ["#email", "ada@example.com"], ["#pw", "s3cretPassw0rd"], ["#num", "123456"], ["#tel", "5551234567"],
    ["#url", "https://example.com/a"], ["#otp", "123456"],
  ])("no typos where a slip breaks the field: %s", async (sel, value) => {
    typos(0.6);
    await reset();
    await page.fill(sel, value);
    expect(await page.inputValue(sel)).toBe(value);
    expect(await pressed("Backspace", sel.slice(1)), `no slips in ${sel}`).toEqual([]);
  }, T);

  it("keyboard.type into a focused password makes no slips", async () => {
    typos(0.6);
    await page.click("#pw");
    await reset();
    await page.keyboard.type("abcdefgh");
    expect(await page.inputValue("#pw")).toBe("abcdefgh");
    expect(await pressed("Backspace", "pw")).toEqual([]);
  }, T);

  it("typos respect maxlength", async () => {
    typos(1.0);
    for (let i = 0; i < 3; i++) {
      await page.fill("#zip", "12345");
      expect(await page.inputValue("#zip")).toBe("12345");
    }
  }, T);

  it("typos in a masked field never delete real digits", async () => {
    typos(1.0);
    for (let i = 0; i < 3; i++) {
      await page.fill("#masked", "24680");
      expect(await page.inputValue("#masked")).toBe("24680");
    }
  }, T);

  // ------------------------------------------------------------------------------------ select
  it("select by label fires one trusted change", async () => {
    await reset();
    expect(await page.selectOption("#country", "Germany")).toEqual(["ger25"]);
    expect(await page.inputValue("#country")).toBe("ger25");
    const ch = await changes("country");
    expect(ch.length).toBe(1);
    expect(ch[0].tr).toBe(true);
    expect((await ev("mousedown", "country")).length, "the drop-down was opened with the mouse").toBeGreaterThan(0);
  }, T);

  it.each([
    ["value", "swi50", "swi50"], ["index", { index: 3 }, "and3"], ["label", { label: "Japan" }, "jap35"],
    ["value as label", "United Kingdom", "uni53"], ["value-only object", { value: "swi50" }, "swi50"],
  ] as const)("select by %s", async (_, values, want) => {
    await reset();
    expect(await page.selectOption("#country", values as any)).toEqual([want]);
    expect(await page.inputValue("#country")).toBe(want);
    expect((await changes("country")).length).toBe(1);
  }, T);

  it("select through a locator", async () => {
    await reset();
    expect(await page.locator("#country").selectOption("France")).toEqual(["fra23"]);
    expect((await changes("country")).length).toBe(1);
  }, T);

  it("select far down a long list is quick", async () => {
    await reset();
    const t0 = Date.now();
    await page.selectOption("#country", "Uruguay");
    expect(await page.inputValue("#country")).toBe("uru55");
    expect((await changes("country")).length).toBe(1);
    expect(Date.now() - t0).toBeLessThan(10_000);
  }, T);

  it("selecting the current option fires nothing", async () => {
    await reset();
    await page.selectOption("#country", "afg0");
    expect(await changes("country")).toEqual([]);
  }, T);

  it("duplicate labels", async () => {
    await reset();
    await page.selectOption("#dup", "p2");
    expect(await page.inputValue("#dup")).toBe("p2");
    expect((await changes("dup")).length).toBe(1);
  }, T);

  it("select skips disabled rows", async () => {
    await reset();
    await page.selectOption("#dis", "e");
    expect(await page.inputValue("#dis")).toBe("e");
    expect((await changes("dis")).length).toBe(1);
  }, T);

  it("a list box scrolls with the wheel", async () => {
    await reset();
    await page.selectOption("#lb", "opt17");
    expect(await page.inputValue("#lb")).toBe("opt17");
    expect((await ev("wheel", "lb")).length + (await ev("wheel")).length, "the box is scrolled by the wheel, not by script").toBeGreaterThan(0);
    const ch = await changes("lb");
    expect(ch.length).toBeGreaterThan(0);
    expect(ch.every((c) => c.tr)).toBe(true);
  }, T);

  it("multi-select with Ctrl-click", async () => {
    await reset();
    const got = await page.selectOption("#ms", ["b", "d", "g"]);
    expect([...got].sort()).toEqual(["b", "d", "g"]);
    const sel = await page.$eval("#ms", (e: any) => [...e.selectedOptions].map((o: any) => o.value));
    expect(sel).toEqual(["b", "d", "g"]);
    expect((await pressed("Control")).length + (await pressed("Meta")).length).toBeGreaterThan(0);
    expect((await changes("ms")).every((c) => c.tr)).toBe(true);
  }, T);

  // ------------------------------------------------------------------------------------- range
  it("a range reaches the exact value", async () => {
    await reset();
    await page.fill("#rng", "145");
    expect(await page.inputValue("#rng")).toBe("145");
    const inputs = await ev("input", "rng");
    expect(inputs.length).toBeGreaterThan(0);
    expect(inputs.every((e) => e.tr)).toBe(true);
    expect((await ev("pointerdown", "rng")).length, "moved with the pointer, not set by script").toBeGreaterThan(0);
  }, T);

  it("range values Playwright rejects are rejected", async () => {
    // Off the step, out of range, not a number: Playwright raises "Malformed value"; the humanized
    // fill must too, never a silently snapped or clamped value.
    for (const bad of ["13", "250", "nan"]) {
      await expect(page.fill("#rng2", bad, { timeout: 3000 }), bad).rejects.toThrow();
    }
    await page.fill("#rng2", "15");
    expect(await page.inputValue("#rng2")).toBe("15");
  }, T);

  it("a right-to-left range", async () => {
    await page.fill("#rngrtl", "30");
    expect(await page.inputValue("#rngrtl")).toBe("30");
  }, T);

  // ---------------------------------------------------------------------------- date and time
  it.each([
    ["#d", "2026-10-11"], ["#dpre", "2026-10-11"], ["#t", "17:45"], ["#t", "09:05"], ["#ts", "17:45:30"],
    ["#m", "2026-10"], ["#w", "2026-W41"], ["#dtl", "2026-10-11T09:30"],
  ])("date-like field %s is typed segment by segment: %s", async (sel, value) => {
    await reset();
    await page.fill(sel, value);
    expect(await page.inputValue(sel)).toBe(value);
    const ident = sel.slice(1);
    expect((await ev("keydown", ident)).length, "typed with the keyboard, not set by script").toBeGreaterThan(0);
    expect((await ev("mousedown", ident)).length, "clicked into the field first").toBeGreaterThan(0);
    expect((await ev()).filter((e) => e.id === ident && ["input", "change"].includes(e.t)).every((e) => e.tr)).toBe(true);
  }, T);

  it("color goes through Playwright", async () => {
    await page.fill("#col", "#ff0000");
    expect(await page.inputValue("#col")).toBe("#ff0000");
  }, T);

  it("fill on a checkbox raises like Playwright", async () => {
    await expect(page.fill("#cb", "x", { timeout: 2000 })).rejects.toThrow();
  }, T);

  // ----------------------------------------------- cases the first verification found missing
  it("a frame-locator fill replaces the value", async () => {
    await page.frameLocator("#fr").locator("#fi").fill("new");
    expect(await page.frameLocator("#fr").locator("#fi").inputValue()).toBe("new");
  }, T);

  it("a covered field still ends with the value, and no key goes to the page", async () => {
    await reset();
    await page.fill("#cov", "new", { timeout: 5000 });
    expect(await page.inputValue("#cov")).toBe("new");
    const stray = (await ev("keydown")).filter((e) => ["", "BODY", "covlay"].includes(e.id));
    expect(stray, "keys went to the page instead of the field").toEqual([]);
  }, T);

  it("a reformatting mask is typed once", async () => {
    await reset();
    await page.fill("#phone", "5551234567");
    expect(await page.inputValue("#phone")).toBe("(555) 123-4567");
    const digits = (await ev("keydown", "phone")).filter((e) => /^\d$/.test(e.key));
    expect(digits.length, `typed ${digits.length} digits for a 10-digit number`).toBe(10);
  }, T);

  it("force on hidden targets does not wait", async () => {
    const t0 = Date.now();
    await page.click("#hiddenbtn", { force: true, timeout: 3000 });
    await page.fill("#hiddendate", "2026-10-11", { force: true, timeout: 3000 });
    expect(Date.now() - t0).toBeLessThan(2500);
    expect(await page.inputValue("#hiddendate")).toBe("2026-10-11");
  }, T);

  it("type into a hidden helper textarea is quick and right", async () => {
    const t0 = Date.now();
    await page.type("#helper", "abc", { timeout: 3000 });
    expect(await page.inputValue("#helper")).toBe("abc");
    expect(Date.now() - t0).toBeLessThan(2500);
  }, T);

  it("press on a button activates it once (page and locator)", async () => {
    await page.press("#counter", "Enter");
    expect(await page.evaluate("window.__clicks || 0")).toBe(1);
    await page.locator("#counter").press("Enter");
    expect(await page.evaluate("window.__clicks || 0")).toBe(2);
  }, T);

  it("the caller's default timeout is honoured", async () => {
    page.setDefaultTimeout(1500);
    const t0 = Date.now();
    await expect(page.click("#nope")).rejects.toBeInstanceOf(errors.TimeoutError);
    await expect(page.fill("#nope", "x")).rejects.toBeInstanceOf(errors.TimeoutError);
    await expect(page.selectOption("#nope", "x")).rejects.toBeInstanceOf(errors.TimeoutError);
    expect(Date.now() - t0, "each wait must be the page's 1.5 s, not a hard-coded 30 s").toBeLessThan(6500);
  }, T);

  it("check on a checkbox that cancels its click clicks once and raises", async () => {
    await reset();
    await expect(page.locator("#stubborn").check({ timeout: 3000 })).rejects.toThrow();
    expect((await ev("click", "stubborn")).length).toBe(1);
  }, T);

  it("delay 0 still holds the button", async () => {
    await reset();
    await page.click("#btn", { delay: 0 });
    const d = (await ev("mousedown", "btn"))[0], u = (await ev("mouseup", "btn"))[0];
    expect(u.ts - d.ts).toBeGreaterThanOrEqual(40);
  }, T);

  it("select-all holds the modifier before the A, and lets it go after", async () => {
    await reset();
    await page.fill("#prefilled", "x");
    const ks = (await ev()).filter((e) => (e.t === "keydown" || e.t === "keyup") && e.id === "prefilled");
    const modDown = ks.find((e) => e.t === "keydown" && ["Control", "Meta"].includes(e.key))!;
    const aDown = ks.find((e) => e.t === "keydown" && e.key === "a")!;
    const aUp = ks.find((e) => e.t === "keyup" && e.key === "a")!;
    const modUp = ks.find((e) => e.t === "keyup" && ["Control", "Meta"].includes(e.key))!;
    expect(aDown.ts - modDown.ts, "the modifier leads the A like a finger").toBeGreaterThanOrEqual(30);
    expect(modUp.ts - aUp.ts, "and comes up just after it").toBeGreaterThanOrEqual(15);
  }, T);

  it("a select under a floating overlay opens and picks once", async () => {
    await reset();
    expect(await page.selectOption("#selfloat", "e")).toEqual(["e"]);
    expect(await page.inputValue("#selfloat")).toBe("e");
    const ch = await changes("selfloat");
    expect(ch.length).toBe(1);
    expect(ch[0].tr).toBe(true);
  }, T);

  it("the typo gate reads through shadow roots and fails closed in frames", async () => {
    await page.click("#spw #inner");
    expect(((await worldFor(page).evaluate<any>(ISO_ACTIVE_FIELD)) ?? {}).type).toBe("password");
    await page.frameLocator("#fr").locator("#fpw").click();
    expect(await worldFor(page).evaluate(ISO_ACTIVE_FIELD)).toEqual({ frame: true });
  }, T);

  it("no slips in a password inside a shadow root", async () => {
    typos(1.0);
    await reset();
    await page.fill("#spw #inner", "abcdef");
    expect(await page.inputValue("#spw #inner")).toBe("abcdef");
    expect(await pressed("Backspace", "spw")).toEqual([]);
  }, T);

  // Node only: one page has one pointer and one keyboard focus, so concurrent calls take turns.
  it("a Promise.all of fills on one page types each field itself, one after another", async () => {
    typos(0);
    await reset();
    await Promise.all([page.fill("#name", "alpha one"), page.fill("#bio", "beta two"), page.fill("#city", "Gamma")]);
    expect(await page.inputValue("#name")).toBe("alpha one");
    expect(await page.inputValue("#bio")).toBe("beta two");
    expect(await page.inputValue("#city")).toBe("Gamma");
    // Interleaved, the keys of one fill landed in another field, the check failed and Playwright set
    // the values with insertText: the end values were right, but no field was typed once, in order.
    for (const [id, want] of [["name", "alpha one"], ["bio", "beta two"], ["city", "Gamma"]]) {
      const keys = (await ev("keydown", id)).map((e) => e.key).filter((k) => k !== "Shift").join("");
      expect(keys, `#${id} was not typed once, in order`).toBe(want);
    }
  }, T);

  // ----------------------------------------------- cases verifier B found (SC 197 verify-final-B)
  // Counts every read of Node.prototype.textContent made in the PAGE's world after it is installed (the
  // fixture's own recorder uses a getter saved before this runs). Playwright computes a preview for
  // every main-world element handle it makes, and that preview reads textContent where the page can see.
  const COUNT_TEXT_READS = `() => { const d = Object.getOwnPropertyDescriptor(Node.prototype, 'textContent');
  window.__reads = 0;
  Object.defineProperty(Node.prototype, 'textContent', { configurable: true, enumerable: d.enumerable,
    get() { window.__reads++; return d.get.call(this); }, set(v) { d.set.call(this, v); } }); }`;
  // Where the pointer went, move by move -- each pointermove's COALESCED moves, since a busy renderer
  // merges a frame's moves into one event (one glide arrived as a single 224 px step on the .NET
  // port) -- and every pointerdown a WINDOW capture listener sees (it runs
  // before anything Playwright installs during an action, so a press Playwright blocks is still seen).
  const TRACK_POINTER = `() => { window.__mv = []; window.__downs = [];
  addEventListener('pointermove', e => { const l = e.getCoalescedEvents ? e.getCoalescedEvents() : [];
    for (const c of (l.length ? l : [e])) __mv.push([c.clientX, c.clientY]); }, true);
  addEventListener('pointerdown', e => __downs.push([e.target.id, e.button, e.isTrusted]), true); }`;
  // page.evaluate(string) evaluates an EXPRESSION: wrap the function source so it is called.
  const run = (fnSource: string) => page.evaluate(`(${fnSource})()`);

  it("a long list is typed through its spaced label", async () => {
    // "Item 371" of 400 "Item NNN": the space is typed too. Without it the prefix stopped at "item" and
    // the other 370 rows were single ArrowDowns -- 68 s.
    await reset();
    const t0 = Date.now();
    expect(await page.selectOption("#long", "Item 371")).toEqual(["v371"]);
    expect(Date.now() - t0).toBeLessThan(15_000);
    const ch = await changes("long");
    expect(ch.length).toBe(1);
    expect(ch[0].tr).toBe(true);
  }, T);

  it("an untypeable long list holds the arrow key", async () => {
    await reset();
    const t0 = Date.now();
    expect(await page.selectOption("#same", "s100")).toEqual(["s100"]);
    expect(Date.now() - t0, "100 rows are crossed with a held key, not 100 separate presses").toBeLessThan(20_000);
    const ch = await changes("same");
    expect(ch.length).toBe(1);
    expect(ch[0].tr).toBe(true);
  }, T);

  it.each(["m10", "m14", "m03"])("a list-box row that needs a mid-list scroll: %s", async (value) => {
    // The old loop read the box mid-animation, wheeled the wrong way and oscillated, then fell back to
    // an untrusted native change.
    await page.evaluate("document.getElementById('lbmid').scrollTop = 0");
    await reset();
    expect(await page.selectOption("#lbmid", value)).toEqual([value]);
    expect(await page.inputValue("#lbmid")).toBe(value);
    expect((await ev()).filter((e) => !e.tr), "every event trusted: no native fallback").toEqual([]);
    expect((await ev("wheel")).length).toBeLessThanOrEqual(6);
    expect((await ev("mousedown", "OPTION")).length, "the row itself was clicked").toBeGreaterThan(0);
  }, T);

  it("a click does not jump the pointer or press twice", async () => {
    // The covered-point check used to be a Playwright TRIAL click, which moved the real pointer onto the
    // target and back and fired a pointerdown that only its own listener blocked.
    await run(TRACK_POINTER);
    await page.mouse.move(60, 60);
    for (const [sel, button] of [["#btn", "left"], ["#btn", "right"], ["#counter", "left"]] as const) {
      await page.evaluate("__mv.length = 0; __downs.length = 0");
      await page.click(sel, { button });
      const mv = (await page.evaluate("__mv")) as [number, number][];
      const jumps = mv.slice(1).map((b, k) => Math.hypot(b[0] - mv[k][0], b[1] - mv[k][1]));
      const worst = Math.max(0, ...jumps);
      expect(mv.length).toBeGreaterThan(0);
      expect(worst, `a jump of ${worst.toFixed(0)} px`).toBeLessThan(80);
      expect(await page.evaluate("__downs")).toEqual([[sel.slice(1), button === "left" ? 0 : 2, true]]);
    }
  }, T);

  it("dblclick presses twice, not three times", async () => {
    await run(TRACK_POINTER);
    await page.evaluate("__downs.length = 0");
    await page.dblclick("#btn");
    expect(await page.evaluate("__downs")).toEqual([["btn", 0, true], ["btn", 0, true]]);
  }, T);

  it("CSS targets cause no main-world reads", async () => {
    await run(COUNT_TEXT_READS);
    await page.click("#btn");
    await page.locator("#btn").click();
    await page.dblclick("#btn");
    await page.hover("#btn");
    await page.fill("#bio", "two words");
    await page.type("#bio", " more");
    await page.fill("#ce", "edited");
    await page.click("#far");
    expect(await page.evaluate("__reads")).toBe(0);
    expect(await page.inputValue("#bio")).toBe("two words more");
  }, T);

  it("role targets cause at most one main-world read", async () => {
    // getByRole cannot be resolved without Playwright's boundingBox(), whose handle preview reads
    // textContent once; scrolling the window to an off-screen one makes no further handle.
    await run(COUNT_TEXT_READS);
    await page.getByRole("button", { name: "Target" }).click();
    expect(await page.evaluate("__reads")).toBeLessThanOrEqual(1);
    await page.evaluate("scrollTo(0, 0); __reads = 0");
    await reset();
    await page.getByRole("button", { name: "Far away" }).click();
    expect(await page.evaluate("__reads")).toBeLessThanOrEqual(1);
    expect((await ev("click", "far")).map((e) => e.tr)).toEqual([true]);
  }, T);

  it.each(["css", "role"])("a button clipped by a scrolling container is scrolled to and clicked (%s)", async (how) => {
    // In the viewport, but cut off by its overflow:auto parent: the hit test sees that, and the
    // container is scrolled before the humanized click (it is not handed to a native click).
    await page.evaluate("document.getElementById('scroller').scrollIntoView({block: 'center'});"
      + "document.getElementById('scroller').scrollTop = 0");
    await reset();
    if (how === "css") await page.click("#innerbtn");
    else await page.getByRole("button", { name: "Inner one" }).click();
    expect((await ev("click", "innerbtn")).map((e) => e.tr)).toEqual([true]);
    expect(await page.evaluate("document.getElementById('scroller').scrollTop")).toBeGreaterThan(0);
    expect((await ev("mousemove")).length, "the pointer travelled to it").toBeGreaterThan(0);
  }, T);

  // --------------------------------------------------------------- SC 210: pointer-events:none, trial
  type W = { t: string; id: string; tr: boolean };
  const wev = async (): Promise<[W[], W[]]> => (await page.evaluate("[window.__wev, window.__fev || []]")) as [W[], W[]];
  const resetW = () => page.evaluate("window.__wev.length = 0; (window.__fev || []).length = 0");
  const moves = async () => (await ev("mousemove")).reduce((a, e) => a + e.n, 0);

  it("controls under a pointer-events:none layer are humanized", async () => {
    // A real press goes through such a layer, so the hit test must skip it too. With
    // ignorePointerEventsNone the layer answered and fill / selectOption / click fell back to stock
    // Playwright (SC 209: untrusted change, 6/6).
    await reset();
    await page.fill("#pen", "glass");
    expect(await page.inputValue("#pen")).toBe("glass");
    await assertHumanKeys("pen");
    await reset();
    await page.selectOption("#pensel", "c");
    expect(await page.inputValue("#pensel")).toBe("c");
    expect((await changes("pensel")).map((e) => e.tr), "one trusted change").toEqual([true]);
    await reset();
    await page.click("#penbtn");
    expect(await moves(), "the click glided (humanized), not a native jump").toBeGreaterThanOrEqual(5);
    expect((await ev("mousedown", "penbtn"))[0].tr).toBe(true);
  }, T);

  it("trial moves and presses nothing a window listener could see", async () => {
    // Playwright's own trial moved the pointer onto the target and dispatched pointerdown/pointerup/click
    // that listeners registered before its blocker saw (SC 209).
    await page.locator("#trialbtn").scrollIntoViewIfNeeded();
    await page.locator("#frb").scrollIntoViewIfNeeded();
    await resetW();
    await page.click("#trialbtn", { trial: true });
    await page.locator("#trialbtn").click({ trial: true });
    await page.dblclick("#trialbtn", { trial: true });
    await page.hover("#trialbtn", { trial: true });
    await expect(page.locator("#trialbtn").tap({ trial: true }))   // no hasTouch here: Playwright's own error
      .rejects.toThrow(/does not support tap/);
    await page.click("#trialbtn", { trial: true, position: { x: 5, y: 5 } });
    await page.check("#cb", { trial: true });
    await page.locator("#cb").setChecked(true, { trial: true });
    await page.locator("#btn").dragTo(page.locator("#trialbtn"), { trial: true });
    await page.click("#frb", { trial: true });   // the click point lies inside a child frame
    expect(await wev(), "a trial dispatched pointer events").toEqual([[], []]);
    expect(await page.isChecked("#cb")).toBe(false);
  }, T);

  it("trial fails like Playwright when a check never passes", async () => {
    await page.locator("#disbtn").scrollIntoViewIfNeeded();
    await resetW();
    for (const sel of ["#disbtn", "#hiddenbtn", "#cov"]) {   // disabled; hidden; covered by #covlay
      const t0 = Date.now();
      await expect(page.click(sel, { trial: true, timeout: 600 }), sel).rejects.toBeInstanceOf(errors.TimeoutError);
      expect(Date.now() - t0, sel).toBeLessThan(3000);
    }
    page.setDefaultTimeout(700);   // no timeout given: the page default, as in Playwright
    const t1 = Date.now();
    await expect(page.click("#disbtn", { trial: true })).rejects.toBeInstanceOf(errors.TimeoutError);
    expect(Date.now() - t1, "the page default timeout").toBeLessThan(3000);
    await page.hover("#disbtn", { trial: true, timeout: 600 });   // hover does not need enabled
    expect(await wev()).toEqual([[], []]);
  }, T);

  it("a click on a frame is humanized without a trial", async () => {
    // The click point of an <iframe> lies in its child frame: the hit test answers it with the <iframe>
    // element, so the click glides and presses with no Playwright trial before it.
    await page.locator("#frb").scrollIntoViewIfNeeded();
    await reset();
    await resetW();
    await page.click("#frb");
    const [main, inner] = await wev();
    const downs = inner.filter((e) => e.t === "pointerdown");
    expect(downs.map((d) => d.tr), JSON.stringify(inner)).toEqual([true]);
    expect(await moves(), "the click glided (humanized)").toBeGreaterThanOrEqual(5);
    expect(main.filter((e) => ["pointerdown", "mousedown", "click"].includes(e.t))).toEqual([]);
  }, T);

  // ------------------------------------------------- SC 210 lane F: maxlength, pre-press, bulk, date timing
  const LONG_VALUE = "A long note pasted into a text area: it runs well past the two hundred character limit so the "
    + "SDK inserts it in one go instead of typing it key by key, but a person still has to click into "
    + "the field before pasting anything into it, which is what this checks. The end.";

  /** ms from the last mousemove (end of its collapsed run) to each pointerdown on `id`. */
  const pressGaps = (events: E[], id: string): (number | null)[] => {
    let last: number | null = null;
    const out: (number | null)[] = [];
    for (const e of events) {
      if (e.t === "mousemove") last = e.te ?? e.ts;
      else if (e.t === "pointerdown" && e.id === id) out.push(last === null ? null : e.ts - last);
    }
    return out;
  };

  it("maxlength: only what fits is typed, no wipe and retype, no insertText", async () => {
    typos(0);
    await reset();
    await page.fill("#zip", "1234567");
    expect(await page.inputValue("#zip")).toBe("12345");
    expect((await ev("keydown", "zip")).map((e) => e.key)).toEqual(["1", "2", "3", "4", "5"]);
    const inputs = await ev("input", "zip");
    expect(inputs.map((e) => e.value)).toEqual(["1", "12", "123", "1234", "12345"]);
    expect(inputs.every((e) => e.tr)).toBe(true);
  }, T);

  it.each(["fill_text", "type", "slider_drag", "range_fill", "select", "listbox", "date", "check", "click"])(
    "every SDK press waits after the pointer arrives (%s)", async (act) => {
      await reset();
      const target = ({ fill_text: "name", type: "city", slider_drag: "rng", range_fill: "rng2", select: "country",
        listbox: "OPTION", date: "d", check: "cb", click: "btn" } as Record<string, string>)[act];
      if (act === "fill_text") await page.fill("#name", "Ada Lovelace");
      else if (act === "type") await page.type("#city", "Leiden");
      else if (act === "slider_drag") {
        const b = (await page.locator("#rng").boundingBox())!;
        const y = b.y + b.height / 2;
        await page.mouse.move(b.x + 30, y);
        await page.mouse.down();
        await page.mouse.move(b.x + 200, y);
        await page.mouse.up();
      } else if (act === "range_fill") await page.fill("#rng2", "15");
      else if (act === "select") await page.selectOption("#country", { label: "Iceland" });
      else if (act === "listbox") await page.selectOption("#lb", "opt3");
      else if (act === "date") await page.fill("#d", "1990-04-12");
      else if (act === "check") await page.check("#cb");
      else await page.click("#btn");
      const gaps = pressGaps(await ev(), target);
      expect(gaps.length, `a press reached #${target}`).toBeGreaterThan(0);
      expect(gaps.filter((g) => g === null || g < 40), "pressed too soon after the pointer arrived").toEqual([]);
    }, T);

  it("a bulk fill clicks into the field before inserting", async () => {
    expect(LONG_VALUE.length).toBeGreaterThan(200);
    await reset();
    await page.fill("#bio", LONG_VALUE);
    expect(await page.inputValue("#bio")).toBe(LONG_VALUE);
    const events = await ev();
    const downs = events.filter((e) => e.t === "pointerdown" && e.id === "bio");
    const inputs = events.filter((e) => e.t === "input" && e.id === "bio");
    expect(downs.length, "a press into the field").toBeGreaterThan(0);
    expect(downs[0].tr).toBe(true);
    expect(inputs.length).toBe(1);
    expect(inputs[0].it).toBe("insertText");
    expect(downs[0].ts).toBeLessThan(inputs[0].ts);
  }, T);

  it("the select open click is held as planned, not plus the popup's opening", async () => {
    // Node's motion binding cannot be stubbed, so the persona's own 60-150 ms plan is the reference:
    // the median page hold must stay inside it (it was ~206 ms in the SC 209 lab).
    const holds: number[] = [];
    for (const label of ["Iceland", "Norway", "Chile", "Kenya", "Malta", "Spain", "Japan", "Ghana"]) {
      await reset();
      await page.selectOption("#country", { label });
      const d = await ev("mousedown", "country"), u = await ev("mouseup", "country");
      expect(d.length && u.length).toBeTruthy();
      holds.push(u[0].ts - d[0].ts);
    }
    holds.sort((a, b) => a - b);
    expect(holds[holds.length >> 1], `holds ${holds.map(Math.round)}`).toBeLessThanOrEqual(160);
  }, 180_000);

  it("date segments use the typing model across identities, values exact", async () => {
    const VALUES: Record<string, string[]> = {
      "#d": ["1990-04-12", "2031-11-03"], "#t": ["14:35", "07:58"], "#m": ["2026-10", "1999-03"],
      "#w": ["2026-W41", "2019-W07"], "#dtl": ["2026-01-15T09:30", "1987-06-24T21:12"],
    };
    const gaps: number[] = [];
    let rolls = 0;
    for (const ident of ["dt-id-1", "dt-id-2", "dt-id-3"]) {
      const b = await launch({ executablePath: LIVE, headless: true, humanize: true, fingerprint: ident, quiet: true,
        args: ["--no-sandbox"] });
      try {
        const p = await b.newPage();
        await p.goto(FIXTURE);
        for (const [sel, values] of Object.entries(VALUES)) {
          for (const value of values) {
            await p.evaluate("window.__ev.length = 0");
            await p.fill(sel, value);
            expect(await p.inputValue(sel), `${ident} ${sel}`).toBe(value);
            const evs = (await p.evaluate("window.__ev")) as E[];
            const kd = evs.filter((e) => e.t === "keydown" && e.id === sel.slice(1));
            const ku = evs.filter((e) => e.t === "keyup" && e.id === sel.slice(1));
            for (let i = 1; i < kd.length; i++) if (kd[i].ts - kd[i - 1].ts < 2000) gaps.push(kd[i].ts - kd[i - 1].ts);
            kd.slice(0, -1).forEach((d, i) => {
              const up = ku.find((u) => u.code === d.code && u.ts >= d.ts);
              if (up && kd[i + 1].ts < up.ts) rolls++;
            });
          }
        }
      } finally {
        await b.close();
      }
    }
    const mean = gaps.reduce((a, b) => a + b, 0) / gaps.length;
    const sd = Math.sqrt(gaps.reduce((a, b) => a + (b - mean) ** 2, 0) / gaps.length);
    const cv = sd / mean;
    expect(cv, `CV ${cv.toFixed(2)} over ${gaps.length} gaps (rollover ${rolls})`).toBeGreaterThanOrEqual(0.3);
    expect(cv).toBeLessThanOrEqual(1.0);
  }, 600_000);

  // ------------------------------------------------- SC 210 fix 2: twins, masks, Playwright's error wording
  it("a MUI-style button is clicked humanized and its trial passes", async () => {
    // A ripple span with inset:0 and pointer-events:none fills the button: the old twin rule called every
    // such button covered, sent the click to stock Playwright and failed trial (verifier A).
    await page.locator("#muibtn").scrollIntoViewIfNeeded();
    await page.click("#muibtn", { trial: true, timeout: 1500 });
    await page.locator("#muibtn").click({ trial: true, timeout: 1500 });
    await page.hover("#muibtn", { trial: true, timeout: 1500 });
    await reset();
    await page.click("#muibtn");
    expect(await moves(), "the click glided (humanized), not a native jump").toBeGreaterThanOrEqual(5);
    expect((await ev("mousedown", "muibtn")).map((e) => e.tr)).toEqual([true]);
  }, T);

  it("a label-wrapped custom checkbox is checked humanized", async () => {
    await page.locator("#swlab").scrollIntoViewIfNeeded();
    await page.check("#swchk", { trial: true, timeout: 1500 });
    await reset();
    await page.check("#swchk", { position: { x: 20, y: 15 } });   // on the mark, off the input's own 3 px rim
    expect(await page.isChecked("#swchk")).toBe(true);
    expect(await moves(), "humanized, not stock Playwright").toBeGreaterThanOrEqual(5);
    expect((await ev("mousedown", "swmark")).map((e) => e.tr)).toEqual([true]);
    await reset();
    await page.locator("#swchk").uncheck();
    expect(await page.isChecked("#swchk")).toBe(false);
    expect(await moves()).toBeGreaterThanOrEqual(5);
  }, T);

  it("a decorative span under a button's text is not an overlay", async () => {
    await page.locator("#bgbtn").scrollIntoViewIfNeeded();
    await page.click("#bgbtn", { trial: true, timeout: 1500 });
    await reset();
    await page.click("#bgbtn", { position: { x: 60, y: 20 } });
    expect(await moves()).toBeGreaterThanOrEqual(5);
    expect((await ev("click", "bgtext"))[0].tr).toBe(true);
  }, T);

  it("a real overlay is still covered", async () => {
    await page.locator("#ovbtn").scrollIntoViewIfNeeded();
    await expect(page.click("#ovbtn", { trial: true, timeout: 600 })).rejects.toBeInstanceOf(errors.TimeoutError);
    await reset();
    await expect(page.click("#ovbtn", { timeout: 900 })).rejects.toBeInstanceOf(errors.TimeoutError);
    expect(await ev("mousedown", "ovbtn"), "no press reached the covered button").toEqual([]);
  }, T);

  for (const [sel, value, want, keys] of [
    ["#exp", "123456", "12/34", "12345"],                               // MM/YY on maxlength 5 (verifier B)
    ["#telmask", "5551234567890", "555-123-4567", "555123456789"],      // 3-3-4 dashes on maxlength 12
    ["#pcmask", "1234abx", "1234 AB", "1234abx"],                       // a script that formats and cuts
  ] as const) {
    it(`a mask that stops taking keys is typed once (${sel})`, async () => {
      typos(0);
      await reset();
      await page.fill(sel, value);
      expect(await page.inputValue(sel)).toBe(want);
      const downs = await ev("keydown", sel.slice(1));
      expect(downs.map((e) => e.key).join("")).toBe(keys);
      expect(downs.filter((e) => e.ctrl || e.meta || e.key === "Backspace"), "wiped and retyped").toEqual([]);
      expect(downs.every((e) => e.tr)).toBe(true);
    }, T);
  }

  const apiError = async (p: Promise<unknown>): Promise<[string, string]> => {
    try { await p; } catch (e: any) { return [e instanceof errors.TimeoutError ? "TimeoutError" : e?.name, String(e?.message).split("\n")[0]]; }
    return ["no error", ""];
  };

  it("errors name the API the caller used", async () => {
    // Playwright words an error after the method called ("page.click: Timeout 600ms exceeded."); the
    // humanized calls rejected with the inner call's ("locator.waitFor: ...", "locator.isChecked: ...").
    const L = (s: string) => page.locator(s);
    const calls: [string, () => Promise<unknown>][] = [
      ["page.click", () => page.click("#missing", { timeout: 600 })],
      ["page.dblclick", () => page.dblclick("#missing", { timeout: 600 })],
      ["page.hover", () => page.hover("#missing", { timeout: 600 })],
      ["page.fill", () => page.fill("#missing", "x", { timeout: 600 })],
      ["page.type", () => page.type("#missing", "x", { timeout: 600 })],
      ["page.press", () => page.press("#missing", "a", { timeout: 600 })],
      ["page.check", () => page.check("#missing", { timeout: 600 })],
      ["page.uncheck", () => page.uncheck("#missing", { timeout: 600 })],
      ["page.setChecked", () => page.setChecked("#missing", true, { timeout: 600 })],
      ["page.selectOption", () => page.selectOption("#missing", "a", { timeout: 600 })],
      ["locator.click", () => L("#missing").click({ timeout: 600 })],
      ["locator.dblclick", () => L("#missing").dblclick({ timeout: 600 })],
      ["locator.hover", () => L("#missing").hover({ timeout: 600 })],
      ["locator.fill", () => L("#missing").fill("x", { timeout: 600 })],
      ["locator.clear", () => L("#missing").clear({ timeout: 600 })],
      ["locator.press", () => L("#missing").press("a", { timeout: 600 })],
      ["locator.type", () => L("#missing").type("x", { timeout: 600 })],
      ["locator.pressSequentially", () => L("#missing").pressSequentially("x", { timeout: 600 })],
      ["locator.check", () => L("#missing").check({ timeout: 600 })],
      ["locator.uncheck", () => L("#missing").uncheck({ timeout: 600 })],
      ["locator.setChecked", () => L("#missing").setChecked(true, { timeout: 600 })],
      ["locator.selectOption", () => L("#missing").selectOption("a", { timeout: 600 })],
      ["locator.click(trial)", () => L("#disbtn").click({ trial: true, timeout: 600 })],
    ];
    const got: Record<string, [string, string]> = {}, want: Record<string, [string, string]> = {};
    for (const [api, fn] of calls) {
      got[api] = await apiError(fn());
      want[api] = ["TimeoutError", api.split("(")[0] + ": Timeout 600ms exceeded."];
    }
    expect(got).toEqual(want);
  }, T);

  it("check on a non-checkbox rejects with Playwright's own error", async () => {
    const native = await apiError(Object.getPrototypeOf(page).check.call(page, "#btn", { timeout: 600 }));
    expect(native).toEqual(["Error", "page.check: Error: Not a checkbox or radio button"]);
    expect(await apiError(page.check("#btn", { timeout: 600 }))).toEqual(native);
    expect(await apiError(page.uncheck("#btn", { timeout: 600 }))).toEqual(["Error", native[1].replace("check", "uncheck")]);
    expect(await apiError(page.locator("#btn").check({ timeout: 600 }))).toEqual(["Error", "locator" + native[1].slice(4)]);
    expect(await apiError(page.locator("#btn").setChecked(true, { timeout: 600 })))
      .toEqual(["Error", "locator.setChecked" + native[1].slice("page.check".length)]);
  }, T);

  it("tap without touch rejects like Playwright", async () => {
    const [kind, msg] = await apiError(page.locator("#btn").tap({ timeout: 600 }));
    expect(kind).toBe("Error");
    expect(msg.startsWith("locator.tap: The page does not support tap"), msg).toBe(true);
  }, T);

  it("a click presses the whole pixel its hit test proved", async () => {
    // DOM.getNodeForLocation answers for a whole pixel; a press at 971.6 next to a badge ending at 972 was
    // proven at 972 and landed on the badge, reported as a success (verifier B).
    for (let i = 0; i < 5; i++) {
      await reset();
      await page.click("#btn");
      const d: any = (await ev("pointerdown", "btn"))[0];
      expect(Number.isInteger(d.cx) && Number.isInteger(d.cy), `${d.cx}, ${d.cy}`).toBe(true);
    }
  }, T);
});
