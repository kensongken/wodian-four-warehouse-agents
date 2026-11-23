import {
  CaptureSessionSchema,
  LibraryAssetSchema,
  type CaptureSession,
  type LibraryAsset,
} from "@wearable/contracts";
import { readFile } from "node:fs/promises";

import type { ContentLibrary } from "./content-library";
import { assetIdForIngest } from "./asset-identity";
import {
  assertVerifiedGlass3LocalOriginal,
  sha256,
  type ImportedMedia,
} from "./glass3-import-adapter";

export type IngestBatchInput = Readonly<{
  batchId: string;
  session: CaptureSession;
  media: readonly ImportedMedia[];
  mediaKinds: Readonly<Record<string, "video" | "image" | "audio">>;
  captureContext?: NonNullable<LibraryAsset["captureContext"]>;
}>;

export type IngestBatchResult = Readonly<{
  batchId: string;
  assets: LibraryAsset[];
}>;

export async function ingestNewBatch(
  input: IngestBatchInput,
  library: ContentLibrary,
): Promise<IngestBatchResult> {
  const assets = await prepareNewBatchAssets(input);
  const stored: LibraryAsset[] = [];
  for (const asset of assets) stored.push(await library.putAsset(asset));
  return { batchId: input.batchId, assets: stored };
}

/**
 * Runs the same local-original and checksum checks as batch ingest without
 * writing. Creative capture uses this before its single library transaction.
 */
export async function prepareNewBatchAssets(
  input: IngestBatchInput,
): Promise<LibraryAsset[]> {
  if (!input.batchId.trim()) throw new Error("batch-id-required");
  const session = CaptureSessionSchema.parse(input.session);
  if (session.privacyState === "paused") {
    throw new Error("privacy-paused-session-not-ingestable");
  }

  const assets: LibraryAsset[] = [];
  for (const media of input.media) {
    assertVerifiedGlass3LocalOriginal(media);
    const technicalFlags: string[] = [];
    try {
      const fileDigest = sha256(await readFile(media.localPath));
      if (fileDigest !== media.digest) technicalFlags.push("glass3-path-digest-mismatch");
    } catch {
      technicalFlags.push("glass3-local-file-unreadable");
    }
    const mediaKind = input.mediaKinds[media.id];
    if (!mediaKind) throw new Error("media-kind-required");

    const asset = LibraryAssetSchema.parse({
      id: assetIdForIngest({
        batchId: input.batchId,
        sessionId: session.id,
        digest: media.digest,
        captureContext: input.captureContext,
      }),
      batchId: input.batchId,
      sessionId: session.id,
      digest: media.digest,
      localPath: media.localPath,
      mediaKind,
      importedAt: media.importedAt,
      permissionState:
        session.privacyState === "visitor" ? "review-required" : "allowed",
      origin: "new",
      technicalFlags,
      captureContext: input.captureContext,
    });
    assets.push(asset);
  }
  return assets;
}
