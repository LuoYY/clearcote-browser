/**
 * The "node SDK tests (oldest playwright-core)" CI job sets CLEARCOTE_PW_FLOOR=1 after installing the
 * lowest playwright-core that package.json allows. This proves the run really used it, so a resolution
 * that quietly kept the lockfile's version cannot turn that job into a copy of the normal one.
 */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);

describe.runIf(process.env.CLEARCOTE_PW_FLOOR)("playwright-core floor", () => {
  it("the installed playwright-core is the lowest version package.json allows", () => {
    const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
    const floor = String(pkg.dependencies["playwright-core"]).replace(/^[\^~>=\s]+/, "");
    const installed = JSON.parse(readFileSync(join(dirname(require.resolve("playwright-core")), "package.json"), "utf8")).version;
    expect(installed).toBe(floor);
  });
});
