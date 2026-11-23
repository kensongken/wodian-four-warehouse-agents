import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmod,
  mkdtemp,
  mkdir,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  ModelAnalysisReceiptV1Schema,
  type ModelAnalysisJobV1,
  type ModelAnalysisReceiptV1,
} from "@wearable/contracts";
import {
  createFfprobeStudioSourceIntervalResolver,
  CustomerMaterialLibrary,
  StudioAnalysisJobService,
  type StudioModelWorker,
} from "@wearable/mobile-agent";
import ffmpegPath from "ffmpeg-static";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { loadLockedRealMaterialManifest } from "../src/real-material-manifest";
import { prepareRealMaterialRun } from "../src/real-material-preparer";

const ffprobePath = (createRequire(import.meta.url)("ffprobe-static") as { path: string }).path;
const roots: string[] = [];
const NOW = "2026-09-22T08:00:00.000Z";

beforeAll(() => {
  if (!ffmpegPath) throw new Error("test-ffmpeg-unavailable");
});

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("real material preparer", () => {
  it("imports authorized current and historical material, derives stills, and keeps reference media separate", async () => {
    const fixture = await createFixture();

    const prepared = await prepareRealMaterialRun(fixture.prepareInput);

    expect(prepared.assets).toHaveLength(2);
    expect(prepared.privateBindings.map((binding) => binding.inputId)).toEqual([
      "current-video", "historical-image", "reference-video",
    ]);
    expect(prepared.assets.every((asset) =>
      asset.viewPermission === "allowed" && asset.usePermission === "allowed"
    )).toBe(true);
    const current = assetForInput(prepared, "current-video");
    const historical = assetForInput(prepared, "historical-image");
    expect(current.digest).toBe(fixture.digests.current);
    expect(historical.mediaKind).toBe("video");
    expect(historical.digest).not.toBe(fixture.digests.historical);
    expect(bindingForInput(prepared, "historical-image").sourceDigest).toBe(fixture.digests.historical);
    expect(prepared.assets.some((asset) =>
      asset.id === bindingForInput(prepared, "reference-video").assetId
    )).toBe(false);
    expect(prepared.referenceInputId).toBe("reference-video");
    expect(prepared.stories.find((story) => story.storyType === "product-journey"))
      .toMatchObject({ status: "ready", errorCode: null });
    expect(prepared.stories.find((story) => story.storyType === "inner-journey"))
      .toMatchObject({ status: "insufficient-evidence" });
    expect(prepared.capability.layers.find((layer) => layer.layer === "media")?.status).toBe("ready");
    expect(prepared.capability.layers.find((layer) => layer.layer === "scene")?.status).toBe("ready");
    expect(prepared.capability.layers.find((layer) => layer.layer === "transcript")?.status).toBe("blocked");

    const productSegments = prepared.segments.filter((segment) =>
      segment.tags.products.some((product) => product.id === "ring-01")
    );
    expect(productSegments.map((segment) => ({
      startMs: segment.startMs,
      endMs: segment.endMs,
      stages: segment.tags.processStages,
    }))).toEqual(expect.arrayContaining([
      { startMs: 200, endMs: 1_200, stages: ["design"] },
      { startMs: 0, endMs: 4_000, stages: ["finished"] },
    ]));
  }, 120_000);

  it("returns an identical prepared result when the exact manifest is replayed", async () => {
    const fixture = await createFixture();
    const first = await prepareRealMaterialRun(fixture.prepareInput);

    const replay = await prepareRealMaterialRun(fixture.prepareInput);

    expect(replay).toEqual(first);
  }, 120_000);

  it("accepts no confirmed fact after a locked source mutates", async () => {
    const fixture = await createFixture();
    await writeFile(fixture.paths.current, Buffer.from("mutated-after-lock"));

    await expect(prepareRealMaterialRun(fixture.prepareInput))
      .rejects.toThrow("real-material-source-changed");
  });

  it("rejects changed content under the same run operation identity", async () => {
    const fixture = await createFixture();
    await prepareRealMaterialRun(fixture.prepareInput);
    await makeVideo(fixture.paths.current, "0x2255cc", 2);
    const changedDigest = await sha256File(fixture.paths.current);
    const changedManifest = structuredClone(fixture.manifest);
    changedManifest.inputs[0]!.sourceDigest = changedDigest;
    await writeManifest(fixture.manifestPath, changedManifest);
    const changedLock = await loadLockedRealMaterialManifest({
      manifestPath: fixture.manifestPath,
      approvedRoots: [fixture.sourceRoot],
    });

    await expect(prepareRealMaterialRun({ ...fixture.prepareInput, locked: changedLock }))
      .rejects.toThrow("real-material-run-conflict");
  }, 120_000);

  it("returns no absolute source or runtime path in the preparation result", async () => {
    const fixture = await createFixture();

    const prepared = await prepareRealMaterialRun(fixture.prepareInput);
    const serialized = JSON.stringify(prepared);

    expect(serialized).not.toContain(fixture.sourceRoot);
    expect(serialized).not.toContain(fixture.runtimeRoot);
    expect(serialized).not.toContain("sourcePath");
    expect(serialized).not.toMatch(/\/(?:Users|private|var|tmp)\//);
  }, 120_000);
});

async function createFixture() {
  const root = await mkdtemp(join(tmpdir(), "real-material-preparer-"));
  roots.push(root);
  const sourceRoot = join(root, "authorized");
  const runtimeRoot = join(root, "runtime");
  await mkdir(sourceRoot, { recursive: true });
  const paths = {
    current: join(sourceRoot, "current.mp4"),
    historical: join(sourceRoot, "historical.png"),
    reference: join(sourceRoot, "reference.mp4"),
  };
  await makeVideo(paths.current, "0x8833aa", 2, false);
  await makeImage(paths.historical);
  await makeVideo(paths.reference, "0x224466", 3, true);
  const digests = {
    current: await sha256File(paths.current),
    historical: await sha256File(paths.historical),
    reference: await sha256File(paths.reference),
  };
  const manifestPath = join(root, "manifest.json");
  const manifest = manifestFor(paths, digests);
  await writeManifest(manifestPath, manifest);
  const locked = await loadLockedRealMaterialManifest({
    manifestPath,
    approvedRoots: [sourceRoot],
  });
  const library = new CustomerMaterialLibrary(join(runtimeRoot, "library"), () => true);
  const analysisJobs = new StudioAnalysisJobService(library, localProbeWorker(), {
    now: () => NOW,
    resolveSourceInterval: createFfprobeStudioSourceIntervalResolver(ffprobePath),
  });
  return {
    root, sourceRoot, runtimeRoot, paths, digests, manifestPath, manifest,
    prepareInput: { locked, runtimeRoot, library, analysisJobs, now: () => NOW },
  };
}

function manifestFor(
  paths: { current: string; historical: string; reference: string },
  digests: { current: string; historical: string; reference: string },
) {
  const fact = (kind: string, value: string, image = false) => ({
    kind,
    value,
    sourceStartMs: image ? null : 200,
    sourceEndMs: image ? null : 1_200,
    confirmedBy: "designer-01",
    confirmedAt: NOW,
  });
  return {
    schemaVersion: 1 as const,
    runId: "real-run-01",
    customerId: "customer-01",
    actorId: "designer-01",
    createdAt: NOW,
    purpose: "real-material-acceptance" as const,
    inputs: [
      {
        inputId: "current-video", role: "current" as const, mediaKind: "video" as const,
        sourcePath: paths.current, sourceDigest: digests.current, permissionState: "allowed" as const,
        allowedUses: ["story-analysis", "render"] as const, declaredOwnership: "customer-owned" as const,
        confirmedFacts: [fact("product-id", "ring-01"), fact("process-stage", "design")],
      },
      {
        inputId: "historical-image", role: "historical" as const, mediaKind: "image" as const,
        sourcePath: paths.historical, sourceDigest: digests.historical, permissionState: "allowed" as const,
        allowedUses: ["story-analysis", "render", "still-motion"] as const,
        declaredOwnership: "customer-owned" as const,
        confirmedFacts: [fact("product-id", "ring-01", true), fact("process-stage", "finished", true)],
      },
      {
        inputId: "reference-video", role: "reference" as const, mediaKind: "video" as const,
        sourcePath: paths.reference, sourceDigest: digests.reference, permissionState: "allowed" as const,
        allowedUses: ["reference-analysis"] as const, declaredOwnership: "external-style-only" as const,
        confirmedFacts: [],
      },
    ],
    referencePolicy: "external-style-only" as const,
    requestedStoryTypes: ["product-journey", "inner-journey", "persona-monologue"] as const,
    requestedVariants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"] as const,
  };
}

function localProbeWorker(): StudioModelWorker {
  return {
    health: async () => ({
      mode: "local",
      installed: true,
      modelName: "ffmpeg-local-probe",
      checkpointDigest: "d".repeat(64),
    }),
    run: async (job) => realProbeReceipt(job),
  };
}

async function realProbeReceipt(job: ModelAnalysisJobV1): Promise<ModelAnalysisReceiptV1> {
  const output = await run(ffprobePath, [
    "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
    "-of", "json", job.sourcePath,
  ]);
  const probe = JSON.parse(output) as {
    streams?: Array<{ codec_type?: string; width?: number; height?: number }>;
    format?: { duration?: string };
  };
  const durationMs = Math.round(Number(probe.format?.duration) * 1_000);
  const hasVideo = probe.streams?.some((stream) => stream.codec_type === "video") ?? false;
  const hasAudio = probe.streams?.some((stream) => stream.codec_type === "audio") ?? false;
  const { requestedAt: _requestedAt, ...binding } = job;
  return ModelAnalysisReceiptV1Schema.parse({
    ...binding,
    adapterName: job.layer === "technical" ? "ffprobe-local" : "scene-boundary-local",
    modelName: job.layer === "technical" ? "ffprobe" : "deterministic-scene-probe",
    modelVersion: "1",
    checkpointDigest: null,
    inputDigest: job.sourceDigest,
    promptVersion: "real-media-probe-v1",
    observations: [],
    suggestedTags: [],
    qualitySignals: job.layer === "technical"
      ? { durationMs, hasVideo, hasAudio }
      : { scenes: [{ startMs: 0, endMs: durationMs, representativeFrameMs: Math.floor(durationMs / 2) }] },
    confidence: 1,
    warnings: [],
    startedAt: NOW,
    completedAt: NOW,
    runtimeMs: 0,
    status: "succeeded",
    failure: null,
    synthetic: false,
    providerMode: "local",
  });
}

async function makeVideo(path: string, color: string, duration: number, withAudio = false): Promise<void> {
  const args = [
    "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", `color=c=${color}:s=720x1280:r=25:d=${duration}`,
  ];
  if (withAudio) args.push("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-shortest");
  args.push("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p");
  if (withAudio) args.push("-c:a", "aac");
  args.push("-y", path);
  await run(ffmpegPath!, args);
}

async function makeImage(path: string): Promise<void> {
  await run(ffmpegPath!, [
    "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
    "color=c=0xdd8844@0.6:s=721x1279:d=0.04,format=rgba",
    "-frames:v", "1", "-y", path,
  ]);
}

async function writeManifest(path: string, manifest: unknown): Promise<void> {
  await writeFile(path, `${JSON.stringify(manifest)}\n`, { mode: 0o600 });
  await chmod(path, 0o600);
}

async function sha256File(path: string): Promise<string> {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}

async function run(command: string, args: readonly string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, [...args], { stdio: ["ignore", "pipe", "pipe"] });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
    child.once("error", reject);
    child.once("close", (code) => code === 0
      ? resolve(Buffer.concat(stdout).toString("utf8"))
      : reject(new Error(Buffer.concat(stderr).toString("utf8"))));
  });
}

function bindingForInput(
  prepared: Awaited<ReturnType<typeof prepareRealMaterialRun>>,
  inputId: string,
) {
  const binding = prepared.privateBindings.find((candidate) => candidate.inputId === inputId);
  if (!binding) throw new Error("test-binding-missing");
  return binding;
}

function assetForInput(
  prepared: Awaited<ReturnType<typeof prepareRealMaterialRun>>,
  inputId: string,
) {
  const binding = bindingForInput(prepared, inputId);
  const asset = prepared.assets.find((candidate) => candidate.id === binding.assetId);
  if (!asset) throw new Error("test-asset-missing");
  return asset;
}
