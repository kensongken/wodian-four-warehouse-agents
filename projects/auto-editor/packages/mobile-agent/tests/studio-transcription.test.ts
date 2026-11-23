import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { access, chmod, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { afterEach, describe, expect, it } from "vitest";

import { LocalProcessStudioModelWorker } from "../src";

const execFileAsync = promisify(execFile);
const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const workerRoot = join(repositoryRoot, "tools/media-model-worker");
const checkerPath = join(repositoryRoot, "scripts/check-studio-models.mjs");
const installerPath = join(repositoryRoot, "scripts/install-studio-models.mjs");
const whisperArtifactId = "whisper.cpp-1.9.2-small-q5_1";
const whisperBinaryArtifactId = "whisper.cpp-1.9.2-cli-darwin-arm64";

const digest = (value: Buffer | string) => createHash("sha256").update(value).digest("hex");

async function executable(root: string, name: string, body: string): Promise<string> {
  const path = join(root, name);
  await writeFile(path, `#!${process.execPath}\n${body}\n`, "utf8");
  await chmod(path, 0o700);
  return path;
}

function approvedRuntimeArtifact(
  id: string,
  version: string,
  kind: "code" | "weights",
  relativePath: string,
  bytes: Buffer,
) {
  const revision = "a".repeat(40);
  const sourceUrl = `https://models.invalid/org/${id}`;
  return {
    id,
    version,
    artifactKind: kind === "code" ? "binary-model" : "checkpoint",
    sources: [{
      id: kind,
      kind,
      sourceUrl,
      sourceRevision: revision,
      inventoryVerified: true,
      files: [{
        relativePath,
        sourceUrl: `${sourceUrl}/raw/${revision}/${relativePath}`,
        required: true,
        executable: kind === "code",
        sha256: digest(bytes),
        byteLength: bytes.byteLength,
      }],
    }],
    licenseEvidence: {
      code: kind === "code"
        ? { applies: true, sourceId: kind, sourceRevision: revision, spdxExpression: "MIT", evidenceUrl: `${sourceUrl}/raw/${revision}/LICENSE` }
        : { applies: false, sourceId: null, sourceRevision: null, spdxExpression: null, evidenceUrl: null },
      weights: kind === "weights"
        ? { applies: true, sourceId: kind, sourceRevision: revision, spdxExpression: "MIT", evidenceUrl: `${sourceUrl}/raw/${revision}/LICENSE` }
        : { applies: false, sourceId: null, sourceRevision: null, spdxExpression: null, evidenceUrl: null },
    },
    licenseReview: "approved",
    runtimeBackend: "synthetic",
    quantization: null,
    enabled: true,
    approved: true,
    approvedBy: "synthetic-reviewer",
    approvedAt: "2026-09-12T06:00:00.000Z",
    testedDevices: [],
    testSetVersion: null,
  };
}

describe("studio transcript worker and model gates", () => {
  const cleanup: string[] = [];

  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("routes a transcript job through fake binaries and removes the extracted wav", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-transcript-worker-"));
    cleanup.push(root);
    const sourcePath = join(root, "名字包含 空格;$(bad).mp4");
    const sourceBytes = Buffer.from("synthetic-media");
    await writeFile(sourcePath, sourceBytes);
    const modelPath = join(root, "model.bin");
    const modelBytes = Buffer.from("synthetic-model");
    await writeFile(modelPath, modelBytes);
    const wavLog = join(root, "wav-path.txt");
    const whisperLog = join(root, "whisper-args.json");
    const ffprobe = await executable(root, "ffprobe", [
      `process.stdout.write(${JSON.stringify(JSON.stringify({
        format: { duration: "3.000" },
        streams: [{ codec_type: "video", codec_name: "h264", width: 160, height: 120 },
          { codec_type: "audio", codec_name: "aac" }],
      }))});`,
    ].join("\n"));
    const ffmpeg = await executable(root, "ffmpeg", [
      "const { writeFileSync } = require('node:fs');",
      "const wav = process.argv.at(-1);",
      "writeFileSync(wav, 'RIFF-fake');",
      `writeFileSync(${JSON.stringify(wavLog)}, wav);`,
    ].join("\n"));
    const whisper = await executable(root, "whisper", [
      "const { writeFileSync } = require('node:fs');",
      "const args = process.argv.slice(2);",
      `writeFileSync(${JSON.stringify(whisperLog)}, JSON.stringify(args));`,
      `const payload = ${JSON.stringify(JSON.stringify({
        result: { language: "zh" },
        transcription: [{ offsets: { from: 120, to: 880 }, text: "介绍设计灵感", no_speech_prob: 0.03 }],
      }))};`,
      "if (args.includes('-np') && args[args.indexOf('-of') + 1] === '-') process.stdout.write(payload);",
      "else writeFileSync(`${args[args.indexOf('-f') + 1]}.json`, payload);",
    ].join("\n"));
    const binaryBytes = await readFile(whisper);
    const binaryArtifactId = "synthetic-whisper-cli";
    const modelArtifactId = "synthetic-whisper-model";
    const lockPath = join(root, "models.lock.json");
    await writeFile(lockPath, JSON.stringify({
      schemaVersion: 1,
      artifacts: [
        approvedRuntimeArtifact(binaryArtifactId, "1.9.2-fixture", "code", "whisper", binaryBytes),
        approvedRuntimeArtifact(modelArtifactId, "small-q5_1-fixture", "weights", "model.bin", modelBytes),
      ],
    }), "utf8");
    const worker = new LocalProcessStudioModelWorker({
      command: "/usr/bin/python3",
      args: [
        "worker.py",
        "--ffprobe-path", ffprobe,
        "--ffmpeg-path", ffmpeg,
        "--whisper-path", whisper,
        "--whisper-model-path", modelPath,
        "--model-lock-path", lockPath,
        "--model-root", root,
        "--whisper-binary-artifact-id", binaryArtifactId,
        "--whisper-model-artifact-id", modelArtifactId,
      ],
      cwd: workerRoot,
      timeoutMs: 10_000,
      maxStdoutBytes: 128 * 1024,
      maxStderrBytes: 4 * 1024,
      allowedMediaRoot: root,
    });

    const receipt = await worker.run({
      schemaVersion: 1,
      jobId: "job-transcript-1",
      customerId: "customer-a",
      sessionId: "session-a",
      sourceAssetId: "asset-a",
      sourceDigest: digest(sourceBytes),
      sourcePath,
      sourceStartMs: 1_000,
      sourceEndMs: 2_000,
      layer: "transcript",
      operationKey: "transcript-worker-v1",
      requestedAt: "2026-09-12T06:00:00.000Z",
    }, AbortSignal.timeout(10_000));

    expect(receipt).toMatchObject({
      status: "succeeded",
      sourcePath: await realpath(sourcePath),
      adapterName: binaryArtifactId,
      modelName: modelArtifactId,
      modelVersion: "small-q5_1-fixture",
      checkpointDigest: digest(modelBytes),
      suggestedTags: [],
      warnings: ["transcript-is-model-suggestion"],
      qualitySignals: {
        detectedLanguage: "zh",
        spans: [{ startMs: 1_120, endMs: 1_880, text: "介绍设计灵感" }],
        artifacts: {
          binary: { id: binaryArtifactId, version: "1.9.2-fixture", sha256: digest(binaryBytes) },
          model: { id: modelArtifactId, version: "small-q5_1-fixture", sha256: digest(modelBytes) },
        },
      },
    });
    await expect(access(await readFile(wavLog, "utf8"))).rejects.toThrow();
    await expect(access(join(root, "bad"))).rejects.toThrow();
    const whisperArguments = JSON.parse(await readFile(whisperLog, "utf8"));
    expect(whisperArguments.slice(-6)).toEqual(["-oj", "-of", "-", "-np", "-l", "auto"]);
  });

  it("reports approved Whisper artifacts missing from an empty root and keeps the checker read-only", async () => {
    const root = join(tmpdir(), `studio-model-check-${Date.now()}-${Math.random()}`);
    const { stdout } = await execFileAsync(process.execPath, [checkerPath, "--json", "--root", root]);
    const report = JSON.parse(stdout) as { artifacts: Array<{ id: string; status: string }> };

    expect(report.artifacts.find((item) => item.id === whisperArtifactId)?.status).toBe("missing");
    expect(report.artifacts.find((item) => item.id === whisperBinaryArtifactId)?.status).toBe("missing");
    expect(report.artifacts.find(
      (item) => item.id === "whisper.cpp-1.9.2-small-q5_0",
    )?.status).toBe("unapproved");
    expect(report.artifacts.some((item) => item.status === "ready")).toBe(false);
    await expect(access(root)).rejects.toThrow();
  });

  it("reports missing, digest-mismatch, and ready from an approved synthetic lock", async () => {
    const fixture = await mkdtemp(join(tmpdir(), "studio-model-status-"));
    cleanup.push(fixture);
    const root = join(fixture, "models");
    const lockPath = join(fixture, "models.lock.json");
    const relativePath = "whisper/model.bin";
    const modelBytes = Buffer.from("approved-synthetic-model");
    await writeFile(lockPath, JSON.stringify({
      schemaVersion: 1,
      artifacts: [{
        id: "synthetic-approved",
        version: "fixture-v1",
        artifactKind: "checkpoint",
        sources: [{
          id: "weights",
          kind: "weights",
          sourceUrl: "https://models.invalid/org/repo",
          sourceRevision: "a".repeat(40),
          inventoryVerified: true,
          files: [{
            relativePath,
            sourceUrl: `https://models.invalid/org/repo/resolve/${"a".repeat(40)}/model.bin`,
            required: true,
            executable: false,
            sha256: digest(modelBytes),
            byteLength: modelBytes.byteLength,
          }],
        }],
        licenseEvidence: {
          code: { applies: false, sourceId: null, sourceRevision: null, spdxExpression: null, evidenceUrl: null },
          weights: {
            applies: true,
            sourceId: "weights",
            sourceRevision: "a".repeat(40),
            spdxExpression: "MIT",
            evidenceUrl: `https://models.invalid/org/repo/raw/${"a".repeat(40)}/LICENSE`,
          },
        },
        licenseReview: "approved",
        runtimeBackend: "synthetic",
        quantization: null,
        enabled: true,
        approved: true,
        approvedBy: "synthetic-reviewer",
        approvedAt: "2026-09-12T06:00:00.000Z",
        testedDevices: [],
        testSetVersion: null,
      }],
    }), "utf8");
    const check = async () => {
      const { stdout } = await execFileAsync(process.execPath, [
        checkerPath, "--json", "--lock", lockPath, "--root", root,
      ]);
      return JSON.parse(stdout) as { artifacts: Array<{ status: string }> };
    };

    expect((await check()).artifacts[0]?.status).toBe("missing");
    await mkdir(dirname(join(root, relativePath)), { recursive: true });
    await writeFile(join(root, relativePath), "wrong");
    expect((await check()).artifacts[0]?.status).toBe("digest-mismatch");
    await writeFile(join(root, relativePath), modelBytes);
    expect((await check()).artifacts[0]?.status).toBe("ready");
  });

  it("requires an explicit approved artifact before the installer can touch disk", async () => {
    const root = join(tmpdir(), `studio-model-install-${Date.now()}-${Math.random()}`);
    await expect(execFileAsync(process.execPath, [installerPath, "--root", root]))
      .rejects.toMatchObject({ stderr: expect.stringContaining("studio-model-installer-explicit-artifact-required") });
    await expect(execFileAsync(process.execPath, [
      installerPath, "--artifact", "whisper.cpp-1.9.2-small-q5_0", "--root", root,
    ])).rejects.toMatchObject({ stderr: expect.stringContaining("studio-model-installer-artifact-unapproved") });
    await expect(access(root)).rejects.toThrow();
  });

  it("refuses an approved-looking lock with placeholder license evidence before download", async () => {
    const fixture = await mkdtemp(join(tmpdir(), "studio-model-license-gate-"));
    cleanup.push(fixture);
    const root = join(fixture, "models");
    const lockPath = join(fixture, "models.lock.json");
    const revision = "b".repeat(40);
    await writeFile(lockPath, JSON.stringify({
      schemaVersion: 1,
      artifacts: [{
        id: "synthetic-placeholder",
        version: "fixture-v1",
        artifactKind: "checkpoint",
        sources: [{
          id: "weights",
          kind: "weights",
          sourceUrl: "https://models.invalid/org/repo",
          sourceRevision: revision,
          inventoryVerified: true,
          files: [{
            relativePath: "model.bin",
            sourceUrl: `https://models.invalid/org/repo/resolve/${revision}/model.bin`,
            required: true,
            executable: false,
            sha256: "c".repeat(64),
            byteLength: 10,
          }],
        }],
        licenseEvidence: {
          code: { applies: false, sourceId: null, sourceRevision: null, spdxExpression: null, evidenceUrl: null },
          weights: {
            applies: true,
            sourceId: "weights",
            sourceRevision: revision,
            spdxExpression: "MIT",
            evidenceUrl: `https://models.invalid/org/repo/raw/${revision}/TODO-LICENSE.txt`,
          },
        },
        licenseReview: "approved",
        runtimeBackend: "synthetic",
        quantization: null,
        enabled: true,
        approved: true,
        approvedBy: "synthetic-reviewer",
        approvedAt: "2026-09-12T06:00:00.000Z",
        testedDevices: [],
        testSetVersion: null,
      }],
    }), "utf8");

    const { stdout } = await execFileAsync(process.execPath, [
      checkerPath, "--json", "--lock", lockPath, "--root", root,
    ]);
    expect(JSON.parse(stdout).artifacts[0]?.status).toBe("unapproved");
    await expect(execFileAsync(process.execPath, [
      installerPath, "--artifact", "synthetic-placeholder", "--lock", lockPath, "--root", root,
    ])).rejects.toMatchObject({
      stderr: expect.stringContaining("studio-model-installer-license-evidence-invalid"),
    });
    await expect(access(root)).rejects.toThrow();
  });
});
