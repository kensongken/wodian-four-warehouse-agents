import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";

import type {
  ContentDemand,
  CustomerDraftFeedback,
  DraftPackage,
  EditBlueprint,
  FeedbackEvent,
  MatchAssessment,
  MaterialCard,
  RevisionProposal,
  StudioCreativeBrief,
  StudioMediaAsset,
} from "@wearable/contracts";
import {
  CustomerMaterialLibrary,
  OfflineFixtureUnderstandingAdapter,
  PrivateContentStudio,
  interpretOfflineStudioFeedback,
  type OfflineStudioFeedbackAdjustment,
  type StudioScope,
} from "@wearable/mobile-agent";
import ffmpegPath from "ffmpeg-static";

const bundledFfprobePath = (
  createRequire(import.meta.url)("ffprobe-static") as { path?: unknown }
).path;

const PRIMARY_SCOPE: StudioScope = Object.freeze({
  actorId: "synthetic-operator-a",
  customerId: "synthetic-customer-a",
  sessionId: "synthetic-session-a",
});
const OTHER_SCOPE: StudioScope = Object.freeze({
  actorId: "synthetic-operator-b",
  customerId: "synthetic-customer-b",
  sessionId: "synthetic-session-b",
});

export type PrivateStudioMediaProbe = Readonly<{
  kind: "source" | "draft";
  path: string;
  videoCodec: string;
  audioCodec: string | null;
  width: number;
  height: number;
  videoStreamCount: number;
  audioStreamCount: number;
  durationSeconds: number;
  digest: string;
  sourceAssetIds: string[];
  permission: { view: "allowed"; use: "allowed" };
}>;

export type PrivateStudioPilotReport = Readonly<{
  syntheticOnly: true;
  automaticPublishing: false;
  fixtureDescription: "纯色合成商品夹具";
  sourceClipCount: 4;
  demandVersions: number;
  matchVerdict: MatchAssessment["verdict"];
  briefVersions: number;
  briefVersionNumbers: number[];
  draftCount: number;
  customerFeedbackCount: number;
  revisionProposalStatuses: RevisionProposal["status"][];
  adoptedDraftVersion: number;
  capability: { mode: "offline-fallback"; label: "离线/模拟分析" };
  demand: ContentDemand;
  matchAssessment: MatchAssessment;
  customerFeedback: CustomerDraftFeedback[];
  revisionProposals: RevisionProposal[];
  outputGate: {
    filesAfterV1Render: number;
    filesAfterPartialFeedback: number;
    filesAfterProposal: number;
    filesAfterInternalConfirmation: number;
    v2CreatedBeforeInternalConfirmation: boolean;
  };
  journey: {
    inventoryItemCount: number;
    opportunityCount: number;
    firstDraftCount: number;
    requestChangesRecorded: boolean;
    revisedDraftCount: number;
  };
  adoptionEvidence: {
    event: FeedbackEvent;
    adoptedDraft: DraftPackage;
    briefVersion: number;
  };
  initialRenderSnapshot: {
    stage: "at-render-time";
    draftStatuses: ReadonlyArray<Readonly<{ draftId: string; status: "reviewable" }>>;
  };
  finalPermissionSnapshot: {
    stage: "after-withdrawal";
    withdrawnAssetId: string;
    assets: Array<{
      assetId: string;
      viewPermission: "allowed" | "withdrawn";
      usePermission: "allowed" | "withdrawn";
    }>;
  };
  dependentDraftFinalStatuses: Array<{
    draftId: string;
    initialRenderStatus: "reviewable";
    finalStatus: DraftPackage["status"];
  }>;
  briefs: StudioCreativeBrief[];
  drafts: DraftPackage[];
  blueprints: EditBlueprint[];
  sourceAssets: StudioMediaAsset[];
  materialCards: MaterialCard[];
  mediaProbes: PrivateStudioMediaProbe[];
  duplicateImports: Array<{ requestId: string; firstAssetId: string; retryAssetId: string }>;
  contentDuplicateImport: {
    originalRequestId: "pilot-import-1";
    duplicateRequestId: "pilot-content-duplicate-1";
    originalAssetId: string;
    duplicateAssetId: string;
    deduplicated: true;
  };
  revisionEffect: {
    feedbackText: string;
    interpreter: "offline-rule-v2";
    adjustments: OfflineStudioFeedbackAdjustment[];
    variants: Array<{
      direction: string;
      targetRole: "designer-viewpoint-open" | "finished-product-close";
      applicability: "applied" | "not-applicable";
      v1Timeline: Array<{
        sourceAssetId: string;
        sourceStartMs: number;
        sourceEndMs: number;
        timelineStartMs: number;
      }>;
      v2Timeline: Array<{
        sourceAssetId: string;
        sourceStartMs: number;
        sourceEndMs: number;
        timelineStartMs: number;
      }>;
      timelineChanged: boolean;
      v1OutputDigest: string;
      v2OutputDigest: string;
      outputDigestChanged: boolean;
    }>;
  };
  negativeCases: {
    partialMatchBlocked: boolean;
    blockedMatchBlocked: boolean;
    captureGapConfirmationBlocked: boolean;
    crossCustomerBlocked: boolean;
    staleDemandBlocked: boolean;
    withdrawalBlocked: boolean;
    exactReplay: boolean;
    duplicateIdempotent: boolean;
    contentHashDeduplicated: boolean;
  };
  negativeCaseCodes: {
    partialMatch: "match-not-ready";
    blockedMatch: "match-not-ready";
    captureGapConfirmation: "revision-proposal-has-unresolved-gaps";
    crossCustomer: "asset-not-found";
    staleDemand: "demand-version-not-current";
    withdrawal: "draft-source-withdrawn";
  };
  exactReplayOperations: string[];
  reportPath: string;
  sourceMapsPath: string;
  ffprobePath: string;
  negativeCasesPath: string;
  historyPath: string;
  matchAssessmentPath: string;
  revisionProposalPath: string;
}>;

