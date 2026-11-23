import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import {
  mkdir,
  mkdtemp,
  readFile,
  realpath,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { join, resolve } from "node:path";

import {
  VariantArtifactSchema,
  type ContentSegment,
  type EditRevision,
  type LibraryAsset,
  type VariantArtifact,
} from "@wearable/contracts";
import ffmpegPath from "ffmpeg-static";
import { segmentWithoutUsage } from "./creative-editorial-evidence";
import { assertDifferentTimelines } from "./variant-timelines";

export async function renderRevision(
  revision: EditRevision,
  segments: readonly ContentSegment[],
  assets: readonly LibraryAsset[],
  outputDir: string,
): Promise<Readonly<{ artifacts: VariantArtifact[]; stillPaths: string[] }>> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  if (!/^[A-Za-z0-9_-]+$/.test(revision.id)) {
    throw new Error("unsafe-revision-id");
  }

  const segmentById = new Map(segments.map((segment) => [segment.id, segment]));
  const assetById = new Map(assets.map((asset) => [asset.id, asset]));
  const referencedSegmentIds = unique([
    ...revision.variants.flatMap((variant) =>
      variant.timeline.map((item) => item.segmentId),
    ),
    ...revision.variants.map((variant) => variant.cover.segmentId),
    ...revision.selectedStills.map((still) => still.segmentId),
  ]);

  await preflightSources(referencedSegmentIds, segmentById, assetById);
  const renderInputDigest = calculateRenderInputDigest(revision, segments, assets);
  await mkdir(outputDir, { recursive: true });
  outputDir = await realpath(outputDir);
  const finalDirectory = join(outputDir, `${revision.id}-${renderInputDigest.slice(0, 12)}`);
  if (await pathExists(finalDirectory)) {
    const manifest = JSON.parse(await readFile(join(finalDirectory, "render-manifest.json"), "utf8")) as {
      renderInputDigest: string; artifacts: VariantArtifact[]; stillPaths: string[];
    };
    if (manifest.renderInputDigest !== renderInputDigest) throw new Error("render-manifest-mismatch");
    const artifacts = VariantArtifactSchema.array().parse(manifest.artifacts);
    if (artifacts.length !== revision.variants.length) throw new Error("render-manifest-mismatch");
    for (const artifact of artifacts) {
      const variant = revision.variants.find((item) => item.kind === artifact.kind);
      const expectedStillPaths = revision.selectedStills.map((_, index) =>
        join(finalDirectory, `still-${String(index + 1).padStart(3, "0")}.jpg`),
      );
      if (!variant ||
        artifact.id !== `artifact_${revision.id}_${artifact.kind}` ||
        artifact.revisionId !== revision.id ||
        artifact.videoPath !== join(finalDirectory, `${artifact.kind}.mp4`) ||
        artifact.coverPath !== join(finalDirectory, `cover-${artifact.kind}.jpg`) ||
        artifact.renderInputDigest !== renderInputDigest ||
        !sameArray(artifact.sourceSegmentIds, unique(variant.timeline.map((item) => item.segmentId))) ||
        !sameArray(artifact.stillPaths, expectedStillPaths) ||
        !sameArray(artifact.visualSourceSegmentIds, unique([
          variant.cover.segmentId,
          ...revision.selectedStills.map((still) => still.segmentId),
        ]))) {
        throw new Error("render-manifest-mismatch");
      }
      if (await digestFile(artifact.videoPath) !== artifact.digest) throw new Error("render-output-digest-mismatch");
      if (await digestFile(artifact.coverPath) !== artifact.coverDigest) throw new Error("render-output-digest-mismatch");
      for (const [index, path] of artifact.stillPaths.entries()) {
        if (await digestFile(path) !== artifact.stillDigests[index]) throw new Error("render-output-digest-mismatch");
      }
    }
    await verifyRenderedRevision(revision, artifacts, segments, assets, outputDir);
    return { artifacts, stillPaths: manifest.stillPaths };
  }
  const temporaryDirectory = await mkdtemp(
    join(outputDir, `.${revision.id}.tmp-`),
  );

  try {
    const pendingArtifacts: Omit<VariantArtifact, "coverDigest" | "stillDigests">[] = [];
    const finalStillPaths = revision.selectedStills.map((_, index) =>
      join(finalDirectory, `still-${String(index + 1).padStart(3, "0")}.jpg`),
    );
    for (const variant of revision.variants) {
      const clipPaths: string[] = [];
      for (const [index, item] of variant.timeline.entries()) {
        const segment = requireSegment(item.segmentId, segmentById);
        assertTimelineRange(segment, item.inMs, item.outMs);
        const asset = requireAsset(segment.assetId, assetById);
        if (asset.mediaKind !== "video") {
          throw new Error(`timeline-source-not-video:${asset.id}`);
        }
        const clipPath = join(
          temporaryDirectory,
          `${variant.kind}-clip-${String(index).padStart(3, "0")}.mp4`,
        );
        await renderClip(
          asset.localPath,
          segment.startMs + item.inMs,
          item.outMs - item.inMs,
          clipPath,
        );
        clipPaths.push(clipPath);
      }
      if (clipPaths.length === 0) {
        throw new Error(`empty-variant-timeline:${variant.kind}`);
      }

      const listPath = join(temporaryDirectory, `${variant.kind}-concat.txt`);
      await writeFile(
        listPath,
        `${clipPaths.map((path) => `file '${escapeConcatPath(path)}'`).join("\n")}\n`,
        "utf8",
      );
      const temporaryVideoPath = join(
        temporaryDirectory,
        `${variant.kind}.mp4`,
      );
      await runFfmpeg([
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        listPath,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        temporaryVideoPath,
      ]);
      await assertNonEmpty(temporaryVideoPath);
      const coverSegment = requireSegment(variant.cover.segmentId, segmentById);
      assertStillRange(coverSegment, variant.cover.atMs);
      const coverAsset = requireAsset(coverSegment.assetId, assetById);
      const coverFilename = `cover-${variant.kind}.jpg`;
      await renderStill(
        coverAsset.localPath,
        coverSegment.startMs + variant.cover.atMs,
        join(temporaryDirectory, coverFilename),
      );
      await assertNonEmpty(join(temporaryDirectory, coverFilename));
      pendingArtifacts.push({
          id: `artifact_${revision.id}_${variant.kind}`,
          revisionId: revision.id,
          kind: variant.kind,
          videoPath: join(finalDirectory, `${variant.kind}.mp4`),
          digest: await digestFile(temporaryVideoPath),
          sourceSegmentIds: unique(
            variant.timeline.map((item) => item.segmentId),
          ),
          coverPath: join(finalDirectory, coverFilename),
          stillPaths: finalStillPaths,
          visualSourceSegmentIds: unique([
            variant.cover.segmentId,
            ...revision.selectedStills.map((still) => still.segmentId),
          ]),
          renderInputDigest,
          status: "active",
        });
    }

    const stillPaths: string[] = [];
    for (const [index, still] of revision.selectedStills.entries()) {
      const segment = requireSegment(still.segmentId, segmentById);
      assertStillRange(segment, still.atMs);
      const asset = requireAsset(segment.assetId, assetById);
      const filename = `still-${String(index + 1).padStart(3, "0")}.jpg`;
      const temporaryStillPath = join(temporaryDirectory, filename);
      await renderStill(
        asset.localPath,
        segment.startMs + still.atMs,
        temporaryStillPath,
      );
      await assertNonEmpty(temporaryStillPath);
      stillPaths.push(join(finalDirectory, filename));
    }

    const stillDigests = await Promise.all(stillPaths.map((path) =>
      digestFile(join(temporaryDirectory, path.split("/").at(-1)!)),
    ));
    const artifacts = await Promise.all(pendingArtifacts.map(async (artifact) =>
      VariantArtifactSchema.parse({
        ...artifact,
        coverDigest: await digestFile(join(temporaryDirectory, artifact.coverPath.split("/").at(-1)!)),
        stillDigests,
      }),
    ));
    await writeFile(join(temporaryDirectory, "render-manifest.json"), JSON.stringify({
      renderInputDigest,
      artifacts,
      stillPaths,
    }, null, 2));
    await rename(temporaryDirectory, finalDirectory);
    return { artifacts, stillPaths };
  } catch (error) {
    await rm(temporaryDirectory, { recursive: true, force: true });
    throw error;
  }
}

