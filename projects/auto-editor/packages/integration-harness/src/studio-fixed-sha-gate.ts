import { createHash } from "node:crypto";
import { readdir, readFile, stat } from "node:fs/promises";
import { join, relative, resolve } from "node:path";

export type FixedShaCommandEvidence = Readonly<{
  name: string;
  argv: readonly string[];
  exitCode: number;
  stdoutDigest: string;
  stderrDigest: string;
  summary: string;
}>;

export type FixedShaTestCounts = Readonly<{
  mainVitest: number;
  nodeInstaller: number;
  creativeApi: number;
  modelChromium: number;
  total: number;
}>;

export type FixedShaOutputDigest = Readonly<{
  relativePath: string;
  sha256: string;
  byteLength: number;
}>;

export function summarizeFullTestOutput(output: string): FixedShaTestCounts {
  const plain = output.replace(/\x1B\[[0-?]*[ -/]*[@-~]/gu, "");
  const vitestCounts = [...plain.matchAll(/^\s*Tests\s+(\d+) passed \(\1\)\s*$/gmu)]
    .map((match) => Number(match[1]));
  const nodeTests = /\u2139 tests (\d+)/u.exec(plain);
  const nodePass = /\u2139 pass (\d+)/u.exec(plain);
  const creative = /creative-api deterministic shards:\s*(\d+)\/(\d+) tests passed/u.exec(plain);
  if (vitestCounts.length < 2 || !nodeTests || !nodePass || !creative ||
      nodeTests[1] !== nodePass[1] || creative[1] !== creative[2] ||
      !plain.includes("studio model Chromium pilot: isolated pass")) {
    throw new Error("studio-fixed-gate-test-summary-incomplete");
  }
  const counts = {
    mainVitest: vitestCounts[0]!,
    nodeInstaller: Number(nodeTests[1]),
    creativeApi: Number(creative[1]),
    modelChromium: vitestCounts.at(-1)!,
  };
  return { ...counts, total: Object.values(counts).reduce((sum, value) => sum + value, 0) };
}

export async function digestGateOutputs(rootValue: string): Promise<FixedShaOutputDigest[]> {
  const root = resolve(rootValue);
  const files: string[] = [];
  await walk(root);
  const result: FixedShaOutputDigest[] = [];
  for (const path of files.sort(codePointCompare)) {
    const relativePath = relative(root, path);
    if (relativePath === "fixed-sha-gate.json") continue;
    const bytes = await readFile(path);
    result.push({
      relativePath,
      sha256: createHash("sha256").update(bytes).digest("hex"),
      byteLength: bytes.byteLength,
    });
  }
  return result;

  async function walk(directory: string): Promise<void> {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await walk(path);
      else if (entry.isFile()) files.push(path);
    }
  }
}

export function createFixedShaGateReport(input: Readonly<{
  candidateSha: string;
  cleanBefore: boolean;
  cleanAfter: boolean;
  commands: readonly FixedShaCommandEvidence[];
  testCounts: FixedShaTestCounts;
  modelGate: Readonly<{
    exitCode: number;
    unapprovedArtifactCount: number;
    visionProbe: string;
  }>;
  outputs: readonly FixedShaOutputDigest[];
}>) {
  if (!/^[a-f0-9]{40}$/u.test(input.candidateSha)) throw new Error("studio-fixed-gate-candidate-sha-invalid");
  const required = ["model-checker", "full-tests", "typecheck", "evaluation", "browser", "diff-check"];
  if (required.some((name) => input.commands.filter((command) => command.name === name).length !== 1)) {
    throw new Error("studio-fixed-gate-command-set-invalid");
  }
  const checker = input.commands.find((command) => command.name === "model-checker")!;
  const green = input.commands.filter((command) => command.name !== "model-checker");
  if (checker.exitCode !== 1 || input.modelGate.exitCode !== 1 ||
      input.modelGate.unapprovedArtifactCount < 1 || !input.modelGate.visionProbe.startsWith("not-run:")) {
    throw new Error("studio-fixed-gate-model-boundary-invalid");
  }
  if (green.some((command) => command.exitCode !== 0) || !input.cleanBefore || !input.cleanAfter) {
    throw new Error("studio-fixed-gate-mechanical-command-failed");
  }
  const outputSetDigest = createHash("sha256")
    .update(JSON.stringify(input.outputs))
    .digest("hex");
  return {
    schemaVersion: 1 as const,
    candidateSha: input.candidateSha,
    clean: { before: input.cleanBefore, after: input.cleanAfter },
    commands: input.commands,
    testCounts: input.testCounts,
    modelGate: {
      status: "blocked" as const,
      exitCode: input.modelGate.exitCode,
      unapprovedArtifactCount: input.modelGate.unapprovedArtifactCount,
      visionProbe: input.modelGate.visionProbe,
    },
    realProviderAcceptance: {
      status: "blocked" as const,
      reason: "approved-local-browser-evidence-not-provided" as const,
    },
    outputs: input.outputs,
    outputSetDigest,
    gateStatus: "fixture-mechanical-pass-real-provider-blocked" as const,
  };
}

export async function assertNonEmptyGateOutput(path: string): Promise<void> {
  if ((await stat(path)).size < 1) throw new Error("studio-fixed-gate-output-empty");
}

function codePointCompare(left: string, right: string): number {
  const leftPoints = Array.from(left, (value) => value.codePointAt(0)!);
  const rightPoints = Array.from(right, (value) => value.codePointAt(0)!);
  for (let index = 0; index < Math.min(leftPoints.length, rightPoints.length); index += 1) {
    if (leftPoints[index] !== rightPoints[index]) return leftPoints[index]! - rightPoints[index]!;
  }
  return leftPoints.length - rightPoints.length;
}