export function captureInitialRenderDraftStatuses(
  drafts: readonly Pick<DraftPackage, "id" | "status">[],
): ReadonlyArray<Readonly<{ draftId: string; status: "reviewable" }>> {
  return Object.freeze(drafts.map((draft) => {
    if (draft.status !== "reviewable") {
      throw new Error("pilot-initial-render-status-not-reviewable");
    }
    return Object.freeze({ draftId: draft.id, status: draft.status });
  }));
}

/** Runs the complete phase 0—2 loop using generated local media only. */
export async function runPrivateStudioPilot(workDirValue: string): Promise<PrivateStudioPilotReport> {
  const workDir = resolve(workDirValue);
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  if (typeof bundledFfprobePath !== "string") throw new Error("ffprobe-binary-unavailable");

  const evidenceRoot = join(workDir, "evidence");
  const sourceRoot = join(evidenceRoot, "media", "sources");
  const draftRoot = join(evidenceRoot, "media", "drafts");
  const library = new CustomerMaterialLibrary(join(workDir, "library"), ({ actorId, customerId }) =>
    (actorId === PRIMARY_SCOPE.actorId && customerId === PRIMARY_SCOPE.customerId) ||
    (actorId === OTHER_SCOPE.actorId && customerId === OTHER_SCOPE.customerId));
  const studio = new PrivateContentStudio(
    library,
    new OfflineFixtureUnderstandingAdapter(),
    { now: () => "2026-09-10T08:00:00.000Z", outputRoot: draftRoot },
  );
  await mkdir(sourceRoot, { recursive: true });
  await mkdir(draftRoot, { recursive: true });

  const definitions = [
    ["0x6d3b2d", 392],
    ["0x326a63", 440],
    ["0x876942", 494],
    ["0x425c78", 523],
  ] as const;
  const assets: StudioMediaAsset[] = [];
  const materialCards: MaterialCard[] = [];
  const sourcePaths = new Map<string, string>();
  const duplicateImports: Array<{ requestId: string; firstAssetId: string; retryAssetId: string }> = [];
  for (const [index, [color, frequency]] of definitions.entries()) {
    const sourcePath = join(sourceRoot, `synthetic-color-tone-fixture-${index + 1}.mp4`);
    await makeSyntheticClip(sourcePath, color, frequency);
    const bytes = await readFile(sourcePath);
    const requestId = `pilot-import-${index + 1}`;
    const input = {
      requestId,
      bytes,
      mediaKind: "video" as const,
      fileName: `synthetic-color-tone-fixture-${index + 1}.mp4`,
      capturedAt: `2026-09-10T08:00:0${index}.000Z`,
      sessionId: PRIMARY_SCOPE.sessionId,
      viewPermission: "allowed" as const,
      usePermission: "allowed" as const,
    };
    const asset = await library.importOriginal(PRIMARY_SCOPE.actorId, PRIMARY_SCOPE.customerId, input);
    const retry = await library.importOriginal(PRIMARY_SCOPE.actorId, PRIMARY_SCOPE.customerId, input);
    assets.push(asset);
    sourcePaths.set(asset.id, sourcePath);
    duplicateImports.push({ requestId, firstAssetId: asset.id, retryAssetId: retry.id });
    materialCards.push(await studio.analyze(PRIMARY_SCOPE, asset.id, {
      synthetic: true,
      contentType: "single-product",
      segment: { startMs: 0, endMs: 10_500 },
      ffprobe: {
        durationSeconds: 10.5,
        hasVideo: true,
        hasAudio: true,
        width: 180,
        height: 320,
      },
      observations: [`纯色合成商品夹具 ${index + 1}`],
      merchandiseFacts: ["example-attribute: 示例属性字段待人工确认"],
    }));
  }

  const firstSourceBytes = await readFile(sourcePaths.get(assets[0]!.id)!);
  const contentDuplicate = await library.importOriginal(
    PRIMARY_SCOPE.actorId,
    PRIMARY_SCOPE.customerId,
    {
      requestId: "pilot-content-duplicate-1",
      bytes: firstSourceBytes,
      mediaKind: "video",
      fileName: "synthetic-color-tone-fixture-1.mp4",
      capturedAt: "2026-09-10T08:00:00.000Z",
      sessionId: PRIMARY_SCOPE.sessionId,
      viewPermission: "allowed",
      usePermission: "allowed",
    },
  );
  const contentHashDeduplicated = contentDuplicate.id === assets[0]!.id;

  const inventory = await studio.inventory(PRIMARY_SCOPE);
  const demand = await studio.createDemand(PRIMARY_SCOPE, {
    contentType: "single-product",
    goal: "用本地合成旧素材讲清楚纯色合成商品夹具",
    targetAudience: "本地合成测试观众",
    desiredEffect: "理解示例流程",
    mustInclude: [],
    mustAvoid: [],
    submittedBy: "operator-on-behalf",
    operationKey: "pilot-demand-v1",
  });
  const demandReplay = await studio.createDemand(PRIMARY_SCOPE, {
    contentType: "single-product",
    goal: "用本地合成旧素材讲清楚纯色合成商品夹具",
    targetAudience: "本地合成测试观众",
    desiredEffect: "理解示例流程",
    mustInclude: [],
    mustAvoid: [],
    submittedBy: "operator-on-behalf",
    operationKey: "pilot-demand-v1",
  });
  const assessment = await studio.assessDemand(PRIMARY_SCOPE, demand.id, "pilot-match-v1");
  const assessmentReplay = await studio.assessDemand(PRIMARY_SCOPE, demand.id, "pilot-match-v1");
  const discovery = await studio.discoverOpportunities(PRIMARY_SCOPE, {
    demandId: demand.id,
    matchAssessmentId: assessment.id,
  });
  const opportunity = discovery.opportunities[0];
  if (!opportunity) throw new Error("pilot-opportunity-missing");
  const briefV1 = await studio.confirmBrief(PRIMARY_SCOPE, {
    opportunityId: opportunity.id,
    matchAssessmentId: assessment.id,
    selectedAssetIds: assets.map((asset) => asset.id),
    confirmedFactIds: ["example-attribute: 示例属性字段待人工确认"],
    reviewRequirements: ["采用前人工复核"],
    operationKey: "pilot-brief-v1",
  });
  const briefV1Replay = await studio.confirmBrief(PRIMARY_SCOPE, {
    opportunityId: opportunity.id,
    matchAssessmentId: assessment.id,
    selectedAssetIds: assets.map((asset) => asset.id),
    confirmedFactIds: ["example-attribute: 示例属性字段待人工确认"],
    reviewRequirements: ["采用前人工复核"],
    operationKey: "pilot-brief-v1",
  });
  const draftsV1 = await studio.createDrafts(PRIMARY_SCOPE, briefV1.id, "pilot-drafts-v1");
  const draftsV1Replay = await studio.createDrafts(PRIMARY_SCOPE, briefV1.id, "pilot-drafts-v1");
  const initialDraftStatusesV1 = captureInitialRenderDraftStatuses(draftsV1);
  const blueprintsV1 = await Promise.all(draftsV1.map((draft) =>
    studio.getBlueprint(PRIMARY_SCOPE, draft.blueprintId),
  ));
  const filesAfterV1Render = await countMp4Files(draftRoot);
  const feedbackText = "请让设计师观点更突出";
  const partialFeedbackInput = {
    draftId: draftsV1[0]!.id,
    satisfaction: "partially-satisfied" as const,
    issueCategories: ["style" as const],
    comment: feedbackText,
    mustKeep: [],
    mustRemove: [],
    needsReshoot: [],
    submittedBy: "customer-self-declared" as const,
    operationKey: "pilot-customer-feedback-v1",
  };
  const partialFeedback = await studio.recordCustomerFeedback(PRIMARY_SCOPE, partialFeedbackInput);
  const partialFeedbackReplay = await studio.recordCustomerFeedback(PRIMARY_SCOPE, partialFeedbackInput);
  const filesAfterPartialFeedback = await countMp4Files(draftRoot);
  const revisionProposal = await studio.proposeRevision(
    PRIMARY_SCOPE,
    partialFeedback.id,
    "pilot-revision-proposal-v1",
  );
  const revisionProposalReplay = await studio.proposeRevision(
    PRIMARY_SCOPE,
    partialFeedback.id,
    "pilot-revision-proposal-v1",
  );
  const filesAfterProposal = await countMp4Files(draftRoot);
  const briefV2 = await studio.confirmRevisionProposal(
    PRIMARY_SCOPE,
    revisionProposal.id,
    "pilot-confirm-revision-v1",
  );
  const briefV2Replay = await studio.confirmRevisionProposal(
    PRIMARY_SCOPE,
    revisionProposal.id,
    "pilot-confirm-revision-v1",
  );
  const filesAfterInternalConfirmation = await countMp4Files(draftRoot);
  const draftsV2 = await studio.createDrafts(PRIMARY_SCOPE, briefV2.id, "pilot-drafts-v2");
  const draftsV2Replay = await studio.createDrafts(PRIMARY_SCOPE, briefV2.id, "pilot-drafts-v2");
  const initialDraftStatusesV2 = captureInitialRenderDraftStatuses(draftsV2);
  const blueprintsV2 = await Promise.all(draftsV2.map((draft) =>
    studio.getBlueprint(PRIMARY_SCOPE, draft.blueprintId),
  ));
  const satisfiedFeedbackInput = {
    draftId: draftsV2[0]!.id,
    satisfaction: "satisfied" as const,
    issueCategories: [],
    comment: null,
    mustKeep: [],
    mustRemove: [],
    needsReshoot: [],
    submittedBy: "customer-self-declared" as const,
    operationKey: "pilot-customer-feedback-v2",
  };
  const satisfiedFeedback = await studio.recordCustomerFeedback(PRIMARY_SCOPE, satisfiedFeedbackInput);
  const satisfiedFeedbackReplay = await studio.recordCustomerFeedback(PRIMARY_SCOPE, satisfiedFeedbackInput);
  const adoption = await studio.adoptDraft(PRIMARY_SCOPE, draftsV2[0]!.id, "pilot-adopt-v2");
  const adoptionReplay = await studio.adoptDraft(PRIMARY_SCOPE, draftsV2[0]!.id, "pilot-adopt-v2");
  const drafts = [...draftsV1, ...draftsV2];
  const blueprints = [...blueprintsV1, ...blueprintsV2];
  const adoptedDraft = await studio.getDraft(PRIMARY_SCOPE, adoption.draftId);
  const history = await studio.listStudioHistory(PRIMARY_SCOPE);
  const exactReplayOperations = [
    ["create-demand", demandReplay, demand],
    ["assess-demand", assessmentReplay, assessment],
    ["confirm-brief-v1", briefV1Replay, briefV1],
    ["render-v1", draftsV1Replay, draftsV1],
    ["partial-feedback", partialFeedbackReplay, partialFeedback],
    ["revision-proposal", revisionProposalReplay, revisionProposal],
    ["confirm-revision", briefV2Replay, briefV2],
    ["render-v2", draftsV2Replay, draftsV2],
    ["satisfied-feedback", satisfiedFeedbackReplay, satisfiedFeedback],
    ["adopt-v2", adoptionReplay, adoption],
  ].map(([label, replay, original]) => {
    if (JSON.stringify(replay) !== JSON.stringify(original)) {
      throw new Error(`pilot-exact-replay-mismatch:${label}`);
    }
    return label as string;
  });
  if (filesAfterV1Render !== 2 || filesAfterPartialFeedback !== filesAfterV1Render ||
      filesAfterProposal !== filesAfterV1Render ||
      filesAfterInternalConfirmation !== filesAfterV1Render) {
    throw new Error("pilot-output-created-before-confirmed-v2-render");
  }
  const initialRenderSnapshot: PrivateStudioPilotReport["initialRenderSnapshot"] = Object.freeze({
    stage: "at-render-time",
    draftStatuses: Object.freeze([...initialDraftStatusesV1, ...initialDraftStatusesV2]),
  });

  const mediaProbes: PrivateStudioMediaProbe[] = [];
  for (const asset of assets) {
    mediaProbes.push(await inspectMedia(
      "source",
      sourcePaths.get(asset.id)!,
      [asset.id],
      { view: asset.viewPermission as "allowed", use: asset.usePermission as "allowed" },
      bundledFfprobePath,
      asset.digest,
    ));
  }
  const assetById = new Map(assets.map((asset) => [asset.id, asset]));
  for (const draft of drafts) {
    const blueprint = blueprints.find((candidate) => candidate.id === draft.blueprintId);
    if (!blueprint) throw new Error("pilot-blueprint-missing");
    assertPilotDraftTraceability({ draft, blueprint, sourceAssets: assets, materialCards });
    const permission = draft.sourceMap.every((row) => {
      const asset = assetById.get(row.sourceAssetId);
      return asset?.viewPermission === "allowed" && asset.usePermission === "allowed";
    });
    if (!permission) throw new Error("pilot-draft-permission-not-allowed");
    mediaProbes.push(await inspectMedia(
      "draft",
      draft.outputPath,
      draft.sourceMap.map((row) => row.sourceAssetId),
      { view: "allowed", use: "allowed" },
      bundledFfprobePath,
      draft.outputDigest,
    ));
  }

  const crossCustomerBlocked = await rejectsWith(
    () => library.readOriginal(OTHER_SCOPE.actorId, OTHER_SCOPE.customerId, assets[0]!.id),
    "asset-not-found",
  );
  const demandNegativeCases = await proveDemandNegativeCases(
    join(workDir, "negative-cases"),
    [...sourcePaths.values()],
  );
  const duplicateDrafts = await studio.createDrafts(PRIMARY_SCOPE, briefV2.id, "pilot-drafts-v2");
  const duplicateIdempotent =
    duplicateImports.every((item) => item.firstAssetId === item.retryAssetId) &&
    duplicateDrafts.map((draft) => draft.id).join("\0") === draftsV2.map((draft) => draft.id).join("\0");
  await library.withdraw(
    PRIMARY_SCOPE.actorId,
    PRIMARY_SCOPE.customerId,
    assets[0]!.id,
    "pilot-withdraw-source-1",
  );
  const [visibleAfterWithdrawal, inventoryAfterWithdrawal, finalDrafts] = await Promise.all([
    library.listAssets(PRIMARY_SCOPE.actorId, PRIMARY_SCOPE.customerId),
    library.inventoryAt(PRIMARY_SCOPE.actorId, PRIMARY_SCOPE.customerId, "2026-09-10T08:00:00.000Z"),
    Promise.all(drafts.map((draft) => studio.getDraft(PRIMARY_SCOPE, draft.id))),
  ]);
  const visibleAssetIds = new Set(visibleAfterWithdrawal.map((asset) => asset.id));
  const usableAssetIds = new Set(inventoryAfterWithdrawal.map((card) => card.sourceAssetId));
  const initialRenderStatusByDraftId = new Map(initialRenderSnapshot.draftStatuses.map((draft) =>
    [draft.draftId, draft.status] as const));
  const finalPermissionSnapshot: PrivateStudioPilotReport["finalPermissionSnapshot"] = {
    stage: "after-withdrawal",
    withdrawnAssetId: assets[0]!.id,
    assets: assets.map((asset) => ({
      assetId: asset.id,
      viewPermission: visibleAssetIds.has(asset.id) ? "allowed" : "withdrawn",
      usePermission: usableAssetIds.has(asset.id) ? "allowed" : "withdrawn",
    })),
  };
  const dependentDraftFinalStatuses: PrivateStudioPilotReport["dependentDraftFinalStatuses"] =
    finalDrafts.map((draft) => {
      const initialRenderStatus = initialRenderStatusByDraftId.get(draft.id);
      if (!initialRenderStatus) throw new Error("pilot-initial-render-snapshot-missing");
      return {
        draftId: draft.id,
        initialRenderStatus,
        finalStatus: draft.status,
      };
    });
  const withdrawalBlocked = await rejectsWith(
    () => studio.adoptDraft(PRIMARY_SCOPE, draftsV2[0]!.id, "pilot-adopt-after-withdrawal"),
    "draft-source-withdrawn",
  );
  if (!crossCustomerBlocked || !withdrawalBlocked || !duplicateIdempotent || !contentHashDeduplicated ||
      !demandNegativeCases.partialMatchBlocked || !demandNegativeCases.blockedMatchBlocked ||
      !demandNegativeCases.captureGapConfirmationBlocked || !demandNegativeCases.staleDemandBlocked) {
    throw new Error(`pilot-negative-case-missing:${JSON.stringify({
      ...demandNegativeCases,
      crossCustomerBlocked,
      withdrawalBlocked,
      duplicateIdempotent,
      contentHashDeduplicated,
    })}`);
  }
  if (dependentDraftFinalStatuses.some((draft) => draft.finalStatus !== "invalidated")) {
    throw new Error("pilot-dependent-draft-not-invalidated");
  }
  if (history.demands.length !== 1 || history.assessments.length !== 1 ||
      history.briefs.length !== 2 || history.drafts.length !== 4 ||
      history.customerFeedback.length !== 2 || history.revisionProposals.length !== 1 ||
      history.revisionProposals[0]?.status !== "executed" || adoptedDraft.version !== 2) {
    throw new Error("pilot-history-lineage-incomplete");
  }

  const reportPath = join(evidenceRoot, "private-studio-pilot-report.json");
  const sourceMapsPath = join(evidenceRoot, "source-maps.json");
  const ffprobeReportPath = join(evidenceRoot, "ffprobe-report.json");
  const negativeCasesPath = join(evidenceRoot, "negative-cases.json");
  const historyPath = join(evidenceRoot, "immutable-history.json");
  const matchAssessmentPath = join(evidenceRoot, "match-assessment.json");
  const revisionProposalPath = join(evidenceRoot, "revision-proposal.json");
  const report: PrivateStudioPilotReport = {
    syntheticOnly: true,
    automaticPublishing: false,
    fixtureDescription: "纯色合成商品夹具",
    sourceClipCount: 4,
    demandVersions: history.demands.length,
    matchVerdict: assessment.verdict,
    briefVersions: history.briefs.length,
    briefVersionNumbers: history.briefs.map((brief) => brief.version),
    draftCount: history.drafts.length,
    customerFeedbackCount: history.customerFeedback.length,
    revisionProposalStatuses: history.revisionProposals.map((proposal) => proposal.status),
    adoptedDraftVersion: adoptedDraft.version,
    capability: { mode: "offline-fallback", label: "离线/模拟分析" },
    demand,
    matchAssessment: assessment,
    customerFeedback: [partialFeedback, satisfiedFeedback],
    revisionProposals: [...history.revisionProposals],
    outputGate: {
      filesAfterV1Render,
      filesAfterPartialFeedback,
      filesAfterProposal,
      filesAfterInternalConfirmation,
      v2CreatedBeforeInternalConfirmation: filesAfterInternalConfirmation > filesAfterV1Render,
    },
    journey: {
      inventoryItemCount: inventory.items.length,
      opportunityCount: discovery.opportunities.length,
      firstDraftCount: draftsV1.length,
      requestChangesRecorded: partialFeedback.satisfaction === "partially-satisfied",
      revisedDraftCount: draftsV2.length,
    },
    adoptionEvidence: {
      event: adoption,
      adoptedDraft,
      briefVersion: briefV2.version,
    },
    initialRenderSnapshot,
    finalPermissionSnapshot,
    dependentDraftFinalStatuses,
    briefs: [briefV1, briefV2],
    drafts,
    blueprints,
    sourceAssets: assets,
    materialCards,
    mediaProbes,
    duplicateImports,
    contentDuplicateImport: {
      originalRequestId: "pilot-import-1",
      duplicateRequestId: "pilot-content-duplicate-1",
      originalAssetId: assets[0]!.id,
      duplicateAssetId: contentDuplicate.id,
      deduplicated: true,
    },
    revisionEffect: makeRevisionEffect(
      feedbackText,
      blueprintsV1,
      blueprintsV2,
      draftsV1,
      draftsV2,
    ),
    negativeCases: {
      ...demandNegativeCases,
      crossCustomerBlocked,
      withdrawalBlocked,
      exactReplay: exactReplayOperations.length === 10,
      duplicateIdempotent,
      contentHashDeduplicated,
    },
    negativeCaseCodes: {
      partialMatch: "match-not-ready",
      blockedMatch: "match-not-ready",
      captureGapConfirmation: "revision-proposal-has-unresolved-gaps",
      crossCustomer: "asset-not-found",
      staleDemand: "demand-version-not-current",
      withdrawal: "draft-source-withdrawn",
    },
    exactReplayOperations,
    reportPath,
    sourceMapsPath,
    ffprobePath: ffprobeReportPath,
    negativeCasesPath,
    historyPath,
    matchAssessmentPath,
    revisionProposalPath,
  };
  await Promise.all([
    writeJson(sourceMapsPath, drafts.map((draft) => ({
      draftId: draft.id,
      briefId: draft.briefId,
      blueprintDigest: draft.blueprintDigest,
      sourceMap: draft.sourceMap,
    }))),
    writeJson(ffprobeReportPath, mediaProbes),
    writeJson(negativeCasesPath, {
      results: report.negativeCases,
      codes: report.negativeCaseCodes,
      exactReplayOperations,
    }),
    writeJson(historyPath, history),
    writeJson(matchAssessmentPath, assessment),
    writeJson(revisionProposalPath, history.revisionProposals[0]),
  ]);
  await writeJson(reportPath, report);
  return report;
}

