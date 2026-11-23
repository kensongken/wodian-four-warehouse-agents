import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { promisify } from "node:util";

import {
  StudioAnalysisLayerSchema,
  type MaterialCard,
  type ModelAnalysisJobV1,
  type ModelAnalysisReceiptV1,
  type StudioAnalysisLayer,
} from "@wearable/contracts";
import {
  StudioAnalysisJobService,
  StudioEmbeddingIndex,
  StudioPreferenceRanker,
  selectFinalReferenceAnalysisAttempt,
  type CustomerMaterialLibrary,
  type FinalReferenceAnalysisAttempt,
  type PrivateContentStudio,
  type ReferenceAnalysisAssetSnapshot,
  type StudioAnalysisHistoryEntry,
  type StudioEditorialTranscriptSpan,
  type StudioEditorialVisualEvidence,
  type StudioModelWorker,
  type StudioScope,
  type StudioSourceIntervalResolver,
  type StudioTrustedWorkerContext,
} from "@wearable/mobile-agent";

export const STUDIO_ANALYSIS_REVIEW_ACTIONS = Object.freeze([
  "accept-suggestion",
  "correct-suggestion",
  "reject-suggestion",
  "keep-from-duplicate-group",
] as const);

export type StudioAnalysisReviewAction = typeof STUDIO_ANALYSIS_REVIEW_ACTIONS[number];

export type StudioModelsStatus = Readonly<{
  mode: "local" | "offline-fallback";
  localOnly: true;
  degraded: boolean;
  artifacts: readonly Readonly<{
    id: string;
    version: string;
    status: string;
  }>[];
  visionProbe: string;
  workerRuntime?: Readonly<{
    status: "runtime-unapproved" | "runtime-not-installed" | "runtime-integrity-failed"
      | "runtime-installed-probe-required" | "runtime-real-probe-failed"
      | "runtime-real-probe-succeeded";
  }>;
}>;

export type StudioModelStatusReader = () => Promise<StudioModelsStatus>;

const STUDIO_MODEL_CHECKER_TIMEOUT_MS = 45_000;
const CURRENT_EDITORIAL_VISION_PROMPT_VERSION = "studio-qwen3-vl-highlight-pair-json-v19";
const CURRENT_EDITORIAL_VISION_OPERATION_PREFIX = "editorial-highlight-v19-";

export type StudioAnalysisReviewInput = Readonly<{
  action: StudioAnalysisReviewAction;
  receiptJobId: string;
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  target: string;
  correction: string | null;
  operationKey: string;
}>;

export type StudioAnalysisReview = Readonly<{
  id: string;
  action: StudioAnalysisReviewAction;
  receiptJobId: string;
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  target: string;
  correction: string | null;
  createdAt: string;
}>;

export type StudioMaterialSuitabilityInput = Readonly<{
  decision: "suitable" | "not-suitable";
  demandId: string;
  matchAssessmentId: string;
  receiptJobId: string;
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  reason: string;
  operationKey: string;
}>;

export type StudioMaterialSuitability = Readonly<{
  id: string;
  decision: "suitable" | "not-suitable";
  demandId: string;
  matchAssessmentId: string;
  receiptJobId: string;
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  reason: string;
  createdAt: string;
}>;

export type PublicStudioAnalysisEntry = ReturnType<typeof publicAnalysisEntry>;
type StudioPublicDuplicateGroup = Readonly<{
  id: string;
  kind: "exact" | "near";
  sourceAssetIds: readonly string[];
  receiptJobId: string | null;
}>;

export type StudioModelRuntimeOptions = Readonly<{
  scope: StudioScope;
  library: CustomerMaterialLibrary;
  studio: PrivateContentStudio;
  worker: StudioModelWorker;
  now?: () => string;
  resolveSourceInterval?: StudioSourceIntervalResolver;
  modelStatusReader?: StudioModelStatusReader;
  embeddingIndex?: StudioEmbeddingIndex;
  reviewStatePath?: string;
  preferenceRanker?: StudioPreferenceRanker;
}>;

type StoredReview = StudioAnalysisReview & Readonly<{
  operationKey: string;
  bodyDigest: string;
}>;
type StoredSuitability = StudioMaterialSuitability & Readonly<{
  operationKey: string;
  bodyDigest: string;
}>;
type StoredHumanReviewState = Readonly<{
  reviews: readonly StoredReview[];
  suitabilityReviews: readonly StoredSuitability[];
}>;

/** Authenticated-session facade over durable analysis jobs and human review decisions. */
export class StudioModelRuntime {
  private static readonly reviewStateQueues = new Map<string, Promise<void>>();
  private readonly jobs: StudioAnalysisJobService;
  private readonly now: () => string;
  private readonly modelStatusReader: StudioModelStatusReader;
  private readonly embeddingIndex: StudioEmbeddingIndex | null;
  private readonly starts = new Map<string, Readonly<{ digest: string; promise: Promise<PublicStudioAnalysisEntry> }>>();
  private readonly evidenceByJobId = new Map<string, unknown>();

  constructor(private readonly options: StudioModelRuntimeOptions) {
    assertScope(options.scope);
    this.now = options.now ?? (() => new Date().toISOString());
    this.modelStatusReader = options.modelStatusReader ?? (async () => ({
      mode: "offline-fallback",
      localOnly: true,
      degraded: true,
      artifacts: [],
      visionProbe: "not-run: model-status-unavailable",
    }));
    this.embeddingIndex = options.embeddingIndex ?? null;
    this.jobs = new StudioAnalysisJobService(options.library, options.worker, {
      now: this.now,
      resolveSourceInterval: options.resolveSourceInterval ?? this.resolveMaterialInterval.bind(this),
      resolveWorkerContext: this.resolveWorkerContext.bind(this),
    });
  }

  async start(input: Readonly<{
    assetId: string;
    layers: readonly StudioAnalysisLayer[];
    operationKey: string;
  }>): Promise<PublicStudioAnalysisEntry> {
    const assetId = requireText(input.assetId, "studio-analysis-asset-id-required", 1_000);
    const layers = parseLayers(input.layers);
    const operationKey = requireText(input.operationKey, "studio-analysis-operation-key-required", 200);
    const requestDigest = digest(JSON.stringify({ assetId, layers }));
    const existing = this.starts.get(operationKey);
    if (existing && existing.digest !== requestDigest) {
      throw new Error("studio-analysis-operation-key-conflict");
    }
    if (existing) return clone(await existing.promise);
    const promise = this.executeStart(assetId, layers, operationKey);
    this.starts.set(operationKey, { digest: requestDigest, promise });
    try {
      return clone(await promise);
    } finally {
      if (this.starts.get(operationKey)?.promise === promise) this.starts.delete(operationKey);
    }
  }

