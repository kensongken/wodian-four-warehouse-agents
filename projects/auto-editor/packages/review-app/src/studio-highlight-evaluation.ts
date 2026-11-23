import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

import type { MaterialCard } from "@wearable/contracts";
import type { StudioEditorialVisualEvidence } from "@wearable/mobile-agent";

export type StudioHighlightLabel = Readonly<{
  id: string;
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  reason: string;
  confirmationMode: "manual-range" | "accepted-model-candidate";
  modelReceiptJobId: string | null;
  provenance: "human-confirmed";
  createdAt: string;
}>;

export type StudioHighlightCandidateRejection = Readonly<{
  id: string;
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  receiptJobId: string;
  reason: "not-a-highlight";
  provenance: "human-confirmed";
  createdAt: string;
}>;

export type StudioHighlightEvaluation = Readonly<{
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  status: "awaiting-human-label" | "no-discriminative-model-candidate" | "passed" | "failed";
  threshold: Readonly<{ minimumIoU: 0.5 }>;
  labels: readonly StudioHighlightLabel[];
  modelCandidates: readonly Readonly<{ startMs: number; endMs: number; text: string; receiptJobId: string }>[];
  scores: readonly Readonly<{ labelId: string; bestIoU: number; bestCandidateStartMs: number | null; bestCandidateEndMs: number | null }>[];
  publishable: false;
  trainingEligible: false;
  boundary: "human-label-required-model-evaluation-only";
}>;

export type StudioHighlightAttempt = Readonly<{
  sourceAssetId: string;
  sourceDigest: string;
  status: "succeeded" | "failed-retryable" | "failed-manual-review" | "cancelled";
  attempt: number;
  failureCode: string | null;
  lastStage: string | null;
  activeStageElapsedMs: number | null;
  runtimeMs: number | null;
  analysisStrategy?: string | null;
  analyzedFrames?: number | null;
}>;

export type StudioHighlightCoverageProfile = Readonly<{
  sourceAssetId: string;
  sourceDigest: string;
  lighting: "stable" | "mixed" | "low";
  motion: "static-display" | "handheld-product" | "making-action" | "person-speaking" | "device-motion";
  environment: "indoor-controlled" | "indoor-mixed" | "outdoor";
  provenance: "human-confirmed";
  updatedAt: string;
}>;

export type StudioHighlightBatchGate = Readonly<{
  status: "insufficient-samples" | "below-pilot-threshold" | "pilot-threshold-met";
  thresholds: Readonly<{ minimumLabeledVideos: 10; minimumPassRate: 0.8; maximumP95RuntimeMs: 180_000 }>;
  labeledVideos: number;
  remainingVideos: number;
  passedVideos: number;
  failedVideos: number;
  passRate: number | null;
  attemptedVideos: number;
  evidenceCoverageComplete: boolean;
  successfulRuntimeSamples: number;
  p95RuntimeMs: number | null;
  failureStages: readonly Readonly<{ stage: string; count: number }>[];
  sampleCoverage: Readonly<{
    status: "insufficient-variety" | "pilot-variety-met";
    thresholds: Readonly<{
      minimumContentTypes: 2;
      minimumDurationBands: 2;
      minimumLightingTypes: 2;
      minimumMotionTypes: 2;
      minimumEnvironmentTypes: 2;
    }>;
    contentTypes: readonly string[];
    durationBands: readonly ("short" | "medium" | "long")[];
    lightingTypes: readonly StudioHighlightCoverageProfile["lighting"][];
    motionTypes: readonly StudioHighlightCoverageProfile["motion"][];
    environmentTypes: readonly StudioHighlightCoverageProfile["environment"][];
    profiledVideos: number;
    profileCoverageComplete: boolean;
    missingContentTypes: readonly string[];
    missingDurationBands: readonly ("short" | "medium" | "long")[];
    modelInferencesCounted: 0;
    boundary: "verified-upload-metadata-only";
  }>;
  actionPlan: Readonly<{
    existingEligibleVideos: number;
    existingVideosAwaitingLabel: number;
    existingVideosAwaitingProfile: number;
    minimumCountShortfall: number;
    additionalEligibleVideosNeeded: 0;
    captureTargets: readonly never[];
    customerCaptureRequired: false;
    futureEvidenceMode: "natural-upload-accumulation";
    nextAction: Readonly<{
      kind: "profile-existing" | "label-existing" | "retest-batch";
      sourceAssetId: string | null;
      captureSequence: number | null;
    }> | null;
    evidenceBasis: "confirmed-gaps-only";
    modelInferencesCounted: 0;
  }>;
  deliveryEvidence: Readonly<{
    status: "insufficient-evidence" | "ready-for-human-acceptance";
    threshold: Readonly<{ minimumLabeledVideos: 20 }>;
    labeledVideos: number;
    remainingVideos: number;
    readyForHumanAcceptance: boolean;
    productionAccepted: false;
    boundary: "evidence-readiness-is-not-production-acceptance";
  }>;
  automaticEditingEligible: boolean;
  productionAccepted: false;
  automaticPublishing: false;
  boundary: "local-pilot-evidence-only";
}>;