async function countMp4Files(root: string): Promise<number> {
  const entries = await readdir(root, { recursive: true });
  return entries.filter((entry) => String(entry).endsWith(".mp4")).length;
}

async function proveDemandNegativeCases(
  root: string,
  sourcePaths: readonly string[],
): Promise<Readonly<{
  partialMatchBlocked: boolean;
  blockedMatchBlocked: boolean;
  captureGapConfirmationBlocked: boolean;
  staleDemandBlocked: boolean;
}>> {
  const partialScope: StudioScope = {
    actorId: "synthetic-negative-operator",
    customerId: "synthetic-negative-customer",
    sessionId: "synthetic-partial-session",
  };
  const partialSetup = await buildNegativeStudio(
    join(root, "partial"),
    partialScope,
    sourcePaths.slice(0, 2),
  );
  const partialDemand = await partialSetup.studio.createDemand(partialScope, {
    contentType: "single-product",
    goal: "部分素材不足以生成两版初稿",
    targetAudience: "本地合成测试观众",
    desiredEffect: "证明必须拒绝继续",
    mustInclude: [],
    mustAvoid: [],
    submittedBy: "operator-on-behalf",
    operationKey: "negative-partial-demand",
  });
  const partialAssessment = await partialSetup.studio.assessDemand(
    partialScope,
    partialDemand.id,
    "negative-partial-match",
  );
  if (partialAssessment.verdict !== "partial") throw new Error("pilot-partial-verdict-missing");
  const partialMatchBlocked = await rejectsWith(
    () => partialSetup.studio.discoverOpportunities(partialScope, {
      demandId: partialDemand.id,
      matchAssessmentId: partialAssessment.id,
    }),
    "match-not-ready",
  );

  const blockedScope: StudioScope = {
    ...partialScope,
    sessionId: "synthetic-blocked-session",
  };
  const blockedSetup = await buildNegativeStudio(join(root, "blocked"), blockedScope, []);
  const blockedDemand = await blockedSetup.studio.createDemand(blockedScope, {
    contentType: "single-product",
    goal: "无素材时不允许继续",
    targetAudience: "本地合成测试观众",
    desiredEffect: "证明完全阻断",
    mustInclude: [],
    mustAvoid: [],
    submittedBy: "operator-on-behalf",
    operationKey: "negative-blocked-demand-v1",
  });
  const blockedAssessment = await blockedSetup.studio.assessDemand(
    blockedScope,
    blockedDemand.id,
    "negative-blocked-match-v1",
  );
  if (blockedAssessment.verdict !== "blocked") throw new Error("pilot-blocked-verdict-missing");
  const blockedMatchBlocked = await rejectsWith(
    () => blockedSetup.studio.discoverOpportunities(blockedScope, {
      demandId: blockedDemand.id,
      matchAssessmentId: blockedAssessment.id,
    }),
    "match-not-ready",
  );
  await blockedSetup.studio.createDemand(blockedScope, {
    contentType: "single-product",
    goal: "新版需求使旧需求失效",
    targetAudience: "本地合成测试观众",
    desiredEffect: "证明 stale 门禁",
    mustInclude: [],
    mustAvoid: [],
    submittedBy: "operator-on-behalf",
    operationKey: "negative-blocked-demand-v2",
  });
  const staleDemandBlocked = await rejectsWith(
    () => blockedSetup.studio.assessDemand(
      blockedScope,
      blockedDemand.id,
      "negative-blocked-match-v1",
    ),
    "demand-version-not-current",
  );

  const captureScope: StudioScope = {
    ...partialScope,
    sessionId: "synthetic-capture-gap-session",
  };
  const captureSetup = await buildNegativeStudio(
    join(root, "capture-gap"),
    captureScope,
    sourcePaths,
  );
  const captureDemand = await captureSetup.studio.createDemand(captureScope, {
    contentType: "single-product",
    goal: "补拍缺口必须人工解决",
    targetAudience: "本地合成测试观众",
    desiredEffect: "证明提案确认阻断",
    mustInclude: [],
    mustAvoid: [],
    submittedBy: "operator-on-behalf",
    operationKey: "negative-capture-demand",
  });
  const captureAssessment = await captureSetup.studio.assessDemand(
    captureScope,
    captureDemand.id,
    "negative-capture-match",
  );
  const captureDiscovery = await captureSetup.studio.discoverOpportunities(captureScope, {
    demandId: captureDemand.id,
    matchAssessmentId: captureAssessment.id,
  });
  const captureBrief = await captureSetup.studio.confirmBrief(captureScope, {
    opportunityId: captureDiscovery.opportunities[0]!.id,
    matchAssessmentId: captureAssessment.id,
    selectedAssetIds: captureSetup.assets.map((asset) => asset.id),
    confirmedFactIds: [],
    reviewRequirements: ["采用前人工复核"],
    operationKey: "negative-capture-brief",
  });
  const captureDrafts = await captureSetup.studio.createDrafts(
    captureScope,
    captureBrief.id,
    "negative-capture-drafts",
  );
  const captureFeedback = await captureSetup.studio.recordCustomerFeedback(captureScope, {
    draftId: captureDrafts[0]!.id,
    satisfaction: "partially-satisfied",
    issueCategories: ["shot-selection"],
    comment: "需要补拍设计师口述",
    mustKeep: [],
    mustRemove: [],
    needsReshoot: ["补拍设计师口述"],
    submittedBy: "customer-self-declared",
    operationKey: "negative-capture-feedback",
  });
  const captureProposal = await captureSetup.studio.proposeRevision(
    captureScope,
    captureFeedback.id,
    "negative-capture-proposal",
  );
  if (captureProposal.captureGaps.length === 0) throw new Error("pilot-capture-gap-missing");
  const captureGapConfirmationBlocked = await rejectsWith(
    () => captureSetup.studio.confirmRevisionProposal(
      captureScope,
      captureProposal.id,
      "negative-capture-confirmation",
    ),
    "revision-proposal-has-unresolved-gaps",
  );

  return {
    partialMatchBlocked,
    blockedMatchBlocked,
    captureGapConfirmationBlocked,
    staleDemandBlocked,
  };
}

