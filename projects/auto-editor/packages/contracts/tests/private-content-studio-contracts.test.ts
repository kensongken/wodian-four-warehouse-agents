import { describe, expect, it } from "vitest";

import {
  CaptureEnvelopeV1Schema,
  ContentDemandSchema,
  ContentOpportunitySchema,
  CustomerDraftFeedbackSchema,
  DraftPackageSchema,
  EditBlueprintSchema,
  ExternalEditHandoffModeSchema,
  ExternalEditHandoffSchema,
  ExternalEditHandoffStatusSchema,
  ExternalEditPackageSchema,
  ExternalEditReturnCandidateSchema,
  ExternalEditReturnedDraftBindingSchema,
  FeedbackEventSchema,
  GapListSchema,
  MatchAssessmentSchema,
  MaterialCardSchema,
  MaterialInventorySchema,
  RevisionProposalSchema,
  StudioCapabilitySchema,
  StudioCreativeBriefSchema,
  StudioDistributionEventSchema,
  StudioMediaAssetSchema,
  StudioOperationKeySchema,
  StudioPlatformVariantSchema,
} from "../src";

const capability = {
  mode: "offline-fallback" as const,
  label: "离线/模拟分析" as const,
  version: 1,
  provider: null,
};

const validInventory = {
  id: "inventory-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  asOf: "2026-09-09T08:00:00.000Z",
  items: [{
    materialCardId: "card-1",
    sourceAssetId: "asset-a",
    selectionReason: "usable-process-evidence",
  }],
  capability,
};

const validBlueprint = {
  id: "blueprint-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  briefId: "brief-1",
  direction: "process-first",
  durationSeconds: 35,
  shots: [{
    id: "shot-1",
    sourceAssetId: "asset-a",
    sourceDigest: "a".repeat(64),
    sourceStartMs: 0,
    sourceEndMs: 10_000,
    timelineStartMs: 0,
    reason: "show-process-proof",
  }],
  capability,
  publishClaimed: false as const,
  createdAt: "2026-09-09T08:00:00.000Z",
};