type StoredLabel = StudioHighlightLabel & Readonly<{ operationKey: string; bodyDigest: string }>;
type StoredCoverageProfile = StudioHighlightCoverageProfile & Readonly<{ operationKey: string; bodyDigest: string }>;
type StoredCandidateRejection = StudioHighlightCandidateRejection & Readonly<{ operationKey: string; bodyDigest: string }>;
type StoredState = Readonly<{
  labels: readonly StoredLabel[];
  coverageProfiles: readonly StoredCoverageProfile[];
  candidateRejections: readonly StoredCandidateRejection[];
}>;

export class StudioHighlightEvaluationStore {
  private tail: Promise<void> = Promise.resolve();

  constructor(
    private readonly path: string,
    private readonly scope: Readonly<{ actorId: string; customerId: string; sessionId: string }>,
    private readonly now: () => string = () => new Date().toISOString(),
  ) {}

  async record(input: Readonly<{
    card: MaterialCard;
    sourceStartMs: number;
    sourceEndMs: number;
    reason: string;
    operationKey: string;
    confirmationMode?: "manual-range" | "accepted-model-candidate";
    modelReceiptJobId?: string | null;
  }>): Promise<StudioHighlightLabel> {
    const sourceStartMs = requireMs(input.sourceStartMs);
    const sourceEndMs = requireMs(input.sourceEndMs);
    if (sourceEndMs <= sourceStartMs || sourceEndMs - sourceStartMs < 500) {
      throw new Error("studio-highlight-range-invalid");
    }
    if (sourceStartMs < input.card.sourceStartMs || sourceEndMs > input.card.sourceEndMs) {
      throw new Error("studio-highlight-range-outside-source");
    }
    const reason = requireText(input.reason, "studio-highlight-reason-required", 500);
    const operationKey = requireText(input.operationKey, "studio-highlight-operation-key-required", 200);
    const confirmationMode = input.confirmationMode ?? "manual-range";
    const modelReceiptJobId = input.modelReceiptJobId ?? null;
    if (
      (confirmationMode === "manual-range" && modelReceiptJobId !== null)
      || (confirmationMode === "accepted-model-candidate" && modelReceiptJobId === null)
    ) throw new Error("studio-highlight-confirmation-binding-invalid");
    if (modelReceiptJobId !== null) {
      requireText(modelReceiptJobId, "studio-highlight-candidate-receipt-required", 1_000);
    }
    const normalized = {
      sourceAssetId: input.card.sourceAssetId,
      sourceDigest: input.card.sourceDigest,
      sourceStartMs,
      sourceEndMs,
      reason,
    };
    const bodyDigest = digest(JSON.stringify(confirmationMode === "manual-range"
      ? normalized
      : { ...normalized, confirmationMode, modelReceiptJobId }));
    return this.withState(async (state) => {
      const existing = state.labels.find((item) => item.operationKey === operationKey);
      if (existing && existing.bodyDigest !== bodyDigest) throw new Error("studio-highlight-operation-key-conflict");
      if (existing) return publicLabel(existing);
      if (confirmationMode === "accepted-model-candidate" && state.candidateRejections.some((item) =>
        item.sourceAssetId === normalized.sourceAssetId
        && item.sourceDigest === normalized.sourceDigest
        && item.receiptJobId === modelReceiptJobId
        && item.sourceStartMs === sourceStartMs
        && item.sourceEndMs === sourceEndMs)) {
        throw new Error("studio-highlight-candidate-decision-conflict");
      }
      const label: StoredLabel = Object.freeze({
        id: `highlight_${digest(`${this.scope.customerId}\0${this.scope.sessionId}\0${operationKey}`)}`,
        ...normalized,
        confirmationMode,
        modelReceiptJobId,
        provenance: "human-confirmed",
        createdAt: this.now(),
        operationKey,
        bodyDigest,
      });
      await this.persist({ ...state, labels: [...state.labels, label] });
      return publicLabel(label);
    });
  }