  private async executeStart(
    assetId: string,
    layers: readonly StudioAnalysisLayer[],
    operationKey: string,
  ): Promise<PublicStudioAnalysisEntry> {
    const entry = await this.jobs.enqueue(this.options.scope, assetId, layers, operationKey);
    if (entry.job.status === "succeeded") await this.indexEmbeddingReceipt(entry);
    if (
      entry.job.status === "succeeded"
      && entry.receipts.length > 0
      && !this.evidenceByJobId.has(entry.job.id)
    ) {
      const cards = await this.options.library.inventoryAt(
        this.options.scope.actorId,
        this.options.scope.customerId,
        this.timestamp(),
      );
      const sourceCard = cards
        .filter((card) => card.sessionId === this.options.scope.sessionId && card.sourceAssetId === assetId)
        .sort((left, right) => right.version - left.version)[0];
      if (!sourceCard) throw new Error("studio-analysis-source-card-not-found");
      const merged = await this.options.studio.analyzeProviderEvidence(this.options.scope, {
        assetId,
        sourceStartMs: entry.job.sourceStartMs,
        sourceEndMs: entry.job.sourceEndMs,
        contentType: sourceCard.contentType,
        receipts: [...entry.receipts],
        humanFacts: [],
      });
      this.evidenceByJobId.set(entry.job.id, publicEvidence(merged));
    }
    return publicAnalysisEntry(entry, this.evidenceByJobId.get(entry.job.id) ?? null);
  }

  async history(): Promise<Readonly<{
    entries: readonly PublicStudioAnalysisEntry[];
    reviews: readonly StudioAnalysisReview[];
    duplicateGroups: readonly unknown[];
    suitabilityReviews: readonly StudioMaterialSuitability[];
    suitabilityContext: Readonly<{ demandId: string; matchAssessmentId: string }> | null;
  }>> {
    const entries = (await this.jobs.history(this.options.scope)).map((entry) =>
      publicAnalysisEntry(entry, this.evidenceByJobId.get(entry.job.id) ?? null));
    const groups = await this.duplicateGroups(entries);
    const reviewState = await this.loadReviewState();
    const suitabilityContext = await this.currentDemandBinding();
    return clone({
      entries,
      reviews: reviewState.reviews.map(publicReview),
      duplicateGroups: groups,
      suitabilityReviews: reviewState.suitabilityReviews.map(publicSuitability),
      suitabilityContext,
    });
  }

  async editorialTranscriptSpans(
    cards: readonly MaterialCard[],
  ): Promise<readonly StudioEditorialTranscriptSpan[]> {
    const entries = await this.jobs.history(this.options.scope);
    return cards.flatMap((card) => {
      const entry = entries
        .filter((candidate) =>
          candidate.job.status === "succeeded"
          && candidate.job.sourceAssetId === card.sourceAssetId
          && candidate.job.sourceDigest === card.sourceDigest
          && candidate.job.sourceStartMs >= card.sourceStartMs
          && candidate.job.sourceEndMs <= card.sourceEndMs
          && candidate.receipts.some((receipt) =>
            receipt.layer === "transcript"
            && receipt.status === "succeeded"
            && receipt.providerMode === "local"
            && receipt.synthetic === false
            && receipt.speechState === "speech"))
        .sort((left, right) => left.job.updatedAt.localeCompare(right.job.updatedAt)).at(-1);
      if (!entry) return [];
      const receipt = entry.receipts.find((candidate) =>
        candidate.layer === "transcript"
        && candidate.status === "succeeded"
        && candidate.providerMode === "local"
        && candidate.synthetic === false
        && candidate.speechState === "speech");
      if (!receipt) return [];
      return parseTranscriptSpans(receipt, entry.job).map((span) => ({
        sourceAssetId: card.sourceAssetId,
        sourceDigest: span.sourceDigest,
        startMs: span.startMs,
        endMs: span.endMs,
        text: span.text,
      }));
    });
  }

  async ensureEditorialTranscriptSpans(
    cards: readonly MaterialCard[],
  ): Promise<readonly StudioEditorialTranscriptSpan[]> {
    const existing = await this.editorialTranscriptSpans(cards);
    const existingAssets = new Set(existing.map((span) => span.sourceAssetId));
    const missing = cards.filter((card) => !existingAssets.has(card.sourceAssetId));
    if (missing.length === 0) return existing;
    await Promise.allSettled(missing.map((card) => this.jobs.enqueue(
      this.options.scope,
      card.sourceAssetId,
      ["transcript"],
      `editorial-transcript-v1-${digest(`${card.sourceAssetId}\0${card.sourceDigest}`)}`,
    )));
    return this.editorialTranscriptSpans(cards);
  }

  async editorialVisualEvidence(
    cards: readonly MaterialCard[],
  ): Promise<readonly StudioEditorialVisualEvidence[]> {
    const entries = await this.jobs.history(this.options.scope);
    return cards.flatMap((card) => {
      const entry = entries
        .filter((candidate) =>
          candidate.job.status === "succeeded"
          && candidate.job.sourceAssetId === card.sourceAssetId
          && candidate.job.sourceDigest === card.sourceDigest
          && candidate.job.sourceStartMs <= card.sourceStartMs
          && candidate.job.sourceEndMs >= card.sourceEndMs)
        .filter((candidate) => candidate.receipts.some(isTrustedEditorialVisionReceipt))
        .sort((left, right) => left.job.updatedAt.localeCompare(right.job.updatedAt)).at(-1);
      const receipt = entry?.receipts.find(isTrustedEditorialVisionReceipt);
      if (!entry || !receipt) return [];
      const observations = visionReceiptObservations(receipt);
      const localizedObservations = visionReceiptLocalizedObservations(receipt);
      const highlightCandidates = visionReceiptHighlightCandidates(receipt);
      if (observations.length === 0 && highlightCandidates.length === 0) return [];
      return [{
        sourceAssetId: card.sourceAssetId,
        sourceDigest: card.sourceDigest,
        sourceStartMs: receipt.sourceStartMs,
        sourceEndMs: receipt.sourceEndMs,
        receiptJobId: receipt.jobId,
        provider: {
          layer: "vision" as const,
          adapterName: receipt.adapterName,
          modelName: receipt.modelName,
          modelVersion: receipt.modelVersion,
          checkpointDigest: receipt.checkpointDigest,
        },
        observations,
        ...(localizedObservations.length > 0 ? { localizedObservations } : {}),
        ...(highlightCandidates.length > 0 ? { highlightCandidates } : {}),
      }];
    });
  }

  async ensureEditorialVisualEvidence(
    cards: readonly MaterialCard[],
  ): Promise<readonly StudioEditorialVisualEvidence[]> {
    const entries = await this.jobs.history(this.options.scope);
    const currentAssets = new Set(entries.flatMap((entry) => entry.receipts
      .filter((receipt) =>
        isTrustedEditorialVisionReceipt(receipt)
        && receipt.promptVersion === CURRENT_EDITORIAL_VISION_PROMPT_VERSION)
      .map((receipt) => `${receipt.sourceAssetId}\0${receipt.sourceDigest}`)));
    const missing = cards.filter((card) =>
      !currentAssets.has(`${card.sourceAssetId}\0${card.sourceDigest}`));
    if (missing.length === 0) return this.editorialVisualEvidence(cards);
    await Promise.allSettled(missing.map((card) => this.jobs.enqueue(
      this.options.scope,
      card.sourceAssetId,
      ["vision"],
      `${CURRENT_EDITORIAL_VISION_OPERATION_PREFIX}${digest(`${card.sourceAssetId}\0${card.sourceDigest}`)}`,
    )));
    return this.editorialVisualEvidence(cards);
  }

