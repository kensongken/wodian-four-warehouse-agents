#!/usr/bin/env node

import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import {
  artifactManifest,
  findArtifact,
  inspectArtifact,
  loadModelLock,
} from "./studio-model-registry.mjs";

const execFileAsync = promisify(execFile);
const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const digestPattern = /^[a-f0-9]{64}$/u;
const expectedArtifactIds = Object.freeze({
  whisperBinary: "whisper.cpp-1.9.2-cli-darwin-arm64",
  whisperModel: "whisper.cpp-1.9.2-small-q5_1",
  mlxRuntime: "mlx-vlm-0.6.6",
  qwenModel: "qwen3-vl-2b-instruct-4bit",
});

const sha256 = (value) => createHash("sha256").update(value).digest("hex");

function validTimestamp(value) {
  return typeof value === "string" && value.endsWith("Z") && Number.isFinite(Date.parse(value));
}

function artifactEvidence(value, expectedId) {
  if (value?.id !== expectedId || !digestPattern.test(value?.sha256)) {
    throw new Error("local-persona-model-probe-invalid");
  }
  return Object.freeze({ id: value.id, sha256: value.sha256 });
}

function resultEvidence(value, { includeSuggestionCount = false, includePeakFootprint = false } = {}) {
  if (value?.status !== "succeeded"
    || value?.realModel !== true
    || value?.syntheticInput !== true
    || !digestPattern.test(value?.inputSha256)
    || !digestPattern.test(value?.outputSha256)
    || !Number.isSafeInteger(value?.runtimeMs)
    || value.runtimeMs < 0
    || !Number.isSafeInteger(value?.maximumResidentSetBytes)
    || value.maximumResidentSetBytes <= 0) {
    throw new Error("local-persona-model-probe-invalid");
  }
  const result = {
    status: "succeeded",
    inputSha256: value.inputSha256,
    outputSha256: value.outputSha256,
    runtimeMs: value.runtimeMs,
    maximumResidentSetBytes: value.maximumResidentSetBytes,
  };
  if (includeSuggestionCount) {
    if (!Number.isSafeInteger(value?.suggestionCount) || value.suggestionCount <= 0) {
      throw new Error("local-persona-model-probe-invalid");
    }
    result.suggestionCount = value.suggestionCount;
  }
  if (includePeakFootprint) {
    if (!Number.isSafeInteger(value?.peakMemoryFootprintBytes)
      || value.peakMemoryFootprintBytes <= 0) {
      throw new Error("local-persona-model-probe-invalid");
    }
    result.peakMemoryFootprintBytes = value.peakMemoryFootprintBytes;
  }
  return Object.freeze(result);
}

export function buildLocalPersonaModelProbeReport(input) {
  if (!validTimestamp(input?.startedAt)
    || !validTimestamp(input?.completedAt)
    || Date.parse(input.completedAt) < Date.parse(input.startedAt)
    || typeof input?.device?.model !== "string"
    || !input.device.model.trim()
    || input.device?.architecture !== "arm64"
    || !Number.isSafeInteger(input.device?.memoryBytes)
    || input.device.memoryBytes <= 0) {
    throw new Error("local-persona-model-probe-invalid");
  }
  const artifacts = Object.fromEntries(Object.entries(expectedArtifactIds).map(([key, id]) => (
    [key, artifactEvidence(input.artifacts?.[key], id)]
  )));
  return Object.freeze({
    schemaVersion: 1,
    startedAt: input.startedAt,
    completedAt: input.completedAt,
    device: Object.freeze({
      model: input.device.model,
      memoryBytes: input.device.memoryBytes,
      architecture: "arm64",
    }),
    artifacts: Object.freeze(artifacts),
    transcript: resultEvidence(input.whisper, { includeSuggestionCount: true }),
    vision: resultEvidence(input.qwen, { includePeakFootprint: true }),
    syntheticInput: true,
    realModel: true,
    customerContentUsed: false,
    publishedClaimed: false,
  });
}