  async evaluate(
    cards: readonly MaterialCard[],
    evidence: readonly StudioEditorialVisualEvidence[],
  ): Promise<readonly StudioHighlightEvaluation[]> {
    const state = await this.load();
    return cards.map((card) => evaluateCard(card, state.labels, evidence));
  }

  async rejectCandidate(input: Readonly<{
    card: MaterialCard;
    sourceStartMs: number;
    sourceEndMs: number;
    receiptJobId: string;
    operationKey: string;
  }>): Promise<StudioHighlightCandidateRejection> {
    const sourceStartMs = requireMs(input.sourceStartMs);
    const sourceEndMs = requireMs(input.sourceEndMs);
    if (sourceEndMs <= sourceStartMs || sourceEndMs - sourceStartMs < 500) {
      throw new Error("studio-highlight-range-invalid");
    }
    if (sourceStartMs < input.card.sourceStartMs || sourceEndMs > input.card.sourceEndMs) {
      throw new Error("studio-highlight-range-outside-source");
    }
    const receiptJobId = requireText(input.receiptJobId, "studio-highlight-candidate-receipt-required", 1_000);
    const operationKey = requireText(input.operationKey, "studio-highlight-operation-key-required", 200);
    const normalized = {
      sourceAssetId: input.card.sourceAssetId,
      sourceDigest: input.card.sourceDigest,
      sourceStartMs,
      sourceEndMs,
      receiptJobId,
      reason: "not-a-highlight" as const,
    };
    const bodyDigest = digest(JSON.stringify(normalized));
    return this.withState(async (state) => {
      const existing = state.candidateRejections.find((item) => item.operationKey === operationKey);
      if (existing && existing.bodyDigest !== bodyDigest) throw new Error("studio-highlight-operation-key-conflict");
      if (existing) return publicCandidateRejection(existing);
      if (state.labels.some((item) =>
        item.confirmationMode === "accepted-model-candidate"
        && item.modelReceiptJobId === receiptJobId
        && item.sourceAssetId === normalized.sourceAssetId
        && item.sourceDigest === normalized.sourceDigest
        && item.sourceStartMs === sourceStartMs
        && item.sourceEndMs === sourceEndMs)) {
        throw new Error("studio-highlight-candidate-decision-conflict");
      }
      const rejection: StoredCandidateRejection = Object.freeze({
        id: `highlight_rejection_${digest(`${this.scope.customerId}\0${this.scope.sessionId}\0${operationKey}`)}`,
        ...normalized,
        provenance: "human-confirmed",
        createdAt: this.now(),
        operationKey,
        bodyDigest,
      });
      await this.persist({
        ...state,
        candidateRejections: [...state.candidateRejections, rejection].slice(-256),
      });
      return publicCandidateRejection(rejection);
    });
  }

  async candidateRejections(): Promise<readonly StudioHighlightCandidateRejection[]> {
    const state = await this.load();
    return [...new Map(state.candidateRejections.map((rejection) => [
      `${rejection.sourceAssetId}\0${rejection.sourceDigest}\0${rejection.receiptJobId}\0${rejection.sourceStartMs}\0${rejection.sourceEndMs}`,
      publicCandidateRejection(rejection),
    ])).values()];
  }

  async recordCoverage(input: Readonly<{
    card: MaterialCard;
    lighting: unknown;
    motion: unknown;
    environment: unknown;
    operationKey: unknown;
  }>): Promise<StudioHighlightCoverageProfile> {
    const lighting = requireChoice(input.lighting, ["stable", "mixed", "low"], "studio-highlight-lighting-invalid");
    const motion = requireChoice(input.motion, [
      "static-display", "handheld-product", "making-action", "person-speaking", "device-motion",
    ], "studio-highlight-motion-invalid");
    const environment = requireChoice(input.environment, [
      "indoor-controlled", "indoor-mixed", "outdoor",
    ], "studio-highlight-environment-invalid");
    const operationKey = requireText(input.operationKey, "studio-highlight-operation-key-required", 200);
    const normalized = {
      sourceAssetId: input.card.sourceAssetId,
      sourceDigest: input.card.sourceDigest,
      lighting,
      motion,
      environment,
    };
    const bodyDigest = digest(JSON.stringify(normalized));
    return this.withState(async (state) => {
      const existing = state.coverageProfiles.find((item) => item.operationKey === operationKey);
      if (existing && existing.bodyDigest !== bodyDigest) throw new Error("studio-highlight-operation-key-conflict");
      if (existing) return publicCoverageProfile(existing);
      const profile: StoredCoverageProfile = Object.freeze({
        ...normalized,
        provenance: "human-confirmed",
        updatedAt: this.now(),
        operationKey,
        bodyDigest,
      });
      await this.persist({
        ...state,
        coverageProfiles: [...state.coverageProfiles, profile].slice(-256),
      });
      return publicCoverageProfile(profile);
    });
  }

