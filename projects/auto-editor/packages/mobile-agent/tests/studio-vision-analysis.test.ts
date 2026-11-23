import { createHash } from "node:crypto";
import { execFile, spawn } from "node:child_process";
import { access, chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { afterEach, describe, expect, it } from "vitest";

import { ModelAnalysisReceiptV1Schema } from "@wearable/contracts";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const workerRoot = join(repositoryRoot, "tools/media-model-worker");
const python = join(workerRoot, ".venv/bin/python");
const execFileAsync = promisify(execFile);
const digest = (value: Buffer | string) => createHash("sha256").update(value).digest("hex");

describe("studio vision worker process", () => {
  const cleanup: string[] = [];

  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("fails a non-manifest Qwen runtime path before reading media and reports missing probe truth", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-vision-unapproved-"));
    cleanup.push(root);
    const missingSource = join(root, "must-not-be-read.mp4");
    const result = await runWorker([
      "--model-lock-path", join(repositoryRoot, "models.lock.json"),
      "--model-root", root,
      "--mlx-vlm-artifact-id", "mlx-vlm-0.6.6",
      "--mlx-vlm-package-path", join(root, "packages/mlx-vlm-0.6.6.tar.gz"),
      "--qwen-artifact-id", "qwen3-vl-2b-instruct-4bit",
      "--qwen-checkpoint-path", join(root, "qwen3-vl-2b-instruct-4bit/model.safetensors"),
    ], [job(missingSource, "0".repeat(64), 1)]);

    expect(result.code).toBe(0);
    expect(result.stderr).toBe("");
    expect(result.receipts).toHaveLength(1);
    expect(result.receipts[0]).toMatchObject({
      status: "failed-manual-review",
      failure: { code: "studio-model-artifact-path-outside-manifest" },
    });
    const checked = await execFileAsync(process.execPath, [
      join(repositoryRoot, "scripts/check-studio-models.mjs"),
      "--json", "--artifact", "qwen3-vl-2b-instruct-4bit", "--root", root,
    ]);
    expect(JSON.parse(checked.stdout).visionProbe).toBe("not-run: runtime-not-installed");
    await expect(access(missingSource)).rejects.toThrow();
  });

  it("flushes scheduled restart evidence even when job 50 is unapproved or invalid", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-vision-failed-restart-"));
    cleanup.push(root);
    const missingSource = join(root, "must-not-be-read.mp4");
    const unapprovedArguments = [
      "--model-lock-path", join(repositoryRoot, "models.lock.json"),
      "--model-root", root,
      "--mlx-vlm-artifact-id", "mlx-vlm-0.6.6",
      "--mlx-vlm-package-path", join(root, "runtime"),
      "--qwen-artifact-id", "qwen3-vl-2b-instruct-4bit",
      "--qwen-checkpoint-path", join(root, "qwen/model.safetensors"),
    ];
    const unapproved = await runWorker(
      unapprovedArguments,
      Array.from({ length: 51 }, (_, index) => job(missingSource, "0".repeat(64), index + 1)),
    );
    expect(unapproved.code).toBe(0);
    expect(unapproved.receipts).toHaveLength(50);
    expect(unapproved.receipts.at(-1)).toMatchObject({
      status: "failed-manual-review",
      qualitySignals: { workerRestart: { reason: "scheduled-job-limit", required: true } },
    });

    for (const expected of [
      ["invalid-json-at-50", "failed-manual-review", "studio-vision-output-invalid-json"],
      ["timeout-at-50", "failed-retryable", "studio-vision-model-timeout"],
      [
        "manual-at-50",
        "failed-manual-review",
        "studio-vision-output-schema-top-level-object-keys",
      ],
    ] as const) {
      const fixture = await visionFixture();
      cleanup.push(fixture.directory);
      const failed = await runWorker(
        fixture.arguments,
        Array.from({ length: 51 }, (_, index) => job(
          fixture.source,
          fixture.sourceDigest,
          index + 1,
        )),
        fixture.pythonPath,
        { STUDIO_FAKE_VISION_MODE: expected[0] },
      );
      expect(failed.code).toBe(0);
      expect(failed.receipts).toHaveLength(50);
      expect(failed.receipts.at(-1)).toMatchObject({
        status: expected[1],
        failure: { code: expected[2] },
        qualitySignals: { workerRestart: { reason: "scheduled-job-limit", required: true } },
      });
    }
  });

  it("flushes invalid numeric evidence and exits immediately", async () => {
    const fixture = await visionFixture();
    cleanup.push(fixture.directory);
    const result = await runWorker(
      fixture.arguments,
      [
        job(fixture.source, fixture.sourceDigest, 1),
        job(fixture.source, fixture.sourceDigest, 2),
      ],
      fixture.pythonPath,
      { STUDIO_FAKE_VISION_MODE: "invalid-numeric-immediate" },
    );
    expect(result.code).toBe(0);
    expect(result.receipts).toHaveLength(1);
    expect(result.receipts[0]).toMatchObject({
      status: "failed-manual-review",
      failure: { code: "studio-vision-output-invalid-number" },
      qualitySignals: { workerRestart: { reason: "invalid-numeric-output", required: true } },
    });
  });

  it("aggregates runtime and weight readiness even for single-artifact checks", async () => {
    const unapprovedRuntime = await visionFixture();
    cleanup.push(unapprovedRuntime.directory);
    const lock = JSON.parse(await readFile(unapprovedRuntime.lockPath, "utf8"));
    lock.artifacts[0].approved = false;
    lock.artifacts[0].enabled = false;
    await writeFile(unapprovedRuntime.lockPath, JSON.stringify(lock));
    const selected = await execFileAsync(process.execPath, [
      join(repositoryRoot, "scripts/check-studio-models.mjs"),
      "--json", "--artifact", "synthetic-qwen", "--lock", unapprovedRuntime.lockPath,
      "--root", unapprovedRuntime.modelRoot,
    ]);
    expect(JSON.parse(selected.stdout)).toMatchObject({
      artifacts: [{ id: "synthetic-qwen", status: "ready" }],
      visionProbe: "not-run: runtime-unapproved",
    });

    const changedRuntime = await visionFixture();
    cleanup.push(changedRuntime.directory);
    await writeFile(changedRuntime.runtimeFile, "tampered-runtime");
    const mismatched = await execFileAsync(process.execPath, [
      join(repositoryRoot, "scripts/check-studio-models.mjs"),
      "--json", "--artifact", "synthetic-qwen", "--lock", changedRuntime.lockPath,
      "--root", changedRuntime.modelRoot,
    ]);
    expect(JSON.parse(mismatched.stdout)).toMatchObject({
      artifacts: [{ id: "synthetic-qwen", status: "ready" }],
      visionProbe: "not-run: runtime-digest-mismatch",
    });
  });

  it("flushes job 50 restart evidence and really exits before consuming job 51", async () => {
    const fixture = await visionFixture();
    cleanup.push(fixture.directory);
    const jobs = Array.from({ length: 51 }, (_, index) => job(
      fixture.source,
      fixture.sourceDigest,
      index + 1,
    ));
    const result = await runWorker(fixture.arguments, jobs, fixture.pythonPath);

    expect(result.code).toBe(0);
    expect(result.stderr).toBe("");
    expect(result.receipts).toHaveLength(50);
    expect(result.receipts.every((receipt) => ModelAnalysisReceiptV1Schema.safeParse(receipt).success))
      .toBe(true);
    expect(result.receipts.every((receipt) => receipt.synthetic === true)).toBe(true);
    expect(result.receipts.at(-1)).toMatchObject({
      status: "succeeded",
      qualitySignals: {
        workerRestart: {
          required: true,
          reason: "scheduled-job-limit",
          completedVisionJobs: 50,
        },
      },
    });
    expect(result.receipts.some((receipt) => receipt.jobId === "job-vision-51")).toBe(false);

    const restarted = await runWorker(
      fixture.arguments,
      Array.from({ length: 50 }, (_, index) => job(
        fixture.source,
        fixture.sourceDigest,
        index + 51,
      )),
      fixture.pythonPath,
    );
    expect(restarted.code).toBe(0);
    expect(restarted.receipts).toHaveLength(50);
    expect(restarted.receipts.every((receipt) => ModelAnalysisReceiptV1Schema.safeParse(receipt).success))
      .toBe(true);
    expect(restarted.receipts.at(-1)).toMatchObject({
      jobId: "job-vision-100",
      qualitySignals: {
        workerRestart: {
          required: true,
          reason: "scheduled-job-limit",
          completedVisionJobs: 50,
        },
      },
    });
  }, 60_000);
});

