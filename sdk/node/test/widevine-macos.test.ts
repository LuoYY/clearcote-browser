// Widevine on a Mac: the CDM a Mac asks Google for, and that it lands where a Mac browser loads it.
//
// Before this, cdmPlatform knew Linux and sent everything else down the Windows branch, so the native
// macOS PRO build (an Apple Silicon Chromium.app) was handed widevinecdm.dll under win_x64 — a library
// no Mac can load, seeded into every profile. The coordinates here are what Google's server really
// answers (2026-10-17: an arm64 Mac gets `oimompecagnajdejgnnjijobebaeigek_4.10.3112.0_mac_arm64_….crx3`,
// whose zip holds _platform_specific/mac_arm64/libwidevinecdm.dylib). There is no Mac in CI, so the
// host is faked (process.platform + hostArch.get, as macos-native.test.ts does) and Google is a stub
// that records every request.
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as W from "../src/widevine.js";
import { hostArch } from "../src/release.js";
import { storedZip } from "./helpers/fake-build.js";

const VERSION = "4.10.3112.0";
const DOWNLOAD_URL = "https://dl.example.test/cdm.crx3";

let root: string;
let platformDesc: PropertyDescriptor;
let realFetch: typeof fetch;
const ENV_KEYS = ["CLEARCOTE_WIDEVINE_DIR", "HOME", "USERPROFILE"];
const savedEnv: Record<string, string | undefined> = {};

function on(platform: string, arch = "arm64"): void {
  Object.defineProperty(process, "platform", { ...platformDesc, value: platform });
  vi.spyOn(hostArch, "get").mockReturnValue(arch);
}

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "cc-wv-mac-"));
  platformDesc = Object.getOwnPropertyDescriptor(process, "platform")!;
  for (const k of ENV_KEYS) savedEnv[k] = process.env[k];
  process.env.CLEARCOTE_WIDEVINE_DIR = join(root, "wv");
  process.env.HOME = process.env.USERPROFILE = join(root, "home");
  realFetch = globalThis.fetch;
  globalThis.fetch = (async (url: unknown) => { throw new Error(`unexpected fetch ${String(url)}`); }) as typeof fetch;
  W.resetWidevineDefaultState();
});

afterEach(() => {
  Object.defineProperty(process, "platform", platformDesc);
  vi.restoreAllMocks();
  globalThis.fetch = realFetch;
  W.resetWidevineDefaultState();
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
  rmSync(root, { recursive: true, force: true, maxRetries: 5 });
});

function crx(files: Record<string, Buffer>): Buffer {
  const header = Buffer.from([1, 2, 3, 4]);
  const fixed = Buffer.alloc(12);
  fixed.write("Cr24", 0, "latin1");
  fixed.writeUInt32LE(3, 4);
  fixed.writeUInt32LE(header.length, 8);
  return Buffer.concat([fixed, header, storedZip(files)]);
}

/** The macOS package's real layout (library + its .sig + manifest). */
const macCrx = () => crx({
  "manifest.json": Buffer.from(`{"version":"${VERSION}"}`),
  "_platform_specific/mac_arm64/libwidevinecdm.dylib": Buffer.from("\xcf\xfa\xed\xfe fake mach-o", "latin1"),
  "_platform_specific/mac_arm64/libwidevinecdm.dylib.sig": Buffer.from("sig"),
});

/** Google's update server + download host, recording the update check's body and every UA it is sent. */
function fakeGoogle(blob: Buffer) {
  const sha = createHash("sha256").update(blob).digest("hex");
  const seen = { bodies: [] as any[], agents: [] as string[] };
  globalThis.fetch = (async (url: unknown, init?: { body?: string; headers?: Record<string, string> }) => {
    const u = String(url);
    seen.agents.push(init?.headers?.["User-Agent"] ?? "");
    if (u === W.OMAHA_URL) {
      seen.bodies.push(JSON.parse(String(init?.body)));
      // The classic shape Google really returns (codebase + package), as observed on 2026-10-17.
      return new Response(")]}'\n" + JSON.stringify({ response: { app: [{ updatecheck: { status: "ok",
        urls: { url: [{ codebase: "https://dl.example.test/" }] },
        manifest: { version: VERSION, packages: { package: [{ name: "cdm.crx3", hash_sha256: sha }] } } } }] } }), { status: 200 });
    }
    if (u === DOWNLOAD_URL) return new Response(blob, { status: 200 });
    throw new Error(`unexpected fetch ${u}`);
  }) as typeof fetch;
  return seen;
}

