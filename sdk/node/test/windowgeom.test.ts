/**
 * Persona window geometry (engine r39+): the engine opens each persona's own browser window.
 *
 * Until r38 every seed opened the same window while its screen varied (780x580 headless whenever the
 * SDK's fit did not run; the SDK's maximize otherwise). From r39 the engine derives the window from the
 * persona and applies it to the real window; ./src/windowgeom.ts is the bit-exact port. The vector file
 * (test/data/window_vectors.txt) is the engine's own output, from the standalone C++ test; the Python
 * and .NET suites assert the same file.
 */
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  applyHeadlessGeometry,
  enginePlacesPersonaWindow,
  fitWindowOverCdp,
  headedLinuxFit,
  placeWindow,
  reconcilePlan,
  servedGeometry,
} from "../src/geometry.js";
import { deriveWindow, fnv1a64, mixedSeed, windowKey } from "../src/windowgeom.js";

const VECTORS = path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")),
  "data", "window_vectors.txt");

function vectors(): string[][] {
  return readFileSync(VECTORS, "utf8").split(/\r?\n/).filter((l) => l.trim()).map((l) => l.split(/\s+/));
}

describe("windowgeom port", () => {
  it("is the full engine table", () => {
    expect(vectors()).toHaveLength(1536);
  });

  it("matches the engine bit for bit (rectangles and saved-placement keys)", () => {
    const bad: unknown[] = [];
    for (const [raw, sw, sh, aw, ah, plat, valid, kind, state, x, y, w, h, key] of vectors()) {
      const seed = mixedSeed(raw);
      const g = deriveWindow(seed, +sw, +sh, +aw, +ah, +plat);
      const got = [g.valid, g.kind ?? "-", g.state ?? "normal", g.x, g.y, g.width, g.height].join(" ");
      const want = [valid === "1", kind, state, +x, +y, +w, +h].join(" ");
      const k = windowKey(seed, +sw, +sh, +aw, +ah, +plat).toString(16).padStart(16, "0");
      if (got !== want || k !== key) bad.push([raw, sw, sh, aw, ah, plat, got, want]);
    }
    expect(bad.slice(0, 5)).toEqual([]);
  });

  it("parses seeds the way the engine does", () => {
    expect(fnv1a64("a")).toBe(0xaf63dc4c8601ec8cn);
    expect(fnv1a64("")).toBe(0xcbf29ce484222325n);
    expect(mixedSeed("0")).toBe(0xe220a8397b1dcdafn);
    expect(mixedSeed("geo-1")).toBe(mixedSeed("geo-1"));
  });

  it.each([
    [1366, 768, 1366, 728], [1920, 1080, 1920, 1032], [1920, 1080, 1920, 1080],
    [2560, 1440, 2560, 1392], [3440, 1440, 3440, 1392], [3840, 2160, 3840, 2112],
  ])("distribution over 2000 seeds on %ix%i/%ix%i", (sw, sh, aw, ah) => {
    for (const platform of ["windows", "linux"]) {
      const sizes = new Map<string, number>();
      let maximized = 0;
      for (let i = 1; i <= 2000; i++) {
        const g = deriveWindow(mixedSeed(String(i)), sw, sh, aw, ah, platform);
        expect(g.valid).toBe(true);
        expect(g.x >= 0 && g.y >= 0 && g.x + g.width <= aw && g.y + g.height <= ah).toBe(true);
        if (platform !== "windows") expect(g.kind?.startsWith("snap")).toBe(false);
        if (g.state === "maximized") maximized++;
        else if (!g.kind?.startsWith("snap")) {
          const k = `${g.width}x${g.height}`;
          sizes.set(k, (sizes.get(k) ?? 0) + 1);
        }
      }
      expect(maximized / 2000).toBeGreaterThanOrEqual(0.4);
      expect(maximized / 2000).toBeLessThanOrEqual(0.75);
      expect(Math.max(...sizes.values())).toBeLessThanOrEqual(20);
      expect(sizes.has(`${Math.floor(aw / 2) - 15}x${ah - 20}`)).toBe(false); // Chrome's fresh-profile box
    }
  });

  it("gives no window to a work area too small for a desktop", () => {
    expect(deriveWindow(1n, 800, 600, 800, 500).valid).toBe(false);
    expect(deriveWindow(1n, 1366, 768, 1400, 728).valid).toBe(false);
  });
});

function engine(withSwitch = true): string {
  const dir = mkdtempSync(path.join(os.tmpdir(), "cc-r39-"));
  const body = Buffer.from(withSwitch ? "\0junk\0fingerprint-window\0" : "\0junk\0fingerprint\0", "latin1");
  const exe = path.join(dir, "fake-chrome");
  writeFileSync(exe, body);
  if (process.platform === "win32") writeFileSync(path.join(dir, "chrome.dll"), body);
  return exe;
}