async function visionFixture() {
  const directory = await mkdtemp(join(tmpdir(), "studio-vision-process-"));
  const modelRoot = join(directory, "models");
  const checkpointDirectory = join(modelRoot, "qwen");
  const runtimeDirectory = join(modelRoot, "runtime");
  const runtimePackage = join(runtimeDirectory, "mlx_vlm");
  const runtimeDistribution = join(runtimeDirectory, "mlx_vlm-0.6.6.dist-info");
  const modules = join(directory, "modules");
  await mkdir(checkpointDirectory, { recursive: true });
  await mkdir(runtimePackage, { recursive: true });
  await mkdir(runtimeDistribution, { recursive: true });
  await mkdir(modules, { recursive: true });
  const checkpoint = join(checkpointDirectory, "model.safetensors");
  const config = join(checkpointDirectory, "config.json");
  const runtime = join(runtimePackage, "__init__.py");
  const runtimeStructured = join(runtimePackage, "structured.py");
  const runtimeMetadata = join(runtimeDistribution, "METADATA");
  await writeFile(checkpoint, "synthetic-qwen-weights");
  await writeFile(config, "synthetic-config");
  const fakeCv2 = [
    "import hashlib",
    "CAP_PROP_POS_MSEC = 0",
    "COLOR_BGR2RGB = 1",
    "INTER_AREA = 0",
    "class Pixels:",
    "    def __init__(self, shape=(2, 2, 3)): self.shape = shape",
    "    def tobytes(self): return b'frame'",
    "    def transpose(self, *axes): return Pixels(tuple(self.shape[index] for index in axes))",
    "    def copy(self): return Pixels(self.shape)",
    "class VideoCapture:",
    "    def __init__(self, path): self.path = path",
    "    def isOpened(self): return True",
    "    def set(self, key, value): return True",
    "    def read(self): return True, Pixels()",
    "    def release(self): pass",
    "def resize(value, size, interpolation=0): return value",
    "def cvtColor(value, code): return value",
  ].join("\n");
  await writeFile(join(modules, "cv2.py"), `${fakeCv2}\n`);
  const fakeSceneDetect = [
    "class Time:",
    "    def __init__(self, seconds=0): self.seconds = seconds",
    "    def __add__(self, frames): return Time(frames / 10)",
    "class Video:",
    "    frame_rate = 10",
    "    base_timecode = Time(0)",
    "    def seek(self, value): pass",
    "class AdaptiveDetector:",
    "    def __init__(self, min_scene_len): pass",
    "class SceneManager:",
    "    def add_detector(self, value): pass",
    "    def detect_scenes(self, video, end_time): pass",
    "    def get_scene_list(self, start_in_scene=True): return []",
    "def open_video(path): return Video()",
  ].join("\n");
  await writeFile(join(modules, "scenedetect.py"), `${fakeSceneDetect}\n`);
  const fakeMlx = [
    "import json",
    "import os",
    "import signal",
    "__version__ = '0.6.6'",
    "calls = 0",
    "class Processor:",
    "    def apply_chat_template(self, messages, **kwargs): return '<image>' + messages[0]['content'][-1]['text']",
    "def load(path): return object(), Processor()",
    "def generate(model, processor, prompt, image, **kwargs):",
    "    global calls",
    "    if any(frame.shape != (3, 2, 2) for frame in image): raise TypeError('expected-chw-frame-copy')",
    "    calls += 1",
    "    mode = os.environ.get('STUDIO_FAKE_VISION_MODE')",
    "    if mode == 'invalid-json-at-50' and calls == 50: return 'not-json'",
    "    if mode == 'timeout-at-50' and calls == 50: os.kill(os.getpid(), signal.SIGALRM)",
    "    if mode == 'manual-at-50' and calls == 50: return '{\"unexpected\":true}'",
    "    if mode == 'invalid-numeric-immediate': return '{\"suggestedTags\":[],\"visualObservations\":[],\"demandEvidenceCandidates\":[],\"highlightCandidates\":[],\"forbiddenContentWarnings\":[],\"uncertainty\":[],\"confidence\":NaN}'",
    "    return json.dumps({",
    "      'suggestedTags': ['synthetic'],",
    "      'visualObservations': [{'kind':'visible-scene','text':'synthetic frame','frameId':'frame-1','confidence':0.8}],",
    "      'demandEvidenceCandidates': [],",
    "      'highlightCandidates': [],",
    "      'forbiddenContentWarnings': [],",
    "      'uncertainty': ['fixture only'],",
    "      'confidence': 0.5,",
    "    })",
  ].join("\n");
  await writeFile(runtime, `${fakeMlx}\n`);
  await writeFile(runtimeStructured, [
    "def build_json_schema_logits_processor(tokenizer, schema):",
    "    return lambda input_ids, logits: logits",
    "",
  ].join("\n"));
  await writeFile(runtimeMetadata, "Name: mlx-vlm\nVersion: 0.6.6\n");
  const source = join(directory, "source.mp4");
  await writeFile(source, "synthetic-source");
  const artifactId = "synthetic-qwen";
  const revision = "a".repeat(40);
  const sourceUrl = "https://models.invalid/qwen";
  const files = await Promise.all([config, checkpoint].map(async (path) => {
    const bytes = await readFile(path);
    const relativePath = path.slice(modelRoot.length + 1);
    return {
      relativePath,
      sourceUrl: `${sourceUrl}/resolve/${revision}/${relativePath}`,
      required: true,
      executable: false,
      sha256: digest(bytes),
      byteLength: bytes.byteLength,
    };
  }));
  const lockPath = join(directory, "models.lock.json");
  const runtimeRevision = "b".repeat(40);
  const runtimeUrl = "https://code.invalid/mlx-vlm";
  const runtimeFiles = await Promise.all([
    runtime,
    runtimeStructured,
    runtimeMetadata,
  ].map(async (path) => {
    const bytes = await readFile(path);
    return {
      relativePath: path.slice(modelRoot.length + 1),
      sourceUrl: `${runtimeUrl}/resolve/${runtimeRevision}/${path.slice(runtimeDirectory.length + 1)}`,
      required: true,
      executable: false,
      sha256: digest(bytes),
      byteLength: bytes.byteLength,
    };
  }));
  await writeFile(lockPath, JSON.stringify({
    schemaVersion: 1,
    artifacts: [{
      id: "synthetic-mlx-vlm",
      version: "0.6.6",
      artifactKind: "source-package",
      sources: [{ id: "code", kind: "code", sourceUrl: runtimeUrl,
        sourceRevision: runtimeRevision, inventoryVerified: true, files: runtimeFiles }],
      licenseEvidence: {
        code: { applies: true, sourceId: "code", sourceRevision: runtimeRevision,
          spdxExpression: "MIT", evidenceUrl: `${runtimeUrl}/raw/${runtimeRevision}/LICENSE` },
        weights: { applies: false, sourceId: null, sourceRevision: null,
          spdxExpression: null, evidenceUrl: null },
      },
      licenseReview: "approved",
      runtimeBackend: "synthetic-mlx-vlm",
      quantization: null,
      enabled: true,
      approved: true,
      approvedBy: "fixture-reviewer",
      approvedAt: "2026-09-12T09:00:00.000Z",
      testedDevices: ["synthetic"],
      testSetVersion: "fixture-v1",
    }, {
      id: artifactId,
      version: "0.6.6-fixture",
      artifactKind: "checkpoint",
      sources: [{ id: "weights", kind: "weights", sourceUrl, sourceRevision: revision,
        inventoryVerified: true, files }],
      licenseEvidence: {
        code: { applies: false, sourceId: null, sourceRevision: null, spdxExpression: null, evidenceUrl: null },
        weights: { applies: true, sourceId: "weights", sourceRevision: revision, spdxExpression: "MIT",
          evidenceUrl: `${sourceUrl}/raw/${revision}/LICENSE` },
      },
      licenseReview: "approved",
      runtimeBackend: "synthetic-mlx-vlm",
      quantization: "4bit",
      enabled: true,
      approved: true,
      approvedBy: "fixture-reviewer",
      approvedAt: "2026-09-12T09:00:00.000Z",
      testedDevices: ["synthetic"],
      testSetVersion: "fixture-v1",
    }],
  }));
  await chmod(checkpoint, 0o600);
  await chmod(config, 0o600);
  return {
    directory,
    source,
    sourceDigest: digest("synthetic-source"),
    arguments: [
      "--model-lock-path", lockPath,
      "--model-root", modelRoot,
      "--mlx-vlm-artifact-id", "synthetic-mlx-vlm",
      "--mlx-vlm-package-path", runtimeDirectory,
      "--qwen-artifact-id", artifactId,
      "--qwen-checkpoint-path", checkpoint,
    ],
    pythonPath: modules,
    lockPath,
    modelRoot,
    runtimeFile: runtime,
  };
}

