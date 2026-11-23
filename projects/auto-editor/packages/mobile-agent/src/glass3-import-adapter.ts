import { createHash } from "node:crypto";
import { isAbsolute } from "node:path";

export type Glass3ImportOrigin = "phone-local-original" | "preview-stream";

/**
 * Metadata supplied by the phone after a Glass3 file has already been imported.
 * This adapter deliberately has no Glass3 SDK, transport, or preview-stream API.
 */
export type Glass3ImportManifest = Readonly<{
  origin: Glass3ImportOrigin;
  deviceModel: string;
  importedAt: string;
  digest: string;
}>;

export type ImportedMedia = Readonly<{
  id: string;
  origin: "phone-local-original";
  localPath: string;
  originalBytes: Buffer;
  deviceModel: string;
  importedAt: string;
  digest: string;
}>;

export function importGlass3LocalOriginal(input: {
  localPath: string;
  originalBytes: Buffer;
  manifest: Glass3ImportManifest;
}): ImportedMedia {
  const { localPath, originalBytes, manifest } = input;
  if (manifest.origin !== "phone-local-original") {
    throw new Error("glass3-local-original-required");
  }
  if (!isAbsolute(localPath) || !Buffer.isBuffer(originalBytes)) {
    throw new Error("glass3-local-file-required");
  }
  if (!manifest.deviceModel.trim() || !isValidTimestamp(manifest.importedAt)) {
    throw new Error("glass3-source-metadata-required");
  }

  const digest = sha256(originalBytes);
  if (manifest.digest !== digest) {
    throw new Error("glass3-digest-mismatch");
  }

  return {
    id: `glass3_${digest}`,
    origin: "phone-local-original",
    localPath,
    originalBytes: Buffer.from(originalBytes),
    deviceModel: manifest.deviceModel,
    importedAt: manifest.importedAt,
    digest,
  };
}

/** Rechecks the invariant at the catalog boundary to reject forged inputs. */
export function assertVerifiedGlass3LocalOriginal(media: ImportedMedia): void {
  if (media.origin !== "phone-local-original") {
    throw new Error("glass3-local-original-required");
  }
  if (!isAbsolute(media.localPath) || !Buffer.isBuffer(media.originalBytes)) {
    throw new Error("glass3-local-file-required");
  }
  if (!media.deviceModel.trim() || !isValidTimestamp(media.importedAt)) {
    throw new Error("glass3-source-metadata-required");
  }
  if (sha256(media.originalBytes) !== media.digest) {
    throw new Error("glass3-digest-mismatch");
  }
}

export function sha256(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function isValidTimestamp(value: string): boolean {
  return !Number.isNaN(Date.parse(value));
}