  async retestEditorialVisualEvidence(cards: readonly MaterialCard[]): Promise<Readonly<{
    evidence: readonly StudioEditorialVisualEvidence[];
    attempts: readonly Readonly<{
      sourceAssetId: string;
      sourceDigest: string;
      status: "succeeded" | "failed-retryable" | "failed-manual-review" | "cancelled";
      attempt: number;
      failureCode: string | null;
      lastStage: string | null;
      activeStageElapsedMs: number | null;
      runtimeMs: number | null;
    }>[];
  }>> {
    for (const card of cards) {
      const entries = await this.jobs.history(this.options.scope);
      const latest = entries
        .filter((entry) =>
          entry.job.sourceAssetId === card.sourceAssetId
          && entry.job.sourceDigest === card.sourceDigest
          && entry.job.operationKey.startsWith(CURRENT_EDITORIAL_VISION_OPERATION_PREFIX))
        .sort((left, right) => left.job.updatedAt.localeCompare(right.job.updatedAt)).at(-1);
      if (latest?.job.status === "failed-retryable") {
        await this.jobs.retry(this.options.scope, latest.job.id);
      } else if (!latest || ["succeeded", "cancelled", "failed-manual-review"].includes(latest.job.status)) {
        const initial = !latest;
        await this.jobs.enqueue(
          this.options.scope,
          card.sourceAssetId,
          ["vision"],
          initial
            ? `${CURRENT_EDITORIAL_VISION_OPERATION_PREFIX}${digest(`${card.sourceAssetId}\0${card.sourceDigest}`)}`
            : `${CURRENT_EDITORIAL_VISION_OPERATION_PREFIX}${digest(`${card.sourceAssetId}\0${card.sourceDigest}`)}-retest-${randomUUID()}`,
        );
      }
    }
    return {
      evidence: await this.editorialVisualEvidence(cards),
      attempts: await this.editorialVisualAttempts(cards),
    };
  }

  async editorialVisualAttempts(cards: readonly MaterialCard[]): Promise<readonly Readonly<{
    sourceAssetId: string;
    sourceDigest: string;
    status: "succeeded" | "failed-retryable" | "failed-manual-review" | "cancelled";
    attempt: number;
    failureCode: string | null;
    lastStage: string | null;
    activeStageElapsedMs: number | null;
    runtimeMs: number | null;
    analysisStrategy: string | null;
    analyzedFrames: number | null;
  }>[]> {
    const entries = await this.jobs.history(this.options.scope);
    return cards.flatMap((card) => {
      const latest = entries
        .filter((entry) =>
          entry.job.sourceAssetId === card.sourceAssetId
          && entry.job.sourceDigest === card.sourceDigest
          && entry.job.operationKey.startsWith(CURRENT_EDITORIAL_VISION_OPERATION_PREFIX)
          && !["queued", "running"].includes(entry.job.status))
        .sort((left, right) => left.job.updatedAt.localeCompare(right.job.updatedAt)).at(-1);
      if (!latest) return [];
      const receiptFailure = latest.receipts.at(-1)?.failure?.code;
      const visionReceipt = [...latest.receipts].reverse().find((receipt) => receipt.layer === "vision");
      const signals = visionReceipt?.qualitySignals;
      const highlightPrefilter = signals && typeof signals === "object"
        ? (signals as Record<string, unknown>).highlightPrefilter
        : null;
      const prefilterRecord = highlightPrefilter && typeof highlightPrefilter === "object"
        ? highlightPrefilter as Record<string, unknown>
        : null;
      const selectedFrameCount = prefilterRecord?.selectedFrameCount;
      return [{
        sourceAssetId: card.sourceAssetId,
        sourceDigest: card.sourceDigest,
        status: latest.job.status as "succeeded" | "failed-retryable" | "failed-manual-review" | "cancelled",
        attempt: latest.job.attempt,
        failureCode: receiptFailure ?? latest.job.failure?.code ?? null,
        lastStage: latest.job.failure?.stageTelemetry?.lastStage ?? null,
        activeStageElapsedMs: latest.job.failure?.stageTelemetry?.activeStageElapsedMs ?? null,
        runtimeMs: visionReceipt?.runtimeMs ?? null,
        analysisStrategy: typeof prefilterRecord?.algorithm === "string"
          ? prefilterRecord.algorithm
          : null,
        analyzedFrames: typeof selectedFrameCount === "number" && Number.isSafeInteger(selectedFrameCount)
          ? selectedFrameCount
          : null,
      }];
    });
  }

  /** Resolves one server-owned, exact-bound final attempt for reference analysis. */
  async resolveReferenceAnalysisAttempt(
    source: ReferenceAnalysisAssetSnapshot,
  ): Promise<FinalReferenceAnalysisAttempt> {
    if (
      source.authorization !== "reference-view-authorized"
      || source.asset.customerId !== this.options.scope.customerId
      || source.asset.digest.length !== 64
      || !source.sourcePath.trim()
    ) throw new Error("reference-analysis-source-binding-mismatch");
    const entries = (await this.jobs.history(this.options.scope))
      .filter((entry) =>
        entry.job.status === "succeeded"
        && entry.job.sourceAssetId === source.asset.id
        && (entry.job.sourceAssetSessionId === undefined
          ? entry.job.sessionId
          : entry.job.sourceAssetSessionId) === source.asset.sessionId
        && entry.job.sourceDigest === source.asset.digest
        && entry.job.sourcePath === source.sourcePath
        && entry.job.layers.includes("scene"))
      .sort((left, right) =>
        right.job.updatedAt.localeCompare(left.job.updatedAt)
        || right.job.id.localeCompare(left.job.id));
    const selected = entries[0];
    if (!selected) throw new Error("reference-analysis-receipts-not-found");
    return clone(selectFinalReferenceAnalysisAttempt(selected, {
      customerId: this.options.scope.customerId,
      sessionId: this.options.scope.sessionId,
      sourceAssetId: source.asset.id,
      sourceDigest: source.asset.digest,
      sourcePath: source.sourcePath,
      sourceStartMs: selected.job.sourceStartMs,
      sourceEndMs: selected.job.sourceEndMs,
    }));
  }

  async produceReferenceAnalysisAttempt(
    source: ReferenceAnalysisAssetSnapshot,
  ): Promise<FinalReferenceAnalysisAttempt> {
    if (
      source.authorization !== "reference-view-authorized"
      || source.asset.customerId !== this.options.scope.customerId
      || source.asset.digest.length !== 64
      || !source.sourcePath.trim()
    ) throw new Error("reference-analysis-source-binding-mismatch");
    const operationKey = `reference-analysis-${digest(JSON.stringify({
      assetId: source.asset.id,
      digest: source.asset.digest,
      path: source.sourcePath,
    }))}`;
    const prior = (await this.jobs.history(this.options.scope))
      .find((candidate) => candidate.job.operationKey === operationKey);
    let entry = await this.jobs.enqueueReference(
      this.options.scope,
      source,
      ["scene", "transcript", "vision"],
      operationKey,
    );
    if (prior?.job.status === "failed-retryable" && entry.job.status === "failed-retryable") {
      entry = await this.jobs.retryIfCurrent(
        this.options.scope,
        prior.job.id,
        prior.job.attempt,
      );
    }
    return clone(selectFinalReferenceAnalysisAttempt(entry, {
      customerId: this.options.scope.customerId,
      sessionId: this.options.scope.sessionId,
      sourceAssetId: source.asset.id,
      sourceDigest: source.asset.digest,
      sourcePath: source.sourcePath,
      sourceStartMs: entry.job.sourceStartMs,
      sourceEndMs: entry.job.sourceEndMs,
    }));
  }

