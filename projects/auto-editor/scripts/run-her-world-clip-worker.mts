import { createRequire } from "node:module";
import { lstat, readFile, rename, writeFile } from "node:fs/promises";
import { isAbsolute, join, resolve } from "node:path";

import {
  HerWorldClipGatewayClient,
  HerWorldClipIntake,
  HerWorldClipRunnerClient,
  HerWorldClipWorker,
  HerWorldStoryRenderer,
} from "../packages/mobile-agent/src/index.ts";

const require = createRequire(new URL("../packages/mobile-agent/package.json", import.meta.url));
const ffprobePath = (require("ffprobe-static") as { path: string }).path;
const baseUrl = requiredUrl("HER_WORLD_CLIP_BASE_URL");
const secretFile = requiredAbsolutePath("HER_WORLD_CLIP_RUNNER_SECRET_FILE");
const workRoot = requiredAbsolutePath("HER_WORLD_CLIP_WORK_ROOT");
const statusFile = requiredAbsolutePath("HER_WORLD_CLIP_STATUS_FILE");
const runOnce = process.env.HER_WORLD_CLIP_RUN_ONCE === "1";
const pollMs = boundedInteger(process.env.HER_WORLD_CLIP_POLL_MS, 10_000, 2_000, 60_000);
const runnerSecret = await readPrivateSecret(secretFile);

const runner = new HerWorldClipRunnerClient({ baseUrl, runnerSecret });
const intake = new HerWorldClipIntake({ root: join(workRoot, "intake"), allowedMediaOrigins: [baseUrl] });
const renderer = new HerWorldStoryRenderer({ workRoot: join(workRoot, "render"), ffprobePath });
const worker = new HerWorldClipWorker({
  runner,
  gatewayFor: (claim) => new HerWorldClipGatewayClient({ baseUrl, jobId: claim.jobId, bearerToken: claim.jobToken }),
  intake,
  renderer,
});

do {
  try {
    const result = await worker.runOnce();
    await recordStatus(result.outcome);
    if (runOnce || result.outcome !== "no-job") process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch {
    await recordStatus("connection-failed").catch(() => undefined);
    process.stderr.write("her-world-clip-worker: connection or claim failed; no customer data was changed\n");
    if (runOnce) process.exitCode = 1;
  }
  if (!runOnce) await new Promise((resolvePromise) => setTimeout(resolvePromise, pollMs));
} while (!runOnce);

function requiredUrl(name: string): string {
  const value = process.env[name]?.trim() ?? "";
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.pathname !== "/") throw new Error(`${name}-invalid`);
  return parsed.origin;
}

function requiredAbsolutePath(name: string): string {
  const value = process.env[name]?.trim() ?? "";
  if (!isAbsolute(value)) throw new Error(`${name}-invalid`);
  return resolve(value);
}

function boundedInteger(raw: string | undefined, fallback: number, min: number, max: number): number {
  const value = raw === undefined ? fallback : Number(raw);
  if (!Number.isSafeInteger(value) || value < min || value > max) throw new Error("HER_WORLD_CLIP_POLL_MS-invalid");
  return value;
}

async function readPrivateSecret(path: string): Promise<string> {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || (info.mode & 0o077) !== 0) {
    throw new Error("HER_WORLD_CLIP_RUNNER_SECRET_FILE-not-private");
  }
  const value = (await readFile(path, "utf8")).trim();
  if (!/^[A-Za-z0-9._~-]{32,256}$/.test(value)) throw new Error("HER_WORLD_CLIP_RUNNER_SECRET_FILE-invalid");
  return value;
}

async function recordStatus(outcome: string): Promise<void> {
  const temporary = `${statusFile}.tmp`;
  await writeFile(temporary, `${JSON.stringify({ lastPollAt: new Date().toISOString(), outcome })}\n`, { mode: 0o600 });
  await rename(temporary, statusFile);
}
