import { createHash, randomUUID } from "node:crypto";
import { open as openFile, link, mkdir, readFile, readdir, realpath, rename, unlink, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

import {
  CaptureEnvelopeV1Schema,
  MaterialCardSchema,
  StudioMediaAssetSchema,
  type CaptureEnvelopeV1Input,
  type MaterialCard,
  type StudioMediaAsset,
  type StudioPermissionState,
} from "@wearable/contracts";

export type CustomerMaterialAction = "import" | "view" | "use" | "withdraw";

export type CustomerMaterialAuthorizationRequest = Readonly<{
  actorId: string;
  customerId: string;
  action: CustomerMaterialAction;
  phase: "before" | "after";
  assetId?: string;
}>;

export type CustomerMaterialAuthorization = (
  request: CustomerMaterialAuthorizationRequest,
) => boolean | Promise<boolean>;

export type ImportOriginalInput = Readonly<{
  requestId: string;
  bytes: Uint8Array;
  mediaKind: "video" | "image" | "audio";
  fileName: string;
  capturedAt: string | null;
  sessionId?: string | null;
  viewPermission?: StudioPermissionState;
  usePermission?: StudioPermissionState;
  captureContext?: Readonly<{
    envelopeId: string;
    adapterName: string;
    adapterVersion: string;
  }> | null;
}>;

export type MaterialWithdrawalEvent = Readonly<{
  id: string;
  customerId: string;
  assetId: string;
  version: 1;
  action: "withdrawn";
  operationKey: string;
  actorId: string;
  createdAt: string;
}>;

declare const OUTPUT_USE_ANALYSIS_CAPABILITY: unique symbol;
declare const REFERENCE_VIEW_ANALYSIS_CAPABILITY: unique symbol;

export type StudioAnalysisAssetSnapshot = Readonly<{
  asset: StudioMediaAsset;
  sourcePath: string;
  authorization: "output-use-authorized";
  [OUTPUT_USE_ANALYSIS_CAPABILITY]: true;
}>;

export type ReferenceAnalysisAssetSnapshot = Readonly<{
  asset: StudioMediaAsset;
  sourcePath: string;
  authorization: "reference-view-authorized";
  [REFERENCE_VIEW_ANALYSIS_CAPABILITY]: true;
}>;

export type ReferenceOriginalSnapshot = Readonly<{
  asset: StudioMediaAsset;
  bytes: Buffer;
}>;

export type ReferenceWorkflowAssetBinding = Readonly<{
  role: "sample-source";
  assetId: string;
  assetVersion: number;
  sourceDigest: string;
}> | Readonly<{
  role: "render-source";
  assetId: string;
  assetVersion: number;
  sourceDigest: string;
  permission: Readonly<{
    version: number;
    epoch: number;
    view: "allowed";
    use: "allowed";
  }>;
}>;

export type StudioAnalysisAssetBinding = Readonly<{
  actorId: string;
  customerId: string;
  sessionId: string;
  sourceAssetSessionId: string | null;
  assetId: string;
  sourceDigest: string;
  sourcePath: string;
}>;

type ImportRequestRecord = Readonly<{
  id: string;
  customerId: string;
  version: 1;
  requestId: string;
  digest: string;
  metadataDigest?: string;
  assetId: string;
  createdAt: string;
}>;

type ImmutableWrite = Readonly<{ path: string; created: boolean }>;

export type CustomerMaterialLibraryOptions = Readonly<{
  atomicRename?: (from: string, to: string) => Promise<void>;
  now?: () => string;
}>;

type SourceBlobRecord = Readonly<{
  id: string;
  customerId: string;
  version: 1;
  digest: string;
  byteLength: number;
  createdAt: string;
}>;

/**
 * Customer-scoped, append-only storage for historical and capture originals.
 * Caller-controlled values are hashed before they become directory or file names.
 */
export class CustomerMaterialLibrary {
  private static readonly operationQueues = new Map<string, Promise<void>>();
  private readonly root: string;
  private readonly atomicRename: (from: string, to: string) => Promise<void>;
  private readonly now: () => string;

  constructor(
    root: string,
    private readonly authorize: CustomerMaterialAuthorization,
    options: CustomerMaterialLibraryOptions = {},
  ) {
    if (!root.trim()) throw new Error("material-library-root-required");
    this.root = resolve(root);
    this.atomicRename = options.atomicRename ?? rename;
    this.now = options.now ?? (() => new Date().toISOString());
  }

  async importOriginal(
    actorId: string,
    customerId: string,
    input: ImportOriginalInput,
  ): Promise<StudioMediaAsset> {
    assertScope(actorId, customerId);
    assertNoCallerPath(input);
    assertImportInput(input);
    const bytes = Buffer.from(input.bytes);
    const digest = sha256(bytes);

    return this.withCustomerLock(customerId, async () => {
      await this.assertAuthorized({ actorId, customerId, action: "import", phase: "before" });
      const paths = this.paths(customerId);
      await mkdir(paths.customer, { recursive: true });
      const normalizationId = `asset_${"0".repeat(64)}`;
      const normalizedAsset = StudioMediaAssetSchema.parse({
        id: normalizationId,
        importBindingId: normalizationId,
        blobId: blobIdFor(digest),
        customerId,
        sessionId: input.sessionId ?? null,
        version: 1,
        digest,
        blobDigest: digest,
        mediaKind: input.mediaKind,
        byteLength: bytes.byteLength,
        fileName: input.fileName,
        capturedAt: input.capturedAt,
        importedAt: this.now(),
        viewPermission: input.viewPermission ?? "allowed",
        usePermission: input.usePermission ?? "allowed",
        captureContext: input.captureContext ?? null,
      });
      const metadataDigest = importMetadataDigest(normalizedAsset);
      const generatedAssetId = assetIdFor(customerId, digest, metadataDigest);
      const requestPath = join(paths.requests, `${sha256(input.requestId)}.json`);
      const existingRequest = await readJsonIfPresent<ImportRequestRecord>(requestPath);
      if (existingRequest?.digest !== undefined && existingRequest.digest !== digest) {
        throw new Error("import-request-conflict");
      }
      const assetId = existingRequest?.assetId ?? generatedAssetId;
      assertAssetId(assetId);
      const assetDirectory = join(paths.assets, assetId);
      const assetPath = join(assetDirectory, "asset.json");
      const existingAsset = await readAssetIfPresent(assetPath);
      if (existingRequest && !existingAsset) throw new Error("import-request-conflict");
      if (existingRequest?.metadataDigest && existingRequest.metadataDigest !== metadataDigest) {
        throw new Error("import-request-conflict");
      }
      if (existingAsset && importMetadataDigest(existingAsset) !== metadataDigest) {
        throw new Error(existingRequest ? "import-request-conflict" : "immutable-asset-conflict");
      }
      const blobId = existingAsset?.blobId ?? blobIdFor(digest);
      const requestedAsset = StudioMediaAssetSchema.parse({
        ...normalizedAsset,
        id: assetId,
        importBindingId: assetId,
        blobId,
      });
      const asset = existingAsset ?? requestedAsset;
      if (asset.customerId !== customerId || asset.digest !== digest) {
        throw new Error("immutable-asset-conflict");
      }

      const blobDirectory = join(paths.blobs, blobId);
      const originalPath = join(blobDirectory, "original.bin");
      const blobPath = join(blobDirectory, "blob.json");
      const existingBlob = await readJsonIfPresent<SourceBlobRecord>(blobPath);
      if (existingBlob && (
        existingBlob.id !== blobId ||
        existingBlob.customerId !== customerId ||
        existingBlob.digest !== digest ||
        existingBlob.byteLength !== bytes.byteLength
      )) {
        throw new Error("immutable-source-blob-conflict");
      }
      const blob: SourceBlobRecord = existingBlob ?? {
        id: blobId,
        customerId,
        version: 1,
        digest,
        byteLength: bytes.byteLength,
        createdAt: this.now(),
      };

      const writes: ImmutableWrite[] = [];
      try {
        writes.push(await writeImmutable(originalPath, bytes, "source-digest-mismatch"));
        await assertDigest(originalPath, digest);
        writes.push(await writeImmutableJson(blobPath, blob, "immutable-source-blob-conflict"));
        if (!existingAsset) {
          writes.push(await writeImmutableJson(assetPath, asset, "immutable-asset-conflict"));
        }
        const request: ImportRequestRecord = existingRequest ?? {
          id: `import_${sha256(`${customerId}\0${input.requestId}`)}`,
          customerId,
          version: 1,
          requestId: input.requestId,
          digest,
          metadataDigest,
          assetId,
          createdAt: this.now(),
        };
        if (!existingRequest) {
          writes.push(await writeImmutableJson(requestPath, request, "import-request-conflict"));
        }
        await this.assertAuthorized({ actorId, customerId, action: "import", phase: "after", assetId });
        return asset;
      } catch (error) {
        await rollbackCreated(writes);
        throw error;
      }
    });
  }

  async listAssets(actorId: string, customerId: string): Promise<StudioMediaAsset[]> {
    assertScope(actorId, customerId);
    return this.withCustomerLock(customerId, async () => {
      await this.assertAuthorized({ actorId, customerId, action: "view", phase: "before" });
      const assets = await this.listAssetRecords(customerId);
      const visible: StudioMediaAsset[] = [];
      for (const asset of assets) {
        if (asset.viewPermission !== "allowed") continue;
        if (await this.isWithdrawn(customerId, asset.id)) continue;
        visible.push(asset);
      }
      await this.assertAuthorized({ actorId, customerId, action: "view", phase: "after" });
      return visible;
    });
  }

  async assertReferenceOperationAuthorized(
    actorId: string,
    customerId: string,
    action: "import" | "view",
    phase: "before" | "after",
  ): Promise<void> {
    assertScope(actorId, customerId);
    await this.assertAuthorized({ actorId, customerId, action, phase });
  }

  async readOriginal(actorId: string, customerId: string, assetId: string): Promise<Buffer> {
    assertScope(actorId, customerId);
    assertAssetId(assetId);
    return this.withCustomerLock(customerId, async () => {
      await this.assertAuthorized({ actorId, customerId, action: "view", phase: "before", assetId });
      const asset = await this.requireAsset(customerId, assetId);
      if (await this.isWithdrawn(customerId, asset.id)) throw new Error("asset-withdrawn");
      if (asset.viewPermission !== "allowed") throw new Error("asset-view-not-allowed");
      const bytes = await this.readContainedReferenceBytes(customerId, asset);
      await this.assertAuthorized({ actorId, customerId, action: "view", phase: "after", assetId });
      return bytes;
    });
  }

  async withReferenceOriginal<T>(
    actorId: string,
    customerId: string,
    assetId: string,
    work: (snapshot: ReferenceOriginalSnapshot) => Promise<T> | T,
  ): Promise<T> {
    assertScope(actorId, customerId);
    assertAssetId(assetId);
    return this.withCustomerLock(customerId, async () => {
      await this.assertAuthorized({ actorId, customerId, action: "view", phase: "before", assetId });
      const asset = await this.requireAsset(customerId, assetId);
      if (await this.isWithdrawn(customerId, asset.id)) throw new Error("asset-withdrawn");
      if (asset.viewPermission !== "allowed") throw new Error("asset-view-not-allowed");
      const bytes = await this.readContainedReferenceBytes(customerId, asset);
      const result = await work({ asset, bytes });
      await this.assertAuthorized({ actorId, customerId, action: "view", phase: "after", assetId });
      return result;
    });
  }

  /**
   * Holds the customer-material lock while a caller checks/commits reference
   * workflow state. Callers that also enter the reference store must do so
   * inside `work`, preserving the only supported lock order: library -> store.
   */
  async withReferenceWorkflowAssets<T>(
    actorId: string,
    customerId: string,
    bindingsValue: readonly ReferenceWorkflowAssetBinding[],
    work: () => Promise<T> | T,
  ): Promise<T> {
    assertScope(actorId, customerId);
    const bindings = parseReferenceWorkflowAssetBindings(bindingsValue);
    if (typeof work !== "function") throw new Error("reference-workflow-work-required");
    return this.withCustomerLock(customerId, async () => {
      for (const binding of bindings) {
        await this.assertReferenceWorkflowAssetCurrent(actorId, customerId, binding, "before");
      }
      const result = await work();
      for (const binding of bindings) {
        await this.assertReferenceWorkflowAssetCurrent(actorId, customerId, binding, "after");
      }
      return result;
    });
  }

  async appendMaterialCard(
    actorId: string,
    customerId: string,
    value: MaterialCard,
  ): Promise<MaterialCard> {
    assertScope(actorId, customerId);
    const card = MaterialCardSchema.parse(value);
    if (card.customerId !== customerId) throw new Error("customer-mismatch");

    return this.withCustomerLock(customerId, async () => {
      await this.assertAuthorized({
        actorId, customerId, action: "view", phase: "before", assetId: card.sourceAssetId,
      });
      const asset = await this.requireViewableAsset(customerId, card.sourceAssetId);
      if (asset.digest !== card.sourceDigest) throw new Error("source-digest-mismatch");
      await this.assertSourceDigest(customerId, asset);

      const cardPath = join(
        this.paths(customerId).cards,
        `${sha256(`${card.id}\0${card.version}`)}.json`,
      );
      const write = await writeImmutableJson(cardPath, card, "immutable-material-card-conflict");
      try {
        await this.assertAuthorized({
          actorId, customerId, action: "view", phase: "after", assetId: card.sourceAssetId,
        });
        return card;
      } catch (error) {
        await rollbackCreated([write]);
        throw error;
      }
    });
  }

  async inventoryAt(
    actorId: string,
    customerId: string,
    asOf: string,
    sessionId?: string,
  ): Promise<MaterialCard[]> {
    assertScope(actorId, customerId);
    if (sessionId !== undefined) assertId(sessionId, "session-id-required");
    const asOfTime = Date.parse(asOf);
    if (!Number.isFinite(asOfTime)) throw new Error("inventory-as-of-invalid");

    return this.withCustomerLock(customerId, async () => {
      await this.assertAuthorized({ actorId, customerId, action: "use", phase: "before" });
      const cards = await this.listCards(customerId);
      const eligible: MaterialCard[] = [];
      for (const card of cards) {
        if (sessionId !== undefined && card.sessionId !== sessionId) continue;
        if (Date.parse(card.createdAt) > asOfTime) continue;
        const asset = await this.findAsset(customerId, card.sourceAssetId);
        if (!asset || asset.customerId !== customerId || asset.digest !== card.sourceDigest) continue;
        if (asset.viewPermission !== "allowed" || asset.usePermission !== "allowed") continue;
        if (await this.isWithdrawn(customerId, asset.id)) continue;
        await this.assertSourceDigest(customerId, asset);
        eligible.push(card);
      }
      await this.assertAuthorized({ actorId, customerId, action: "use", phase: "after" });
      return eligible.sort(compareCreatedRecords);
    });
  }

  async withdraw(
    actorId: string,
    customerId: string,
    assetId: string,
    operationKey: string,
  ): Promise<MaterialWithdrawalEvent> {
    assertScope(actorId, customerId);
    assertAssetId(assetId);
    assertId(operationKey, "withdrawal-operation-key-required");

    return this.withCustomerLock(customerId, async () => {
      await this.assertAuthorized({ actorId, customerId, action: "withdraw", phase: "before", assetId });
      await this.requireAsset(customerId, assetId);
      const eventPath = join(this.paths(customerId).withdrawals, `${sha256(operationKey)}.json`);
      const existing = await readJsonIfPresent<MaterialWithdrawalEvent>(eventPath);
      if (existing && (existing.assetId !== assetId || existing.actorId !== actorId)) {
        throw new Error("withdrawal-request-conflict");
      }
      const event: MaterialWithdrawalEvent = existing ?? {
        id: `withdrawal_${sha256(`${customerId}\0${operationKey}`)}`,
        customerId,
        assetId,
        version: 1,
        action: "withdrawn",
        operationKey,
        actorId,
        createdAt: this.now(),
      };
      const write = await writeImmutableJson(eventPath, event, "withdrawal-request-conflict");
      try {
        await this.assertAuthorized({ actorId, customerId, action: "withdraw", phase: "after", assetId });
        return event;
      } catch (error) {
        await rollbackCreated([write]);
        throw error;
      }
    });
  }

  async importCaptureEnvelope(
    actorId: string,
    customerId: string,
    value: CaptureEnvelopeV1Input,
  ): Promise<StudioMediaAsset> {
    assertScope(actorId, customerId);
    const envelope = CaptureEnvelopeV1Schema.parse(value);
    if (envelope.customerId !== customerId || envelope.binding.customerId !== customerId) {
      throw new Error("capture-customer-mismatch");
    }
    if (envelope.sessionId !== envelope.binding.sessionId) {
      throw new Error("capture-session-mismatch");
    }
    return this.importOriginal(actorId, customerId, {
      requestId: envelope.requestId,
      bytes: envelope.bytes,
      mediaKind: envelope.mediaKind,
      fileName: envelope.fileName,
      capturedAt: envelope.capturedAt,
      sessionId: envelope.sessionId,
      viewPermission: envelope.viewPermission,
      usePermission: envelope.usePermission,
      captureContext: {
        envelopeId: envelope.id,
        adapterName: envelope.adapter.name,
        adapterVersion: envelope.adapter.version,
      },
    });
  }

  async inspectAnalysisAsset(
    actorId: string,
    customerId: string,
    sessionId: string,
    assetId: string,
  ): Promise<StudioAnalysisAssetSnapshot> {
    assertScope(actorId, customerId);
    assertId(sessionId, "session-id-required");
    assertAssetId(assetId);
    return this.withCustomerLock(customerId, () =>
      this.inspectAnalysisAssetUnlocked(actorId, customerId, sessionId, assetId));
  }

  async inspectReferenceAnalysisAsset(
    actorId: string,
    customerId: string,
    sessionId: string,
    assetId: string,
  ): Promise<ReferenceAnalysisAssetSnapshot> {
    assertScope(actorId, customerId);
    assertId(sessionId, "session-id-required");
    assertAssetId(assetId);
    return this.withCustomerLock(customerId, () =>
      this.inspectReferenceAnalysisAssetUnlocked(actorId, customerId, sessionId, assetId));
  }

  async startStudioAnalysisRun<T>(
    binding: StudioAnalysisAssetBinding,
    start: (snapshot: StudioAnalysisAssetSnapshot) => Promise<T>,
  ): Promise<{ snapshot: StudioAnalysisAssetSnapshot; result: Promise<T> }> {
    assertAnalysisBinding(binding);
    return this.withCustomerLock(binding.customerId, async () => {
      const snapshot = await this.inspectAnalysisAssetUnlocked(
        binding.actorId, binding.customerId, binding.sessionId, binding.assetId,
      );
      assertAnalysisSnapshotBinding(snapshot, binding);
      // Deliberately invoke start synchronously while the customer lock is held. A worker
      // implementation must spawn before returning its promise.
      const result = start(snapshot);
      return { snapshot, result };
    });
  }

  async startReferenceAnalysisRun<T>(
    binding: StudioAnalysisAssetBinding,
    start: (snapshot: ReferenceAnalysisAssetSnapshot) => Promise<T>,
  ): Promise<{ snapshot: ReferenceAnalysisAssetSnapshot; result: Promise<T> }> {
    assertAnalysisBinding(binding);
    return this.withCustomerLock(binding.customerId, async () => {
      const snapshot = await this.inspectReferenceAnalysisAssetUnlocked(
        binding.actorId, binding.customerId, binding.sessionId, binding.assetId,
      );
      assertReferenceAnalysisSnapshotBinding(snapshot, binding);
      const result = start(snapshot);
      return { snapshot, result };
    });
  }

  async readStudioAnalysisState(
    customerId: string,
    sessionId: string,
  ): Promise<unknown | null> {
    assertId(customerId, "customer-id-required");
    assertId(sessionId, "session-id-required");
    return this.withCustomerLock(customerId, async () => {
      const raw = await readFile(this.analysisStatePath(customerId, sessionId), "utf8").catch(
        (error: unknown) => {
          if (hasCode(error, "ENOENT")) return null;
          throw error;
        },
      );
      return raw === null ? null : JSON.parse(raw);
    });
  }

  async writeStudioAnalysisState(
    customerId: string,
    sessionId: string,
    value: unknown,
  ): Promise<void> {
    assertId(customerId, "customer-id-required");
    assertId(sessionId, "session-id-required");
    await this.withCustomerLock(customerId, () => this.writeStudioAnalysisStateUnlocked(customerId, sessionId, value));
  }

  async writeStudioAnalysisStateIfCurrent(
    binding: StudioAnalysisAssetBinding,
    value: unknown,
    canCommit: () => boolean,
  ): Promise<void> {
    assertAnalysisBinding(binding);
    await this.withCustomerLock(binding.customerId, async () => {
      await this.writeStudioAnalysisStateUnlocked(
        binding.customerId,
        binding.sessionId,
        value,
        async () => {
          const snapshot = await this.inspectAnalysisAssetUnlocked(
            binding.actorId, binding.customerId, binding.sessionId, binding.assetId,
          );
          assertAnalysisSnapshotBinding(snapshot, binding);
        },
        canCommit,
      );
    });
  }

  async writeStudioReferenceAnalysisStateIfCurrent(
    binding: StudioAnalysisAssetBinding,
    value: unknown,
    canCommit: () => boolean,
  ): Promise<void> {
    assertAnalysisBinding(binding);
    await this.withCustomerLock(binding.customerId, async () => {
      await this.writeStudioAnalysisStateUnlocked(
        binding.customerId,
        binding.sessionId,
        value,
        async () => {
          const snapshot = await this.inspectReferenceAnalysisAssetUnlocked(
            binding.actorId, binding.customerId, binding.sessionId, binding.assetId,
          );
          assertReferenceAnalysisSnapshotBinding(snapshot, binding);
        },
        canCommit,
      );
    });
  }

  async readStudioPreferenceState(
    actorId: string,
    customerId: string,
    sessionId: string,
  ): Promise<unknown | null> {
    assertScope(actorId, customerId);
    assertId(sessionId, "session-id-required");
    return this.withCustomerLock(customerId, async () => {
      await this.assertAuthorized({ actorId, customerId, action: "use", phase: "before" });
      const raw = await readFile(
        this.preferenceStatePath(actorId, customerId, sessionId),
        "utf8",
      ).catch((error: unknown) => {
        if (hasCode(error, "ENOENT")) return null;
        throw error;
      });
      await this.assertAuthorized({ actorId, customerId, action: "use", phase: "after" });
      return raw === null ? null : JSON.parse(raw);
    });
  }

  async writeStudioPreferenceState(
    actorId: string,
    customerId: string,
    sessionId: string,
    value: unknown,
  ): Promise<void> {
    assertScope(actorId, customerId);
    assertId(sessionId, "session-id-required");
    await this.withCustomerLock(customerId, async () => {
      await this.assertAuthorized({ actorId, customerId, action: "use", phase: "before" });
      await this.writeStudioAnalysisStateUnlocked(
        customerId,
        sessionId,
        value,
        () => this.assertAuthorized({ actorId, customerId, action: "use", phase: "after" }),
        () => true,
        this.preferenceStatePath(actorId, customerId, sessionId),
      );
    });
  }

  async readStudioDraftFeedbackState(
    actorId: string,
    customerId: string,
    sessionId: string,
  ): Promise<unknown | null> {
    assertScope(actorId, customerId);
    assertId(sessionId, "session-id-required");
    return this.withCustomerLock(customerId, async () => {
      await this.assertAuthorized({ actorId, customerId, action: "use", phase: "before" });
      const raw = await readFile(
        this.draftFeedbackStatePath(actorId, customerId, sessionId),
        "utf8",
      ).catch((error: unknown) => {
        if (hasCode(error, "ENOENT")) return null;
        throw error;
      });
      await this.assertAuthorized({ actorId, customerId, action: "use", phase: "after" });
      return raw === null ? null : JSON.parse(raw);
    });
  }

  async writeStudioDraftFeedbackState(
    actorId: string,
    customerId: string,
    sessionId: string,
    value: unknown,
  ): Promise<void> {
    assertScope(actorId, customerId);
    assertId(sessionId, "session-id-required");
    await this.withCustomerLock(customerId, async () => {
      await this.assertAuthorized({ actorId, customerId, action: "use", phase: "before" });
      await this.writeStudioAnalysisStateUnlocked(
        customerId,
        sessionId,
        value,
        () => this.assertAuthorized({ actorId, customerId, action: "use", phase: "after" }),
        () => true,
        this.draftFeedbackStatePath(actorId, customerId, sessionId),
      );
    });
  }

  studioPreferenceCoordinationKey(actorId: string, customerId: string, sessionId: string): string {
    assertScope(actorId, customerId);
    assertId(sessionId, "session-id-required");
    return sha256(`${this.root}\0${customerId}\0${sessionId}\0${actorId}\0preferences`);
  }

  studioAnalysisCoordinationKey(customerId: string, sessionId: string): string {
    assertId(customerId, "customer-id-required");
    assertId(sessionId, "session-id-required");
    return sha256(`${this.root}\0${customerId}\0${sessionId}`);
  }

  private async assertAuthorized(request: CustomerMaterialAuthorizationRequest): Promise<void> {
    let authorized = false;
    try {
      authorized = await this.authorize(request);
    } catch {
      authorized = false;
    }
    if (!authorized) throw new Error("not-authorized");
  }

  private async inspectAnalysisAssetUnlocked(
    actorId: string,
    customerId: string,
    sessionId: string,
    assetId: string,
  ): Promise<StudioAnalysisAssetSnapshot> {
    await this.assertAuthorized({ actorId, customerId, action: "use", phase: "before", assetId });
    const asset = await this.requireAsset(customerId, assetId);
    if (asset.sessionId !== sessionId) throw new Error("asset-session-mismatch");
    if (await this.isWithdrawn(customerId, asset.id)) throw new Error("asset-withdrawn");
    if (asset.viewPermission !== "allowed" || asset.usePermission !== "allowed") {
      throw new Error("asset-use-not-allowed");
    }
    const sourcePath = await this.sourcePath(customerId, asset);
    await this.assertSourceDigest(customerId, asset);
    await this.assertAuthorized({ actorId, customerId, action: "use", phase: "after", assetId });
    return outputUseAnalysisSnapshot(asset, sourcePath);
  }

  private async assertReferenceWorkflowAssetCurrent(
    actorId: string,
    customerId: string,
    binding: ReferenceWorkflowAssetBinding,
    phase: "before" | "after",
  ): Promise<void> {
    await this.assertAuthorized({
      actorId,
      customerId,
      action: binding.role === "render-source" ? "use" : "view",
      phase,
      assetId: binding.assetId,
    });
    const asset = await this.requireAsset(customerId, binding.assetId);
    if (await this.isWithdrawn(customerId, asset.id)) {
      throw new Error("reference-workflow-asset-withdrawn");
    }
    const exactIdentity = asset.version === binding.assetVersion &&
      asset.digest === binding.sourceDigest && asset.viewPermission === "allowed";
    const exactPermission = binding.role === "sample-source" || (
      asset.usePermission === "allowed" &&
      binding.permission.version === asset.version &&
      binding.permission.epoch === asset.version &&
      binding.permission.view === asset.viewPermission &&
      binding.permission.use === asset.usePermission
    );
    if (!exactIdentity || !exactPermission) {
      throw new Error("reference-workflow-asset-not-current");
    }
    try {
      await this.assertSourceDigest(customerId, asset);
    } catch (error) {
      throw new Error("reference-workflow-asset-not-current", { cause: error });
    }
  }

  private async inspectReferenceAnalysisAssetUnlocked(
    actorId: string,
    customerId: string,
    sessionId: string,
    assetId: string,
  ): Promise<ReferenceAnalysisAssetSnapshot> {
    await this.assertAuthorized({ actorId, customerId, action: "view", phase: "before", assetId });
    const asset = await this.requireAsset(customerId, assetId);
    if (await this.isWithdrawn(customerId, asset.id)) throw new Error("asset-withdrawn");
    if (asset.viewPermission !== "allowed") throw new Error("asset-view-not-allowed");
    const sourcePath = await this.sourcePath(customerId, asset);
    await this.assertSourceDigest(customerId, asset);
    await this.assertAuthorized({ actorId, customerId, action: "view", phase: "after", assetId });
    return referenceAnalysisSnapshot(asset, sourcePath);
  }

  private async writeStudioAnalysisStateUnlocked(
    customerId: string,
    sessionId: string,
    value: unknown,
    verifyCurrent: () => Promise<void> = async () => undefined,
    beforeCommit: () => boolean = () => true,
    targetPath?: string,
  ): Promise<void> {
    const path = targetPath ?? this.analysisStatePath(customerId, sessionId);
    await mkdir(dirname(path), { recursive: true });
    const pending = `${path}.next-${randomUUID()}`;
    try {
      await writeFile(pending, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", flag: "wx" });
      const handle = await openFile(pending, "r+");
      try {
        await handle.sync();
      } finally {
        await handle.close();
      }
      await verifyCurrent();
      if (!beforeCommit()) throw new Error("studio-analysis-cancelled");
      // The rename invocation is the linearization point. Do not insert an await
      // between the final synchronous predicate and starting this atomic replace.
      const commit = this.atomicRename(pending, path);
      await commit;
    } finally {
      await unlink(pending).catch(() => undefined);
    }
  }

  private async requireViewableAsset(customerId: string, assetId: string): Promise<StudioMediaAsset> {
    const asset = await this.requireAsset(customerId, assetId);
    if (await this.isWithdrawn(customerId, assetId)) throw new Error("asset-withdrawn");
    if (asset.viewPermission !== "allowed") throw new Error("asset-view-not-allowed");
    return asset;
  }

  private async requireAsset(customerId: string, assetId: string): Promise<StudioMediaAsset> {
    assertAssetId(assetId);
    const asset = await this.findAsset(customerId, assetId);
    if (!asset || asset.customerId !== customerId) throw new Error("asset-not-found");
    return asset;
  }

  private async findAsset(customerId: string, assetId: string): Promise<StudioMediaAsset | null> {
    assertAssetId(assetId);
    return readAssetIfPresent(join(this.paths(customerId).assets, assetId, "asset.json"));
  }

  private async listAssetRecords(customerId: string): Promise<StudioMediaAsset[]> {
    const directory = this.paths(customerId).assets;
    const entries = await readdir(directory, { withFileTypes: true }).catch(() => []);
    const assets: StudioMediaAsset[] = [];
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const asset = await readAssetIfPresent(join(directory, entry.name, "asset.json"));
      if (asset?.customerId === customerId) assets.push(asset);
    }
    return assets.sort((left, right) => left.id.localeCompare(right.id));
  }

  private async listCards(customerId: string): Promise<MaterialCard[]> {
    const directory = this.paths(customerId).cards;
    const entries = await readdir(directory, { withFileTypes: true }).catch(() => []);
    const cards = await Promise.all(entries.map(async (entry) => {
      if (!entry.isFile() || !entry.name.endsWith(".json")) return null;
      const raw = await readFile(join(directory, entry.name), "utf8");
      const card = MaterialCardSchema.parse(JSON.parse(raw));
      return card.customerId === customerId ? card : null;
    }));
    return cards.filter((card): card is MaterialCard => card !== null);
  }

  private async isWithdrawn(customerId: string, assetId: string): Promise<boolean> {
    const directory = this.paths(customerId).withdrawals;
    const entries = await readdir(directory, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      if (!entry.isFile() || !entry.name.endsWith(".json")) continue;
      const event = await readJsonIfPresent<MaterialWithdrawalEvent>(join(directory, entry.name));
      if (event?.customerId === customerId && event.assetId === assetId && event.action === "withdrawn") {
        return true;
      }
    }
    return false;
  }

  private async readSourceBytes(customerId: string, asset: StudioMediaAsset): Promise<Buffer> {
    const blobPath = join(this.paths(customerId).blobs, asset.blobId, "original.bin");
    const blobBytes = await readFile(blobPath).catch((error: unknown) => {
      if (hasCode(error, "ENOENT")) return null;
      throw error;
    });
    const bytes = blobBytes ?? await readFile(
      join(this.paths(customerId).assets, asset.id, "original.bin"),
    ).catch(() => {
      throw new Error("asset-not-found");
    });
    if (sha256(bytes) !== asset.blobDigest) throw new Error("source-digest-mismatch");
    return bytes;
  }

  private async readContainedReferenceBytes(customerId: string, asset: StudioMediaAsset): Promise<Buffer> {
    const paths = this.paths(customerId);
    const blobPath = join(paths.blobs, asset.blobId, "original.bin");
    const assetPath = join(paths.assets, asset.id, "original.bin");
    const canonicalRoot = await realpath(paths.customer);
    const canonicalSource = await realpath(blobPath).catch((error: unknown) => {
      if (hasCode(error, "ENOENT")) return realpath(assetPath);
      throw error;
    });
    const withinRoot = relative(canonicalRoot, canonicalSource);
    if (!withinRoot || withinRoot === ".." || withinRoot.startsWith(`..${sep}`) || isAbsolute(withinRoot)) {
      throw new Error("reference-source-path-outside-library");
    }
    const handle = await openFile(canonicalSource, "r");
    try {
      const fileStat = await handle.stat();
      if (!fileStat.isFile()) throw new Error("reference-source-file-invalid");
      const bytes = await handle.readFile();
      if (sha256(bytes) !== asset.blobDigest) throw new Error("source-digest-mismatch");
      return bytes;
    } finally {
      await handle.close();
    }
  }

  private async sourcePath(customerId: string, asset: StudioMediaAsset): Promise<string> {
    const blobPath = join(this.paths(customerId).blobs, asset.blobId, "original.bin");
    const hasBlob = await readFile(blobPath).then(() => true).catch((error: unknown) => {
      if (hasCode(error, "ENOENT")) return false;
      throw error;
    });
    return realpath(hasBlob ? blobPath : join(this.paths(customerId).assets, asset.id, "original.bin"));
  }

  private async assertSourceDigest(customerId: string, asset: StudioMediaAsset): Promise<void> {
    await this.readSourceBytes(customerId, asset);
  }

  private paths(customerId: string) {
    const customer = join(this.root, "customers", sha256(customerId));
    return {
      customer,
      assets: join(customer, "assets"),
      blobs: join(customer, "blobs"),
      requests: join(customer, "requests"),
      cards: join(customer, "cards"),
      withdrawals: join(customer, "withdrawals"),
    };
  }

  private analysisStatePath(customerId: string, sessionId: string): string {
    return join(this.paths(customerId).customer, "analysis", sha256(sessionId), "analysis-state.json");
  }

  private preferenceStatePath(actorId: string, customerId: string, sessionId: string): string {
    return join(
      this.paths(customerId).customer,
      "preferences",
      sha256(sessionId),
      `${sha256(actorId)}.json`,
    );
  }

  private draftFeedbackStatePath(actorId: string, customerId: string, sessionId: string): string {
    return join(
      this.paths(customerId).customer,
      "draft-feedback",
      sha256(sessionId),
      `${sha256(actorId)}.json`,
    );
  }

  private async withCustomerLock<T>(customerId: string, work: () => Promise<T>): Promise<T> {
    const key = join(this.root, sha256(customerId));
    const previous = CustomerMaterialLibrary.operationQueues.get(key) ?? Promise.resolve();
    let release!: () => void;
    const gate = new Promise<void>((resolveGate) => { release = resolveGate; });
    const tail = previous.then(() => gate, () => gate);
    CustomerMaterialLibrary.operationQueues.set(key, tail);
    await previous.catch(() => undefined);
    try {
      return await work();
    } finally {
      release();
      if (CustomerMaterialLibrary.operationQueues.get(key) === tail) {
        CustomerMaterialLibrary.operationQueues.delete(key);
      }
    }
  }
}

function assertScope(actorId: string, customerId: string): void {
  assertId(actorId, "actor-id-required");
  assertId(customerId, "customer-id-required");
}

function assertId(value: string, error: string): void {
  if (typeof value !== "string" || !value.trim()) throw new Error(error);
}

function assertAssetId(value: string): void {
  if (typeof value !== "string" || !/^asset_[a-f0-9]{64}$/.test(value)) {
    throw new Error("asset-id-invalid");
  }
}

function parseReferenceWorkflowAssetBindings(
  value: readonly ReferenceWorkflowAssetBinding[],
): ReferenceWorkflowAssetBinding[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > 512) {
    throw new Error("reference-workflow-assets-required");
  }
  const identities = new Set<string>();
  return value.map((binding) => {
    if (!binding || typeof binding !== "object" ||
        (binding.role !== "sample-source" && binding.role !== "render-source")) {
      throw new Error("reference-workflow-asset-binding-invalid");
    }
    assertAssetId(binding.assetId);
    if (!Number.isInteger(binding.assetVersion) || binding.assetVersion <= 0 ||
        !/^[a-f0-9]{64}$/.test(binding.sourceDigest)) {
      throw new Error("reference-workflow-asset-binding-invalid");
    }
    const identity = `${binding.role}\0${binding.assetId}`;
    if (identities.has(identity)) throw new Error("reference-workflow-asset-binding-duplicate");
    identities.add(identity);
    if (binding.role === "render-source") {
      const permission = binding.permission;
      if (!permission || !Number.isInteger(permission.version) || permission.version <= 0 ||
          !Number.isInteger(permission.epoch) || permission.epoch <= 0 ||
          permission.view !== "allowed" || permission.use !== "allowed") {
        throw new Error("reference-workflow-asset-binding-invalid");
      }
    }
    return binding;
  }).sort((left, right) => {
    const leftIdentity = `${left.assetId}\0${left.role}`;
    const rightIdentity = `${right.assetId}\0${right.role}`;
    return leftIdentity < rightIdentity ? -1 : leftIdentity > rightIdentity ? 1 : 0;
  });
}

