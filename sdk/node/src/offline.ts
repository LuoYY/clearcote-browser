// Offline mode: run the PRO engine with an Offline Key file, with no licence calls at all.
//
// A Platform Licence holder downloads an Offline Key (a JSON file from the Licenses page, or
// GET /api/v1/offline/key). Its `token` is a PRO run token the engine verifies locally (signature, validity
// window and a rollback guard). With `offlineKey` (or CLEARCOTE_OFFLINE_KEY) set, a launch:
//   - makes no lease checkout, heartbeat or check-in, and no download: the binary must be given
//     (`executablePath` or CLEARCOTE_BINARY), normally the PRO binary from the offline bundle;
//   - passes the key's token to the engine as CLEARCOTE_RUN_TOKEN.
// The key is checked here first, so an expired or malformed file fails with a clear message instead of an
// engine refusal. The engine stays the authority. Mirrors sdk/python/clearcote/_offline.py.

import { readFileSync } from "node:fs";
import type { LeaseSession } from "./license.js";

export const OFFLINE_KEY_ENV = "CLEARCOTE_OFFLINE_KEY";
export const OFFLINE_KEY_FORMAT = "clearcote-offline-key";
const WARN_DAYS = 7;

export class OfflineKeyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "OfflineKeyError";
  }
}

/** explicit `offlineKey` > CLEARCOTE_OFFLINE_KEY. undefined means online mode. */
export function offlineKeyPath(explicit?: string | null, env: Record<string, string | undefined> = process.env): string | undefined {
  if (explicit && explicit.trim()) return explicit.trim();
  const v = (env[OFFLINE_KEY_ENV] ?? "").trim();
  return v || undefined;
}

export interface OfflineKeyFile {
  format: string;
  token: string;
  expires_at: string;
  [k: string]: unknown;
}

/** Read and check an Offline Key file; throws OfflineKeyError. */
export function loadOfflineKey(path: string, now: Date = new Date()): { file: OfflineKeyFile; expires: Date } {
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    throw new OfflineKeyError(`Offline key file not found: ${path}`);
  }
  let data: OfflineKeyFile;
  try {
    data = JSON.parse(raw);
  } catch (e) {
    throw new OfflineKeyError(`Offline key file can't be read as JSON (${path}): ${(e as Error).message}`);
  }
  if (!data || typeof data !== "object" || data.format !== OFFLINE_KEY_FORMAT)
    throw new OfflineKeyError(`${path} is not a Clearcote offline key file.`);
  if (typeof data.token !== "string" || data.token.split(".").length !== 2)
    throw new OfflineKeyError(`${path} has no valid token.`);
  const expires = new Date(String(data.expires_at));
  if (Number.isNaN(expires.getTime())) throw new OfflineKeyError(`${path} has no valid expires_at.`);
  if (expires.getTime() <= now.getTime())
    throw new OfflineKeyError(
      `This offline key expired on ${expires.toISOString().slice(0, 10)}. Download the newest key from the ` +
        "Offline card on your Licenses page (or GET /api/v1/offline/key) and install it.",
    );
  return { file: data, expires };
}

/** The offline lease for this launch, or null for online mode. Same surface as an online lease, no network. */
export function offlineLease(explicit?: string | null, quiet = false, now: Date = new Date()): LeaseSession | null {
  const path = offlineKeyPath(explicit);
  if (!path) return null;
  const { file, expires } = loadOfflineKey(path, now);
  const left = Math.floor((expires.getTime() - now.getTime()) / 86_400_000);
  if (left < WARN_DAYS && !quiet)
    process.stderr.write(
      `[clearcote] [offline] the offline key expires in ${left} day(s) (${expires.toISOString().slice(0, 10)}); ` +
        "install the newer key from your renewal before then.\n",
    );
  return {
    token: file.token,
    leaseId: "offline",
    async stop() {},
    bindLaunch() {
      return { file: "", release() {} };
    },
  };
}

/** Offline mode never downloads: the binary must come from the offline bundle. */
export function requireOfflineBinary(offlineKey: string | null | undefined, executablePath: string | undefined, env: Record<string, string | undefined> = process.env): void {
  if (!offlineKeyPath(offlineKey, env)) return;
  if (!executablePath && !env.CLEARCOTE_BINARY)
    throw new OfflineKeyError(
      "Offline mode runs the PRO binary from your offline bundle: pass executablePath or set CLEARCOTE_BINARY.",
    );
}