  async coverageProfiles(): Promise<readonly StudioHighlightCoverageProfile[]> {
    const state = await this.load();
    return [...new Map(state.coverageProfiles.map((profile) => [
      `${profile.sourceAssetId}\0${profile.sourceDigest}`,
      publicCoverageProfile(profile),
    ])).values()];
  }

  private async load(): Promise<StoredState> {
    const raw = await readFile(this.path, "utf8").catch((error: unknown) => {
      if (hasCode(error, "ENOENT")) return null;
      throw error;
    });
    if (raw === null) return { labels: [], coverageProfiles: [], candidateRejections: [] };
    try {
      const value = JSON.parse(raw) as Record<string, unknown>;
      if (
        ![1, 2, 3, 4].includes(Number(value.schemaVersion))
        || value.actorId !== this.scope.actorId
        || value.customerId !== this.scope.customerId
        || value.sessionId !== this.scope.sessionId
        || !Array.isArray(value.labels)
      ) throw new Error();
      const labels = (value.labels as StoredLabel[]).map((label) => ({
        ...label,
        confirmationMode: label.confirmationMode ?? "manual-range",
        modelReceiptJobId: label.modelReceiptJobId ?? null,
      }));
      const coverageProfiles = Number(value.schemaVersion) >= 2
        ? value.coverageProfiles as StoredCoverageProfile[]
        : [];
      const candidateRejections = Number(value.schemaVersion) >= 4
        ? value.candidateRejections as StoredCandidateRejection[]
        : [];
      if (new Set(labels.map((item) => item.operationKey)).size !== labels.length) throw new Error();
      labels.forEach(assertStoredLabel);
      if (!Array.isArray(coverageProfiles)) throw new Error();
      coverageProfiles.forEach(assertStoredCoverageProfile);
      if (!Array.isArray(candidateRejections)) throw new Error();
      if (new Set(candidateRejections.map((item) => item.operationKey)).size !== candidateRejections.length) throw new Error();
      candidateRejections.forEach(assertStoredCandidateRejection);
      return structuredClone({ labels, coverageProfiles, candidateRejections });
    } catch {
      throw new Error("studio-highlight-state-invalid");
    }
  }

  private async persist(state: StoredState): Promise<void> {
    await mkdir(dirname(this.path), { recursive: true });
    const pending = `${this.path}.next-${randomUUID()}`;
    try {
      await writeFile(pending, `${JSON.stringify({
        schemaVersion: 4,
        ...this.scope,
        labels: state.labels,
        coverageProfiles: state.coverageProfiles,
        candidateRejections: state.candidateRejections,
      }, null, 2)}\n`, { encoding: "utf8", flag: "wx" });
      await rename(pending, this.path);
    } finally {
      await unlink(pending).catch(() => undefined);
    }
  }

  private async withState<T>(work: (state: StoredState) => Promise<T>): Promise<T> {
    const current = this.tail.catch(() => undefined).then(async () => work(await this.load()));
    this.tail = current.then(() => undefined, () => undefined);
    return current;
  }
}

export function evaluateCard(
  card: MaterialCard,
  labels: readonly StudioHighlightLabel[],
  evidence: readonly StudioEditorialVisualEvidence[],
): StudioHighlightEvaluation {
  const cardLabels = labels.filter((item) =>
    item.sourceAssetId === card.sourceAssetId && item.sourceDigest === card.sourceDigest);
  const candidates = discriminativeCandidates(card, evidence);
  const scores = cardLabels.map((label) => {
    const score = intervalSetIoU(label, candidates);
    return {
      labelId: label.id,
      bestIoU: score,
      bestCandidateStartMs: candidates.length > 0 ? Math.min(...candidates.map((item) => item.startMs)) : null,
      bestCandidateEndMs: candidates.length > 0 ? Math.max(...candidates.map((item) => item.endMs)) : null,
    };
  });
  const status = cardLabels.length === 0
    ? "awaiting-human-label" as const
    : candidates.length === 0
      ? "no-discriminative-model-candidate" as const
      : scores.every((score) => score.bestIoU >= 0.5)
        ? "passed" as const
        : "failed" as const;
  return Object.freeze({
    sourceAssetId: card.sourceAssetId,
    sourceDigest: card.sourceDigest,
    sourceStartMs: card.sourceStartMs,
    sourceEndMs: card.sourceEndMs,
    status,
    threshold: Object.freeze({ minimumIoU: 0.5 as const }),
    labels: cardLabels.map(publicLabel),
    modelCandidates: candidates,
    scores,
    publishable: false as const,
    trainingEligible: false as const,
    boundary: "human-label-required-model-evaluation-only" as const,
  });
}

