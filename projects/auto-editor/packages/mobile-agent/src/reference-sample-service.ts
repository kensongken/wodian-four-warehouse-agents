import { createHash } from "node:crypto";

import {
  ReferenceSampleSchema,
  ReferenceSourceKindSchema,
  type ReferenceSample,
  type ReferenceSourceKind,
  type StudioMediaAsset,
} from "@wearable/contracts";

import { CustomerMaterialLibrary } from "./customer-material-library";
import { JsonReferenceSampleStore } from "./reference-sample-store";
import type { StudioScope } from "./studio-capabilities";

export type ImportDedicatedReferenceInput = Readonly<{
  requestId: string;
  fileName: string;
  bytes: Uint8Array;
  sourceKind: ReferenceSourceKind;
  operationKey: string;
}>;

export type RegisterHistoricalReferenceInput = Readonly<{
  assetId: string;
  sourceKind: ReferenceSourceKind;
  operationKey: string;
}>;

export type ReferenceSampleRegistration = Readonly<{
  asset: StudioMediaAsset;
  sample: ReferenceSample;
}>;

export type HistoricalReferenceAsset = Readonly<{
  assetId: string;
  fileName: string;
  byteLength: number;
  capturedAt: string | null;
  importedAt: string;
  viewPermission: StudioMediaAsset["viewPermission"];
  usePermission: StudioMediaAsset["usePermission"];
}>;

export type ReferenceSampleMedia = Readonly<{
  bytes: Buffer;
  fileName: string;
}>;

export type ReferenceSampleServiceOptions = Readonly<{
  now?: () => string;
}>;

export class ReferenceSampleService {
  private readonly now: () => string;

  constructor(
    private readonly library: CustomerMaterialLibrary,
    private readonly root: string,
    options: ReferenceSampleServiceOptions = {},
  ) {
    if (typeof root !== "string" || !root.trim()) throw new Error("reference-store-root-required");
    this.now = options.now ?? (() => new Date().toISOString());
  }

  async importDedicated(
    scopeValue: StudioScope,
    inputValue: ImportDedicatedReferenceInput,
  ): Promise<ReferenceSampleRegistration> {
    const scope = parseScope(scopeValue);
    const input = parseDedicatedInput(inputValue);
    await this.library.assertReferenceOperationAuthorized(scope.actorId, scope.customerId, "import", "before");
    const store = await JsonReferenceSampleStore.open(this.root, scope);
    await store.reserveOperation({
      operationKey: input.operationKey,
      bodyDigest: dedicatedOperationDigest(input),
      kind: "dedicated-upload",
    });
    const asset = await this.library.importOriginal(scope.actorId, scope.customerId, {
      requestId: input.requestId,
      bytes: input.bytes,
      mediaKind: "video",
      fileName: input.fileName,
      capturedAt: null,
      sessionId: scope.sessionId,
      viewPermission: "allowed",
      usePermission: "review-required",
    });
    const sample = ReferenceSampleSchema.parse({
      id: sampleId(scope, input.operationKey),
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 1,
      sourceAssetId: asset.id,
      sourceDigest: asset.digest,
      sourceKind: input.sourceKind,
      collectionMode: "dedicated-upload",
      candidateScope: "reference-only",
      status: "pending-rights",
      viewPermission: asset.viewPermission,
      usePermission: asset.usePermission,
      createdAt: this.now(),
    });
    return { asset, sample: await persistRegistration(store, sample) };
  }

  async registerHistorical(
    scopeValue: StudioScope,
    inputValue: RegisterHistoricalReferenceInput,
  ): Promise<ReferenceSampleRegistration> {
    const scope = parseScope(scopeValue);
    const input = parseHistoricalInput(inputValue);
    await this.library.assertReferenceOperationAuthorized(scope.actorId, scope.customerId, "view", "before");
    const store = await JsonReferenceSampleStore.open(this.root, scope);
    await store.reserveOperation({
      operationKey: input.operationKey,
      bodyDigest: historicalOperationDigest(input),
      kind: "historical-pointer",
    });
    const asset = (await this.library.listAssets(scope.actorId, scope.customerId))
      .find((candidate) => candidate.id === input.assetId && candidate.mediaKind === "video");
    if (!asset) throw new Error("reference-history-asset-not-visible");
    if (asset.sessionId === scope.sessionId) {
      throw new Error("reference-history-asset-current-session");
    }
    const sample = ReferenceSampleSchema.parse({
      id: sampleId(scope, input.operationKey),
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 1,
      sourceAssetId: asset.id,
      sourceDigest: asset.digest,
      sourceKind: input.sourceKind,
      collectionMode: "historical-pointer",
      candidateScope: "reference-only",
      status: "pending-rights",
      viewPermission: asset.viewPermission,
      usePermission: asset.usePermission,
      createdAt: this.now(),
    });
    return { asset, sample: await persistRegistration(store, sample) };
  }

