import { createHash } from "node:crypto";

import type {
  ContentSegment,
  HistoricalMaterialConsent,
  LibraryAsset,
} from "@wearable/contracts";

export function creativeEditorialInputDigest(input: Readonly<{
  batchId: string;
  authorization: HistoricalMaterialConsent;
  historicalCandidateDigest: string;
  assets: readonly LibraryAsset[];
}>): string {
  return sha256(JSON.stringify({
    batchId: input.batchId,
    authorization: input.authorization,
    historicalCandidateDigest: input.historicalCandidateDigest,
    assets: [...input.assets]
      .sort((left, right) => left.id.localeCompare(right.id))
      .map((asset) => ({ id: asset.id, digest: asset.digest, permissionState: asset.permissionState })),
  }));
}

export function frozenHistoricalCandidateDigest(candidates: readonly ContentSegment[]): string {
  return sha256(JSON.stringify([...candidates].sort((left, right) => left.id.localeCompare(right.id))));
}

/**
 * Canonical evidence for historical selection. The only deliberately mutable
 * field is usage, which approvals update after a cut is complete. The complete
 * parent asset is bound, including localPath, so moving/replacing source media
 * invalidates unfinished and completed editorial output.
 */
export function frozenHistoricalEvidenceDigest(
  candidates: readonly ContentSegment[],
  assets: readonly LibraryAsset[],
): string {
  const values = [...candidates].sort((left, right) => left.id.localeCompare(right.id)).map((segment) => ({
    segment: segmentWithoutUsage(segment),
    parentAsset: assets.find((asset) => asset.id === segment.assetId) ?? null,
  }));
  return sha256(JSON.stringify(values));
}

export function segmentWithoutUsage(segment: ContentSegment): Omit<ContentSegment, "tags"> & {
  tags: Omit<ContentSegment["tags"], "usage">;
} {
  const { usage: _usage, ...tags } = segment.tags;
  return { ...segment, tags };
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}