async function buildNegativeStudio(
  root: string,
  scope: StudioScope,
  sourcePaths: readonly string[],
): Promise<Readonly<{ studio: PrivateContentStudio; assets: StudioMediaAsset[] }>> {
  const library = new CustomerMaterialLibrary(join(root, "library"), ({ actorId, customerId }) =>
    actorId === scope.actorId && customerId === scope.customerId);
  const studio = new PrivateContentStudio(
    library,
    new OfflineFixtureUnderstandingAdapter(),
    { now: () => "2026-09-11T08:00:00.000Z", outputRoot: join(root, "drafts") },
  );
  const assets: StudioMediaAsset[] = [];
  for (const [index, sourcePath] of sourcePaths.entries()) {
    const asset = await library.importOriginal(scope.actorId, scope.customerId, {
      requestId: `negative-import-${index + 1}`,
      bytes: await readFile(sourcePath),
      mediaKind: "video",
      fileName: `negative-synthetic-fixture-${index + 1}.mp4`,
      capturedAt: `2026-09-11T08:00:0${index}.000Z`,
      sessionId: scope.sessionId,
      viewPermission: "allowed",
      usePermission: "allowed",
    });
    assets.push(asset);
    await studio.analyze(scope, asset.id, {
      synthetic: true,
      contentType: "single-product",
      segment: { startMs: 0, endMs: 10_500 },
      ffprobe: {
        durationSeconds: 10.5,
        hasVideo: true,
        hasAudio: true,
        width: 180,
        height: 320,
      },
      observations: [`负向门禁合成夹具 ${index + 1}`],
      merchandiseFacts: [],
    });
  }
  return { studio, assets };
}