describe("r39 persona window plan", () => {
  const r39 = engine(true);
  const r38 = engine(false);

  it("needs a persona, the switch literal and the engine's own rule", () => {
    expect(enginePlacesPersonaWindow(r39, ["--fingerprint=7"])).toBe(true);
    expect(enginePlacesPersonaWindow(r39, ["--persona-from-env"])).toBe(true);
    expect(enginePlacesPersonaWindow(r38, ["--fingerprint=7"])).toBe(false);
    expect(enginePlacesPersonaWindow(r39, [])).toBe(false);
    expect(enginePlacesPersonaWindow(undefined, ["--fingerprint=7"])).toBe(false);
    expect(enginePlacesPersonaWindow(r39, ["--fingerprint=7", "--fingerprint-window=off"])).toBe(false);
    expect(enginePlacesPersonaWindow(r39, ["--fingerprint=7", "--fingerprint-platform=android"])).toBe(false);
    expect(enginePlacesPersonaWindow(r39, ["--fingerprint=7", "--fingerprint-schema=3"])).toBe(false);
    expect(enginePlacesPersonaWindow(r39, ["--fingerprint=7", "--fingerprint-schema=3", "--fingerprint-window=on"])).toBe(true);
  });

  it("sends no display switch and plans no fit on r39", () => {
    const opts: Record<string, unknown> = {};
    const g = applyHeadlessGeometry(opts, "7", ["--fingerprint=7"], {}, r39);
    expect(g).toEqual({ mode: "persona-native", args: [], fit: false, reconcile: false });
    expect(opts.viewport).toBeNull();
  });

  it.each([
    [{ viewport: null }, false],
    [{ viewport: { width: 1280, height: 720 } }, true],
    [{ screen: { width: 1280, height: 720 } }, false],
  ])("keeps explicit choice %j (reconcile %s)", (explicit, reconcile) => {
    const opts: Record<string, unknown> = { ...explicit };
    const g = applyHeadlessGeometry(opts, "7", ["--fingerprint=7"], {}, r39);
    expect(g).toEqual({ mode: "persona-native", args: [], fit: false, reconcile });
    expect(opts).toEqual(explicit);
  });

  it("keeps the old plan on older engines", () => {
    const g = applyHeadlessGeometry({}, "7", ["--fingerprint=7"], {}, r38);
    expect(g?.mode).toBe("persona");
    expect(g?.args.some((a) => a.startsWith("--screen-info="))).toBe(true);
  });

  it("skips the headed Linux fit for an r39 persona", () => {
    expect(headedLinuxFit(["--fingerprint=7"], "linux", r39)).toBe(false);
    expect(headedLinuxFit([], "linux", r39)).toBe(true);
  });

  it.each([
    [{ left: 0, top: 0, width: 1296, height: 854, windowState: "normal" }, [1920, 1032], null],
    [{ left: 900, top: 300, width: 1296, height: 854, windowState: "normal" }, [1920, 1032], [624, 178]],
    [{ left: 40, top: 20, width: 2000, height: 900, windowState: "normal" }, [1920, 1032], [0, 20]],
    [{ left: 0, top: 0, width: 1920, height: 1032, windowState: "maximized" }, [1920, 1032], null],
  ])("reconcile moves %j back onto %j", (b, avail, want) => {
    expect(reconcilePlan(b, avail)).toEqual(want);
  });

  it("does nothing for a native persona page without a viewport, and only moves it with one", async () => {
    const calls: [string, unknown][] = [];
    const page = {
      evaluate: async () => [1920, 1032],
      context: () => ({
        newCDPSession: async () => ({
          send: async (m: string, p?: unknown) => {
            calls.push([m, p]);
            return m === "Browser.getWindowForTarget"
              ? { windowId: 3, bounds: { left: 900, top: 300, width: 1296, height: 854, windowState: "normal" } }
              : {};
          },
        }),
      }),
    };
    await placeWindow(page as never, [], { mode: "persona-native", args: [], fit: false, reconcile: false });
    expect(calls).toEqual([]);
    await placeWindow(page as never, [], { mode: "persona-native", args: [], fit: false, reconcile: true });
    expect(calls.filter(([m]) => m === "Browser.setWindowBounds"))
      .toEqual([["Browser.setWindowBounds", { windowId: 3, bounds: { left: 624, top: 178 } }]]);
  });

  it("serves a native persona with nothing but an explicit window size", async () => {
    expect(servedGeometry(["--fingerprint=7"], { fingerprint: "7" }, true, r39))
      .toEqual({ persona: true, native: true, display: null, args: [], fit: false });
    expect(servedGeometry(["--fingerprint=7"], { fingerprint: "7" }, true, r39, { width: 1200, height: 800 })?.fit).toBe(true);

    const bounds = { left: 900, top: 30, width: 1400, height: 900, windowState: "maximized" as string };
    const calls: [string, Record<string, unknown> | undefined][] = [];
    const cdp = {
      send: async (m: string, p?: Record<string, unknown>) => {
        calls.push([m, p]);
        if (m === "Target.getTargets") return { targetInfos: [{ targetId: "T", type: "page" }] };
        if (m === "Target.attachToTarget") return { sessionId: "S" };
        if (m === "Runtime.evaluate") {
          return String(p?.expression).includes("availLeft")
            ? { result: { value: [1920, 1080, 0, 0, 1920, 1032] } }
            : { result: { value: [bounds.width, bounds.height] } };
        }
        if (m === "Browser.getWindowForTarget") return { windowId: 3, bounds: { ...bounds } };
        if (m === "Browser.setWindowBounds") {
          const b = p?.bounds as Record<string, number | string>;
          if ("width" in b) Object.assign(bounds, { width: b.width, height: b.height, left: b.left, top: b.top });
          bounds.windowState = String(b.windowState ?? "normal");
        }
        return {};
      },
    };
    const out = await fitWindowOverCdp(cdp, { persona: true, native: true, windowSize: { width: 1200, height: 800 }, settleMs: 50 });
    expect(out?.outer).toEqual([1200, 800]);
    expect(calls.some(([m]) => m === "Emulation.updateScreen")).toBe(false);
    const sets = calls.filter(([m]) => m === "Browser.setWindowBounds").map(([, p]) => p?.bounds);
    expect(sets[0]).toEqual({ windowState: "normal" });
    expect(sets[sets.length - 1]).toEqual({ left: 720, top: 30, width: 1200, height: 800 });
  });
});