function assertAnalysisBinding(binding: StudioAnalysisAssetBinding): void {
  assertScope(binding.actorId, binding.customerId);
  assertId(binding.sessionId, "session-id-required");
  if (binding.sourceAssetSessionId !== null) {
    assertId(binding.sourceAssetSessionId, "source-asset-session-id-required");
  }
  assertAssetId(binding.assetId);
  if (!/^[a-f0-9]{64}$/.test(binding.sourceDigest) || !binding.sourcePath.trim()) {
    throw new Error("studio-analysis-source-binding-invalid");
  }
}

function assertReferenceAnalysisSnapshotBinding(
  snapshot: ReferenceAnalysisAssetSnapshot,
  binding: StudioAnalysisAssetBinding,
): void {
  if (
    snapshot.asset.customerId !== binding.customerId
    || snapshot.asset.sessionId !== binding.sourceAssetSessionId
    || snapshot.asset.id !== binding.assetId
    || snapshot.asset.digest !== binding.sourceDigest
    || snapshot.sourcePath !== binding.sourcePath
  ) {
    throw new Error("source-digest-mismatch");
  }
}

function assertAnalysisSnapshotBinding(
  snapshot: StudioAnalysisAssetSnapshot | ReferenceAnalysisAssetSnapshot,
  binding: StudioAnalysisAssetBinding,
): void {
  if (
    snapshot.asset.customerId !== binding.customerId
    || snapshot.asset.sessionId !== binding.sessionId
    || snapshot.asset.id !== binding.assetId
    || snapshot.asset.digest !== binding.sourceDigest
    || snapshot.sourcePath !== binding.sourcePath
  ) {
    throw new Error("source-digest-mismatch");
  }
}

