import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  ReferenceMaterialMappingPlanSchema,
  ReferenceDraftPackageSchema,
  ReferenceDraftApprovalSchema,
  ReferenceMobileReviewV1Schema,
  ReferenceRightsDeclarationSchema,
  ReferenceSampleSchema,
  ReferenceStyleAnalysisSchema,
  ReferenceTransitionKindSchema,
  ReferenceVariantPlanSchema,
} from "../src/reference-guided-editing";

const digest = "a".repeat(64);
const rightsDigest = "b".repeat(64);
const analysisDigest = "c".repeat(64);
const selectionDigest = "d".repeat(64);
const mappingDigest = "e".repeat(64);
const permissionSetDigest = "f".repeat(64);
const createdAt = "2026-09-19T00:00:00.000Z";

function sample(overrides: Record<string, unknown> = {}) {
  return {
    id: "reference_1",
    customerId: "customer_a",
    sessionId: "session_a",
    version: 1,
    sourceAssetId: "reference_asset_1",
    sourceDigest: digest,
    sourceKind: "external-style-only",
    collectionMode: "dedicated-upload",
    candidateScope: "reference-only",
    status: "pending-rights",
    viewPermission: "allowed",
    usePermission: "review-required",
    createdAt,
    ...overrides,
  };
}

function sampleBinding(overrides: Record<string, unknown> = {}) {
  const value = sample();
  return {
    id: value.id,
    version: value.version,
    customerId: value.customerId,
    sessionId: value.sessionId,
    sourceAssetId: value.sourceAssetId,
    sourceDigest: value.sourceDigest,
    sourceKind: value.sourceKind,
    collectionMode: value.collectionMode,
    candidateScope: value.candidateScope,
    ...overrides,
  };
}

function rights(overrides: Record<string, unknown> = {}) {
  return {
    id: "rights_1",
    customerId: "customer_a",
    sessionId: "session_a",
    version: 1,
    contentDigest: rightsDigest,
    sample: sampleBinding(),
    sourceKind: "external-style-only",
    reusableElements: [],
    actorId: "designer_1",
    createdAt,
    ...overrides,
  };
}

function rightsBinding(overrides: Record<string, unknown> = {}) {
  return {
    id: "rights_1",
    version: 1,
    customerId: "customer_a",
    sessionId: "session_a",
    referenceSampleId: "reference_1",
    referenceSampleVersion: 1,
    referenceSampleDigest: digest,
    sourceKind: "external-style-only",
    reusableElements: [],
    contentDigest: rightsDigest,
    ...overrides,
  };
}

function confirmation(receiptId: string) {
  return { receiptId, actorId: "designer_1", confirmedAt: createdAt };
}

function permissionIdentity() {
  const { view: _view, use: _use, ...identity } = permission();
  return identity;
}

const workflowBindings = {
  sample: sampleBinding(),
  rights: rightsBinding(),
  analysis: {
    id: "analysis_1",
    version: 1,
    customerId: "customer_a",
    sessionId: "session_a",
    contentDigest: analysisDigest,
  },
  styleSelection: {
    id: "selection_1",
    version: 1,
    customerId: "customer_a",
    sessionId: "session_a",
    contentDigest: selectionDigest,
    status: "confirmed",
    confirmation: confirmation("selection_confirmation_1"),
  },
  mappingPlan: {
    id: "mapping_1",
    version: 1,
    customerId: "customer_a",
    sessionId: "session_a",
    contentDigest: mappingDigest,
    permissionSetDigest,
    permissions: [permissionIdentity()],
    status: "confirmed",
    confirmation: confirmation("mapping_confirmation_1"),
  },
};

function permission(overrides: Record<string, unknown> = {}) {
  return {
    id: "permission_1",
    version: 3,
    epoch: 7,
    customerId: "customer_a",
    sessionId: "session_a",
    sourceAssetId: "asset_1",
    sourceDigest: digest,
    view: "allowed",
    use: "allowed",
    ...overrides,
  };
}

