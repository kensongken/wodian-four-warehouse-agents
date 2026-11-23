import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { CaptureSession } from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ingestNewBatch } from "../src/batch-ingest";
import { importGlass3LocalOriginal } from "../src/glass3-import-adapter";
import { JsonContentStore } from "../src/json-content-store";

describe("content library", () => {
  let testDirectory: string;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "wearable-content-library-"));
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("reuses the existing asset when the same digest is imported twice", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const batchInput = await createBatchInput(testDirectory);

    const first = await ingestNewBatch(batchInput, store);
    const second = await ingestNewBatch(batchInput, store);

    expect(second.assets[0].id).toBe(first.assets[0].id);
    expect(await store.listAssets()).toHaveLength(1);
  });

  it("reloads committed assets without storing original bytes in JSON", async () => {
    const path = join(testDirectory, "library.json");
    const store = await JsonContentStore.open(path);
    const batchInput = await createBatchInput(testDirectory);

    await ingestNewBatch(batchInput, store);
    const reopened = await JsonContentStore.open(path);

    expect((await reopened.listAssets()).map((asset) => asset.digest)).toEqual([
      batchInput.media[0].digest,
    ]);
    expect(await readFile(path, "utf8")).not.toContain("original-video-file");
  });

  it("keeps separate provenance when identical bytes arrive in another batch", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const first = await createBatchInput(testDirectory);
    const second = {
      ...first,
      batchId: "batch_02",
      session: { ...first.session, id: "cap_creator_02" },
    };

    await ingestNewBatch(first, store);
    await ingestNewBatch(second, store);

    expect((await store.listAssets()).map((asset) => asset.batchId)).toEqual([
      "batch_01",
      "batch_02",
    ]);
  });

  it("does not allow a stored raw asset path or digest to be replaced", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const input = await createBatchInput(testDirectory);
    const asset = (await ingestNewBatch(input, store)).assets[0];
    await expect(store.putAsset({
      ...asset,
      localPath: join(testDirectory, "replacement.mp4"),
      digest: "replacement-digest",
    })).rejects.toThrow("immutable-asset-conflict");
    const withdrawn = { ...asset, id: `${asset.id}_withdrawn`, permissionState: "withdrawn" as const };
    await store.putAsset(withdrawn);
    await expect(store.putAsset({ ...withdrawn, permissionState: "allowed" }))
      .rejects.toThrow("immutable-asset-conflict");
  });

  it("rejects a stale second writer instead of silently losing data", async () => {
    const path = join(testDirectory, "library.json");
    const firstStore = await JsonContentStore.open(path);
    const secondStore = await JsonContentStore.open(path);
    const input = await createBatchInput(testDirectory);
    const asset = (await ingestNewBatch(input, firstStore)).assets[0];
    await expect(secondStore.putAsset({ ...asset, id: `${asset.id}_second` }))
      .rejects.toThrow("stale-content-store");
    await expect((await JsonContentStore.open(path)).listAssets()).resolves.toEqual([asset]);
  });

  it("rejects every queued write after an instance becomes stale", async () => {
    const path = join(testDirectory, "library.json");
    const current = await JsonContentStore.open(path);
    const stale = await JsonContentStore.open(path);
    const input = await createBatchInput(testDirectory);
    const asset = (await ingestNewBatch(input, current)).assets[0];
    const results = await Promise.allSettled([
      stale.putAsset({ ...asset, id: `${asset.id}_stale_b`, sessionId: "stale_b" }),
      stale.putAsset({ ...asset, id: `${asset.id}_stale_c`, sessionId: "stale_c" }),
    ]);
    expect(results.map((result) => result.status)).toEqual(["rejected", "rejected"]);
    await expect(stale.listAssets()).resolves.toEqual([asset]);
    await expect((await JsonContentStore.open(path)).listAssets()).resolves.toEqual([asset]);
  });

  it("fails closed on an invalid orphan lock instead of racing to delete it", async () => {
    const path = join(testDirectory, "library.json");
    await writeFile(`${path}.lock`, "orphaned-lock-without-owner");
    const store = await JsonContentStore.open(path);
    const input = await createBatchInput(testDirectory);
    await expect(ingestNewBatch(input, store)).rejects.toThrow("orphaned-content-store-lock");
    expect(await readFile(`${path}.lock`, "utf8")).toBe("orphaned-lock-without-owner");
  });

  it("never steals an old lock while its owner process is alive", async () => {
    const path = join(testDirectory, "library.json");
    await writeFile(`${path}.lock`, JSON.stringify({
      token: "live-owner", pid: process.pid, createdAt: Date.now() - 60_000,
    }));
    const store = await JsonContentStore.open(path);
    const input = await createBatchInput(testDirectory);
    await expect(ingestNewBatch(input, store)).rejects.toThrow("content-store-lock-timeout");
    expect(await readFile(`${path}.lock`, "utf8")).toContain("live-owner");
  });
});

async function createBatchInput(testDirectory: string) {
  const originalBytes = Buffer.from("original-video-file");
  const localPath = join(testDirectory, "GLASS3_001.mp4");
  await writeFile(localPath, originalBytes);
  const digest = createHash("sha256").update(originalBytes).digest("hex");
  const media = importGlass3LocalOriginal({
    localPath,
    originalBytes,
    manifest: {
      origin: "phone-local-original",
      deviceModel: "Rokid Glass3-test-unit",
      importedAt: "2026-09-03T10:02:00.000Z",
      digest,
    },
  });
  const session: CaptureSession = {
    id: "cap_creator_01",
    startedAt: "2026-09-03T10:00:00.000Z",
    mode: "daily-pendant",
    privacyState: "creator",
    visitorConsent: "not-required",
    status: "stopped",
    productRef: null,
  };

  return {
    batchId: "batch_01",
    session,
    media: [media],
    mediaKinds: { [media.id]: "video" as const },
  };
}