function outputUseAnalysisSnapshot(
  asset: StudioMediaAsset,
  sourcePath: string,
): StudioAnalysisAssetSnapshot {
  return {
    asset,
    sourcePath,
    authorization: "output-use-authorized",
  } as StudioAnalysisAssetSnapshot;
}

function referenceAnalysisSnapshot(
  asset: StudioMediaAsset,
  sourcePath: string,
): ReferenceAnalysisAssetSnapshot {
  return {
    asset,
    sourcePath,
    authorization: "reference-view-authorized",
  } as ReferenceAnalysisAssetSnapshot;
}

function assertNoCallerPath(input: ImportOriginalInput): void {
  const value = input as ImportOriginalInput & Record<string, unknown>;
  for (const key of ["path", "localPath", "storagePath", "targetPath"]) {
    if (Object.prototype.hasOwnProperty.call(value, key)) {
      throw new Error("caller-storage-path-not-allowed");
    }
  }
}

function assertImportInput(input: ImportOriginalInput): void {
  assertId(input.requestId, "import-request-id-required");
  assertId(input.fileName, "import-file-name-required");
  if (!(input.bytes instanceof Uint8Array) || input.bytes.byteLength === 0) {
    throw new Error("import-bytes-required");
  }
  if (!["video", "image", "audio"].includes(input.mediaKind)) {
    throw new Error("import-media-kind-invalid");
  }
  if (input.capturedAt !== null && !Number.isFinite(Date.parse(input.capturedAt))) {
    throw new Error("import-captured-at-invalid");
  }
}