/** Check the file and its exact revision/source identity before publishing a local review result. */
export async function verifyRenderedRevision(
  revision: EditRevision,
  artifacts: readonly VariantArtifact[],
  segments: readonly ContentSegment[],
  assets: readonly LibraryAsset[],
  outputRoot: string,
): Promise<void> {
  assertDifferentTimelines(revision.variants);
  const checked = VariantArtifactSchema.array().parse(artifacts);
  if (checked.length !== 4 || new Set(checked.map((item) => item.kind)).size !== 4) {
    throw new Error("four-rendered-variants-required");
  }
  await preflightSources(unique(revision.variants.flatMap((variant) => [
    ...variant.timeline.map((item) => item.segmentId), variant.cover.segmentId,
    ...revision.selectedStills.map((item) => item.segmentId),
  ])), new Map(segments.map((item) => [item.id, item])), new Map(assets.map((item) => [item.id, item])));
  const renderInputDigest = calculateRenderInputDigest(revision, segments, assets);
  // This root comes from the render/commit authority, never from the files under inspection.
  if (!/^[A-Za-z0-9_-]+$/.test(revision.id) || resolve(outputRoot) !== outputRoot ||
    await realpath(outputRoot) !== outputRoot) {
    throw new Error("render-manifest-mismatch");
  }
  const outputDirectory = join(outputRoot, `${revision.id}-${renderInputDigest.slice(0, 12)}`);
  for (const artifact of checked) {
    const variant = revision.variants.find((item) => item.kind === artifact.kind);
    if (!variant || artifact.revisionId !== revision.id || artifact.status !== "active" ||
      artifact.id !== `artifact_${revision.id}_${artifact.kind}` || artifact.renderInputDigest !== renderInputDigest ||
      artifact.videoPath !== join(outputDirectory, `${artifact.kind}.mp4`) ||
      artifact.coverPath !== join(outputDirectory, `cover-${artifact.kind}.jpg`) ||
      !sameArray(artifact.stillPaths, revision.selectedStills.map((_, index) =>
        join(outputDirectory, `still-${String(index + 1).padStart(3, "0")}.jpg`))) ||
      !sameArray(artifact.sourceSegmentIds, unique(variant.timeline.map((item) => item.segmentId))) ||
      !sameArray(artifact.visualSourceSegmentIds, unique([
        variant.cover.segmentId, ...revision.selectedStills.map((item) => item.segmentId),
      ]))) throw new Error("render-manifest-mismatch");
    for (const [path, digest] of [[artifact.videoPath, artifact.digest], [artifact.coverPath, artifact.coverDigest],
      ...artifact.stillPaths.map((path, index) => [path, artifact.stillDigests[index]])]) {
      if (await realpath(path) !== path) throw new Error("render-manifest-mismatch");
      await assertNonEmpty(path);
      if (await digestFile(path) !== digest) throw new Error("render-output-digest-mismatch");
    }
    await runFfmpeg(["-v", "error", "-xerror", "-i", artifact.videoPath, "-map", "0:v:0", "-f", "null", "-"]);
  }
}

