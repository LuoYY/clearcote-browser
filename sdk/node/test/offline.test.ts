// Offline mode: an Offline Key file replaces every licence call (no checkout, heartbeat, check-in or
// download); its token goes to the engine as CLEARCOTE_RUN_TOKEN and the binary must be given.
import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { OfflineKeyError, loadOfflineKey, offlineKeyPath, offlineLease, requireOfflineBinary } from "../src/offline.js";
import { acquireLease, withRunToken } from "../src/license.js";

const NOW = new Date("2026-10-16T12:00:00Z");
// Every temp dir a test makes is removed after it: CI fails a run that leaves anything in the temp directory.
const made: string[] = [];
const tempDir = () => {
  const d = mkdtempSync(join(tmpdir(), "cc-offline-"));
  made.push(d);
  return d;
};
const day = 86_400_000;

function keyFile(over: Record<string, unknown> = {}): string {
  const dir = tempDir();
  const p = join(dir, "key.json");
  writeFileSync(
    p,
    JSON.stringify({
      format: "clearcote-offline-key",
      v: 1,
      licence_id: "lic_1",
      expires_at: new Date(NOW.getTime() + 40 * day).toISOString(),
      token: "eyJwYXlsb2FkIjoxfQ.c2ln",
      ...over,
    }),
  );
  return p;
}

afterEach(() => {
  for (const d of made.splice(0)) rmSync(d, { recursive: true, force: true });
  delete process.env.CLEARCOTE_OFFLINE_KEY;
  vi.restoreAllMocks();
});

describe("offline key", () => {
  it("no key means online mode", () => {
    expect(offlineLease(undefined)).toBeNull();
    expect(offlineKeyPath(undefined, {})).toBeUndefined();
  });

  it("a valid key gives its token and an inert lease", async () => {
    const lease = offlineLease(keyFile(), false, NOW)!;
    expect(lease.token).toBe("eyJwYXlsb2FkIjoxfQ.c2ln");
    expect(lease.bindLaunch().file).toBe("");
    expect(lease.refreshToken).toBeUndefined();
    await lease.stop();
  });

  it.each([
    [{ expires_at: new Date(NOW.getTime() - day).toISOString() }, /expired/],
    [{ format: "something-else" }, /not a Clearcote offline key/],
    [{ token: "no-dot" }, /no valid token/],
    [{ expires_at: "soon" }, /no valid expires_at/],
  ])("bad keys fail clearly (%o)", (over, msg) => {
    expect(() => offlineLease(keyFile(over), false, NOW)).toThrow(msg);
  });

  it("missing and unreadable files", () => {
    expect(() => loadOfflineKey(join(tmpdir(), "nope-cc-offline.json"), NOW)).toThrow(OfflineKeyError);
    const dir = tempDir();
    writeFileSync(join(dir, "bad.json"), "{not json");
    expect(() => loadOfflineKey(join(dir, "bad.json"), NOW)).toThrow(/JSON/);
  });

  it("warns when close to expiry, unless quiet", () => {
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    offlineLease(keyFile({ expires_at: new Date(NOW.getTime() + 3 * day).toISOString() }), false, NOW);
    expect(String(err.mock.calls[0]?.[0])).toContain("expires in 3 day(s)");
    err.mockClear();
    offlineLease(keyFile({ expires_at: new Date(NOW.getTime() + 3 * day).toISOString() }), true, NOW);
    expect(err).not.toHaveBeenCalled();
  });

  it("offline mode needs a binary; the env var key counts too", () => {
    expect(() => requireOfflineBinary("/k.json", undefined, {})).toThrow(/executablePath/);
    expect(() => requireOfflineBinary(undefined, undefined, { CLEARCOTE_OFFLINE_KEY: "/k.json" })).toThrow(OfflineKeyError);
    expect(() => requireOfflineBinary("/k.json", "/opt/clearcote/chrome", {})).not.toThrow();
    expect(() => requireOfflineBinary("/k.json", undefined, { CLEARCOTE_BINARY: "/opt/clearcote/chrome" })).not.toThrow();
    expect(() => requireOfflineBinary(undefined, undefined, {})).not.toThrow(); // online mode: nothing to check
  });

  it("acquireLease with an offline key makes no network call, even with a licence key set", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("offline mode must not call the server"));
    process.env.CLEARCOTE_OFFLINE_KEY = keyFile({ expires_at: new Date(Date.now() + 40 * day).toISOString() });
    const lease = await acquireLease({ licenseKey: "cc_lic_" + "x".repeat(40) });
    expect(lease?.token).toBe("eyJwYXlsb2FkIjoxfQ.c2ln");
    expect(fetchSpy).not.toHaveBeenCalled();
    const env = withRunToken(lease!.token, {}, lease!.bindLaunch().file);
    expect(env.CLEARCOTE_RUN_TOKEN).toBe("eyJwYXlsb2FkIjoxfQ.c2ln");
    expect(env.CLEARCOTE_RUN_TOKEN_FILE).toBeUndefined();
  });
});