  async listSamples(scopeValue: StudioScope): Promise<ReferenceSample[]> {
    const scope = parseScope(scopeValue);
    await this.library.assertReferenceOperationAuthorized(scope.actorId, scope.customerId, "view", "before");
    const store = await JsonReferenceSampleStore.open(this.root, scope);
    const samples = await store.listSamples();
    await this.library.assertReferenceOperationAuthorized(scope.actorId, scope.customerId, "view", "after");
    return samples;
  }

  async listHistoricalAssets(scopeValue: StudioScope): Promise<HistoricalReferenceAsset[]> {
    const scope = parseScope(scopeValue);
    const assets = await this.library.listAssets(scope.actorId, scope.customerId);
    return assets
      .filter((asset) => asset.mediaKind === "video" && asset.sessionId !== scope.sessionId)
      .sort((left, right) =>
        right.importedAt.localeCompare(left.importedAt) || compareText(left.id, right.id))
      .map((asset) => ({
        assetId: asset.id,
        fileName: safeDisplayFileName(asset.fileName),
        byteLength: asset.byteLength,
        capturedAt: asset.capturedAt,
        importedAt: asset.importedAt,
        viewPermission: asset.viewPermission,
        usePermission: asset.usePermission,
      }));
  }

  async readSampleMedia(scopeValue: StudioScope, sampleIdValue: string): Promise<ReferenceSampleMedia> {
    const scope = parseScope(scopeValue);
    const sampleId = requireId(sampleIdValue, "reference-sample-id-required");
    const store = await JsonReferenceSampleStore.open(this.root, scope);
    const initial = currentSample(await store.snapshot(), sampleId);
    if (!initial) throw new Error("reference-sample-not-found");
    if (initial.status === "invalidated") throw new Error("reference-sample-invalidated");
    try {
      return await this.library.withReferenceOriginal(
        scope.actorId,
        scope.customerId,
        initial.sourceAssetId,
        async ({ asset, bytes }) => store.withCurrentSnapshot((snapshot) => {
          const current = currentSample(snapshot, sampleId);
          if (!current) throw new Error("reference-sample-not-found");
          if (current.status === "invalidated") throw new Error("reference-sample-invalidated");
          if (
            current.version !== initial.version
            || current.sourceAssetId !== initial.sourceAssetId
            || current.sourceDigest !== initial.sourceDigest
            || asset.id !== current.sourceAssetId
            || asset.digest !== current.sourceDigest
          ) throw new Error("reference-sample-not-current");
          return { bytes: Buffer.from(bytes), fileName: asset.fileName };
        }),
      );
    } catch (error) {
      if (error instanceof Error &&
          (error.message === "asset-withdrawn" || error.message === "asset-view-not-allowed")) {
        throw new Error("reference-sample-source-withdrawn");
      }
      if (error instanceof Error && (error.message === "reference-source-path-outside-library" ||
          error.message === "reference-source-file-invalid" ||
          error.message === "source-digest-mismatch")) {
        throw new Error("reference-sample-source-binding-invalid");
      }
      throw error;
    }
  }

  async listOrdinaryCandidateAssetIds(scopeValue: StudioScope): Promise<string[]> {
    const scope = parseScope(scopeValue);
    const assets = await this.library.listAssets(scope.actorId, scope.customerId);
    return assets
      .filter((asset) =>
        asset.sessionId === scope.sessionId &&
        asset.viewPermission === "allowed" &&
        asset.usePermission === "allowed")
      .map((asset) => asset.id)
      .sort(compareText);
  }
}

