import { access, chmod, lstat, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { ModelAnalysisJobV1Schema, type ModelAnalysisJobV1 } from "@wearable/contracts";
import {
  LocalProcessStudioModelWorker,
  StudioModelWorkerFailureError,
  StudioModelWorkerTimeoutError,
  type StudioTrustedWorkerContext,
} from "@wearable/mobile-agent";
import { afterEach, describe, expect, it } from "vitest";

import { PinnedWorkerRuntimeDiagnosticV1Schema } from "../src/pinned-worker-runtime-diagnostic";
import {
  buildPinnedWorkerRuntimeProbeReport,
  generatePinnedWorkerRuntimeProbeMedia,
  runPinnedWorkerRuntimeProbeCommand,
} from "../src/pinned-worker-runtime-probe";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const workerRoot = join(repositoryRoot, "tools", "media-model-worker");
const pythonPath = join(workerRoot, ".venv", "bin", "python");
const formalInputSha256 = "1ca0727bf07eb75b2d54a7060fb06b7e86afca54cdb763f47883d292b89b873a";
const roots: string[] = [];

describe("formal probe stage telemetry integration", () => {
  afterEach(async () => {
    await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
  });

  it("persists a sanitized model-generation timeout from the real Python transport and reaps it", async () => {
    const root = await privateRoot();
    const home = join(root, "home");
    const temporary = join(root, "tmp");
    await mkdir(home, { mode: 0o700 });
    await mkdir(temporary, { mode: 0o700 });

    const media = await generatePinnedWorkerRuntimeProbeMedia(root);
    const pidPath = join(root, "worker.pid");
    const fixturePath = join(root, "worker-fixture.py");
    await writeFile(fixturePath, pythonFixture(pidPath, "hang"), { mode: 0o600 });

    const worker = new LocalProcessStudioModelWorker({
      command: await realpath(pythonPath),
      args: [
        fixturePath,
        "--model-lock-path", join(root, "must-not-open-models.lock.json"),
        "--model-root", join(root, "must-not-open-model-root"),
        "--qwen-checkpoint-path", join(root, "must-not-open-model.safetensors"),
        "--qwen-artifact-id", "qwen3-vl-2b-instruct-4bit",
        "--worker-runtime-root", join(root, "must-not-open-runtime"),
        "--worker-runtime-artifact-id", "synthetic-worker-runtime",
        "--worker-runtime-tree-sha256", digest("4"),
      ],
      cwd: workerRoot,
      env: {
        PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
        HOME: home,
        LANG: "en_US.UTF-8",
        LC_ALL: "en_US.UTF-8",
        TMPDIR: temporary,
        HF_HUB_OFFLINE: "1",
        TRANSFORMERS_OFFLINE: "1",
      },
      timeoutMs: 2_000,
      maxStdoutBytes: 64 * 1024,
      maxStderrBytes: 64,
      allowedMediaRoot: root,
      stageTelemetry: {
        protocol: "studio-worker-stage-telemetry-v1",
        maxBytes: 4096,
      },
    });
    const job = modelJob(media.path, media.sha256);
    const trustedContext: StudioTrustedWorkerContext = Object.freeze({
      customerId: job.customerId,
      sessionId: job.sessionId,
      sourceAssetId: job.sourceAssetId,
      sourceDigest: job.sourceDigest,
      sourceStartMs: job.sourceStartMs,
      sourceEndMs: job.sourceEndMs,
      transcriptSpans: Object.freeze([]),
      demand: null,
    });
    const successPath = join(root, "success.json");
    const diagnosticPath = join(root, "diagnostic.json");

    await expect(runPinnedWorkerRuntimeProbeCommand({
      lockPath: join(root, "must-not-open-models.lock.json"),
      modelRoot: join(root, "must-not-open-model-root"),
      runtimeId: "synthetic-worker-runtime",
      outputPath: successPath,
      diagnosticOutputPath: diagnosticPath,
    }, {
      resolveBindings: async () => Object.freeze({
        runtime: Object.freeze({
          id: "synthetic-worker-runtime",
          version: "fixture-v1",
          treeSha256: digest("4"),
        }),
        model: Object.freeze({
          id: "qwen3-vl-2b-instruct-4bit",
          version: "fixture-v1",
          sha256: digest("7"),
        }),
      }),
      runProbe: async () => {
        await worker.run(
          job,
          AbortSignal.timeout(10_000),
          trustedContext,
        );
        throw new Error("fixture-worker-returned");
      },
    })).rejects.toBeInstanceOf(StudioModelWorkerTimeoutError);

    await expect(access(successPath)).rejects.toMatchObject({ code: "ENOENT" });
    const diagnosticText = await readFile(diagnosticPath, "utf8");
    const diagnostic = PinnedWorkerRuntimeDiagnosticV1Schema.parse(JSON.parse(diagnosticText));
    expect(diagnostic).toMatchObject({
      status: "timeout",
      safeFailureCategory: "model-timeout",
      lastStage: "model-generation",
      runtimeId: "synthetic-worker-runtime",
      modelId: "qwen3-vl-2b-instruct-4bit",
      inputSha256: formalInputSha256,
      syntheticInput: true,
      customerContentUsed: false,
    });
    expect(diagnostic.completedStageMs).toEqual(expect.objectContaining({
      "runtime-preaccept": expect.any(Number),
      "frontstage-extraction": expect.any(Number),
      "model-load": expect.any(Number),
      "processor-prepare": expect.any(Number),
    }));
    expect(diagnostic.activeStageElapsedMs).toBeGreaterThan(0);
    expect(diagnosticText).not.toContain(root);
    expect(diagnosticText).not.toMatch(/"(?:prompt|stdout|stderr|exception|customerId|sessionId|jobId)"/u);

    const workerPid = Number.parseInt(await readFile(pidPath, "utf8"), 10);
    expect(Number.isSafeInteger(workerPid)).toBe(true);
    await expectProcessGone(workerPid);
    await expect(access(join(root, "must-not-open-models.lock.json")))
      .rejects.toMatchObject({ code: "ENOENT" });
    await expect(access(join(root, "must-not-open-model.safetensors")))
      .rejects.toMatchObject({ code: "ENOENT" });
  }, 15_000);

  it("persists a failed diagnostic when real vision instrumentation returns an internal timeout", async () => {
    const fixture = await integrationFixture("internal-timeout");

    await expect(runPinnedWorkerRuntimeProbeCommand(fixture.command, {
      resolveBindings: async () => fixture.bindings,
      runProbe: async () => {
        await fixture.worker.run(
          fixture.job,
          AbortSignal.timeout(10_000),
          fixture.trustedContext,
        );
        throw new Error("fixture-worker-returned");
      },
    })).rejects.toBeInstanceOf(StudioModelWorkerFailureError);

    const diagnostic = PinnedWorkerRuntimeDiagnosticV1Schema.parse(JSON.parse(
      await readFile(fixture.command.diagnosticOutputPath, "utf8"),
    ));
    expect(diagnostic).toMatchObject({
      status: "failed",
      safeFailureCategory: "model-timeout",
      lastStage: "model-generation",
    });
    await expect(access(fixture.command.outputPath)).rejects.toMatchObject({ code: "ENOENT" });
    await expectProcessGone(Number.parseInt(await readFile(fixture.pidPath, "utf8"), 10));
  }, 15_000);

  it("persists only a mode-0600 number category when Python rejects a huge JSON integer", async () => {
    const fixture = await integrationFixture("huge-integer");

    await expect(runPinnedWorkerRuntimeProbeCommand(fixture.command, {
      resolveBindings: async () => fixture.bindings,
      runProbe: async () => {
        await fixture.worker.run(
          fixture.job,
          AbortSignal.timeout(10_000),
          fixture.trustedContext,
        );
        throw new Error("fixture-worker-returned");
      },
    })).rejects.toBeInstanceOf(StudioModelWorkerFailureError);

    const diagnosticText = await readFile(fixture.command.diagnosticOutputPath, "utf8");
    const diagnostic = PinnedWorkerRuntimeDiagnosticV1Schema.parse(JSON.parse(diagnosticText));
    expect(diagnostic).toMatchObject({
      status: "failed",
      safeFailureCategory: "structured-output-number",
      lastStage: "output-validation",
    });
    expect(diagnostic).not.toHaveProperty("structuredOutputFailureDetail");
    expect(diagnosticText).not.toContain("9999999999999999");
    expect(diagnosticText).not.toContain("studio-vision-output-invalid-number");
    expect(diagnosticText).not.toContain(fixture.root);
    const details = await lstat(fixture.command.diagnosticOutputPath);
    expect(details.mode & 0o777).toBe(0o600);
    expect(details.nlink).toBe(1);
    await expect(access(fixture.command.outputPath)).rejects.toMatchObject({ code: "ENOENT" });
    await expectProcessGone(Number.parseInt(await readFile(fixture.pidPath, "utf8"), 10));
  }, 15_000);

  it("builds the formal report from a real publicized receipt and rejects a wrong public digest", async () => {
    const fixture = await integrationFixture("success");
    const receipt = await fixture.worker.run(
      fixture.job,
      AbortSignal.timeout(10_000),
      fixture.trustedContext,
    );
    const reportInput = {
      runtime: fixture.bindings.runtime,
      model: fixture.bindings.model,
      job: fixture.job,
      receipt,
      inputSha256: formalInputSha256,
      maximumResidentSetBytes: 1,
      syntheticInput: true,
      customerContentUsed: false,
      realModel: true,
      formalWorkerPath: true,
      networkPolicyEnforced: true,
      networkConnectionObserved: false,
      publishedClaimed: false,
    } as const;

    expect(buildPinnedWorkerRuntimeProbeReport(reportInput)).toMatchObject({
      inputSha256: formalInputSha256,
      observationCount: 1,
      observationKinds: ["visible-product"],
    });
    expect(() => buildPinnedWorkerRuntimeProbeReport({
      ...reportInput,
      receipt: { ...receipt, inputDigest: digest("0") },
    })).toThrow("pinned-worker-runtime-probe-input-binding-mismatch");
    await expectProcessGone(Number.parseInt(await readFile(fixture.pidPath, "utf8"), 10));
  }, 15_000);
});

function pythonFixture(
  pidPath: string,
  outcome: "hang" | "internal-timeout" | "huge-integer" | "success",
): string {
  return [
    "import functools",
    "import hashlib",
    "import os",
    "import sys",
    "import time",
    "from types import SimpleNamespace",
    `sys.path.insert(0, ${JSON.stringify(workerRoot)})`,
    "import worker",
    "import vision",
    "from runtime_integrity import RuntimeExpectation",
    "class FakeRegistry:",
    "    def __init__(self, *_arguments): pass",
    "    def resolve_ready_artifact(self, artifact_id, path, **_kwargs):",
    "        return {'id': artifact_id, 'version': 'fixture-v1', 'sha256': '7' * 64, 'path': path}",
    "def fake_validate_runtime(expectation):",
    "    return SimpleNamespace(artifact_id=expectation.artifact_id, version='fixture-v1', root=expectation.runtime_root, tree_sha256=expectation.tree_sha256)",
    "def fake_preaccept(arguments):",
    "    return RuntimeExpectation(registry=FakeRegistry(), artifact_id=arguments.worker_runtime_artifact_id, runtime_root=arguments.worker_runtime_root, tree_sha256=arguments.worker_runtime_tree_sha256, model_sha256='7' * 64)",
    "def fake_context(job):",
    "    pixels = b'frame'",
    "    return {'sourceAssetId': job['sourceAssetId'], 'sourceDigest': job['sourceDigest'], 'sourceStartMs': job['sourceStartMs'], 'sourceEndMs': job['sourceEndMs'], 'frames': [{'sourceDigest': job['sourceDigest'], 'startMs': 0, 'endMs': 2000, 'representativeFrameMs': 1000, 'representativeFrameDigest': hashlib.sha256(pixels).hexdigest(), 'width': 2, 'height': 2, 'pixels': pixels}], 'transcriptSpans': [], 'demand': None}",
    "class FakeModel:",
    "    def generate(self, *, prompt, frames, max_tokens, temperature, timeout_seconds, stage_boundary):",
    "        stage_boundary()",
    `        with open(${JSON.stringify(pidPath)}, 'w', encoding='utf-8') as target:`,
    "            target.write(str(os.getpid()))",
    "            target.flush()",
    "            os.fsync(target.fileno())",
    ...(outcome === "hang"
      ? [
          "        while True:",
          "            time.sleep(60)",
        ]
      : outcome === "internal-timeout"
        ? ["        raise vision.VisionAnalysisError('studio-vision-model-timeout')"]
        : outcome === "huge-integer"
          ? [
              "        return '{\"suggestedTags\":[],\"visualObservations\":[],\"demandEvidenceCandidates\":[],\"highlightCandidates\":[],\"forbiddenContentWarnings\":[],\"uncertainty\":[],\"confidence\":' + ('9' * 5000) + '}'",
            ]
          : [
              `        return ${JSON.stringify(JSON.stringify({
                suggestedTags: ["synthetic"],
                visualObservations: [{
                  kind: "visible-product",
                  text: "synthetic visible product",
                  frameId: "frame-1",
                  confidence: 0.75,
                }],
                demandEvidenceCandidates: [],
                highlightCandidates: [],
                forbiddenContentWarnings: [],
                uncertainty: [],
                confidence: 0.75,
              }))}`,
            ]),
    "def fake_model_loader(*_arguments): return FakeModel()",
    "vision.ArtifactRegistry = FakeRegistry",
    "vision.validate_runtime_process = fake_validate_runtime",
    "worker._preaccept_formal_vision = fake_preaccept",
    "worker.trusted_context_provider = fake_context",
    "worker.analyze_vision = functools.partial(vision.analyze_vision, context_provider=fake_context, model_loader=fake_model_loader, inference_timeout_seconds=60)",
    "raise SystemExit(worker.main())",
    "",
  ].join("\n");
}

async function integrationFixture(
  outcome: "hang" | "internal-timeout" | "huge-integer" | "success",
) {
  const root = await privateRoot();
  const home = join(root, "home");
  const temporary = join(root, "tmp");
  await mkdir(home, { mode: 0o700 });
  await mkdir(temporary, { mode: 0o700 });
  const media = await generatePinnedWorkerRuntimeProbeMedia(root);
  const pidPath = join(root, "worker.pid");
  const fixturePath = join(root, "worker-fixture.py");
  await writeFile(fixturePath, pythonFixture(pidPath, outcome), { mode: 0o600 });
  const worker = new LocalProcessStudioModelWorker({
    command: await realpath(pythonPath),
    args: [
      fixturePath,
      "--model-lock-path", join(root, "must-not-open-models.lock.json"),
      "--model-root", join(root, "must-not-open-model-root"),
      "--qwen-checkpoint-path", join(root, "must-not-open-model.safetensors"),
      "--qwen-artifact-id", "qwen3-vl-2b-instruct-4bit",
      "--worker-runtime-root", join(root, "must-not-open-runtime"),
      "--worker-runtime-artifact-id", "synthetic-worker-runtime",
      "--worker-runtime-tree-sha256", digest("4"),
    ],
    cwd: workerRoot,
    env: {
      PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
      HOME: home,
      LANG: "en_US.UTF-8",
      LC_ALL: "en_US.UTF-8",
      TMPDIR: temporary,
      HF_HUB_OFFLINE: "1",
      TRANSFORMERS_OFFLINE: "1",
    },
    timeoutMs: outcome === "hang" ? 2_000 : 5_000,
    maxStdoutBytes: 64 * 1024,
    maxStderrBytes: 64,
    allowedMediaRoot: root,
    stageTelemetry: { protocol: "studio-worker-stage-telemetry-v1", maxBytes: 4096 },
  });
  const job = modelJob(media.path, media.sha256);
  const trustedContext: StudioTrustedWorkerContext = Object.freeze({
    customerId: job.customerId,
    sessionId: job.sessionId,
    sourceAssetId: job.sourceAssetId,
    sourceDigest: job.sourceDigest,
    sourceStartMs: job.sourceStartMs,
    sourceEndMs: job.sourceEndMs,
    transcriptSpans: Object.freeze([]),
    demand: null,
  });
  return {
    root,
    worker,
    job,
    trustedContext,
    pidPath,
    command: {
      lockPath: join(root, "must-not-open-models.lock.json"),
      modelRoot: join(root, "must-not-open-model-root"),
      runtimeId: "synthetic-worker-runtime",
      outputPath: join(root, "success.json"),
      diagnosticOutputPath: join(root, "diagnostic.json"),
    },
    bindings: Object.freeze({
      runtime: Object.freeze({ id: "synthetic-worker-runtime", version: "fixture-v1", treeSha256: digest("4") }),
      model: Object.freeze({ id: "qwen3-vl-2b-instruct-4bit", version: "fixture-v1", sha256: digest("7") }),
    }),
  };
}

function modelJob(sourcePath: string, sourceDigest: string): ModelAnalysisJobV1 {
  return ModelAnalysisJobV1Schema.parse({
    schemaVersion: 1,
    jobId: "formal-synthetic-qwen-probe",
    customerId: "synthetic-probe-customer",
    sessionId: "synthetic-probe-session",
    sourceAssetId: "synthetic-programmatic-workbench",
    sourceDigest,
    sourcePath,
    sourceStartMs: 0,
    sourceEndMs: 2_000,
    layer: "vision",
    operationKey: "formal-synthetic-qwen-probe-v1",
    requestedAt: "2026-09-23T00:00:00.000Z",
  });
}

async function privateRoot(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "pinned-worker-telemetry-integration-"));
  roots.push(root);
  await chmod(root, 0o700);
  return root;
}

async function expectProcessGone(pid: number): Promise<void> {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      process.kill(pid, 0);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ESRCH") return;
      throw error;
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 25));
  }
  throw new Error("fixture-worker-process-still-alive");
}

function digest(character: string): string {
  return character.repeat(64);
}