export function summarizeHighlightBatch(
  evaluations: readonly StudioHighlightEvaluation[],
  attempts: readonly StudioHighlightAttempt[],
  materials: readonly MaterialCard[],
  coverageProfiles: readonly StudioHighlightCoverageProfile[],
): StudioHighlightBatchGate {
  const uniqueEvaluations = [...new Map(evaluations.map((evaluation) => [
    `${evaluation.sourceAssetId}\0${evaluation.sourceDigest}`,
    evaluation,
  ])).values()];
  const labeled = uniqueEvaluations.filter((evaluation) => evaluation.labels.length > 0);
  const passedVideos = labeled.filter((evaluation) => evaluation.status === "passed").length;
  const failedVideos = labeled.length - passedVideos;
  const passRate = labeled.length === 0 ? null : Number((passedVideos / labeled.length).toFixed(4));
  const labeledKeys = new Set(labeled.map((evaluation) =>
    `${evaluation.sourceAssetId}\0${evaluation.sourceDigest}`));
  const uniqueAttempts = [...new Map(attempts
    .filter((attempt) => labeledKeys.has(`${attempt.sourceAssetId}\0${attempt.sourceDigest}`))
    .map((attempt) => [
    `${attempt.sourceAssetId}\0${attempt.sourceDigest}`,
    attempt,
  ])).values()];
  const attemptByKey = new Map(uniqueAttempts.map((attempt) => [
    `${attempt.sourceAssetId}\0${attempt.sourceDigest}`,
    attempt,
  ]));
  const runtimes = uniqueAttempts
    .filter((attempt) => attempt.status === "succeeded" && attempt.runtimeMs !== null)
    .map((attempt) => attempt.runtimeMs as number)
    .sort((left, right) => left - right);
  const p95RuntimeMs = runtimes.length === 0 ? null : runtimes[Math.ceil(runtimes.length * 0.95) - 1]!;
  const stageCounts = new Map<string, number>();
  for (const attempt of uniqueAttempts) {
    if (attempt.status === "succeeded") continue;
    const stage = attempt.lastStage ?? "unknown";
    stageCounts.set(stage, (stageCounts.get(stage) ?? 0) + 1);
  }
  const failureStages = [...stageCounts.entries()]
    .map(([stage, count]) => Object.freeze({ stage, count }))
    .sort((left, right) => right.count - left.count || left.stage.localeCompare(right.stage));
  const enoughSamples = labeled.length >= 10;
  const evidenceCoverageComplete = uniqueAttempts.length === labeled.length
    && labeled
      .filter((evaluation) => evaluation.status === "passed")
      .every((evaluation) => {
        const attempt = attemptByKey.get(`${evaluation.sourceAssetId}\0${evaluation.sourceDigest}`);
        return attempt?.status === "succeeded" && attempt.runtimeMs !== null;
      });
  const qualityMet = passRate !== null && passRate >= 0.8;
  const latencyMet = p95RuntimeMs !== null && p95RuntimeMs <= 180_000;
  const materialByKey = new Map(materials.map((material) => [
    `${material.sourceAssetId}\0${material.sourceDigest}`,
    material,
  ]));
  const labeledMaterials = labeled.flatMap((evaluation) => {
    const material = materialByKey.get(`${evaluation.sourceAssetId}\0${evaluation.sourceDigest}`);
    return material ? [material] : [];
  });
  const contentTypes = [...new Set(labeledMaterials.map((material) => material.contentType))].sort();
  const durationBands = [...new Set(labeledMaterials.map((material) => durationBand(
    material.sourceEndMs - material.sourceStartMs,
  )))].sort(durationBandOrder);
  const knownContentTypes = ["single-product", "jewelry-making-process", "designer-viewpoint"] as const;
  const knownDurationBands = ["short", "medium", "long"] as const;
  const labeledProfiles = coverageProfiles.filter((profile) =>
    labeledKeys.has(`${profile.sourceAssetId}\0${profile.sourceDigest}`));
  const lightingTypes = [...new Set(labeledProfiles.map((profile) => profile.lighting))].sort();
  const motionTypes = [...new Set(labeledProfiles.map((profile) => profile.motion))].sort();
  const environmentTypes = [...new Set(labeledProfiles.map((profile) => profile.environment))].sort();
  const profileCoverageComplete = labeledProfiles.length === labeled.length;
  const varietyMet = contentTypes.length >= 2 && durationBands.length >= 2
    && profileCoverageComplete && lightingTypes.length >= 2
    && motionTypes.length >= 2 && environmentTypes.length >= 2;
  const sampleCoverage = Object.freeze({
    status: varietyMet ? "pilot-variety-met" as const : "insufficient-variety" as const,
    thresholds: Object.freeze({
      minimumContentTypes: 2 as const,
      minimumDurationBands: 2 as const,
      minimumLightingTypes: 2 as const,
      minimumMotionTypes: 2 as const,
      minimumEnvironmentTypes: 2 as const,
    }),
    contentTypes,
    durationBands,
    lightingTypes,
    motionTypes,
    environmentTypes,
    profiledVideos: labeledProfiles.length,
    profileCoverageComplete,
    missingContentTypes: knownContentTypes.filter((item) => !contentTypes.includes(item)),
    missingDurationBands: knownDurationBands.filter((item) => !durationBands.includes(item)),
    modelInferencesCounted: 0 as const,
    boundary: "verified-upload-metadata-only" as const,
  });
  const allEvaluationKeys = new Set(uniqueEvaluations.map((evaluation) =>
    `${evaluation.sourceAssetId}\0${evaluation.sourceDigest}`));
  const profiledEligibleVideos = coverageProfiles.filter((profile) =>
    allEvaluationKeys.has(`${profile.sourceAssetId}\0${profile.sourceDigest}`)).length;
  const profiledKeys = new Set(coverageProfiles.map((profile) =>
    `${profile.sourceAssetId}\0${profile.sourceDigest}`));
  const minimumCountShortfall = Math.max(0, 10 - uniqueEvaluations.length);
  const captureTargets = Object.freeze([]);
  const labeledWithoutProfile = labeled.find((evaluation) =>
    !profiledKeys.has(`${evaluation.sourceAssetId}\0${evaluation.sourceDigest}`));
  const unlabeledExisting = uniqueEvaluations.find((evaluation) => evaluation.labels.length === 0);
  const nextAction = labeledWithoutProfile
    ? Object.freeze({ kind: "profile-existing" as const, sourceAssetId: labeledWithoutProfile.sourceAssetId, captureSequence: null })
    : unlabeledExisting
      ? Object.freeze({ kind: "label-existing" as const, sourceAssetId: unlabeledExisting.sourceAssetId, captureSequence: null })
      : labeled.length > 0 && !evidenceCoverageComplete
        ? Object.freeze({ kind: "retest-batch" as const, sourceAssetId: null, captureSequence: null })
        : null;
  const actionPlan = Object.freeze({
    existingEligibleVideos: uniqueEvaluations.length,
    existingVideosAwaitingLabel: uniqueEvaluations.length - labeled.length,
    existingVideosAwaitingProfile: Math.max(0, uniqueEvaluations.length - profiledEligibleVideos),
    minimumCountShortfall,
    additionalEligibleVideosNeeded: 0 as const,
    captureTargets,
    customerCaptureRequired: false as const,
    futureEvidenceMode: "natural-upload-accumulation" as const,
    nextAction,
    evidenceBasis: "confirmed-gaps-only" as const,
    modelInferencesCounted: 0 as const,
  });
  const status = !enoughSamples
    ? "insufficient-samples" as const
    : evidenceCoverageComplete && qualityMet && latencyMet && varietyMet
      ? "pilot-threshold-met" as const
      : "below-pilot-threshold" as const;
  const readyForHumanAcceptance = labeled.length >= 20
    && evidenceCoverageComplete && qualityMet && latencyMet && varietyMet;
  const deliveryEvidence = Object.freeze({
    status: readyForHumanAcceptance
      ? "ready-for-human-acceptance" as const
      : "insufficient-evidence" as const,
    threshold: Object.freeze({ minimumLabeledVideos: 20 as const }),
    labeledVideos: labeled.length,
    remainingVideos: Math.max(0, 20 - labeled.length),
    readyForHumanAcceptance,
    productionAccepted: false as const,
    boundary: "evidence-readiness-is-not-production-acceptance" as const,
  });
  return Object.freeze({
    status,
    thresholds: Object.freeze({
      minimumLabeledVideos: 10 as const,
      minimumPassRate: 0.8 as const,
      maximumP95RuntimeMs: 180_000 as const,
    }),
    labeledVideos: labeled.length,
    remainingVideos: Math.max(0, 10 - labeled.length),
    passedVideos,
    failedVideos,
    passRate,
    attemptedVideos: uniqueAttempts.length,
    evidenceCoverageComplete,
    successfulRuntimeSamples: runtimes.length,
    p95RuntimeMs,
    failureStages,
    sampleCoverage,
    actionPlan,
    deliveryEvidence,
    automaticEditingEligible: status === "pilot-threshold-met",
    productionAccepted: false as const,
    automaticPublishing: false as const,
    boundary: "local-pilot-evidence-only" as const,
  });
}