function mappingPlan(mappingOverrides: Record<string, unknown> = {}) {
  return {
    id: "mapping_1",
    customerId: "customer_a",
    sessionId: "session_a",
    version: 1,
    contentDigest: mappingDigest,
    permissionSetDigest,
    bindings: structuredClone({
      sample: workflowBindings.sample,
      rights: workflowBindings.rights,
      analysis: workflowBindings.analysis,
      styleSelection: workflowBindings.styleSelection,
    }),
    mappings: [{
      id: "mapping_row_1",
      ruleId: "rule_1",
      referenceStartMs: 0,
      referenceEndMs: 2_000,
      targetRule: "keep-the-opening-tight",
      sourceBindingId: "source_binding_1",
      sourceAssetId: "asset_1",
      sourceDigest: digest,
      sourceStartMs: 500,
      sourceEndMs: 2_500,
      sourceProvenance: "customer-current",
      permission: permission(),
      reuseRights: null,
      status: "matched",
      differenceReason: null,
      ...mappingOverrides,
    }],
    unresolvedCount: 0,
    status: "draft",
    confirmedByActorId: null,
    confirmationReceiptId: null,
    confirmedAt: null,
    createdAt,
  };
}

function authorizedSource(overrides: Record<string, unknown> = {}) {
  return {
    id: "source_binding_1",
    mappingId: "mapping_row_1",
    customerId: "customer_a",
    sessionId: "session_a",
    sourceAssetId: "asset_1",
    sourceDigest: digest,
    provenance: "customer-current",
    permission: permission(),
    reuseRights: null,
    ...overrides,
  };
}

function variantPlan(overrides: Record<string, unknown> = {}) {
  return {
    id: "variant_1",
    customerId: "customer_a",
    sessionId: "session_a",
    version: 1,
    bindings: structuredClone(workflowBindings),
    kind: "editorial-master",
    authorizedSources: [authorizedSource()],
    timeline: [{
      id: "timeline_1",
      mappingId: "mapping_row_1",
      ruleId: "rule_1",
      sourceBindingId: "source_binding_1",
      sourceStartMs: 0,
      sourceEndMs: 2_000,
      timelineStartMs: 0,
      transition: "cut",
      targetDurationRatio: 1,
      reasonCode: "match-reference-rhythm",
    }],
    audioOverlay: null,
    platformChangeCodes: [],
    status: "planned",
    publishClaimed: false,
    createdAt,
    ...overrides,
  };
}

function reportArtifacts() {
  return [
    { kind: "editorial-master", variantPlanId: "variant_1", variantPlanVersion: 1, draftId: "draft_1", draftVersion: 1, outputDigest: "1".repeat(64) },
    { kind: "xiaohongshu", variantPlanId: "variant_2", variantPlanVersion: 1, draftId: "draft_2", draftVersion: 1, outputDigest: "2".repeat(64) },
    { kind: "douyin", variantPlanId: "variant_3", variantPlanVersion: 1, draftId: "draft_3", draftVersion: 1, outputDigest: "3".repeat(64) },
    { kind: "wechat-channels", variantPlanId: "variant_4", variantPlanVersion: 1, draftId: "draft_4", draftVersion: 1, outputDigest: "4".repeat(64) },
  ];
}

function referenceDraft() {
  return {
    id: "reference_draft_1",
    customerId: "customer_a",
    sessionId: "session_a",
    version: 1,
    bindings: {
      mappingPlan: structuredClone(workflowBindings.mappingPlan),
      variantPlan: {
        id: "variant_1", version: 1, customerId: "customer_a", sessionId: "session_a",
        kind: "editorial-master",
      },
    },
    outputDigest: "1".repeat(64),
    outputPath: "/trusted/reference-run/output.mp4",
    durationSeconds: 30,
    lineage: [
      {
        id: "lineage_video_1",
        use: "video",
        timelineEntryId: "timeline_1",
        mappingId: "mapping_row_1",
        sourceBindingId: "source_binding_1",
        sourceAssetId: "asset_1",
        sourceDigest: digest,
        sourceStartMs: 0,
        sourceEndMs: 2_000,
      },
      {
        id: "lineage_audio_1",
        use: "audio-overlay",
        timelineEntryId: null,
        mappingId: "audio_mapping_1",
        sourceBindingId: "audio_binding_1",
        sourceAssetId: "asset_1",
        sourceDigest: digest,
        sourceStartMs: 4_000,
        sourceEndMs: 8_000,
      },
    ],
    status: "reviewable",
    capability: { mode: "offline-fallback", label: "离线/模拟分析", version: 1, provider: null },
    publishClaimed: false,
    createdAt,
  };
}

