import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import {
  createFixedShaGateReport,
  digestGateOutputs,
  summarizeFullTestOutput,
  type FixedShaCommandEvidence,
} from "./studio-fixed-sha-gate";

const repositoryRoot = resolve(import.meta.dirname, "../../..");

const args = process.argv.slice(2).filter((argument) => argument !== "--");
const candidateSha = option(args, "--candidate-sha");
const outputDirectory = resolve(option(args, "--output"));
const currentSha = (await capture(["git", "rev-parse", "HEAD"])).stdout.trim();
if (candidateSha !== currentSha) throw new Error("studio-fixed-gate-candidate-sha-mismatch");
const cleanBefore = (await capture(["git", "status", "--porcelain"])).stdout.trim() === "";
if (!cleanBefore) throw new Error("studio-fixed-gate-worktree-dirty-before");
await mkdir(outputDirectory, { recursive: true });

const commands: FixedShaCommandEvidence[] = [];
const checker = await runCommand("model-checker", [
  process.execPath, "scripts/check-studio-models.mjs", "--require-ready", "--json",
], [1]);
const checkerPayload = parseJson(checker.stdout) as {
  artifacts?: Array<{ status?: unknown }>;
  visionProbe?: unknown;
};
const unapprovedArtifactCount = (checkerPayload.artifacts ?? [])
  .filter((artifact) => artifact.status === "unapproved").length;
const visionProbe = String(checkerPayload.visionProbe ?? "");
commands.push(commandEvidence(checker, `expected blocked: ${unapprovedArtifactCount} unapproved; ${visionProbe}`));

const fullTests = await runCommand("full-tests", ["pnpm", "test"]);
const testCounts = summarizeFullTestOutput(fullTests.stdout);
commands.push(commandEvidence(fullTests,
  `${testCounts.total} passed (${testCounts.mainVitest}+${testCounts.nodeInstaller}+${testCounts.creativeApi}+${testCounts.modelChromium})`));

const typecheck = await runCommand("typecheck", ["pnpm", "typecheck"]);
commands.push(commandEvidence(typecheck, "TypeScript noEmit pass"));

const evaluationDirectory = resolve(outputDirectory, "evaluation");
const evaluation = await runCommand("evaluation", [
  "pnpm", "studio:model-eval", "--", "--manifest", "fixtures/studio-model-eval/manifest.json",
  "--output", evaluationDirectory,
]);
const evaluationPayload = parseJsonFromCommandOutput(evaluation.stdout) as { fixtureMechanicalPass?: unknown };
if (evaluationPayload.fixtureMechanicalPass !== true) throw new Error("studio-fixed-gate-evaluation-not-pass");
commands.push(commandEvidence(evaluation, "fixture/synthetic evaluation mechanical pass"));

const browserDirectory = resolve(outputDirectory, "browser");
const browser = await runCommand("browser", [
  resolve(repositoryRoot, "node_modules/.bin/tsx"),
  "packages/review-app/src/studio-model-browser-pilot.ts", "--output", browserDirectory,
]);
const browserPayload = parseJson(browser.stdout) as { fixtureMechanicalPass?: unknown; providerEvidence?: unknown };
if (browserPayload.fixtureMechanicalPass !== true || browserPayload.providerEvidence !== "fixture/synthetic") {
  throw new Error("studio-fixed-gate-browser-not-fixture-pass");
}
commands.push(commandEvidence(browser, "Chromium fixture/synthetic mechanical pass"));

const diffCheck = await runCommand("diff-check", ["git", "diff", "--check"]);
commands.push(commandEvidence(diffCheck, "git diff --check pass"));
const finalSha = (await capture(["git", "rev-parse", "HEAD"])).stdout.trim();
const cleanAfter = finalSha === candidateSha &&
  (await capture(["git", "status", "--porcelain"])).stdout.trim() === "";
const outputs = await digestGateOutputs(outputDirectory);
const report = createFixedShaGateReport({
  candidateSha,
  cleanBefore,
  cleanAfter,
  commands,
  testCounts,
  modelGate: { exitCode: checker.exitCode, unapprovedArtifactCount, visionProbe },
  outputs,
});
await writeFile(resolve(outputDirectory, "fixed-sha-gate.json"), `${JSON.stringify(report, null, 2)}\n`, "utf8");
process.stdout.write(`${JSON.stringify({
  gateStatus: report.gateStatus,
  candidateSha: report.candidateSha,
  testCounts: report.testCounts,
  outputSetDigest: report.outputSetDigest,
}, null, 2)}\n`);

function option(values: readonly string[], name: string): string {
  const index = values.indexOf(name);
  const value = values[index + 1];
  if (index === -1 || !value || value.startsWith("--")) throw new Error(`${name.slice(2)}-required`);
  return value;
}

type CommandResult = Readonly<{
  name: string;
  argv: readonly string[];
  exitCode: number;
  stdout: string;
  stderr: string;
}>;

async function runCommand(
  name: string,
  argv: readonly string[],
  acceptedExitCodes: readonly number[] = [0],
): Promise<CommandResult> {
  const result = await capture(argv);
  const value = { name, argv, ...result };
  if (!acceptedExitCodes.includes(result.exitCode)) {
    throw new Error(`studio-fixed-gate-command-failed:${name}:${result.exitCode}\n${result.stderr.slice(-2_000)}`);
  }
  return value;
}

function capture(argv: readonly string[]): Promise<Readonly<{ exitCode: number; stdout: string; stderr: string }>> {
  return new Promise((resolveCapture, reject) => {
    const child = spawn(argv[0]!, argv.slice(1), {
      cwd: repositoryRoot,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += String(chunk); });
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.once("error", reject);
    child.once("close", (code, signal) => {
      if (signal) return reject(new Error(`studio-fixed-gate-command-signal:${signal}`));
      resolveCapture({ exitCode: code ?? 1, stdout, stderr });
    });
  });
}

function commandEvidence(command: CommandResult, summary: string): FixedShaCommandEvidence {
  return {
    name: command.name,
    argv: command.argv,
    exitCode: command.exitCode,
    stdoutDigest: sha256(command.stdout),
    stderrDigest: sha256(command.stderr),
    summary,
  };
}

function parseJson(value: string): unknown {
  try {
    return JSON.parse(value);
  } catch {
    throw new Error("studio-fixed-gate-command-json-invalid");
  }
}

function parseJsonFromCommandOutput(value: string): unknown {
  const start = value.indexOf("{");
  if (start === -1) throw new Error("studio-fixed-gate-command-json-missing");
  return parseJson(value.slice(start));
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}