describe("private content studio contracts", () => {
  it("validates external-edit status, mode, package, candidate, and parent-only lineage", () => {
    expect(ExternalEditHandoffStatusSchema.options).toEqual([
      "preparing", "ready", "editing", "candidate-found", "returned", "degraded", "failed", "cancelled",
    ]);
    expect(ExternalEditHandoffModeSchema.options).toEqual(["jianying-draft", "file-package"]);
    expect(ExternalEditHandoffSchema.parse(validExternalEditHandoff)).toMatchObject({
      parentDraftVersion: 1,
      operationKey: "external-edit-1",
      publishClaimed: false,
    });
    expect(ExternalEditReturnCandidateSchema.parse(validExternalEditCandidate).relativePath)
      .toBe("exports/final.mp4");
    expect(ExternalEditReturnedDraftBindingSchema.parse(validExternalEditBinding).lineageConfidence)
      .toBe("parent-only");
    expect(ExternalEditReturnedDraftBindingSchema.parse(validExternalEditBinding)).toMatchObject({
      externalEditor: "unverified-external-file",
      declaredEditor: "jianying-desktop",
      editorProvenance: "return-folder-only",
    });
    expect(ExternalEditPackageSchema.parse({
      id: "package-1",
      handoffId: "handoff-1",
      customerId: "customer-a",
      parentDraftId: "draft-1",
      packageDigest: "f".repeat(64),
      files: [{ relativePath: "video/parent.mp4", role: "parent-video", digest: "c".repeat(64), sizeBytes: 2048 }],
      createdAt: "2026-09-30T08:00:00.000Z",
    }).files).toHaveLength(1);
  });

  it("keeps all four platform versions explicit", () => {
    expect(StudioPlatformVariantSchema.options).toEqual([
      "editorial-master", "xiaohongshu", "douyin", "wechat-channels",
    ]);
  });

  it("rejects absolute return paths, fake publishing, and false shot-level lineage", () => {
    expect(() => ExternalEditReturnCandidateSchema.parse({
      ...validExternalEditCandidate,
      relativePath: "/Users/test-user/export.mp4",
    })).toThrow();
    expect(() => ExternalEditReturnCandidateSchema.parse({
      ...validExternalEditCandidate,
      relativePath: "../outside.mp4",
    })).toThrow();
    expect(() => ExternalEditHandoffSchema.parse({
      ...validExternalEditHandoff,
      publishClaimed: true,
    })).toThrow();
    expect(() => ExternalEditReturnedDraftBindingSchema.parse({
      ...validExternalEditBinding,
      lineageConfidence: "shot-level",
    })).toThrow();
    expect(() => ExternalEditReturnedDraftBindingSchema.parse({
      ...validExternalEditBinding,
      editorProvenance: undefined,
    })).toThrow("external-editor-provenance-required");
    expect(() => ExternalEditReturnedDraftBindingSchema.parse({
      origin: "external-editor-return",
      externalEditor: "jianying-desktop",
      parentDraftId: "legacy-draft",
      handoffId: "legacy-handoff",
      lineageConfidence: "parent-only",
    })).not.toThrow();
    expect(() => DraftPackageSchema.parse({
      ...validDraft,
      externalEdit: validExternalEditBinding,
    })).not.toThrow();
  });

  it("accepts one immutable demand-to-revision lineage", () => {
    expect(ContentDemandSchema.parse(validDemand)).toMatchObject({
      version: 1,
      submittedBy: "operator-on-behalf",
      status: "current",
    });
    expect(MatchAssessmentSchema.parse(validMatch)).toMatchObject({
      demandId: validDemand.id,
      verdict: "ready",
    });
    expect(ContentOpportunitySchema.parse(validOpportunity)).toMatchObject({
      demandId: validDemand.id,
      matchAssessmentId: validMatch.id,
    });
    expect(StudioCreativeBriefSchema.parse(validBrief)).toMatchObject({
      demandId: validDemand.id,
      matchAssessmentId: validMatch.id,
      parentBriefId: null,
      feedbackId: null,
      revisionProposalId: null,
    });
    expect(DraftPackageSchema.parse(validDraft)).toMatchObject({
      demandId: validDemand.id,
      revisionProposalId: null,
    });
    expect(CustomerDraftFeedbackSchema.parse(validCustomerFeedback)).toMatchObject({
      satisfaction: "partially-satisfied",
      publishClaimed: false,
    });
    expect(RevisionProposalSchema.parse(validProposal)).toMatchObject({
      status: "proposed",
      confirmedByActorId: null,
    });
  });

  it("binds a vertical share copy to its immutable parent without pretending it was published", () => {
    const shareDraft = DraftPackageSchema.parse({
      ...validDraft,
      id: "draft-share-1",
      outputDigest: "d".repeat(64),
      outputPath: "/synthetic-output/draft-share-1.mp4",
      shareOptimization: {
        parentDraftId: validDraft.id,
        targetWidth: 720,
        targetHeight: 1280,
        mode: "fit-pad-no-crop",
      },
    });

    expect(shareDraft.shareOptimization).toEqual({
      parentDraftId: validDraft.id,
      targetWidth: 720,
      targetHeight: 1280,
      mode: "fit-pad-no-crop",
    });
    expect(shareDraft.publishClaimed).toBe(false);
    expect(() => DraftPackageSchema.parse({
      ...shareDraft,
      shareOptimization: { ...shareDraft.shareOptimization, targetWidth: 1080 },
    })).toThrow();
    expect(() => DraftPackageSchema.parse({
      ...shareDraft,
      shareOptimization: { ...shareDraft.shareOptimization, mode: "crop" },
    })).toThrow();
  });

  it("keeps ordinary DraftPackage source assets unique for existing consumers", () => {
    expect(() => DraftPackageSchema.parse({
      ...validDraft,
      sourceMap: [
        { ...validDraft.sourceMap[0], shotId: "shot-1", sourceStartMs: 0, sourceEndMs: 1_000 },
        { ...validDraft.sourceMap[0], shotId: "shot-2", sourceStartMs: 2_000, sourceEndMs: 3_000 },
      ],
    })).toThrow("duplicate-source-asset-id");
  });

  it("allows traceable segments from one source only when reuse is explicit", () => {
    expect(DraftPackageSchema.parse({
      ...validDraft,
      sourceReuseMode: "same-source-segments",
      sourceMap: [
        { ...validDraft.sourceMap[0], shotId: "shot-1", sourceStartMs: 0, sourceEndMs: 10_000 },
        { ...validDraft.sourceMap[0], shotId: "shot-2", sourceStartMs: 5_000, sourceEndMs: 15_000 },
        { ...validDraft.sourceMap[0], shotId: "shot-3", sourceStartMs: 10_000, sourceEndMs: 20_000 },
      ],
    }).sourceReuseMode).toBe("same-source-segments");
  });

  it("binds every visual-model receipt to the exact draft shot", () => {
    const visualEvidence = {
      status: "used" as const,
      degradedReason: null,
      items: [{
        shotId: "shot-1",
        sourceAssetId: "asset-a",
        sourceDigest: "a".repeat(64),
        sourceStartMs: 0,
        sourceEndMs: 10_000,
        receiptJobId: "vision-job-1",
        provider: {
          layer: "vision" as const,
          adapterName: "mlx-vlm-worker",
          modelName: "qwen3-vl-2b-instruct-4bit",
          modelVersion: "fixture-v1",
          checkpointDigest: "d".repeat(64),
        },
        observations: ["商品主体清晰"],
        selectionReason: "视觉模型识别到商品主体，与镜头用途一致",
      }],
    };
    expect(() => DraftPackageSchema.parse({ ...validDraft, visualEvidence })).not.toThrow();
    expect(() => DraftPackageSchema.parse({
      ...validDraft,
      visualEvidence: {
        ...visualEvidence,
        items: [{ ...visualEvidence.items[0], sourceDigest: "e".repeat(64) }],
      },
    })).toThrow("draft-visual-evidence-source-binding-mismatch");
  });

  it("enforces studio demand, feedback, structured-item, array, and operation-key limits", () => {
    const items = Array.from({ length: 20 }, (_, index) =>
      `${String(index).padStart(2, "0")}${"项".repeat(198)}`);
    expect(() => ContentDemandSchema.parse({
      ...validDemand,
      goal: "g".repeat(500),
      targetAudience: "a".repeat(200),
      desiredEffect: "e".repeat(500),
      mustInclude: items,
      mustAvoid: items.map((item) => item.replace("项", "避")),
    })).not.toThrow();
    expect(() => CustomerDraftFeedbackSchema.parse({
      ...validCustomerFeedback,
      comment: "c".repeat(1000),
      mustKeep: items,
      mustRemove: items.map((item) => item.replace("项", "删")),
      needsReshoot: items.map((item) => item.replace("项", "拍")),
    })).not.toThrow();
    expect(() => StudioOperationKeySchema.parse("k".repeat(200))).not.toThrow();

    for (const demand of [
      { ...validDemand, goal: "g".repeat(501) },
      { ...validDemand, targetAudience: "a".repeat(201) },
      { ...validDemand, desiredEffect: "e".repeat(501) },
      { ...validDemand, mustInclude: Array.from({ length: 21 }, (_, index) => `item-${index}`) },
      { ...validDemand, mustAvoid: ["x".repeat(201)] },
    ]) expect(() => ContentDemandSchema.parse(demand)).toThrow();
    for (const feedback of [
      { ...validCustomerFeedback, comment: "c".repeat(1001) },
      { ...validCustomerFeedback, mustKeep: Array.from({ length: 21 }, (_, index) => `item-${index}`) },
      { ...validCustomerFeedback, mustRemove: ["x".repeat(201)] },
      { ...validCustomerFeedback, needsReshoot: ["x".repeat(201)] },
    ]) expect(() => CustomerDraftFeedbackSchema.parse(feedback)).toThrow();
    expect(() => StudioOperationKeySchema.parse("k".repeat(201))).toThrow();
  });

  it("rejects unsupported enums, duplicate arrays, unbound evidence, and fake publishing", () => {
    expect(() => ContentDemandSchema.parse({
      ...validDemand,
      mustInclude: ["成品", "成品"],
    })).toThrow("duplicate-must-include");
    expect(() => MatchAssessmentSchema.parse({
      ...validMatch,
      matchedEvidence: [{ requirement: "成品", materialCardIds: [], explanation: "有" }],
    })).toThrow();
    expect(() => CustomerDraftFeedbackSchema.parse({
      ...validCustomerFeedback,
      publishClaimed: true,
    })).toThrow();
    expect(() => RevisionProposalSchema.parse({
      ...validProposal,
      status: "confirmed",
      confirmedByActorId: null,
    })).toThrow("proposal-confirmation-state-mismatch");
  });

  it("rejects studio objects without customer ownership", () => {
    expect(() => MaterialInventorySchema.parse(validInventory)).not.toThrow();
    expect(() => MaterialInventorySchema.parse({ ...validInventory, customerId: "" })).toThrow();
  });

  it("requires every blueprint shot to carry immutable source lineage", () => {
    expect(() => EditBlueprintSchema.parse(validBlueprint)).not.toThrow();
    expect(() => EditBlueprintSchema.parse({
      ...validBlueprint,
      shots: [{ ...validBlueprint.shots[0], sourceAssetId: "" }],
    })).toThrow();
    expect(() => EditBlueprintSchema.parse({
      ...validBlueprint,
      shots: [{ ...validBlueprint.shots[0], sourceDigest: "" }],
    })).toThrow();
  });

  it("accepts additive structured editorial fields but rejects unknown enum values", () => {
    expect(() => EditBlueprintSchema.parse(validBlueprint)).not.toThrow();
    expect(() => EditBlueprintSchema.parse({
      ...validBlueprint,
      editorialDirection: "chronological-evidence-first",
      shots: [{ ...validBlueprint.shots[0], role: "process-evidence-open" }],
    })).not.toThrow();
    expect(() => EditBlueprintSchema.parse({
      ...validBlueprint,
      editorialDirection: "alpha",
      shots: [{ ...validBlueprint.shots[0], role: "foo" }],
    })).toThrow();
  });

  it("accepts only bounded transition plans", () => {
    expect(() => EditBlueprintSchema.parse({
      ...validBlueprint,
      transitionPlan: { style: "cut", durationMs: 0 },
    })).not.toThrow();
    expect(() => EditBlueprintSchema.parse({
      ...validBlueprint,
      transitionPlan: { style: "fade-through-black", durationMs: 280 },
    })).not.toThrow();
    expect(() => EditBlueprintSchema.parse({
      ...validBlueprint,
      transitionPlan: { style: "cut", durationMs: 100 },
    })).toThrow();
    expect(() => EditBlueprintSchema.parse({
      ...validBlueprint,
      transitionPlan: { style: "fade-through-black", durationMs: 700 },
    })).toThrow();
  });

  it("rejects duplicate source IDs in immutable inventories and blueprints", () => {
    expect(() => MaterialInventorySchema.parse(validInventory)).not.toThrow();
    expect(() => EditBlueprintSchema.parse(validBlueprint)).not.toThrow();
    expect(() => MaterialInventorySchema.parse({
      ...validInventory,
      items: [validInventory.items[0], { ...validInventory.items[0], materialCardId: "card-2" }],
    })).toThrow("duplicate-source-asset-id");
    expect(() => EditBlueprintSchema.parse({
      ...validBlueprint,
      shots: [validBlueprint.shots[0], { ...validBlueprint.shots[0], id: "shot-2" }],
    })).toThrow("duplicate-source-asset-id");
  });

  it("accepts only positive immutable versions and strict records", () => {
    expect(() => MaterialInventorySchema.parse(validInventory)).not.toThrow();
    expect(() => MaterialInventorySchema.parse({ ...validInventory, version: 0 })).toThrow();
    expect(() => MaterialInventorySchema.parse({ ...validInventory, unexpected: true })).toThrow();
  });

  it("constrains draft and blueprint duration to 6 through 60 seconds", () => {
    expect(() => EditBlueprintSchema.parse(validBlueprint)).not.toThrow();
    expect(() => DraftPackageSchema.parse(validDraft)).not.toThrow();
    expect(() => EditBlueprintSchema.parse({ ...validBlueprint, durationSeconds: 5.9 })).toThrow();
    expect(() => EditBlueprintSchema.parse({ ...validBlueprint, durationSeconds: 6 })).not.toThrow();
    expect(() => EditBlueprintSchema.parse({ ...validBlueprint, durationSeconds: 61 })).toThrow();
    expect(() => DraftPackageSchema.parse({ ...validDraft, durationSeconds: 6 })).not.toThrow();
    expect(() => DraftPackageSchema.parse({ ...validDraft, durationSeconds: 60 })).not.toThrow();
    expect(() => DraftPackageSchema.parse({ ...validDraft, durationSeconds: 60.1 })).toThrow();
  });

  it("forbids studio records from claiming publication", () => {
    expect(() => EditBlueprintSchema.parse(validBlueprint)).not.toThrow();
    expect(() => DraftPackageSchema.parse(validDraft)).not.toThrow();
    expect(() => FeedbackEventSchema.parse(validFeedback)).not.toThrow();
    expect(() => EditBlueprintSchema.parse({ ...validBlueprint, publishClaimed: true })).toThrow();
    expect(() => DraftPackageSchema.parse({ ...validDraft, publishClaimed: true })).toThrow();
    expect(() => FeedbackEventSchema.parse({ ...validFeedback, publishClaimed: true })).toThrow();
    const distributionEvent = {
      id: "distribution-1", customerId: "customer-a", sessionId: "session-1", version: 1,
      draftId: "draft-1", action: "download-started", platformVariant: "xiaohongshu",
      actorId: "actor-a", publishClaimed: false, externalDeliveryClaimed: false,
      createdAt: "2026-09-09T08:00:00.000Z",
    } as const;
    expect(() => StudioDistributionEventSchema.parse(distributionEvent)).not.toThrow();
    expect(() => StudioDistributionEventSchema.parse({ ...distributionEvent, publishClaimed: true })).toThrow();
    expect(() => StudioDistributionEventSchema.parse({ ...distributionEvent, externalDeliveryClaimed: true })).toThrow();
  });

  it("supports only the three phase-zero-to-two content categories", () => {
    expect(() => MaterialCardSchema.parse(validCard)).not.toThrow();
    expect(() => MaterialCardSchema.parse({ ...validCard, contentType: "daily-vlog" })).toThrow();
  });

  it("keeps view and use permission separate", () => {
    expect(() => StudioMediaAssetSchema.parse(validAsset)).not.toThrow();
    expect(() => StudioMediaAssetSchema.parse({
      ...validAsset,
      viewPermission: "allowed",
      usePermission: "blocked",
    })).not.toThrow();
  });

  it("distinguishes the shared source blob from an import authorization binding", () => {
    expect(StudioMediaAssetSchema.parse(validAsset)).toMatchObject({
      id: "asset-a",
      importBindingId: "asset-a",
      blobId: `blob_${"a".repeat(64)}`,
      digest: "a".repeat(64),
      blobDigest: "a".repeat(64),
    });
    expect(() => StudioMediaAssetSchema.parse({
      ...validAsset,
      importBindingId: "another-binding",
    })).toThrow("asset-import-binding-id-mismatch");
    expect(() => StudioMediaAssetSchema.parse({
      ...validAsset,
      blobDigest: "b".repeat(64),
    })).toThrow("asset-blob-digest-mismatch");
    expect(() => StudioMediaAssetSchema.parse({
      ...validAsset,
      blobId: "../../escape",
    })).toThrow();
  });

  it("fails closed on invalid capability claims", () => {
    expect(() => StudioCapabilitySchema.parse(capability)).not.toThrow();
    expect(() => StudioCapabilitySchema.parse({
      ...capability,
      label: "provider analysis",
    })).toThrow("offline-fallback-label-required");
    expect(() => StudioCapabilitySchema.parse({
      ...capability,
      provider: "synthetic-provider",
    })).toThrow("offline-fallback-provider-must-be-null");
    expect(() => StudioCapabilitySchema.parse({
      ...capability,
      mode: "provider",
    })).toThrow("provider-name-required");
  });

  it("defaults an otherwise valid capture envelope to manual permission review", () => {
    expect(CaptureEnvelopeV1Schema.parse(validEnvelope)).toMatchObject({
      viewPermission: "review-required",
      usePermission: "review-required",
    });
  });

  it("accepts the complete phase-zero-to-two contract family", () => {
    expect(() => StudioCapabilitySchema.parse(capability)).not.toThrow();
    expect(() => StudioMediaAssetSchema.parse(validAsset)).not.toThrow();
    expect(() => MaterialCardSchema.parse(validCard)).not.toThrow();
    expect(() => MaterialInventorySchema.parse(validInventory)).not.toThrow();
    expect(() => ContentOpportunitySchema.parse(validOpportunity)).not.toThrow();
    expect(() => StudioCreativeBriefSchema.parse(validBrief)).not.toThrow();
    expect(() => GapListSchema.parse(validGapList)).not.toThrow();
    expect(() => EditBlueprintSchema.parse(validBlueprint)).not.toThrow();
    expect(() => DraftPackageSchema.parse(validDraft)).not.toThrow();
    expect(() => FeedbackEventSchema.parse(validFeedback)).not.toThrow();
    expect(() => CaptureEnvelopeV1Schema.parse(validEnvelope)).not.toThrow();
  });
});

