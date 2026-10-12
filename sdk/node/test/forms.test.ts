/**
 * Humanized form input planning (src/forms.ts) against the SHARED vectors in
 * sdk/testdata/humanize_forms_vectors.json -- the same file the Python and .NET suites read, so the
 * three SDKs cannot drift apart -- plus the property checks of sdk/python/tests/test_forms.py, which
 * the vectors cannot express.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import * as F from "../src/forms.js";
import { hashSeed, mulberry32 } from "../src/motion.js";

const VEC = JSON.parse(readFileSync(fileURLToPath(new URL("../../testdata/humanize_forms_vectors.json", import.meta.url)), "utf8"));

const cases = (name: string): [number, any][] => (VEC[name] as any[]).map((c, i) => [i, c]);

describe("shared vectors", () => {
  it.each(cases("classify_field"))("classifyField #%i", (_, c) => {
    expect(F.classifyField(c.tag, c.type, c.ce)).toBe(c.want);
  });

  it.each(cases("typos_allowed"))("typosAllowed #%i", (_, c) => {
    expect(F.typosAllowed(c.field)).toBe(c.want);
  });

  it.each(cases("typo_eligible"))("typoEligible #%i", (_, c) => {
    expect(F.typoEligible(c.ch)).toBe(c.want);
  });

  it("digit neighbours are the shared map", () => {
    expect(F.NEARBY_DIGITS).toEqual(VEC.nearby_digits);
  });

  it.each(cases("typeahead_simulate"))("typeaheadSimulate #%i", (_, c) => {
    expect(F.typeaheadSimulate(c.labels, c.start, c.keys)).toBe(c.want);
  });

  it.each(cases("plan_select"))("planSelect #%i", (_, c) => {
    expect(F.planSelect(c.labels, c.current, c.target)).toEqual(c.want);
  });

  it.each(cases("match_option"))("matchOption #%i", (_, c) => {
    expect(F.matchOption(c.options, { value: c.value, index: c.index, label: c.label })).toBe(c.want);
  });

  it.each(cases("range_sanitize"))("rangeSanitize #%i", (_, c) => {
    // pytest.approx(want, rel=1e-12, abs=1e-9), as test_forms.py: one float step at 1.2e8 is 1.5e-8
    const tol = Math.max(1e-9, 1e-12 * Math.abs(c.want));
    expect(Math.abs(F.rangeSanitize(c.value, c.min, c.max, c.step) - c.want)).toBeLessThanOrEqual(tol);
  });

  it.each(cases("range_value_ok"))("rangeValueOk #%i", (_, c) => {
    expect(F.rangeValueOk(c.value, c.min, c.max, c.step)).toBe(c.want);
  });

  it.each(cases("formatted_equal"))("formattedEqual #%i", (_, c) => {
    expect(F.formattedEqual(c.got, c.want)).toBe(c.result);
  });

  it.each(cases("range_point"))("rangePoint #%i", (_, c) => {
    const [x, y] = F.rangePoint(c.box, c.fraction, c.rtl);
    expect(Math.abs(x - c.want[0])).toBeLessThan(1e-9);
    expect(Math.abs(y - c.want[1])).toBeLessThan(1e-9);
  });

  it.each(cases("range_nudge"))("rangeNudge #%i", (_, c) => {
    expect(F.rangeNudge(c.current, c.target, c.step)).toEqual(c.want);
  });

  it.each(cases("plan_datetime"))("planDatetime #%i", (_, c) => {
    expect(F.planDatetime(c.kind, c.segments, c.value, c.current)).toEqual(c.want);
  });

  it.each(cases("datetime_equal"))("datetimeEqual #%i", (_, c) => {
    expect(F.datetimeEqual(c.kind, c.a, c.b)).toBe(c.want);
  });

  it.each(cases("normalize_click_options"))("normalizeClickOptions #%i", (_, c) => {
    const got = F.normalizeClickOptions(c.options);
    // the vectors use Python's spelling for the count
    const { clickCount, ...rest } = got;
    expect({ ...rest, click_count: clickCount }).toEqual(c.want);
  });

  it.each(cases("engine_path_ok"))("enginePathOk #%i", (_, c) => {
    expect(F.enginePathOk(F.normalizeClickOptions(c.options))).toBe(c.want);
  });

  it("every vector group is exercised", () => {
    const tested = ["classify_field", "typos_allowed", "typo_eligible", "nearby_digits", "typeahead_simulate",
      "plan_select", "match_option", "range_sanitize", "range_value_ok", "range_point", "range_nudge",
      "plan_datetime", "datetime_equal", "formatted_equal", "normalize_click_options", "engine_path_ok",
      "fit_maxlength", "mask_kept", "pre_press_center", "pre_press_ms"];
    expect(Object.keys(VEC).filter((k) => !k.startsWith("_")).sort()).toEqual([...tested].sort());
  });
});

// ------------------------------------------------------------------ properties beyond the vectors
/** A seeded stand-in for Python's random.Random. */
const rngOf = (seed: number) => {
  const r = mulberry32(seed);
  return {
    random: r,
    randint: (a: number, b: number) => a + Math.floor(r() * (b - a + 1)),
    randrange: (n: number) => Math.floor(r() * n),
    choice: <T>(xs: readonly T[]): T => xs[Math.floor(r() * xs.length)],
    uniform: (a: number, b: number) => a + r() * (b - a),
  };
};

