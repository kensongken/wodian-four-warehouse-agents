import { createHash, randomUUID } from "node:crypto";
import { createReadStream } from "node:fs";

import {
  ApprovalSchema,
  type Approval,
  type ContentSegment,
  type VideoVariant,
} from "@wearable/contracts";

import type { ContentLibrary } from "./content-library";
import { calculateRenderInputDigest } from "./ffmpeg-renderer";
import { verifyPrivacyDerivative } from "./privacy-processor";

export async function confirmSegmentPublicUse(
  input: Readonly<{
    segmentId: string;
    designerId: string;
    completedActions: string[];
    sanitizedAssetId?: string;
  }>,
  library: ContentLibrary,
): Promise<ContentSegment> {
  const segment = (await library.listSegments()).find(
    (item) => item.id === input.segmentId,
  );
  if (!segment) throw new Error(`segment-not-found:${input.segmentId}`);
  if (segment.permissionState === "allowed") return segment;
  if (segment.permissionState !== "review-required") {
    throw new Error("segment-not-reviewable");
  }
  const actions = unique(
    input.completedActions.map((action) => action.trim()).filter(Boolean),
  );
  if (actions.length === 0) throw new Error("privacy-actions-required");
  if (!input.sanitizedAssetId) throw new Error("sanitized-derivative-required");
  const sourceAsset = (await library.listAssets()).find(
    (asset) => asset.id === segment.assetId,
  );
  const sanitizedAsset = (await library.listAssets()).find(
    (asset) => asset.id === input.sanitizedAssetId,
  );
  if (
    !sourceAsset ||
    !sanitizedAsset ||
    sanitizedAsset.permissionState !== "allowed" ||
    sanitizedAsset.derivedFromAssetId !== sourceAsset.id ||
    !sanitizedAsset.privacyEvidence ||
    sanitizedAsset.privacyEvidence.processor !== "ffmpeg-privacy-v1" ||
    sanitizedAsset.privacyEvidence.sourceDigest !== sourceAsset.digest ||
    sanitizedAsset.privacyEvidence.outputDigest !== sanitizedAsset.digest ||
    (await digestFile(sourceAsset.localPath)) !== sourceAsset.digest ||
    (await digestFile(sanitizedAsset.localPath)) !== sanitizedAsset.digest ||
    !actions.every((action) => sanitizedAsset.privacyActions?.includes(action))
  ) {
    throw new Error("sanitized-derivative-required");
  }
  await verifyPrivacyDerivative(sourceAsset, sanitizedAsset);

  const allowed: ContentSegment = {
    ...segment,
    assetId: sanitizedAsset.id,
    permissionState: "allowed",
  };
  await library.putSegments([allowed]);
  await library.putAuditEvent({
    id: `audit_${randomUUID()}`,
    entityType: "segment",
    entityId: segment.id,
    actorId: input.designerId,
    action: "segment-public-use-confirmed",
    reason: actions.join(","),
    before: segment,
    after: allowed,
    createdAt: new Date().toISOString(),
  });
  return allowed;
}

export async function approveVariant(
  input: Readonly<{
    storyId: string;
    revisionId: string;
    revision: number;
    artifactId: string;
    variant: VideoVariant;
    reviewerId: string;
  }>,
  library: ContentLibrary,
): Promise<Approval> {
  const revision = (await library.listRevisions(input.storyId)).find(
    (item) => item.id === input.revisionId && item.revision === input.revision,
  );
  await library.validateRevisionTimelines(input.revisionId, input);
  if (!revision) throw new Error("exact-revision-required");
  if (!revision.variants.some((variant) => variant.kind === input.variant)) {
    throw new Error("exact-variant-required");
  }
  const artifact = (await library.listArtifacts(input.revisionId)).find(
    (item) =>
      item.id === input.artifactId &&
      item.kind === input.variant &&
      item.status === "active",
  );
  if (!artifact) throw new Error("exact-active-artifact-required");
  const variantPlan = revision.variants.find((variant) => variant.kind === input.variant)!;
  const expectedVideoSources = unique(
    variantPlan.timeline.map((item) => item.segmentId),
  );
  const expectedVisualSources = unique([
    variantPlan.cover.segmentId,
    ...revision.selectedStills.map((still) => still.segmentId),
  ]);
  if (
    !sameSet(artifact.sourceSegmentIds, expectedVideoSources) ||
    !sameSet(artifact.visualSourceSegmentIds, expectedVisualSources)
  ) {
    throw new Error("artifact-lineage-mismatch");
  }
  if (
    (await digestFile(artifact.videoPath)) !== artifact.digest ||
    (await digestFile(artifact.coverPath)) !== artifact.coverDigest ||
    artifact.stillPaths.length !== artifact.stillDigests.length
  ) throw new Error("artifact-bytes-changed");
  for (const [index, path] of artifact.stillPaths.entries()) {
    if ((await digestFile(path)) !== artifact.stillDigests[index]) {
      throw new Error("artifact-bytes-changed");
    }
  }

  const [segments, assets] = await Promise.all([
    library.listSegments(),
    library.listAssets(),
  ]);
  if (calculateRenderInputDigest(revision, segments, assets) !== artifact.renderInputDigest) {
    throw new Error("artifact-render-input-mismatch");
  }
  const segmentById = new Map(segments.map((segment) => [segment.id, segment]));
  const sourceSegments = unique([
    ...artifact.sourceSegmentIds,
    ...artifact.visualSourceSegmentIds,
  ]).map((segmentId) => {
    const segment = segmentById.get(segmentId);
    if (!segment || segment.permissionState !== "allowed") {
      throw new Error("all-source-segments-must-be-allowed");
    }
    return segment;
  });

  const approvalId = `approval_${input.storyId}_${variantToken(input.variant)}_v${input.revision}`;
  const approvedAt = new Date().toISOString();
  const approval = ApprovalSchema.parse({
    id: approvalId,
    storyId: input.storyId,
    revisionId: input.revisionId,
    revision: input.revision,
    artifactId: input.artifactId,
    artifactDigest: artifact.digest,
    coverDigest: artifact.coverDigest,
    stillDigests: artifact.stillDigests,
    renderInputDigest: artifact.renderInputDigest,
    variant: input.variant,
    reviewerId: input.reviewerId,
    approvedAt,
    status: "active",
  });
  return library.commitApproval(approval);
}

function variantToken(variant: VideoVariant): string {
  if (variant === "xiaohongshu") return "xhs";
  if (variant === "douyin") return "douyin";
  if (variant === "wechat-channels") return "channels";
  return "master";
}

function unique<T>(values: T[]): T[] {
  return [...new Set(values)];
}

function sameSet(left: string[], right: string[]): boolean {
  if (new Set(left).size !== left.length || new Set(right).size !== right.length) return false;
  return left.length === right.length && left.every((value) => right.includes(value));
}

async function digestFile(path: string): Promise<string> {
  const hash = createHash("sha256");
  await new Promise<void>((resolve, reject) => {
    const stream = createReadStream(path);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", reject);
    stream.on("end", resolve);
  });
  return hash.digest("hex");
}
