// Humanize score hook (SC 232 lane G; mirrors sdk/python/clearcote/_trace.py).
//
// CLEARCOTE_HUMANIZE_SCORE=1 makes the SDK record every action it dispatches, score it with
// motionscore.scoreAction and append one JSON line per action to CLEARCOTE_HUMANIZE_SCORE_FILE (default
// ./clearcote-humanize-score.jsonl). A score below WARN_BELOW prints a `clearcote: warning:` line naming
// the penalties (once per kind + penalty set, through warnings.warnOnce).
//
// Read-only tap: nothing here changes what is dispatched or when, and record() never throws.
// Callers: pacing.walk (every walked pointer path, kind glide/drag/ambient, ACTUAL dispatch times); the
// typing loop (lane D) record("typed", [{key, down, up}]); the wheel loop (lane E) record("wheel",
// [{t, dy, dx, mode}]); hover dwell (lane C) record("hover", samplesFromArrival).

import { appendFileSync } from "node:fs";
import { scoreAction, type ActionScore } from "./motionscore.js";
import { warnOnce } from "./warnings.js";

export const SCORE_ENV = "CLEARCOTE_HUMANIZE_SCORE";
export const SCORE_FILE_ENV = "CLEARCOTE_HUMANIZE_SCORE_FILE";
export const DEFAULT_SCORE_FILE = "./clearcote-humanize-score.jsonl";
export const WARN_BELOW = 0.7;

const FALSE = new Set(["", "0", "false", "off", "no"]);

/** True when the hook is on (CLEARCOTE_HUMANIZE_SCORE set to anything but 0/false/off/no). */
export function traceEnabled(): boolean {
  return !FALSE.has((process.env[SCORE_ENV] ?? "").trim().toLowerCase());
}

export function scoreFile(): string {
  return process.env[SCORE_FILE_ENV] || DEFAULT_SCORE_FILE;
}

const r4 = (v: unknown): unknown => {
  if (typeof v === "number") return Number.isFinite(v) ? Math.round(v * 1e4) / 1e4 : v;
  if (Array.isArray(v)) return v.map(r4);
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, r4(x)]));
  return v;
};

/** Score `samples` as `kind` and append the line; returns the score (null when the hook is off, the
 *  sample list is empty, or anything failed). */
export function record(kind: string, samples: readonly unknown[] | null | undefined, meta?: Record<string, unknown>): ActionScore | null {
  if (!traceEnabled() || !samples || !samples.length) return null;
  try {
    const r = scoreAction(kind, samples as any[]);
    const line: Record<string, unknown> = {
      t: Date.now() / 1000, pid: process.pid, kind: r.kind, n: samples.length,
      score: r.score, flags: r.flags, features: r4(r.features),
    };
    if (meta) line.meta = r4(meta);
    appendFileSync(scoreFile(), JSON.stringify(line) + "\n", "utf8");
    if (r.score < WARN_BELOW) {
      const names = r.flags.map((f) => f.split("(")[0]).join(", ") || "none";
      warnOnce(`humanize-score:${r.kind}:${names}`, `humanize ${r.kind} scored ${r.score.toFixed(2)} (< ${WARN_BELOW}): ${names}`);
    }
    return r;
  } catch {
    return null;
  }
}
