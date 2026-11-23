import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type {
  Approval,
  ContentSegment,
  EditRevision,
  LibraryAsset,
  VariantArtifact,
} from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  createExportPackage,
  deliverExportPackage,
  type PhonePublisherAdapter,
} from "../src/export-package";
import { calculateRenderInputDigest } from "../src/ffmpeg-renderer";
import { JsonContentStore } from "../src/json-content-store";

describe("export package", () => {
  let testDirectory: string;
  let library: JsonContentStore;
  let artifact: VariantArtifact;
  let approval: Approval;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "export-package-"));
    library = await JsonContentStore.open(join(testDirectory, "library.json"));
    const source = makeSource(testDirectory);
    const segment = makeSegment();
    await writeFile(source.localPath, "source");
    await library.putAsset(source);
    await library.putSegments([segment]);
    artifact = makeArtifact(testDirectory);
    artifact.renderInputDigest = calculateRenderInputDigest(makeRevision(), [segment], [source]);
    await writeFile(artifact.videoPath, "video");
    await writeFile(artifact.coverPath, "cover");
    await writeFile(artifact.stillPaths[0], "still");
    approval = makeApproval("active", artifact);
    await library.putStories([{
      id: "story_01",
      batchId: "legacy_batch_01",
      type: "product-journey",
      anchorSegmentIds: ["segment_01"],
      historicalSegmentIds: [],
      historicalReasons: {},
      status: "candidate",
    }]);
    await library.putRevision(makeRevision());
    await library.putArtifact(artifact);
    await library.commitApproval(approval);
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("refuses to package an unapproved or invalidated artifact", async () => {
    await library.putApproval({ ...approval, status: "invalidated" });

    await expect(
      createExportPackage(
        {
          approvalId: approval.id,
          artifactId: artifact.id,
          revisionId: artifact.revisionId,
        },
        library,
      ),
    ).rejects.toThrow("active-approval-required");
  });

  it("reads approved copy from the exact revision and builds a stable key", async () => {
    const value = await createExportPackage(
      {
        approvalId: approval.id,
        artifactId: artifact.id,
        revisionId: artifact.revisionId,
      },
      library,
    );

    expect(value).toMatchObject({
      title: "approved xhs title",
      caption: "approved xhs caption",
      variant: "xiaohongshu",
      idempotencyKey: `${approval.id}:${artifact.digest}`,
      status: "pending",
    });
  });

  it("keeps a noncreative export working when a consent fixture shares its batch id", async () => {
    await library.putCreativeSession({
      id: "creative_export_01",
      customerId: "customer_01",
      title: "A-0 scoped export fixture",
      stage: "editorial_review",
      currentIntentVersion: 1,
      currentBriefVersion: 0,
      currentScriptVersion: 0,
      currentShotPlanVersion: 0,
      materialViewDecisionIds: [],
      materialUseDecisionIds: [],
      deviceBindings: [],
      captureBatchIds: ["legacy_batch_01"],
      editorialBatchId: "legacy_batch_01",
      privacyMode: "creator",
      prohibitedAreas: [],
      status: "active",
      createdAt: "2026-09-08T09:00:00.000Z",
      updatedAt: "2026-09-08T09:00:00.000Z",
    });
    const input = { approvalId: approval.id, artifactId: artifact.id, revisionId: artifact.revisionId };

    await expect(createExportPackage(input, library))
      .resolves.toMatchObject({ approvalId: approval.id, artifactId: artifact.id });
    await expect(createExportPackage({ ...input, creativeSessionId: "creative_export_01" }, library))
      .resolves.toMatchObject({ approvalId: approval.id, artifactId: artifact.id });
    expect(await library.listExportPackages()).toHaveLength(1);
  });

  it("keeps delivery pending until the phone publisher returns a receipt", async () => {
    const value = await createExportPackage(
      {
        approvalId: approval.id,
        artifactId: artifact.id,
        revisionId: artifact.revisionId,
      },
      library,
    );
    const timeoutAdapter: PhonePublisherAdapter = {
      deliver: async () => {
        throw new Error("timeout");
      },
    };

    const result = await deliverExportPackage(value.id, timeoutAdapter, library);

    expect(result.status).toBe("pending-retry");
    expect(result.publishedAt).toBeNull();
    expect(result.publishedClaimed).toBe(false);
    expect((await library.listExportPackages())[0].status).toBe("pending-retry");
  });

  it("records receipt as received without claiming public publication", async () => {
    const value = await createExportPackage(
      {
        approvalId: approval.id,
        artifactId: artifact.id,
        revisionId: artifact.revisionId,
      },
      library,
    );
    const adapter: PhonePublisherAdapter = {
      deliver: async (input) => ({
        receiptId: `receipt_${input.idempotencyKey}`,
        receivedAt: "2026-09-04T01:00:00.000Z",
      }),
    };

    const result = await deliverExportPackage(value.id, adapter, library);

    expect(result).toMatchObject({
      status: "received",
      receivedAt: "2026-09-04T01:00:00.000Z",
      publishedAt: null,
      publishedClaimed: false,
    });
  });

  it("refuses delivery when approved bytes change after packaging", async () => {
    const value = await createExportPackage({
      approvalId: approval.id, artifactId: artifact.id,
      revisionId: artifact.revisionId,
    }, library);
    await writeFile(artifact.videoPath, "tampered-after-package");
    await expect(deliverExportPackage(value.id, {
      deliver: async () => ({ receiptId: "should-not-run", receivedAt: new Date().toISOString() }),
    }, library)).rejects.toThrow("export-package-bytes-changed");
  });

  it("atomically rejects package creation when approval is invalidated after the initial read", async () => {
    const initialAudit = await library.listAuditEvents();
    const initialBytes = await readFile(artifact.videoPath);
    const originalCommitPackage = library.commitExportPackage.bind(library);
    let releaseRead!: () => void;
    let markRead!: () => void;
    const readReached = new Promise<void>((resolve) => { markRead = resolve; });
    const continueRead = new Promise<void>((resolve) => { releaseRead = resolve; });
    let intercepted = false;
    library.commitExportPackage = async (...args) => {
      if (!intercepted) {
        intercepted = true;
        markRead();
        await continueRead;
      }
      return originalCommitPackage(...args);
    };

    const creating = createExportPackage({
      approvalId: approval.id,
      artifactId: artifact.id,
      revisionId: artifact.revisionId,
    }, library);
    await readReached;
    await library.putApproval({ ...approval, status: "invalidated" });
    releaseRead();

    await expect(creating).rejects.toThrow("active-approval-required");
    expect(await library.listExportPackages()).toEqual([]);
    expect(await library.listAuditEvents()).toEqual(initialAudit);
    expect(await readFile(artifact.videoPath)).toEqual(initialBytes);
  });

  it("does not record a received delivery when approval is invalidated after package lookup", async () => {
    const value = await createExportPackage({
      approvalId: approval.id,
      artifactId: artifact.id,
      revisionId: artifact.revisionId,
    }, library);
    const originalListPackages = library.listExportPackages.bind(library);
    let releaseRead!: () => void;
    let markRead!: () => void;
    const readReached = new Promise<void>((resolve) => { markRead = resolve; });
    const continueRead = new Promise<void>((resolve) => { releaseRead = resolve; });
    let intercepted = false;
    library.listExportPackages = async () => {
      const values = await originalListPackages();
      if (!intercepted) {
        intercepted = true;
        markRead();
        await continueRead;
      }
      return values;
    };
    let deliveryCalls = 0;
    const delivering = deliverExportPackage(value.id, {
      deliver: async () => {
        deliveryCalls += 1;
        return { receiptId: "must-not-be-recorded", receivedAt: "2026-09-08T09:00:00.000Z" };
      },
    }, library);
    await readReached;
    await library.putApproval({ ...approval, status: "invalidated" });
    releaseRead();

    await expect(delivering).rejects.toThrow("active-export-package-required");
    expect(deliveryCalls).toBe(0);
    expect(await library.listDeliveries()).toEqual([]);
  });

  it("atomically rejects a receipt when approval is invalidated while the adapter is returning", async () => {
    const value = await createExportPackage({
      approvalId: approval.id,
      artifactId: artifact.id,
      revisionId: artifact.revisionId,
    }, library);
    let releaseAdapter!: () => void;
    let markAdapter!: () => void;
    const adapterReached = new Promise<void>((resolve) => { markAdapter = resolve; });
    const continueAdapter = new Promise<void>((resolve) => { releaseAdapter = resolve; });
    const delivering = deliverExportPackage(value.id, {
      deliver: async () => {
        markAdapter();
        await continueAdapter;
        return { receiptId: "raced-receipt", receivedAt: "2026-09-08T09:02:00.000Z" };
      },
    }, library);
    await adapterReached;
    await library.putApproval({ ...approval, status: "invalidated" });
    releaseAdapter();

    await expect(delivering).rejects.toThrow("active-export-package-required");
    expect(await library.listDeliveries()).toEqual([]);
    expect((await library.listExportPackages())[0]).toMatchObject({
      id: value.id,
      status: "invalidated",
    });
  });
});

