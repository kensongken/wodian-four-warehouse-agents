import { createHash } from "node:crypto";

import { describe, expect, it } from "vitest";

import {
  importGlass3LocalOriginal,
  type Glass3ImportManifest,
} from "../src/glass3-import-adapter";
import {
  ingestImportedMedia,
  ingestSession,
} from "../src/session-ingest";
import {
  canExportToPublic,
  canRouteToProductSystems,
  confirmProductAssociation,
  isAvailableToStoryLibrary,
  rejectProductAssociation,
} from "../src/asset-catalog";
import type { CaptureSession } from "@wearable/contracts";

const visitorSession: CaptureSession = {
  id: "cap_visitor_01",
  startedAt: "2026-09-03T09:00:00.000Z",
  mode: "maker-brooch",
  privacyState: "visitor",
  visitorConsent: "confirmed",
  status: "stopped",
  productRef: "prod_turquoise_01",
};

const creatorSession: CaptureSession = {
  id: "cap_creator_01",
  startedAt: "2026-09-03T10:00:00.000Z",
  mode: "daily-pendant",
  privacyState: "creator",
  visitorConsent: "not-required",
  status: "stopped",
  productRef: null,
};

function manifestFor(bytes: Buffer): Glass3ImportManifest {
  return {
    origin: "phone-local-original",
    deviceModel: "Rokid Glass3-test-unit",
    importedAt: "2026-09-03T10:02:00.000Z",
    digest: createHash("sha256").update(bytes).digest("hex"),
  };
}

describe("Glass3 local-original intake and asset catalog", () => {
  it("stores handmade visitor material as review-required and only suggested", () => {
    const asset = ingestSession(visitorSession, [Buffer.from("clip")]);

    expect(asset.type).toBe("handmade");
    expect(asset.publicUse).toBe("review-required");
    expect(asset.productAssociation.status).toBe("suggested");
    expect(asset.productAssociation.confirmedProductId).toBeNull();
  });

  it("accepts only an already-imported local original whose digest matches", () => {
    const bytes = Buffer.from("original-video-file");
    const imported = importGlass3LocalOriginal({
      localPath: "/private/var/mobile/Media/DCIM/GLASS3_001.mp4",
      originalBytes: bytes,
      manifest: manifestFor(bytes),
    });
    const asset = ingestImportedMedia(creatorSession, [imported]);

    expect(imported.origin).toBe("phone-local-original");
    expect(asset.source.kind).toBe("glass3-local-original");
    expect(asset.source.digests).toEqual([manifestFor(bytes).digest]);
  });

  it("rejects preview streams and mismatched local-original digests", () => {
    const bytes = Buffer.from("original-video-file");
    expect(() =>
      importGlass3LocalOriginal({
        localPath: "/private/var/mobile/Media/DCIM/GLASS3_001.mp4",
        originalBytes: bytes,
        manifest: { ...manifestFor(bytes), origin: "preview-stream" },
      }),
    ).toThrow("glass3-local-original-required");

    expect(() =>
      importGlass3LocalOriginal({
        localPath: "/private/var/mobile/Media/DCIM/GLASS3_001.mp4",
        originalBytes: bytes,
        manifest: { ...manifestFor(bytes), digest: "not-the-file-digest" },
      }),
    ).toThrow("glass3-digest-mismatch");
  });

  it("refuses a forged media object that did not come from a verified local original", () => {
    const bytes = Buffer.from("original-video-file");
    const imported = importGlass3LocalOriginal({
      localPath: "/private/var/mobile/Media/DCIM/GLASS3_001.mp4",
      originalBytes: bytes,
      manifest: manifestFor(bytes),
    });

    expect(() =>
      ingestImportedMedia(creatorSession, [
        { ...imported, origin: "preview-stream" } as unknown as typeof imported,
      ]),
    ).toThrow("glass3-local-original-required");
  });

  it("keeps unconfirmed visitor material out of public export", () => {
    const asset = ingestSession(visitorSession, [Buffer.from("clip")]);

    expect(canExportToPublic(asset)).toBe(false);
  });

  it("does not route a rejected product suggestion to product systems", () => {
    const asset = ingestSession(visitorSession, [Buffer.from("clip")]);
    const rejected = rejectProductAssociation(asset);

    expect(rejected.productAssociation.status).toBe("rejected");
    expect(canRouteToProductSystems(rejected)).toBe(false);
  });

  it("keeps unassigned creator material in the story library", () => {
    const asset = ingestSession(creatorSession, [Buffer.from("scene-clip")]);

    expect(asset.productAssociation.status).toBe("unassigned");
    expect(isAvailableToStoryLibrary(asset)).toBe(true);
  });

  it("requires an explicit designer confirmation before product routing", () => {
    const asset = ingestSession(visitorSession, [Buffer.from("clip")]);
    const confirmed = confirmProductAssociation(asset, "prod_turquoise_01");

    expect(confirmed.productAssociation).toMatchObject({
      status: "confirmed",
      confirmedProductId: "prod_turquoise_01",
    });
    expect(canRouteToProductSystems(confirmed)).toBe(true);
  });
});
