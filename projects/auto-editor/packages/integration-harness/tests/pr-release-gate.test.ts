import { access } from "node:fs/promises";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  PR_CONTRACT_TEST_FILES,
  PR_FULL_TEST_MINIMUM,
  assertPrFullTestMinimum,
  parsePrGateMode,
  prGateCommands,
  verifyContractTestFiles,
} from "../src/pr-release-gate";

describe("PR release gate policy", () => {
  it("pins the fast gate to the approved reference workflow surfaces", () => {
    expect(PR_CONTRACT_TEST_FILES).toEqual([
      "packages/contracts/tests/reference-guided-editing-contracts.test.ts",
      "packages/mobile-agent/tests/reference-sample-store.test.ts",
      "packages/mobile-agent/tests/reference-approval-store.test.ts",
      "packages/mobile-agent/tests/reference-material-mapper.test.ts",
      "packages/mobile-agent/tests/studio-evidence-merger.test.ts",
      "packages/mobile-agent/tests/reference-guided-editor.test.ts",
      "packages/mobile-agent/tests/reference-prompt-revision.test.ts",
      "packages/mobile-agent/tests/reference-withdrawal-coordinator.test.ts",
      "packages/review-app/tests/studio-reference-api.test.ts",
      "packages/review-app/tests/studio-reference-ui.test.ts",
    ]);
  });

  it("builds a typecheck plus explicit single-worker fast gate", () => {
    expect(prGateCommands("contract")).toEqual([
      { name: "typecheck", argv: ["pnpm", "typecheck"] },
      {
        name: "contract-tests",
        argv: [
          "node_modules/.bin/vitest", "run", ...PR_CONTRACT_TEST_FILES,
          "--maxWorkers=1", "--fileParallelism=false",
          "--testTimeout=180000", "--hookTimeout=60000",
        ],
      },
    ]);
    expect(prGateCommands("full")).toEqual([
      { name: "full-tests", argv: ["pnpm", "test"] },
    ]);
  });

  it("accepts the exact baseline and additive tests", () => {
    expect(PR_FULL_TEST_MINIMUM).toEqual({
      mainVitest: 1321,
      nodeInstaller: 16,
      creativeApi: 208,
      modelChromium: 3,
      total: 1548,
    });
    expect(() => assertPrFullTestMinimum(PR_FULL_TEST_MINIMUM)).not.toThrow();
    expect(() => assertPrFullTestMinimum({
      mainVitest: 1326,
      nodeInstaller: 17,
      creativeApi: 209,
      modelChromium: 4,
      total: 1556,
    })).not.toThrow();
  });

  it.each([
    ["mainVitest", { mainVitest: 1320, nodeInstaller: 16, creativeApi: 208, modelChromium: 3, total: 1547 }],
    ["nodeInstaller", { mainVitest: 1321, nodeInstaller: 15, creativeApi: 208, modelChromium: 3, total: 1547 }],
    ["creativeApi", { mainVitest: 1321, nodeInstaller: 16, creativeApi: 207, modelChromium: 3, total: 1547 }],
    ["modelChromium", { mainVitest: 1321, nodeInstaller: 16, creativeApi: 208, modelChromium: 2, total: 1547 }],
  ] as const)("rejects a %s regression", (key, counts) => {
    expect(() => assertPrFullTestMinimum(counts)).toThrow(
      `pr-full-gate-baseline-regressed:${key}`,
    );
  });

  it("accepts only the two public gate modes", () => {
    expect(parsePrGateMode(["--mode", "contract"])).toBe("contract");
    expect(parsePrGateMode(["--mode", "full"])).toBe("full");
    expect(() => parsePrGateMode([])).toThrow("pr-gate-mode-required");
    expect(() => parsePrGateMode(["--mode", "quick"])).toThrow("pr-gate-mode-invalid:quick");
  });

  it("finds every pinned fast-gate test in this checkout", async () => {
    const root = resolve(import.meta.dirname, "../../..");
    await expect(verifyContractTestFiles(root, access)).resolves.toBeUndefined();
  });

  it("fails closed when a pinned test file is absent", async () => {
    await expect(verifyContractTestFiles("/missing-root", async () => {
      throw new Error("ENOENT");
    })).rejects.toThrow(`pr-contract-gate-test-file-missing:${PR_CONTRACT_TEST_FILES[0]}`);
  });
});
