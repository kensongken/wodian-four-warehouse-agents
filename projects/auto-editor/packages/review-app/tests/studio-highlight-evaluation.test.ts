import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { MaterialCard } from "@wearable/contracts";
import type { StudioEditorialVisualEvidence } from "@wearable/mobile-agent";
import { afterEach, describe, expect, it } from "vitest";

import {
  evaluateCard,
  isEligibleHighlightMaterial,
  StudioHighlightEvaluationStore,
  summarizeHighlightBatch,
  type StudioHighlightAttempt,
  type StudioHighlightCoverageProfile,
  type StudioHighlightEvaluation,
} from "../src/studio-highlight-evaluation";

const roots: string[] = [];
const card = materialCard();

describe("studio human highlight evaluation", () => {
  afterEach(async () => Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))));

  it("waits for a human label and never marks the result publishable or training eligible", () => {
    const result = evaluateCard(card, [], evidence([
      [2_000, 5_000, "飞机进入晚霞"],
      [7_000, 10_000, "云层打开"],
    ], [0]));
    expect(result).toMatchObject({
      status: "awaiting-human-label",
      publishable: false,
      trainingEligible: false,
      boundary: "human-label-required-model-evaluation-only",
    });
  });

  it("counts only explicit uploads or non-simulated hardware as eligible evidence", () => {
    expect(isEligibleHighlightMaterial({
      ...card, observations: ["客户上传素材：日常视频.mov"],
    })).toBe(true);
    expect(isEligibleHighlightMaterial({
      ...card, observations: ["硬件接入素材：戒指.mp4"], technicalFlags: ["接入设备：iPhone"],
    })).toBe(true);
    expect(isEligibleHighlightMaterial({
      ...card, observations: ["硬件接入素材：模拟.mp4"],
      technicalFlags: ["接入方式：模拟硬件经 iPhone"],
      pendingFacts: ["设备真实性：本地模拟，待真实硬件验证"],
    })).toBe(false);
    expect(isEligibleHighlightMaterial({
      ...card, observations: ["没有来源记录的旧素材"],
    })).toBe(false);
    expect(isEligibleHighlightMaterial({
      ...card, observations: ["客户上传素材：synthetic-color-tone-fixture-1.mp4"],
    })).toBe(false);
  });

  it("rejects repeated descriptions across time windows as non-discriminative", () => {
    const result = evaluateCard(card, [{
      id: "human-a", sourceAssetId: card.sourceAssetId, sourceDigest: card.sourceDigest,
      sourceStartMs: 3_000, sourceEndMs: 6_000, reason: "人工确认",
      confirmationMode: "manual-range", modelReceiptJobId: null,
      provenance: "human-confirmed", createdAt: "2026-10-14T00:00:00.000Z",
    }], evidence([
      [0, 3_000, "飞机飞过黄昏天空"],
      [3_000, 6_000, "飞机飞过黄昏天空"],
      [6_000, 9_000, "飞机飞过黄昏天空"],
    ], []));
    expect(result.status).toBe("no-discriminative-model-candidate");
    expect(result.modelCandidates).toEqual([]);
  });

  it("passes only when a distinct model interval overlaps the human interval by at least half", () => {
    const label = {
      id: "human-a", sourceAssetId: card.sourceAssetId, sourceDigest: card.sourceDigest,
      sourceStartMs: 3_000, sourceEndMs: 6_000, reason: "人工确认",
      confirmationMode: "manual-range" as const, modelReceiptJobId: null,
      provenance: "human-confirmed" as const, createdAt: "2026-10-14T00:00:00.000Z",
    };
    const passed = evaluateCard(card, [label], evidence([
      [2_500, 6_000, "飞机进入晚霞"], [8_000, 11_000, "云层变暗"],
    ], [0]));
    const failed = evaluateCard(card, [label], evidence([
      [0, 2_000, "飞机进入晚霞"], [8_000, 11_000, "云层变暗"],
    ], [0]));
    expect(passed.status).toBe("passed");
    expect(passed.scores[0]?.bestIoU).toBeGreaterThanOrEqual(0.5);
    expect(failed.status).toBe("failed");
  });

  it("scores multiple selected windows as a set so a long human interval is not impossible to pass", () => {
    const label = {
      id: "human-long", sourceAssetId: card.sourceAssetId, sourceDigest: card.sourceDigest,
      sourceStartMs: 3_000, sourceEndMs: 15_000, reason: "晚霞持续变化",
      confirmationMode: "manual-range" as const, modelReceiptJobId: null,
      provenance: "human-confirmed" as const, createdAt: "2026-10-14T00:00:00.000Z",
    };
    const result = evaluateCard(card, [label], evidence([
      [3_586, 7_173, "色彩增强"], [7_173, 10_759, "云层变化"], [10_759, 14_346, "天际线清楚"],
    ], [0, 1, 2]));
    expect(result.status).toBe("passed");
    expect(result.scores[0]?.bestIoU).toBeGreaterThan(0.8);
  });

  it("keeps automatic editing closed until ten distinct human-labeled videos exist", () => {
    const evaluation = evaluateCard(card, [{
      id: "human-a", sourceAssetId: card.sourceAssetId, sourceDigest: card.sourceDigest,
      sourceStartMs: 3_000, sourceEndMs: 15_000, reason: "晚霞持续变化",
      confirmationMode: "manual-range", modelReceiptJobId: null,
      provenance: "human-confirmed", createdAt: "2026-10-14T00:00:00.000Z",
    }], evidence([[3_000, 15_000, "晚霞持续变化"]], [0]));
    const batch = summarizeHighlightBatch([evaluation, evaluation], [{
      ...attempt(0, "succeeded", 127_198),
      sourceAssetId: card.sourceAssetId,
      sourceDigest: card.sourceDigest,
    }], [card], []);

    expect(batch).toMatchObject({
      status: "insufficient-samples",
      labeledVideos: 1,
      remainingVideos: 9,
      passedVideos: 1,
      passRate: 1,
      attemptedVideos: 1,
      evidenceCoverageComplete: true,
      p95RuntimeMs: 127_198,
      automaticEditingEligible: false,
      productionAccepted: false,
      automaticPublishing: false,
      actionPlan: {
        existingEligibleVideos: 1,
        existingVideosAwaitingLabel: 0,
        existingVideosAwaitingProfile: 1,
        minimumCountShortfall: 9,
        additionalEligibleVideosNeeded: 0,
        captureTargets: [],
        customerCaptureRequired: false,
        futureEvidenceMode: "natural-upload-accumulation",
        nextAction: { kind: "profile-existing", sourceAssetId: "asset-a", captureSequence: null },
        evidenceBasis: "confirmed-gaps-only",
        modelInferencesCounted: 0,
      },
      deliveryEvidence: {
        status: "insufficient-evidence", labeledVideos: 1, remainingVideos: 19,
        readyForHumanAcceptance: false, productionAccepted: false,
      },
    });
  });

  it("opens only the local pilot gate at ten videos, eighty percent pass rate, and acceptable P95", () => {
    const evaluations = Array.from({ length: 10 }, (_, index) => batchEvaluation(index, index < 8 ? "passed" : "failed"));
    const attempts = Array.from({ length: 10 }, (_, index) =>
      attempt(index, index === 9 ? "failed-retryable" : "succeeded", index === 9 ? null : 100_000 + index * 5_000));
    const materials = evaluations.map((evaluation, index) => ({
      ...materialCard(),
      id: `card-${index}`,
      sourceAssetId: evaluation.sourceAssetId,
      sourceDigest: evaluation.sourceDigest,
      sourceEndMs: index < 5 ? 20_000 : 120_000,
      contentType: index < 5 ? "single-product" as const : "jewelry-making-process" as const,
    }));
    const profiles = evaluations.map((evaluation, index) => coverageProfile(evaluation, index));
    const batch = summarizeHighlightBatch(evaluations, attempts, materials, profiles);

    expect(batch).toMatchObject({
      status: "pilot-threshold-met",
      labeledVideos: 10,
      passedVideos: 8,
      failedVideos: 2,
      passRate: 0.8,
      attemptedVideos: 10,
      evidenceCoverageComplete: true,
      p95RuntimeMs: 140_000,
      successfulRuntimeSamples: 9,
      automaticEditingEligible: true,
      productionAccepted: false,
      failureStages: [{ stage: "model-generation", count: 1 }],
      sampleCoverage: {
        status: "pilot-variety-met",
        contentTypes: ["jewelry-making-process", "single-product"],
        durationBands: ["short", "long"],
        profiledVideos: 10,
        profileCoverageComplete: true,
        modelInferencesCounted: 0,
      },
      actionPlan: {
        existingEligibleVideos: 10,
        existingVideosAwaitingLabel: 0,
        existingVideosAwaitingProfile: 0,
        minimumCountShortfall: 0,
        additionalEligibleVideosNeeded: 0,
        captureTargets: [],
        nextAction: null,
      },
      deliveryEvidence: {
        status: "insufficient-evidence", labeledVideos: 10, remainingVideos: 10,
        readyForHumanAcceptance: false, productionAccepted: false,
      },
    });
  });

  it("keeps automatic editing closed when ten passing videos are all the same kind and length", () => {
    const evaluations = Array.from({ length: 10 }, (_, index) => batchEvaluation(index, "passed"));
    const attempts = Array.from({ length: 10 }, (_, index) => attempt(index, "succeeded", 100_000));
    const materials = evaluations.map((evaluation, index) => ({
      ...materialCard(), id: `same-${index}`,
      sourceAssetId: evaluation.sourceAssetId, sourceDigest: evaluation.sourceDigest,
    }));
    const profiles = evaluations.map((evaluation) => coverageProfile(evaluation, 0));
    const batch = summarizeHighlightBatch(evaluations, attempts, materials, profiles);
    expect(batch).toMatchObject({
      status: "below-pilot-threshold",
      automaticEditingEligible: false,
      sampleCoverage: {
        status: "insufficient-variety",
        contentTypes: ["designer-viewpoint"],
        durationBands: ["short"],
        lightingTypes: ["stable"],
        motionTypes: ["static-display"],
        environmentTypes: ["indoor-controlled"],
        modelInferencesCounted: 0,
      },
      actionPlan: {
        existingEligibleVideos: 10,
        minimumCountShortfall: 0,
        additionalEligibleVideosNeeded: 0,
        captureTargets: [],
        customerCaptureRequired: false,
        futureEvidenceMode: "natural-upload-accumulation",
        nextAction: null,
      },
    });
  });

  it("requires twenty qualified samples before delivery evidence can enter human acceptance", () => {
    const evaluations = Array.from({ length: 20 }, (_, index) => batchEvaluation(index, index < 16 ? "passed" : "failed"));
    const attempts = Array.from({ length: 20 }, (_, index) => attempt(index, "succeeded", 100_000 + index * 1_000));
    const materials = evaluations.map((evaluation, index) => ({
      ...materialCard(), id: `delivery-${index}`,
      sourceAssetId: evaluation.sourceAssetId, sourceDigest: evaluation.sourceDigest,
      sourceEndMs: index < 10 ? 20_000 : 120_000,
      contentType: index < 10 ? "single-product" as const : "jewelry-making-process" as const,
    }));
    const profiles = evaluations.map((evaluation, index) => coverageProfile(evaluation, index));

    const batch = summarizeHighlightBatch(evaluations, attempts, materials, profiles);

    expect(batch).toMatchObject({
      status: "pilot-threshold-met", automaticEditingEligible: true, productionAccepted: false,
      deliveryEvidence: {
        status: "ready-for-human-acceptance", threshold: { minimumLabeledVideos: 20 },
        labeledVideos: 20, remainingVideos: 0, readyForHumanAcceptance: true,
        productionAccepted: false, boundary: "evidence-readiness-is-not-production-acceptance",
      },
    });
  });

  it("persists human provenance, enforces source bounds, and makes operation keys idempotent", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-highlights-"));
    roots.push(root);
    const store = new StudioHighlightEvaluationStore(join(root, "state.json"), {
      actorId: "actor-a", customerId: "customer-a", sessionId: "session-a",
    }, () => "2026-10-14T00:00:00.000Z");
    const input = { card, sourceStartMs: 3_000, sourceEndMs: 6_000, reason: "飞机穿过晚霞", operationKey: "op-a" };
    const first = await store.record(input);
    const replay = await store.record(input);
    expect(replay).toEqual(first);
    expect(first).toMatchObject({
      provenance: "human-confirmed", reason: "飞机穿过晚霞",
      confirmationMode: "manual-range", modelReceiptJobId: null,
    });
    const statePath = join(root, "state.json");
    const legacy = JSON.parse(await readFile(statePath, "utf8")) as Record<string, any>;
    legacy.schemaVersion = 2;
    delete legacy.labels[0].confirmationMode;
    delete legacy.labels[0].modelReceiptJobId;
    await writeFile(statePath, `${JSON.stringify(legacy, null, 2)}\n`);
    const migrated = await new StudioHighlightEvaluationStore(statePath, {
      actorId: "actor-a", customerId: "customer-a", sessionId: "session-a",
    }).evaluate([card], []);
    expect(migrated[0]?.labels[0]).toMatchObject({
      confirmationMode: "manual-range", modelReceiptJobId: null,
    });
    await expect(store.record({ ...input, reason: "换一个答案" })).rejects.toThrow("studio-highlight-operation-key-conflict");
    await expect(store.record({ ...input, operationKey: "op-b", sourceEndMs: 20_001 }))
      .rejects.toThrow("studio-highlight-range-outside-source");
    const accepted = await store.record({
      ...input,
      operationKey: "candidate-a",
      reason: "人工看过并确认模型候选：飞机穿过晚霞",
      confirmationMode: "accepted-model-candidate",
      modelReceiptJobId: "vision-job-a",
    });
    expect(accepted).toMatchObject({
      confirmationMode: "accepted-model-candidate", modelReceiptJobId: "vision-job-a",
    });
    await expect(store.record({
      ...input,
      operationKey: "candidate-invalid",
      confirmationMode: "accepted-model-candidate",
      modelReceiptJobId: null,
    })).rejects.toThrow("studio-highlight-confirmation-binding-invalid");

    const rejected = await store.rejectCandidate({
      card, sourceStartMs: 7_000, sourceEndMs: 10_000,
      receiptJobId: "vision-job-b", operationKey: "reject-candidate-b",
    });
    expect(rejected).toMatchObject({
      sourceAssetId: card.sourceAssetId, sourceDigest: card.sourceDigest,
      sourceStartMs: 7_000, sourceEndMs: 10_000, receiptJobId: "vision-job-b",
      reason: "not-a-highlight", provenance: "human-confirmed",
    });
    expect(await store.rejectCandidate({
      card, sourceStartMs: 7_000, sourceEndMs: 10_000,
      receiptJobId: "vision-job-b", operationKey: "reject-candidate-b",
    })).toEqual(rejected);
    expect(await store.candidateRejections()).toEqual([rejected]);
    expect(await new StudioHighlightEvaluationStore(statePath, {
      actorId: "actor-a", customerId: "customer-a", sessionId: "session-a",
    }).candidateRejections()).toEqual([rejected]);
    await expect(store.rejectCandidate({
      card, sourceStartMs: 3_000, sourceEndMs: 6_000,
      receiptJobId: "vision-job-a", operationKey: "reject-accepted",
    })).rejects.toThrow("studio-highlight-candidate-decision-conflict");
    await expect(store.record({
      card, sourceStartMs: 7_000, sourceEndMs: 10_000,
      reason: "不应同时成为正反馈", operationKey: "accept-rejected",
      confirmationMode: "accepted-model-candidate", modelReceiptJobId: "vision-job-b",
    })).rejects.toThrow("studio-highlight-candidate-decision-conflict");
    await expect(store.rejectCandidate({
      card, sourceStartMs: 7_000, sourceEndMs: 20_001,
      receiptJobId: "vision-job-b", operationKey: "reject-outside",
    })).rejects.toThrow("studio-highlight-range-outside-source");
    await expect(store.rejectCandidate({
      card, sourceStartMs: 7_000, sourceEndMs: 10_000,
      receiptJobId: "vision-job-c", operationKey: "reject-candidate-b",
    })).rejects.toThrow("studio-highlight-operation-key-conflict");

    const profile = await store.recordCoverage({
      card, lighting: "stable", motion: "static-display", environment: "indoor-controlled",
      operationKey: "coverage-a",
    });
    expect(profile).toMatchObject({
      provenance: "human-confirmed", lighting: "stable", motion: "static-display",
      environment: "indoor-controlled",
    });
    expect(await store.coverageProfiles()).toEqual([profile]);
    await expect(store.recordCoverage({
      card, lighting: "made-up", motion: "static-display", environment: "indoor-controlled",
      operationKey: "coverage-invalid",
    })).rejects.toThrow("studio-highlight-lighting-invalid");
  });
});