export function isEligibleHighlightMaterial(material: MaterialCard): boolean {
  const observations = material.observations || [];
  const evidence = [
    ...observations,
    ...(material.technicalFlags || []),
    ...(material.pendingFacts || []),
  ].join(" ").toLowerCase();
  if (
    evidence.includes("纯色合成商品夹具")
    || evidence.includes("synthetic-color-tone-fixture")
    || evidence.includes("接入方式：模拟硬件")
    || evidence.includes("设备真实性：本地模拟")
  ) return false;
  return observations.some((value) =>
    value.startsWith("客户上传素材：") || value.startsWith("硬件接入素材："));
}

function durationBand(durationMs: number): "short" | "medium" | "long" {
  if (durationMs <= 30_000) return "short";
  if (durationMs <= 90_000) return "medium";
  return "long";
}

function durationBandOrder(left: "short" | "medium" | "long", right: "short" | "medium" | "long"): number {
  return ["short", "medium", "long"].indexOf(left) - ["short", "medium", "long"].indexOf(right);
}

function discriminativeCandidates(
  card: MaterialCard,
  evidence: readonly StudioEditorialVisualEvidence[],
): StudioHighlightEvaluation["modelCandidates"] {
  const candidates = evidence.flatMap((item) => {
    if (
      item.provider.layer !== "vision"
      || item.sourceAssetId !== card.sourceAssetId
      || item.sourceDigest !== card.sourceDigest
      || item.sourceStartMs > card.sourceStartMs
      || item.sourceEndMs < card.sourceEndMs
    ) return [];
    return (item.highlightCandidates ?? [])
      .filter((candidate) =>
        candidate.confidence >= 0.6
        && candidate.startMs >= card.sourceStartMs
        && candidate.endMs <= card.sourceEndMs
        && candidate.endMs > candidate.startMs)
      .map((candidate) => Object.freeze({
        startMs: candidate.startMs,
        endMs: candidate.endMs,
        text: candidate.reason,
        receiptJobId: item.receiptJobId,
      }));
  });
  return [...new Map(candidates.map((candidate) => [
    `${candidate.receiptJobId}\0${candidate.startMs}\0${candidate.endMs}\0${candidate.text}`,
    candidate,
  ])).values()];
}