function parseDedicatedInput(value: ImportDedicatedReferenceInput): ImportDedicatedReferenceInput {
  if (!value || typeof value !== "object") throw new Error("reference-import-input-required");
  assertId(value.requestId, "reference-import-request-id-required");
  assertId(value.fileName, "reference-import-file-name-required");
  assertId(value.operationKey, "reference-operation-key-required");
  if (!(value.bytes instanceof Uint8Array) || value.bytes.byteLength === 0) {
    throw new Error("reference-import-bytes-required");
  }
  return { ...value, sourceKind: ReferenceSourceKindSchema.parse(value.sourceKind) };
}

function parseHistoricalInput(value: RegisterHistoricalReferenceInput): RegisterHistoricalReferenceInput {
  if (!value || typeof value !== "object") throw new Error("reference-history-input-required");
  assertId(value.assetId, "reference-history-asset-id-required");
  assertId(value.operationKey, "reference-operation-key-required");
  return { ...value, sourceKind: ReferenceSourceKindSchema.parse(value.sourceKind) };
}

function parseScope(value: StudioScope): StudioScope {
  if (!value || typeof value !== "object") throw new Error("reference-scope-required");
  assertId(value.actorId, "actor-id-required");
  assertId(value.customerId, "customer-id-required");
  assertId(value.sessionId, "session-id-required");
  return { actorId: value.actorId, customerId: value.customerId, sessionId: value.sessionId };
}

function sampleId(scope: StudioScope, operationKey: string): string {
  return `reference_sample_${sha256(`${scope.customerId}\0${scope.sessionId}\0${operationKey}`)}`;
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function assertId(value: string, error: string): void {
  if (typeof value !== "string" || !value.trim()) throw new Error(error);
}

function requireId(value: string, error: string): string {
  assertId(value, error);
  return value.trim();
}

function safeDisplayFileName(value: string): string {
  const trimmed = value.trim();
  if (!trimmed || /[\\/]$/.test(trimmed)) return "reference-video";
  const name = trimmed.split(/[\\/]/).at(-1)?.replace(/[\u0000-\u001f\u007f]/g, "").trim();
  return !name || name === "." || name === ".." ? "reference-video" : name.slice(0, 255);
}

function currentSample(
  snapshot: Awaited<ReturnType<JsonReferenceSampleStore["snapshot"]>>,
  sampleId: string,
): ReferenceSample | null {
  return snapshot.samples
    .filter((sample) => sample.id === sampleId)
    .sort((left, right) => right.version - left.version)[0] ?? null;
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function dedicatedOperationDigest(input: ImportDedicatedReferenceInput): string {
  return sha256(JSON.stringify({
    requestId: input.requestId,
    fileName: input.fileName,
    bytesDigest: createHash("sha256").update(input.bytes).digest("hex"),
    sourceKind: input.sourceKind,
  }));
}

function historicalOperationDigest(input: RegisterHistoricalReferenceInput): string {
  return sha256(JSON.stringify({ assetId: input.assetId, sourceKind: input.sourceKind }));
}

async function persistRegistration(
  store: JsonReferenceSampleStore,
  sample: ReferenceSample,
): Promise<ReferenceSample> {
  const existing = (await store.listSamples()).find((candidate) => candidate.id === sample.id);
  if (existing) return requireSameRegistration(existing, sample);
  try {
    return await store.putSample(sample);
  } catch (error) {
    if (!(error instanceof Error) || error.message !== "reference-immutable-object-conflict") throw error;
    const concurrent = (await store.listSamples()).find((candidate) => candidate.id === sample.id);
    if (!concurrent) throw error;
    return requireSameRegistration(concurrent, sample);
  }
}

function requireSameRegistration(existing: ReferenceSample, requested: ReferenceSample): ReferenceSample {
  const { createdAt: _existingCreatedAt, ...existingIdentity } = existing;
  const { createdAt: _requestedCreatedAt, ...requestedIdentity } = requested;
  if (JSON.stringify(existingIdentity) !== JSON.stringify(requestedIdentity)) {
    throw new Error("reference-operation-key-conflict");
  }
  return existing;
}