  async recordMaterialSuitability(
    input: StudioMaterialSuitabilityInput,
  ): Promise<StudioMaterialSuitability> {
    const decision = input.decision;
    if (decision !== "suitable" && decision !== "not-suitable") {
      throw new Error("studio-material-suitability-decision-invalid");
    }
    const demandId = requireText(input.demandId, "studio-material-suitability-demand-required", 1_000);
    const matchAssessmentId = requireText(
      input.matchAssessmentId,
      "studio-material-suitability-match-required",
      1_000,
    );
    const receiptJobId = requireText(input.receiptJobId, "studio-material-suitability-receipt-required", 1_000);
    const sourceAssetId = requireText(input.sourceAssetId, "studio-material-suitability-asset-required", 1_000);
    const sourceDigest = requireDigest(input.sourceDigest);
    const sourceStartMs = requireMilliseconds(input.sourceStartMs);
    const sourceEndMs = requireMilliseconds(input.sourceEndMs);
    const reason = requireText(input.reason, "studio-material-suitability-reason-required", 1_000);
    const operationKey = requireText(input.operationKey, "studio-material-suitability-operation-key-required", 200);
    if (sourceEndMs <= sourceStartMs) throw new Error("studio-material-suitability-binding-mismatch");

    const receipt = (await this.jobs.history(this.options.scope))
      .flatMap((entry) => entry.receipts)
      .find((candidate) => candidate.jobId === receiptJobId);
    if (!receipt) throw new Error("studio-material-suitability-receipt-not-found");
    if (receipt.status !== "succeeded") throw new Error("studio-material-suitability-receipt-not-succeeded");
    if (
      receipt.sourceAssetId !== sourceAssetId
      || receipt.sourceDigest !== sourceDigest
      || receipt.sourceStartMs !== sourceStartMs
      || receipt.sourceEndMs !== sourceEndMs
    ) throw new Error("studio-material-suitability-binding-mismatch");

    const history = await this.options.studio.listStudioHistory(this.options.scope);
    const demand = history.demands.find((candidate) => candidate.id === demandId);
    if (!demand) throw new Error("studio-material-suitability-demand-not-found");
    if (demand.status !== "current") throw new Error("studio-material-suitability-demand-not-current");
    const assessment = history.assessments.find((candidate) => candidate.id === matchAssessmentId);
    if (!assessment) throw new Error("studio-material-suitability-match-not-found");
    if (assessment.demandId !== demand.id) throw new Error("studio-material-suitability-match-mismatch");

    const normalized = {
      decision, demandId, matchAssessmentId, receiptJobId, sourceAssetId,
      sourceDigest, sourceStartMs, sourceEndMs, reason,
    };
    const bodyDigest = digest(JSON.stringify(normalized));
    return this.withReviewState(async (state) => {
      const existing = state.suitabilityReviews.find((event) => event.operationKey === operationKey);
      if (existing && existing.bodyDigest !== bodyDigest) {
        throw new Error("studio-material-suitability-operation-key-conflict");
      }
      const event: StoredSuitability = existing ?? {
          id: `material_suitability_${digest(`${this.options.scope.customerId}\0${this.options.scope.sessionId}\0${this.options.scope.actorId}\0${operationKey}`)}`,
          ...normalized,
          operationKey,
          bodyDigest,
          createdAt: this.timestamp(),
        };
      if (!existing) {
        await this.persistReviewState({
          ...state,
          suitabilityReviews: [...state.suitabilityReviews, event],
        });
      }
      await this.recordSuitabilityPreference(event);
      return publicSuitability(event);
    });
  }

  async review(input: StudioAnalysisReviewInput): Promise<StudioAnalysisReview> {
    const action = parseReviewAction(input.action);
    const receiptJobId = requireText(input.receiptJobId, "studio-analysis-review-receipt-required", 1_000);
    const sourceAssetId = requireText(input.sourceAssetId, "studio-analysis-review-asset-required", 1_000);
    const sourceDigest = requireDigest(input.sourceDigest);
    const sourceStartMs = requireMilliseconds(input.sourceStartMs);
    const sourceEndMs = requireMilliseconds(input.sourceEndMs);
    if (sourceEndMs <= sourceStartMs) throw new Error("studio-analysis-review-binding-mismatch");
    const target = requireText(input.target, "studio-analysis-review-target-required", 1_000);
    const correction = input.correction === null
      ? null
      : requireText(input.correction, "studio-analysis-review-correction-invalid", 1_000);
    const operationKey = requireText(input.operationKey, "studio-analysis-review-operation-key-required", 200);

    // Resolve the receipt in this authenticated scope before consulting idempotency state.
    const receipt = (await this.jobs.history(this.options.scope))
      .flatMap((entry) => entry.receipts)
      .find((candidate) => candidate.jobId === receiptJobId);
    if (!receipt) throw new Error("studio-analysis-review-receipt-not-found");
    if (receipt.status !== "succeeded") throw new Error("studio-analysis-review-receipt-not-succeeded");
    if (
      receipt.sourceAssetId !== sourceAssetId
      || receipt.sourceDigest !== sourceDigest
      || receipt.sourceStartMs !== sourceStartMs
      || receipt.sourceEndMs !== sourceEndMs
    ) throw new Error("studio-analysis-review-binding-mismatch");
    const scopedEntries = (await this.jobs.history(this.options.scope)).map((entry) =>
      publicAnalysisEntry(entry, this.evidenceByJobId.get(entry.job.id) ?? null));
    const groups = await this.duplicateGroups(scopedEntries);
    assertReviewTarget(receipt, action, target, groups);
    if (action === "correct-suggestion" && correction === null) {
      throw new Error("studio-analysis-review-correction-required");
    }
    if (action !== "correct-suggestion" && correction !== null) {
      throw new Error("studio-analysis-review-correction-not-allowed");
    }

    const normalized = {
      action,
      receiptJobId,
      sourceAssetId,
      sourceDigest,
      sourceStartMs,
      sourceEndMs,
      target,
      correction,
    };
    const bodyDigest = digest(JSON.stringify(normalized));
    return this.withReviewState(async (state) => {
      const existing = state.reviews.find((candidate) => candidate.operationKey === operationKey);
      if (existing && existing.bodyDigest !== bodyDigest) {
        throw new Error("studio-analysis-review-operation-key-conflict");
      }
      if (existing) {
        await this.recordReviewPreference(existing);
        return publicReview(existing);
      }

      if (action === "keep-from-duplicate-group") {
        if (!this.embeddingIndex) throw new Error("studio-analysis-review-target-mismatch");
        const group = groups.find((candidate) => candidate.id === target);
        if (!group) throw new Error("studio-analysis-review-target-mismatch");
        await this.embeddingIndex.recordKeepDecision(this.options.scope, {
          operationKey,
          groupAssetIds: group.sourceAssetIds,
          keepAssetIds: [sourceAssetId],
        });
      }

      const review: StoredReview = Object.freeze({
        id: `analysis_review_${digest(`${this.options.scope.customerId}\0${this.options.scope.sessionId}\0${this.options.scope.actorId}\0${operationKey}`)}`,
        ...normalized,
        operationKey,
        bodyDigest,
        createdAt: this.timestamp(),
      });
      await this.persistReviewState({
        ...state,
        reviews: [...state.reviews, review],
      });
      await this.recordReviewPreference(review);
      return publicReview(review);
    });
  }

