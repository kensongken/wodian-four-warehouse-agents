import { createHash } from "node:crypto";
import { isAbsolute, normalize as normalizePath } from "node:path";

import {
  MaterialCardSchema,
  ModelAnalysisReceiptV1Schema,
  StudioEvidenceDegradedReasonSchema,
  StudioEvidenceRecordSchema,
  StudioContentTypeSchema,
  StudioMediaAssetSchema,
  type MaterialCard,
  type ModelAnalysisReceiptV1,
  type StudioAnalysisLayer,
  type StudioContentType,
  type StudioEvidenceDegradedReason,
  type StudioEvidenceRecord,
  type StudioMediaAsset,
} from "@wearable/contracts";

import {
  OFFLINE_STUDIO_CAPABILITY,
  studioProviderCapability,
  type StudioModelProviderIdentity,
  type StudioScope,
} from "./studio-capabilities";

export const STUDIO_REQUIRED_EVIDENCE_LAYERS = Object.freeze([
  "technical",
  "scene",
  "transcript",
  "embedding",
  "vision",
] as const satisfies readonly StudioAnalysisLayer[]);

export type { StudioEvidenceDegradedReason, StudioEvidenceRecord } from "@wearable/contracts";

export type MergeStudioEvidenceInput = {
  scope: StudioScope;
  asset: StudioMediaAsset;
  sourcePath: string;
  sourceStartMs: number;
  sourceEndMs: number;
  contentType: StudioContentType;
  receipts: ModelAnalysisReceiptV1[];
  humanFacts: string[];
  createdAt: string;
  version: number;
};

export type MergedStudioEvidence = Readonly<{
  card: MaterialCard;
  evidence: readonly StudioEvidenceRecord[];
  degradedReasons: readonly StudioEvidenceDegradedReason[];
  providerIdentities: readonly StudioModelProviderIdentity[];
}>;

