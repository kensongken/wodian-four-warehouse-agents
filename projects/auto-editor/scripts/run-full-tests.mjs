import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const vitestCli = resolve(repositoryRoot, "node_modules/vitest/vitest.mjs");
const creativeTest = "packages/review-app/tests/creative-api.test.ts";
const studioModelBrowserTest = "packages/review-app/tests/studio-model-browser-pilot.test.ts";
const studioModelInstallTest = "scripts/tests/studio-model-install.test.mjs";
const studioModelApprovalTest = "scripts/tests/studio-model-approval.test.mjs";
const localPersonaModelProbeTest = "scripts/tests/local-persona-model-probe.test.mjs";
const workerRuntimeInstallTest = "scripts/tests/studio-worker-runtime-install.test.mjs";
const common = ["--testTimeout=180000", "--hookTimeout=60000"];

await runInherited([
  "run",
  "--exclude", creativeTest,
  "--exclude", studioModelBrowserTest,
  "--exclude", studioModelInstallTest,
  "--exclude", studioModelApprovalTest,
  "--exclude", localPersonaModelProbeTest,
  "--exclude", workerRuntimeInstallTest,
  "--exclude", ".superpowers/**",
  "--maxWorkers=1",
  "--fileParallelism=false",
  ...common,
]);

await runNodeTest(studioModelInstallTest);
await runNodeTest(studioModelApprovalTest);
await runNodeTest(localPersonaModelProbeTest);
await runNodeTest(workerRuntimeInstallTest);

const listed = await runCaptured(["list", creativeTest]);
const expectedCreativeTests = listed.stdout.split(/\r?\n/)
  .filter((line) => line.includes(`${creativeTest} > `)).length;
if (expectedCreativeTests < 1) throw new Error("creative-api-test-list-empty");

const shardCount = 4;
const shards = await Promise.all(Array.from({ length: shardCount }, (_, offset) =>
  runCaptured(["run", creativeTest, "--maxWorkers=1", "--fileParallelism=false", ...common], {
    CREATIVE_API_SHARD: `${offset + 1}/${shardCount}`,
  })));
let passed = 0;
for (const [offset, result] of shards.entries()) {
  process.stdout.write(`\n[creative-api shard ${offset + 1}/${shardCount}]\n${result.stdout}`);
  process.stderr.write(result.stderr);
  const plain = result.stdout.replace(/\x1b\[[0-9;]*m/g, "");
  const match = /Tests\s+(\d+) passed(?:\s+\|\s+(\d+) skipped)?\s+\((\d+)\)/.exec(plain);
  if (!match || Number(match[3]) !== expectedCreativeTests) {
    throw new Error(`creative-api-shard-count-invalid:${offset + 1}`);
  }
  passed += Number(match[1]);
}
if (passed !== expectedCreativeTests) {
  throw new Error(`creative-api-shard-coverage-invalid:${passed}/${expectedCreativeTests}`);
}
process.stdout.write(`\ncreative-api deterministic shards: ${passed}/${expectedCreativeTests} tests passed\n`);

await runInherited([
  "run",
  studioModelBrowserTest,
  "--maxWorkers=1",
  "--fileParallelism=false",
  ...common,
]);
process.stdout.write("\nstudio model Chromium pilot: isolated pass\n");

function runInherited(args) {
  return new Promise((resolveRun, reject) => {
    const child = spawn(process.execPath, [vitestCli, ...args], {
      cwd: repositoryRoot,
      env: process.env,
      stdio: "inherit",
    });
    child.once("error", reject);
    child.once("close", (code) => code === 0
      ? resolveRun()
      : reject(new Error(`vitest-unsharded-failed:${code}`)));
  });
}

function runNodeTest(path) {
  return new Promise((resolveRun, reject) => {
    const child = spawn(process.execPath, ["--test", path], {
      cwd: repositoryRoot,
      env: process.env,
      stdio: "inherit",
    });
    child.once("error", reject);
    child.once("close", (code) => code === 0
      ? resolveRun()
      : reject(new Error(`node-test-failed:${path}:${code}`)));
  });
}

function runCaptured(args, extraEnvironment = {}) {
  return new Promise((resolveRun, reject) => {
    const child = spawn(process.execPath, [vitestCli, ...args], {
      cwd: repositoryRoot,
      env: { ...process.env, ...extraEnvironment },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += String(chunk); });
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.once("error", reject);
    child.once("close", (code) => code === 0
      ? resolveRun({ stdout, stderr })
      : reject(new Error(`vitest-shard-failed:${code}\n${stdout}\n${stderr}`)));
  });
}