  async modelsStatus(): Promise<StudioModelsStatus> {
    return validateModelsStatus(await this.modelStatusReader());
  }

  async rank(demandId: string) {
    if (!this.options.preferenceRanker) throw new Error("studio-preference-ranker-unavailable");
    await this.reconcilePersistedPreferences();
    const ranked = await this.options.studio.rankMaterials(
      this.options.scope,
      requireText(demandId, "demand-id-required", 1_000),
    );
    return ranked.map(({ customerId: _customerId, sessionId: _sessionId, ...candidate }) =>
      candidate);
  }

  private async resolveMaterialInterval(input: Parameters<StudioSourceIntervalResolver>[0]) {
    const cards = await this.options.library.inventoryAt(
      input.scope.actorId,
      input.scope.customerId,
      this.timestamp(),
    );
    const matching = cards.filter((card) =>
      card.sessionId === input.scope.sessionId && card.sourceAssetId === input.asset.id);
    if (!matching.length) return null;
    const sourceEndMs = Math.max(...matching.map((card) => card.sourceEndMs));
    if (!Number.isSafeInteger(sourceEndMs) || sourceEndMs <= 0) return null;
    return { sourceStartMs: 0, sourceEndMs, sourceDurationMs: sourceEndMs };
  }

  private async indexEmbeddingReceipt(entry: StudioAnalysisHistoryEntry): Promise<void> {
    if (!this.embeddingIndex) return;
    const receipt = entry.receipts.find((candidate) =>
      candidate.layer === "embedding" && candidate.status === "succeeded");
    if (!receipt) return;
    const signals = receipt.qualitySignals as Record<string, unknown>;
    const artifact = signals.artifact as Record<string, unknown> | undefined;
    const frames = signals.representativeFrames;
    if (
      !Array.isArray(signals.embedding)
      || !Array.isArray(frames)
      || !artifact
      || artifact.id !== receipt.modelName
      || artifact.version !== receipt.modelVersion
      || artifact.sha256 !== receipt.checkpointDigest
      || receipt.checkpointDigest === null
    ) throw new Error("studio-analysis-embedding-receipt-invalid");
    const scenes = frames.map((frame) => {
      if (!frame || typeof frame !== "object" || Array.isArray(frame)) {
        throw new Error("studio-analysis-embedding-receipt-invalid");
      }
      const value = frame as Record<string, unknown>;
      if (
        value.sourceDigest !== receipt.sourceDigest
        || !Number.isSafeInteger(value.startMs)
        || !Number.isSafeInteger(value.endMs)
        || (value.startMs as number) < receipt.sourceStartMs
        || (value.endMs as number) > receipt.sourceEndMs
      ) throw new Error("studio-analysis-embedding-receipt-invalid");
      return { startMs: value.startMs as number, endMs: value.endMs as number };
    });
    await this.embeddingIndex.add(this.options.scope, {
      assetId: receipt.sourceAssetId,
      sourceDigest: receipt.sourceDigest,
      embeddingSpace: {
        artifactId: receipt.modelName,
        modelVersion: receipt.modelVersion,
        checkpointDigest: receipt.checkpointDigest,
      },
      vector: signals.embedding as number[],
      scenes,
    });
  }

  private async duplicateGroups(
    entries: readonly PublicStudioAnalysisEntry[],
  ): Promise<StudioPublicDuplicateGroup[]> {
    if (!this.embeddingIndex) return [];
    const groups = await this.embeddingIndex.groups(this.options.scope, 0.97);
    return groups.map((group) => {
      const sourceAssetIds = [...group.assetIds].sort();
      const receipt = entries.flatMap((entry) => entry.receipts).find((candidate) =>
        candidate.layer === "embedding"
        && candidate.status === "succeeded"
        && candidate.sourceAssetId === sourceAssetIds[0]);
      return {
        id: `duplicate_group_${digest(`${group.kind}\0${sourceAssetIds.join("\0")}`)}`,
        kind: group.kind,
        sourceAssetIds,
        receiptJobId: receipt?.jobId ?? null,
      };
    });
  }

  private async resolveWorkerContext(input: Readonly<{
    scope: StudioScope;
    job: ModelAnalysisJobV1;
    priorReceipts: readonly ModelAnalysisReceiptV1[];
  }>): Promise<StudioTrustedWorkerContext | undefined> {
    if (input.job.layer !== "vision") return undefined;
    const transcript = input.priorReceipts.find((receipt) =>
      receipt.layer === "transcript" && receipt.status === "succeeded");
    const spans = transcript === undefined ? [] : parseTranscriptSpans(transcript, input.job);
    const history = await this.options.studio.listStudioHistory(input.scope);
    const demand = [...history.demands]
      .filter((candidate) => candidate.status === "current")
      .sort((left, right) => right.version - left.version)[0];
    const assessment = demand === undefined ? undefined : [...history.assessments]
      .filter((candidate) => candidate.demandId === demand.id)
      .sort((left, right) => right.version - left.version)[0];
    return {
      customerId: input.scope.customerId,
      sessionId: input.scope.sessionId,
      sourceAssetId: input.job.sourceAssetId,
      sourceDigest: input.job.sourceDigest,
      sourceStartMs: input.job.sourceStartMs,
      sourceEndMs: input.job.sourceEndMs,
      transcriptSpans: spans,
      demand: demand === undefined || assessment === undefined ? null : {
        demandId: demand.id,
        matchAssessmentId: assessment.id,
        sourceDigest: input.job.sourceDigest,
        sourceStartMs: input.job.sourceStartMs,
        sourceEndMs: input.job.sourceEndMs,
        text: JSON.stringify({
          goal: demand.goal,
          targetAudience: demand.targetAudience,
          desiredEffect: demand.desiredEffect,
          mustInclude: demand.mustInclude,
          mustAvoid: demand.mustAvoid,
        }),
      },
    };
  }

  private async currentDemandBinding(): Promise<Readonly<{
    demandId: string;
    matchAssessmentId: string;
  }> | null> {
    const history = await this.options.studio.listStudioHistory(this.options.scope);
    const demand = [...history.demands]
      .filter((candidate) => candidate.status === "current")
      .sort((left, right) => right.version - left.version)[0];
    if (!demand) return null;
    const assessment = [...history.assessments]
      .filter((candidate) => candidate.demandId === demand.id)
      .sort((left, right) => right.version - left.version)[0];
    return assessment ? { demandId: demand.id, matchAssessmentId: assessment.id } : null;
  }