export function mergeStudioEvidence(input: MergeStudioEvidenceInput): MergedStudioEvidence {
  const asset = StudioMediaAssetSchema.parse(input.asset);
  const contentType = StudioContentTypeSchema.parse(input.contentType);
  assertInput(input, asset);
  const receipts = input.receipts.map((value) => ModelAnalysisReceiptV1Schema.parse(value));
  const receiptByLayer = new Map<StudioAnalysisLayer, ModelAnalysisReceiptV1>();
  for (const receipt of receipts) {
    if (receipt.status !== "succeeded") throw new Error("studio-evidence-receipt-not-succeeded");
    if (receipt.customerId !== input.scope.customerId ||
        receipt.sessionId !== input.scope.sessionId ||
        receipt.sourceAssetId !== asset.id ||
        receipt.sourceDigest !== asset.digest ||
        receipt.sourcePath !== input.sourcePath ||
        receipt.sourceStartMs !== input.sourceStartMs ||
        receipt.sourceEndMs !== input.sourceEndMs) {
      throw new Error("studio-evidence-receipt-binding-mismatch");
    }
    if (receiptByLayer.has(receipt.layer)) throw new Error("studio-evidence-duplicate-layer");
    receiptByLayer.set(receipt.layer, receipt);
  }

  // Provider readiness is fixed by the studio contract; callers cannot relax it.
  const requiredLayers = STUDIO_REQUIRED_EVIDENCE_LAYERS;
  const degradedReasons: StudioEvidenceDegradedReason[] = [];
  for (const layer of requiredLayers) {
    const receipt = receiptByLayer.get(layer);
    if (!receipt) {
      degradedReasons.push(StudioEvidenceDegradedReasonSchema.parse({
        code: "studio-evidence-required-provider-missing",
        layer,
        message: "Required " + layer + " provider receipt is missing",
      }));
      continue;
    }
    if (receipt.providerMode === "offline-fallback") {
      degradedReasons.push(StudioEvidenceDegradedReasonSchema.parse({
        code: "studio-evidence-required-provider-offline",
        layer,
        message: "Required " + layer + " receipt uses offline-fallback",
      }));
    }
    if (receipt.synthetic) {
      degradedReasons.push(StudioEvidenceDegradedReasonSchema.parse({
        code: "studio-evidence-required-provider-synthetic",
        layer,
        message: "Required " + layer + " receipt is synthetic",
      }));
    }
  }
  const normalizedDegradedReasons = uniqueDegradedReasons(degradedReasons);
  const providerIdentities = receipts
    .filter((receipt) => receipt.providerMode === "local" && !receipt.synthetic)
    .map(providerIdentity);
  const capability = normalizedDegradedReasons.length === 0
    ? studioProviderCapability(providerIdentities)
    : OFFLINE_STUDIO_CAPABILITY;

  const technicalFacts = verifiedTechnicalFacts(receiptByLayer, input.sourceStartMs, input.sourceEndMs);
  const confirmedFacts = normalize(input.humanFacts);
  const pendingFacts = normalize(modelSuggestions(receipts))
    .filter((value) => !confirmedFacts.includes(value));
  const technical = receiptByLayer.get("technical");
  const technicalUsable = technical !== undefined && isTechnicalUsable(
    technical.qualitySignals,
    input.sourceEndMs,
  );
  const technicalFlags = normalize([
    ...(technicalUsable ? ["verified-technical-evidence"] : ["verified-technical-evidence-insufficient"]),
    ...normalizedDegradedReasons.map((reason) => reason.code + ":" + reason.layer),
  ]);
  const id = "card_" + stableDigest({
    customerId: input.scope.customerId,
    sessionId: input.scope.sessionId,
    assetId: asset.id,
    sourceDigest: asset.digest,
    sourcePath: input.sourcePath,
    sourceStartMs: input.sourceStartMs,
    sourceEndMs: input.sourceEndMs,
    contentType,
    receipts: receipts.map(receiptIdentity).sort((left, right) =>
      compareText(JSON.stringify(left), JSON.stringify(right))),
    confirmedFacts,
  });
  const card = MaterialCardSchema.parse({
    id,
    customerId: input.scope.customerId,
    sessionId: input.scope.sessionId,
    version: input.version,
    sourceAssetId: asset.id,
    sourceDigest: asset.digest,
    sourceStartMs: input.sourceStartMs,
    sourceEndMs: input.sourceEndMs,
    contentType,
    technicalUsable,
    technicalFlags,
    observations: technicalFacts,
    confirmedFacts,
    pendingFacts,
    capability,
    createdAt: input.createdAt,
  });
  const base = {
    materialCardId: card.id,
    sourceAssetId: card.sourceAssetId,
    sourceDigest: card.sourceDigest,
    sourceStartMs: card.sourceStartMs,
    sourceEndMs: card.sourceEndMs,
  };
  const evidence: StudioEvidenceRecord[] = [
    {
      ...base,
      kind: "permission",
      value: "authorized-current-source",
      receiptJobId: null,
      layer: null,
      reason: "Current customer permission and source binding were verified",
    },
    ...confirmedFacts.map((value): StudioEvidenceRecord => ({
      ...base,
      kind: "human-confirmed-fact",
      value,
      receiptJobId: null,
      layer: null,
      reason: "Explicitly confirmed by a human reviewer",
    })),
    ...technicalFacts.map((value): StudioEvidenceRecord => {
      const layer = value.startsWith("scene-count:") || value.startsWith("transition-kind:")
        ? "scene"
        : "technical";
      return {
        ...base,
        kind: "verified-technical-fact",
        value,
        receiptJobId: receiptByLayer.get(layer)?.jobId ?? null,
        layer,
        reason: "Derived from a verified technical field in a bound successful receipt",
      };
    }),
    ...pendingFacts.map((value): StudioEvidenceRecord => {
      const source = receipts.find((receipt) => suggestionsForReceipt(receipt).includes(value));
      return {
        ...base,
        kind: "model-suggestion",
        value,
        receiptJobId: source?.jobId ?? null,
        layer: source?.layer ?? null,
        reason: "Model output is a pending suggestion and requires human confirmation",
      };
    }),
  ].map((record) => StudioEvidenceRecordSchema.parse(record));
  return { card, evidence, degradedReasons: normalizedDegradedReasons, providerIdentities };
}

function assertInput(input: MergeStudioEvidenceInput, asset: StudioMediaAsset): void {
  if (!input.scope || !input.scope.actorId.trim() || !input.scope.customerId.trim() || !input.scope.sessionId.trim()) {
    throw new Error("studio-evidence-scope-required");
  }
  if (asset.customerId !== input.scope.customerId || asset.sessionId !== input.scope.sessionId) {
    throw new Error("studio-evidence-asset-binding-mismatch");
  }
  if (typeof input.sourcePath !== "string" || input.sourcePath.trim() !== input.sourcePath ||
      !isAbsolute(input.sourcePath) || normalizePath(input.sourcePath) !== input.sourcePath) {
    throw new Error("studio-evidence-source-path-invalid");
  }
  if (asset.viewPermission !== "allowed" || asset.usePermission !== "allowed") {
    throw new Error("studio-evidence-permission-blocked");
  }
  if (!Number.isInteger(input.sourceStartMs) || input.sourceStartMs < 0 ||
      !Number.isInteger(input.sourceEndMs) || input.sourceEndMs <= input.sourceStartMs) {
    throw new Error("studio-evidence-interval-invalid");
  }
  if (!Array.isArray(input.receipts) || !Array.isArray(input.humanFacts)) {
    throw new Error("studio-evidence-arrays-required");
  }
  if (!Number.isInteger(input.version) || input.version < 1 || !Number.isFinite(Date.parse(input.createdAt))) {
    throw new Error("studio-evidence-metadata-invalid");
  }
}

