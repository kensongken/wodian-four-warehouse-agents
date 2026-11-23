import { describe, expect, it } from "vitest";

import { buildStudioHighlightFeedbackDataset } from "../src/studio-highlight-feedback-dataset";
import type {
  StudioHighlightCandidateRejection,
  StudioHighlightEvaluation,
} from "../src/studio-highlight-evaluation";

describe("studio highlight feedback dataset", () => {
  it("versions and deduplicates human decisions while keeping training closed", () => {
    const first = buildStudioHighlightFeedbackDataset({
      evaluations: [evaluation()],
      rejections: [rejection()],
      excludedUnverifiedSourceRecords: 2,
      generatedAt: "2026-10-15T00:00:00.000Z",
    });
    const regenerated = buildStudioHighlightFeedbackDataset({
      evaluations: [evaluation()],
      rejections: [rejection()],
      excludedUnverifiedSourceRecords: 2,
      generatedAt: "2026-10-16T00:00:00.000Z",
    });

    expect(first.datasetId).toMatch(/^highlight-feedback-v1-[a-f0-9]{12}$/);
    expect(first.datasetDigest).toMatch(/^[a-f0-9]{64}$/);
    expect(regenerated.datasetId).toBe(first.datasetId);
    expect(regenerated.datasetDigest).toBe(first.datasetDigest);
    expect(first.counts).toEqual({
      total: 3,
      humanSelectedHighlights: 1,
      acceptedModelCandidates: 1,
      rejectedModelCandidates: 1,
      exactModelBindings: 2,
      excludedUnverifiedSourceRecords: 2,
    });
    expect(first.readiness).toEqual({
      status: "offline-evaluation-candidate",
      trainingEligible: false,
      automaticTraining: false,
      releaseEligible: false,
      nextGate: "human-dataset-review-required",
    });
    expect(first.entries.every((entry) =>
      entry.provenance === "human-confirmed"
      && entry.use === "offline-evaluation-only"
      && entry.trainingEligible === false)).toBe(true);
    expect(first.boundary).toBe("versioned-candidate-dataset-not-training-approval");
  });

  it("marks a receipt as historical when its exact candidate is no longer current", () => {
    const stale = evaluation();
    const dataset = buildStudioHighlightFeedbackDataset({
      evaluations: [{ ...stale, modelCandidates: [] }],
      rejections: [rejection()],
      excludedUnverifiedSourceRecords: 0,
      generatedAt: "2026-10-15T00:00:00.000Z",
    });

    expect(dataset.counts.exactModelBindings).toBe(0);
    expect(dataset.entries.filter((entry) => entry.modelReceiptJobId !== null)
      .every((entry) => entry.modelBindingStatus === "receipt-recorded-candidate-not-current")).toBe(true);
    expect(dataset.readiness.trainingEligible).toBe(false);
  });
});

function evaluation(): StudioHighlightEvaluation {
  const digest = "a".repeat(64);
  return {
    sourceAssetId: "asset-a", sourceDigest: digest, sourceStartMs: 0, sourceEndMs: 20_000,
    status: "passed", threshold: { minimumIoU: 0.5 }, publishable: false, trainingEligible: false,
    boundary: "human-label-required-model-evaluation-only", scores: [],
    modelCandidates: [
      { startMs: 3_000, endMs: 6_000, text: "动作变化", receiptJobId: "receipt-a" },
      { startMs: 8_000, endMs: 10_000, text: "画面普通", receiptJobId: "receipt-a" },
    ],
    labels: [
      { id: "manual-old", sourceAssetId: "asset-a", sourceDigest: digest,
        sourceStartMs: 1_000, sourceEndMs: 2_000, reason: "旧理由",
        confirmationMode: "manual-range", modelReceiptJobId: null,
        provenance: "human-confirmed", createdAt: "2026-10-14T00:00:00.000Z" },
      { id: "manual-new", sourceAssetId: "asset-a", sourceDigest: digest,
        sourceStartMs: 1_000, sourceEndMs: 2_000, reason: "人工更新后的理由",
        confirmationMode: "manual-range", modelReceiptJobId: null,
        provenance: "human-confirmed", createdAt: "2026-10-15T00:00:00.000Z" },
      { id: "accepted-a", sourceAssetId: "asset-a", sourceDigest: digest,
        sourceStartMs: 3_000, sourceEndMs: 6_000, reason: "人工确认模型候选",
        confirmationMode: "accepted-model-candidate", modelReceiptJobId: "receipt-a",
        provenance: "human-confirmed", createdAt: "2026-10-15T00:00:00.000Z" },
    ],
  };
}

function rejection(): StudioHighlightCandidateRejection {
  return {
    id: "rejection-a", sourceAssetId: "asset-a", sourceDigest: "a".repeat(64),
    sourceStartMs: 8_000, sourceEndMs: 10_000, receiptJobId: "receipt-a",
    reason: "not-a-highlight", provenance: "human-confirmed", createdAt: "2026-10-15T00:00:00.000Z",
  };
}
