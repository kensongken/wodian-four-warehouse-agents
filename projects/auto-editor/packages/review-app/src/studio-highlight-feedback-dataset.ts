import { createHash } from "node:crypto";

import type {
  StudioHighlightCandidateRejection,
  StudioHighlightEvaluation,
} from "./studio-highlight-evaluation";

export type StudioHighlightFeedbackEntry = Readonly<{
  id: string;
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  decision: "human-selected-highlight" | "accepted-model-candidate" | "rejected-model-candidate";
  target: "highlight" | "not-highlight";
  reason: string;
  modelReceiptJobId: string | null;
  modelBindingStatus: "not-applicable" | "exact-current-candidate" | "receipt-recorded-candidate-not-current";
  provenance: "human-confirmed";
  createdAt: string;
  use: "offline-evaluation-only";
  trainingEligible: false;
}>;

export type StudioHighlightFeedbackDataset = Readonly<{
  schemaVersion: 1;
  datasetId: string;
  datasetDigest: string;
  generatedAt: string;
  counts: Readonly<{
    total: number;
    humanSelectedHighlights: number;
    acceptedModelCandidates: number;
    rejectedModelCandidates: number;
    exactModelBindings: number;
    excludedUnverifiedSourceRecords: number;
  }>;
  entries: readonly StudioHighlightFeedbackEntry[];
  readiness: Readonly<{
    status: "empty" | "offline-evaluation-candidate";
    trainingEligible: false;
    automaticTraining: false;
    releaseEligible: false;
    nextGate: "human-dataset-review-required";
  }>;
  boundary: "versioned-candidate-dataset-not-training-approval";
}>;

export function buildStudioHighlightFeedbackDataset(input: Readonly<{
  evaluations: readonly StudioHighlightEvaluation[];
  rejections: readonly StudioHighlightCandidateRejection[];
  excludedUnverifiedSourceRecords: number;
  generatedAt: string;
}>): StudioHighlightFeedbackDataset {
  const entries = deduplicateLatest([
    ...input.evaluations.flatMap((evaluation) => evaluation.labels.map((label) => {
      const acceptedCandidate = label.confirmationMode === "accepted-model-candidate";
      const exactCandidate = acceptedCandidate && evaluation.modelCandidates.some((candidate) =>
        candidate.receiptJobId === label.modelReceiptJobId
        && candidate.startMs === label.sourceStartMs
        && candidate.endMs === label.sourceEndMs);
      return Object.freeze({
        id: label.id,
        sourceAssetId: label.sourceAssetId,
        sourceDigest: label.sourceDigest,
        sourceStartMs: label.sourceStartMs,
        sourceEndMs: label.sourceEndMs,
        decision: acceptedCandidate ? "accepted-model-candidate" as const : "human-selected-highlight" as const,
        target: "highlight" as const,
        reason: label.reason,
        modelReceiptJobId: label.modelReceiptJobId,
        modelBindingStatus: acceptedCandidate
          ? exactCandidate ? "exact-current-candidate" as const : "receipt-recorded-candidate-not-current" as const
          : "not-applicable" as const,
        provenance: "human-confirmed" as const,
        createdAt: label.createdAt,
        use: "offline-evaluation-only" as const,
        trainingEligible: false as const,
      });
    })),
    ...input.rejections.map((rejection) => {
      const evaluation = input.evaluations.find((item) =>
        item.sourceAssetId === rejection.sourceAssetId && item.sourceDigest === rejection.sourceDigest);
      const exactCandidate = evaluation?.modelCandidates.some((candidate) =>
        candidate.receiptJobId === rejection.receiptJobId
        && candidate.startMs === rejection.sourceStartMs
        && candidate.endMs === rejection.sourceEndMs) ?? false;
      return Object.freeze({
        id: rejection.id,
        sourceAssetId: rejection.sourceAssetId,
        sourceDigest: rejection.sourceDigest,
        sourceStartMs: rejection.sourceStartMs,
        sourceEndMs: rejection.sourceEndMs,
        decision: "rejected-model-candidate" as const,
        target: "not-highlight" as const,
        reason: rejection.reason,
        modelReceiptJobId: rejection.receiptJobId,
        modelBindingStatus: exactCandidate
          ? "exact-current-candidate" as const
          : "receipt-recorded-candidate-not-current" as const,
        provenance: "human-confirmed" as const,
        createdAt: rejection.createdAt,
        use: "offline-evaluation-only" as const,
        trainingEligible: false as const,
      });
    }),
  ]).sort((left, right) =>
    left.sourceAssetId.localeCompare(right.sourceAssetId)
    || left.sourceStartMs - right.sourceStartMs
    || left.sourceEndMs - right.sourceEndMs
    || left.decision.localeCompare(right.decision));
  const digestInput = {
    schemaVersion: 1,
    entries,
    excludedUnverifiedSourceRecords: input.excludedUnverifiedSourceRecords,
    boundary: "versioned-candidate-dataset-not-training-approval",
  };
  const datasetDigest = createHash("sha256").update(JSON.stringify(digestInput)).digest("hex");
  const counts = Object.freeze({
    total: entries.length,
    humanSelectedHighlights: entries.filter((item) => item.decision === "human-selected-highlight").length,
    acceptedModelCandidates: entries.filter((item) => item.decision === "accepted-model-candidate").length,
    rejectedModelCandidates: entries.filter((item) => item.decision === "rejected-model-candidate").length,
    exactModelBindings: entries.filter((item) => item.modelBindingStatus === "exact-current-candidate").length,
    excludedUnverifiedSourceRecords: input.excludedUnverifiedSourceRecords,
  });
  return Object.freeze({
    schemaVersion: 1 as const,
    datasetId: `highlight-feedback-v1-${datasetDigest.slice(0, 12)}`,
    datasetDigest,
    generatedAt: input.generatedAt,
    counts,
    entries,
    readiness: Object.freeze({
      status: entries.length > 0 ? "offline-evaluation-candidate" as const : "empty" as const,
      trainingEligible: false as const,
      automaticTraining: false as const,
      releaseEligible: false as const,
      nextGate: "human-dataset-review-required" as const,
    }),
    boundary: "versioned-candidate-dataset-not-training-approval" as const,
  });
}

function deduplicateLatest(
  entries: readonly StudioHighlightFeedbackEntry[],
): StudioHighlightFeedbackEntry[] {
  const latest = new Map<string, StudioHighlightFeedbackEntry>();
  for (const entry of entries) {
    const key = [
      entry.sourceAssetId,
      entry.sourceDigest,
      entry.sourceStartMs,
      entry.sourceEndMs,
      entry.modelReceiptJobId ?? "manual",
      entry.decision,
    ].join("\0");
    const current = latest.get(key);
    if (!current || current.createdAt.localeCompare(entry.createdAt) <= 0) latest.set(key, entry);
  }
  return [...latest.values()];
}