function assetIdFor(customerId: string, digest: string, metadataDigest: string): string {
  return `asset_${sha256(`${customerId}\0${digest}\0${metadataDigest}`)}`;
}

function blobIdFor(digest: string): string {
  return `blob_${digest}`;
}

function importMetadataDigest(asset: Pick<StudioMediaAsset,
  "customerId" | "sessionId" | "mediaKind" | "byteLength" | "fileName" |
  "capturedAt" | "viewPermission" | "usePermission" | "captureContext">): string {
  return sha256(JSON.stringify({
    customerId: asset.customerId,
    sessionId: asset.sessionId,
    mediaKind: asset.mediaKind,
    byteLength: asset.byteLength,
    fileName: asset.fileName,
    capturedAt: asset.capturedAt,
    viewPermission: asset.viewPermission,
    usePermission: asset.usePermission,
    captureContext: asset.captureContext,
  }));
}

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

async function readAssetIfPresent(path: string): Promise<StudioMediaAsset | null> {
  const raw = await readFile(path, "utf8").catch((error: unknown) => {
    if (hasCode(error, "ENOENT")) return null;
    throw error;
  });
  if (raw === null) return null;
  const value = JSON.parse(raw) as Record<string, unknown>;
  const digest = typeof value.digest === "string" ? value.digest : "";
  return StudioMediaAssetSchema.parse({
    ...value,
    importBindingId: value.importBindingId ?? value.id,
    blobId: value.blobId ?? blobIdFor(digest),
    blobDigest: value.blobDigest ?? digest,
  });
}