const validAsset = {
  id: "asset-a",
  importBindingId: "asset-a",
  blobId: `blob_${"a".repeat(64)}`,
  customerId: "customer-a",
  version: 1,
  digest: "a".repeat(64),
  blobDigest: "a".repeat(64),
  mediaKind: "video" as const,
  byteLength: 128,
  fileName: "synthetic-process.mp4",
  capturedAt: "2026-09-08T08:00:00.000Z",
  importedAt: "2026-09-09T08:00:00.000Z",
  viewPermission: "allowed" as const,
  usePermission: "allowed" as const,
};

const validCard = {
  id: "card-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  sourceAssetId: "asset-a",
  sourceDigest: "a".repeat(64),
  sourceStartMs: 0,
  sourceEndMs: 10_000,
  contentType: "jewelry-making-process" as const,
  technicalUsable: true,
  technicalFlags: [],
  observations: ["synthetic declaration: polishing"],
  confirmedFacts: [],
  pendingFacts: ["material-name"],
  capability,
  createdAt: "2026-09-09T08:00:00.000Z",
};

const validOpportunity = {
  id: "opportunity-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  demandId: "demand-1",
  matchAssessmentId: "match-1",
  contentType: "jewelry-making-process" as const,
  goal: "show a synthetic polishing process",
  materialCardIds: ["card-1"],
  rationale: "usable-process-evidence",
  capability,
  createdAt: "2026-09-09T08:00:00.000Z",
};