describe("cdmPlatform", () => {
  it("an Apple Silicon Mac: libwidevinecdm.dylib under mac_arm64, asked for as arm64", () => {
    expect(W.cdmPlatform("darwin", "arm64")).toEqual({
      atOs: "mac", osPlatform: "Mac OS X", osVersion: "14.0.0", subdir: "mac_arm64", filename: "libwidevinecdm.dylib",
      arch: "arm64", naclArch: "arm", osArch: "arm64",
    });
  });
  it("an Intel Mac: the same library under mac_x64", () => {
    expect(W.cdmPlatform("darwin", "x64")).toMatchObject({ subdir: "mac_x64", filename: "libwidevinecdm.dylib", arch: "x64", osArch: "x86_64" });
  });
  it("Windows and Linux are what they were, whatever the CPU", () => {
    for (const arch of ["x64", "arm64"]) {
      expect(W.cdmPlatform("win32", arch)).toEqual({
        atOs: "win", osPlatform: "Windows", osVersion: "10.0.19045.0", subdir: "win_x64", filename: "widevinecdm.dll",
        arch: "x64", naclArch: "x86-64", osArch: "x86_64",
      });
      expect(W.cdmPlatform("linux", arch)).toEqual({
        atOs: "Linux", osPlatform: "Linux", osVersion: "6.1.0", subdir: "linux_x64", filename: "libwidevinecdm.so",
        arch: "x64", naclArch: "x86-64", osArch: "x86_64",
      });
    }
  });
  it("defaults to this host", () => {
    on("darwin", "arm64");
    expect(W.cdmPlatform().subdir).toBe("mac_arm64");
  });
});

describe("omahaRequestBody", () => {
  /** The body the SDK sent before macOS existed, for Windows / Linux. */
  const before = (atOs: string, platform: string, version: string) => ({
    request: {
      "@os": atOs, "@updater": "clearcote", acceptformat: "crx3", protocol: "3.1",
      arch: "x64", nacl_arch: "x86-64", prodversion: "149.0.0.0", updaterversion: "149.0.0.0", dedup: "cr",
      os: { arch: "x86_64", platform, version },
      app: [{ appid: W.WIDEVINE_APP_ID, version: "0.0.0.0", updatecheck: {}, ping: { r: -2 } }],
    },
  });

  it("on an Apple Silicon Mac asks as Google's own Mac client does", () => {
    on("darwin", "arm64");
    const r = (W.omahaRequestBody() as any).request;
    expect(r["@os"]).toBe("mac");
    expect([r.arch, r.nacl_arch]).toEqual(["arm64", "arm"]);
    expect(r.os).toEqual({ arch: "arm64", platform: "Mac OS X", version: "14.0.0" });
  });

  it("Windows and Linux bodies are byte-for-byte the old ones", () => {
    on("win32", "arm64");
    expect(JSON.stringify(W.omahaRequestBody())).toBe(JSON.stringify(before("win", "Windows", "10.0.19045.0")));
    on("linux", "x64");
    expect(JSON.stringify(W.omahaRequestBody())).toBe(JSON.stringify(before("Linux", "Linux", "6.1.0")));
  });
});

describe("omahaUserAgent", () => {
  it("names the OS the CDM is asked for", () => {
    expect(W.omahaUserAgent("darwin")).toMatch(/^Mozilla\/5\.0 \(Macintosh; Intel Mac OS X 10_15_7\)/);
    expect(W.omahaUserAgent("win32")).toMatch(/\(Windows NT 10\.0; Win64; x64\)/);
    expect(W.omahaUserAgent("linux")).toMatch(/\(X11; Linux x86_64\)/);
  });
});

describe("on a Mac, end to end against a stub of Google", () => {
  it("fetchWidevine asks for the Mac CDM, with a Mac UA on every request, and installs the dylib", async () => {
    on("darwin", "arm64");
    const seen = fakeGoogle(macCrx());
    const dir = await W.fetchWidevine({ quiet: true });
    expect(seen.bodies).toHaveLength(1);
    expect(seen.bodies[0].request).toMatchObject({ "@os": "mac", arch: "arm64", os: { platform: "Mac OS X", arch: "arm64" } });
    expect(seen.agents).toHaveLength(2); // the check and the download
    for (const ua of seen.agents) expect(ua).toMatch(/Macintosh/);
    expect(existsSync(join(dir, "_platform_specific", "mac_arm64", "libwidevinecdm.dylib"))).toBe(true);
    expect(existsSync(join(dir, "manifest.json"))).toBe(true);
  });

  it("a package without the Mac library is refused (the old Windows CDM would be)", async () => {
    on("darwin", "arm64");
    fakeGoogle(crx({ "manifest.json": Buffer.from("{}"), "_platform_specific/win_x64/widevinecdm.dll": Buffer.from("MZ") }));
    await expect(W.fetchWidevine({ quiet: true })).rejects.toThrow(/libwidevinecdm\.dylib/);
  });

  it("seedWidevine copies the Mac CDM into the profile", async () => {
    on("darwin", "arm64");
    fakeGoogle(macCrx());
    const udd = join(root, "profile");
    const target = await W.seedWidevine(udd, { quiet: true });
    expect(target).toBe(join(udd, "WidevineCdm", VERSION));
    expect(readFileSync(join(target, "_platform_specific", "mac_arm64", "libwidevinecdm.dylib"), "latin1")).toContain("fake mach-o");
  });

  it("the startup scan is forced on a Mac (as on Windows), not on Linux", () => {
    on("darwin");
    expect(W.widevineArgs(undefined, []).args).toEqual(["--component-updater=fast-update"]);
    on("win32");
    expect(W.widevineArgs(undefined, []).args).toEqual(["--component-updater=fast-update"]);
    on("linux");
    expect(W.widevineArgs(undefined, []).args).toEqual([]);
  });
});
