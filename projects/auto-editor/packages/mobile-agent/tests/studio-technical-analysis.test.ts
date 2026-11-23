import { createRequire } from "node:module";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import ffmpegPath from "ffmpeg-static";
import { afterEach, describe, expect, it } from "vitest";

import {
  createFfprobeStudioSourceIntervalResolver,
  CustomerMaterialLibrary,
  LocalProcessStudioModelWorker,
  StudioAnalysisJobService,
} from "../src";
import { run } from "./fixtures/private-studio-fixtures";

const ffprobePath = (createRequire(import.meta.url)("ffprobe-static") as { path: string }).path;
const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const workerRoot = join(repositoryRoot, "tools/media-model-worker");
const scope = { actorId: "actor-a", customerId: "customer-a", sessionId: "session-a" };

async function makeFakeFfprobe(root: string, name: string, duration: unknown): Promise<string> {
  const executable = join(root, name);
  const output = JSON.stringify({ format: { duration } });
  await writeFile(
    executable,
    `#!${process.execPath}\nprocess.stdout.write(${JSON.stringify(output)});\n`,
    "utf8",
  );
  await chmod(executable, 0o700);
  return executable;
}

async function makeRawFakeFfprobe(root: string, name: string, output: string): Promise<string> {
  const executable = join(root, name);
  await writeFile(
    executable,
    `#!${process.execPath}\nprocess.stdout.write(${JSON.stringify(output)});\n`,
    "utf8",
  );
  await chmod(executable, 0o700);
  return executable;
}

async function resolveFakeDuration(executable: string): Promise<number> {
  const interval = await createFfprobeStudioSourceIntervalResolver(executable)({
    scope,
    asset: undefined as never,
    sourcePath: "/synthetic/source.mp4",
  });
  if (interval === null) throw new Error("studio-technical-test-interval-missing");
  return interval.sourceDurationMs;
}

describe("studio technical analysis", () => {
  const cleanup: string[] = [];

  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("uses the trusted ffprobe duration for Task 3 technical and scene receipts", async () => {
    if (!ffmpegPath || !ffprobePath || !isAbsolute(ffprobePath)) {
      throw new Error("studio-technical-test-binaries-unavailable");
    }
    const root = await mkdtemp(join(tmpdir(), "studio-technical-analysis-"));
    cleanup.push(root);
    const source = join(root, "fixture.mp4");
    await run(ffmpegPath, [
      "-hide_banner", "-loglevel", "error",
      "-f", "lavfi", "-i", "color=c=red:s=160x120:r=10:d=2.4",
      "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
      "-y", source,
    ]);
    const library = new CustomerMaterialLibrary(join(root, "library"), () => true);
    const asset = await library.importOriginal(scope.actorId, scope.customerId, {
      requestId: "technical-fixture-import",
      bytes: await readFile(source),
      mediaKind: "video",
      fileName: "fixture.mp4",
      capturedAt: "2026-09-12T05:00:00.000Z",
      sessionId: scope.sessionId,
      viewPermission: "allowed",
      usePermission: "allowed",
    });
    const testPython = process.env.STUDIO_TEST_PYTHON;
    if (testPython && !isAbsolute(testPython)) {
      throw new Error("studio-test-python-must-be-absolute");
    }
    const testCommand = testPython
      ?? join(workerRoot, ".venv", "bin", "python");
    if (!isAbsolute(testCommand)) throw new Error("studio-test-command-must-be-absolute");
    const worker = new LocalProcessStudioModelWorker({
      command: testCommand,
      args: ["worker.py", "--ffprobe-path", ffprobePath],
      cwd: workerRoot,
      timeoutMs: 60_000,
      maxStdoutBytes: 128 * 1024,
      maxStderrBytes: 4 * 1024,
      allowedMediaRoot: root,
    });
    const service = new StudioAnalysisJobService(library, worker, {
      now: () => "2026-09-12T05:00:00.000Z",
      resolveSourceInterval: createFfprobeStudioSourceIntervalResolver(ffprobePath),
    });

    const result = await service.enqueue(
      scope,
      asset.id,
      ["technical", "scene"],
      "technical-real-duration",
    );
    expect(result.job.failure).toBeNull();
    expect(result.job).toMatchObject({
      sourceStartMs: 0,
      sourceEndMs: 2_400,
      sourceDurationMs: 2_400,
      status: "succeeded",
    });
    expect(result.receipts).toHaveLength(2);
    expect(result.receipts.every((receipt) =>
      receipt.sourceStartMs === 0 && receipt.sourceEndMs === 2_400,
    )).toBe(true);
    expect(result.receipts.find((receipt) => receipt.layer === "technical")).toMatchObject({
      sourceStartMs: 0,
      sourceEndMs: 2_400,
      status: "succeeded",
      providerMode: "local",
      qualitySignals: {
        hasVideo: true,
        hasAudio: false,
        width: 160,
        height: 120,
        durationMs: 2_400,
      },
    });
    const sceneReceipt = result.receipts.find((receipt) => receipt.layer === "scene");
    expect(sceneReceipt).toMatchObject({
      sourceStartMs: 0,
      sourceEndMs: 2_400,
      status: "succeeded",
      providerMode: "local",
      qualitySignals: {
        scenes: [expect.objectContaining({
          startMs: 0,
          endMs: 2_400,
        })],
      },
    });
    const scenes = sceneReceipt?.qualitySignals.scenes as Array<{
      startMs: number;
      endMs: number;
      representativeFrameMs: number;
    }>;
    expect(scenes.every((scene) =>
      scene.endMs - scene.startMs >= 600
      && scene.startMs < scene.representativeFrameMs
      && scene.representativeFrameMs < scene.endMs,
    )).toBe(true);
  }, 120_000);

  it("rejects a relative ffprobe configuration before it can become a duration resolver", () => {
    expect(() => createFfprobeStudioSourceIntervalResolver("ffprobe"))
      .toThrow("studio-analysis-ffprobe-path-invalid");
  });

  it.each([
    ["finite positive number", 1.2345],
    ["canonical non-empty decimal string", "1.2345"],
  ])("uses shared half-up millisecond rounding for a %s", async (_name, duration) => {
    const root = await mkdtemp(join(tmpdir(), "studio-duration-rounding-"));
    cleanup.push(root);
    const executable = await makeFakeFfprobe(root, "ffprobe", duration);

    await expect(resolveFakeDuration(executable)).resolves.toBe(1_235);
  });

  it.each([
    ["boolean", true],
    ["array", [2.4]],
    ["object", { seconds: 2.4 }],
    ["null", null],
    ["empty string", ""],
    ["trim-dependent string", " 2.4 "],
    ["exponent string", "2.4e0"],
    ["leading-zero string", "02.4"],
  ])("rejects a non-canonical ffprobe duration: %s", async (_name, duration) => {
    const root = await mkdtemp(join(tmpdir(), "studio-duration-invalid-"));
    cleanup.push(root);
    const executable = await makeFakeFfprobe(root, "ffprobe", duration);

    await expect(resolveFakeDuration(executable))
      .rejects.toThrow("studio-analysis-ffprobe-duration-invalid");
  });

  it("keeps malformed ffprobe JSON distinct from an invalid duration", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-duration-malformed-"));
    cleanup.push(root);
    const executable = await makeRawFakeFfprobe(root, "ffprobe", "{");

    await expect(resolveFakeDuration(executable))
      .rejects.toThrow("studio-analysis-ffprobe-output-invalid");
  });
});
