import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tsx = resolve(repositoryRoot, "node_modules/.bin/tsx");
const runner = resolve(
  repositoryRoot,
  "packages/integration-harness/src/studio-model-evaluation.ts",
);
const child = spawn(tsx, [runner, ...process.argv.slice(2)], {
  cwd: repositoryRoot,
  env: process.env,
  stdio: "inherit",
});
child.once("error", (error) => {
  process.stderr.write(`studio-model-eval-cli-failed:${error.message}\n`);
  process.exitCode = 1;
});
child.once("close", (code, signal) => {
  if (signal) process.stderr.write(`studio-model-eval-cli-signal:${signal}\n`);
  process.exitCode = code ?? 1;
});
