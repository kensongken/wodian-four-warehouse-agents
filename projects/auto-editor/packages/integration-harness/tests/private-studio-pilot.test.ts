import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import * as privateStudioPilotModule from "../src/private-studio-pilot";

const {
  assertPilotDraftTraceability,
  runPrivateStudioPilot,
} = privateStudioPilotModule;

describe("private content studio synthetic pilot", () => {
  let tempRoot: string;

  beforeEach(async () => {
    tempRoot = await mkdtemp(join(tmpdir(), "private-studio-pilot-"));
  });

  afterEach(async () => {
    await rm(tempRoot, { recursive: true, force: true });
  });

  it("proves the phase 0—2 loop with real synthetic media and immutable evidence", async () => {
    const report = await runPrivateStudioPilot(tempRoot);

    expect(report).toMatchObject({
      syntheticOnly: true,
      automaticPublishing: false,
      fixtureDescription: "纯色合成商品夹具",
      sourceClipCount: 4,
      demandVersions: 1,
      matchVerdict: "ready",
      briefVersions: 2,
      briefVersionNumbers: [1, 2],
      draftCount: 4,
      customerFeedbackCount: 2,
      revisionProposalStatuses: ["executed"],
      adoptedDraftVersion: 2,
      outputGate: {
        filesAfterV1Render: 2,
        filesAfterPartialFeedback: 2,
        filesAfterProposal: 2,
        filesAfterInternalConfirmation: 2,
        v2CreatedBeforeInternalConfirmation: false,
      },
      negativeCases: {
        partialMatchBlocked: true,
        blockedMatchBlocked: true,
        captureGapConfirmationBlocked: true,
        crossCustomerBlocked: true,
        staleDemandBlocked: true,
        withdrawalBlocked: true,
        exactReplay: true,
        duplicateIdempotent: true,
        contentHashDeduplicated: true,
      },
    });
    expect(report.journey).toMatchObject({
      inventoryItemCount: 4,
      opportunityCount: 1,
      firstDraftCount: 2,
      requestChangesRecorded: true,
      revisedDraftCount: 2,
    });
    expect(report.demand).toMatchObject({ version: 1, status: "current" });
    expect(report.matchAssessment).toMatchObject({ verdict: "ready" });
    expect(report.customerFeedback).toEqual([
      expect.objectContaining({
        draftId: report.drafts[0]!.id,
        satisfaction: "partially-satisfied",
        comment: "请让设计师观点更突出",
      }),
      expect.objectContaining({
        draftId: report.drafts[2]!.id,
        satisfaction: "satisfied",
      }),
    ]);
    expect(report.revisionProposals).toEqual([
      expect.objectContaining({
        feedbackId: report.customerFeedback[0]!.id,
        status: "executed",
      }),
    ]);
    expect(report.adoptionEvidence.event).toMatchObject({
      action: "adopt",
      actorId: "synthetic-operator-a",
      publishClaimed: false,
    });
    expect(report.adoptionEvidence.adoptedDraft).toMatchObject({
      status: "adopted",
      publishClaimed: false,
    });
    expect(report.adoptionEvidence.event.draftId)
      .toBe(report.adoptionEvidence.adoptedDraft.id);
    expect(report.adoptionEvidence.adoptedDraft.briefId).toBe(report.briefs[1]!.id);
    expect(report.adoptionEvidence.briefVersion).toBe(2);
    const createDraftsReturnSnapshot = report.drafts.map((draft) => ({
      draftId: draft.id,
      status: draft.status,
    }));
    expect(createDraftsReturnSnapshot.every((draft) => draft.status === "reviewable")).toBe(true);
    expect(report.initialRenderSnapshot).toStrictEqual({
      stage: "at-render-time",
      draftStatuses: createDraftsReturnSnapshot,
    });
    expect(Object.isFrozen(report.initialRenderSnapshot)).toBe(true);
    expect(Object.isFrozen(report.initialRenderSnapshot.draftStatuses)).toBe(true);
    expect(report.initialRenderSnapshot.draftStatuses.every(Object.isFrozen)).toBe(true);
    expect(report.finalPermissionSnapshot).toMatchObject({
      stage: "after-withdrawal",
      withdrawnAssetId: report.sourceAssets[0]!.id,
    });
    expect(report.finalPermissionSnapshot.assets).toEqual([
      {
        assetId: report.sourceAssets[0]!.id,
        viewPermission: "withdrawn",
        usePermission: "withdrawn",
      },
      ...report.sourceAssets.slice(1).map((asset) => ({
        assetId: asset.id,
        viewPermission: "allowed" as const,
        usePermission: "allowed" as const,
      })),
    ]);
    expect(report.dependentDraftFinalStatuses).toEqual(report.drafts.map((draft) => ({
      draftId: draft.id,
      initialRenderStatus: "reviewable",
      finalStatus: "invalidated",
    })));
    expect(report.briefs.map((brief) => brief.version)).toEqual([1, 2]);
    expect(report.drafts).toHaveLength(4);
    expect(new Set(report.drafts.map((draft) => draft.id))).toHaveLength(4);
    expect(new Set(report.drafts.map((draft) => draft.blueprintDigest))).toHaveLength(4);
    expect(report.drafts.every((draft) =>
      draft.publishClaimed === false &&
      draft.sourceMap.length === 3 &&
      draft.sourceMap.every((row) => row.sourceAssetId && row.sourceDigest && row.reason),
    )).toBe(true);

    expect(report.mediaProbes).toHaveLength(8);
    expect(report.mediaProbes.every((probe) =>
      probe.videoCodec === "h264" &&
      probe.width > 0 &&
      probe.height > 0 &&
      probe.videoStreamCount === 1 &&
      probe.audioStreamCount === 1 &&
      probe.durationSeconds > 0 &&
      /^[a-f0-9]{64}$/.test(probe.digest) &&
      probe.permission.view === "allowed" &&
      probe.permission.use === "allowed" &&
      probe.sourceAssetIds.length > 0,
    )).toBe(true);
    expect(report.mediaProbes.filter((probe) => probe.kind === "draft").every((probe) =>
      probe.width === 720 && probe.height === 1280 &&
      probe.durationSeconds >= 30 && probe.durationSeconds <= 60 &&
      probe.audioCodec === "aac",
    )).toBe(true);
    expect(report.revisionEffect).toMatchObject({
      feedbackText: "请让设计师观点更突出",
      interpreter: "offline-rule-v2",
      adjustments: ["increase-opening-shot-share"],
    });
    expect(report.revisionEffect.variants).toEqual([
      expect.objectContaining({
        direction: "chronological-evidence-first",
        targetRole: "designer-viewpoint-open",
        applicability: "not-applicable",
        timelineChanged: false,
        outputDigestChanged: false,
      }),
      expect.objectContaining({
        direction: "designer-viewpoint-first",
        targetRole: "designer-viewpoint-open",
        applicability: "applied",
        timelineChanged: true,
        outputDigestChanged: true,
      }),
    ]);
    expect(report.contentDuplicateImport).toMatchObject({
      originalRequestId: "pilot-import-1",
      duplicateRequestId: "pilot-content-duplicate-1",
      deduplicated: true,
    });

    const traceInput = {
      draft: report.drafts[0]!,
      blueprint: report.blueprints[0]!,
      sourceAssets: report.sourceAssets,
      materialCards: report.materialCards,
    };
    expect(() => assertPilotDraftTraceability(traceInput)).not.toThrow();
    expect(() => assertPilotDraftTraceability({
      ...traceInput,
      draft: { ...traceInput.draft, sourceMap: [] },
    })).toThrow("pilot-source-map-empty");
    expect(() => assertPilotDraftTraceability({
      ...traceInput,
      draft: {
        ...traceInput.draft,
        sourceMap: traceInput.draft.sourceMap.map((row, index) =>
          index === 0 ? { ...row, shotId: "wrong-shot" } : row),
      },
    })).toThrow("pilot-source-map-shot-mismatch");
    const wrongDigestBlueprint = {
      ...traceInput.blueprint,
      shots: traceInput.blueprint.shots.map((shot, index) =>
        index === 0 ? { ...shot, sourceDigest: "f".repeat(64) } : shot),
    };
    const wrongDigestDraft = {
      ...traceInput.draft,
      blueprintDigest: digest(wrongDigestBlueprint),
      sourceMap: traceInput.draft.sourceMap.map((row, index) =>
        index === 0 ? { ...row, sourceDigest: "f".repeat(64) } : row),
    };
    expect(() => assertPilotDraftTraceability({
      ...traceInput,
      draft: wrongDigestDraft,
      blueprint: wrongDigestBlueprint,
    })).toThrow("pilot-source-digest-mismatch");
    const firstCard = traceInput.materialCards.find((card) =>
      card.sourceAssetId === traceInput.draft.sourceMap[0]!.sourceAssetId)!;
    const wrongRangeBlueprint = {
      ...traceInput.blueprint,
      durationSeconds: traceInput.blueprint.durationSeconds + 0.001,
      shots: traceInput.blueprint.shots.map((shot, index) =>
        index === 0
          ? { ...shot, sourceEndMs: firstCard.sourceEndMs + 1 }
          : { ...shot, timelineStartMs: shot.timelineStartMs + 1 }),
    };
    const wrongRangeDraft = {
      ...traceInput.draft,
      durationSeconds: traceInput.draft.durationSeconds + 0.001,
      blueprintDigest: digest(wrongRangeBlueprint),
      sourceMap: traceInput.draft.sourceMap.map((row, index) =>
        index === 0 ? { ...row, sourceEndMs: firstCard.sourceEndMs + 1 } : row),
    };
    expect(() => assertPilotDraftTraceability({
      ...traceInput,
      draft: wrongRangeDraft,
      blueprint: wrongRangeBlueprint,
    })).toThrow("pilot-source-range-invalid");
    const wrongTimelineBlueprint = {
      ...traceInput.blueprint,
      shots: traceInput.blueprint.shots.map((shot, index) =>
        index === 1 ? { ...shot, timelineStartMs: shot.timelineStartMs + 1 } : shot),
    };
    expect(() => assertPilotDraftTraceability({
      ...traceInput,
      draft: {
        ...traceInput.draft,
        blueprintDigest: digest(wrongTimelineBlueprint),
      },
      blueprint: wrongTimelineBlueprint,
    })).toThrow("pilot-blueprint-timeline-invalid");
    expect(() => assertPilotDraftTraceability({
      ...traceInput,
      sourceAssets: traceInput.sourceAssets.map((asset, index) =>
        index === 0 ? { ...asset, usePermission: "blocked" as const } : asset),
    })).toThrow("pilot-source-permission-not-allowed");

    const persisted = JSON.parse(await readFile(report.reportPath, "utf8"));
    expect(persisted.draftCount).toBe(4);
    expect(persisted.customerFeedbackCount).toBe(2);
    expect(persisted.revisionProposalStatuses).toEqual(["executed"]);
    expect(persisted.negativeCases).toMatchObject({
      partialMatchBlocked: true,
      blockedMatchBlocked: true,
      captureGapConfirmationBlocked: true,
      crossCustomerBlocked: true,
      staleDemandBlocked: true,
      withdrawalBlocked: true,
      exactReplay: true,
    });
    expect(persisted.adoptionEvidence.event.id).toBe(report.adoptionEvidence.event.id);
    expect(persisted.initialRenderSnapshot).toStrictEqual(report.initialRenderSnapshot);
    expect(persisted.finalPermissionSnapshot.stage).toBe("after-withdrawal");
    expect(persisted.dependentDraftFinalStatuses).toEqual(report.dependentDraftFinalStatuses);
    expect((await stat(report.reportPath)).isFile()).toBe(true);
    expect((await stat(report.sourceMapsPath)).isFile()).toBe(true);
    expect((await stat(report.ffprobePath)).isFile()).toBe(true);
    expect((await stat(report.negativeCasesPath)).isFile()).toBe(true);
    expect((await stat(report.historyPath)).isFile()).toBe(true);
    expect((await stat(report.matchAssessmentPath)).isFile()).toBe(true);
    expect((await stat(report.revisionProposalPath)).isFile()).toBe(true);
  }, 180_000);

  it("fails closed instead of snapshotting a non-reviewable createDrafts result", () => {
    const captureInitialRenderDraftStatuses = (
      privateStudioPilotModule as unknown as {
        captureInitialRenderDraftStatuses: (
          drafts: readonly { id: string; status: string }[],
        ) => unknown;
      }
    ).captureInitialRenderDraftStatuses;

    expect(() => captureInitialRenderDraftStatuses([
      { id: "draft-not-reviewable", status: "adopted" },
    ])).toThrow("pilot-initial-render-status-not-reviewable");
  });
});

function digest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}