  private async loadReviewState(): Promise<StoredHumanReviewState> {
    if (!this.options.reviewStatePath) return { reviews: [], suitabilityReviews: [] };
    const raw = await readFile(this.options.reviewStatePath, "utf8").catch((error: unknown) => {
      if (hasCode(error, "ENOENT")) return null;
      throw error;
    });
    if (raw === null) return { reviews: [], suitabilityReviews: [] };
    try {
      const value = JSON.parse(raw) as Record<string, unknown>;
      if (
        value.schemaVersion !== 1
        || value.actorId !== this.options.scope.actorId
        || value.customerId !== this.options.scope.customerId
        || value.sessionId !== this.options.scope.sessionId
        || !Array.isArray(value.suitabilityReviews)
        || (value.reviews !== undefined && !Array.isArray(value.reviews))
      ) throw new Error();
      const reviews = (value.reviews ?? []) as StoredReview[];
      const suitabilityReviews = value.suitabilityReviews as StoredSuitability[];
      if (
        new Set(reviews.map((event) => event.operationKey)).size !== reviews.length
        || new Set(suitabilityReviews.map((event) => event.operationKey)).size !== suitabilityReviews.length
      ) throw new Error();
      for (const event of reviews) assertStoredReview(event);
      for (const event of suitabilityReviews) assertStoredSuitability(event);
      return clone({ reviews, suitabilityReviews });
    } catch {
      throw new Error("studio-model-review-state-invalid");
    }
  }

  private async persistReviewState(state: StoredHumanReviewState): Promise<void> {
    const path = this.options.reviewStatePath;
    if (!path) throw new Error("studio-model-review-state-unavailable");
    await mkdir(dirname(path), { recursive: true });
    const pending = `${path}.next-${randomUUID()}`;
    try {
      await writeFile(pending, `${JSON.stringify({
        schemaVersion: 1,
        actorId: this.options.scope.actorId,
        customerId: this.options.scope.customerId,
        sessionId: this.options.scope.sessionId,
        reviews: state.reviews,
        suitabilityReviews: state.suitabilityReviews,
      }, null, 2)}\n`, { encoding: "utf8", flag: "wx" });
      await rename(pending, path);
    } finally {
      await unlink(pending).catch(() => undefined);
    }
  }

  private async withReviewState<T>(work: (state: StoredHumanReviewState) => Promise<T>): Promise<T> {
    const key = this.options.reviewStatePath ?? `${this.options.scope.customerId}\0${this.options.scope.sessionId}`;
    const previous = StudioModelRuntime.reviewStateQueues.get(key) ?? Promise.resolve();
    const current = previous.catch(() => undefined).then(async () => work(await this.loadReviewState()));
    const tail = current.then(() => undefined, () => undefined);
    StudioModelRuntime.reviewStateQueues.set(key, tail);
    return current.finally(() => {
      if (StudioModelRuntime.reviewStateQueues.get(key) === tail) StudioModelRuntime.reviewStateQueues.delete(key);
    });
  }

  private async reconcilePersistedPreferences(): Promise<void> {
    await this.withReviewState(async (state) => {
      const reviews = [...state.reviews].sort((left, right) => compareCodePoints(left.id, right.id));
      const suitability = [...state.suitabilityReviews]
        .sort((left, right) => compareCodePoints(left.id, right.id));
      await this.options.preferenceRanker?.reconcileSuggestionReviews(
        this.options.scope,
        reviews.map(reviewPreferenceInput),
      );
      await this.options.preferenceRanker?.reconcileMaterialSuitability(
        this.options.scope,
        suitability.map(suitabilityPreferenceInput),
      );
    });
  }

  private async recordReviewPreference(review: StoredReview): Promise<void> {
    await this.options.preferenceRanker?.recordSuggestionReview(
      this.options.scope,
      reviewPreferenceInput(review),
    );
  }

  private async recordSuitabilityPreference(event: StoredSuitability): Promise<void> {
    await this.options.preferenceRanker?.recordMaterialSuitability(
      this.options.scope,
      suitabilityPreferenceInput(event),
    );
  }

  private timestamp(): string {
    const value = this.now();
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(value)) {
      throw new Error("studio-analysis-timestamp-invalid");
    }
    return value;
  }
}

export function createModelCheckerStatusReader(input: Readonly<{
  checkerPath: string;
  lockPath: string;
  modelRoot: string;
}>): StudioModelStatusReader {
  const execFileAsync = promisify(execFile);
  return async () => {
    try {
      const result = await execFileAsync(process.execPath, [
        input.checkerPath,
        "--json",
        "--lock", input.lockPath,
        "--root", input.modelRoot,
      ], {
        encoding: "utf8",
        timeout: STUDIO_MODEL_CHECKER_TIMEOUT_MS,
        maxBuffer: 256 * 1024,
      });
      const value = JSON.parse(result.stdout) as Record<string, unknown>;
      const artifacts = Array.isArray(value.artifacts) ? value.artifacts.map((artifact) => {
        if (!artifact || typeof artifact !== "object" || Array.isArray(artifact)) throw new Error();
        const record = artifact as Record<string, unknown>;
        return {
          id: requireText(record.id, "studio-model-status-invalid", 1_000),
          version: requireText(record.version, "studio-model-status-invalid", 1_000),
          status: requireText(record.status, "studio-model-status-invalid", 200),
        };
      }) : [];
      const workerRuntimeValue = Array.isArray(value.workerRuntimes)
        ? value.workerRuntimes[0]
        : undefined;
      const workerRuntime = workerRuntimeValue
        && typeof workerRuntimeValue === "object"
        && !Array.isArray(workerRuntimeValue)
        ? { status: parseWorkerRuntimeStatus((workerRuntimeValue as Record<string, unknown>).status) }
        : undefined;
      const runtimeReady = workerRuntime?.status === "runtime-real-probe-succeeded";
      const ready = artifacts.length > 0
        && artifacts.every((artifact) => artifact.status === "ready")
        && runtimeReady;
      return validateModelsStatus({
        mode: ready ? "local" : "offline-fallback",
        localOnly: true,
        degraded: !ready,
        artifacts,
        visionProbe: typeof value.visionProbe === "string"
          ? value.visionProbe
          : "not-run: model-status-unavailable",
        ...(workerRuntime === undefined ? {} : { workerRuntime }),
      });
    } catch {
      return {
        mode: "offline-fallback",
        localOnly: true,
        degraded: true,
        artifacts: [],
        visionProbe: "not-run: model-status-unavailable",
      };
    }
  };
}

function publicAnalysisEntry(entry: StudioAnalysisHistoryEntry, evidence: unknown = null) {
  const {
    actorId: _actorId,
    customerId: _customerId,
    sessionId: _sessionId,
    sourceAssetSessionId: _sourceAssetSessionId,
    sourcePath: _sourcePath,
    operationKey: _operationKey,
    requestDigest: _requestDigest,
    runnerInstanceId: _runnerInstanceId,
    ...job
  } = entry.job;
  return clone({
    job,
    receipts: entry.receipts.map(publicReceipt),
    evidence,
  });
}

export function isTrustedEditorialVisionReceipt(receipt: ModelAnalysisReceiptV1): boolean {
  return receipt.layer === "vision"
    && receipt.status === "succeeded"
    && receipt.providerMode === "local"
    && receipt.synthetic === false;
}

function visionReceiptObservations(receipt: ModelAnalysisReceiptV1): string[] {
  const values: string[] = [...receipt.observations, ...receipt.suggestedTags];
  const signals = receipt.qualitySignals;
  if (typeof signals === "object" && signals !== null && !Array.isArray(signals)) {
    const visual = (signals as Record<string, unknown>).visualObservations;
    if (Array.isArray(visual)) {
      for (const row of visual) {
        if (!row || typeof row !== "object" || Array.isArray(row)) continue;
        const value = row as Record<string, unknown>;
        if (
          typeof value.kind === "string"
          && typeof value.text === "string"
          && Number.isSafeInteger(value.startMs)
          && Number.isSafeInteger(value.endMs)
        ) values.push(`${value.kind}|${value.startMs}|${value.endMs}|${value.text}`);
      }
    }
    const pending = (signals as Record<string, unknown>).pendingObservations;
    if (Array.isArray(pending)) {
      for (const value of pending) if (typeof value === "string") values.push(value);
    }
  }
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))].slice(0, 64);
}

