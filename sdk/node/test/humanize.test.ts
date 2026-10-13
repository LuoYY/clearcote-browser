import { describe, it, expect } from "vitest";
import { installHumanize, installHumanizeOnContext, attachHumanize, CURSOR_OVERLAY } from "../src/humanize.js";

// a minimal stand-in: newPage/newContext are stable property functions so identity is comparable.
const fakeBrowser = () =>
  ({ newPage: async () => ({}), newContext: async () => ({}) }) as never;

describe("installHumanize", () => {
  it("is a no-op when humanize + showCursor are both off", () => {
    const b = fakeBrowser() as { newPage: unknown; newContext: unknown };
    const np = b.newPage;
    const nc = b.newContext;
    installHumanize(b as never, {});
    expect(b.newPage).toBe(np);
    expect(b.newContext).toBe(nc);
  });

  it("wraps newPage when humanize is on", () => {
    const b = fakeBrowser() as { newPage: unknown };
    const np = b.newPage;
    installHumanize(b as never, { humanize: true });
    expect(b.newPage).not.toBe(np);
  });

  it("wraps newContext when showCursor is on", () => {
    const b = fakeBrowser() as { newContext: unknown };
    const nc = b.newContext;
    installHumanize(b as never, { showCursor: true });
    expect(b.newContext).not.toBe(nc);
  });
});

// launch() is a persistent context, and on playwright-core 1.49.x a persistent context has no Browser:
// context.browser() is null. Humanize used to attach only when it found one, so it was silently off.
describe("installHumanizeOnContext without a Browser", () => {
  const fakeContext = (pages: object[]) => {
    const listeners: Record<string, (p: object) => void> = {};
    const ctx = {
      browser: () => null,
      pages: () => pages,
      on: (ev: string, fn: (p: object) => void) => { listeners[ev] = fn; },
      addInitScript: async () => {},
    };
    return { ctx, listeners };
  };

  it("humanizes the pages already open and every new one", () => {
    const open = {};
    const { ctx, listeners } = fakeContext([open]);
    installHumanizeOnContext(ctx as never, { humanize: true, seed: "no-browser" });
    expect((open as any).__clearcoteHumanized).toBe(true);
    expect(listeners.page).toBeTypeOf("function");
    const later = {};
    listeners.page(later);
    expect((later as any).__clearcoteHumanized).toBe(true);
  });

  it("stays off when humanize and showCursor are off", () => {
    const open = {};
    const { ctx, listeners } = fakeContext([open]);
    installHumanizeOnContext(ctx as never, {});
    expect((open as any).__clearcoteHumanized).toBeUndefined();
    expect(listeners.page).toBeUndefined();
  });
});

describe("CURSOR_OVERLAY", () => {
  it("is an idempotent IIFE (runs as both addInitScript and page.evaluate)", () => {
    expect(CURSOR_OVERLAY).toContain("__clearcoteCursor");
    const body = CURSOR_OVERLAY.trim();
    expect(body.startsWith("(()")).toBe(true);
    expect(body.endsWith(")();")).toBe(true);
  });
});

// A page stub with just the surface attachHumanize rebinds. Everything is an async no-op; we only
// care about FUNCTION IDENTITY before/after a second attach, not behaviour.
const fakePage = () => {
  const noop = async () => undefined;
  // attachHumanize also patches the Locator PROTOTYPE via page.locator("html"), so the stub has to
  // hand back something with a prototype carrying the locator methods.
  const locatorProto = {
    fill: noop, click: noop, type: noop, dblclick: noop, hover: noop, press: noop,
    pressSequentially: noop, clear: noop, tap: noop, check: noop, uncheck: noop,
    dragTo: noop, page: () => null,
  };
  return {
    mouse: { move: noop, click: noop, wheel: noop, down: noop, up: noop },
    keyboard: { type: noop, press: noop, down: noop, up: noop, insertText: noop },
    click: noop, hover: noop, dblclick: noop, fill: noop, press: noop, type: noop,
    focus: noop, evaluate: async () => undefined, on: () => undefined,
    mainFrame: () => ({}), waitForTimeout: noop,
    locator: () => Object.create(locatorProto),
  } as never;
};

describe("attachHumanize idempotency", () => {
  // REGRESSION GUARD — this is the test that would have caught the 18x blow-up.
  //
  // installHumanize wraps BOTH browser.newPage and browser.newContext, and Playwright's newPage()
  // creates a context internally — so the context "page" listener attached once and the outer
  // newPage wrapper attached again. Nothing failed; it just got 18x slower, which reads as
  // "humanize is expensive" rather than "humanize is broken", so it survived review.
  //
  // Double-wrapping is self-similar: the second `mouse.move.bind(mouse)` captures the FIRST
  // wrapper instead of the real native, so each step of the outer planned path replays an entire
  // inner path. Measured on one ~980px move: 2946 samples / 51338 ms, against 161 / 2833 ms
  // correct (the planner caps a single move near 180 samples).
  //
  // Identity comparison is the right assertion: a second wrap necessarily produces a NEW function.
  it("wrapping a page twice leaves each input method wrapped exactly once", async () => {
    const page = fakePage() as any;
    const browser = {} as never;

    await attachHumanize(browser, page, { humanize: true, seed: "t" });
    const once = {
      move: page.mouse.move, click: page.mouse.click, wheel: page.mouse.wheel,
      kbType: page.keyboard.type, pageClick: page.click, pageType: page.type,
    };

    await attachHumanize(browser, page, { humanize: true, seed: "t" });

    expect(page.mouse.move).toBe(once.move);
    expect(page.mouse.click).toBe(once.click);
    expect(page.mouse.wheel).toBe(once.wheel);
    expect(page.keyboard.type).toBe(once.kbType);
    expect(page.click).toBe(once.pageClick);
    expect(page.type).toBe(once.pageType);
  });

  it("does wrap on the first attach (the guard must not disable humanize entirely)", async () => {
    const page = fakePage() as any;
    const nativeMove = page.mouse.move;
    await attachHumanize({} as never, page, { humanize: true, seed: "t" });
    expect(page.mouse.move).not.toBe(nativeMove);
  });

  it("marks the page so a later attach can detect it", async () => {
    const page = fakePage() as any;
    await attachHumanize({} as never, page, { humanize: true, seed: "t" });
    expect(page.__clearcoteHumanized).toBe(true);
  });
});
