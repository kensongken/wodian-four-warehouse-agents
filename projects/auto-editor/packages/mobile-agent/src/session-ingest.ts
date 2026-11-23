import {
  CaptureSessionSchema,
  type CaptureSession,
} from "@wearable/contracts";

import {
  AssetType,
  createAssetRecord,
  type AssetRecord,
  type AssetType as AssetTypeValue,
} from "./asset-catalog";
import {
  assertVerifiedGlass3LocalOriginal,
  sha256,
  type ImportedMedia,
} from "./glass3-import-adapter";

export type SessionIngestOptions = Readonly<{
  type?: AssetTypeValue;
  sceneTags?: string[];
}>;

/**
 * Ingests later-restored CaptureSession segments without assuming a Glass3 SDK.
 */
export function ingestSession(
  session: CaptureSession,
  segments: readonly Buffer[],
  options: SessionIngestOptions = {},
): AssetRecord {
  const checkedSession = checkIngestibleSession(session);
  if (segments.length === 0) {
    throw new Error("capture-segments-required");
  }

  return createAssetRecord({
    sessionId: checkedSession.id,
    type: resolveAssetType(checkedSession, options.type),
    sceneTags: options.sceneTags,
    privacyState: checkedSession.privacyState,
    source: {
      kind: "capture-session-segments",
      digests: segments.map((segment) => sha256(Buffer.from(segment))),
    },
    suggestedProductId: checkedSession.productRef,
  });
}

/**
 * Ingests only phone-local, checksum-verified Glass3 originals created by the
 * import adapter. There is intentionally no device connection or preview path.
 */
export function ingestImportedMedia(
  session: CaptureSession,
  media: readonly ImportedMedia[],
  options: SessionIngestOptions = {},
): AssetRecord {
  const checkedSession = checkIngestibleSession(session);
  if (media.length === 0) {
    throw new Error("imported-media-required");
  }
  media.forEach(assertVerifiedGlass3LocalOriginal);

  return createAssetRecord({
    sessionId: checkedSession.id,
    type: resolveAssetType(checkedSession, options.type),
    sceneTags: options.sceneTags,
    privacyState: checkedSession.privacyState,
    source: {
      kind: "glass3-local-original",
      digests: media.map((item) => item.digest),
    },
    suggestedProductId: checkedSession.productRef,
  });
}

type IngestibleSession = CaptureSession & {
  privacyState: "creator" | "visitor";
};

function checkIngestibleSession(session: CaptureSession): IngestibleSession {
  const checked = CaptureSessionSchema.parse(session);
  if (isIngestibleSession(checked)) {
    return checked;
  }
  throw new Error("privacy-paused-session-not-ingestable");
}

function isIngestibleSession(
  session: CaptureSession,
): session is IngestibleSession {
  return session.privacyState === "creator" || session.privacyState === "visitor";
}

function resolveAssetType(
  session: CaptureSession,
  requested: AssetTypeValue | undefined,
): AssetTypeValue {
  if (requested) {
    if (!AssetType.includes(requested)) {
      throw new Error("asset-type-not-permitted");
    }
    return requested;
  }

  switch (session.mode) {
    case "maker-brooch":
      return "handmade";
    case "creator-necklace":
      return "persona-copy";
    case "daily-pendant":
      return "scene";
  }
}