describe("properties", () => {
  it("typos never touch ineligible characters", () => {
    const rng = rngOf(1);
    for (const text of ["ABC!@# ", "ÉÀü", "--__..", "\t\n"]) {
      const chars = Array.from(text);
      for (let i = 0; i < chars.length; i++) {
        for (let k = 0; k < 200; k++) expect(F.planTypo(text, i, rng.random, 1.0)).toBeNull();
      }
    }
  });

  it("a typo's wrong key is a real neighbour of the same class", () => {
    const rng = rngOf(2);
    const text = "abcdefghijklmnopqrstuvwxyz0123456789";
    const seen = new Set<string>();
    for (let n = 0; n < 3000; n++) {
      const i = rng.randrange(text.length);
      const tp = F.planTypo(text, i, rng.random, 1.0);
      expect(tp).not.toBeNull();
      seen.add(tp!.kind);
      if (tp!.kind === "sub") {
        const ch = text[i], w = tp!.wrong;
        const isDigit = (c: string) => c >= "0" && c <= "9";
        expect(w).not.toBe(ch);
        expect(isDigit(w)).toBe(isDigit(ch));
        expect((isDigit(ch) ? F.NEARBY_DIGITS[ch] : F.NEARBY_LETTERS[ch]).includes(w)).toBe(true);
        for (const c of text.slice(i + 1, i + 1 + tp!.late)) expect(F.typoEligible(c) || c === " ").toBe(true);
        expect(tp!.late).toBeGreaterThanOrEqual(0);
        expect(tp!.late).toBeLessThanOrEqual(2);
      }
      if (tp!.kind === "swap") {
        expect(i + 1 < text.length && text[i + 1] !== text[i] && F.typoEligible(text[i + 1])).toBe(true);
      }
    }
    expect([...seen].sort()).toEqual(["double", "sub", "swap"]);
  });

  it("the typo rate is roughly honoured", () => {
    const rng = rngOf(3);
    let hits = 0;
    for (let n = 0; n < 20000; n++) if (F.planTypo("hello world", 1, rng.random, 0.02) !== null) hits++;
    expect(hits).toBeGreaterThanOrEqual(250); // 2 % of 20000 = 400
    expect(hits).toBeLessThanOrEqual(550);
  });

  it("a zero rate means no typos", () => {
    const rng = rngOf(4);
    for (let n = 0; n < 1000; n++) expect(F.planTypo("abc", 0, rng.random, 0.0)).toBeNull();
  });

  it("letter and digit neighbour maps never map a key to itself", () => {
    for (const [k, v] of [...Object.entries(F.NEARBY_LETTERS), ...Object.entries(F.NEARBY_DIGITS)]) {
      expect(v.includes(k)).toBe(false);
    }
  });

  /** An independent restatement of type_ahead.cc, to fuzz the simulator against. */
  const blinkReference = (labels: (string | null)[], start: number, keys: string): number => {
    const n = labels.length;
    if (n === 0) return start;
    let buf = "", rep: string | null = null, cur = start;
    for (const c of keys.toLowerCase()) {
      buf += c;
      let prefix: string, off: number;
      if (c === rep) { prefix = c; off = 1; }
      else if (buf.length > 1) { prefix = buf; off = 0; rep = null; }
      else { prefix = buf; off = 1; rep = c; }
      const i0 = ((cur >= 0 ? cur : 0) + off) % n;
      for (let k = 0; k < n; k++) {
        const j = (i0 + k) % n;
        const lab = labels[j];
        if (lab && lab.replace(/^\s+/, "").toLowerCase().startsWith(prefix)) { cur = j; break; }
      }
    }
    return cur;
  };

  it("type-ahead matches an independent restatement", () => {
    const rng = rngOf(5);
    const alphabet = "aabbcd";
    for (let n = 0; n < 3000; n++) {
      const labels = Array.from({ length: rng.randint(1, 8) }, () => {
        const s = Array.from({ length: rng.randint(0, 4) }, () => rng.choice(alphabet.split(""))).join("");
        return s || null;
      });
      const start = rng.randint(-1, labels.length - 1);
      const keys = Array.from({ length: rng.randint(1, 4) }, () => rng.choice("abcd".split(""))).join("");
      expect(F.typeaheadSimulate(labels, start, keys)).toBe(blinkReference(labels, start, keys));
    }
  });

  it("planSelect always lands on the target", () => {
    // Whatever the plan, simulating it (type-ahead, then arrows over enabled rows) must end on the
    // target -- the property the live Enter relies on.
    const rng = rngOf(6);
    const words = ["alpha", "alpine", "beta", "bet", "gamma", "Gamma", "delta", "  delta", "zeta", "", "eta",
      "eta", "Ünï", "item 1", "item 2", "x", "xx", "xxx"];
    for (let n = 0; n < 4000; n++) {
      const labels = Array.from({ length: rng.randint(1, 12) }, () => (rng.random() > 0.15 ? rng.choice(words) : null));
      const enabled = labels.map((l, i) => (l !== null ? i : -1)).filter((i) => i >= 0);
      if (!enabled.length) continue;
      const target = rng.choice(enabled);
      // any current row: an enabled one, none (-1), or a DISABLED one (a page can preselect it)
      const disabled = labels.map((l, i) => (l === null ? i : -1)).filter((i) => i >= 0);
      const current = rng.choice([...enabled, -1, ...disabled]);
      const plan = F.planSelect(labels, current, target);
      expect(plan).not.toBeNull();
      const land = plan!.keys
        ? F.typeaheadSimulate(labels, current >= 0 ? current : -1, plan!.keys)
        : (current >= 0 ? current : enabled[0]);
      let pos = land;
      const step = plan!.arrows > 0 ? 1 : -1;
      for (let k = 0; k < Math.abs(plan!.arrows); k++) {
        pos += step;
        while (labels[pos] === null) pos += step;
      }
      expect(pos, JSON.stringify({ labels, current, target, plan })).toBe(target);
      for (const ch of plan!.keys) expect(F.TYPEABLE.has(ch)).toBe(true);
    }
  });

  it("planSelect is never worse than arrows alone", () => {
    const rng = rngOf(7);
    for (let n = 0; n < 2000; n++) {
      const labels = Array.from({ length: rng.randint(2, 30) }, () => `opt${rng.randint(0, 30)}`);
      const cur = rng.randrange(labels.length), tgt = rng.randrange(labels.length);
      const plan = F.planSelect(labels, cur, tgt)!;
      expect(plan.keys.length + Math.abs(plan.arrows)).toBeLessThanOrEqual(Math.abs(tgt - cur));
    }
  });

  it("rangeSanitize always returns quickly on hostile attributes", () => {
    // A page chooses min/max/step: no combination may make the planner spin (it used to walk down in
    // a loop that never ended once a step was smaller than the float spacing at the value, which in
    // Node froze the whole event loop).
    const rng = rngOf(11);
    const t0 = Date.now();
    for (let n = 0; n < 5000; n++) {
      const hi = rng.choice([1e15, 123456789, 1e300, 5, 1e-300]);
      const st = rng.choice([1e-9, 1e-30, 1e-300, 0.5, 7]);
      const v = F.rangeSanitize(rng.choice([hi, hi * 10, -hi, 0.0, 1e308]), 0, hi, st);
      expect(Number.isNaN(v)).toBe(false);
    }
    expect(Date.now() - t0).toBeLessThan(5000);
  });

  it("rangeSanitize lands on the step grid inside the bounds", () => {
    const rng = rngOf(8);
    for (let n = 0; n < 3000; n++) {
      const lo = rng.choice([0, -10, 3, 0.5]);
      const hi = lo + rng.choice([1, 7, 100, 250.5]);
      const step = rng.choice([1, 2, 5, 0.1, 0.25, 7]);
      const v = F.rangeSanitize(rng.uniform(lo - 20, hi + 20), lo, hi, step);
      expect(v).toBeGreaterThanOrEqual(lo - 1e-9);
      expect(v).toBeLessThanOrEqual(hi + 1e-9);
      const k = (v - lo) / step;
      expect(Math.abs(k - Math.round(k))).toBeLessThan(1e-6);
    }
  });

  it("rangePoint stays on the track", () => {
    const rng = rngOf(9);
    for (let n = 0; n < 1000; n++) {
      const box = { x: rng.uniform(0, 500), y: rng.uniform(0, 500), width: rng.uniform(1, 600), height: 16 };
      const [x, y] = F.rangePoint(box, rng.random(), rng.random() < 0.5);
      expect(x).toBeGreaterThanOrEqual(box.x);
      expect(x).toBeLessThanOrEqual(box.x + box.width);
      expect(y).toBe(box.y + 8);
    }
  });

  it("every date-like plan types only digits and the ampm letters", () => {
    const kinds: Record<string, string[]> = {
      date: ["month-field", "day-field", "year-field"], time: ["hour-field", "minute-field", "ampm-field"],
      "datetime-local": ["month-field", "day-field", "year-field", "hour-field", "minute-field", "ampm-field"],
      week: ["week-field", "year-field"],
    };
    const values: Record<string, string> = { date: "2031-02-28", time: "23:59", "datetime-local": "1999-12-31T00:00", week: "2020-W01" };
    for (const [k, segs] of Object.entries(kinds)) {
      const plan = F.planDatetime(k, segs, values[k]);
      expect(plan && plan.length).toBeTruthy();
      for (const a of plan!) {
        if (a[0] === "type") expect(/^\d+$/.test(a[1]) || a[1] === "a" || a[1] === "p").toBe(true);
        else expect(["ArrowRight", "ArrowUp", "ArrowDown"]).toContain(a[1]);
      }
    }
  });

  it("segment names and nodes come from a pierced describeNode tree, in order", () => {
    const el = (pseudo: string | null, bid: number, children: any[] = [], shadow: any[] = []) => {
      const n: any = { nodeName: "DIV", attributes: pseudo ? ["pseudo", pseudo] : [], backendNodeId: bid };
      if (children.length) n.children = children;
      if (shadow.length) n.shadowRoots = shadow;
      return n;
    };
    const tree = el(null, 1, [], [el(null, 2, [
      el("-webkit-datetime-edit", 3, [
        el("-webkit-datetime-edit-month-field", 4), el("-webkit-datetime-edit-text", 5),
        el("-webkit-datetime-edit-day-field", 6), el("-webkit-datetime-edit-text", 7),
        el("-webkit-datetime-edit-year-field", 8)]),
      el("-webkit-calendar-picker-indicator", 9)])]);
    expect(F.segmentNamesFromTree(tree)).toEqual(["month-field", "day-field", "year-field"]);
    expect(F.segmentNodesFromTree(tree)).toEqual([["month-field", 4], ["day-field", 6], ["year-field", 8]]);
  });

  // Node-only details the port has to get right where JavaScript and Python differ.
  it("Python semantics where JavaScript differs: float parsing, half-even rounding, -0, casefold", () => {
    expect(F.pyFloat(" 12.5 ")).toBe(12.5);
    expect(F.pyFloat("")).toBeNull();
    expect(F.pyFloat("0x10")).toBeNull();
    expect(F.pyFloat("1_000")).toBe(1000);
    expect(F.pyFloat("abc")).toBeNull();
    expect(F.pyFloat(null)).toBeNull();
    expect(F.rangeAttrs("", "abc", "any")).toEqual([0, 100, null]);
    // Python's round(2.5) == 2: 0.25 / 0.1 rounds to 2 presses, 0.35 / 0.1 to 3 (4 would overshoot)
    expect(F.rangeNudge(0, 2.5, 1)).toEqual(["ArrowRight", 2]);
    expect(F.rangeNudge(0, 3.5, 1)).toEqual(["ArrowRight", 4]);
    // a disabled current row: one ArrowUp reaches the row above it (the start row never counts; this
    // used to plan 0 arrows, one short), and a step count is never -0 (toEqual tells -0 from 0)
    expect(F.planSelect(["A", null, "C"], 2, 1)).toBeNull();
    expect(F.planSelect(["Ü1", "Ü2", null], 2, 1)).toEqual({ keys: "", arrows: -1 });
    expect(F.planSelect(["Ü1", "Ü2", null], 2, 0)).toEqual({ keys: "", arrows: -2 });
    expect(F.planSelect(["Paris", "Paris", "London"], 2, 1)).toEqual({ keys: "", arrows: -1 });
    expect(Object.is(F.planSelect(["Ü1", "Ü2"], 1, 1)!.arrows, 0)).toBe(true);
    // a page-served range that would loop forever (max 123456789, step 1e-9) returns at once
    const t0 = Date.now();
    expect(F.rangeSanitize("1e12", "0", "123456789", "0.000000001")).toBeCloseTo(123456789, 6);
    expect(F.rangeSanitize("5", "0", "1e308", "1e-308")).toBe(5);   // too fine to count: unsnapped
    expect(Date.now() - t0).toBeLessThan(100);
    expect(F.normalizeClickOptions({ delay: Infinity }).delay).toBeNull();
    expect(F.normalizeClickOptions({ delay: "abc" }).delay).toBeNull();
    expect(F.typosAllowed({ type: "text", frame: true })).toBe(false);
    expect(F.formattedEqual("ÄBC-1", "äbc1")).toBe(true);
    // casefold: a sharp s folds to "ss", as Blink's FoldCase and Python's casefold do (lower-casing
    // alone would land on Strasbourg)
    expect(F.typeaheadSimulate(["Strasbourg", "Straße"], 0, "strass")).toBe(1);
    expect(F.selectAllCombo("macOS")).toBe("Meta+a");
    expect(F.selectAllCombo("windows")).toBe("Control+a");
    expect(F.normalizeClickOptions({ clickCount: "2", delay: "80" })).toMatchObject({ clickCount: 2, delay: 80 });
  });
});

