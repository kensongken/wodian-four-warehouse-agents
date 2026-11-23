import type { LibraryAsset } from "@wearable/contracts";

export type CaptureContext = NonNullable<LibraryAsset["captureContext"]>;

export type CreativeCaptureProvenance = Readonly<{
  creativeSessionId: string;
  briefVersion: number;
  scriptVersion: number;
  shotPlanVersion: number;
  stepId: string;
  deviceId: string;
  deviceRole: string;
  carrier: string;
  action: string;
  take: number;
  batchId: string;
}>;

/** Stable lineage for the event and audit produced by one returned capture. */
export function canonicalCreativeCaptureProvenanceKey(value: CreativeCaptureProvenance): string {
  return [
    value.creativeSessionId,
    value.briefVersion,
    value.scriptVersion,
    value.shotPlanVersion,
    value.stepId,
    value.deviceId,
    value.deviceRole,
    value.carrier,
    value.action,
    value.take,
    value.batchId,
  ].map((item) => {
    const string = String(item);
    return `${string.length}:${string}`;
  }).join("|");
}

/** A field-stable identity shared by asset IDs and duplicate detection. */
export function canonicalCaptureContextKey(context: LibraryAsset["captureContext"]): string {
  if (!context) return "legacy";
  return [
    context.creativeSessionId,
    context.stepId,
    context.deviceId,
    context.deviceRole,
    context.carrier,
    context.scriptVersion,
    context.shotPlanVersion,
    context.take,
    context.stepOutcome,
  ].map((value) => encodeURIComponent(String(value))).join("|");
}

export function assetIdForIngest(input: {
  batchId: string;
  sessionId: string;
  digest: string;
  captureContext?: LibraryAsset["captureContext"];
}): string {
  if (!input.captureContext) return `asset_${input.batchId}_${input.sessionId}_${input.digest}`;
  return `asset_${[
    input.batchId,
    input.sessionId,
    input.digest,
    canonicalCaptureContextKey(input.captureContext),
  ].map((value) => encodeURIComponent(value)).join("_")}`;
}

/** Exact retry equality, including the local import metadata and damage flags. */
export function hasSameLibraryAssetIdentity(left: LibraryAsset, right: LibraryAsset): boolean {
  return canonicalLibraryAssetIdentity(left) === canonicalLibraryAssetIdentity(right);
}

function canonicalLibraryAssetIdentity(asset: LibraryAsset): string {
  return [
    asset.id,
    asset.batchId,
    asset.sessionId,
    asset.digest,
    asset.localPath,
    asset.mediaKind,
    asset.importedAt,
    asset.permissionState,
    asset.origin,
    JSON.stringify(asset.technicalFlags),
    canonicalCaptureContextKey(asset.captureContext),
    asset.derivedFromAssetId ?? "",
    JSON.stringify(asset.privacyActions ?? []),
    asset.privacyEvidence?.processor ?? "",
    asset.privacyEvidence?.sourceDigest ?? "",
    asset.privacyEvidence?.outputDigest ?? "",
    asset.privacyEvidence?.processedAt ?? "",
  ].map((value) => encodeURIComponent(value)).join("|");
}