function parseArguments(arguments_) {
  const options = {
    lock: join(repositoryRoot, "models.lock.json"),
    root: join(homedir(), ".local", "share", "wearable-studio-models"),
    output: null,
    mlxGenerate: null,
    ffmpeg: join(repositoryRoot, "node_modules", ".pnpm", "ffmpeg-static@5.2.0", "node_modules", "ffmpeg-static", "ffmpeg"),
    say: "/usr/bin/say",
  };
  for (let index = 0; index < arguments_.length; index += 1) {
    const argument = arguments_[index];
    if (["--lock", "--root", "--output", "--mlx-generate", "--ffmpeg", "--say"].includes(argument)) {
      const value = arguments_[index + 1];
      if (!value) throw new Error("local-persona-model-probe-arguments-invalid");
      const key = argument === "--mlx-generate"
        ? "mlxGenerate"
        : argument.slice(2);
      options[key] = value;
      index += 1;
    } else {
      throw new Error("local-persona-model-probe-arguments-invalid");
    }
  }
  if (!options.output || !options.mlxGenerate) {
    throw new Error("local-persona-model-probe-arguments-invalid");
  }
  return options;
}

function parseResourceValue(stderr, label) {
  const match = stderr.match(new RegExp(`^\\s*(\\d+)\\s+${label}$`, "mu"));
  const value = match === null ? Number.NaN : Number(match[1]);
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error("local-persona-model-probe-resource-invalid");
  }
  return value;
}

async function runTimed(command, arguments_, timeout) {
  const started = performance.now();
  let result;
  try {
    result = await execFileAsync("/usr/bin/time", ["-l", command, ...arguments_], {
      timeout,
      maxBuffer: 512 * 1024,
      encoding: "buffer",
      env: {
        ...process.env,
        HF_HUB_OFFLINE: "1",
        TRANSFORMERS_OFFLINE: "1",
      },
    });
  } catch {
    throw new Error("local-persona-model-probe-command-failed");
  }
  const stderr = Buffer.from(result.stderr).toString("utf8");
  return {
    stdout: Buffer.from(result.stdout),
    runtimeMs: Math.max(0, Math.round(performance.now() - started)),
    maximumResidentSetBytes: parseResourceValue(stderr, "maximum resident set size"),
    peakMemoryFootprintBytes: parseResourceValue(stderr, "peak memory footprint"),
  };
}

async function readyArtifact(lock, artifactId, root) {
  const artifact = findArtifact(lock, artifactId);
  const inspected = await inspectArtifact(artifact, root);
  if (inspected.status !== "ready") throw new Error("local-persona-model-probe-artifact-not-ready");
  return artifact;
}

function manifestDigest(artifact, matcher) {
  const files = artifactManifest(artifact).filter(matcher);
  if (files.length !== 1) throw new Error("local-persona-model-probe-artifact-invalid");
  return files[0].sha256;
}

async function deviceEvidence() {
  try {
    const [model, memory, architecture] = await Promise.all([
      execFileAsync("/usr/sbin/sysctl", ["-n", "hw.model"]),
      execFileAsync("/usr/sbin/sysctl", ["-n", "hw.memsize"]),
      execFileAsync("/usr/bin/uname", ["-m"]),
    ]);
    return {
      model: model.stdout.trim(),
      memoryBytes: Number(memory.stdout.trim()),
      architecture: architecture.stdout.trim(),
    };
  } catch {
    throw new Error("local-persona-model-probe-device-unavailable");
  }
}

