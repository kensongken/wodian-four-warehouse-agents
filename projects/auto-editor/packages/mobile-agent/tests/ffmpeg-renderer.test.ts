import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type {
  ContentSegment,
  EditRevision,
  LibraryAsset,
  VariantPlan,
} from "@wearable/contracts";
import ffmpegPath from "ffmpeg-static";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { calculateRenderInputDigest, renderRevision } from "../src/ffmpeg-renderer";

describe("ffmpeg renderer", () => {
  let testDirectory: string;
  let outputDirectory: string;
  let assets: LibraryAsset[];
  let segments: ContentSegment[];

  beforeEach(async () => {
    if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
    testDirectory = await mkdtemp(join(tmpdir(), "ffmpeg-renderer-"));
    outputDirectory = join(testDirectory, "output");
    const redPath = join(testDirectory, "red.mp4");
    const bluePath = join(testDirectory, "blue.mp4");
    await makeSyntheticClip(redPath, "red");
    await makeSyntheticClip(bluePath, "blue");
    assets = [await makeAsset("asset_red", redPath), await makeAsset("asset_blue", bluePath)];
    segments = [
      makeSegment("segment_red", "asset_red"),
      makeSegment("segment_blue", "asset_blue"),
    ];
  }, 30_000);

  afterEach(async () => {
    if (testDirectory) {
      await rm(testDirectory, { recursive: true, force: true });
    }
  });

  it("renders four non-empty video artifacts with exact source lineage", async () => {
    const revision = makeRevision();

    const { artifacts, stillPaths } = await renderRevision(
      revision,
      segments,
      assets,
      outputDirectory,
    );

    expect(artifacts.map((item) => item.kind)).toEqual([
      "editorial-master",
      "xiaohongshu",
      "douyin",
      "wechat-channels",
    ]);
    for (const artifact of artifacts) {
      expect((await stat(artifact.videoPath)).size).toBeGreaterThan(0);
      expect(artifact.sourceSegmentIds.length).toBeGreaterThan(0);
      expect(artifact.digest).toMatch(/^[a-f0-9]{64}$/);
    }
    expect(artifacts[0].sourceSegmentIds).toEqual([
      "segment_red",
      "segment_blue",
    ]);
    expect(stillPaths).toHaveLength(1);
    expect((await stat(stillPaths[0])).size).toBeGreaterThan(0);
  }, 30_000);

  it("keeps the revision plan intact when rendering fails", async () => {
    const revisionWithMissingSource = makeRevision();
    const missingAssets = [
      assets[0],
      { ...assets[1], localPath: join(testDirectory, "missing.mp4") },
    ];

    await expect(
      renderRevision(
        revisionWithMissingSource,
        segments,
        missingAssets,
        outputDirectory,
      ),
    ).rejects.toThrow("source-media-missing");
    expect(revisionWithMissingSource.id).toBe("revision_story_01_1");
  });

  it("rejects a cached manifest that repeats one variant in all slots", async () => {
    const rendered = await renderRevision(makeRevision(), segments, assets, outputDirectory);
    const manifestPath = join(rendered.artifacts[0]!.videoPath, "..", "render-manifest.json");
    const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
    manifest.artifacts = [manifest.artifacts[0], manifest.artifacts[0], manifest.artifacts[0]];
    await writeFile(manifestPath, JSON.stringify(manifest));
    await expect(renderRevision(makeRevision(), segments, assets, outputDirectory))
      .rejects.toThrow("render-manifest-mismatch");
  }, 30_000);

  it("rejects a source path whose bytes changed after ingest", async () => {
    await writeFile(assets[0].localPath, Buffer.from("tampered-after-ingest"));

    await expect(
      renderRevision(makeRevision(), segments, assets, outputDirectory),
    ).rejects.toThrow("source-digest-mismatch");
  });

  it("changes the render identity when a segment source offset changes", () => {
    const revision = makeRevision();
    const first = calculateRenderInputDigest(revision, segments, assets);
    const shifted = segments.map((segment) => segment.id === "segment_red"
      ? { ...segment, startMs: 500, endMs: 2_000 }
      : segment);
    expect(calculateRenderInputDigest(revision, shifted, assets)).not.toBe(first);
  });
});

function makeRevision(): EditRevision {
  return {
    id: "revision_story_01_1",
    storyId: "story_01",
    revision: 1,
    parentRevisionId: null,
    designerPrompt: null,
    changeExplanation: null,
    variants: [
      makeVariant("editorial-master", ["segment_red", "segment_blue"]),
      makeVariant("xiaohongshu", ["segment_blue"]),
      makeVariant("douyin", ["segment_red"]),
      makeVariant("wechat-channels", ["segment_blue", "segment_red"]),
    ],
    selectedStills: [
      { segmentId: "segment_red", atMs: 500, reason: "clear-product-view" },
    ],
    createdAt: "2026-09-04T00:00:00.000Z",
  };
}

function makeVariant(
  kind: VariantPlan["kind"],
  segmentIds: string[],
): VariantPlan {
  return {
    kind,
    timeline: segmentIds.map((segmentId) => ({
      segmentId,
      inMs: 0,
      outMs: 800,
      role: "anchor" as const,
    })),
    title: `${kind} title`,
    caption: `${kind} caption`,
    cover: { segmentId: segmentIds[0], atMs: 300 },
  };
}

async function makeAsset(id: string, localPath: string): Promise<LibraryAsset> {
  const digest = createHash("sha256").update(await readFile(localPath)).digest("hex");
  return {
    id,
    batchId: "batch_01",
    sessionId: "session_01",
    digest,
    localPath,
    mediaKind: "video",
    importedAt: "2026-09-04T00:00:00.000Z",
    permissionState: "allowed",
    origin: "new",
    technicalFlags: [],
  };
}

function makeSegment(id: string, assetId: string): ContentSegment {
  return {
    id,
    assetId,
    batchId: "batch_01",
    startMs: 0,
    endMs: 1_500,
    permissionState: "allowed",
    tags: {
      baseTypes: ["handmade"],
      people: [],
      products: [],
      processStages: [],
      topics: [],
      emotions: [],
      transcript: "",
      technicalFlags: [],
      usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
    },
  };
}

async function makeSyntheticClip(path: string, color: string): Promise<void> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  await run(ffmpegPath, [
    "-hide_banner",
    "-loglevel",
    "error",
    "-f",
    "lavfi",
    "-i",
    `color=c=${color}:s=160x120:r=25:d=1.5`,
    "-f",
    "lavfi",
    "-i",
    "anullsrc=r=44100:cl=stereo",
    "-shortest",
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-c:a",
    "aac",
    "-y",
    path,
  ]);
}

async function run(command: string, args: string[]): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr += String(chunk);
    });
    child.once("error", reject);
    child.once("close", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`ffmpeg-test-fixture-failed:${stderr}`));
    });
  });
}