async function preflightSources(
  segmentIds: string[],
  segmentById: Map<string, ContentSegment>,
  assetById: Map<string, LibraryAsset>,
): Promise<void> {
  for (const segmentId of segmentIds) {
    const segment = requireSegment(segmentId, segmentById);
    if (segment.permissionState !== "allowed") {
      throw new Error(`source-permission-not-allowed:${segment.id}`);
    }
    const asset = requireAsset(segment.assetId, assetById);
    if (asset.permissionState !== "allowed") {
      throw new Error(`source-permission-not-allowed:${asset.id}`);
    }
    try {
      const metadata = await stat(asset.localPath);
      if (!metadata.isFile() || metadata.size === 0) throw new Error("empty");
    } catch {
      throw new Error(`source-media-missing:${asset.id}`);
    }
    if ((await digestFile(asset.localPath)) !== asset.digest) {
      throw new Error(`source-digest-mismatch:${asset.id}`);
    }
  }
}

async function renderClip(
  sourcePath: string,
  startMs: number,
  durationMs: number,
  outputPath: string,
): Promise<void> {
  const hasAudio = await sourceHasAudio(sourcePath);
  const args = [
    "-hide_banner",
    "-loglevel",
    "error",
    "-ss",
    seconds(startMs),
    "-i",
    sourcePath,
  ];
  if (!hasAudio) {
    args.push(
      "-f",
      "lavfi",
      "-i",
      "anullsrc=r=44100:cl=stereo",
      "-map",
      "0:v:0",
      "-map",
      "1:a:0",
    );
  } else {
    args.push("-map", "0:v:0", "-map", "0:a:0");
  }
  args.push(
    "-t",
    seconds(durationMs),
    "-vf",
    "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25",
    "-c:v",
    "libx264",
    "-preset",
    "ultrafast",
    "-pix_fmt",
    "yuv420p",
    "-c:a",
    "aac",
    "-ar",
    "44100",
    "-ac",
    "2",
    "-shortest",
    "-y",
    outputPath,
  );
  await runFfmpeg(args);
  await assertNonEmpty(outputPath);
}