async function runRealProbe(options) {
  const root = resolve(options.root);
  const lock = await loadModelLock(options.lock);
  const artifacts = {};
  for (const [key, id] of Object.entries(expectedArtifactIds)) {
    artifacts[key] = await readyArtifact(lock, id, root);
  }
  const evidence = {
    whisperBinary: {
      id: expectedArtifactIds.whisperBinary,
      sha256: manifestDigest(artifacts.whisperBinary, (file) => file.executable),
    },
    whisperModel: {
      id: expectedArtifactIds.whisperModel,
      sha256: manifestDigest(artifacts.whisperModel, (file) => file.sourceKind === "weights"),
    },
    mlxRuntime: {
      id: expectedArtifactIds.mlxRuntime,
      sha256: manifestDigest(artifacts.mlxRuntime, () => true),
    },
    qwenModel: {
      id: expectedArtifactIds.qwenModel,
      sha256: manifestDigest(
        artifacts.qwenModel,
        (file) => file.relativePath.endsWith("/model.safetensors"),
      ),
    },
  };
  const temporaryRoot = await mkdtemp(join(tmpdir(), "local-persona-model-probe-"));
  const aiff = join(temporaryRoot, "speech.aiff");
  const wav = join(temporaryRoot, "speech.wav");
  const image = join(temporaryRoot, "shapes.png");
  try {
    await execFileAsync(options.say, [
      "-v", "Tingting", "-o", aiff,
      "这是一段本地模型测试音频，不包含任何客户内容。今天我们正在验证珠宝内容剪辑系统。",
    ], { timeout: 30_000, maxBuffer: 32 * 1024 });
    await execFileAsync(options.ffmpeg, [
      "-v", "error", "-nostdin", "-i", aiff,
      "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", wav,
    ], { timeout: 30_000, maxBuffer: 32 * 1024 });
    await execFileAsync(options.ffmpeg, [
      "-v", "error", "-nostdin", "-f", "lavfi",
      "-i", "color=c=white:s=640x360:d=1",
      "-vf", "drawbox=x=70:y=80:w=180:h=180:color=red:t=fill,drawbox=x=390:y=80:w=180:h=180:color=blue:t=fill",
      "-frames:v", "1", "-y", image,
    ], { timeout: 30_000, maxBuffer: 32 * 1024 });

    const whisper = await runTimed(join(root, "whisper/bin/darwin-arm64/whisper-cli"), [
      "-m", join(root, "whisper/ggml-small-q5_1.bin"),
      "-f", wav, "-oj", "-of", "-", "-np", "-l", "zh",
    ], 180_000);
    let whisperPayload;
    try {
      whisperPayload = JSON.parse(whisper.stdout.toString("utf8"));
    } catch {
      throw new Error("local-persona-model-probe-output-invalid");
    }
    const suggestions = whisperPayload?.transcription;
    if (!Array.isArray(suggestions) || suggestions.length === 0) {
      throw new Error("local-persona-model-probe-output-invalid");
    }

    const qwen = await runTimed(resolve(options.mlxGenerate), [
      "--model", join(root, "qwen3-vl-2b-instruct-4bit"),
      "--image", image,
      "--max-tokens", "64",
      "--temperature", "0",
      "--no-verbose",
      "--prompt", "只用一行中文描述图中颜色和几何图形，不要猜测身份、材质或用途。",
    ], 180_000);
    if (!qwen.stdout.toString("utf8").trim()) {
      throw new Error("local-persona-model-probe-output-invalid");
    }
    const startedAt = new Date(Date.now() - whisper.runtimeMs - qwen.runtimeMs).toISOString();
    return buildLocalPersonaModelProbeReport({
      startedAt,
      completedAt: new Date().toISOString(),
      device: await deviceEvidence(),
      artifacts: evidence,
      whisper: {
        status: "succeeded",
        realModel: true,
        syntheticInput: true,
        inputSha256: sha256(await readFile(wav)),
        outputSha256: sha256(whisper.stdout),
        runtimeMs: whisper.runtimeMs,
        maximumResidentSetBytes: whisper.maximumResidentSetBytes,
        suggestionCount: suggestions.length,
      },
      qwen: {
        status: "succeeded",
        realModel: true,
        syntheticInput: true,
        inputSha256: sha256(await readFile(image)),
        outputSha256: sha256(qwen.stdout),
        runtimeMs: qwen.runtimeMs,
        maximumResidentSetBytes: qwen.maximumResidentSetBytes,
        peakMemoryFootprintBytes: qwen.peakMemoryFootprintBytes,
      },
    });
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  if (![options.mlxGenerate, options.ffmpeg, options.say].every(isAbsolute)) {
    throw new Error("local-persona-model-probe-arguments-invalid");
  }
  const report = await runRealProbe(options);
  const output = resolve(options.output);
  await mkdir(dirname(output), { recursive: true, mode: 0o700 });
  await writeFile(output, `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 });
  process.stdout.write(`${JSON.stringify({ status: "succeeded", outputSha256: sha256(await readFile(output)) })}\n`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    const code = error instanceof Error && error.message.startsWith("local-persona-model-probe-")
      ? error.message
      : "local-persona-model-probe-failed";
    process.stderr.write(`${code}\n`);
    process.exitCode = 1;
  });
}
