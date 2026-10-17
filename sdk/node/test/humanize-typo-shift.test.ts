import { describe, expect, it, vi } from "vitest";

// SC 239 (lab contenteditable, SC 209 row "Capitals typed with Shift held 12/14"): a keydown 'M' right after
// Control+Backspace had no Shift keydown before it. restore (the word put back after the word-delete fix) and the
// late-notice keys of doTypo emitted every key bare, while the type loop holds a real ShiftLeft around a capital
// (humanize-shift.test.ts). The planner is pinned to one substitution and the word-fix draw to 1, so the path is
// taken every time; the fake deletes the word under Control+Backspace as the browser does.
const plan = vi.hoisted(() => ({ at: 1, late: 0 }));
vi.mock("../src/keys.js", async (importOriginal) => ({ ...(await importOriginal<typeof import("../src/keys.js")>()), WORD_FIX_RATE: 1 }));
vi.mock("../src/forms.js", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/forms.js")>()),
  planTypo: (_text: unknown, i: number) => (i === plan.at ? { kind: "sub", wrong: "t", late: plan.late } : null),
}));
import { SLOW, attached, type Fake } from "./helpers/humanize-fake.js";

const ENV = "CLEARCOTE_HUMANIZE_TYPO_RATE";
type K = [string, string];
const keysOf = (f: Fake): K[] =>
  f.events.filter((e) => ["kdown", "kup", "press"].includes(e.t)).map((e) => [e.t, e.t === "press" ? e.a.k : e.a] as K);
// Shift held across the press at keys[i]: its last Shift event before is a keydown, and a Shift keyup follows it
// before the next press.
const shiftHeldAround = (keys: K[], i: number): boolean => {
  const before = keys.slice(0, i).filter(([, k]) => k === "Shift");
  const after = keys.slice(i + 1);
  const nxt = after.findIndex(([t]) => t === "press");
  return before.length > 0 && before[before.length - 1][0] === "kdown"
    && after.slice(0, nxt < 0 ? after.length : nxt).some(([t, k]) => t === "kup" && k === "Shift");
};
const pressesOf = (keys: K[], key: string, from: number, to: number): number[] =>
  keys.map(([t, k], i) => (i > from && i < to && t === "press" && k === key ? i : -1)).filter((i) => i >= 0);

describe("capitals on the typo word-fix paths", () => {
  it("a capital put back after the Ctrl+Backspace word fix goes behind a real ShiftLeft", async () => {
    process.env[ENV] = "1";
    plan.at = 1; plan.late = 0;
    const f = await attached();
    f.setFocused(true);
    await f.page.keyboard.type("Mr");
    expect(f.field.value).toBe("Mr");
    const keys = keysOf(f);
    const wd = keys.findIndex(([t, k]) => t === "kdown" && k === "Control");
    expect(wd, "the word fix").toBeGreaterThan(0);
    const back = pressesOf(keys, "M", wd, keys.length);
    expect(back.length, "the word fix put the M back").toBe(1);
    expect(shiftHeldAround(keys, back[0]), JSON.stringify(keys.slice(wd))).toBe(true);
  }, SLOW);

  it("a late-notice capital goes behind a real ShiftLeft", async () => {
    // planTypo keeps late notice over plain keys today; the helper's contract holds when a shifted one is asked for
    process.env[ENV] = "1";
    plan.at = 0; plan.late = 1;
    const f = await attached();
    f.setFocused(true);
    await f.page.keyboard.type("aB");
    expect(f.field.value).toBe("aB");
    const keys = keysOf(f);
    const bs = keys.findIndex(([t, k]) => t === "press" && k === "Backspace");
    const late = pressesOf(keys, "B", -1, bs);
    expect(late.length, "the slip was noticed one key late, after the B").toBe(1);
    expect(shiftHeldAround(keys, late[0]), JSON.stringify(keys.slice(0, bs + 1))).toBe(true);
  }, SLOW);
});