describe("maxlength + pre-press pause (SC 210)", () => {
  it.each(cases("fit_maxlength"))("fitMaxlength #%i", (_, c) => {
    expect(F.fitMaxlength(c.value, c.maxlength, c.tag, c.type)).toBe(c.want);
  });

  it.each(cases("mask_kept"))("maskKept #%i", (_, c) => {
    expect(F.maskKept(c.got, c.want, c.maxlength, c.tag, c.type)).toBe(c.result);
  });

  it.each(cases("pre_press_center"))("prePressCenter #%i", (_, c) => {
    expect(F.prePressCenter(c.seed)).toBeCloseTo(c.center, 6);
  });

  it.each(cases("pre_press_ms"))("prePressMs #%i", (_, c) => {
    const r = mulberry32(c.rng_seed);
    const got = (c.ms as number[]).map(() => F.prePressMs(c.seed, r));
    got.forEach((g, i) => expect(g).toBeCloseTo(c.ms[i], 6));
  });

  it("the pause is 40-250 ms, right-skewed, with its own centre per identity", () => {
    const centres = new Set<number>();
    for (const name of ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"]) {
      const seed = hashSeed(name);
      const r = mulberry32(seed);
      const xs = Array.from({ length: 4000 }, () => F.prePressMs(seed, r)).sort((a, b) => a - b);
      expect(xs[0]).toBeGreaterThanOrEqual(40);
      expect(xs[xs.length - 1]).toBeLessThanOrEqual(250);
      const mean = xs.reduce((a, b) => a + b, 0) / xs.length;
      expect(mean).toBeGreaterThan(xs[xs.length >> 1]);
      expect(xs.filter((x) => x === 40 || x === 250).length).toBeLessThanOrEqual(4);
      const c = F.prePressCenter(seed);
      expect(c).toBeGreaterThanOrEqual(70);
      expect(c).toBeLessThanOrEqual(135);
      centres.add(Math.round(c * 1000));
    }
    expect(centres.size).toBe(6);
  });

  it("fitMaxlength is the longest prefix that fits, in UTF-16 units", () => {
    const r = mulberry32(5);
    const pool = ["a", "b", "1", "\u{1F600}"];
    for (let k = 0; k < 500; k++) {
      const n = Math.floor(r() * 14) - 1;
      const v = Array.from({ length: Math.floor(r() * 11) }, () => pool[Math.floor(r() * pool.length)]).join("");
      const got = F.fitMaxlength(v, n, "input", "text");
      expect(v.startsWith(got)).toBe(true);
      if (n < 0) { expect(got).toBe(v); continue; }
      expect(got.length).toBeLessThanOrEqual(n);
      if (got !== v) expect(got.length + Array.from(v.slice(got.length))[0].length).toBeGreaterThan(n);
    }
  });
});