function intervalSetIoU(
  left: Readonly<{ sourceStartMs: number; sourceEndMs: number }>,
  candidates: readonly Readonly<{ startMs: number; endMs: number }>[],
): number {
  const merged = mergeIntervals(candidates);
  const selectedDuration = merged.reduce((total, item) => total + item.endMs - item.startMs, 0);
  const intersection = merged.reduce((total, item) => total + Math.max(
    0,
    Math.min(left.sourceEndMs, item.endMs) - Math.max(left.sourceStartMs, item.startMs),
  ), 0);
  const humanDuration = left.sourceEndMs - left.sourceStartMs;
  const union = humanDuration + selectedDuration - intersection;
  return union <= 0 ? 0 : Number((intersection / union).toFixed(4));
}

function mergeIntervals(
  candidates: readonly Readonly<{ startMs: number; endMs: number }>[],
): ReadonlyArray<Readonly<{ startMs: number; endMs: number }>> {
  const sorted = [...candidates].sort((left, right) => left.startMs - right.startMs || left.endMs - right.endMs);
  const merged: Array<{ startMs: number; endMs: number }> = [];
  for (const item of sorted) {
    const previous = merged.at(-1);
    if (previous && item.startMs <= previous.endMs) previous.endMs = Math.max(previous.endMs, item.endMs);
    else merged.push({ startMs: item.startMs, endMs: item.endMs });
  }
  return merged;
}