const validBrief = {
  id: "brief-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  demandId: "demand-1",
  matchAssessmentId: "match-1",
  parentBriefId: null,
  feedbackId: null,
  revisionProposalId: null,
  opportunityId: "opportunity-1",
  contentType: "jewelry-making-process" as const,
  goal: "show a synthetic polishing process",
  selectedAssetIds: ["asset-a"],
  confirmedFactIds: ["fact-1"],
  reviewRequirements: ["human-review-before-adoption"],
  status: "confirmed" as const,
  capability,
  createdAt: "2026-09-09T08:00:00.000Z",
};

const validGapList = {
  id: "gaps-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  contentType: "single-product" as const,
  goal: "explain a synthetic ring",
  gaps: [{
    id: "gap-1",
    missingEvidence: "three usable video segments",
    impactWithoutCapture: "cannot create a traceable draft",
    recommendedCapture: "capture one synthetic close-up",
  }],
  capability,
  createdAt: "2026-09-09T08:00:00.000Z",
};

const validDraft = {
  id: "draft-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  demandId: "demand-1",
  revisionProposalId: null,
  briefId: "brief-1",
  blueprintId: "blueprint-1",
  blueprintDigest: "b".repeat(64),
  outputDigest: "c".repeat(64),
  outputPath: "/synthetic-output/draft-1.mp4",
  durationSeconds: 35,
  sourceMap: [{
    shotId: "shot-1",
    sourceAssetId: "asset-a",
    sourceDigest: "a".repeat(64),
    sourceStartMs: 0,
    sourceEndMs: 10_000,
    reason: "show-process-proof",
  }],
  status: "reviewable" as const,
  capability,
  publishClaimed: false as const,
  createdAt: "2026-09-09T08:00:00.000Z",
};

