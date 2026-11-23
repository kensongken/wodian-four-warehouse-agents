#!/usr/bin/env node

import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { installArtifact, installPersonaBundle } from "./studio-model-installer-lib.mjs";
import { installWorkerRuntime } from "./studio-worker-runtime-installer.mjs";
import {
  artifactApprovalIssue,
  findArtifact,
  loadModelLock,
} from "./studio-model-registry.mjs";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function parseArguments(arguments_) {
  const options = {
    artifact: null,
    persona: false,
    vision: false,
    workerRuntime: false,
    candidateRoot: null,
    derivedRoot: null,
    lock: join(repositoryRoot, "models.lock.json"),
    root: join(homedir(), ".local", "share", "wearable-studio-models"),
  };
  const seen = new Set();
  for (let index = 0; index < arguments_.length; index += 1) {
    const argument = arguments_[index];
    if (seen.has(argument)) throw new Error("studio-model-installer-arguments-invalid");
    seen.add(argument);
    if (argument === "--vision") options.vision = true;
    else if (argument === "--persona") options.persona = true;
    else if (argument === "--worker-runtime") options.workerRuntime = true;
    else if (["--artifact", "--lock", "--root", "--derived-root", "--candidate-root"].includes(argument)) {
      const value = arguments_[index + 1];
      if (!value) throw new Error("studio-model-installer-arguments-invalid");
      const key = argument === "--derived-root"
        ? "derivedRoot"
        : argument === "--candidate-root" ? "candidateRoot" : argument.slice(2);
      options[key] = value;
      index += 1;
    } else {
      throw new Error("studio-model-installer-arguments-invalid");
    }
  }
  if (options.workerRuntime) {
    if (
      options.persona
      || options.vision
      || options.derivedRoot !== null
      || options.artifact === null
      || options.candidateRoot === null
    ) throw new Error("studio-model-installer-arguments-invalid");
    return options;
  }
  if (options.candidateRoot !== null) throw new Error("studio-model-installer-arguments-invalid");
  if ([Boolean(options.artifact), options.persona, options.vision].filter(Boolean).length !== 1) {
    throw new Error("studio-model-installer-explicit-artifact-required");
  }
  return options;
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  if (options.workerRuntime) {
    const result = await installWorkerRuntime({
      bundleId: options.artifact,
      lockPath: options.lock,
      modelRoot: options.root,
      candidateRoot: options.candidateRoot,
    });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return;
  }
  if (options.persona) {
    const result = await installPersonaBundle({
      lockPath: options.lock,
      root: options.root,
      derivedRoot: options.derivedRoot,
    });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return;
  }
  if (options.vision) {
    let lock;
    try {
      lock = await loadModelLock(options.lock);
    } catch {
      throw new Error("studio-model-installer-lock-invalid");
    }
    const artifactIds = ["mlx-vlm-0.6.6", "qwen3-vl-2b-instruct-4bit"];
    for (const artifactId of artifactIds) {
      let artifact;
      try {
        artifact = findArtifact(lock, artifactId);
      } catch {
        throw new Error("studio-model-installer-artifact-unknown");
      }
      const issue = artifactApprovalIssue(artifact);
      if (issue !== null) throw new Error(`studio-model-installer-${issue}`);
    }
    const artifacts = [];
    for (const artifactId of artifactIds) {
      artifacts.push(await installArtifact({
        artifactId,
        lockPath: options.lock,
        root: options.root,
        derivedRoot: options.derivedRoot,
      }));
    }
    process.stdout.write(`${JSON.stringify({ status: "ready", artifacts })}\n`);
    return;
  }
  const result = await installArtifact({
    artifactId: options.artifact,
    lockPath: options.lock,
    root: options.root,
    derivedRoot: options.derivedRoot,
  });
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

main().catch((error) => {
  const code = error instanceof Error && (
    error.message.startsWith("studio-model-installer-")
    || error.message.startsWith("studio-runtime-")
  ) ? error.message : "studio-model-installer-failed";
  process.stderr.write(`${code}\n`);
  process.exitCode = 1;
});
