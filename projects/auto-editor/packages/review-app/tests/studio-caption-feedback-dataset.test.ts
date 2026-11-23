import { describe, expect, it } from "vitest";

import type { DraftPackage, FeedbackEvent } from "@wearable/contracts";
import { buildStudioCaptionFeedbackDataset } from "../src/studio-caption-feedback-dataset";

describe("studio caption feedback dataset", () => {
  it("exports exact before/after cue bindings without claiming training approval", () => {
    const parent = draft("draft-parent", "原始带大家了解宝珊老虹");
    const revised = draft("draft-revised", "从原石带大家了解保山南红", {
      parentDraftId: parent.id,
      changedCueIds: ["cue-a"],
      styleChanged: false,
    });
    const feedback = review(revised.id, "adopt", null);
    const first = buildStudioCaptionFeedbackDataset({
      drafts: [parent, revised],
      feedbackByDraftId: { [revised.id]: [feedback] },
      generatedAt: "2026-10-24T00:00:00.000Z",
    });
    const regenerated = buildStudioCaptionFeedbackDataset({
      drafts: [parent, revised],
      feedbackByDraftId: { [revised.id]: [feedback] },
      generatedAt: "2026-10-25T00:00:00.000Z",
    });

    expect(first.datasetId).toMatch(/^caption-feedback-v1-[a-f0-9]{12}$/);
    expect(first.datasetDigest).toMatch(/^[a-f0-9]{64}$/);
    expect(regenerated.datasetId).toBe(first.datasetId);
    expect(regenerated.datasetDigest).toBe(first.datasetDigest);
    expect(first.counts).toEqual({
      total: 1,
      adopted: 1,
      rejected: 0,
      changesRequested: 0,
      unreviewed: 0,
      excludedInvalidatedDrafts: 0,
      excludedMissingParents: 0,
      excludedBindingMismatches: 0,
    });
    expect(first.entries[0]).toMatchObject({
      parentDraftId: "draft-parent",
      revisedDraftId: "draft-revised",
      cueId: "cue-a",
      sourceAssetId: "asset-a",
      sourceDigest: "a".repeat(64),
      beforeText: "原始带大家了解宝珊老虹",
      afterText: "从原石带大家了解保山南红",
      workflowDecision: "adopted",
      workflowDecisionReason: null,
      provenance: "operator-submitted-caption-revision",
      use: "offline-evaluation-only",
      trainingEligible: false,
    });
    expect(first.readiness).toEqual({
      status: "offline-evaluation-candidate",
      trainingEligible: false,
      automaticTraining: false,
      releaseEligible: false,
      nextGate: "human-dataset-review-required",
    });
    expect(first.boundary).toBe("versioned-caption-candidate-dataset-not-training-approval");
  });

  it("fails closed on invalidated, missing-parent, and altered timing bindings", () => {
    const parent = draft("draft-parent", "原字幕");
    const invalidated = { ...draft("draft-invalid", "改后字幕", revision(parent.id)), status: "invalidated" } as DraftPackage;
    const missing = draft("draft-missing", "改后字幕", revision("missing-parent"));
    const bindingMismatch = draft("draft-mismatch", "改后字幕", revision(parent.id), 9_999);
    const dataset = buildStudioCaptionFeedbackDataset({
      drafts: [parent, invalidated, missing, bindingMismatch],
      feedbackByDraftId: {},
      generatedAt: "2026-10-24T00:00:00.000Z",
    });

    expect(dataset.counts).toMatchObject({
      total: 0,
      excludedInvalidatedDrafts: 1,
      excludedMissingParents: 1,
      excludedBindingMismatches: 1,
    });
    expect(dataset.readiness).toMatchObject({
      status: "empty",
      trainingEligible: false,
      automaticTraining: false,
      releaseEligible: false,
    });
  });
});

function revision(parentDraftId: string) {
  return { parentDraftId, changedCueIds: ["cue-a"], styleChanged: false };
}

function draft(
  id: string,
  text: string,
  captionRevision?: DraftPackage["captionRevision"],
  sourceStartMs = 1_000,
): DraftPackage {
  return {
    id,
    status: "reviewable",
    platformVariant: "douyin",
    outputDigest: id === "draft-parent" ? "b".repeat(64) : "c".repeat(64),
    qualityReport: { status: "passed" },
    createdAt: "2026-10-24T00:00:00.000Z",
    captionRevision,
    captionCues: [{
      id: "cue-a",
      sourceAssetId: "asset-a",
      sourceDigest: "a".repeat(64),
      sourceStartMs,
      sourceEndMs: 2_000,
      timelineStartMs: 0,
      timelineEndMs: 1_000,
      text,
      reviewStatus: "model-suggestion",
    }],
  } as unknown as DraftPackage;
}

function review(
  draftId: string,
  action: FeedbackEvent["action"],
  reason: string | null,
): FeedbackEvent {
  return {
    id: "feedback-a",
    customerId: "customer-a",
    sessionId: "session-a",
    version: 1,
    draftId,
    action,
    reason,
    actorId: "actor-a",
    publishClaimed: false,
    createdAt: "2026-10-24T00:01:00.000Z",
  };
}