const validExternalEditHandoff = {
  id: "handoff-1",
  customerId: "customer-a",
  sessionId: "session-1",
  parentDraftId: "draft-1",
  parentDraftVersion: 1,
  parentDraftDigest: "c".repeat(64),
  platformVariant: "wechat-channels",
  revisionInstruction: "让开头更自然，保留打磨细节。",
  mustKeep: ["打磨细节"],
  includeSources: true,
  sourceAssetBindings: [{ sourceAssetId: "asset-a", digest: "a".repeat(64) }],
  mode: "file-package" as const,
  adapterId: null,
  adapterVersion: null,
  status: "ready" as const,
  operationKey: "external-edit-1",
  packageDigest: "d".repeat(64),
  publishClaimed: false as const,
  createdAt: "2026-09-30T08:00:00.000Z",
  updatedAt: "2026-09-30T08:00:00.000Z",
};

const validExternalEditCandidate = {
  id: "candidate-1",
  handoffId: "handoff-1",
  parentDraftId: "draft-1",
  relativePath: "exports/final.mp4",
  digest: "e".repeat(64),
  sizeBytes: 1024,
  durationSeconds: 32.5,
  mediaFacts: { width: 1080, height: 1920 },
  stableSince: "2026-09-30T08:00:00.000Z",
  detectedAt: "2026-09-30T08:01:00.000Z",
  status: "pending-review" as const,
};