async function readJsonIfPresent<T>(path: string): Promise<T | null> {
  const raw = await readFile(path, "utf8").catch((error: unknown) => {
    if (hasCode(error, "ENOENT")) return null;
    throw error;
  });
  return raw === null ? null : JSON.parse(raw) as T;
}

async function writeImmutableJson(
  path: string,
  value: unknown,
  conflictError: string,
): Promise<ImmutableWrite> {
  return writeImmutable(path, Buffer.from(`${JSON.stringify(value)}\n`), conflictError);
}

async function writeImmutable(
  path: string,
  bytes: Uint8Array,
  conflictError: string,
): Promise<ImmutableWrite> {
  await mkdir(dirname(path), { recursive: true });
  const pending = `${path}.pending-${randomUUID()}`;
  await writeFile(pending, bytes, { flag: "wx" });
  try {
    await link(pending, path);
    return { path, created: true };
  } catch (error) {
    if (!hasCode(error, "EEXIST")) throw error;
    const current = await readFile(path);
    if (!Buffer.from(current).equals(Buffer.from(bytes))) throw new Error(conflictError);
    return { path, created: false };
  } finally {
    await unlink(pending).catch(() => undefined);
  }
}

async function assertDigest(path: string, digest: string): Promise<void> {
  const bytes = await readFile(path).catch(() => {
    throw new Error("asset-not-found");
  });
  if (sha256(bytes) !== digest) throw new Error("source-digest-mismatch");
}

async function rollbackCreated(writes: readonly ImmutableWrite[]): Promise<void> {
  await Promise.all(writes.filter((write) => write.created).map((write) => unlink(write.path).catch(() => undefined)));
}

function hasCode(error: unknown, code: string): boolean {
  return typeof error === "object" && error !== null && "code" in error && error.code === code;
}

function compareCreatedRecords(left: MaterialCard, right: MaterialCard): number {
  return left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id);
}