function job(sourcePath: string, sourceDigest: string, position: number) {
  return {
    schemaVersion: 1,
    jobId: `job-vision-${position}`,
    customerId: "customer-a",
    sessionId: "session-a",
    sourceAssetId: "asset-a",
    sourceDigest,
    sourcePath,
    sourceStartMs: 1_000,
    sourceEndMs: 2_000,
    layer: "vision",
    operationKey: `vision-process-${position}`,
    requestedAt: "2026-09-12T09:00:00.000Z",
  };
}

async function runWorker(
  arguments_: string[],
  jobs: unknown[],
  pythonPath?: string,
  environment: NodeJS.ProcessEnv = {},
) {
  const child = spawn(python, [join(workerRoot, "worker.py"), ...arguments_], {
    cwd: workerRoot,
    env: {
      ...process.env,
      ...environment,
      ...(pythonPath ? { PYTHONPATH: `${pythonPath}:${workerRoot}` } : {}),
    },
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
  });
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
  child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
  child.stdin.on("error", () => undefined);
  child.stdin.end(jobs.map((value) => `${JSON.stringify(value)}\n`).join(""));
  const code = await new Promise<number | null>((resolveCode, reject) => {
    child.once("error", reject);
    child.once("close", resolveCode);
  });
  const output = Buffer.concat(stdout).toString("utf8");
  return {
    code,
    stderr: Buffer.concat(stderr).toString("utf8"),
    receipts: output.trim() ? output.trimEnd().split("\n").map((line) => JSON.parse(line)) : [],
  };
}
