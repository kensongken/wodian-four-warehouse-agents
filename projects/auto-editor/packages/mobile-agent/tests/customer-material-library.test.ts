import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { MaterialCard } from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  CustomerMaterialLibrary,
  type CustomerMaterialAuthorization,
} from "../src/customer-material-library";

const input = {
  requestId: "import-request-1",
  bytes: Buffer.from("synthetic-jewelry-process-original"),
  mediaKind: "video" as const,
  fileName: "synthetic-process.mp4",
  capturedAt: "2026-09-09T07:00:00.000Z",
  viewPermission: "allowed" as const,
  usePermission: "allowed" as const,
};

describe("customer material library", () => {
  let root: string;
  let library: CustomerMaterialLibrary;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "customer-material-library-"));
    library = new CustomerMaterialLibrary(root, allowEverything);
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("isolates identical originals and denies cross-customer reads", async () => {
    const a = await library.importOriginal("actor-a", "customer-a", input);

    await expect(library.readOriginal("actor-b", "customer-b", a.id))
      .rejects.toThrow("asset-not-found");

    const b = await library.importOriginal("actor-b", "customer-b", input);
    expect(b.id).not.toBe(a.id);
    expect(b.digest).toBe(a.digest);
    await expect(library.listAssets("actor-a", "customer-a")).resolves.toEqual([a]);
    await expect(library.listAssets("actor-b", "customer-b")).resolves.toEqual([b]);
  });

  it("deduplicates content and rejects request conflicts", async () => {
    const first = await library.importOriginal("actor-a", "customer-a", input);
    const contentRetry = await library.importOriginal("actor-a", "customer-a", {
      ...input,
      requestId: "retry-2",
    });

    expect(contentRetry.id).toBe(first.id);
    await expect(library.importOriginal("actor-a", "customer-a", {
      ...input,
      bytes: Buffer.from("different-synthetic-original"),
    })).rejects.toThrow("import-request-conflict");
    await expect(library.listAssets("actor-a", "customer-a")).resolves.toHaveLength(1);
  });

  it("keeps an exact request idempotent after trim normalization", async () => {
    const trimNormalizedInput = {
      ...input,
      fileName: " synthetic-process.mp4 ",
      sessionId: " session-1 ",
      captureContext: {
        envelopeId: " capture-1 ",
        adapterName: " synthetic-adapter ",
        adapterVersion: " 1.0.0 ",
      },
    };

    const first = await library.importOriginal("actor-a", "customer-a", trimNormalizedInput);
    const retry = await library.importOriginal("actor-a", "customer-a", trimNormalizedInput);

    expect(retry).toEqual(first);
    expect(first).toMatchObject({
      fileName: "synthetic-process.mp4",
      sessionId: "session-1",
      captureContext: {
        envelopeId: "capture-1",
        adapterName: "synthetic-adapter",
        adapterVersion: "1.0.0",
      },
    });
  });

  it("creates independent session bindings over one blob for identical bytes", async () => {
    const first = await library.importOriginal("actor-a", "customer-a", {
      ...input,
      sessionId: "session-1",
    });

    const second = await library.importOriginal("actor-a", "customer-a", {
      ...input,
      requestId: "session-2-import",
      sessionId: "session-2",
    });

    expect(second.id).not.toBe(first.id);
    expect(second.importBindingId).toBe(second.id);
    expect(second.sessionId).toBe("session-2");
    expect(second.blobId).toBe(first.blobId);
    expect(second.blobDigest).toBe(first.blobDigest);
    await expect(library.listAssets("actor-a", "customer-a"))
      .resolves.toEqual(expect.arrayContaining([first, second]));
  });

  it("keeps stricter permissions on an independent binding over the same blob", async () => {
    const first = await library.importOriginal("actor-a", "customer-a", input);

    const restricted = await library.importOriginal("actor-a", "customer-a", {
      ...input,
      requestId: "restricted-import",
      sessionId: "restricted-session",
      viewPermission: "allowed",
      usePermission: "blocked",
    });
    expect(restricted.id).not.toBe(first.id);
    expect(restricted.blobId).toBe(first.blobId);
    expect(restricted.usePermission).toBe("blocked");

    const allowedCard = materialCard(first.id, first.digest, "card-allowed");
    const restrictedCard = materialCard(restricted.id, restricted.digest, "card-restricted");
    await library.appendMaterialCard("actor-a", "customer-a", allowedCard);
    await library.appendMaterialCard("actor-a", "customer-a", restrictedCard);

    await expect(library.readOriginal("actor-a", "customer-a", first.id)).resolves.toEqual(input.bytes);
    await expect(library.readOriginal("actor-a", "customer-a", restricted.id)).resolves.toEqual(input.bytes);
    await expect(library.inventoryAt("actor-a", "customer-a", "2026-09-10T00:00:00.000Z"))
      .resolves.toEqual([allowedCard]);
  });

  it("limits session inventory before verifying unrelated original bytes", async () => {
    const current = await library.importOriginal("actor-a", "customer-a", {
      ...input,
      requestId: "current-session-import",
      sessionId: "session-1",
    });
    const unrelated = await library.importOriginal("actor-a", "customer-a", {
      ...input,
      requestId: "unrelated-session-import",
      bytes: Buffer.from("unrelated-session-original"),
      sessionId: "session-2",
    });
    const currentCard = materialCard(current.id, current.digest, "card-current-session");
    const unrelatedCard = {
      ...materialCard(unrelated.id, unrelated.digest, "card-unrelated-session"),
      sessionId: "session-2",
    };
    await library.appendMaterialCard("actor-a", "customer-a", currentCard);
    await library.appendMaterialCard("actor-a", "customer-a", unrelatedCard);

    const customerDirectory = createHash("sha256").update("customer-a").digest("hex");
    const unrelatedOriginal = join(
      root, "customers", customerDirectory, "blobs", unrelated.blobId, "original.bin",
    );
    await writeFile(unrelatedOriginal, "tampered-unrelated-session");

    await expect(library.inventoryAt(
      "actor-a", "customer-a", "2026-09-10T00:00:00.000Z", "session-1",
    )).resolves.toEqual([currentCard]);
    await expect(library.inventoryAt("actor-a", "customer-a", "2026-09-10T00:00:00.000Z"))
      .rejects.toThrow("source-digest-mismatch");
  });

  it("keeps an identical retry idempotent for a legacy request record without metadataDigest", async () => {
    const first = await library.importOriginal("actor-a", "customer-a", input);
    const customerDirectory = createHash("sha256").update("customer-a").digest("hex");
    const requestPath = join(
      root,
      "customers",
      customerDirectory,
      "requests",
      `${createHash("sha256").update(input.requestId).digest("hex")}.json`,
    );
    const legacyRequest = JSON.parse(await readFile(requestPath, "utf8"));
    delete legacyRequest.metadataDigest;
    await writeFile(requestPath, `${JSON.stringify(legacyRequest)}\n`);

    const reopened = new CustomerMaterialLibrary(root, allowEverything);
    await expect(reopened.importOriginal("actor-a", "customer-a", input)).resolves.toEqual(first);
  });

  it("keeps a trim-normalized legacy request idempotent without metadataDigest", async () => {
    const trimNormalizedInput = {
      ...input,
      fileName: " synthetic-process.mp4 ",
      sessionId: " session-1 ",
      captureContext: {
        envelopeId: " capture-legacy ",
        adapterName: " synthetic-legacy-adapter ",
        adapterVersion: " 1.0.0 ",
      },
    };
    const first = await library.importOriginal("actor-a", "customer-a", trimNormalizedInput);
    const customerDirectory = createHash("sha256").update("customer-a").digest("hex");
    const requestPath = join(
      root,
      "customers",
      customerDirectory,
      "requests",
      `${createHash("sha256").update(input.requestId).digest("hex")}.json`,
    );
    const legacyRequest = JSON.parse(await readFile(requestPath, "utf8"));
    delete legacyRequest.metadataDigest;
    await writeFile(requestPath, `${JSON.stringify(legacyRequest)}\n`);

    const reopened = new CustomerMaterialLibrary(root, allowEverything);
    await expect(reopened.importOriginal("actor-a", "customer-a", trimNormalizedInput))
      .resolves.toEqual(first);
  });

  it("derives blob lineage from a fully legacy binding without rewriting old records", async () => {
    const digest = createHash("sha256").update(input.bytes).digest("hex");
    const customerDirectory = createHash("sha256").update("customer-a").digest("hex");
    const legacyAssetId = `asset_${createHash("sha256")
      .update(`customer-a\0${digest}`)
      .digest("hex")}`;
    const customerRoot = join(root, "customers", customerDirectory);
    const legacyAssetDirectory = join(customerRoot, "assets", legacyAssetId);
    const legacyAsset = {
      id: legacyAssetId,
      customerId: "customer-a",
      sessionId: null,
      version: 1,
      digest,
      mediaKind: "video",
      byteLength: input.bytes.byteLength,
      fileName: input.fileName,
      capturedAt: input.capturedAt,
      importedAt: "2026-09-09T08:00:00.000Z",
      viewPermission: "allowed",
      usePermission: "allowed",
    };
    await mkdir(legacyAssetDirectory, { recursive: true });
    await writeFile(join(legacyAssetDirectory, "original.bin"), input.bytes);
    await writeFile(join(legacyAssetDirectory, "asset.json"), `${JSON.stringify(legacyAsset)}\n`);
    const requestDirectory = join(customerRoot, "requests");
    await mkdir(requestDirectory, { recursive: true });
    await writeFile(
      join(requestDirectory, `${createHash("sha256").update(input.requestId).digest("hex")}.json`),
      `${JSON.stringify({
        id: "legacy-request-1",
        customerId: "customer-a",
        version: 1,
        requestId: input.requestId,
        digest,
        assetId: legacyAssetId,
        createdAt: "2026-09-09T08:00:00.000Z",
      })}\n`,
    );

    const reopened = new CustomerMaterialLibrary(root, allowEverything);
    const retried = await reopened.importOriginal("actor-a", "customer-a", input);

    expect(retried).toMatchObject({
      id: legacyAssetId,
      importBindingId: legacyAssetId,
      blobId: `blob_${digest}`,
      blobDigest: digest,
    });
    await expect(reopened.readOriginal("actor-a", "customer-a", legacyAssetId))
      .resolves.toEqual(input.bytes);
    expect(await readFile(join(legacyAssetDirectory, "asset.json"), "utf8"))
      .toBe(`${JSON.stringify(legacyAsset)}\n`);
    await expect(readFile(join(customerRoot, "blobs", `blob_${digest}`, "original.bin")))
      .resolves.toEqual(input.bytes);
  });

  it("atomically converges concurrent imports on one immutable original", async () => {
    const secondLibrary = new CustomerMaterialLibrary(root, allowEverything);
    const [first, second] = await Promise.all([
      library.importOriginal("actor-a", "customer-a", input),
      secondLibrary.importOriginal("actor-a", "customer-a", { ...input, requestId: "parallel-2" }),
    ]);

    expect(second.id).toBe(first.id);
    await expect(library.listAssets("actor-a", "customer-a")).resolves.toHaveLength(1);
  });

  it("checks authorization after writing and rolls back a revoked import", async () => {
    let importChecks = 0;
    let revoked = false;
    const authorize: CustomerMaterialAuthorization = ({ action }) => {
      if (action !== "import" || revoked) return !revoked;
      importChecks += 1;
      if (importChecks === 2) {
        revoked = true;
        return false;
      }
      return true;
    };
    const guarded = new CustomerMaterialLibrary(root, authorize);

    await expect(guarded.importOriginal("actor-a", "customer-a", input))
      .rejects.toThrow("not-authorized");

    revoked = false;
    await expect(guarded.listAssets("actor-a", "customer-a")).resolves.toEqual([]);
  });

  it("never accepts a caller-supplied storage path", async () => {
    await expect(library.importOriginal("actor-a", "customer-a", {
      ...input,
      storagePath: "/tmp/caller-controlled.mp4",
    } as typeof input)).rejects.toThrow("caller-storage-path-not-allowed");
  });

  it("rejects path-like asset IDs before they can escape the customer directory", async () => {
    const escapedBytes = Buffer.from("synthetic-file-outside-customer-vault");
    const escapedDirectory = join(root, "customers", "escape");
    await mkdir(escapedDirectory, { recursive: true });
    await writeFile(join(escapedDirectory, "original.bin"), escapedBytes);
    await writeFile(join(escapedDirectory, "asset.json"), `${JSON.stringify({
      id: "../../escape",
      customerId: "customer-a",
      sessionId: null,
      version: 1,
      digest: createHash("sha256").update(escapedBytes).digest("hex"),
      mediaKind: "video",
      byteLength: escapedBytes.byteLength,
      fileName: "synthetic-escaped.mp4",
      capturedAt: null,
      importedAt: "2026-09-09T08:00:00.000Z",
      viewPermission: "allowed",
      usePermission: "allowed",
    })}\n`);

    await expect(library.readOriginal("actor-a", "customer-a", "../../escape"))
      .rejects.toThrow("asset-id-invalid");
  });

  it("rejects a path-like asset ID stored in a legacy import request", async () => {
    const customerDirectory = createHash("sha256").update("customer-a").digest("hex");
    const requestDirectory = join(root, "customers", customerDirectory, "requests");
    await mkdir(requestDirectory, { recursive: true });
    await writeFile(
      join(requestDirectory, `${createHash("sha256").update(input.requestId).digest("hex")}.json`),
      `${JSON.stringify({
        id: "tampered-request",
        customerId: "customer-a",
        version: 1,
        requestId: input.requestId,
        digest: createHash("sha256").update(input.bytes).digest("hex"),
        assetId: "../../escape",
        createdAt: "2026-09-09T08:00:00.000Z",
      })}\n`,
    );

    await expect(library.importOriginal("actor-a", "customer-a", input))
      .rejects.toThrow("asset-id-invalid");
  });

  it("rejects a path-like blob ID before it can escape the customer directory", async () => {
    const asset = await library.importOriginal("actor-a", "customer-a", input);
    const customerDirectory = createHash("sha256").update("customer-a").digest("hex");
    const recordPath = join(root, "customers", customerDirectory, "assets", asset.id, "asset.json");
    const record = JSON.parse(await readFile(recordPath, "utf8"));
    record.blobId = "../../../escape";
    await writeFile(recordPath, `${JSON.stringify(record)}\n`);
    const escapedDirectory = join(root, "escape");
    await mkdir(escapedDirectory, { recursive: true });
    await writeFile(join(escapedDirectory, "original.bin"), input.bytes);

    await expect(library.readOriginal("actor-a", "customer-a", asset.id)).rejects.toThrow();
  });

  it("detects changed original bytes before returning them", async () => {
    const asset = await library.importOriginal("actor-a", "customer-a", input);
    const customerDirectory = createHash("sha256").update("customer-a").digest("hex");
    const originalPath = join(root, "customers", customerDirectory, "blobs", asset.blobId, "original.bin");
    await writeFile(originalPath, "tampered-synthetic-bytes");

    await expect(library.readOriginal("actor-a", "customer-a", asset.id))
      .rejects.toThrow("source-digest-mismatch");
  });

  it("keeps view and use permissions separate when building an inventory", async () => {
    const visibleOnly = await library.importOriginal("actor-a", "customer-a", {
      ...input,
      usePermission: "blocked",
    });
    const card = materialCard(visibleOnly.id, visibleOnly.digest);
    await library.appendMaterialCard("actor-a", "customer-a", card);

    await expect(library.readOriginal("actor-a", "customer-a", visibleOnly.id))
      .resolves.toEqual(input.bytes);
    await expect(library.inventoryAt("actor-a", "customer-a", "2026-09-10T00:00:00.000Z"))
      .resolves.toEqual([]);
  });

  it("rejects wrong-source cards and never rewrites a material-card version", async () => {
    const asset = await library.importOriginal("actor-a", "customer-a", input);
    const card = materialCard(asset.id, asset.digest);

    await expect(library.appendMaterialCard("actor-a", "customer-a", {
      ...card,
      sourceDigest: "f".repeat(64),
    })).rejects.toThrow("source-digest-mismatch");
    await expect(library.appendMaterialCard("actor-a", "customer-b", card))
      .rejects.toThrow("customer-mismatch");

    await expect(library.appendMaterialCard("actor-a", "customer-a", card)).resolves.toEqual(card);
    await expect(library.appendMaterialCard("actor-a", "customer-a", card)).resolves.toEqual(card);
    await expect(library.appendMaterialCard("actor-a", "customer-a", {
      ...card,
      observations: ["changed synthetic observation"],
    })).rejects.toThrow("immutable-material-card-conflict");
  });

  it("withdraws dependent viewing and use through an immutable event", async () => {
    const asset = await library.importOriginal("actor-a", "customer-a", input);
    const card = materialCard(asset.id, asset.digest);
    await library.appendMaterialCard("actor-a", "customer-a", card);

    const event = await library.withdraw("actor-a", "customer-a", asset.id, "withdraw-op-1");
    const retry = await library.withdraw("actor-a", "customer-a", asset.id, "withdraw-op-1");

    expect(retry).toEqual(event);
    expect(event).toMatchObject({
      customerId: "customer-a",
      assetId: asset.id,
      action: "withdrawn",
      operationKey: "withdraw-op-1",
    });
    await expect(library.readOriginal("actor-a", "customer-a", asset.id))
      .rejects.toThrow("asset-withdrawn");
    await expect(library.listAssets("actor-a", "customer-a")).resolves.toEqual([]);
    await expect(library.inventoryAt("actor-a", "customer-a", "2026-09-10T00:00:00.000Z"))
      .resolves.toEqual([]);
  });

  it("holds the customer lock while independently revalidating exact reference workflow asset bindings", async () => {
    const sampleSource = await library.importOriginal("actor-a", "customer-a", {
      ...input,
      requestId: "reference-guard-sample",
      sessionId: "session-1",
    });
    const renderSource = await library.importOriginal("actor-a", "customer-a", {
      ...input,
      requestId: "reference-guard-render",
      bytes: Buffer.from("independent-render-source"),
      sessionId: "session-1",
    });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let entered = false;
    const guarded = library.withReferenceWorkflowAssets(
      "actor-a",
      "customer-a",
      [{
        role: "sample-source",
        assetId: sampleSource.id,
        assetVersion: sampleSource.version,
        sourceDigest: sampleSource.digest,
      }, {
        role: "render-source",
        assetId: renderSource.id,
        assetVersion: renderSource.version,
        sourceDigest: renderSource.digest,
        permission: {
          version: renderSource.version,
          epoch: renderSource.version,
          view: "allowed",
          use: "allowed",
        },
      }],
      async () => {
        entered = true;
        await gate;
        return "authorized";
      },
    );
    await waitUntil(() => entered);
    let withdrawalFinished = false;
    const withdrawal = library.withdraw(
      "actor-a", "customer-a", renderSource.id, "reference-guard-withdraw",
    ).then((value) => {
      withdrawalFinished = true;
      return value;
    });
    await Promise.resolve();
    expect(withdrawalFinished).toBe(false);
    release();

    await expect(guarded).resolves.toBe("authorized");
    await withdrawal;
    await expect(library.withReferenceWorkflowAssets(
      "actor-a",
      "customer-a",
      [{
        role: "render-source",
        assetId: renderSource.id,
        assetVersion: renderSource.version,
        sourceDigest: renderSource.digest,
        permission: {
          version: renderSource.version,
          epoch: renderSource.version,
          view: "allowed",
          use: "allowed",
        },
      }],
      async () => "must-not-run",
    )).rejects.toThrow("reference-workflow-asset-withdrawn");
  });

  it("fails closed when a reference workflow binding has stale digest, version, view, or use", async () => {
    const allowed = await library.importOriginal("actor-a", "customer-a", {
      ...input,
      requestId: "reference-binding-allowed",
      sessionId: "session-1",
    });
    const useBlocked = await library.importOriginal("actor-a", "customer-a", {
      ...input,
      requestId: "reference-binding-use-blocked",
      bytes: Buffer.from("use-blocked-source"),
      sessionId: "session-1",
      usePermission: "blocked",
    });
    const cases = [{
      role: "sample-source" as const,
      assetId: allowed.id,
      assetVersion: allowed.version,
      sourceDigest: createHash("sha256").update("wrong").digest("hex"),
    }, {
      role: "sample-source" as const,
      assetId: allowed.id,
      assetVersion: allowed.version + 1,
      sourceDigest: allowed.digest,
    }, {
      role: "render-source" as const,
      assetId: useBlocked.id,
      assetVersion: useBlocked.version,
      sourceDigest: useBlocked.digest,
      permission: {
        version: useBlocked.version,
        epoch: useBlocked.version,
        view: "allowed" as const,
        use: "allowed" as const,
      },
    }];
    for (const binding of cases) {
      await expect(library.withReferenceWorkflowAssets(
        "actor-a", "customer-a", [binding], async () => "must-not-run",
      )).rejects.toThrow("reference-workflow-asset-not-current");
    }
  });

  it("binds a withdrawal operation key to the actor that created the event", async () => {
    const asset = await library.importOriginal("actor-a", "customer-a", input);

    const event = await library.withdraw(
      "actor-a",
      "customer-a",
      asset.id,
      "actor-bound-withdraw-op",
    );

    await expect(library.withdraw(
      "actor-b",
      "customer-a",
      asset.id,
      "actor-bound-withdraw-op",
    )).rejects.toThrow("withdrawal-request-conflict");
    await expect(library.withdraw(
      "actor-a",
      "customer-a",
      asset.id,
      "actor-bound-withdraw-op",
    )).resolves.toEqual(event);
    expect(event.actorId).toBe("actor-a");
  });

  it("imports only a verified capture envelope bound to the same customer and session", async () => {
    const envelope = {
      id: "capture-1",
      customerId: "customer-a",
      sessionId: "session-1",
      version: 1 as const,
      requestId: "capture-request-1",
      mediaKind: "video" as const,
      fileName: "synthetic-capture.mp4",
      bytes: new Uint8Array([1, 2, 3]),
      binding: { customerId: "customer-a", sessionId: "session-1", verified: true as const },
      adapter: { name: "synthetic-phone-adapter", version: "1.0.0" },
      capturedAt: "2026-09-09T07:59:00.000Z",
    };

    const asset = await library.importCaptureEnvelope("actor-a", "customer-a", envelope);
    expect(asset.customerId).toBe("customer-a");
    await expect(library.importCaptureEnvelope("actor-b", "customer-b", envelope))
      .rejects.toThrow("capture-customer-mismatch");
  });

  it("defaults capture envelopes to review-required instead of allowed", async () => {
    const envelope = captureEnvelope();

    const asset = await library.importCaptureEnvelope("actor-a", "customer-a", envelope);

    expect(asset).toMatchObject({
      viewPermission: "review-required",
      usePermission: "review-required",
    });
    await expect(library.readOriginal("actor-a", "customer-a", asset.id))
      .rejects.toThrow("asset-view-not-allowed");
    await expect(library.inventoryAt("actor-a", "customer-a", "2026-09-10T00:00:00.000Z"))
      .resolves.toEqual([]);
  });

  it("creates independent capture bindings over one blob for different capture contexts", async () => {
    const first = await library.importCaptureEnvelope("actor-a", "customer-a", captureEnvelope());

    const second = await library.importCaptureEnvelope("actor-a", "customer-a", {
      ...captureEnvelope(),
      id: "capture-2",
      requestId: "capture-request-2",
      adapter: { name: "synthetic-watch-adapter", version: "2.0.0" },
    });

    expect(second.id).not.toBe(first.id);
    expect(second.blobId).toBe(first.blobId);
    expect(second.captureContext).not.toEqual(first.captureContext);
    expect(second.viewPermission).toBe("review-required");
    await expect(library.readOriginal("actor-a", "customer-a", second.id))
      .rejects.toThrow("asset-view-not-allowed");
  });

  it("stores no original bytes inside immutable JSON records", async () => {
    const asset = await library.importOriginal("actor-a", "customer-a", input);
    const customerDirectory = createHash("sha256").update("customer-a").digest("hex");
    const recordPath = join(root, "customers", customerDirectory, "assets", asset.id, "asset.json");

    const record = await readFile(recordPath, "utf8");
    expect(record).not.toContain(input.bytes.toString("utf8"));
    expect(JSON.parse(record)).toEqual(asset);
  });
});

