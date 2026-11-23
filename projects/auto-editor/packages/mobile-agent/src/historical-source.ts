import { createHash } from "node:crypto";

import {
  ContentSegmentSchema,
  LibraryAssetSchema,
  type ContentSegment,
  type CreativeSession,
  type LibraryAsset,
} from "@wearable/contracts";

declare const historicalMaterialSourceSnapshotBrand: unique symbol;

export type HistoricalMaterialSourceSnapshot = Readonly<{
  segmentId: string;
  assetId: string;
  batchId: string;
  /**
   * SHA-256 of the complete schema-parsed segment and parent asset records
   * supplied to the matcher. This intentionally includes localPath in A-0.
   */
  contentDigest: string;
  readonly [historicalMaterialSourceSnapshotBrand]: true;
}>;

export type HistoricalMaterialCatalog = Readonly<{
  segments: ContentSegment[];
  sources: HistoricalMaterialSourceSnapshot[];
}>;

export function isEligibleHistoricalSegmentRecord(
  segment: ContentSegment | undefined,
): segment is ContentSegment {
  return segment !== undefined &&
    segment.permissionState === "allowed" &&
    segment.tags.technicalFlags.length === 0 &&
    (segment.tags.products.length === 0 ||
      segment.tags.products.every((product) => product.status === "confirmed"));
}

export function isEligibleHistoricalSource(
  segment: ContentSegment | undefined,
  asset: LibraryAsset | undefined,
): segment is ContentSegment {
  return isEligibleHistoricalSegmentRecord(segment) && asset !== undefined &&
    asset.permissionState === "allowed" &&
    asset.origin === "historical" &&
    asset.batchId === segment.batchId;
}

/**
 * Resolves the complete material-matcher catalog for one creative session.
 * Explicitly unavailable segments are not candidates. A segment that is
 * otherwise eligible, and is not from this session's capture batches, must
 * still have a valid historical parent or the catalog is corrupt/forbidden.
 */
export function resolveHistoricalMaterialCatalog(
  session: CreativeSession,
  segments: readonly ContentSegment[],
  assets: readonly LibraryAsset[],
  sessions: readonly CreativeSession[] = [session],
): HistoricalMaterialCatalog {
  const parsedSegments = ContentSegmentSchema.array().parse(segments);
  const parsedAssets = LibraryAssetSchema.array().parse(assets);
  const creativeCaptureBatches = new Set(sessions.flatMap((item) => item.captureBatchIds));
  const assetsById = new Map(parsedAssets.map((asset) => [asset.id, asset]));
  const candidates = parsedSegments.filter((segment) =>
    !creativeCaptureBatches.has(segment.batchId) && isEligibleHistoricalSegmentRecord(segment));
  if (candidates.some((segment) =>
    !isEligibleHistoricalSource(segment, assetsById.get(segment.assetId)))) {
    throw new Error("historical-material-catalog-invalid");
  }
  const ordered = [...candidates].sort((left, right) => left.id.localeCompare(right.id));
  return {
    segments: ordered,
    sources: ordered.map((segment) => {
      const asset = assetsById.get(segment.assetId)!;
      return {
        segmentId: segment.id,
        assetId: segment.assetId,
        batchId: segment.batchId,
        contentDigest: historicalSourceContentDigest(segment, asset),
      } as HistoricalMaterialSourceSnapshot;
    }),
  };
}

export function canonicalHistoricalMaterialSources(
  sources: readonly HistoricalMaterialSourceSnapshot[],
): string {
  if (!Array.isArray(sources) || sources.some((source) =>
    typeof source !== "object" || source === null) ||
    new Set(sources.map((source) => source.segmentId)).size !== sources.length ||
    sources.some((source) =>
      typeof source.segmentId !== "string" || source.segmentId.length === 0 ||
      typeof source.assetId !== "string" || source.assetId.length === 0 ||
      typeof source.batchId !== "string" || source.batchId.length === 0 ||
      typeof source.contentDigest !== "string" || !/^[a-f0-9]{64}$/.test(source.contentDigest))) {
    throw new Error("material-suggestion-precondition-invalid");
  }
  return stableSerialize([...sources].sort((left, right) =>
    left.segmentId.localeCompare(right.segmentId)));
}

function historicalSourceContentDigest(segment: ContentSegment, asset: LibraryAsset): string {
  const parsed = {
    segment: ContentSegmentSchema.parse(segment),
    asset: LibraryAssetSchema.parse(asset),
  };
  return createHash("sha256")
    .update("historical-material-source-v1\0")
    .update(stableSerialize(parsed))
    .digest("hex");
}

function stableSerialize(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

function canonicalize(value: unknown): unknown {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("historical-material-source-not-canonical");
    return value;
  }
  if (Array.isArray(value)) return value.map(canonicalize);
  if (typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, canonicalize(item)]));
  }
  throw new Error("historical-material-source-not-canonical");
}