function visionReceiptLocalizedObservations(
  receipt: ModelAnalysisReceiptV1,
): NonNullable<StudioEditorialVisualEvidence["localizedObservations"]> {
  const signals = receipt.qualitySignals;
  if (typeof signals !== "object" || signals === null || Array.isArray(signals)) return [];
  const visual = (signals as Record<string, unknown>).visualObservations;
  if (!Array.isArray(visual)) return [];
  return visual.flatMap((row) => {
    if (!row || typeof row !== "object" || Array.isArray(row)) return [];
    const value = row as Record<string, unknown>;
    if (
      !["visible-person", "visible-product", "visible-action", "visible-scene"].includes(String(value.kind))
      || typeof value.text !== "string"
      || !Number.isSafeInteger(value.startMs)
      || !Number.isSafeInteger(value.endMs)
      || typeof value.confidence !== "number"
      || !Number.isFinite(value.confidence)
      || (value.startMs as number) < receipt.sourceStartMs
      || (value.endMs as number) > receipt.sourceEndMs
      || (value.endMs as number) <= (value.startMs as number)
      || value.confidence < 0
      || value.confidence > 1
    ) return [];
    return [{
      kind: value.kind as "visible-person" | "visible-product" | "visible-action" | "visible-scene",
      text: value.text.trim().slice(0, 1_000),
      startMs: value.startMs as number,
      endMs: value.endMs as number,
      confidence: value.confidence,
    }];
  }).filter((row) => row.text.length > 0).slice(0, 64);
}

function visionReceiptHighlightCandidates(
  receipt: ModelAnalysisReceiptV1,
): NonNullable<StudioEditorialVisualEvidence["highlightCandidates"]> {
  const signals = receipt.qualitySignals;
  if (typeof signals !== "object" || signals === null || Array.isArray(signals)) return [];
  const rows = (signals as Record<string, unknown>).highlightCandidates;
  if (!Array.isArray(rows)) return [];
  return rows.flatMap((row) => {
    if (!row || typeof row !== "object" || Array.isArray(row)) return [];
    const value = row as Record<string, unknown>;
    if (
      typeof value.reason !== "string" || !value.reason.trim()
      || !Number.isSafeInteger(value.startMs) || !Number.isSafeInteger(value.endMs)
      || typeof value.confidence !== "number" || !Number.isFinite(value.confidence)
      || (value.startMs as number) < receipt.sourceStartMs
      || (value.endMs as number) > receipt.sourceEndMs
      || (value.endMs as number) <= (value.startMs as number)
      || value.confidence < 0 || value.confidence > 1
    ) return [];
    return [{
      reason: value.reason.trim().slice(0, 1_000),
      startMs: value.startMs as number,
      endMs: value.endMs as number,
      confidence: value.confidence,
    }];
  }).slice(0, 3);
}

function publicReceipt(receipt: ModelAnalysisReceiptV1) {
  const {
    customerId: _customerId,
    sessionId: _sessionId,
    sourcePath: _sourcePath,
    operationKey: _operationKey,
    ...safe
  } = receipt;
  return safe;
}

function publicEvidence(value: Awaited<ReturnType<PrivateContentStudio["analyzeProviderEvidence"]>>) {
  const { customerId: _customerId, sessionId: _sessionId, ...card } = value.card;
  return {
    card,
    evidence: value.evidence,
    degradedReasons: value.degradedReasons,
    providerIdentities: value.providerIdentities,
  };
}

function publicSuitability(value: StoredSuitability): StudioMaterialSuitability {
  const { operationKey: _operationKey, bodyDigest: _bodyDigest, ...event } = value;
  return clone(event);
}

function publicReview(value: StoredReview): StudioAnalysisReview {
  const { operationKey: _operationKey, bodyDigest: _bodyDigest, ...event } = value;
  return clone(event);
}

function reviewPreferenceInput(review: StoredReview) {
  return {
    operationKey: `analysis-review:${review.operationKey}`,
    eventId: review.id,
    action: review.action === "keep-from-duplicate-group"
      ? "accept-suggestion" as const
      : review.action,
    target: review.target,
    correction: review.correction,
    receiptJobId: review.receiptJobId,
    source: {
      sourceAssetId: review.sourceAssetId,
      sourceDigest: review.sourceDigest,
      sourceStartMs: review.sourceStartMs,
      sourceEndMs: review.sourceEndMs,
    },
    createdAt: review.createdAt,
  };
}

function suitabilityPreferenceInput(event: StoredSuitability) {
  return {
    operationKey: `material-suitability:${event.operationKey}`,
    eventId: event.id,
    decision: event.decision,
    demandId: event.demandId,
    matchAssessmentId: event.matchAssessmentId,
    receiptJobId: event.receiptJobId,
    source: {
      sourceAssetId: event.sourceAssetId,
      sourceDigest: event.sourceDigest,
      sourceStartMs: event.sourceStartMs,
      sourceEndMs: event.sourceEndMs,
    },
    reason: event.reason,
    createdAt: event.createdAt,
  };
}

function assertStoredReview(value: StoredReview): void {
  const keys = [
    "id", "action", "receiptJobId", "sourceAssetId", "sourceDigest", "sourceStartMs",
    "sourceEndMs", "target", "correction", "createdAt", "operationKey", "bodyDigest",
  ].sort();
  if (
    !value || typeof value !== "object"
    || Object.keys(value).sort().join("\0") !== keys.join("\0")
    || !STUDIO_ANALYSIS_REVIEW_ACTIONS.includes(value.action)
    || !value.id?.trim()
    || !value.receiptJobId?.trim()
    || !value.sourceAssetId?.trim()
    || !/^[a-f0-9]{64}$/.test(value.sourceDigest)
    || !Number.isSafeInteger(value.sourceStartMs)
    || !Number.isSafeInteger(value.sourceEndMs)
    || value.sourceEndMs <= value.sourceStartMs
    || !value.target?.trim()
    || (value.correction !== null && !value.correction.trim())
    || (value.action === "correct-suggestion") !== (value.correction !== null)
    || !value.operationKey?.trim()
    || !/^[a-f0-9]{64}$/.test(value.bodyDigest)
    || !Number.isFinite(Date.parse(value.createdAt))
  ) throw new Error("studio-model-review-state-invalid");
}