const validExternalEditBinding = {
  origin: "external-editor-return" as const,
  externalEditor: "unverified-external-file" as const,
  declaredEditor: "jianying-desktop" as const,
  editorProvenance: "return-folder-only" as const,
  parentDraftId: "draft-1",
  handoffId: "handoff-1",
  lineageConfidence: "parent-only" as const,
};

const validFeedback = {
  id: "feedback-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  draftId: "draft-1",
  action: "request-changes" as const,
  reason: "designer viewpoint should appear first",
  actorId: "actor-a",
  publishClaimed: false as const,
  createdAt: "2026-09-09T08:00:00.000Z",
};

const validEnvelope = {
  id: "capture-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  requestId: "capture-request-1",
  mediaKind: "video" as const,
  fileName: "synthetic-capture.mp4",
  bytes: new Uint8Array([1, 2, 3]),
  binding: {
    customerId: "customer-a",
    sessionId: "session-1",
    verified: true as const,
  },
  adapter: {
    name: "synthetic-phone-adapter",
    version: "1.0.0",
  },
  capturedAt: "2026-09-09T07:59:00.000Z",
};

const validDemand = {
  id: "demand-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  contentType: "jewelry-making-process" as const,
  goal: "show a synthetic polishing process",
  targetAudience: "existing-customer",
  desiredEffect: "understand-craftsmanship",
  mustInclude: ["成品"],
  mustAvoid: ["未确认材质声称"],
  submittedBy: "operator-on-behalf" as const,
  status: "current" as const,
  createdAt: "2026-09-09T08:00:00.000Z",
};