function materialCard(): MaterialCard {
  return {
    id: "card-a", customerId: "customer-a", sessionId: "session-a", version: 1,
    sourceAssetId: "asset-a", sourceDigest: "a".repeat(64), sourceStartMs: 0, sourceEndMs: 20_000,
    contentType: "designer-viewpoint", technicalUsable: true, technicalFlags: ["ffprobe:video"],
    observations: ["airplane"], confirmedFacts: [], pendingFacts: [],
    capability: { mode: "provider", label: "local", version: 1, provider: "local-v1" },
    createdAt: "2026-10-14T00:00:00.000Z",
  };
}

function coverageProfile(
  evaluation: StudioHighlightEvaluation,
  index: number,
): StudioHighlightCoverageProfile {
  return {
    sourceAssetId: evaluation.sourceAssetId,
    sourceDigest: evaluation.sourceDigest,
    lighting: index % 2 === 0 ? "stable" : "mixed",
    motion: index % 2 === 0 ? "static-display" : "making-action",
    environment: index % 2 === 0 ? "indoor-controlled" : "outdoor",
    provenance: "human-confirmed",
    updatedAt: "2026-10-15T00:00:00.000Z",
  };
}

function evidence(
  rows: readonly (readonly [number, number, string])[],
  highlightIndices: readonly number[] = [],
): StudioEditorialVisualEvidence[] {
  return [{
    sourceAssetId: card.sourceAssetId, sourceDigest: card.sourceDigest,
    sourceStartMs: card.sourceStartMs, sourceEndMs: card.sourceEndMs, receiptJobId: "vision-job-a",
    provider: {
      layer: "vision", adapterName: "local", modelName: "qwen", modelVersion: "3",
      checkpointDigest: "b".repeat(64),
    },
    observations: rows.map((row) => row[2]),
    localizedObservations: rows.map(([startMs, endMs, text]) => ({
      kind: "visible-scene", startMs, endMs, text, confidence: 0.96,
    })),
    highlightCandidates: highlightIndices.map((index) => ({
      startMs: rows[index]![0], endMs: rows[index]![1], reason: rows[index]![2], confidence: 0.9,
    })),
  }];
}