function assertStoredSuitability(value: StoredSuitability): void {
  const keys = [
    "id", "decision", "demandId", "matchAssessmentId", "receiptJobId", "sourceAssetId",
    "sourceDigest", "sourceStartMs", "sourceEndMs", "reason", "createdAt", "operationKey", "bodyDigest",
  ].sort();
  if (
    !value || typeof value !== "object"
    || Object.keys(value).sort().join("\0") !== keys.join("\0")
    || (value.decision !== "suitable" && value.decision !== "not-suitable")
    || !value.id?.trim()
    || !value.demandId?.trim()
    || !value.matchAssessmentId?.trim()
    || !value.receiptJobId?.trim()
    || !value.sourceAssetId?.trim()
    || !/^[a-f0-9]{64}$/.test(value.sourceDigest)
    || !Number.isSafeInteger(value.sourceStartMs)
    || !Number.isSafeInteger(value.sourceEndMs)
    || value.sourceEndMs <= value.sourceStartMs
    || !value.reason?.trim()
    || !value.operationKey?.trim()
    || !/^[a-f0-9]{64}$/.test(value.bodyDigest)
    || !Number.isFinite(Date.parse(value.createdAt))
  ) throw new Error("studio-model-review-state-invalid");
}

function assertReviewTarget(
  receipt: ModelAnalysisReceiptV1,
  action: StudioAnalysisReviewAction,
  target: string,
  derivedGroups: readonly StudioPublicDuplicateGroup[],
): void {
  if (action === "keep-from-duplicate-group") {
    const derived = derivedGroups.some((group) =>
      group.id === target
      && group.sourceAssetIds.includes(receipt.sourceAssetId));
    if (!derived) throw new Error("studio-analysis-review-target-mismatch");
    return;
  }
  const suggestions = new Set([...receipt.suggestedTags, ...receipt.observations]);
  if (!suggestions.has(target)) throw new Error("studio-analysis-review-target-mismatch");
}

function compareCodePoints(left: string, right: string): number {
  const leftScalars = Array.from(left);
  const rightScalars = Array.from(right);
  const length = Math.min(leftScalars.length, rightScalars.length);
  for (let index = 0; index < length; index += 1) {
    const leftValue = leftScalars[index]!.codePointAt(0)!;
    const rightValue = rightScalars[index]!.codePointAt(0)!;
    if (leftValue !== rightValue) return leftValue < rightValue ? -1 : 1;
  }
  return leftScalars.length - rightScalars.length;
}

function parseTranscriptSpans(
  receipt: ModelAnalysisReceiptV1,
  job: Pick<ModelAnalysisJobV1, "sourceDigest" | "sourceStartMs" | "sourceEndMs">,
): StudioTrustedWorkerContext["transcriptSpans"] {
  const spans = (receipt.qualitySignals as Record<string, unknown>).spans;
  if (!Array.isArray(spans)) throw new Error("studio-analysis-trusted-context-invalid");
  return spans.map((span) => {
    if (!span || typeof span !== "object" || Array.isArray(span)) {
      throw new Error("studio-analysis-trusted-context-invalid");
    }
    const value = span as Record<string, unknown>;
    if (
      !Number.isSafeInteger(value.startMs)
      || !Number.isSafeInteger(value.endMs)
      || (value.startMs as number) < job.sourceStartMs
      || (value.endMs as number) > job.sourceEndMs
      || (value.endMs as number) <= (value.startMs as number)
      || typeof value.text !== "string"
      || !value.text.trim()
    ) throw new Error("studio-analysis-trusted-context-invalid");
    return {
      sourceDigest: job.sourceDigest,
      startMs: value.startMs as number,
      endMs: value.endMs as number,
      text: value.text.trim(),
    };
  });
}

function parseLayers(input: readonly StudioAnalysisLayer[]): StudioAnalysisLayer[] {
  if (!Array.isArray(input) || input.length === 0) throw new Error("studio-analysis-layers-required");
  const layers = input.map((value) => {
    const result = StudioAnalysisLayerSchema.safeParse(value);
    if (!result.success) throw new Error("studio-analysis-layer-invalid");
    return result.data;
  });
  if (new Set(layers).size !== layers.length) throw new Error("studio-analysis-duplicate-layer");
  return layers;
}

function parseReviewAction(value: unknown): StudioAnalysisReviewAction {
  if (!STUDIO_ANALYSIS_REVIEW_ACTIONS.includes(value as StudioAnalysisReviewAction)) {
    throw new Error("studio-analysis-review-action-invalid");
  }
  return value as StudioAnalysisReviewAction;
}

function requireText(value: unknown, code: string, maximum: number): string {
  if (typeof value !== "string" || !value.trim() || value.trim().length > maximum) {
    throw new Error(code);
  }
  return value.trim();
}

function requireDigest(value: unknown): string {
  if (typeof value !== "string" || !/^[a-f0-9]{64}$/.test(value)) {
    throw new Error("studio-analysis-review-binding-mismatch");
  }
  return value;
}

function requireMilliseconds(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new Error("studio-analysis-review-binding-mismatch");
  }
  return value as number;
}

function validateModelsStatus(value: StudioModelsStatus): StudioModelsStatus {
  if (
    (value.mode !== "local" && value.mode !== "offline-fallback")
    || value.localOnly !== true
    || typeof value.degraded !== "boolean"
    || !Array.isArray(value.artifacts)
    || typeof value.visionProbe !== "string"
  ) throw new Error("studio-model-status-invalid");
  const artifacts = value.artifacts.map((artifact) => ({
    id: requireText(artifact.id, "studio-model-status-invalid", 1_000),
    version: requireText(artifact.version, "studio-model-status-invalid", 1_000),
    status: requireText(artifact.status, "studio-model-status-invalid", 200),
  }));
  const allowedRuntimeStatuses = new Set([
    "runtime-unapproved",
    "runtime-not-installed",
    "runtime-integrity-failed",
    "runtime-installed-probe-required",
    "runtime-real-probe-failed",
    "runtime-real-probe-succeeded",
  ]);
  const workerRuntime = value.workerRuntime === undefined
    ? undefined
    : { status: requireText(value.workerRuntime.status, "studio-model-status-invalid", 200) };
  if (workerRuntime !== undefined && !allowedRuntimeStatuses.has(workerRuntime.status)) {
    throw new Error("studio-model-status-invalid");
  }
  const allReady = artifacts.length > 0
    && artifacts.every((artifact) => artifact.status === "ready")
    && workerRuntime?.status === "runtime-real-probe-succeeded";
  if ((value.mode === "local") !== allReady || value.degraded === allReady) {
    throw new Error("studio-model-status-invalid");
  }
  return clone({
    mode: value.mode,
    localOnly: true as const,
    degraded: value.degraded,
    artifacts,
    visionProbe: value.visionProbe,
    ...(workerRuntime === undefined ? {} : { workerRuntime }),
  }) as StudioModelsStatus;
}

function parseWorkerRuntimeStatus(value: unknown): NonNullable<StudioModelsStatus["workerRuntime"]>["status"] {
  const statuses = [
    "runtime-unapproved",
    "runtime-not-installed",
    "runtime-integrity-failed",
    "runtime-installed-probe-required",
    "runtime-real-probe-failed",
    "runtime-real-probe-succeeded",
  ] as const;
  if (!statuses.includes(value as typeof statuses[number])) {
    throw new Error("studio-model-status-invalid");
  }
  return value as typeof statuses[number];
}

function assertScope(scope: StudioScope): void {
  requireText(scope?.actorId, "actor-id-required", 1_000);
  requireText(scope?.customerId, "customer-id-required", 1_000);
  requireText(scope?.sessionId, "session-id-required", 1_000);
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

function hasCode(error: unknown, code: string): boolean {
  return Boolean(error && typeof error === "object" && "code" in error && error.code === code);
}
