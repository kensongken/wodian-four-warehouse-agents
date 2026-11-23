import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type {
  Approval,
  ContentSegment,
  EditRevision,
  ExportPackage,
  VariantArtifact,
} from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import ffmpegPath from "ffmpeg-static";

import { JsonContentStore } from "../src/json-content-store";
import { calculateRenderInputDigest } from "../src/ffmpeg-renderer";
import { withdrawSegmentPermission } from "../src/privacy-invalidation";
import { approveVariant, confirmSegmentPublicUse } from "../src/review-gate";
import { createPrivacyDerivative } from "../src/privacy-processor";

describe("review and privacy gates", () => {
  let testDirectory: string;
  let library: JsonContentStore;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "review-privacy-"));
    library = await JsonContentStore.open(join(testDirectory, "library.json"));
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("approves only one exact artifact revision and variant", async () => {
    const segment = makeSegment("safe_seg_01", "allowed");
    const source = makeLibraryAsset(segment.assetId, "allowed", testDirectory, "approval-source");
    await writeFile(source.localPath, "approval-source");
    await library.putAsset(source);
    await library.putSegments([segment]);
    const revision = makeRevision("story_01", 2, "safe_seg_01");
    await library.putRevision(revision);
    const artifact = makeArtifact(
        "artifact_story_01_xhs_v2",
        "revision_story_01_2",
        "xiaohongshu",
        ["safe_seg_01"],
        testDirectory,
      );
    artifact.renderInputDigest = calculateRenderInputDigest(revision, [segment], [source]);
    await writeArtifactFiles(artifact);
    await library.putArtifact(artifact);
    const staleRetryHandle = await JsonContentStore.open(join(testDirectory, "library.json"));

    const input = {
      storyId: "story_01",
      revisionId: "revision_story_01_2",
      revision: 2,
      artifactId: "artifact_story_01_xhs_v2",
      variant: "xiaohongshu" as const,
      reviewerId: "designer_01",
    };
    const approval = await approveVariant(input, library);
    const replay = await approveVariant(input, staleRetryHandle);

    expect(approval.status).toBe("active");
    expect(replay).toEqual(approval);
    expect(
      (await library.listApprovals()).some(
        (item) => item.variant === "wechat-channels",
      ),
    ).toBe(false);
    expect((await library.listSegments())[0].tags.usage).toMatchObject({
      storyIds: ["story_01"],
      variants: ["xiaohongshu"],
      useCount: 1,
    });
    expect((await library.listAuditEvents(approval.id)).filter((event) =>
      event.action === "variant-approved")).toHaveLength(1);
  });

  it("rejects duplicate lineage IDs and changed artifact bytes", async () => {
    const segment = makeSegment("safe_seg_01", "allowed");
    const source = makeLibraryAsset(segment.assetId, "allowed", testDirectory, "tamper-source");
    await writeFile(source.localPath, "tamper-source");
    await library.putAsset(source);
    await library.putSegments([segment]);
    const revision = makeRevision("story_01", 2, "safe_seg_01");
    await library.putRevision(revision);
    const artifact = makeArtifact("artifact_tamper", "revision_story_01_2", "xiaohongshu", ["safe_seg_01"], testDirectory);
    artifact.renderInputDigest = calculateRenderInputDigest(revision, [segment], [source]);
    await expect(library.putArtifact({ ...artifact, sourceSegmentIds: ["safe_seg_01", "safe_seg_01"] }))
      .rejects.toThrow("duplicate-source-segment-id");
    await writeArtifactFiles(artifact);
    await library.putArtifact(artifact);
    await writeFile(artifact.videoPath, "replaced-after-render");
    await expect(approveVariant({
      storyId: "story_01", revisionId: "revision_story_01_2", revision: 2,
      artifactId: artifact.id, variant: "xiaohongshu", reviewerId: "designer_01",
    }, library)).rejects.toThrow("artifact-bytes-changed");
  });

  it("requires completed visitor privacy actions before public use", async () => {
    const sourcePath = join(testDirectory, "visitor-source.mp4");
    await createTestVideo(sourcePath);
    const source = {
      ...makeLibraryAsset("asset_visitor_seg_01", "review-required", testDirectory, "source"),
      localPath: sourcePath,
      digest: digestBuffer(await readFile(sourcePath)),
    };
    await library.putAsset(source);
    const forgedPath = join(testDirectory, "forged-safe.mp4");
    await writeFile(forgedPath, "safe");
    await library.putAsset({
      ...source,
      id: "asset_visitor_forged",
      localPath: forgedPath,
      digest: digestText("safe"),
      permissionState: "allowed",
      derivedFromAssetId: source.id,
      privacyActions: ["voice-muted"],
      privacyEvidence: {
        processor: "ffmpeg-privacy-v1",
        sourceDigest: source.digest,
        outputDigest: digestText("safe"),
        processedAt: "2026-09-04T00:00:00.000Z",
      },
    });
    await library.putSegments([makeSegment("visitor_seg_01", "review-required")]);
    await expect(confirmSegmentPublicUse({
      segmentId: "visitor_seg_01",
      designerId: "designer_01",
      completedActions: ["voice-muted"],
      sanitizedAssetId: "asset_visitor_forged",
    }, library)).rejects.toThrow("privacy-derivative-byte-verification-failed");
    const safe = await createPrivacyDerivative({
      sourceAssetId: source.id,
      outputAssetId: "asset_visitor_safe",
      outputPath: join(testDirectory, "visitor-safe.mp4"),
      actions: ["voice-muted"],
    }, library);
    await expect(
      confirmSegmentPublicUse(
        {
          segmentId: "visitor_seg_01",
          designerId: "designer_01",
          completedActions: [],
          sanitizedAssetId: "asset_visitor_safe",
        },
        library,
      ),
    ).rejects.toThrow("privacy-actions-required");
    const allowed = await confirmSegmentPublicUse(
      {
        segmentId: "visitor_seg_01",
        designerId: "designer_01",
        completedActions: ["voice-muted"],
        sanitizedAssetId: "asset_visitor_safe",
      },
      library,
    );

    expect(allowed.permissionState).toBe("allowed");
    expect(allowed.assetId).toBe("asset_visitor_safe");
    expect(await library.listAuditEvents("visitor_seg_01")).toEqual([
      expect.objectContaining({
        action: "segment-public-use-confirmed",
        reason: "voice-muted",
      }),
    ]);
  });

  it("invalidates affected approvals but leaves unrelated stories active", async () => {
    const affectedSegment = makeSegment("visitor_seg_01", "allowed");
    const safeSegment = makeSegment("safe_seg_02", "allowed");
    const affectedSource = makeLibraryAsset(affectedSegment.assetId, "allowed", testDirectory, "affected-source");
    const safeSource = makeLibraryAsset(safeSegment.assetId, "allowed", testDirectory, "safe-source");
    await writeFile(affectedSource.localPath, "affected-source");
    await writeFile(safeSource.localPath, "safe-source");
    await library.putAsset(affectedSource);
    await library.putAsset(safeSource);
    await library.putSegments([affectedSegment, safeSegment]);
    const affectedRevision = makeRevision("story_01", 2, affectedSegment.id);
    const safeRevision = makeRevision("story_02", 1, safeSegment.id);
    await library.putRevision(affectedRevision);
    await library.putRevision(safeRevision);
    const affectedArtifact = makeArtifact(
      "artifact_story_01_xhs_v2",
      "revision_story_01_2",
      "xiaohongshu",
      ["visitor_seg_01"],
      testDirectory,
    );
    const safeArtifact = makeArtifact(
      "artifact_story_02_xhs_v1",
      "revision_story_02_1",
      "xiaohongshu",
      ["safe_seg_02"],
      testDirectory,
    );
    affectedArtifact.renderInputDigest = calculateRenderInputDigest(
      affectedRevision,
      [affectedSegment, safeSegment],
      [affectedSource, safeSource],
    );
    safeArtifact.renderInputDigest = calculateRenderInputDigest(
      safeRevision,
      [affectedSegment, safeSegment],
      [affectedSource, safeSource],
    );
    await writeArtifactFiles(affectedArtifact);
    await writeArtifactFiles(safeArtifact);
    await library.putArtifact(affectedArtifact);
    await library.putArtifact(safeArtifact);
    await library.commitApproval(
      makeApproval(
        "approval_story_01_xhs_v2",
        "story_01",
        2,
        affectedArtifact,
      ),
    );
    await library.commitApproval(
      makeApproval(
        "approval_story_02_xhs_v1",
        "story_02",
        1,
        safeArtifact,
      ),
    );
    await library.putExportPackage(
      makeExportPackage(
        "export_story_01",
        "approval_story_01_xhs_v2",
        affectedArtifact,
      ),
    );

    const result = await withdrawSegmentPermission(
      {
        segmentId: "visitor_seg_01",
        reason: "visitor-withdrew",
        actorId: "designer_01",
      },
      library,
    );

    expect(result.invalidatedApprovalIds).toEqual([
      "approval_story_01_xhs_v2",
    ]);
    expect(result.invalidatedArtifactIds).toEqual([
      "artifact_story_01_xhs_v2",
    ]);
    expect(result.invalidatedExportPackageIds).toEqual(["export_story_01"]);
    expect(
      (await library.listApprovals()).find(
        (item) => item.id === "approval_story_02_xhs_v1",
      )?.status,
    ).toBe("active");
    expect(
      (await library.listSegments()).find(
        (item) => item.id === "visitor_seg_01",
      )?.permissionState,
    ).toBe("withdrawn");
  });
});

