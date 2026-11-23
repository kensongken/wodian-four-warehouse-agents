import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const child = spawn(resolve(repositoryRoot, "node_modules/.bin/tsx"), [
  resolve(repositoryRoot, "packages/integration-harness/src/studio-fixed-sha-gate-cli.ts"),
  ...process.argv.slice(2),
], {
  cwd: repositoryRoot,
  env: process.env,
  stdio: "inherit",
  shell: false,
});
child.once("error", (error) => {
  process.stderr.write(`studio-fixed-sha-gate-cli-failed:${error.message}\n`);
  process.exitCode = 1;
});
child.once("close", (code, signal) => {
  if (signal) process.stderr.write(`studio-fixed-sha-gate-cli-signal:${signal}\n`);
  process.exitCode = code ?? 1;
});