async function makeSyntheticClip(path: string, color: string, frequency: number): Promise<void> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  await runChecked(ffmpegPath, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", `color=c=${color}:s=180x320:r=25:d=10.5`,
    "-f", "lavfi", "-i", `sine=frequency=${frequency}:sample_rate=44100:duration=10.5`,
    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart", "-y", path,
  ], "pilot-fixture-generation-failed");
}

async function inspectMedia(
  kind: "source" | "draft",
  path: string,
  sourceAssetIds: string[],
  permission: { view: "allowed"; use: "allowed" },
  ffprobe: string,
  expectedDigest: string,
): Promise<PrivateStudioMediaProbe> {
  const { stdout } = await runChecked(ffprobe, [
    "-v", "error",
    "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
    "-of", "json",
    path,
  ], "pilot-ffprobe-failed");
  const parsed = JSON.parse(stdout) as {
    streams?: Array<{ codec_type?: string; codec_name?: string; width?: number; height?: number }>;
    format?: { duration?: string };
  };
  const videoStreams = (parsed.streams ?? []).filter((stream) => stream.codec_type === "video");
  const audioStreams = (parsed.streams ?? []).filter((stream) => stream.codec_type === "audio");
  const video = videoStreams[0];
  const audio = audioStreams[0];
  const durationSeconds = Number(parsed.format?.duration);
  if (!video || video.codec_name !== "h264" || videoStreams.length !== 1 || audioStreams.length !== 1 ||
      audio?.codec_name !== "aac" || !Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    throw new Error("pilot-media-probe-invalid");
  }
  if (kind === "draft" && (durationSeconds < 30 || durationSeconds > 60 ||
      video.width !== 720 || video.height !== 1280)) {
    throw new Error("pilot-draft-probe-invalid");
  }
  const digest = sha256(await readFile(path));
  if (digest !== expectedDigest) throw new Error("pilot-media-digest-mismatch");
  return {
    kind,
    path,
    videoCodec: video.codec_name,
    audioCodec: audio.codec_name ?? null,
    width: Number(video.width),
    height: Number(video.height),
    videoStreamCount: videoStreams.length,
    audioStreamCount: audioStreams.length,
    durationSeconds,
    digest,
    sourceAssetIds: [...sourceAssetIds],
    permission,
  };
}