const allowEverything: CustomerMaterialAuthorization = () => true;

function materialCard(
  sourceAssetId: string,
  sourceDigest: string,
  id = "card-1",
): MaterialCard {
  return {
    id,
    customerId: "customer-a",
    sessionId: "session-1",
    version: 1,
    sourceAssetId,
    sourceDigest,
    sourceStartMs: 0,
    sourceEndMs: 10_000,
    contentType: "jewelry-making-process",
    technicalUsable: true,
    technicalFlags: [],
    observations: ["synthetic declaration: polishing"],
    confirmedFacts: [],
    pendingFacts: ["material-name"],
    capability: {
      mode: "offline-fallback",
      label: "离线/模拟分析",
      version: 1,
      provider: null,
    },
    createdAt: "2026-09-09T08:00:00.000Z",
  };
}

function captureEnvelope() {
  return {
    id: "capture-1",
    customerId: "customer-a",
    sessionId: "session-1",
    version: 1 as const,
    requestId: "capture-request-1",
    mediaKind: "video" as const,
    fileName: "synthetic-capture.mp4",
    bytes: new Uint8Array([1, 2, 3]),
    binding: { customerId: "customer-a", sessionId: "session-1", verified: true as const },
    adapter: { name: "synthetic-phone-adapter", version: "1.0.0" },
    capturedAt: "2026-09-09T07:59:00.000Z",
  };
}

async function waitUntil(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  }
  throw new Error("test-timeout");
}
