import { resolve } from "node:path";

import type { FixedShaTestCounts } from "./studio-fixed-sha-gate";

export type PrGateMode = "contract" | "full";
export type PrGateCommand = Readonly<{ name: string; argv: readonly string[] }>;

export const PR_CONTRACT_TEST_FILES = [
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
] as const;

export const PR_FULL_TEST_MINIMUM: FixedShaTestCounts = {
  mainVitest: 1321,
  nodeInstaller: 16,
  creativeApi: 208,
  modelChromium: 3,
  total: 1548,
};

export function prGateCommands(mode: PrGateMode): readonly PrGateCommand[] {
  if (mode === "full") return [{ name: "full-tests", argv: ["pnpm", "test"] }];
  return [
    { name: "typecheck", argv: ["pnpm", "typecheck"] },
    {
      name: "contract-tests",
      argv: [
        "node_modules/.bin/vitest", "run", ...PR_CONTRACT_TEST_FILES,
        "--maxWorkers=1", "--fileParallelism=false",
        "--testTimeout=180000", "--hookTimeout=60000",
      ],
    },
  ];
}

export function assertPrFullTestMinimum(counts: FixedShaTestCounts): void {
  for (const key of ["mainVitest", "nodeInstaller", "creativeApi", "modelChromium", "total"] as const) {
    if (counts[key] < PR_FULL_TEST_MINIMUM[key]) {
      throw new Error(
        `pr-full-gate-baseline-regressed:${key}:${counts[key]}<${PR_FULL_TEST_MINIMUM[key]}`,
      );
    }
  }
}

export function parsePrGateMode(args: readonly string[]): PrGateMode {
  const index = args.indexOf("--mode");
  const value = args[index + 1];
  if (index === -1 || !value) throw new Error("pr-gate-mode-required");
  if (value !== "contract" && value !== "full") throw new Error(`pr-gate-mode-invalid:${value}`);
  return value;
}

export async function verifyContractTestFiles(
  repositoryRoot: string,
  accessFile: (path: string) => Promise<unknown>,
): Promise<void> {
  for (const relativePath of PR_CONTRACT_TEST_FILES) {
    try {
      await accessFile(resolve(repositoryRoot, relativePath));
    } catch {
      throw new Error(`pr-contract-gate-test-file-missing:${relativePath}`);
    }
  }
}