async function renderStill(
  sourcePath: string,
  atMs: number,
  outputPath: string,
): Promise<void> {
  await runFfmpeg([
    "-hide_banner",
    "-loglevel",
    "error",
    "-ss",
    seconds(atMs),
    "-i",
    sourcePath,
    "-frames:v",
    "1",
    "-q:v",
    "2",
    "-y",
    outputPath,
  ]);
}

async function sourceHasAudio(sourcePath: string): Promise<boolean> {
  const result = await runProcess(ffmpegPath!, [
    "-hide_banner",
    "-i",
    sourcePath,
  ]);
  return /Stream #[^\n]+Audio:/.test(result.stderr);
}

async function runFfmpeg(args: string[]): Promise<void> {
  const result = await runProcess(ffmpegPath!, args);
  if (result.code !== 0) {
    throw new Error(`ffmpeg-render-failed:${result.stderr.trim()}`);
  }
}

async function runProcess(
  command: string,
  args: string[],
): Promise<{ code: number | null; stderr: string }> {
  return await new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: ["ignore", "ignore", "pipe"],
    });
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr += String(chunk);
    });
    child.once("error", reject);
    child.once("close", (code) => resolve({ code, stderr }));
  });
}

function requireSegment(
  segmentId: string,
  segmentById: Map<string, ContentSegment>,
): ContentSegment {
  const segment = segmentById.get(segmentId);
  if (!segment) throw new Error(`source-segment-missing:${segmentId}`);
  return segment;
}

function requireAsset(
  assetId: string,
  assetById: Map<string, LibraryAsset>,
): LibraryAsset {
  const asset = assetById.get(assetId);
  if (!asset) throw new Error(`source-media-missing:${assetId}`);
  return asset;
}

function assertTimelineRange(
  segment: ContentSegment,
  inMs: number,
  outMs: number,
): void {
  const duration = segment.endMs - segment.startMs;
  if (inMs < 0 || outMs <= inMs || outMs > duration) {
    throw new Error(`timeline-range-outside-segment:${segment.id}`);
  }
}

function assertStillRange(segment: ContentSegment, atMs: number): void {
  if (atMs < 0 || atMs >= segment.endMs - segment.startMs) {
    throw new Error(`still-time-outside-segment:${segment.id}`);
  }
}

async function assertNonEmpty(path: string): Promise<void> {
  const metadata = await stat(path);
  if (!metadata.isFile() || metadata.size === 0) {
    throw new Error(`rendered-artifact-empty:${path}`);
  }
}

async function digestFile(path: string): Promise<string> {
  const hash = createHash("sha256");
  await new Promise<void>((resolve, reject) => {
    const stream = createReadStream(path);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.once("error", reject);
    stream.once("end", resolve);
  });
  return hash.digest("hex");
}

function digestText(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

export function calculateRenderInputDigest(
  revision: EditRevision,
  segments: readonly ContentSegment[],
  assets: readonly LibraryAsset[],
): string {
  const segmentById = new Map(segments.map((segment) => [segment.id, segment]));
  const assetById = new Map(assets.map((asset) => [asset.id, asset]));
  return calculateRenderInputDigestFromMaps(revision, segmentById, assetById);
}

export function calculateRenderInputDigests(
  revisions: readonly EditRevision[],
  segments: readonly ContentSegment[],
  assets: readonly LibraryAsset[],
): ReadonlyMap<string, string> {
  const segmentById = new Map(segments.map((segment) => [segment.id, segment]));
  const assetById = new Map(assets.map((asset) => [asset.id, asset]));
  return new Map(revisions.map((revision) => [
    revision.id,
    calculateRenderInputDigestFromMaps(revision, segmentById, assetById),
  ]));
}

function calculateRenderInputDigestFromMaps(
  revision: EditRevision,
  segmentById: Map<string, ContentSegment>,
  assetById: Map<string, LibraryAsset>,
): string {
  const referenced = unique([
    ...revision.variants.flatMap((variant) => variant.timeline.map((item) => item.segmentId)),
    ...revision.variants.map((variant) => variant.cover.segmentId),
    ...revision.selectedStills.map((still) => still.segmentId),
  ]).sort();
  return digestText(JSON.stringify({
    revision,
    sources: referenced.map((segmentId) => {
      const segment = requireSegment(segmentId, segmentById);
      const asset = requireAsset(segment.assetId, assetById);
      return {
        segment: segmentWithoutUsage(segment),
        parentAsset: asset,
      };
    }),
  }));
}

function sameArray(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function seconds(milliseconds: number): string {
  return (milliseconds / 1000).toFixed(3);
}

function escapeConcatPath(path: string): string {
  return path.replaceAll("'", "'\\''");
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}

async function pathExists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}
