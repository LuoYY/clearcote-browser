/**
 * The keyboard planner (keys.ts) against the SHARED vectors in sdk/testdata/humanize_typing_vectors.json,
 * the file the Python (tests/test_keys.py) and .NET (KeysTests.cs) suites read -- so the three SDKs plan
 * the same gaps, chords, repeats and pauses -- plus the Node half of the cadence parity (lane D, SC 232):
 * the humanized keyboard.type of this SDK must match the shared KS reference in distribution. On 0.51.0
 * Node drew a uniform 45-150 ms gap with the thinking pause REPLACING it (KS D ~0.6 against the reference).
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";

const forced = vi.hoisted(() => ({ roll: 0 as number | null }));
vi.mock("../src/motion.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("../src/motion.js")>();
  return { ...real, rolloverRate: (p: any) => (forced.roll === null ? real.rolloverRate(p) : forced.roll) };
});

import * as K from "../src/keys.js";
import { makePersona, mulberry32 } from "../src/motion.js";
import { attachHumanize, typoRate } from "../src/humanize.js";

const VEC = JSON.parse(readFileSync(fileURLToPath(new URL("../../testdata/humanize_typing_vectors.json", import.meta.url)), "utf8"));
const camel = (k: string) => k.replace(/_([a-z])/g, (_, c) => c.toUpperCase());
const traitsOf = (p: string) => K.typingTraits(makePersona(p).seed);
const close = (a: number[], b: number[], tol = 1e-9) => {
  expect(a.length).toBe(b.length);
  a.forEach((x, i) => expect(Math.abs(x - b[i]), `#${i}: ${x} vs ${b[i]}`).toBeLessThanOrEqual(tol));
};

describe("keys.ts against the shared vectors", () => {
  it("makePersona's existing fields are unchanged (the typing identity is a salted side stream)", () => {
    for (const c of VEC.persona_pin) {
      const p: any = makePersona(c.input);
      for (const [k, v] of Object.entries(c.persona)) {
        const ck = camel(k);
        if (ck in p) expect(p[ck], `${c.input} ${ck}`).toBe(v);
      }
      expect(p.seed).toBe(c.persona.seed);
    }
  });

  it("typing traits", () => {
    for (const c of VEC.typing_traits) {
      const t: any = traitsOf(c.persona);
      for (const [k, v] of Object.entries(c)) {
        if (k === "persona") continue;
        if (typeof v === "number") expect(Math.abs(t[camel(k)] - v), `${c.persona} ${k}`).toBeLessThanOrEqual(1e-12);
        else expect(t[camel(k)], `${c.persona} ${k}`).toBe(v);
      }
    }
  });

  it("finger map and bigram classes", () => {
    for (const c of VEC.finger_of) expect(K.fingerOf(c.ch), JSON.stringify(c.ch)).toEqual(c.want);
    for (const c of VEC.bigram_class) expect(K.bigramClass(c.prev, c.ch), c.prev + c.ch).toEqual([c.cls, c.jump]);
  });

  it("planned flights and gaps", () => {
    for (const c of VEC.gaps) {
      const t = traitsOf(c.persona), s = Array.from(c.text as string);
      let r = mulberry32(c.rng);
      close(s.slice(0, -1).map((ch, i) => K.flightMs(t, ch, s[i + 1], r)), c.flight_ms);
      r = mulberry32(c.rng);
      close(s.slice(0, -1).map((ch, i) => K.planGap(t, ch, s[i + 1], r)), c.gap_ms);
    }
  });

  it("chords, repeat, modifier timing and pauses", () => {
    for (const c of VEC.split_chord) expect(K.splitChord(c.key), c.key).toEqual([c.mods, c.key_out]);
    for (const c of VEC.timing) {
      const t = traitsOf(c.persona), r = mulberry32(c.rng);
      const rp = K.planRepeat(r);
      close([rp.firstMs, rp.gapMs, rp.releaseMs], c.repeat);
      close([K.modifierLeadMs(r), K.modifierLagMs(r)], [c.lead, c.lag]);
      close([0, 1, 2, 3].map(() => K.submitPauseMs(t, r)), c.submit);
      close([0, 1, 2, 3].map(() => K.tabPauseMs(t, r)), c.tab);
    }
  });

  it("the KS reference is this planner", () => {
    const t = traitsOf(VEC.ks_persona), r = mulberry32(VEC.ks_rng), s = Array.from(VEC.ks_text as string);
    close(s.slice(0, -1).map((ch, i) => K.planGap(t, ch, s[i + 1], r)), VEC.ks_reference);
  });

  it("the JS reads are the Python module's (same Tab order, blank point and selection logic)", () => {
    expect(K.TAB_DISTANCE).toContain(K.FOCUSABLE);
    expect(K.BLANK_POINT.startsWith("(a) =>")).toBe(true);
    expect(K.SELECTION_STATE.startsWith("() =>")).toBe(true);
  });
});

// ------------------------------------------------------------------------- the wrapper (fake clock)
type Ev = { op: string; k: string; ts: number; delay: number; held: string[]; rep?: boolean };

const fakePage = () => {
  const noop = async () => undefined;
  const ev: Ev[] = [];
  const held = new Set<string>();
  let value = "";
  let fieldType = "text";
  const apply = (k: string, mods: Set<string>) => {
    if (k === "Backspace") {
      if (mods.has("Control") || mods.has("Alt")) {
        let i = value.length;
        while (i && value[i - 1] === " ") i--;
        while (i && value[i - 1] !== " ") i--;
        value = value.slice(0, i);
      } else value = value.slice(0, -1);
    } else if (Array.from(k).length === 1 && !["Control", "Alt", "Meta", "ControlOrMeta"].some((m) => mods.has(m))) value += k;
  };
  const keyboard = {
    type: async (t: string) => { for (const c of Array.from(t)) apply(c, new Set()); },
    press: async (k: string, o: any = {}) => {
      ev.push({ op: "press", k, ts: Date.now(), delay: o.delay ?? 0, held: [...held] });
      const parts = k === "+" ? [k] : k.split("+");
      apply(parts[parts.length - 1], new Set([...parts.slice(0, -1), ...held]));
    },
    down: async (k: string) => {
      ev.push({ op: "down", k, ts: Date.now(), delay: 0, held: [...held], rep: held.has(k) });
      held.add(k);
      if (Array.from(k).length === 1 || k === "Backspace") apply(k, new Set([...held].filter((h) => h !== k)));
    },
    up: async (k: string) => { ev.push({ op: "up", k, ts: Date.now(), delay: 0, held: [...held] }); held.delete(k); },
    insertText: noop,
  };
  const cdp = {
    send: async (m: string, params: any = {}) => {
      if (m === "Page.getFrameTree") return { frameTree: { frame: { id: "MAIN" } } };
      if (m === "Page.createIsolatedWorld") return { executionContextId: 7 };
      if (m === "Runtime.evaluate") {
        const e: string = params.expression;
        if (e.includes("activeElement") && e.includes("inputmode")) return { result: { value: { type: fieldType, inputmode: null, autocomplete: null } } };
        if (e.includes("activeElement") && e.includes("isContentEditable ? e.textContent")) return { result: { value } };
        if (e.includes("innerWidth")) return { result: { value: [1280, 800] } };
        return { result: { value: null } };
      }
      return {};
    },
    detach: noop, on: () => undefined,
  };
  const locatorProto = {
    fill: noop, click: noop, type: noop, dblclick: noop, hover: noop, press: noop, pressSequentially: noop, clear: noop,
    tap: noop, check: noop, uncheck: noop, dragTo: noop, page: () => null,
  };
  const page: any = {
    mouse: { move: noop, click: noop, wheel: noop, down: noop, up: noop },
    keyboard, click: noop, hover: noop, dblclick: noop, fill: noop, press: noop, type: noop, focus: noop,
    evaluate: async () => undefined, on: () => undefined, mainFrame: () => ({}), waitForTimeout: noop,
    viewportSize: () => ({ width: 1280, height: 800 }),
    context: () => ({ newCDPSession: async () => cdp, browser: () => null }),
    locator: () => Object.create(locatorProto),
  };
  return { page, ev, get value() { return value; }, set value(v: string) { value = v; }, setType: (t: string) => { fieldType = t; } };
};

const ENV = "CLEARCOTE_HUMANIZE_TYPO_RATE";

/** Run `fn` on a fake clock: every sleep resolves at once, Date.now() moves by what was slept. */
const onFakeClock = async <T>(fn: () => Promise<T>): Promise<T> => {
  vi.useFakeTimers({ toFake: ["setTimeout", "Date"], now: 1_000_000 });
  try {
    const p = fn();
    p.catch(() => undefined); // a rejection is the caller's to assert, not an unhandled one
    await vi.runAllTimersAsync();
    return await p;
  } finally { vi.useRealTimers(); }
};