function publicLabel(label: StudioHighlightLabel): StudioHighlightLabel {
  return Object.freeze({
    id: label.id,
    sourceAssetId: label.sourceAssetId,
    sourceDigest: label.sourceDigest,
    sourceStartMs: label.sourceStartMs,
    sourceEndMs: label.sourceEndMs,
    reason: label.reason,
    confirmationMode: label.confirmationMode,
    modelReceiptJobId: label.modelReceiptJobId,
    provenance: "human-confirmed",
    createdAt: label.createdAt,
  });
}

function publicCoverageProfile(profile: StudioHighlightCoverageProfile): StudioHighlightCoverageProfile {
  return Object.freeze({
    sourceAssetId: profile.sourceAssetId,
    sourceDigest: profile.sourceDigest,
    lighting: profile.lighting,
    motion: profile.motion,
    environment: profile.environment,
    provenance: "human-confirmed",
    updatedAt: profile.updatedAt,
  });
}

function publicCandidateRejection(
  rejection: StudioHighlightCandidateRejection,
): StudioHighlightCandidateRejection {
  return Object.freeze({
    id: rejection.id,
    sourceAssetId: rejection.sourceAssetId,
    sourceDigest: rejection.sourceDigest,
    sourceStartMs: rejection.sourceStartMs,
    sourceEndMs: rejection.sourceEndMs,
    receiptJobId: rejection.receiptJobId,
    reason: "not-a-highlight",
    provenance: "human-confirmed",
    createdAt: rejection.createdAt,
  });
}

function assertStoredLabel(value: StoredLabel): void {
  if (
    typeof value.id !== "string" || typeof value.sourceAssetId !== "string"
    || !/^[a-f0-9]{64}$/.test(value.sourceDigest)
    || !Number.isSafeInteger(value.sourceStartMs) || !Number.isSafeInteger(value.sourceEndMs)
    || value.sourceEndMs <= value.sourceStartMs || value.provenance !== "human-confirmed"
    || !["manual-range", "accepted-model-candidate"].includes(value.confirmationMode)
    || (value.confirmationMode === "manual-range" && value.modelReceiptJobId !== null)
    || (value.confirmationMode === "accepted-model-candidate" && !value.modelReceiptJobId?.trim())
    || typeof value.reason !== "string" || typeof value.operationKey !== "string"
    || !/^[a-f0-9]{64}$/.test(value.bodyDigest) || Number.isNaN(Date.parse(value.createdAt))
  ) throw new Error("studio-highlight-state-invalid");
}

function assertStoredCoverageProfile(value: StoredCoverageProfile): void {
  if (
    typeof value.sourceAssetId !== "string" || !/^[a-f0-9]{64}$/.test(value.sourceDigest)
    || !["stable", "mixed", "low"].includes(value.lighting)
    || !["static-display", "handheld-product", "making-action", "person-speaking", "device-motion"].includes(value.motion)
    || !["indoor-controlled", "indoor-mixed", "outdoor"].includes(value.environment)
    || value.provenance !== "human-confirmed" || Number.isNaN(Date.parse(value.updatedAt))
    || typeof value.operationKey !== "string" || !/^[a-f0-9]{64}$/.test(value.bodyDigest)
  ) throw new Error("studio-highlight-state-invalid");
}

function assertStoredCandidateRejection(value: StoredCandidateRejection): void {
  if (
    typeof value.id !== "string" || typeof value.sourceAssetId !== "string"
    || !/^[a-f0-9]{64}$/.test(value.sourceDigest)
    || !Number.isSafeInteger(value.sourceStartMs) || !Number.isSafeInteger(value.sourceEndMs)
    || value.sourceEndMs <= value.sourceStartMs || value.reason !== "not-a-highlight"
    || !value.receiptJobId?.trim() || value.provenance !== "human-confirmed"
    || Number.isNaN(Date.parse(value.createdAt)) || typeof value.operationKey !== "string"
    || !/^[a-f0-9]{64}$/.test(value.bodyDigest)
  ) throw new Error("studio-highlight-state-invalid");
}

function requireChoice<const T extends string>(value: unknown, choices: readonly T[], code: string): T {
  if (typeof value !== "string" || !choices.includes(value as T)) throw new Error(code);
  return value as T;
}

function requireMs(value: unknown): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) throw new Error("studio-highlight-range-invalid");
  return Number(value);
}

function requireText(value: unknown, code: string, max: number): string {
  if (typeof value !== "string" || !value.trim() || value.trim().length > max) throw new Error(code);
  return value.trim();
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function hasCode(error: unknown, code: string): boolean {
  return typeof error === "object" && error !== null && "code" in error && error.code === code;
}