function mobileReview() {
  const section = {
    status: "matched" as const,
    summaryCode: "rule-followed",
    evidenceRefs: ["render_evidence_1"],
  };
  return {
    id: "mobile_review_1",
    customerId: "customer_a",
    sessionId: "session_a",
    version: 1,
    bindings: {
      ...structuredClone(workflowBindings),
      executionReport: { id: "report_1", version: 1, customerId: "customer_a", sessionId: "session_a" },
    },
    styleSummary: {
      status: "complete",
      ruleKinds: ["rhythm"],
      capability: { mode: "offline-fallback", label: "离线/模拟分析", version: 1, provider: null },
      warningCodes: [],
    },
    artifacts: reportArtifacts().map((artifact) => ({
      ...artifact,
      mediaRoute: { kind: "reference-draft-media", version: 1, draftId: artifact.draftId },
      status: "reviewable",
    })),
    executionReport: {
      id: "report_1",
      version: 1,
      artifacts: reportArtifacts(),
      rhythm: section,
      transitions: section,
      narrative: section,
      audio: section,
      mappingCounts: { matched: 1, substituted: 0, unachievable: 0 },
      platformChanges: [
        { kind: "xiaohongshu", changeCodes: ["shorter-opening"] },
        { kind: "douyin", changeCodes: ["alternate-cut"] },
        { kind: "wechat-channels", changeCodes: ["longer-context"] },
      ],
    },
    approval: { status: "pending", approvedArtifactKinds: [] },
    status: "reviewable",
    publishClaimed: false,
    createdAt,
  };
}

function styleAnalysis(transitionOverrides: Record<string, unknown> = {}) {
  return {
    id: "analysis_1",
    customerId: "customer_a",
    sessionId: "session_a",
    version: 1,
    contentDigest: analysisDigest,
    bindings: { sample: workflowBindings.sample, rights: workflowBindings.rights },
    cuts: [1_000],
    sections: [{ id: "section_1", startMs: 0, endMs: 2_000, summary: "opening" }],
    transcriptSpans: [],
    beatMarkers: [500],
    transitions: [{ id: "transition_1", atMs: 1_000, kind: "cut", confidence: 0.9, ...transitionOverrides }],
    rules: [{ id: "rule_1", kind: "rhythm", description: "tight-opening", referenceStartMs: 0, referenceEndMs: 2_000, evidenceIds: ["section_1"] }],
    warnings: [],
    confidence: 0.9,
    status: "complete",
    capability: { mode: "offline-fallback", label: "离线/模拟分析", version: 1, provider: null },
    createdAt,
  };
}