export function assertPilotDraftTraceability(input: Readonly<{
  draft: DraftPackage;
  blueprint: EditBlueprint;
  sourceAssets: readonly StudioMediaAsset[];
  materialCards: readonly MaterialCard[];
}>): void {
  const { draft, blueprint } = input;
  if (!Array.isArray(draft.sourceMap) || draft.sourceMap.length === 0) {
    throw new Error("pilot-source-map-empty");
  }
  if (draft.blueprintId !== blueprint.id || draft.briefId !== blueprint.briefId ||
      draft.blueprintDigest !== stableDigest(blueprint)) {
    throw new Error("pilot-blueprint-binding-mismatch");
  }
  if (draft.sourceMap.length !== blueprint.shots.length) {
    throw new Error("pilot-source-map-shot-count-mismatch");
  }
  let timelineStartMs = 0;
  for (const shot of blueprint.shots) {
    if (shot.timelineStartMs !== timelineStartMs || shot.sourceEndMs <= shot.sourceStartMs) {
      throw new Error("pilot-blueprint-timeline-invalid");
    }
    timelineStartMs += shot.sourceEndMs - shot.sourceStartMs;
  }
  if (Math.abs(blueprint.durationSeconds - timelineStartMs / 1_000) > 0.001 ||
      Math.abs(draft.durationSeconds - blueprint.durationSeconds) > 0.1) {
    throw new Error("pilot-blueprint-timeline-invalid");
  }
  const assets = new Map(input.sourceAssets.map((asset) => [asset.id, asset]));
  const cards = new Map(input.materialCards.map((card) => [card.sourceAssetId, card]));
  for (const [index, row] of draft.sourceMap.entries()) {
    const shot = blueprint.shots[index];
    if (!shot || row.shotId !== shot.id || row.sourceAssetId !== shot.sourceAssetId ||
        row.sourceDigest !== shot.sourceDigest || row.sourceStartMs !== shot.sourceStartMs ||
        row.sourceEndMs !== shot.sourceEndMs || row.reason !== shot.reason) {
      throw new Error("pilot-source-map-shot-mismatch");
    }
    const asset = assets.get(row.sourceAssetId);
    const card = cards.get(row.sourceAssetId);
    if (!asset || !card) throw new Error("pilot-source-record-missing");
    if (asset.viewPermission !== "allowed" || asset.usePermission !== "allowed") {
      throw new Error("pilot-source-permission-not-allowed");
    }
    if (asset.digest !== row.sourceDigest || card.sourceDigest !== row.sourceDigest) {
      throw new Error("pilot-source-digest-mismatch");
    }
    if (row.sourceStartMs < card.sourceStartMs || row.sourceEndMs > card.sourceEndMs ||
        row.sourceEndMs <= row.sourceStartMs) {
      throw new Error("pilot-source-range-invalid");
    }
  }
}

