/**
 * Live: Widevine ON by default for a licensed (PRO) launch, against a real engine.
 *
 * Skipped unless CLEARCOTE_LIVE_ENGINE points at a PRO chrome binary (r32+, it has --widevine-cdm-path) AND a
 * licence key resolves (CLEARCOTE_LICENSE_KEY or ~/.clearcote/license.key). Downloads Google's CDM from Google
 * into a temp cache once, so it needs the network. Headless. These belong in the release gate. Mirrors
 * sdk/python/tests/test_widevine_live.py.
 */
import { createServer, type Server } from "node:http";
import { existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Page } from "playwright-core";
import { launch, launchPersistentContext, resolveLicenseKey } from "../src/index.js";
import * as W from "../src/widevine.js";

const LIVE_EXE = process.env.CLEARCOTE_LIVE_ENGINE;
const RUN = !!LIVE_EXE && !!resolveLicenseKey();

const EME = `(async () => { try {
  const a = await navigator.requestMediaKeySystemAccess('com.widevine.alpha', [{initDataTypes: ['cenc'],
    videoCapabilities: [{contentType: 'video/mp4; codecs="avc1.42E01E"'}]}]);
  await a.createMediaKeys(); return 'ok ' + a.keySystem;
} catch (e) { return e.name; } })()`;

describe.skipIf(!RUN)("Widevine by default on a licensed launch (live)", () => {
  let server: Server;
  let url = "";
  let root = "";
  let cache = "";
  const saved = { dir: process.env.CLEARCOTE_WIDEVINE_DIR, wv: process.env.CLEARCOTE_WIDEVINE, t: process.env.CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS };

  beforeAll(async () => {
    // EME needs a secure context: http://127.0.0.1 is one
    server = createServer((_req, res) => { res.setHeader("content-type", "text/html"); res.end("<!doctype html><title>eme</title><p>eme"); });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    const addr = server.address();
    url = `http://127.0.0.1:${typeof addr === "object" && addr ? addr.port : 0}/`;
    root = mkdtempSync(join(tmpdir(), "cc-wv-live-"));
    cache = join(root, "cache"); // EMPTY: the first default launch must download, verify and install it
    process.env.CLEARCOTE_WIDEVINE_DIR = cache;
    delete process.env.CLEARCOTE_WIDEVINE;
    process.env.CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS = "120"; // a first download on a slow link
    W.resetWidevineDefaultState();
  });

  afterAll(async () => {
    await new Promise<void>((r) => server.close(() => r()));
    rmSync(root, { recursive: true, force: true, maxRetries: 5 });
    for (const [k, v] of [["CLEARCOTE_WIDEVINE_DIR", saved.dir], ["CLEARCOTE_WIDEVINE", saved.wv], ["CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", saved.t]] as const) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  });

  const eme = async (page: Page) => {
    await page.goto(url);
    return page.evaluate(EME);
  };

  it("a default licensed launch downloads the CDM and has Widevine", async () => {
    expect(W.cachedCdm(cache)).toBeUndefined();
    const udd = join(root, "udd");
    const ctx = await launchPersistentContext(udd, { executablePath: LIVE_EXE, headless: true, quiet: true });
    try {
      expect(await eme(await ctx.newPage())).toBe("ok com.widevine.alpha");
    } finally {
      await ctx.close();
    }
    expect(W.cachedCdm(cache)).toBeDefined();
    expect(readdirSync(cache).filter((n) => /^\.(tmp-|trash-|install-lock)/.test(n))).toEqual([]);
    expect(existsSync(join(udd, "WidevineCdm"))).toBe(false); // the default never seeds the profile
    rmSync(udd, { recursive: true, force: true, maxRetries: 5 });
  }, 240_000);

  it("a second launch (throwaway profile) and an incognito one use the cache", async () => {
    for (const extra of [{}, { ephemeralProfile: false }]) {
      const browser = await launch({ executablePath: LIVE_EXE, headless: true, quiet: true, ...extra });
      try {
        expect(await eme(await browser.newPage())).toBe("ok com.widevine.alpha");
      } finally {
        await browser.close();
      }
    }
  }, 120_000);

  it.each(["option", "env"])("opting out (%s) leaves no Widevine", async (how) => {
    if (how === "env") process.env.CLEARCOTE_WIDEVINE = "0";
    const udd = join(root, `udd-${how}`);
    try {
      const ctx = await launchPersistentContext(udd, { executablePath: LIVE_EXE, headless: true, quiet: true, ...(how === "option" ? { widevine: false } : {}) });
      try {
        expect(await eme(await ctx.newPage())).toBe("NotSupportedError");
      } finally {
        await ctx.close();
      }
    } finally {
      delete process.env.CLEARCOTE_WIDEVINE;
      rmSync(udd, { recursive: true, force: true, maxRetries: 5 });
    }
  }, 120_000);
});
