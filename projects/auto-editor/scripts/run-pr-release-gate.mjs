import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const child = spawn(resolve(root, "node_modules/.bin/tsx"), [
  resolve(root, "packages/integration-harness/src/pr-release-gate-cli.ts"),
  ...process.argv.slice(2),
], { cwd: root, env: process.env, stdio: "inherit", shell: false });
child.once("error", (error) => {
  process.stderr.write(`pr-release-gate-cli-failed:${error.message}\n`);
  process.exitCode = 1;
});
child.once("close", (code, signal) => {
  if (signal) process.stderr.write(`pr-release-gate-cli-signal:${signal}\n`);
  process.exitCode = code ?? 1;
});
