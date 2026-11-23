#!/usr/bin/env node

import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { inspectArtifact, loadModelLock } from "./studio-model-registry.mjs";
import {
  FORMAL_WORKER_RUNTIME_ID,
  findWorkerRuntime,
  inspectWorkerRuntime,
} from "./studio-worker-runtime-registry.mjs";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const safePublicRuntimeIdentity = /^[a-zA-Z0-9._-]{1,200}$/u;
const safeDigest = /^[a-f0-9]{64}$/u;
const defaultFormalRuntimeId = FORMAL_WORKER_RUNTIME_ID;

export function normalizeWorkerRuntimeReadiness(bundle, inspectedRuntime) {
  const status = inspectedRuntime?.status;
  const allowed = new Set([
    "runtime-not-installed",
    "runtime-integrity-failed",
    "runtime-installed-probe-required",
    "runtime-real-probe-failed",
    "runtime-real-probe-succeeded",
  ]);
  return allowed.has(status) ? status : "runtime-integrity-failed";
}

export function isStudioModelCheckerReady(artifacts, workerRuntime) {
  return Array.isArray(artifacts)
    && artifacts.length > 0
    && artifacts.every((artifact) => artifact?.status === "ready")
    && workerRuntime?.status === "runtime-real-probe-succeeded";
}

function parseArguments(arguments_) {
  const options = {
    json: false,
    lock: join(repositoryRoot, "models.lock.json"),
    root: join(homedir(), ".local", "share", "wearable-studio-models"),
    artifact: null,
    requireReady: false,
  };
  for (let index = 0; index < arguments_.length; index += 1) {
    const argument = arguments_[index];
    if (argument === "--json") options.json = true;
    else if (argument === "--require-ready") options.requireReady = true;
    else if (["--lock", "--root", "--artifact"].includes(argument)) {
      const value = arguments_[index + 1];
      if (!value) throw new Error("studio-model-checker-arguments-invalid");
      options[argument.slice(2)] = value;
      index += 1;
    } else {
      throw new Error("studio-model-checker-arguments-invalid");
    }
  }
  return options;
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  let lock;
  try {
    lock = await loadModelLock(options.lock);
  } catch {
    throw new Error("studio-model-checker-lock-invalid");
  }
  if (options.artifact !== null) {
    const artifactMatches = lock.artifacts.filter((artifact) => artifact.id === options.artifact);
    const runtimeMatches = (lock.workerRuntimes ?? []).filter((runtime) => runtime?.id === options.artifact);
    if (artifactMatches.length + runtimeMatches.length !== 1) {
      throw new Error("studio-model-checker-artifact-unknown");
    }
    if (runtimeMatches.length === 1) {
      let bundle;
      try {
        bundle = findWorkerRuntime(lock, options.artifact);
      } catch {
        throw new Error("studio-model-checker-artifact-unknown");
      }
      const publicMetadataSafe = safePublicRuntimeIdentity.test(bundle.id)
        && safePublicRuntimeIdentity.test(bundle.version)
        && safeDigest.test(bundle.treeSha256);
      const inspectedRuntime = publicMetadataSafe
        ? await inspectWorkerRuntime(bundle, resolve(options.root), lock)
        : { status: "runtime-integrity-failed" };
      const runtime = {
        ...inspectedRuntime,
        status: normalizeWorkerRuntimeReadiness(bundle, inspectedRuntime),
      };
      const report = { schemaVersion: 1, workerRuntimes: [runtime] };
      if (options.json) process.stdout.write(`${JSON.stringify(report)}\n`);
      else process.stdout.write(`${runtime.id}: ${runtime.status}\n`);
      if (options.requireReady && runtime.status !== "runtime-real-probe-succeeded") {
        process.exitCode = 1;
      }
      return;
    }
  }
  const selected = options.artifact === null
    ? lock.artifacts
    : lock.artifacts.filter((artifact) => artifact.id === options.artifact);
  if (options.artifact !== null && selected.length !== 1) {
    throw new Error("studio-model-checker-artifact-unknown");
  }
  const root = resolve(options.root);
  const artifacts = [];
  for (const artifact of selected) artifacts.push(await inspectArtifact(artifact, root));
  const runtimeLock = lock.artifacts.find((artifact) => (
    artifact.id === "mlx-vlm-0.6.6"
    || (artifact.artifactKind === "source-package" && artifact.id.includes("mlx-vlm"))
  ));
  const qwenLock = lock.artifacts.find((artifact) => (
    artifact.id === "qwen3-vl-2b-instruct-4bit"
    || (artifact.artifactKind === "checkpoint" && artifact.id.includes("qwen"))
  ));
  const selectedById = new Map(artifacts.map((artifact) => [artifact.id, artifact]));
  const inspectForProbe = async (artifact) => {
    if (artifact === undefined) return undefined;
    return selectedById.get(artifact.id) ?? inspectArtifact(artifact, root);
  };
  const runtime = await inspectForProbe(runtimeLock);
  const qwen = await inspectForProbe(qwenLock);
  const formalRuntimeBundle = (lock.workerRuntimes ?? []).find(
    (candidate) => candidate?.id === defaultFormalRuntimeId,
  );
  let formalRuntime;
  if (formalRuntimeBundle !== undefined) {
    const publicMetadataSafe = safePublicRuntimeIdentity.test(formalRuntimeBundle.id)
      && safePublicRuntimeIdentity.test(formalRuntimeBundle.version)
      && safeDigest.test(formalRuntimeBundle.treeSha256);
    const inspected = publicMetadataSafe
      ? await inspectWorkerRuntime(formalRuntimeBundle, root, lock)
      : { status: "runtime-integrity-failed" };
    formalRuntime = {
      ...inspected,
      status: normalizeWorkerRuntimeReadiness(formalRuntimeBundle, inspected),
    };
  }
  const notRunReason = (role, artifact) => {
    if (artifact === undefined) return `${role}-not-locked`;
    if (artifact.status === "unapproved") return `${role}-unapproved`;
    if (artifact.status !== "ready") return `${role}-${artifact.status}`;
    return null;
  };
  const runtimeReason = notRunReason("runtime", runtime);
  const modelReason = notRunReason("model", qwen);
  const visionProbe = formalRuntime === undefined
    ? `not-run: ${runtimeReason ?? modelReason ?? "explicit-local-probe-required"}`
    : formalRuntime.status === "runtime-real-probe-succeeded"
      ? "succeeded: formal-synthetic-qwen-probe"
      : `not-run: ${formalRuntime.status}`;
  const report = {
    schemaVersion: 1,
    root,
    artifacts,
    visionProbe,
    ...(formalRuntime === undefined ? {} : { workerRuntimes: [formalRuntime] }),
  };
  if (options.json) process.stdout.write(`${JSON.stringify(report)}\n`);
  else {
    for (const artifact of artifacts) process.stdout.write(`${artifact.id}: ${artifact.status}\n`);
    process.stdout.write(`vision-probe: ${visionProbe}\n`);
  }
  if (options.requireReady && !isStudioModelCheckerReady(artifacts, formalRuntime)) {
    process.exitCode = 1;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    const code = error instanceof Error && error.message.startsWith("studio-model-checker-")
      ? error.message
      : "studio-model-checker-failed";
    process.stderr.write(`${code}\n`);
    process.exitCode = 1;
  });
}