const seededRandom = (seed: number) => vi.spyOn(Math, "random").mockImplementation(mulberry32(seed));

const typed = async (seed: string, text: string, opts: { roll?: number | null; rate?: string; delay?: number; type?: string } = {}) => {
  forced.roll = opts.roll === undefined ? 0 : opts.roll;
  if (opts.rate === undefined) delete process.env[ENV]; else process.env[ENV] = opts.rate;
  const f = fakePage();
  if (opts.type) f.setType(opts.type);
  await onFakeClock(async () => {
    await attachHumanize({} as never, f.page, { humanize: true, seed });
    await f.page.keyboard.type(text, opts.delay ? { delay: opts.delay } : {});
  });
  return f;
};

const charPresses = (f: ReturnType<typeof fakePage>) => f.ev.filter((e) => e.op === "press" && Array.from(e.k).length === 1);

function ksD(a: number[], b: number[]): number {
  const x = [...a].sort((p, q) => p - q), y = [...b].sort((p, q) => p - q);
  let i = 0, j = 0, d = 0;
  while (i < x.length && j < y.length) {
    const v = Math.min(x[i], y[j]);
    while (i < x.length && x[i] === v) i++;
    while (j < y.length && y[j] === v) j++;
    d = Math.max(d, Math.abs(i / x.length - j / y.length));
  }
  return d;
}
const median = (xs: number[]) => { const s = [...xs].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };

describe("humanized keyboard (Node wrapper, fake clock)", () => {
  it("key gaps match the shared reference distribution (KS, 500 keys) -- the same as Python and .NET", async () => {
    const spy = seededRandom(1);
    try {
      const f = await typed(VEC.ks_persona, VEC.ks_text, { rate: "0" });
      const p = charPresses(f);
      // a fake press takes no time: the next keydown comes exactly the planned gap later
      const gaps = p.slice(1).map((e, i) => e.ts - p[i].ts);
      expect(gaps.length).toBe(500);
      // Date is whole ms here, the reference is not: compare at 1 ms resolution
      const d = ksD(gaps, (VEC.ks_reference as number[]).map((g) => Math.floor(g)));
      expect(d, `KS D=${d.toFixed(3)}`).toBeLessThan(1.95 * Math.sqrt(2 / 500));
    } finally { spy.mockRestore(); }
  }, 60_000);

  it("same-finger bigrams are slower than alternating ones (median ratio 1.2-1.8)", async () => {
    const SF = ["ed", "de", "ju", "ki", "lo", "fr", "sw", "ol"], ALT = ["en", "th", "ha", "so", "al", "ka", "fj", "dk"];
    const ratios: number[] = [];
    for (const [n, seed] of ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"].entries()) {
      const spy = seededRandom(100 + n);
      try {
        const words: string[] = [];
        for (let k = 0; k < 25; k++) words.push(...SF, ...ALT);
        words.sort(() => Math.random() - 0.5);
        const f = await typed(seed, words.join(" "), { rate: "0" });
        const p = charPresses(f);
        const sf: number[] = [], alt: number[] = [];
        for (let i = 1; i < p.length; i++) {
          const pair = p[i - 1].k + p[i].k, iv = p[i].ts - p[i - 1].ts + p[i - 1].delay; // keydown to keydown
          if (SF.includes(pair)) sf.push(iv); else if (ALT.includes(pair)) alt.push(iv);
        }
        ratios.push(median(sf) / median(alt));
      } finally { spy.mockRestore(); }
    }
    const m = median(ratios);
    expect(m, JSON.stringify(ratios)).toBeGreaterThanOrEqual(1.2);
    expect(m, JSON.stringify(ratios)).toBeLessThanOrEqual(1.8);
    for (const r of ratios) expect(r).toBeGreaterThanOrEqual(1.15);
  }, 60_000);

  it("a chord presses the modifier first (40-110 ms) and lifts it last (20-80 ms after the key's hold)", async () => {
    forced.roll = 0;
    const f = fakePage();
    await onFakeClock(async () => {
      await attachHumanize({} as never, f.page, { humanize: true, seed: "ha-1" });
      for (let j = 0; j < 10; j++) await f.page.keyboard.press("Control+a");
      await f.page.keyboard.press("ControlOrMeta+Shift+ArrowLeft");
    });
    const ev = f.ev;
    for (let j = 0; j < 10; j++) {
      const [d, p, u] = ev.slice(3 * j, 3 * j + 3);
      expect([d.op, d.k, p.op, p.k, u.op, u.k]).toEqual(["down", "Control", "press", "a", "up", "Control"]);
      expect(p.ts - d.ts).toBeGreaterThanOrEqual(39);
      expect(p.ts - d.ts).toBeLessThanOrEqual(111);
      // the fake press takes no time: the lag is measured from the press
      expect(u.ts - p.ts).toBeGreaterThanOrEqual(19);
      expect(u.ts - p.ts).toBeLessThanOrEqual(81);
      expect(p.delay).toBeGreaterThanOrEqual(45);
    }
    expect(ev.slice(30).map((e) => `${e.op}:${e.k}`)).toEqual(
      ["down:ControlOrMeta", "down:Shift", "press:ArrowLeft", "up:Shift", "up:ControlOrMeta"]);
  }, 30_000);

  it("a modifier the caller holds is honoured: no second Shift; a down/up pair still holds the key", async () => {
    forced.roll = 0;
    process.env[ENV] = "0";
    const f = fakePage();
    await onFakeClock(async () => {
      await attachHumanize({} as never, f.page, { humanize: true, seed: "ha-1" });
      await f.page.keyboard.down("Shift");
      await f.page.keyboard.press("Shift+a");
      await f.page.keyboard.type("bc");
      await f.page.keyboard.up("Shift");
      await f.page.keyboard.down("x");
      await f.page.keyboard.up("x");
    });
    expect(f.ev.filter((e) => e.op === "down" && e.k === "Shift").length).toBe(1);
    expect(f.ev.filter((e) => e.op === "press").map((e) => e.k)).toEqual(["a", "b", "c"]);
    const [dx, ux] = f.ev.filter((e) => e.k === "x");
    expect(ux.ts - dx.ts).toBeGreaterThanOrEqual(45);
  }, 30_000);

  it("press(key, {repeat: 12}) is an OS auto-repeat: 250-500 ms, then 25-40 ms, repeat flags, one keyup", async () => {
    const f = fakePage();
    await onFakeClock(async () => {
      await attachHumanize({} as never, f.page, { humanize: true, seed: "ha-2" });
      await f.page.keyboard.press("ArrowRight", { repeat: 12 });
    });
    const downs = f.ev.filter((e) => e.op === "down");
    expect(downs.length).toBe(12);
    expect(downs.map((e) => e.rep)).toEqual([false, ...Array(11).fill(true)]);
    expect(f.ev.filter((e) => e.op === "up").length).toBe(1);
    const gaps = downs.slice(1).map((e, i) => e.ts - downs[i].ts);
    expect(gaps[0]).toBeGreaterThanOrEqual(249);
    expect(gaps[0]).toBeLessThanOrEqual(501);
    for (const g of gaps.slice(1)) { expect(g).toBeGreaterThanOrEqual(24); expect(g).toBeLessThanOrEqual(41); }
  }, 30_000);

  it("Enter after typing waits 250-900 ms, Tab 120-400 ms, once per typed run", async () => {
    for (const [k, lo, hi] of [["Enter", 250, 900], ["Tab", 120, 400]] as const) {
      for (const seed of ["ha-1", "ha-4"]) {
        forced.roll = 0;
        process.env[ENV] = "0";
        const f = fakePage();
        await onFakeClock(async () => {
          await attachHumanize({} as never, f.page, { humanize: true, seed });
          await f.page.keyboard.type("hello");
          await f.page.keyboard.press(k);
          await f.page.keyboard.press(k);
        });
        const p = f.ev.filter((e) => e.op === "press");
        const gap = p[p.length - 2].ts - p[p.length - 3].ts; // the fake press takes no time
        expect(gap, `${k} ${seed}`).toBeGreaterThanOrEqual(lo - 1);
        expect(gap, `${k} ${seed}`).toBeLessThanOrEqual(hi + 1);
        expect(p[p.length - 1].ts - p[p.length - 2].ts).toBeLessThan(2);
      }
    }
  }, 30_000);

  it("keyboard.type keeps the caller's delay as a floor (and does not roll over under it)", async () => {
    const f = await typed("ha-5", "abcdefgh ijkl", { roll: null, rate: "0", delay: 300 });
    const p = charPresses(f);
    expect(p.length).toBe(13);
    for (let i = 1; i < p.length; i++) expect(p[i].ts - p[i - 1].ts).toBeGreaterThanOrEqual(300);
    expect(f.ev.filter((e) => e.op === "down")).toEqual([]);
  }, 30_000);

  it("the typo rate is the persona's (0.5-4 %), and Ctrl+Backspace word fixes keep the value exact", async () => {
    const text = "we keep a quiet mind and type plain words into the form until the day ends then rest slowly between lines ".repeat(12);
    const eligible = Array.from(text).filter((c) => /[a-z0-9]/.test(c)).length;
    const got: Record<string, [number, number]> = {};
    for (const [n, seed] of ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"].entries()) {
      const spy = seededRandom(200 + n);
      try {
        let slips = 0;
        for (let run = 0; run < 3; run++) {
          const f = await typed(seed, text);
          expect(f.value).toBe(text);
          let prevBs = false;
          for (const e of f.ev) {
            if (e.op !== "press") continue;
            const bs = e.k === "Backspace";
            if (bs && !prevBs) slips++;
            prevBs = bs;
          }
        }
        const r = slips / (3 * eligible), want = typoRate(makePersona(seed));   // lane B's per-persona rate (the single source)
        got[seed] = [r, want];
        expect(r, JSON.stringify(got)).toBeGreaterThanOrEqual(Math.max(0.003, 0.4 * want));
        expect(r, JSON.stringify(got)).toBeLessThanOrEqual(Math.min(0.05, 1.9 * want + 0.002));
      } finally { spy.mockRestore(); }
    }
    const spy = seededRandom(7);
    try {
      const t2 = "several ordinary words typed into a plain text field by somebody careful ".repeat(3);
      const f = await typed("ha-3", t2, { rate: "0.25" });
      expect(f.value).toBe(t2);
      expect(f.ev.some((e) => e.op === "press" && e.k === "Backspace" && e.held.includes("Control"))).toBe(true);
    } finally { spy.mockRestore(); }
  }, 120_000);

  it("a search field sometimes keeps a slip; a text field never", async () => {
    const spy = seededRandom(11);
    try {
      const text = "query words for the search box";
      let kept = 0;
      for (let k = 0; k < 30; k++) kept += (await typed("ha-2", text, { rate: "0.3", type: "search" })).value !== text ? 1 : 0;
      expect(kept).toBeGreaterThanOrEqual(1);
      for (let k = 0; k < 10; k++) expect((await typed("ha-2", text, { rate: "0.3", type: "text" })).value).toBe(text);
    } finally { spy.mockRestore(); }
  }, 120_000);

  it("same seed: equal typing traits, different per-key draws (no cross-session timing fingerprint)", async () => {
    const a = await typed("ha-1", "the same words typed twice", { rate: "0" });
    const b = await typed("ha-1", "the same words typed twice", { rate: "0" });
    const ga = charPresses(a).map((e) => e.ts - charPresses(a)[0].ts), gb = charPresses(b).map((e) => e.ts - charPresses(b)[0].ts);
    expect(ga).not.toEqual(gb);
    expect(K.typingTraits(makePersona("ha-1").seed)).toEqual(K.typingTraits(makePersona("ha-1").seed));
  }, 30_000);

  it("lane A's tempo scales the gaps (persona.tempo read at call time)", async () => {
    const med: number[] = [];
    for (const tempo of [1, 2]) {
      const spy = seededRandom(3);
      try {
        forced.roll = 0;
        process.env[ENV] = "0";
        const f = fakePage();
        await onFakeClock(async () => {
          await attachHumanize({} as never, f.page, { humanize: true, seed: "ha-2" });
          f.page._clearcotePersona.tempo = tempo;
          await f.page.keyboard.type((VEC.ks_text as string).slice(0, 200));
        });
        const p = charPresses(f);
        med.push(median(p.slice(1).map((e, i) => e.ts - p[i].ts)));
      } finally { spy.mockRestore(); }
    }
    expect(med[1] / med[0]).toBeGreaterThanOrEqual(1.7);
    expect(med[1] / med[0]).toBeLessThanOrEqual(2.3);
  }, 30_000);

  it("a strict error from the insertText fallback reaches the caller (not swallowed by the typing loop)", async () => {
    forced.roll = 0;
    process.env[ENV] = "0";
    const f = fakePage();
    const orig = f.page.keyboard.press;
    f.page.keyboard.press = async (k: string, o: any) => { if (k === "ö") throw new Error('Unknown key: "ö"'); return orig(k, o); };
    // strict through the real options object (lane A)
    await expect(onFakeClock(async () => {
      await attachHumanize({} as never, f.page, { humanize: "strict", seed: "ha-1" });
      await f.page.keyboard.type("aöb");
    })).rejects.toThrow(/insertText/);
  }, 30_000);
});