const validMatch = {
  id: "match-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  demandId: "demand-1",
  inventoryId: "inventory-1",
  verdict: "ready" as const,
  matchedEvidence: [{
    requirement: "成品",
    materialCardIds: ["card-1"],
    explanation: "synthetic finished-product evidence",
  }],
  unmetNeeds: [],
  recommendedCaptures: [],
  safeScope: "local synthetic draft only",
  capability,
  createdAt: "2026-09-09T08:00:00.000Z",
};

const validCustomerFeedback = {
  id: "customer-feedback-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  demandId: "demand-1",
  briefId: "brief-1",
  draftId: "draft-1",
  satisfaction: "partially-satisfied" as const,
  issueCategories: ["pacing" as const],
  comment: "synthetic feedback: make the opening shorter",
  mustKeep: ["finished-product-close"],
  mustRemove: [],
  needsReshoot: [],
  submittedBy: "operator-on-behalf" as const,
  actorId: "actor-a",
  publishClaimed: false as const,
  createdAt: "2026-09-09T08:00:00.000Z",
};

const validProposal = {
  id: "proposal-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  demandId: "demand-1",
  feedbackId: "customer-feedback-1",
  parentDraftId: "draft-1",
  parentBriefId: "brief-1",
  understoodFeedback: "shorten the synthetic opening",
  plannedChanges: ["trim-opening"],
  retainedElements: ["finished-product-close"],
  unsupportedRequests: [],
  captureGaps: [],
  status: "proposed" as const,
  confirmedByActorId: null,
  confirmedAt: null,
  capability,
  createdAt: "2026-09-09T08:00:00.000Z",
};