function makeRevisionEffect(
  feedbackText: string,
  blueprintsV1: readonly EditBlueprint[],
  blueprintsV2: readonly EditBlueprint[],
  draftsV1: readonly DraftPackage[],
  draftsV2: readonly DraftPackage[],
): PrivateStudioPilotReport["revisionEffect"] {
  const interpretation = interpretOfflineStudioFeedback(feedbackText);
  const adjustment = interpretation.adjustments.length === 1
    ? interpretation.adjustments[0]
    : null;
  if (!adjustment) throw new Error("pilot-feedback-adjustment-not-singular");
  const targetRole = adjustment === "increase-opening-shot-share"
    ? "designer-viewpoint-open" as const
    : "finished-product-close" as const;
  const expectedPosition = adjustment === "increase-opening-shot-share" ? "opening" : "ending";
  const timeline = (blueprint: EditBlueprint) => blueprint.shots.map((shot) => ({
    sourceAssetId: shot.sourceAssetId,
    sourceStartMs: shot.sourceStartMs,
    sourceEndMs: shot.sourceEndMs,
    timelineStartMs: shot.timelineStartMs,
  }));
  const variants = [0, 1].map((index) => {
    const v1Timeline = timeline(blueprintsV1[index]!);
    const v2Timeline = timeline(blueprintsV2[index]!);
    const timelineChanged = JSON.stringify(v1Timeline) !== JSON.stringify(v2Timeline);
    const outputDigestChanged = draftsV1[index]!.outputDigest !== draftsV2[index]!.outputDigest;
    const targetIndex = blueprintsV2[index]!.shots.findIndex((shot) => shot.role === targetRole);
    const applicable = expectedPosition === "opening"
      ? targetIndex === 0
      : targetIndex === blueprintsV2[index]!.shots.length - 1;
    if (timelineChanged !== applicable || outputDigestChanged !== applicable) {
      throw new Error("pilot-feedback-role-application-mismatch");
    }
    return {
      direction: blueprintsV2[index]!.direction,
      targetRole,
      applicability: applicable ? "applied" as const : "not-applicable" as const,
      v1Timeline,
      v2Timeline,
      timelineChanged,
      v1OutputDigest: draftsV1[index]!.outputDigest,
      v2OutputDigest: draftsV2[index]!.outputDigest,
      outputDigestChanged,
    };
  });
  return {
    feedbackText,
    interpreter: interpretation.interpreter,
    adjustments: interpretation.adjustments,
    variants,
  };
}

async function rejectsWith(work: () => Promise<unknown>, code: string): Promise<boolean> {
  try {
    await work();
    return false;
  } catch (error) {
    return error instanceof Error && error.message.includes(code);
  }
}

async function writeJson(path: string, value: unknown): Promise<void> {
  await mkdir(resolve(path, ".."), { recursive: true });
  await writeFile(path, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

async function runChecked(
  command: string,
  args: readonly string[],
  errorCode: string,
): Promise<{ stdout: string; stderr: string }> {
  return await new Promise((resolveResult, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += String(chunk); });
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.once("error", reject);
    child.once("close", (code) => code === 0
      ? resolveResult({ stdout, stderr })
      : reject(new Error(`${errorCode}:${stderr.trim()}`)));
  });
}

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function stableDigest(value: unknown): string {
  return sha256(Buffer.from(JSON.stringify(value)));
}