function providerIdentity(receipt: ModelAnalysisReceiptV1): StudioModelProviderIdentity {
  return {
    layer: receipt.layer,
    adapterName: receipt.adapterName,
    modelName: receipt.modelName,
    modelVersion: receipt.modelVersion,
    checkpointDigest: receipt.checkpointDigest,
  };
}

function receiptIdentity(receipt: ModelAnalysisReceiptV1): object {
  return {
    jobId: receipt.jobId,
    layer: receipt.layer,
    inputDigest: receipt.inputDigest,
    promptVersion: receipt.promptVersion,
    provider: providerIdentity(receipt),
  };
}

function verifiedTechnicalFacts(
  receipts: ReadonlyMap<StudioAnalysisLayer, ModelAnalysisReceiptV1>,
  sourceStartMs: number,
  sourceEndMs: number,
): string[] {
  const result: string[] = [];
  const signals = receipts.get("technical")?.qualitySignals;
  if (signals && typeof signals === "object" && !Array.isArray(signals)) {
    const value = signals as Record<string, unknown>;
    addNumber(result, "duration-ms", value.durationMs, true);
    addBoolean(result, "video-stream", value.hasVideo);
    addBoolean(result, "audio-stream", value.hasAudio);
    if (positiveInteger(value.width) && positiveInteger(value.height)) {
      result.push("frame-size:" + value.width + "x" + value.height);
    }
    addText(result, "video-codec", value.videoCodec);
    addText(result, "audio-codec", value.audioCodec);
    addNumber(result, "black-ratio", value.blackRatio, false);
    addNumber(result, "freeze-ratio", value.freezeRatio, false);
    addNumber(result, "blur-score", value.blurScore, false);
  }
  const sceneSignals = receipts.get("scene")?.qualitySignals;
  if (sceneSignals && typeof sceneSignals === "object" && !Array.isArray(sceneSignals)) {
    const scenes = (sceneSignals as Record<string, unknown>).scenes;
    if (Array.isArray(scenes)) result.push("scene-count:" + scenes.length);
    const transitions = (sceneSignals as Record<string, unknown>).transitionEvidence;
    if (Array.isArray(transitions)) {
      for (const transition of transitions) {
        if (!isRecord(transition)
            || !Number.isSafeInteger(transition.atMs)
            || Number(transition.atMs) <= sourceStartMs
            || Number(transition.atMs) >= sourceEndMs
            || !["cut", "fade", "crossfade"].includes(String(transition.kind))
            || typeof transition.confidence !== "number"
            || !Number.isFinite(transition.confidence)
            || transition.confidence < 0
            || transition.confidence > 1) continue;
        result.push("transition-kind:" + transition.kind);
      }
    }
  }
  return normalize(result);
}

function modelSuggestions(receipts: readonly ModelAnalysisReceiptV1[]): string[] {
  return receipts.flatMap(suggestionsForReceipt);
}

function suggestionsForReceipt(receipt: ModelAnalysisReceiptV1): string[] {
  const values = [...receipt.observations, ...receipt.suggestedTags];
  if (receipt.layer === "transcript" && isRecord(receipt.qualitySignals)) {
    const spans = receipt.qualitySignals.spans;
    if (Array.isArray(spans)) {
      for (const span of spans) if (isRecord(span) && typeof span.text === "string") values.push(span.text);
    }
  }
  if (receipt.layer === "vision" && isRecord(receipt.qualitySignals)) {
    const pending = receipt.qualitySignals.pendingObservations;
    if (Array.isArray(pending)) {
      for (const value of pending) if (typeof value === "string") values.push(value);
    }
  }
  return normalize(values);
}

function isTechnicalUsable(signals: unknown, sourceEndMs: number): boolean {
  if (!isRecord(signals)) return false;
  return signals.hasVideo === true && positiveInteger(signals.durationMs) && signals.durationMs >= sourceEndMs;
}

function addNumber(target: string[], label: string, value: unknown, integer: boolean): void {
  if (typeof value === "number" && Number.isFinite(value) && (!integer || Number.isInteger(value))) {
    target.push(label + ":" + value);
  }
}

function addBoolean(target: string[], label: string, value: unknown): void {
  if (typeof value === "boolean") target.push(label + ":" + (value ? "present" : "absent"));
}

function addText(target: string[], label: string, value: unknown): void {
  if (typeof value === "string" && value.trim()) target.push(label + ":" + value.trim());
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalize(values: readonly string[]): string[] {
  return [...new Set(values.map((value) => value.normalize("NFKC").trim()).filter(Boolean))].sort();
}

function stableDigest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function uniqueDegradedReasons(
  reasons: readonly StudioEvidenceDegradedReason[],
): StudioEvidenceDegradedReason[] {
  const rank = new Map(STUDIO_REQUIRED_EVIDENCE_LAYERS.map((layer, index) => [layer, index]));
  return [...new Map(reasons.map((reason) => [reason.code + "\0" + reason.layer, reason])).values()]
    .sort((left, right) => (rank.get(left.layer)! - rank.get(right.layer)!) ||
      compareText(left.code, right.code));
}
