import { createHash } from "node:crypto";

import type { DraftPackage, FeedbackEvent } from "@wearable/contracts";

export type StudioCaptionFeedbackEntry = Readonly<{
  id: string;
  parentDraftId: string;
  revisedDraftId: string;
  platformVariant: DraftPackage["platformVariant"] | null;
  cueId: string;
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  timelineStartMs: number;
  timelineEndMs: number;
  beforeText: string;
  afterText: string;
  parentOutputDigest: string;
  revisedOutputDigest: string;
  revisedQualityStatus: "passed" | "needs-review" | "failed" | "not-recorded";
  workflowDecision: "adopted" | "rejected" | "changes-requested" | "unreviewed";
  workflowDecisionReason: string | null;
  provenance: "operator-submitted-caption-revision";
  createdAt: string;
  use: "offline-evaluation-only";
  trainingEligible: false;
}>;

export type StudioCaptionFeedbackDataset = Readonly<{
  schemaVersion: 1;
  datasetId: string;
  datasetDigest: string;
  generatedAt: string;
  counts: Readonly<{
    total: number;
    adopted: number;
    rejected: number;
    changesRequested: number;
    unreviewed: number;
    excludedInvalidatedDrafts: number;
    excludedMissingParents: number;
    excludedBindingMismatches: number;
  }>;
  entries: readonly StudioCaptionFeedbackEntry[];
  readiness: Readonly<{
    status: "empty" | "offline-evaluation-candidate";
    trainingEligible: false;
    automaticTraining: false;
    releaseEligible: false;
    nextGate: "human-dataset-review-required";
  }>;
  boundary: "versioned-caption-candidate-dataset-not-training-approval";
}>;

export function buildStudioCaptionFeedbackDataset(input: Readonly<{
  drafts: readonly DraftPackage[];
  feedbackByDraftId: Readonly<Record<string, readonly FeedbackEvent[]>>;
  generatedAt: string;
}>): StudioCaptionFeedbackDataset {
  const draftsById = new Map(input.drafts.map((draft) => [draft.id, draft]));
  const entries: StudioCaptionFeedbackEntry[] = [];
  let excludedInvalidatedDrafts = 0;
  let excludedMissingParents = 0;
  let excludedBindingMismatches = 0;

  for (const revised of input.drafts) {
    const binding = revised.captionRevision;
    if (!binding || binding.changedCueIds.length === 0) continue;
    if (revised.status === "invalidated") {
      excludedInvalidatedDrafts += binding.changedCueIds.length;
      continue;
    }
    const parent = draftsById.get(binding.parentDraftId);
    if (!parent || parent.status === "invalidated") {
      excludedMissingParents += binding.changedCueIds.length;
      continue;
    }
    const parentCues = new Map((parent.captionCues ?? []).map((cue) => [cue.id, cue]));
    const revisedCues = new Map((revised.captionCues ?? []).map((cue) => [cue.id, cue]));
    const review = latestFeedback(input.feedbackByDraftId[revised.id] ?? []);
    const workflowDecision = review === undefined
      ? "unreviewed" as const
      : review.action === "adopt"
        ? "adopted" as const
        : review.action === "reject"
          ? "rejected" as const
          : "changes-requested" as const;

    for (const cueId of binding.changedCueIds) {
      const before = parentCues.get(cueId);
      const after = revisedCues.get(cueId);
      if (!before || !after || before.text === after.text || !sameCueBinding(before, after)) {
        excludedBindingMismatches += 1;
        continue;
      }
      entries.push(Object.freeze({
        id: `caption-feedback-${digest({ revisedDraftId: revised.id, cueId, afterText: after.text }).slice(0, 16)}`,
        parentDraftId: parent.id,
        revisedDraftId: revised.id,
        platformVariant: revised.platformVariant ?? null,
        cueId,
        sourceAssetId: after.sourceAssetId,
        sourceDigest: after.sourceDigest,
        sourceStartMs: after.sourceStartMs,
        sourceEndMs: after.sourceEndMs,
        timelineStartMs: after.timelineStartMs,
        timelineEndMs: after.timelineEndMs,
        beforeText: before.text,
        afterText: after.text,
        parentOutputDigest: parent.outputDigest,
        revisedOutputDigest: revised.outputDigest,
        revisedQualityStatus: revised.qualityReport?.status ?? "not-recorded",
        workflowDecision,
        workflowDecisionReason: review?.reason ?? null,
        provenance: "operator-submitted-caption-revision",
        createdAt: revised.createdAt,
        use: "offline-evaluation-only",
        trainingEligible: false,
      }));
    }
  }

  entries.sort((left, right) =>
    left.sourceAssetId.localeCompare(right.sourceAssetId)
    || left.sourceStartMs - right.sourceStartMs
    || left.revisedDraftId.localeCompare(right.revisedDraftId)
    || left.cueId.localeCompare(right.cueId));
  const counts = Object.freeze({
    total: entries.length,
    adopted: entries.filter((entry) => entry.workflowDecision === "adopted").length,
    rejected: entries.filter((entry) => entry.workflowDecision === "rejected").length,
    changesRequested: entries.filter((entry) => entry.workflowDecision === "changes-requested").length,
    unreviewed: entries.filter((entry) => entry.workflowDecision === "unreviewed").length,
    excludedInvalidatedDrafts,
    excludedMissingParents,
    excludedBindingMismatches,
  });
  const digestInput = {
    schemaVersion: 1,
    counts,
    entries,
    boundary: "versioned-caption-candidate-dataset-not-training-approval",
  };
  const datasetDigest = digest(digestInput);
  return Object.freeze({
    schemaVersion: 1,
    datasetId: `caption-feedback-v1-${datasetDigest.slice(0, 12)}`,
    datasetDigest,
    generatedAt: input.generatedAt,
    counts,
    entries,
    readiness: Object.freeze({
      status: entries.length > 0 ? "offline-evaluation-candidate" : "empty",
      trainingEligible: false,
      automaticTraining: false,
      releaseEligible: false,
      nextGate: "human-dataset-review-required",
    }),
    boundary: "versioned-caption-candidate-dataset-not-training-approval",
  });
}

type CaptionCue = NonNullable<DraftPackage["captionCues"]>[number];

function sameCueBinding(left: CaptionCue, right: CaptionCue): boolean {
  return left.sourceAssetId === right.sourceAssetId
    && left.sourceDigest === right.sourceDigest
    && left.sourceStartMs === right.sourceStartMs
    && left.sourceEndMs === right.sourceEndMs
    && left.timelineStartMs === right.timelineStartMs
    && left.timelineEndMs === right.timelineEndMs;
}

function latestFeedback(events: readonly FeedbackEvent[]): FeedbackEvent | undefined {
  return [...events].sort((left, right) =>
    right.version - left.version || right.createdAt.localeCompare(left.createdAt))[0];
}

function digest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}