describe("reference guided editing contracts", () => {
  it("binds a reference draft approval without invented cover or still evidence", () => {
    const approval = {
      kind: "reference-draft",
      id: "reference_approval_1",
      customerId: "customer_a",
      sessionId: "session_a",
      version: 1,
      bindings: {
        executionReport: { id: "report_1", version: 2 },
        draft: { id: "draft_1", version: 1, outputDigest: "1".repeat(64) },
        mappingPlan: {
          id: "mapping_1",
          version: 3,
          contentDigest: mappingDigest,
          permissionSetDigest,
        },
      },
      variant: "editorial-master",
      reviewerId: "reviewer_server_bound",
      fileDigest: "1".repeat(64),
      approvedAt: createdAt,
      status: "active",
      publishClaimed: false,
    };

    expect(ReferenceDraftApprovalSchema.parse(approval)).toEqual(approval);
    expect(() => ReferenceDraftApprovalSchema.parse({ ...approval, coverDigest: "2".repeat(64) }))
      .toThrow();
    expect(() => ReferenceDraftApprovalSchema.parse({ ...approval, publishClaimed: true }))
      .toThrow();
    expect(() => ReferenceDraftApprovalSchema.parse({
      ...approval,
      fileDigest: "3".repeat(64),
    })).toThrow("reference-approval-file-digest-mismatch");
  });

  it("has a reference-specific draft with exact bindings and disjoint repeated-asset lineage", () => {
    expect(ReferenceDraftPackageSchema.parse(referenceDraft())).toMatchObject({
      bindings: { mappingPlan: { id: "mapping_1" }, variantPlan: { id: "variant_1" } },
      lineage: [{ use: "video" }, { use: "audio-overlay" }],
      publishClaimed: false,
    });
    expect(() => ReferenceDraftPackageSchema.parse({
      ...referenceDraft(), customerId: "customer_b",
    })).toThrow("reference-draft-binding-scope-mismatch");
  });
  it("rejects reusable content for an external reference", () => {
    expect(() => ReferenceRightsDeclarationSchema.parse(rights({ reusableElements: ["audio-track"] })))
      .toThrow("external-reference-reusable-elements-forbidden");
  });

  it("rejects cross-customer and source-kind rights relabeling", () => {
    expect(() => ReferenceRightsDeclarationSchema.parse(rights({
      sample: sampleBinding({ customerId: "customer_b" }),
    }))).toThrow("reference-rights-customer-binding-mismatch");
    expect(() => ReferenceRightsDeclarationSchema.parse(rights({ sourceKind: "self-owned" })))
      .toThrow("reference-rights-source-kind-mismatch");
  });

  it("keeps dedicated uploads reference-only and review-required", () => {
    expect(() => ReferenceSampleSchema.parse(sample())).not.toThrow();
    expect(() => ReferenceSampleSchema.parse(sample({ usePermission: "allowed" })))
      .toThrow("dedicated-reference-use-permission-must-require-review");
    expect(() => ReferenceSampleSchema.parse(sample({ candidateScope: "ordinary-material" }))).toThrow();
  });

  it("requires every mapping to resolve to one honest status", () => {
    expect(() => ReferenceMaterialMappingPlanSchema.parse(mappingPlan({
      status: "matched", sourceBindingId: null, sourceAssetId: null, sourceDigest: null,
      sourceStartMs: null, sourceEndMs: null, sourceProvenance: null, permission: null,
    }))).toThrow("matched-reference-mapping-source-required");
  });

  it("requires substituted and unachievable mappings to describe the difference honestly", () => {
    expect(() => ReferenceMaterialMappingPlanSchema.parse(mappingPlan({
      status: "substituted", differenceReason: null,
    }))).toThrow("substituted-reference-mapping-difference-required");
    expect(() => ReferenceMaterialMappingPlanSchema.parse(mappingPlan({
      status: "unachievable", differenceReason: "missing-authorized-source",
    }))).toThrow("unachievable-reference-mapping-source-forbidden");
  });

  it("rejects permission provenance mismatches and stale permission identities", () => {
    expect(() => ReferenceMaterialMappingPlanSchema.parse(mappingPlan({
      permission: permission({ sourceAssetId: "other_asset" }),
    }))).toThrow("reference-source-permission-binding-mismatch");
    expect(() => ReferenceVariantPlanSchema.parse(variantPlan({
      authorizedSources: [authorizedSource({
        permission: permission({ version: 4 }),
      })],
    }))).toThrow("reference-source-permission-version-stale");
  });

  it("rejects direct external reference sources from executable variants", () => {
    expect(() => ReferenceVariantPlanSchema.parse(variantPlan({
      authorizedSources: [authorizedSource({ provenance: "external-reference" })],
    }))).toThrow();
  });

  it("rejects relabeling the bound external sample as customer material", () => {
    expect(() => ReferenceMaterialMappingPlanSchema.parse(mappingPlan({
      sourceAssetId: "reference_asset_1",
      permission: permission({ sourceAssetId: "reference_asset_1" }),
    }))).toThrow("external-reference-source-relabeling-forbidden");

    const relabeledVariant = variantPlan({
      authorizedSources: [authorizedSource({
        sourceAssetId: "reference_asset_1",
        permission: permission({ sourceAssetId: "reference_asset_1" }),
      })],
    });
    relabeledVariant.bindings.mappingPlan.permissions = [{
      ...permissionIdentity(),
      sourceAssetId: "reference_asset_1",
    }];
    expect(() => ReferenceVariantPlanSchema.parse(relabeledVariant))
      .toThrow("external-reference-source-relabeling-forbidden");
  });

  it("rejects self-owned video reuse absent from the immutable rights binding", () => {
    const unauthorizedVideoReuse = mappingPlan({
      sourceAssetId: "reference_asset_1",
      sourceProvenance: "self-owned-reference",
      permission: permission({ sourceAssetId: "reference_asset_1" }),
      reuseRights: { id: "rights_1", version: 1, element: "video-clips" },
    });
    unauthorizedVideoReuse.bindings.sample.sourceKind = "self-owned";
    unauthorizedVideoReuse.bindings.rights.sourceKind = "self-owned";
    (unauthorizedVideoReuse.bindings.rights as Record<string, unknown>).reusableElements = [];

    expect(() => ReferenceMaterialMappingPlanSchema.parse(unauthorizedVideoReuse))
      .toThrow("self-owned-reference-reuse-element-not-authorized");
  });

  it("rejects self-owned audio reuse absent from the immutable rights binding", () => {
    const audioPermission = permission({
      id: "audio_permission_1",
      sourceAssetId: "reference_asset_1",
    });
    const unauthorizedAudioReuse = variantPlan({
      authorizedSources: [
        authorizedSource(),
        authorizedSource({
          id: "audio_source_binding_1",
          mappingId: "audio_mapping_1",
          sourceAssetId: "reference_asset_1",
          provenance: "self-owned-reference",
          permission: audioPermission,
          reuseRights: { id: "rights_1", version: 1, element: "audio-track" },
        }),
      ],
      audioOverlay: {
        sourceBindingId: "audio_source_binding_1",
        sourceStartMs: 0,
        sourceEndMs: 1_000,
        volume: 0.5,
      },
    });
    unauthorizedAudioReuse.bindings.sample.sourceKind = "self-owned";
    unauthorizedAudioReuse.bindings.rights.sourceKind = "self-owned";
    (unauthorizedAudioReuse.bindings.rights as Record<string, unknown>).reusableElements = [];
    const { view: _view, use: _use, ...audioPermissionIdentity } = audioPermission;
    unauthorizedAudioReuse.bindings.mappingPlan.permissions.push(audioPermissionIdentity);

    expect(() => ReferenceVariantPlanSchema.parse(unauthorizedAudioReuse))
      .toThrow("self-owned-reference-reuse-element-not-authorized");
  });

  it("requires confirmed style and mapping prerequisites", () => {
    const unconfirmedMapping = mappingPlan();
    unconfirmedMapping.bindings.styleSelection.status = "invalidated";
    expect(() => ReferenceMaterialMappingPlanSchema.parse(unconfirmedMapping))
      .toThrow("confirmed-reference-style-selection-required");
    const unconfirmedVariant = variantPlan();
    unconfirmedVariant.bindings.mappingPlan.status = "draft";
    expect(() => ReferenceVariantPlanSchema.parse(unconfirmedVariant))
      .toThrow("confirmed-reference-mapping-plan-required");
  });

  it("binds mapping plans to immutable upstream versions and rejects stale counts", () => {
    const missingSelectionVersion = structuredClone(mappingPlan());
    delete (missingSelectionVersion.bindings.styleSelection as { version?: number }).version;
    expect(() => ReferenceMaterialMappingPlanSchema.parse(missingSelectionVersion)).toThrow();
    expect(() => ReferenceMaterialMappingPlanSchema.parse({
      ...mappingPlan({
        status: "unachievable", sourceBindingId: null, sourceAssetId: null, sourceDigest: null,
        sourceStartMs: null, sourceEndMs: null, sourceProvenance: null, permission: null,
        reuseRights: null, differenceReason: "missing-authorized-source",
      }),
      unresolvedCount: 0,
    })).toThrow("reference-mapping-unresolved-count-mismatch");
  });

  it("keeps low-confidence supported transitions non-executable", () => {
    expect(() => ReferenceStyleAnalysisSchema.parse(styleAnalysis({ confidence: 0.59 })))
      .toThrow("low-confidence-reference-transition-must-be-unsupported");
    expect(() => ReferenceStyleAnalysisSchema.parse(styleAnalysis({ kind: "unsupported", confidence: 0.59 })))
      .not.toThrow();
  });

  it("keeps unsupported transitions as evidence but never as an executable transition", () => {
    expect(ReferenceTransitionKindSchema.parse("unsupported")).toBe("unsupported");
    const unsupportedVariant = variantPlan();
    unsupportedVariant.timeline[0].transition = "unsupported";
    expect(() => ReferenceVariantPlanSchema.parse(unsupportedVariant)).toThrow();
  });

  it("does not expose a magic total similarity score", () => {
    const parsed = ReferenceMobileReviewV1Schema.parse(mobileReview());
    expect(parsed.executionReport).not.toHaveProperty("totalSimilarity");
    expect(parsed.publishClaimed).toBe(false);
  });

  it("parses the checked-in V1 mobile handoff fixture exactly", () => {
    const fixture = JSON.parse(readFileSync(
      new URL("../fixtures/reference-mobile-review-v1.json", import.meta.url),
      "utf8",
    ));
    expect(ReferenceMobileReviewV1Schema.parse(fixture)).toEqual(mobileReview());
    expect(fixture.artifacts.map((artifact: { kind: string }) => artifact.kind)).toEqual([
      "editorial-master", "xiaohongshu", "douyin", "wechat-channels",
    ]);
  });

  it("rejects mobile artifacts mixed from another execution report", () => {
    const mixed = mobileReview();
    mixed.artifacts[1].draftId = "draft_from_other_run";
    expect(() => ReferenceMobileReviewV1Schema.parse(mixed))
      .toThrow("reference-mobile-artifact-report-binding-mismatch");
  });

  it("rejects raw text channels and unsafe mobile fields", () => {
    expect(() => ReferenceMobileReviewV1Schema.parse({
      ...mobileReview(), styleSummary: { ...mobileReview().styleSummary, rawTranscript: "external spoken content" },
    })).toThrow();
    expect(() => ReferenceMobileReviewV1Schema.parse({
      ...mobileReview(), executionReport: {
        ...mobileReview().executionReport,
        rhythm: { ...mobileReview().executionReport.rhythm, summaryCode: "verbatim external transcript" },
      },
    })).toThrow();
    expect(() => ReferenceMobileReviewV1Schema.parse({ ...mobileReview(), sourcePath: "/tmp/reference.mov" })).toThrow();
    for (const unsafe of [
      { originalBytes: "AAAA" },
      { externalTranscript: "verbatim source words" },
      { externalContent: "copied source content" },
      { rawModelOutput: { hidden: true } },
      { outputPath: "/Users/test-user/private/output.mp4" },
      { localPath: "C:\\Users\\operator\\private.mov" },
    ]) {
      expect(() => ReferenceMobileReviewV1Schema.parse({ ...mobileReview(), ...unsafe })).toThrow();
    }
    expect(() => ReferenceMobileReviewV1Schema.parse({
      ...mobileReview(),
      styleSummary: {
        ...mobileReview().styleSummary,
        capability: { mode: "provider", label: "/Users/test-user/model", version: 1, provider: "local" },
      },
    })).toThrow();
  });

  it.each([
    "/Volumes/private/reference.mov",
    "/opt/private/reference.mov",
    "C:/Users/test-user/private.mov",
    "C:\\Users\\operator\\private.mov",
    "\\\\server\\share\\private.mov",
    "\\\\?\\C:\\private.mov",
    "file:///Volumes/private/reference.mov",
    "%2FVolumes%2Fprivate%2Freference.mov",
    "file%3A%2F%2F%2Fopt%2Fprivate.mov",
    "%252Fopt%252Fprivate.mov",
    "%5C%5Cserver%5Cshare%5Cprivate.mov",
    "模型位于 /Users/test-user/model",
    "错误来源 file:///private/model.json",
    "前缀 C:/Users/test-user/private.mov",
    "前缀 %252Fopt%252Fprivate.mov",
    "/api/studio/foo?debug=/private/draft.mov",
    "/api/studio/foo;debug=/private/draft.mov",
    "/api/studio/foo&debug=/private/draft.mov",
    "/api/studio/foo#debug=/private/draft.mov",
    "/api/studio/foo\\private\\draft.mov",
    "/api/studio/foo%3Fdebug%3D%2Fprivate%2Fdraft.mov",
    "/api/studio/foo%2Fprivate%2Fdraft.mov",
    "路径：/tmp",
    "路径：/Users/test-user/video.mov",
    "路径：/Users/test-user/video.mov",
    "路径：%2FUsers%2F木泽%2Fvideo.mov",
    "路径%EF%BC%9A%2FUsers%2F%E6%9C%A8%E6%B3%BD%2Fvideo.mov",
    "路径%25EF%25BC%259A%252FUsers%252F%25E6%259C%25A8%25E6%25B3%25BD%252Fvideo.mov",
    "路径/木泽/video.mov",
    "%E8%B7%AF%E5%BE%84%2F%E6%9C%A8%E6%B3%BD%2Fvideo.mov",
    "%25E8%25B7%25AF%25E5%25BE%2584%252F%25E6%259C%25A8%25E6%25B3%25BD%252Fvideo.mov",
  ])("rejects arbitrary mobile capability labels instead of passing through unsafe text: %s", (unsafePath) => {
    const base = mobileReview();
    const review = {
      ...base,
      styleSummary: {
        ...base.styleSummary,
        capability: { mode: "provider", label: unsafePath, version: 1, provider: "test-provider" },
      },
    };

    expect(() => ReferenceMobileReviewV1Schema.parse(review)).toThrow();
  });

  it.each([
    "路径%EF%BC%9A%2FUsers%2F%E6%9C%A8%E6%B3%BD%2Fprovider.json",
    "路径/木泽/video.mov",
    "%E8%B7%AF%E5%BE%84%2F%E6%9C%A8%E6%B3%BD%2Fvideo.mov",
    "%25E8%25B7%25AF%25E5%25BE%2584%252F%25E6%259C%A8%25E6%25B3%25BD%252Fvideo.mov",
  ])("rejects an arbitrary provider identity even when the mobile label is fixed: %s", (unsafeProvider) => {
    const base = mobileReview();
    expect(() => ReferenceMobileReviewV1Schema.parse({
      ...base,
      styleSummary: {
        ...base.styleSummary,
        capability: {
          mode: "provider",
          label: "移动审片：已配置模型服务 A/B，节奏/转场",
          version: 1,
          provider: unsafeProvider,
        },
      },
    })).toThrow();
  });

  it.each([
    "/Volumes/private/draft.mov",
    "/opt/private/draft.mov",
    "C:/private/draft.mov",
    "C:\\private\\draft.mov",
    "\\\\server\\share\\draft.mov",
    "\\\\?\\C:\\private\\draft.mov",
    "file:///Volumes/private/draft.mov",
    "%252FVolumes%252Fprivate%252Fdraft.mov",
    "draft at /private/draft.mov",
    "路径：/Users/test-user/draft.mov",
    "路径：/Users/test-user/draft.mov",
    "路径%EF%BC%9A%2FUsers%2F%E6%9C%A8%E6%B3%BD%2Fdraft.mov",
    "路径/木泽/video.mov",
    "%E8%B7%AF%E5%BE%84%2F%E6%9C%A8%E6%B3%BD%2Fvideo.mov",
    "%25E8%25B7%25AF%25E5%25BE%2584%252F%25E6%259C%A8%25E6%25B3%25BD%252Fvideo.mov",
  ])("rejects coordinated local paths across artifact, report, and media identities: %s", (unsafePath) => {
    const review = mobileReview();
    review.artifacts[0]!.draftId = unsafePath;
    review.artifacts[0]!.mediaRoute.draftId = unsafePath;
    review.executionReport.artifacts[0]!.draftId = unsafePath;

    expect(() => ReferenceMobileReviewV1Schema.parse(review))
      .toThrow("reference-mobile-identity-invalid");
  });

  it("keeps the fixed provider label with Chinese, A/B, and rhythm/transition text valid", () => {
    const chineseBase = mobileReview();
    const chineseLabel = {
      ...chineseBase,
      styleSummary: {
        ...chineseBase.styleSummary,
        capability: {
          mode: "provider", label: "移动审片：已配置模型服务 A/B，节奏/转场",
          version: 1, provider: "configured-provider",
        },
      },
    };
    expect(() => ReferenceMobileReviewV1Schema.parse(chineseLabel)).not.toThrow();

  });

  it.each([
    {
      mode: "provider" as const,
      label: "移动审片：已配置模型服务 A/B，节奏/转场" as const,
      version: 730_001,
      provider: "configured-provider" as const,
    },
    {
      mode: "offline-fallback" as const,
      label: "离线/模拟分析" as const,
      version: 830_001,
      provider: null,
    },
  ])("rejects internal capability version fingerprints from the public contract: $mode", (capability) => {
    const base = mobileReview();
    expect(() => ReferenceMobileReviewV1Schema.parse({
      ...base,
      styleSummary: { ...base.styleSummary, capability },
    })).toThrow();
  });

  it("rejects mobile publish claims and incomplete artifact sets", () => {
    expect(() => ReferenceMobileReviewV1Schema.parse({ ...mobileReview(), publishClaimed: true })).toThrow();
    expect(() => ReferenceMobileReviewV1Schema.parse({
      ...mobileReview(), artifacts: mobileReview().artifacts.slice(0, 2),
    })).toThrow("reference-mobile-artifact-set-incomplete");
    expect(() => ReferenceMobileReviewV1Schema.parse({
      ...mobileReview(),
      artifacts: mobileReview().artifacts.map((artifact, index) => index === 0 ? {
        ...artifact,
        mediaRoute: { ...artifact.mediaRoute, draftId: "another_draft" },
      } : artifact),
    })).toThrow("reference-mobile-media-route-binding-mismatch");
  });
});
