import { lstat, mkdtemp, readFile, readdir, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join, relative } from "node:path";

import type {
  ModelAnalysisJobV1,
  ModelAnalysisReceiptV1,
  StudioAnalysisStatus,
} from "@wearable/contracts";
import {
  PlatformAutomaticEditorialAdapter,
  PrivateContentStudio,
  StudioPreferenceRanker,
  type StudioModelWorker,
} from "@wearable/mobile-agent";
import request from "supertest";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createDefaultStudioModelWorker,
  createDefaultStudioModelWorkerConfig,
  createStudioServer,
  type StudioServerOptions,
} from "../src/studio-server";
import {
  createModelCheckerStatusReader,
  isTrustedEditorialVisionReceipt,
  StudioModelRuntime,
} from "../src/studio-model-runtime";

const ORIGIN = "http://127.0.0.1";
const roots: string[] = [];

describe("scoped studio model API", () => {
  afterEach(async () => {
    vi.restoreAllMocks();
    await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
  });

  it("accepts only a server-bound human highlight and keeps evaluation out of training and publishing", async () => {
    const { api } = await createApi({ modelWorker: successfulWorker() });
    const session = await openSession(api, "customer-a");
    const inventory = await studioPost(api, "/api/studio/inventory", {}, session.token);
    const card = inventory.body.materials[0];
    expect(card).toBeTruthy();

    const before = await studioPost(api, "/api/studio/highlights/list", {}, session.token);
    expect(before.body.batch).toMatchObject({
      status: "insufficient-samples", labeledVideos: 0, remainingVideos: 10,
      automaticEditingEligible: false, productionAccepted: false, automaticPublishing: false,
    });
    const blockedAutomaticDraft = await studioPost(api, "/api/studio/highlights/automatic-drafts", {
      briefId: "brief-not-yet-relevant",
      operationKey: "automatic-draft-blocked",
    }, session.token);
    expect(blockedAutomaticDraft.status).toBe(422);
    expect(blockedAutomaticDraft.body).toEqual({ error: "studio-highlight-automatic-edit-gate-not-met" });
    const pending = before.body.evaluations.find((item: { sourceAssetId: string }) =>
      item.sourceAssetId === card.sourceAssetId);
    expect(pending).toMatchObject({
      status: "awaiting-human-label", publishable: false, trainingEligible: false,
    });

    const forged = await studioPost(api, "/api/studio/highlights", {
      materialCardId: card.id,
      sourceStartMs: card.sourceStartMs,
      sourceEndMs: Math.min(card.sourceEndMs, card.sourceStartMs + 1_000),
      reason: "人工确认",
      operationKey: "highlight-forged",
      modelCandidates: [{ startMs: 0, endMs: 1_000 }],
    }, session.token);
    expect(forged.status).toBe(403);

    const unconfirmed = await studioPost(api, "/api/studio/highlights", {
      materialCardId: card.id,
      sourceStartMs: card.sourceStartMs,
      sourceEndMs: Math.min(card.sourceEndMs, card.sourceStartMs + 1_000),
      reason: "模型候选看起来可以",
      humanConfirmed: false,
      operationKey: "highlight-unconfirmed",
    }, session.token);
    expect(unconfirmed.status).toBe(400);
    expect(unconfirmed.body).toEqual({ error: "studio-highlight-human-confirmation-required" });

    const syntheticCoverage = await studioPost(api, "/api/studio/highlights/coverage", {
      materialCardId: card.id,
      lighting: "stable",
      motion: "static-display",
      environment: "indoor-controlled",
      humanConfirmed: true,
      operationKey: "synthetic-coverage-rejected",
    }, session.token);
    expect(syntheticCoverage.status).toBe(400);
    expect(syntheticCoverage.body).toEqual({ error: "studio-highlight-eligible-material-required" });

    const body = {
      materialCardId: card.id,
      sourceStartMs: card.sourceStartMs,
      sourceEndMs: Math.min(card.sourceEndMs, card.sourceStartMs + 1_000),
      reason: "人工确认这段画面最好",
      humanConfirmed: true,
      operationKey: "highlight-human-a",
    };
    const created = await studioPost(api, "/api/studio/highlights", body, session.token);
    expect(created.status).toBe(201);
    expect(created.body.label).toMatchObject({ provenance: "human-confirmed", reason: body.reason });
    const evaluated = created.body.evaluations.find((item: { sourceAssetId: string }) =>
      item.sourceAssetId === card.sourceAssetId);
    expect(evaluated).toMatchObject({
      status: "no-discriminative-model-candidate", publishable: false, trainingEligible: false,
    });
    expect(created.body.batch).toMatchObject({
      status: "insufficient-samples", labeledVideos: 0, remainingVideos: 10,
      automaticEditingEligible: false,
    });

    const replay = await studioPost(api, "/api/studio/highlights", body, session.token);
    expect(replay.status).toBe(201);
    expect(replay.body.label.id).toBe(created.body.label.id);
  });

  it("keeps trusted highlight-only model receipts but excludes synthetic fixtures from the real gate", async () => {
    const worker = {
      run: vi.fn(async (job: ModelAnalysisJobV1): Promise<ModelAnalysisReceiptV1> => {
        const duration = job.sourceEndMs - job.sourceStartMs;
        const firstEnd = job.sourceStartMs + Math.floor(duration / 3);
        const secondEnd = job.sourceStartMs + Math.floor(duration * 2 / 3);
        return {
          ...receiptFor(job),
          observations: [],
          suggestedTags: [],
          qualitySignals: {
            highlightCandidates: [
              { startMs: job.sourceStartMs, endMs: firstEnd, reason: "晚霞颜色开始增强", confidence: 0.8 },
              { startMs: firstEnd, endMs: secondEnd, reason: "飞机穿过晚霞", confidence: 0.8 },
              { startMs: secondEnd, endMs: job.sourceEndMs, reason: "云层轮廓清楚", confidence: 0.8 },
            ],
          },
          synthetic: false,
          providerMode: "local",
        };
      }),
      health: vi.fn(async () => ({
        mode: "local" as const,
        installed: true as const,
        modelName: "qwen-highlight-fixture",
        checkpointDigest: "c".repeat(64),
      })),
    } satisfies StudioModelWorker;
    const { api } = await createApi({ modelWorker: worker });
    const session = await openSession(api, "customer-a");
    const inventory = await studioPost(api, "/api/studio/inventory", {}, session.token).expect(200);
    const card = inventory.body.materials[0];
    expect(card).toBeTruthy();

    const created = await studioPost(api, "/api/studio/highlights", {
      materialCardId: card.id,
      sourceStartMs: card.sourceStartMs,
      sourceEndMs: card.sourceEndMs,
      reason: "人工确认整段用于回归验证",
      humanConfirmed: true,
      operationKey: "highlight-only-human",
    }, session.token).expect(201);
    expect(created.body.evaluations.find((item: { sourceAssetId: string }) =>
      item.sourceAssetId === card.sourceAssetId)).toMatchObject({
      status: "no-discriminative-model-candidate",
    });

    const firstRetest = await studioPost(
      api, "/api/studio/highlights/retest", { materialCardId: card.id }, session.token,
    ).expect(200);
    const evaluated = firstRetest.body.evaluations.find((item: { sourceAssetId: string }) =>
      item.sourceAssetId === card.sourceAssetId);

    expect(evaluated).toMatchObject({
      status: "passed",
      publishable: false,
      trainingEligible: false,
    });
    expect(evaluated.modelCandidates).toHaveLength(3);
    expect(evaluated.scores[0]?.bestIoU).toBe(1);

    const candidate = evaluated.modelCandidates[1];
    const candidateBody = {
      materialCardId: card.id,
      receiptJobId: candidate.receiptJobId,
      sourceStartMs: candidate.startMs,
      sourceEndMs: candidate.endMs,
      humanConfirmed: true,
      operationKey: "accept-bound-model-candidate",
    };
    const forgedCandidate = await studioPost(api, "/api/studio/highlights/accept-candidate", {
      ...candidateBody,
      sourceStartMs: candidate.startMs + 1,
      operationKey: "accept-forged-model-candidate",
    }, session.token);
    expect(forgedCandidate.status).toBe(409);
    expect(forgedCandidate.body).toEqual({ error: "studio-highlight-candidate-binding-mismatch" });

    const acceptedCandidate = await studioPost(
      api, "/api/studio/highlights/accept-candidate", candidateBody, session.token,
    ).expect(201);
    expect(acceptedCandidate.body.label).toMatchObject({
      sourceStartMs: candidate.startMs,
      sourceEndMs: candidate.endMs,
      confirmationMode: "accepted-model-candidate",
      modelReceiptJobId: candidate.receiptJobId,
      provenance: "human-confirmed",
    });
    expect((await studioPost(
      api, "/api/studio/highlights/accept-candidate", candidateBody, session.token,
    ).expect(201)).body.label.id).toBe(acceptedCandidate.body.label.id);

    const rejectedModelCandidate = evaluated.modelCandidates[2];
    const rejectedBody = {
      materialCardId: card.id,
      receiptJobId: rejectedModelCandidate.receiptJobId,
      sourceStartMs: rejectedModelCandidate.startMs,
      sourceEndMs: rejectedModelCandidate.endMs,
      humanConfirmed: true,
      operationKey: "reject-bound-model-candidate",
    };
    const forgedRejection = await studioPost(api, "/api/studio/highlights/reject-candidate", {
      ...rejectedBody,
      sourceStartMs: rejectedModelCandidate.startMs + 1,
      operationKey: "reject-forged-model-candidate",
    }, session.token);
    expect(forgedRejection.status).toBe(409);
    expect(forgedRejection.body).toEqual({ error: "studio-highlight-candidate-binding-mismatch" });
    const rejectedCandidate = await studioPost(
      api, "/api/studio/highlights/reject-candidate", rejectedBody, session.token,
    ).expect(201);
    expect(rejectedCandidate.body.rejection).toMatchObject({
      sourceStartMs: rejectedModelCandidate.startMs,
      sourceEndMs: rejectedModelCandidate.endMs,
      receiptJobId: rejectedModelCandidate.receiptJobId,
      reason: "not-a-highlight",
      provenance: "human-confirmed",
    });
    expect(rejectedCandidate.body.batch).toMatchObject({
      labeledVideos: 0, automaticEditingEligible: false,
    });
    expect((await studioPost(
      api, "/api/studio/highlights/reject-candidate", rejectedBody, session.token,
    ).expect(201)).body.rejection.id).toBe(rejectedCandidate.body.rejection.id);
    expect((await studioPost(api, "/api/studio/highlights/accept-candidate", {
      ...rejectedBody, operationKey: "accept-rejected-model-candidate",
    }, session.token)).status).toBe(409);
    expect((await studioPost(api, "/api/studio/highlights/reject-candidate", {
      ...candidateBody, operationKey: "reject-accepted-model-candidate",
    }, session.token)).status).toBe(409);

    const report = await studioPost(api, "/api/studio/highlights/report", {}, session.token).expect(200);
    expect(report.body.report).toMatchObject({
      schemaVersion: 1,
      gate: { labeledVideos: 0, automaticEditingEligible: false },
      productionAccepted: false,
      automaticPublishing: false,
      boundary: "local-pilot-evidence-only",
    });
    expect(report.body.report.samples).toHaveLength(0);
    expect(report.body.report.candidateRejections).toHaveLength(1);

    const feedbackDataset = await studioPost(
      api, "/api/studio/highlights/feedback-dataset", {}, session.token,
    ).expect(200);
    expect(feedbackDataset.body.dataset).toMatchObject({
      schemaVersion: 1,
      counts: { total: 0, excludedUnverifiedSourceRecords: 3 },
      readiness: {
        status: "empty", trainingEligible: false, automaticTraining: false,
        releaseEligible: false, nextGate: "human-dataset-review-required",
      },
      boundary: "versioned-candidate-dataset-not-training-approval",
    });
    expect(feedbackDataset.body.dataset.datasetId).toMatch(/^highlight-feedback-v1-[a-f0-9]{12}$/);
    expect(feedbackDataset.body.dataset.datasetDigest).toMatch(/^[a-f0-9]{64}$/);

    const secondRetest = await studioPost(
      api, "/api/studio/highlights/retest", { materialCardId: card.id }, session.token,
    ).expect(200);
    expect(secondRetest.body).toMatchObject({
      batch: { labeledVideos: 0, attemptedVideos: 0, automaticEditingEligible: false },
    });
    expect(secondRetest.body.evaluations.find((item: { sourceAssetId: string }) =>
      item.sourceAssetId === card.sourceAssetId).modelCandidates).toHaveLength(3);
    expect(worker.run).toHaveBeenCalledTimes(2);
  });

  it("rejects synthetic, remote, failed, and non-vision receipts as editorial visual proof", () => {
    const job: ModelAnalysisJobV1 = {
      schemaVersion: 1,
      jobId: "vision-proof-job",
      customerId: "customer-a",
      sessionId: "session-a",
      sourceAssetId: "asset-a",
      sourceDigest: "a".repeat(64),
      sourcePath: "/synthetic/input.mp4",
      sourceStartMs: 0,
      sourceEndMs: 10_000,
      layer: "vision",
      operationKey: "vision-proof-operation",
      requestedAt: "2026-10-13T00:00:00.000Z",
    };
    const fallback = receiptFor(job);
    const trusted = {
      ...fallback,
      synthetic: false as const,
      providerMode: "local" as const,
    };
    expect(isTrustedEditorialVisionReceipt(trusted)).toBe(true);
    expect(isTrustedEditorialVisionReceipt({ ...trusted, synthetic: true })).toBe(false);
    expect(isTrustedEditorialVisionReceipt({ ...trusted, providerMode: "offline-fallback" })).toBe(false);
    expect(isTrustedEditorialVisionReceipt({
      ...trusted,
      status: "failed-manual-review",
      failure: { code: "fixture-failed" },
    })).toBe(false);
    expect(isTrustedEditorialVisionReceipt({ ...trusted, layer: "transcript" })).toBe(false);
  });

  it("derives scope and source path while rejecting every client-supplied scope or path field", async () => {
    const worker = successfulWorker();
    const { api, root } = await createApi({ modelWorker: worker });
    const canonicalRoot = await realpath(root);
    const session = await openSession(api, "customer-a");
    const assetId = await firstAsset(api, session.token);

    for (const field of ["customerId", "sessionId", "actorId", "sourcePath", "path"]) {
      const response = await studioPost(api, "/api/studio/analysis/jobs", {
        assetId,
        layers: ["technical"],
        operationKey: `forged-${field}`,
        [field]: field === "customerId" ? "customer-b" : `forged-${field}`,
      }, session.token);
      expect(response.status, field).toBe(403);
      expect(response.body).toEqual({ error: "studio-customer-scope-forbidden" });
    }
    expect(worker.run).not.toHaveBeenCalled();

    const created = await studioPost(api, "/api/studio/analysis/jobs", {
      assetId,
      layers: ["technical", "transcript", "embedding", "vision"],
      operationKey: "analysis-safe-fields-only",
    }, session.token);

    expect(created.status).toBe(201);
    expect(created.body.analysis.job).toMatchObject({
      sourceAssetId: assetId,
      status: "succeeded",
      layers: ["technical", "transcript", "embedding", "vision"],
    });
    expect(created.body.analysis.job).not.toHaveProperty("actorId");
    expect(created.body.analysis.job).not.toHaveProperty("customerId");
    expect(created.body.analysis.job).not.toHaveProperty("sessionId");
    expect(created.body.analysis.job).not.toHaveProperty("sourcePath");
    expect(created.body.analysis.job).not.toHaveProperty("operationKey");
    expect(created.body.analysis.receipts).toHaveLength(4);
    expect(created.body.analysis.receipts.every((receipt: Record<string, unknown>) =>
      !("sourcePath" in receipt) && !("operationKey" in receipt) && !("customerId" in receipt),
    )).toBe(true);
    expect(JSON.stringify(created.body)).not.toContain(session.customerId);
    expect(JSON.stringify(created.body)).not.toContain(session.sessionId);
    expect(worker.run).toHaveBeenCalledTimes(4);
    for (const [job] of worker.run.mock.calls) {
      expect(job.customerId).toBe(session.customerId);
      expect(job.sessionId).toBe(session.sessionId);
      expect(job.sourceAssetId).toBe(assetId);
      expect(isAbsolute(job.sourcePath)).toBe(true);
      expect(relative(canonicalRoot, job.sourcePath)).not.toMatch(/^\.\.(?:\/|$)/);
    }
  }, 120_000);

  it("automatically transcribes selected material and carries subtitle suggestions into four drafts", async () => {
    const worker = editorialTranscriptWorker();
    const { api } = await createApi({
      modelWorker: worker,
      editorial: new PlatformAutomaticEditorialAdapter(),
    });
    const session = await openSession(api, "customer-a");
    const inventory = await studioPost(api, "/api/studio/inventory", {}, session.token).expect(200);
    const createdDemand = await studioPost(api, "/api/studio/demands", {
      contentType: "single-product",
      goal: "把现有素材自动整理成平台样片",
      targetAudience: "珠宝顾客",
      desiredEffect: "清楚可信",
      mustInclude: [],
      mustAvoid: [],
      submittedBy: "customer-self-declared",
      operationKey: "demand-automatic-transcript",
    }, session.token).expect(201);
    const matched = await studioPost(api, "/api/studio/matches", {
      demandId: createdDemand.body.demand.id,
      operationKey: "match-automatic-transcript",
    }, session.token).expect(201);
    const demand = createdDemand.body.demand;
    const assessment = matched.body.assessment;
    const opportunities = await studioPost(api, "/api/studio/opportunities", {
      demandId: demand.id,
      matchAssessmentId: assessment.id,
    }, session.token).expect(200);
    const brief = await studioPost(api, "/api/studio/briefs", {
      opportunityId: opportunities.body.opportunities[0].id,
      matchAssessmentId: assessment.id,
      selectedAssetIds: inventory.body.inventory.items.map((item: { sourceAssetId: string }) => item.sourceAssetId),
      confirmedFactIds: [],
      reviewRequirements: ["采用前人工复核字幕"],
      operationKey: "brief-automatic-transcript",
    }, session.token).expect(201);

    const result = await studioPost(api, "/api/studio/drafts", {
      briefId: brief.body.brief.id,
      operationKey: "drafts-automatic-transcript",
    }, session.token).expect(201);

    expect(result.body.drafts).toHaveLength(4);
    expect(result.body.drafts.every((draft: { captionCues?: unknown[] }) =>
      (draft.captionCues?.length ?? 0) > 0)).toBe(true);
    expect(result.body.drafts.every((draft: { visualEvidence?: { status?: string; items?: unknown[] } }) =>
      draft.visualEvidence?.status === "used"
      && (draft.visualEvidence.items?.length ?? 0) > 0)).toBe(true);
    expect(worker.run).toHaveBeenCalledTimes(8);
    expect(worker.run.mock.calls.filter(([job]) => job.layer === "transcript")).toHaveLength(4);
    expect(worker.run.mock.calls.filter(([job]) => job.layer === "vision")).toHaveLength(4);
  }, 360_000);

  it("keeps analysis history customer/session isolated and exposes no server path", async () => {
    const worker = successfulWorker();
    const { api } = await createApi({ modelWorker: worker });
    const sessionA = await openSession(api, "customer-a");
    const assetA = await firstAsset(api, sessionA.token);
    await studioPost(api, "/api/studio/analysis/jobs", {
      assetId: assetA,
      layers: ["vision"],
      operationKey: "customer-a-analysis",
    }, sessionA.token).expect(201);

    const sessionB = await openSession(api, "customer-b");
    const historyB = await studioPost(api, "/api/studio/analysis/history", {}, sessionB.token);
    const forged = await studioPost(api, "/api/studio/analysis/history", {
      customerId: sessionA.customerId,
    }, sessionB.token);

    expect(historyB.status).toBe(200);
    expect(historyB.body.history.entries).toEqual([]);
    expect(JSON.stringify(historyB.body)).not.toContain(sessionA.sessionId);
    expect(forged.status).toBe(403);
    expect(forged.body).toEqual({ error: "studio-customer-scope-forbidden" });

    const historyA = await studioPost(api, "/api/studio/analysis/history", {}, sessionA.token);
    expect(historyA.body.history.entries).toHaveLength(1);
    expect(JSON.stringify(historyA.body)).not.toMatch(/sourcePath|operationKey/);
  }, 120_000);

  it("accepts only exact receipt-bound human review actions and replays one operation exactly", async () => {
    const worker = successfulWorker();
    const { api } = await createApi({ modelWorker: worker });
    const session = await openSession(api, "customer-a");
    const assetId = await firstAsset(api, session.token);
    const started = await studioPost(api, "/api/studio/analysis/jobs", {
      assetId,
      layers: ["vision", "embedding"],
      operationKey: "reviewable-analysis",
    }, session.token).expect(201);
    const receipt = started.body.analysis.receipts.find((item: Record<string, unknown>) => item.layer === "vision");
    const exact = {
      action: "accept-suggestion",
      receiptJobId: receipt.jobId,
      sourceAssetId: receipt.sourceAssetId,
      sourceDigest: receipt.sourceDigest,
      sourceStartMs: receipt.sourceStartMs,
      sourceEndMs: receipt.sourceEndMs,
      target: "戒指特写",
      correction: null,
      operationKey: "review-accept-ring-closeup",
    };

    const wrongDigest = await studioPost(api, "/api/studio/analysis/review", {
      ...exact,
      operationKey: "wrong-digest",
      sourceDigest: "0".repeat(64),
    }, session.token);
    expect(wrongDigest.status).toBe(409);
    expect(wrongDigest.body).toEqual({ error: "studio-analysis-review-binding-mismatch" });

    const wrongTarget = await studioPost(api, "/api/studio/analysis/review", {
      ...exact,
      operationKey: "wrong-target",
      target: "未出现过的模型建议",
    }, session.token);
    expect(wrongTarget.status).toBe(409);
    expect(wrongTarget.body).toEqual({ error: "studio-analysis-review-target-mismatch" });

    for (const action of ["accept", "correct", "reject", "delete-original", "publish"]) {
      const invalid = await studioPost(api, "/api/studio/analysis/review", {
        ...exact,
        action,
        operationKey: `invalid-${action}`,
      }, session.token);
      expect(invalid.status, action).toBe(400);
      expect(invalid.body).toEqual({ error: "studio-analysis-review-action-invalid" });
    }

    const accepted = await studioPost(api, "/api/studio/analysis/review", exact, session.token);
    const replay = await studioPost(api, "/api/studio/analysis/review", exact, session.token);
    expect(accepted.status).toBe(201);
    expect(replay.status).toBe(201);
    expect(replay.body).toEqual(accepted.body);
    expect(accepted.body.review).toMatchObject({
      action: "accept-suggestion",
      receiptJobId: receipt.jobId,
      sourceAssetId: assetId,
      target: "戒指特写",
      correction: null,
    });

    const corrected = await studioPost(api, "/api/studio/analysis/review", {
      ...exact,
      action: "correct-suggestion",
      correction: "戒指制作特写",
      operationKey: "review-correct-ring-closeup",
    }, session.token);
    expect(corrected.status).toBe(201);

    const rejected = await studioPost(api, "/api/studio/analysis/review", {
      ...exact,
      action: "reject-suggestion",
      operationKey: "review-reject-ring-closeup",
    }, session.token);
    expect(rejected.status).toBe(201);

    const history = await studioPost(api, "/api/studio/analysis/history", {}, session.token);
    expect(history.body.history.reviews).toHaveLength(3);
  }, 120_000);

  it("returns sanitized local model identity and explicit degradation", async () => {
    const { api } = await createApi({
      modelWorker: successfulWorker(),
      modelStatusReader: async () => ({
        mode: "offline-fallback",
        localOnly: true,
        degraded: true,
        artifacts: [{ id: "qwen3-vl-2b", version: "2b-4bit", status: "unapproved" }],
        visionProbe: "not-run: runtime-unapproved",
      }),
    });
    const session = await openSession(api, "customer-a");

    const status = await studioPost(api, "/api/studio/models/status", {}, session.token);

    expect(status.status).toBe(200);
    expect(status.body.models).toEqual({
      mode: "offline-fallback",
      localOnly: true,
      degraded: true,
      artifacts: [{ id: "qwen3-vl-2b", version: "2b-4bit", status: "unapproved" }],
      visionProbe: "not-run: runtime-unapproved",
    });
    expect(JSON.stringify(status.body)).not.toMatch(/root|path|https?:\/\//i);
  }, 120_000);

  it("reads the default locked model identities from a workspace path containing non-ASCII characters", async () => {
    const { api } = await createApi({ modelWorker: successfulWorker() });
    const session = await openSession(api, "customer-a");

    const status = await studioPost(api, "/api/studio/models/status", {}, session.token);

    expect(status.status).toBe(200);
    expect(status.body.models).toMatchObject({
      mode: "offline-fallback",
      localOnly: true,
      degraded: true,
    });
    const artifactStatus = new Map<string, string>(status.body.models.artifacts.map(
      (artifact: { id: string; status: string }) => [artifact.id, artifact.status],
    ));
    expect(artifactStatus.get("whisper.cpp-1.9.2-small-q5_0")).toBe("unapproved");
    expect(artifactStatus.get("openclip-3.3.0-vit-b-32")).toBe("unapproved");
    expect(artifactStatus.get("whisper.cpp-1.9.2-small-q5_1")).not.toBe("unapproved");
    expect(artifactStatus.get("qwen3-vl-2b-instruct-4bit")).not.toBe("unapproved");
    expect(JSON.stringify(status.body)).not.toMatch(/root|path|https?:\/\//i);
  }, 120_000);

  it("constructs the formal worker from the verified installed launcher with exact locked arguments", async () => {
    const sessionRoot = await realpath(await mkdtemp(join(tmpdir(), "studio-default-worker-")));
    roots.push(sessionRoot);
    const runtimeRoot = join(sessionRoot, "installed-runtime");
    const launcherIdentity = {
      device: "123",
      inode: "456",
      byteLength: 789,
      sha256: "6".repeat(64),
    };
    const resolveRuntime = vi.fn(() => ({
      runtimeRoot,
      launcherPath: join(runtimeRoot, "bin", "run-offline-worker"),
      launcherIdentity,
      status: "runtime-real-probe-succeeded" as const,
      bundle: { id: "synthetic-worker-runtime", treeSha256: "9".repeat(64) },
    }));
    const config = createDefaultStudioModelWorkerConfig(sessionRoot, {
      resolveRuntime,
      sandboxPath: "/synthetic/sandbox-exec",
      probeSandbox: () => true,
      lockPath: join(sessionRoot, "models.lock.json"),
      modelRoot: join(sessionRoot, "models"),
      runtimeId: "synthetic-worker-runtime",
    });
    expect(config.command).toBe(join(runtimeRoot, "bin", "run-offline-worker"));
    expect(config.commandIdentity).toEqual(launcherIdentity);
    expect(config.stageTelemetry).toEqual({
      protocol: "studio-worker-stage-telemetry-v1",
      maxBytes: 4096,
    });
    expect(config.command).not.toContain("tools/media-model-worker/.venv");
    expect(resolveRuntime).toHaveBeenCalledWith({
      lockPath: join(sessionRoot, "models.lock.json"),
      modelRoot: join(sessionRoot, "models"),
      runtimeId: "synthetic-worker-runtime",
    });
    const telemetryConfig = createDefaultStudioModelWorkerConfig(sessionRoot, {
      resolveRuntime,
      sandboxPath: "/synthetic/sandbox-exec",
      probeSandbox: () => true,
      lockPath: join(sessionRoot, "models.lock.json"),
      modelRoot: join(sessionRoot, "models"),
      runtimeId: "synthetic-worker-runtime",
      enableProbeStageTelemetry: true,
      telemetryFd: 91,
      telemetryProtocol: "caller-controlled",
      telemetryMaxBytes: 999_999,
    } as any);
    expect(telemetryConfig.stageTelemetry).toEqual({
      protocol: "studio-worker-stage-telemetry-v1",
      maxBytes: 4096,
    });
    expect(telemetryConfig.args).not.toContain("--telemetry-fd");
    const flags = new Map<string, string>();
    for (let index = 1; index < config.args.length; index += 2) {
      flags.set(config.args[index]!, config.args[index + 1]!);
    }
    for (const flag of [
      "--ffprobe-path", "--ffmpeg-path", "--whisper-path", "--whisper-model-path",
      "--model-lock-path", "--model-root", "--whisper-binary-artifact-id",
      "--whisper-model-artifact-id", "--openclip-checkpoint-path", "--openclip-artifact-id",
      "--qwen-checkpoint-path", "--qwen-artifact-id", "--mlx-vlm-package-path",
      "--mlx-vlm-artifact-id", "--worker-runtime-root", "--worker-runtime-artifact-id",
      "--worker-runtime-tree-sha256",
    ]) expect(flags.has(flag), flag).toBe(true);
    for (const flag of [
      "--ffprobe-path", "--ffmpeg-path", "--whisper-path", "--whisper-model-path",
      "--model-lock-path", "--model-root", "--openclip-checkpoint-path",
      "--qwen-checkpoint-path", "--mlx-vlm-package-path",
    ]) expect(isAbsolute(flags.get(flag)!)).toBe(true);
    expect(flags.get("--whisper-model-artifact-id")).toBe("whisper.cpp-1.9.2-small-q5_1");
    expect(flags.get("--whisper-model-path")).toBe(
      join(flags.get("--model-root")!, "whisper", "ggml-small-q5_1.bin"),
    );
    expect(flags.get("--mlx-vlm-package-path")).toBe(join(flags.get("--model-root")!, "runtime"));
    expect(flags.get("--worker-runtime-root")).toBe(runtimeRoot);
    expect(flags.get("--worker-runtime-artifact-id")).toBe("synthetic-worker-runtime");
    expect(flags.get("--worker-runtime-tree-sha256")).toBe("9".repeat(64));
    expect(config.allowedMediaRoot).toBe(sessionRoot);
    expect(config.timeoutMs).toBe(330_000);
    expect(config.env).toEqual({
      PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
      HOME: join(sessionRoot, ".studio-model-worker-private", "home"),
      LANG: "en_US.UTF-8",
      LC_ALL: "en_US.UTF-8",
      TMPDIR: join(sessionRoot, ".studio-model-worker-private", "tmp"),
      HF_HUB_OFFLINE: "1",
      TRANSFORMERS_OFFLINE: "1",
    });
    expect((await lstat(config.env.HOME!)).mode & 0o777).toBe(0o700);
    expect((await lstat(config.env.TMPDIR!)).mode & 0o777).toBe(0o700);
    for (const key of [
      "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "HTTP_PROXY", "HTTPS_PROXY",
      "HF_TOKEN", "OPENAI_API_KEY", "SSL_CERT_FILE", "SSL_CERT_DIR",
    ]) expect(config.env).not.toHaveProperty(key);
  });

  it("permits probe-required runtime only for the dedicated harness and fails closed otherwise", async () => {
    const sessionRoot = await realpath(await mkdtemp(join(tmpdir(), "studio-probe-required-")));
    roots.push(sessionRoot);
    const dependencies = {
      resolveRuntime: () => ({
        runtimeRoot: join(sessionRoot, "runtime"),
        launcherPath: join(sessionRoot, "runtime", "bin", "run-offline-worker"),
        launcherIdentity: { device: "1", inode: "2", byteLength: 3, sha256: "5".repeat(64) },
        status: "runtime-installed-probe-required" as const,
        bundle: { id: "synthetic-worker-runtime", treeSha256: "8".repeat(64) },
      }),
      sandboxPath: "/synthetic/sandbox-exec",
      probeSandbox: () => true,
      lockPath: join(sessionRoot, "models.lock.json"),
      modelRoot: join(sessionRoot, "models"),
      runtimeId: "synthetic-worker-runtime",
    };
    expect(() => createDefaultStudioModelWorkerConfig(sessionRoot, dependencies))
      .toThrow(/^studio-formal-worker-runtime-probe-required$/);
    expect(createDefaultStudioModelWorkerConfig(sessionRoot, {
      ...dependencies,
      allowProbeRequired: true,
    }).command).toBe(join(sessionRoot, "runtime", "bin", "run-offline-worker"));

    const worker = createDefaultStudioModelWorker(sessionRoot, dependencies);
    const job = {
      schemaVersion: 1 as const,
      jobId: "blocked-job",
      customerId: "customer-a",
      sessionId: "session-a",
      sourceAssetId: "asset-a",
      sourceDigest: "a".repeat(64),
      sourcePath: join(sessionRoot, "unread-customer-media.mp4"),
      sourceStartMs: 0,
      sourceEndMs: 1_000,
      layer: "vision" as const,
      operationKey: "blocked-operation",
      requestedAt: "2026-09-27T00:00:00.000Z",
    };
    await expect(worker.run(job, AbortSignal.timeout(1_000))).resolves.toMatchObject({
      status: "failed-manual-review",
      observations: [],
      suggestedTags: [],
      failure: { code: "studio-formal-worker-runtime-probe-required" },
      providerMode: "offline-fallback",
    });
  });

  it("fails closed for stable runtime absence but propagates unexpected configuration faults", async () => {
    const sessionRoot = await realpath(await mkdtemp(join(tmpdir(), "studio-runtime-failure-")));
    roots.push(sessionRoot);
    const stable = createDefaultStudioModelWorker(sessionRoot, {
      resolveRuntime: () => { throw new Error("studio-formal-worker-runtime-unavailable"); },
      sandboxPath: "/synthetic/sandbox-exec",
      probeSandbox: () => true,
    });
    const job = {
      schemaVersion: 1 as const,
      jobId: "missing-runtime-job",
      customerId: "customer-a",
      sessionId: "session-a",
      sourceAssetId: "asset-a",
      sourceDigest: "a".repeat(64),
      sourcePath: join(sessionRoot, "unread-customer-media.mp4"),
      sourceStartMs: 0,
      sourceEndMs: 1_000,
      layer: "vision" as const,
      operationKey: "missing-runtime-operation",
      requestedAt: "2026-09-27T00:00:00.000Z",
    };
    await expect(stable.run(job, AbortSignal.timeout(1_000))).resolves.toMatchObject({
      status: "failed-manual-review",
      observations: [],
      failure: { code: "studio-formal-worker-runtime-unavailable" },
    });
    expect(() => createDefaultStudioModelWorker(sessionRoot, {
      resolveRuntime: () => { throw new Error("unexpected-programming-error"); },
      sandboxPath: "/synthetic/sandbox-exec",
      probeSandbox: () => true,
    })).toThrow(/^unexpected-programming-error$/);
    expect(() => createDefaultStudioModelWorkerConfig(sessionRoot, {
      resolveRuntime: () => ({
        runtimeRoot: join(sessionRoot, "runtime"),
        launcherPath: join(sessionRoot, "runtime", "bin", "run-offline-worker"),
        launcherIdentity: { device: "1", inode: "2", byteLength: 3, sha256: "4".repeat(64) },
        status: "runtime-real-probe-succeeded" as const,
        bundle: { id: "synthetic-worker-runtime", treeSha256: "7".repeat(64) },
      }),
      sandboxPath: "/missing/sandbox-exec",
      probeSandbox: () => false,
    })).toThrow(/^studio-formal-worker-sandbox-unavailable$/);
  });

  it("whitelists browser runtime status fields and drops roots, ids, packets, hashes, and URLs", async () => {
    const { api } = await createApi({
      modelWorker: successfulWorker(),
      modelStatusReader: async () => ({
        mode: "offline-fallback",
        localOnly: true,
        degraded: true,
        artifacts: [],
        visionProbe: "not-run: runtime-installed-probe-required",
        workerRuntime: {
          status: "runtime-installed-probe-required",
          root: "/private/model-root",
          path: "/private/launcher",
          id: "private-runtime-id",
          packet: "private-approval-packet",
          treeSha256: "f".repeat(64),
          sourceUrl: "https://private.invalid/runtime",
        },
      } as any),
    });
    const session = await openSession(api, "customer-a");
    const status = await studioPost(api, "/api/studio/models/status", {}, session.token).expect(200);
    expect(status.body.models.workerRuntime).toEqual({
      status: "runtime-installed-probe-required",
    });
    const serialized = JSON.stringify(status.body);
    for (const secret of [
      "/private/model-root", "/private/launcher", "private-runtime-id",
      "private-approval-packet", "f".repeat(64), "https://private.invalid/runtime",
    ]) expect(serialized).not.toContain(secret);
  }, 120_000);

  it("exposes a failed real probe only as a browser-safe readiness state", async () => {
    const { api } = await createApi({
      modelWorker: successfulWorker(),
      modelStatusReader: async () => ({
        mode: "offline-fallback",
        localOnly: true,
        degraded: true,
        artifacts: [],
        visionProbe: "not-run: runtime-real-probe-failed",
        workerRuntime: {
          status: "runtime-real-probe-failed",
          receiptPath: "/private/runtime/probes/receipt.json",
          receiptSha256: "f".repeat(64),
          rawOutput: "private model output",
          sourceUrl: "https://private.invalid/probe",
        },
      } as any),
    });
    const session = await openSession(api, "customer-a");
    const status = await studioPost(api, "/api/studio/models/status", {}, session.token).expect(200);

    expect(status.body.models.workerRuntime).toEqual({ status: "runtime-real-probe-failed" });
    expect(JSON.stringify(status.body)).not.toMatch(
      /\/private\/|f{64}|private model output|https?:\/\//i,
    );
  }, 120_000);

  it("keeps all-ready artifacts degraded when the exact real probe failed", async () => {
    const root = await realpath(await mkdtemp(join(tmpdir(), "studio-model-checker-probe-failed-")));
    roots.push(root);
    const checker = join(root, "checker.mjs");
    await writeFile(checker, `process.stdout.write(JSON.stringify({
      artifacts: [{ id: "qwen", version: "v1", status: "ready" }],
      visionProbe: "not-run: runtime-real-probe-failed",
      workerRuntimes: [{
        status: "runtime-real-probe-failed",
        receiptPath: "/private/probe.json",
        receiptSha256: "${"f".repeat(64)}"
      }]
    }) + "\\n");\n`);
    const status = await createModelCheckerStatusReader({
      checkerPath: checker,
      lockPath: join(root, "unused.lock.json"),
      modelRoot: join(root, "unused-model-root"),
    })();

    expect(status).toEqual({
      mode: "offline-fallback",
      localOnly: true,
      degraded: true,
      artifacts: [{ id: "qwen", version: "v1", status: "ready" }],
      visionProbe: "not-run: runtime-real-probe-failed",
      workerRuntime: { status: "runtime-real-probe-failed" },
    });
    expect(JSON.stringify(status)).not.toMatch(/\/private\/|f{64}/i);
  });

  it("keeps an unrelated unapproved embedding request fail-closed after the formal runtime probe succeeds", async () => {
    const { api } = await createApi();
    const session = await openSession(api, "customer-a");
    const assetId = await firstAsset(api, session.token);
    const status = await studioPost(api, "/api/studio/models/status", {}, session.token).expect(200);
    expect(status.body.models.artifacts.find(
      (item: { id: string }) => item.id === "openclip-3.3.0-vit-b-32",
    )?.status).toBe("unapproved");

    const result = await studioPost(api, "/api/studio/analysis/jobs", {
      assetId,
      layers: ["embedding"],
      operationKey: "default-unapproved-embedding",
    }, session.token).expect(201);
    const receipt = result.body.analysis.receipts[0];
    expect(receipt).toMatchObject({
      layer: "embedding",
      modelName: "openclip-3.3.0-vit-b-32",
      status: "failed-manual-review",
      observations: [],
    });
    expect(receipt.failure.code).toBe("studio-model-artifact-unapproved");
  }, 120_000);

  it("derives exact and near duplicate groups from real embedding receipt vectors and persists keep decisions", async () => {
    const worker = successfulWorker();
    const { api, root } = await createApi({ modelWorker: worker });
    const session = await openSession(api, "customer-a");
    const inventory = await studioPost(api, "/api/studio/inventory", {}, session.token).expect(200);
    const [assetA, assetB] = inventory.body.inventory.items.map((item: Record<string, string>) => item.sourceAssetId);
    for (const [assetId, key] of [[assetA, "a"], [assetB, "b"]] as const) {
      await studioPost(api, "/api/studio/analysis/jobs", {
        assetId,
        layers: ["embedding"],
        operationKey: `embedding-${key}`,
      }, session.token).expect(201);
    }
    const history = await studioPost(api, "/api/studio/analysis/history", {}, session.token).expect(200);
    expect(history.body.history.duplicateGroups).toHaveLength(1);
    expect(history.body.history.duplicateGroups[0]).toMatchObject({
      kind: "near",
      sourceAssetIds: [assetA, assetB].sort(),
    });
    const group = history.body.history.duplicateGroups[0];
    const receiptA = history.body.history.entries
      .flatMap((entry: { receipts: ModelAnalysisReceiptV1[] }) => entry.receipts)
      .find((receipt: ModelAnalysisReceiptV1) => receipt.sourceAssetId === assetA)!;
    const receiptB = history.body.history.entries
      .flatMap((entry: { receipts: ModelAnalysisReceiptV1[] }) => entry.receipts)
      .find((receipt: ModelAnalysisReceiptV1) => receipt.sourceAssetId === assetB)!;
    const body = keepReviewBody(receiptA, group.id, "keep-near-group");
    const first = await studioPost(api, "/api/studio/analysis/review", body, session.token).expect(201);
    const replay = await studioPost(api, "/api/studio/analysis/review", body, session.token).expect(201);
    expect(replay.body).toEqual(first.body);
    const conflict = await studioPost(api, "/api/studio/analysis/review", {
      ...keepReviewBody(receiptB, group.id, "keep-near-group"),
    }, session.token);
    expect(conflict.status).toBe(409);
    expect(conflict.body).toEqual({ error: "studio-analysis-review-operation-key-conflict" });

    const indexFiles = await findFiles(root, "embedding-index.json");
    expect(indexFiles).toHaveLength(1);
    const persisted = JSON.parse(await readFile(indexFiles[0]!, "utf8"));
    expect(persisted.decisions).toHaveLength(1);
    expect(persisted.decisions[0]).toMatchObject({
      operationKey: "keep-near-group",
      groupAssetIds: [assetA, assetB].sort(),
      keepAssetIds: [assetA],
    });
  }, 120_000);

  it("passes only same-source verified transcript and current demand context into vision", async () => {
    const worker = successfulWorker();
    const { api } = await createApi({ modelWorker: worker });
    const session = await openSession(api, "customer-a");
    const assetId = await firstAsset(api, session.token);
    const { demand, assessment } = await createDemandAndMatch(api, session.token, "vision-context");
    await studioPost(api, "/api/studio/analysis/jobs", {
      assetId,
      layers: ["transcript", "vision"],
      operationKey: "vision-with-trusted-context",
    }, session.token).expect(201);

    const calls = worker.run.mock.calls as unknown as Array<[ModelAnalysisJobV1, AbortSignal, Record<string, unknown>]>;
    const [visionJob, , context] = calls.find(([job]) => job.layer === "vision")!;
    expect(context).toMatchObject({
      customerId: session.customerId,
      sessionId: session.sessionId,
      sourceAssetId: assetId,
      sourceDigest: visionJob.sourceDigest,
      sourceStartMs: visionJob.sourceStartMs,
      sourceEndMs: visionJob.sourceEndMs,
      demand: { demandId: demand.id, matchAssessmentId: assessment.id },
    });
    expect(context.transcriptSpans).toEqual([{
      sourceDigest: visionJob.sourceDigest,
      startMs: 1_000,
      endMs: 5_000,
      text: "设计师讲解戒指灵感",
    }]);
  }, 120_000);

  it("shares one completed result for concurrent same-operation starts", async () => {
    const worker = successfulWorker();
    const originalMerge = PrivateContentStudio.prototype.analyzeProviderEvidence;
    let releaseMerge!: () => void;
    const mergeGate = new Promise<void>((resolve) => { releaseMerge = resolve; });
    const mergeSpy = vi.spyOn(PrivateContentStudio.prototype, "analyzeProviderEvidence")
      .mockImplementation(async function (this: PrivateContentStudio, scope, input) {
        await mergeGate;
        return originalMerge.call(this, scope, input);
      });
    const { api } = await createApi({ modelWorker: worker });
    const session = await openSession(api, "customer-a");
    const assetId = await firstAsset(api, session.token);
    const requests = Array.from({ length: 8 }, () => studioPost(api, "/api/studio/analysis/jobs", {
      assetId,
      layers: ["technical"],
      operationKey: "concurrent-analysis",
    }, session.token).then((response) => response));
    await vi.waitFor(() => expect(mergeSpy).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 25));
    releaseMerge();
    const responses = await Promise.all(requests);
    expect(responses.every((response) => response.status === 201)).toBe(true);
    expect(responses.map((response) => response.body.analysis)).toEqual(
      Array(8).fill(responses[0]!.body.analysis),
    );
    const history = await studioPost(api, "/api/studio/analysis/history", {}, session.token).expect(200);
    expect(history.body.history.entries).toHaveLength(1);
    expect(worker.run).toHaveBeenCalledTimes(1);
    expect(mergeSpy).toHaveBeenCalledTimes(1);
    const inventory = await studioPost(api, "/api/studio/inventory", {}, session.token).expect(200);
    expect(inventory.body.materials.filter((card: Record<string, unknown>) =>
      card.sourceAssetId === assetId)).toHaveLength(2);
  }, 120_000);

  it("persists a demand-bound material suitability event instead of disguising a tag review", async () => {
    const worker = successfulWorker();
    const { api, root } = await createApi({ modelWorker: worker });
    const session = await openSession(api, "customer-a");
    const assetId = await firstAsset(api, session.token);
    const { demand, assessment } = await createDemandAndMatch(api, session.token, "suitability");
    const analysis = await studioPost(api, "/api/studio/analysis/jobs", {
      assetId,
      layers: ["vision"],
      operationKey: "suitability-analysis",
    }, session.token).expect(201);
    const receipt = analysis.body.analysis.receipts[0];
    const exact = {
      decision: "suitable",
      demandId: demand.id,
      matchAssessmentId: assessment.id,
      receiptJobId: receipt.jobId,
      sourceAssetId: receipt.sourceAssetId,
      sourceDigest: receipt.sourceDigest,
      sourceStartMs: receipt.sourceStartMs,
      sourceEndMs: receipt.sourceEndMs,
      reason: "人工确认这段素材适合当前需求",
      operationKey: "material-suitability-1",
    };
    const accepted = await studioPost(api, "/api/studio/analysis/suitability", exact, session.token).expect(201);
    const replay = await studioPost(api, "/api/studio/analysis/suitability", exact, session.token).expect(201);
    expect(replay.body).toEqual(accepted.body);
    expect(accepted.body.suitability).toMatchObject({
      decision: "suitable", demandId: demand.id, matchAssessmentId: assessment.id,
      receiptJobId: receipt.jobId, sourceAssetId: assetId,
    });
    for (const [field, value] of [
      ["demandId", "demand-other"],
      ["matchAssessmentId", "match-other"],
      ["receiptJobId", "receipt-other"],
      ["sourceDigest", "0".repeat(64)],
      ["sourceEndMs", receipt.sourceEndMs - 1],
    ] as const) {
      const invalid = await studioPost(api, "/api/studio/analysis/suitability", {
        ...exact, [field]: value, operationKey: `invalid-${field}`,
      }, session.token);
      expect([404, 409]).toContain(invalid.status);
    }
    const other = await openSession(api, "customer-b");
    const crossCustomer = await studioPost(api, "/api/studio/analysis/suitability", {
      ...exact, operationKey: "cross-customer-suitability",
    }, other.token);
    expect(crossCustomer.status).toBe(404);
    const conflict = await studioPost(api, "/api/studio/analysis/suitability", {
      ...exact, decision: "not-suitable", operationKey: exact.operationKey,
    }, session.token);
    expect(conflict.status).toBe(409);
    const history = await studioPost(api, "/api/studio/analysis/history", {}, session.token).expect(200);
    expect(history.body.history.suitabilityReviews).toEqual([accepted.body.suitability]);
    const stateFiles = await findFiles(root, "studio-model-review-state.json");
    expect(stateFiles).toHaveLength(1);
    expect(JSON.parse(await readFile(stateFiles[0]!, "utf8")).suitabilityReviews).toHaveLength(1);
  }, 120_000);

  it("returns transparent ranking learned from three real reviews without accepting browser preferences", async () => {
    const worker = successfulWorker();
    const { api } = await createApi({ modelWorker: worker });
    const session = await openSession(api, "customer-a");
    const inventory = await studioPost(api, "/api/studio/inventory", {}, session.token).expect(200);
    const assetIds = inventory.body.inventory.items.map(
      (item: Record<string, string>) => item.sourceAssetId,
    );
    for (const [index, assetId] of assetIds.slice(0, 3).entries()) {
      const analysis = await studioPost(api, "/api/studio/analysis/jobs", {
        assetId,
        layers: ["vision"],
        operationKey: `preference-analysis-${index}`,
      }, session.token).expect(201);
      const receipt = analysis.body.analysis.receipts[0];
      await studioPost(api, "/api/studio/analysis/review", {
        action: "accept-suggestion",
        receiptJobId: receipt.jobId,
        sourceAssetId: receipt.sourceAssetId,
        sourceDigest: receipt.sourceDigest,
        sourceStartMs: receipt.sourceStartMs,
        sourceEndMs: receipt.sourceEndMs,
        target: "戒指特写",
        correction: null,
        operationKey: `preference-review-${index}`,
      }, session.token).expect(201);
    }
    const { demand } = await createDemandAndMatch(api, session.token, "preference-ranking");

    const ranking = await studioPost(api, "/api/studio/rankings", {
      demandId: demand.id,
    }, session.token).expect(200);
    expect(ranking.body.ranking).toHaveLength(4);
    expect(ranking.body.ranking.filter((row: Record<string, unknown>) =>
      row.preferenceAdjustment === 0.05)).toHaveLength(3);
    expect(ranking.body.ranking.find((row: Record<string, unknown>) =>
      row.sourceAssetId === assetIds[3])).toMatchObject({
      preferenceAdjustment: 0,
      reasons: [],
    });
    expect(ranking.body.ranking[0]).toHaveProperty("baseScore");
    expect(ranking.body.ranking[0]).toHaveProperty("preferenceAdjustment");
    expect(ranking.body.ranking[0]).toHaveProperty("reasons");
    expect(ranking.body.ranking[0]).toMatchObject({
      preferencePolicyVersion: "studio-preference-policy-v1",
    });
    expect(JSON.stringify(ranking.body.ranking)).not.toContain(session.customerId);
    expect(JSON.stringify(ranking.body.ranking)).not.toContain(session.sessionId);

    const forged = await studioPost(api, "/api/studio/rankings", {
      demandId: demand.id,
      preference: "戒指特写",
    }, session.token);
    expect(forged.status).toBe(403);
    await studioPost(api, "/api/studio/preferences", {
      target: "戒指特写",
    }, session.token).expect(404);
  }, 120_000);

  it("persists a review before derived preference and reconciles it after the derived write fails", async () => {
    const { api, root } = await createApi({ modelWorker: successfulWorker() });
    const session = await openSession(api, "customer-a");
    const inventory = await studioPost(api, "/api/studio/inventory", {}, session.token).expect(200);
    const assetIds = inventory.body.inventory.items.slice(0, 3).map(
      (item: Record<string, string>) => item.sourceAssetId,
    );

    for (const [index, assetId] of assetIds.entries()) {
      const analysis = await studioPost(api, "/api/studio/analysis/jobs", {
        assetId,
        layers: ["vision"],
        operationKey: `recovery-analysis-${index}`,
      }, session.token).expect(201);
      const receipt = analysis.body.analysis.receipts[0];
      const body = {
        action: "accept-suggestion",
        receiptJobId: receipt.jobId,
        sourceAssetId: receipt.sourceAssetId,
        sourceDigest: receipt.sourceDigest,
        sourceStartMs: receipt.sourceStartMs,
        sourceEndMs: receipt.sourceEndMs,
        target: "戒指特写",
        correction: null,
        operationKey: `recovery-review-${index}`,
      };
      if (index < 2) {
        await studioPost(api, "/api/studio/analysis/review", body, session.token).expect(201);
        continue;
      }
      const failure = vi.spyOn(StudioPreferenceRanker.prototype, "recordSuggestionReview")
        .mockRejectedValueOnce(new Error("injected-preference-write-failure"));
      await studioPost(api, "/api/studio/analysis/review", body, session.token)
        .expect(400, { error: "injected-preference-write-failure" });
      failure.mockRestore();
    }

    const history = await studioPost(api, "/api/studio/analysis/history", {}, session.token).expect(200);
    expect(history.body.history.reviews).toHaveLength(3);
    const stateFiles = await findFiles(root, "studio-model-review-state.json");
    expect(stateFiles).toHaveLength(1);
    expect(JSON.parse(await readFile(stateFiles[0]!, "utf8")).reviews).toHaveLength(3);

    const { demand } = await createDemandAndMatch(api, session.token, "review-write-recovery");
    const ranking = await studioPost(api, "/api/studio/rankings", {
      demandId: demand.id,
    }, session.token).expect(200);
    expect(ranking.body.ranking.filter((row: Record<string, unknown>) =>
      row.preferenceAdjustment === 0.05)).toHaveLength(3);
  }, 120_000);

  it("does not create a ghost preference when the canonical review commit fails", async () => {
    const { api } = await createApi({ modelWorker: successfulWorker() });
    const session = await openSession(api, "customer-a");
    const inventory = await studioPost(api, "/api/studio/inventory", {}, session.token).expect(200);
    const assetIds = inventory.body.inventory.items.slice(0, 3).map(
      (item: Record<string, string>) => item.sourceAssetId,
    );
    const reviewBodies: Array<Record<string, unknown>> = [];

    for (const [index, assetId] of assetIds.entries()) {
      const analysis = await studioPost(api, "/api/studio/analysis/jobs", {
        assetId,
        layers: ["vision"],
        operationKey: `canonical-failure-analysis-${index}`,
      }, session.token).expect(201);
      const receipt = analysis.body.analysis.receipts[0];
      reviewBodies.push({
        action: "accept-suggestion",
        receiptJobId: receipt.jobId,
        sourceAssetId: receipt.sourceAssetId,
        sourceDigest: receipt.sourceDigest,
        sourceStartMs: receipt.sourceStartMs,
        sourceEndMs: receipt.sourceEndMs,
        target: "戒指特写",
        correction: null,
        operationKey: `canonical-failure-review-${index}`,
      });
    }

    await studioPost(api, "/api/studio/analysis/review", reviewBodies[0]!, session.token).expect(201);
    await studioPost(api, "/api/studio/analysis/review", reviewBodies[1]!, session.token).expect(201);
    const canonicalFailure = vi.spyOn(
      StudioModelRuntime.prototype as unknown as {
        persistReviewState(state: unknown): Promise<void>;
      },
      "persistReviewState",
    ).mockRejectedValueOnce(new Error("injected-canonical-write-failure"));
    await studioPost(api, "/api/studio/analysis/review", reviewBodies[2]!, session.token)
      .expect(400, { error: "injected-canonical-write-failure" });
    canonicalFailure.mockRestore();

    const { demand } = await createDemandAndMatch(api, session.token, "canonical-write-failure");
    const ranking = await studioPost(api, "/api/studio/rankings", {
      demandId: demand.id,
    }, session.token).expect(200);
    expect(ranking.body.ranking.every((row: Record<string, unknown>) =>
      row.preferenceAdjustment === 0)).toBe(true);
  }, 120_000);

  it("removes a derived review when the canonical ledger no longer contains it", async () => {
    const { api, root } = await createApi({ modelWorker: successfulWorker() });
    const session = await openSession(api, "customer-a");
    const inventory = await studioPost(api, "/api/studio/inventory", {}, session.token).expect(200);
    const assetIds = inventory.body.inventory.items.slice(0, 3).map(
      (item: Record<string, string>) => item.sourceAssetId,
    );
    for (const [index, assetId] of assetIds.entries()) {
      const analysis = await studioPost(api, "/api/studio/analysis/jobs", {
        assetId,
        layers: ["vision"],
        operationKey: `canonical-delete-analysis-${index}`,
      }, session.token).expect(201);
      const receipt = analysis.body.analysis.receipts[0];
      await studioPost(api, "/api/studio/analysis/review", {
        action: "accept-suggestion",
        receiptJobId: receipt.jobId,
        sourceAssetId: receipt.sourceAssetId,
        sourceDigest: receipt.sourceDigest,
        sourceStartMs: receipt.sourceStartMs,
        sourceEndMs: receipt.sourceEndMs,
        target: "戒指特写",
        correction: null,
        operationKey: `canonical-delete-review-${index}`,
      }, session.token).expect(201);
    }

    const [statePath] = await findFiles(root, "studio-model-review-state.json");
    const state = JSON.parse(await readFile(statePath!, "utf8"));
    state.reviews = state.reviews.slice(0, 2);
    await writeFile(statePath!, `${JSON.stringify(state, null, 2)}\n`);

    const { demand } = await createDemandAndMatch(api, session.token, "canonical-delete");
    const ranking = await studioPost(api, "/api/studio/rankings", {
      demandId: demand.id,
    }, session.token).expect(200);
    expect(ranking.body.ranking.every((row: Record<string, unknown>) =>
      row.preferenceAdjustment === 0)).toBe(true);
  }, 120_000);

  it("learns only after three real demand-bound material-suitability decisions", async () => {
    const { api, root } = await createApi({ modelWorker: successfulWorker() });
    const session = await openSession(api, "customer-a");
    const inventory = await studioPost(api, "/api/studio/inventory", {}, session.token).expect(200);
    const assetIds = inventory.body.inventory.items.slice(0, 3).map(
      (item: Record<string, string>) => item.sourceAssetId,
    );
    const receipts: ModelAnalysisReceiptV1[] = [];
    for (const [index, assetId] of assetIds.entries()) {
      const analysis = await studioPost(api, "/api/studio/analysis/jobs", {
        assetId,
        layers: ["vision"],
        operationKey: `suitability-ranking-analysis-${index}`,
      }, session.token).expect(201);
      receipts.push(analysis.body.analysis.receipts[0]);
    }
    const { demand, assessment } = await createDemandAndMatch(
      api,
      session.token,
      "suitability-ranking",
    );
    for (const [index, receipt] of receipts.entries()) {
      const body = {
        decision: "suitable",
        demandId: demand.id,
        matchAssessmentId: assessment.id,
        receiptJobId: receipt.jobId,
        sourceAssetId: receipt.sourceAssetId,
        sourceDigest: receipt.sourceDigest,
        sourceStartMs: receipt.sourceStartMs,
        sourceEndMs: receipt.sourceEndMs,
        reason: "人工确认这段素材适合当前需求",
        operationKey: `suitability-ranking-${index}`,
      };
      if (index < 2) {
        await studioPost(api, "/api/studio/analysis/suitability", body, session.token).expect(201);
        continue;
      }
      const failure = vi.spyOn(StudioPreferenceRanker.prototype, "recordMaterialSuitability")
        .mockRejectedValueOnce(new Error("injected-preference-write-failure"));
      await studioPost(api, "/api/studio/analysis/suitability", body, session.token)
        .expect(400, { error: "injected-preference-write-failure" });
      failure.mockRestore();
    }

    const ranking = await studioPost(api, "/api/studio/rankings", {
      demandId: demand.id,
    }, session.token).expect(200);
    expect(ranking.body.ranking.filter((row: Record<string, unknown>) =>
      row.preferenceAdjustment === 0.05)).toHaveLength(3);
    expect(ranking.body.ranking.filter((row: Record<string, unknown>) =>
      row.preferenceAdjustment === 0.05).every((row: Record<string, unknown>) =>
      (row.reasons as string[]).includes(
        "approved-preference:material-suitability:single-product",
      ))).toBe(true);

    const [statePath] = await findFiles(root, "studio-model-review-state.json");
    const state = JSON.parse(await readFile(statePath!, "utf8"));
    state.suitabilityReviews = state.suitabilityReviews.slice(0, 2);
    await writeFile(statePath!, `${JSON.stringify(state, null, 2)}\n`);
    const afterCanonicalRemoval = await studioPost(api, "/api/studio/rankings", {
      demandId: demand.id,
    }, session.token).expect(200);
    expect(afterCanonicalRemoval.body.ranking.every((row: Record<string, unknown>) =>
      row.preferenceAdjustment === 0)).toBe(true);
  }, 120_000);
});

async function createApi(overrides: Partial<StudioServerOptions> = {}) {
  const root = await mkdtemp(join(tmpdir(), "studio-model-api-"));
  roots.push(root);
  const server = createStudioServer({
    rootDir: root,
    customers: [
      { id: "customer-a", label: "Customer A" },
      { id: "customer-b", label: "Customer B" },
    ],
    now: () => "2026-09-12T12:00:00.000Z",
    ...overrides,
  });
  return { api: request(server), root };
}

async function openSession(api: ReturnType<typeof request>, customerId: string) {
  const response = await api.post("/api/studio/session")
    .set("host", "127.0.0.1")
    .set("origin", ORIGIN)
    .send({ customerId })
    .expect(201);
  return response.body.session as {
    token: string;
    actorId: string;
    customerId: string;
    sessionId: string;
  };
}

async function firstAsset(api: ReturnType<typeof request>, token: string): Promise<string> {
  const response = await studioPost(api, "/api/studio/inventory", {}, token).expect(200);
  return response.body.inventory.items[0].sourceAssetId as string;
}

function studioPost(
  api: ReturnType<typeof request>,
  path: string,
  body: Record<string, unknown>,
  token: string,
) {
  return api.post(path)
    .set("host", "127.0.0.1")
    .set("origin", ORIGIN)
    .set("authorization", `Bearer ${token}`)
    .send(body);
}

function successfulWorker() {
  return {
    run: vi.fn(async (job: ModelAnalysisJobV1) => receiptFor(job)),
    health: vi.fn(async () => ({
      mode: "offline-fallback" as const,
      installed: false as const,
      modelName: null,
      checkpointDigest: null,
    })),
  } satisfies StudioModelWorker;
}

function editorialTranscriptWorker() {
  return {
    run: vi.fn(async (job: ModelAnalysisJobV1): Promise<ModelAnalysisReceiptV1> => ({
      ...receiptFor(job),
      adapterName: "whisper.cpp-local",
      modelName: "whisper.cpp-small-q5_1",
      modelVersion: "1.9.2-small-q5_1",
      checkpointDigest: "c".repeat(64),
      promptVersion: "studio-transcript-zh-v1",
      qualitySignals: {
        detectedLanguage: "zh",
        spans: [
          { startMs: 1_000, endMs: 3_000, text: "现在展示制作过程" },
          { startMs: 4_000, endMs: 6_500, text: "重点是自动完成剪辑" },
          { startMs: 7_000, endMs: 9_000, text: "最后检查成片结果" },
        ],
      },
      synthetic: false,
      providerMode: "local",
      inputBinding: {
        sourceDigest: job.sourceDigest,
        sourceStartMs: job.sourceStartMs,
        sourceEndMs: job.sourceEndMs,
      },
      artifactBinding: {
        adapter: { id: "formal-worker-runtime", version: "fixture-v1", sha256: "d".repeat(64) },
        model: { id: "whisper.cpp-small-q5_1", version: "1.9.2-small-q5_1", sha256: "c".repeat(64) },
      },
      runtimeMetrics: { runtimeMs: 100 },
      speechState: "speech",
    })),
    health: vi.fn(async () => ({
      mode: "local" as const,
      installed: true as const,
      modelName: "whisper.cpp-small-q5_1",
      checkpointDigest: "c".repeat(64),
    })),
  } satisfies StudioModelWorker;
}

async function createDemandAndMatch(api: ReturnType<typeof request>, token: string, key: string) {
  const created = await studioPost(api, "/api/studio/demands", {
    contentType: "single-product",
    goal: "讲清楚戒指设计与细节",
    targetAudience: "珠宝顾客",
    desiredEffect: "清楚可信",
    mustInclude: ["戒指特写"],
    mustAvoid: ["客户身份"],
    submittedBy: "customer-self-declared",
    operationKey: `demand-${key}`,
  }, token).expect(201);
  const matched = await studioPost(api, "/api/studio/matches", {
    demandId: created.body.demand.id,
    operationKey: `match-${key}`,
  }, token).expect(201);
  return { demand: created.body.demand, assessment: matched.body.assessment };
}

function keepReviewBody(receipt: ModelAnalysisReceiptV1, target: string, operationKey: string) {
  return {
    action: "keep-from-duplicate-group",
    receiptJobId: receipt.jobId,
    sourceAssetId: receipt.sourceAssetId,
    sourceDigest: receipt.sourceDigest,
    sourceStartMs: receipt.sourceStartMs,
    sourceEndMs: receipt.sourceEndMs,
    target,
    correction: null,
    operationKey,
  };
}

async function findFiles(root: string, fileName: string): Promise<string[]> {
  const results: string[] = [];
  for (const entry of await readdir(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) results.push(...await findFiles(path, fileName));
    else if (entry.name === fileName) results.push(path);
  }
  return results;
}

function receiptFor(
  job: ModelAnalysisJobV1,
  status: Extract<StudioAnalysisStatus, "succeeded" | "failed-retryable" | "failed-manual-review"> = "succeeded",
): ModelAnalysisReceiptV1 {
  const { requestedAt: _requestedAt, ...binding } = job;
  const qualitySignals = job.layer === "technical"
    ? { durationMs: job.sourceEndMs, hasVideo: true, hasAudio: true, width: 1080, height: 1920 }
    : job.layer === "transcript"
      ? { detectedLanguage: "zh", spans: [{ startMs: 1_000, endMs: 5_000, text: "设计师讲解戒指灵感" }] }
      : job.layer === "embedding"
        ? {
            embedding: [1, 0],
            dimension: 2,
            representativeFrames: [{
              sourceDigest: job.sourceDigest,
              startMs: job.sourceStartMs,
              endMs: job.sourceEndMs,
              representativeFrameMs: job.sourceStartMs + Math.floor((job.sourceEndMs - job.sourceStartMs) / 2),
              representativeFrameDigest: "f".repeat(64),
            }],
            artifact: {
              id: "embedding-local-model",
              version: "fixture-v1",
              sha256: "c".repeat(64),
            },
          }
        : { uncertainty: ["材质未经人工确认"], demandEvidenceCandidates: ["产品近景"] };
  return {
    ...binding,
    adapterName: `${job.layer}-fixture-adapter`,
    modelName: `${job.layer}-local-model`,
    modelVersion: "fixture-v1",
    checkpointDigest: job.layer === "technical" ? null : "c".repeat(64),
    inputDigest: job.sourceDigest,
    promptVersion: `${job.layer}-prompt-v1`,
    observations: job.layer === "vision" ? ["画面可能包含戒指"] : [],
    suggestedTags: job.layer === "vision" ? ["戒指特写"] : [],
    qualitySignals,
    confidence: status === "succeeded" ? 0.78 : 0,
    warnings: [],
    startedAt: "2026-09-12T12:00:00.000Z",
    completedAt: "2026-09-12T12:00:00.100Z",
    runtimeMs: 100,
    status,
    failure: status === "succeeded" ? null : { code: `fixture-${status}` },
    synthetic: true,
    providerMode: "offline-fallback",
  };
}
