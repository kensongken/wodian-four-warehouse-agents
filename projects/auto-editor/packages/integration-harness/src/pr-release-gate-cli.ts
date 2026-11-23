import { spawn } from "node:child_process";
import { access } from "node:fs/promises";
import { resolve } from "node:path";

import {
  assertPrFullTestMinimum,
  parsePrGateMode,
  prGateCommands,
  verifyContractTestFiles,
} from "./pr-release-gate";
import { summarizeFullTestOutput } from "./studio-fixed-sha-gate";

const repositoryRoot = resolve(import.meta.dirname, "../../..");
const mode = parsePrGateMode(process.argv.slice(2));
if (mode === "contract") await verifyContractTestFiles(repositoryRoot, access);

let fullTestOutput = "";
for (const command of prGateCommands(mode)) {
  const result = await run(command.argv);
  if (command.name === "full-tests") fullTestOutput = result.stdout;
}

const testCounts = mode === "full" ? summarizeFullTestOutput(fullTestOutput) : undefined;
if (testCounts) assertPrFullTestMinimum(testCounts);
process.stdout.write(`${JSON.stringify({ gate: mode, status: "pass", testCounts })}\n`);

function run(argv: readonly string[]): Promise<Readonly<{ stdout: string; stderr: string }>> {
  return new Promise((resolveRun, reject) => {
    const child = spawn(argv[0]!, argv.slice(1), {
      cwd: repositoryRoot,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      const value = String(chunk);
      stdout += value;
      process.stdout.write(value);
    });
    child.stderr.on("data", (chunk) => {
      const value = String(chunk);
      stderr += value;
      process.stderr.write(value);
    });
    child.once("error", reject);
    child.once("close", (code, signal) => {
      if (signal) return reject(new Error(`pr-gate-command-signal:${signal}`));
      if (code !== 0) return reject(new Error(`pr-gate-command-failed:${argv[0]}:${code}`));
      resolveRun({ stdout, stderr });
    });
  });
}