function makeArtifact(directory: string): VariantArtifact {
  return {
    id: "artifact_story_01_xhs_v2",
    revisionId: "revision_story_01_2",
    kind: "xiaohongshu",
    videoPath: join(directory, "xiaohongshu.mp4"),
    digest: digestText("video"),
    sourceSegmentIds: ["segment_01"],
    coverPath: join(directory, "cover.jpg"),
    coverDigest: digestText("cover"),
    stillPaths: [join(directory, "still-001.jpg")],
    stillDigests: [digestText("still")],
    visualSourceSegmentIds: ["segment_01"],
    renderInputDigest: "render-input-digest",
    status: "active",
  };
}

function makeSource(directory: string): LibraryAsset {
  return {
    id: "asset_segment_01",
    batchId: "legacy_batch_01",
    sessionId: "legacy_session_01",
    digest: digestText("source"),
    localPath: join(directory, "source.mp4"),
    mediaKind: "video",
    importedAt: "2026-09-04T00:00:00.000Z",
    permissionState: "allowed",
    origin: "new",
    technicalFlags: [],
  };
}

function makeSegment(): ContentSegment {
  return {
    id: "segment_01",
    assetId: "asset_segment_01",
    batchId: "legacy_batch_01",
    startMs: 0,
    endMs: 1_500,
    permissionState: "allowed",
    tags: {
      baseTypes: ["product-image"],
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

function makeApproval(status: Approval["status"], value: VariantArtifact): Approval {
  return {
    id: "approval_story_01_xhs_v2",
    storyId: "story_01",
    revisionId: "revision_story_01_2",
    revision: 2,
    artifactId: "artifact_story_01_xhs_v2",
    artifactDigest: value.digest,
    coverDigest: value.coverDigest,
    stillDigests: value.stillDigests,
    renderInputDigest: value.renderInputDigest,
    variant: "xiaohongshu",
    reviewerId: "designer_01",
    approvedAt: "2026-09-04T00:00:00.000Z",
    status,
  };
}

function digestText(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function makeRevision(): EditRevision {
  return {
    id: "revision_story_01_2",
    storyId: "story_01",
    revision: 2,
    parentRevisionId: "revision_story_01_1",
    designerPrompt: "shorten opening",
    changeExplanation: "shorter opening",
    variants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"].map(
      (kind, index) => ({
        kind: kind as "editorial-master" | "xiaohongshu" | "douyin" | "wechat-channels",
        timeline: [
          { segmentId: "segment_01", inMs: 0, outMs: 1_000 + index * 100, role: "anchor" },
        ],
        title: kind === "xiaohongshu" ? "approved xhs title" : `${kind} title`,
        caption:
          kind === "xiaohongshu" ? "approved xhs caption" : `${kind} caption`,
        cover: { segmentId: "segment_01", atMs: 100 },
      }),
    ),
    selectedStills: [],
    createdAt: "2026-09-04T00:00:00.000Z",
  };
}