function makeSegment(
  id: string,
  permissionState: ContentSegment["permissionState"],
): ContentSegment {
  return {
    id,
    assetId: `asset_${id}`,
    batchId: "batch_01",
    startMs: 0,
    endMs: 1_000,
    permissionState,
    tags: {
      baseTypes: ["scene"],
      people: id.startsWith("visitor") ? ["visitor_01"] : [],
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

function makeLibraryAsset(
  id: string,
  permissionState: "allowed" | "review-required",
  directory: string,
  content: string,
) {
  return {
    id,
    batchId: "batch_01",
    sessionId: "session_01",
    digest: digestText(content),
    localPath: join(directory, id),
    mediaKind: "video" as const,
    importedAt: "2026-09-04T00:00:00.000Z",
    permissionState,
    origin: "new" as const,
    technicalFlags: [],
  };
}

function makeRevision(
  storyId: string,
  revision: number,
  segmentId: string,
): EditRevision {
  return {
    id: `revision_${storyId}_${revision}`,
    storyId,
    revision,
    parentRevisionId: revision === 1 ? null : `revision_${storyId}_${revision - 1}`,
    designerPrompt: null,
    changeExplanation: null,
    variants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"].map(
      (kind, index) => ({
        kind: kind as "editorial-master" | "xiaohongshu" | "douyin" | "wechat-channels",
        timeline: [{ segmentId, inMs: 0, outMs: 800 + index * 100, role: "anchor" }],
        title: `${kind} title`,
        caption: `${kind} caption`,
        cover: { segmentId, atMs: 100 },
      }),
    ),
    selectedStills: [],
    createdAt: "2026-09-04T00:00:00.000Z",
  };
}

function makeArtifact(
  id: string,
  revisionId: string,
  kind: VariantArtifact["kind"],
  sourceSegmentIds: string[],
  directory: string,
): VariantArtifact {
  return {
    id,
    revisionId,
    kind,
    videoPath: join(directory, `${id}.mp4`),
    digest: digestText(`video-${id}`),
    sourceSegmentIds,
    coverPath: join(directory, `${id}-cover.jpg`),
    coverDigest: digestText(`cover-${id}`),
    stillPaths: [],
    stillDigests: [],
    visualSourceSegmentIds: sourceSegmentIds,
    renderInputDigest: `render-${id}`,
    status: "active",
  };
}

function makeApproval(
  id: string,
  storyId: string,
  revision: number,
  artifact: VariantArtifact,
): Approval {
  return {
    id,
    storyId,
    revisionId: artifact.revisionId,
    revision,
    artifactId: artifact.id,
    artifactDigest: artifact.digest,
    coverDigest: artifact.coverDigest,
    stillDigests: artifact.stillDigests,
    renderInputDigest: artifact.renderInputDigest,
    variant: artifact.kind,
    reviewerId: "designer_01",
    approvedAt: "2026-09-04T00:00:00.000Z",
    status: "active",
  };
}

async function writeArtifactFiles(artifact: VariantArtifact): Promise<void> {
  await writeFile(artifact.videoPath, `video-${artifact.id}`);
  await writeFile(artifact.coverPath, `cover-${artifact.id}`);
}

function digestText(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function digestBuffer(value: Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

async function createTestVideo(path: string): Promise<void> {
  if (!ffmpegPath) throw new Error("ffmpeg unavailable");
  const binary = ffmpegPath;
  await new Promise<void>((resolve, reject) => {
    const child = spawn(binary, [
      "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=0.3",
      "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
      "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", path,
    ], { stdio: "ignore" });
    child.on("error", reject);
    child.on("close", (code: number | null) => code === 0 ? resolve() : reject(new Error(`ffmpeg:${code}`)));
  });
}

function makeExportPackage(
  id: string,
  approvalId: string,
  artifact: VariantArtifact,
): ExportPackage {
  return {
    id,
    approvalId,
    artifactId: artifact.id,
    variant: artifact.kind,
    videoPath: artifact.videoPath,
    videoDigest: artifact.digest,
    title: "title",
    caption: "caption",
    coverPath: "/renders/cover.jpg",
    coverDigest: artifact.coverDigest,
    stillPaths: [],
    stillDigests: [],
    renderInputDigest: artifact.renderInputDigest,
    idempotencyKey: `${approvalId}:${artifact.digest}`,
    status: "pending",
    createdAt: "2026-09-04T00:00:00.000Z",
  };
}
