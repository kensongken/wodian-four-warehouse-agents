import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const root = resolve(import.meta.dirname, "../../..");
const runbookPath = resolve(root, "docs/acceptance/pr-release-gates-runbook.md");

describe("PR release gate runbook", () => {
  it("stops the copyable Mac gate before printing a candidate after any failed command", async () => {
    const value = await readFile(runbookPath, "utf8");
    const block = /## Mac 固定 SHA 门\s+```bash\n([\s\S]*?)\n```/u.exec(value)?.[1];
    expect(block).toBeDefined();
    expect(block?.split("\n")[0]).toBe("set -euo pipefail");
    expect(block?.indexOf("set -euo pipefail")).toBeLessThan(block?.indexOf("candidate_sha=") ?? -1);
    expect(block?.indexOf("set -euo pipefail")).toBeLessThan(block?.indexOf("printf '%s\\n'") ?? -1);
  });
});