function batchEvaluation(index: number, status: "passed" | "failed"): StudioHighlightEvaluation {
  return {
    sourceAssetId: `asset-${index}`,
    sourceDigest: index.toString(16).padStart(64, "0"),
    sourceStartMs: 0,
    sourceEndMs: 20_000,
    status,
    threshold: { minimumIoU: 0.5 },
    labels: [{
      id: `label-${index}`, sourceAssetId: `asset-${index}`,
      sourceDigest: index.toString(16).padStart(64, "0"), sourceStartMs: 3_000, sourceEndMs: 15_000,
      reason: "人工高光", confirmationMode: "manual-range", modelReceiptJobId: null,
      provenance: "human-confirmed", createdAt: "2026-10-15T00:00:00.000Z",
    }],
    modelCandidates: status === "passed"
      ? [{ startMs: 3_000, endMs: 15_000, text: "模型高光", receiptJobId: `job-${index}` }]
      : [{ startMs: 0, endMs: 2_000, text: "模型高光", receiptJobId: `job-${index}` }],
    scores: [{
      labelId: `label-${index}`, bestIoU: status === "passed" ? 1 : 0,
      bestCandidateStartMs: status === "passed" ? 3_000 : 0,
      bestCandidateEndMs: status === "passed" ? 15_000 : 2_000,
    }],
    publishable: false,
    trainingEligible: false,
    boundary: "human-label-required-model-evaluation-only",
  };
}

function attempt(
  index: number,
  status: StudioHighlightAttempt["status"],
  runtimeMs: number | null,
): StudioHighlightAttempt {
  return {
    sourceAssetId: `asset-${index}`,
    sourceDigest: index.toString(16).padStart(64, "0"),
    status,
    attempt: 1,
    failureCode: status === "succeeded" ? null : "studio-analysis-worker-timeout",
    lastStage: status === "succeeded" ? null : "model-generation",
    activeStageElapsedMs: status === "succeeded" ? null : 180_000,
    runtimeMs,
  };
}
