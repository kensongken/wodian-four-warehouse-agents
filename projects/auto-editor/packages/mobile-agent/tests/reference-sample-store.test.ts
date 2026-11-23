import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type {
  ReferenceDraftPackage,
  ReferenceMaterialMappingPlan,
  ReferenceSample,
  ReferenceVariantPlan,
} from "@wearable/contracts";

import {
  CustomerMaterialLibrary,
  JsonReferenceSampleStore,
  ReferenceSampleService,
  referenceEditorOperationMetadata,
  referenceWorkflowDigest,
  type ReferenceAnalysisAssetSnapshot,
  type StudioAnalysisAssetSnapshot,
  type StudioScope,
} from "../src";

const scopeA: StudioScope = {
  actorId: "actor-a",
  customerId: "customer-a",
  sessionId: "reference-session-a",
};

const scopeB: StudioScope = {
  actorId: "actor-b",
  customerId: "customer-b",
  sessionId: "reference-session-b",
};

const now = () => "2026-09-19T08:00:00.000Z";
const digest = (value: Uint8Array | string) => createHash("sha256").update(value).digest("hex");

describe("reference sample storage and import isolation", () => {
  let root: string;
  let library: CustomerMaterialLibrary;
  let service: ReferenceSampleService;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "reference-sample-store-"));
    library = new CustomerMaterialLibrary(root, () => true);
    service = new ReferenceSampleService(library, root, { now });
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("rejects a completed editor receipt without its exact persisted result", async () => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    const snapshot = await store.snapshot();
    const { digest: _digest, ...payload } = snapshot;
    const ghost = {
      ...referenceEditorOperationMetadata(scopeA, "render", "ghost-operation", {
        planId: "missing-plan",
        inputBindingDigest: digest("missing-binding"),
        expectedResult: {
          id: "missing-mobile-review",
          version: 1,
          reportId: "missing-report",
          reportVersion: 1,
        },
      }),
      resultDigest: digest("missing-result"),
      result: { kind: "report" as const, id: "missing-report", version: 1 },
    };
    const next = { ...payload, editorOperations: [ghost] };

    await expect(store.commit({ ...next, digest: referenceWorkflowDigest(next) }, snapshot.digest))
      .rejects.toThrow("reference-operation-result-missing");
    expect(await store.snapshot()).toEqual(snapshot);
  });

  it("rejects an orphan reference draft until its exact mapping and variant plans exist", async () => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    const draft = referenceDraftFixture();

    await expect(store.putDraft(draft)).rejects.toThrow("reference-draft-mapping-plan-missing");
    await expect(store.listDrafts()).resolves.toEqual([]);
  });

  it("atomically appends matching variant and draft records against a confirmed mapping", async () => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    const { mappingPlan, variantPlan, draft } = referenceRenderBundleFixture();
    await seedLegacyConfirmedMapping(root, scopeA, mappingPlan);
    const before = await store.snapshot();

    await expect(store.commit({
      ...before,
      variantPlans: [variantPlan],
      drafts: [draft],
    }, before.digest)).resolves.toBeUndefined();

    const stored = await store.snapshot();
    expect(stored.mappingPlans).toEqual([mappingPlan]);
    expect(stored.variantPlans).toEqual([variantPlan]);
    expect(stored.drafts).toEqual([draft]);
  });

  it("allows standalone draft append only after both exact dependencies are durable", async () => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    const { mappingPlan, variantPlan, draft } = referenceRenderBundleFixture();
    await seedLegacyConfirmedMapping(root, scopeA, mappingPlan);
    await store.putVariantPlan(variantPlan);

    await expect(store.putDraft(draft)).resolves.toEqual(draft);
    await expect(store.listDrafts()).resolves.toEqual([draft]);
  });

  it("rejects an invalidated mapping version through the standalone put boundary", async () => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    const { mappingPlan } = referenceRenderBundleFixture();
    const draft = {
      ...mappingPlan,
      status: "draft" as const,
      confirmedByActorId: null,
      confirmationReceiptId: null,
      confirmedAt: null,
    };
    await expect(store.putMappingPlan(mappingPlan))
      .rejects.toThrow("reference-mapping-put-requires-initial-draft");
    await store.putMappingPlan(draft);

    await expect(store.putMappingPlan({
      ...draft,
      version: draft.version + 1,
      status: "invalidated",
    })).rejects.toThrow("reference-invalidation-commit-required");
    await expect(store.listMappingPlans()).resolves.toEqual([draft]);
  });

  it("rejects a 257th current mapping for one asset before invalidation becomes unrepresentable", async () => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    const before = await store.snapshot();
    const { mappingPlan } = referenceRenderBundleFixture();
    const mappingPlans = Array.from({ length: 257 }, (_, index) =>
      ({
        ...mappingPlanForDistinctChain(mappingPlan, index),
        status: "draft" as const,
        confirmedByActorId: null,
        confirmationReceiptId: null,
        confirmedAt: null,
      }));

    await expect(store.commit({ ...before, mappingPlans: mappingPlans.slice(0, 256) }, before.digest))
      .resolves.toBeUndefined();
    const atLimit = await store.snapshot();
    await expect(store.commit({ ...atLimit, mappingPlans }, atLimit.digest))
      .rejects.toThrow("reference-invalidation-identity-capacity-exceeded");
    await expect(store.snapshot()).resolves.toEqual(atLimit);
  });

  it.each([
    ["mapping content", (draft: ReferenceDraftPackage) => {
      draft.bindings.mappingPlan.contentDigest = digest("wrong-mapping-content");
    }, "reference-draft-mapping-plan-mismatch"],
    ["variant kind", (draft: ReferenceDraftPackage) => {
      draft.bindings.variantPlan.kind = "xiaohongshu";
    }, "reference-draft-variant-plan-mismatch"],
  ])("rejects a %s binding mismatch", async (_label, mutate, expected) => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    const before = await store.snapshot();
    const { mappingPlan, variantPlan, draft } = referenceRenderBundleFixture();
    mutate(draft);

    await expect(store.commit({
      ...before,
      mappingPlans: [mappingPlan],
      variantPlans: [variantPlan],
      drafts: [draft],
    }, before.digest)).rejects.toThrow(expected);
    expect(await store.snapshot()).toEqual(before);
  });

  it("rejects a draft whose lineage does not match its bound variant without changing state", async () => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    const before = await store.snapshot();
    const { mappingPlan, variantPlan, draft } = referenceRenderBundleFixture();
    const mismatched = structuredClone(draft);
    mismatched.lineage[0]!.sourceEndMs = 29_960;

    await expect(store.commit({
      ...before,
      mappingPlans: [mappingPlan],
      variantPlans: [variantPlan],
      drafts: [mismatched],
    }, before.digest)).rejects.toThrow("reference-draft-lineage-mismatch");
    expect(await store.snapshot()).toEqual(before);
  });

  it.each([
    ["timeline mapping", (variant: ReferenceVariantPlan) => {
      variant.timeline[0]!.mappingId = "mapping-row-store-forged";
      variant.authorizedSources[0]!.mappingId = "mapping-row-store-forged";
    }],
    ["timeline rule", (variant: ReferenceVariantPlan) => {
      variant.timeline[0]!.ruleId = "rule-store-forged";
    }],
    ["timeline source interval", (variant: ReferenceVariantPlan) => {
      variant.timeline[0]!.sourceStartMs = 30_000;
      variant.timeline[0]!.sourceEndMs = 60_000;
    }],
    ["source provenance", (variant: ReferenceVariantPlan) => {
      variant.authorizedSources[0]!.provenance = "customer-authorized-history";
    }],
  ])("rejects a persisted variant whose %s is not bound to its confirmed mapping snapshot", async (
    _label,
    mutate,
  ) => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    const { mappingPlan, variantPlan } = referenceRenderBundleFixture();
    mutate(variantPlan);
    await seedLegacyConfirmedMapping(root, scopeA, mappingPlan);

    await expect(store.putVariantPlan(variantPlan))
      .rejects.toThrow("reference-variant-mapping-mismatch");
    await expect(store.listVariantPlans()).resolves.toEqual([]);
  });

  it("keeps a dedicated external reference out of ordinary usable assets", async () => {
    const result = await service.importDedicated(scopeA, {
      requestId: "import_1",
      fileName: "sample.mov",
      bytes: Buffer.from("external-reference-video"),
      sourceKind: "external-style-only",
      operationKey: "reference_import_1",
    });

    expect(result.sample.collectionMode).toBe("dedicated-upload");
    expect(result.sample.candidateScope).toBe("reference-only");
    expect(result.asset.usePermission).toBe("review-required");
    expect(await service.listOrdinaryCandidateAssetIds(scopeA)).not.toContain(result.asset.id);
  });

  it("rejects a cross-customer historical pointer without writing state", async () => {
    const customerBAsset = await library.importOriginal(scopeB.actorId, scopeB.customerId, {
      requestId: "customer-b-history",
      bytes: Buffer.from("customer-b-history-video"),
      mediaKind: "video",
      fileName: "customer-b.mov",
      capturedAt: null,
      sessionId: scopeB.sessionId,
    });
    const store = await JsonReferenceSampleStore.open(root, scopeA);

    await expect(service.registerHistorical(scopeA, {
      assetId: customerBAsset.id,
      sourceKind: "self-owned",
      operationKey: "history_1",
    })).rejects.toThrow("reference-history-asset-not-visible");
    expect((await store.snapshot()).samples).toHaveLength(0);
  });

  it("allows view-authorized reference analysis without granting output use", async () => {
    const { asset } = await service.importDedicated(scopeA, {
      requestId: "reference-analysis-view",
      fileName: "analysis.mov",
      bytes: Buffer.from("view-only-reference-video"),
      sourceKind: "external-style-only",
      operationKey: "reference_analysis_view",
    });

    const snapshot: ReferenceAnalysisAssetSnapshot = await library.inspectReferenceAnalysisAsset(
      scopeA.actorId,
      scopeA.customerId,
      scopeA.sessionId,
      asset.id,
    );

    expect(snapshot.asset.viewPermission).toBe("allowed");
    expect(snapshot.asset.usePermission).toBe("review-required");
    expect(snapshot.authorization).toBe("reference-view-authorized");
    // @ts-expect-error A view-only reference capability must not satisfy output-use authorization.
    acceptsOutputSnapshot(snapshot);
    await expect(library.inspectAnalysisAsset(
      scopeA.actorId,
      scopeA.customerId,
      scopeA.sessionId,
      asset.id,
    )).rejects.toThrow("asset-use-not-allowed");
  });

  it("returns a distinct output-use capability for an ordinary authorized asset", async () => {
    const asset = await library.importOriginal(scopeA.actorId, scopeA.customerId, {
      requestId: "ordinary-output-capability",
      bytes: Buffer.from("ordinary-output-video"),
      mediaKind: "video",
      fileName: "ordinary.mov",
      capturedAt: null,
      sessionId: scopeA.sessionId,
      viewPermission: "allowed",
      usePermission: "allowed",
    });

    const snapshot: StudioAnalysisAssetSnapshot = await library.inspectAnalysisAsset(
      scopeA.actorId,
      scopeA.customerId,
      scopeA.sessionId,
      asset.id,
    );

    expect(snapshot.authorization).toBe("output-use-authorized");
    acceptsOutputSnapshot(snapshot);
  });

  it("denies reference metadata to an actor without customer view authorization", async () => {
    const guardedLibrary = new CustomerMaterialLibrary(root, ({ actorId }) => actorId !== "actor-denied");
    const guardedService = new ReferenceSampleService(guardedLibrary, root, { now });
    await guardedService.importDedicated(scopeA, {
      requestId: "authorized-reference-import",
      fileName: "guarded.mov",
      bytes: Buffer.from("guarded-reference-video"),
      sourceKind: "external-style-only",
      operationKey: "authorized_reference_import",
    });

    await expect(guardedService.listSamples({ ...scopeA, actorId: "actor-denied" }))
      .rejects.toThrow("not-authorized");
  });

  it("does not return reference metadata when view authorization is revoked before return", async () => {
    const guardedLibrary = new CustomerMaterialLibrary(root, ({ actorId, action, phase }) =>
      !(actorId === "actor-revoked" && action === "view" && phase === "after"));
    const guardedService = new ReferenceSampleService(guardedLibrary, root, { now });
    await guardedService.importDedicated(scopeA, {
      requestId: "revoked-reference-import",
      fileName: "revoked.mov",
      bytes: Buffer.from("revoked-reference-video"),
      sourceKind: "external-style-only",
      operationKey: "revoked_reference_import",
    });

    await expect(guardedService.listSamples({ ...scopeA, actorId: "actor-revoked" }))
      .rejects.toThrow("not-authorized");
  });

  it("does not reserve an import operation for an actor denied import authorization", async () => {
    const guardedLibrary = new CustomerMaterialLibrary(root, ({ action }) => action !== "import");
    const guardedService = new ReferenceSampleService(guardedLibrary, root, { now });

    await expect(guardedService.importDedicated(scopeA, {
      requestId: "denied-reference-import",
      fileName: "denied.mov",
      bytes: Buffer.from("denied-reference-video"),
      sourceKind: "external-style-only",
      operationKey: "denied_reference_import",
    })).rejects.toThrow("not-authorized");

    expect(await fileTree(root)).toEqual([]);
  });

  it("registers a historical pointer without reimporting or mutating the original", async () => {
    const bytes = Buffer.from("existing-customer-history");
    const asset = await library.importOriginal(scopeA.actorId, scopeA.customerId, {
      requestId: "existing-history",
      bytes,
      mediaKind: "video",
      fileName: "history.mp4",
      capturedAt: "2026-09-18T08:00:00.000Z",
      sessionId: "prior-session-a",
      viewPermission: "allowed",
      usePermission: "allowed",
    });
    const before = await library.listAssets(scopeA.actorId, scopeA.customerId);

    const result = await service.registerHistorical(scopeA, {
      assetId: asset.id,
      sourceKind: "self-owned",
      operationKey: "history_pointer_1",
    });

    expect(result.asset).toEqual(asset);
    expect(result.sample).toMatchObject({
      sourceAssetId: asset.id,
      sourceDigest: asset.digest,
      collectionMode: "historical-pointer",
      candidateScope: "reference-only",
    });
    expect(await library.listAssets(scopeA.actorId, scopeA.customerId)).toEqual(before);
    expect(await library.readOriginal(scopeA.actorId, scopeA.customerId, asset.id)).toEqual(bytes);
  });

  it("returns the immutable original registration when an operation is retried later", async () => {
    let currentTime = "2026-09-19T08:00:00.000Z";
    service = new ReferenceSampleService(library, root, { now: () => currentTime });
    const input = {
      requestId: "retry-reference-import",
      fileName: "retry.mov",
      bytes: Buffer.from("retry-reference-video"),
      sourceKind: "external-style-only" as const,
      operationKey: "retry_reference_import",
    };

    const first = await service.importDedicated(scopeA, input);
    currentTime = "2026-09-19T09:00:00.000Z";
    const retry = await service.importDedicated(scopeA, input);

    expect(retry).toEqual(first);
    expect(await service.listSamples(scopeA)).toEqual([first.sample]);
  });

  it("rejects a sequential operation-key collision before creating import side effects", async () => {
    const first = await service.importDedicated(scopeA, {
      requestId: "operation-first-request",
      fileName: "first.mov",
      bytes: Buffer.from("operation-first-video"),
      sourceKind: "external-style-only",
      operationKey: "colliding_operation",
    });
    const filesBefore = await fileTree(root);
    const assetsBefore = await library.listAssets(scopeA.actorId, scopeA.customerId);

    await expect(service.importDedicated(scopeA, {
      requestId: "operation-conflict-request",
      fileName: "conflict.mov",
      bytes: Buffer.from("operation-conflict-video"),
      sourceKind: "self-owned",
      operationKey: "colliding_operation",
    })).rejects.toThrow("reference-operation-key-conflict");

    expect(await fileTree(root)).toEqual(filesBefore);
    expect(await library.listAssets(scopeA.actorId, scopeA.customerId)).toEqual(assetsBefore);
    expect(await service.listSamples(scopeA)).toEqual([first.sample]);
  });

  it("serializes concurrent operation-key collisions before either can create an orphan import", async () => {
    const results = await Promise.allSettled([
      service.importDedicated(scopeA, {
        requestId: "concurrent-operation-a",
        fileName: "concurrent-a.mov",
        bytes: Buffer.from("concurrent-reference-a"),
        sourceKind: "external-style-only",
        operationKey: "concurrent_collision",
      }),
      service.importDedicated(scopeA, {
        requestId: "concurrent-operation-b",
        fileName: "concurrent-b.mov",
        bytes: Buffer.from("concurrent-reference-b"),
        sourceKind: "self-owned",
        operationKey: "concurrent_collision",
      }),
    ]);

    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    expect(results.filter((result) => result.status === "rejected")).toHaveLength(1);
    expect(String((results.find((result) => result.status === "rejected") as PromiseRejectedResult).reason))
      .toContain("reference-operation-key-conflict");
    expect(await library.listAssets(scopeA.actorId, scopeA.customerId)).toHaveLength(1);
    expect(await service.listSamples(scopeA)).toHaveLength(1);
  });

  it("serializes writers across store instances without dropping immutable samples", async () => {
    const first = await JsonReferenceSampleStore.open(root, scopeA);
    const second = await JsonReferenceSampleStore.open(root, scopeA);

    await Promise.all([
      first.putSample(sampleFor(scopeA, "one")),
      second.putSample(sampleFor(scopeA, "two")),
    ]);

    expect((await first.listSamples()).map((sample) => sample.id).sort()).toEqual([
      "reference-sample-one",
      "reference-sample-two",
    ]);
  });

  it("rejects a stale snapshot digest without merging stale state", async () => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    const stale = await store.snapshot();
    await store.putSample(sampleFor(scopeA, "current"));

    await expect(store.commit(stale, stale.digest)).rejects.toThrow("reference-state-conflict");
    expect((await store.listSamples()).map((sample) => sample.id)).toEqual([
      "reference-sample-current",
    ]);
  });

  it("rejects immutable object replacement through public commit without changing state", async () => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    await store.putSample(sampleFor(scopeA, "commit-replacement"));
    const before = await store.snapshot();
    const replacement = {
      ...before.samples[0]!,
      sourceKind: "external-style-only" as const,
    };

    await expect(store.commit({ ...before, samples: [replacement] }, before.digest))
      .rejects.toThrow("reference-immutable-object-conflict");
    expect(await store.snapshot()).toEqual(before);
  });

  it("rejects immutable object deletion through public commit without changing state", async () => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    await store.putSample(sampleFor(scopeA, "commit-deletion"));
    const before = await store.snapshot();

    await expect(store.commit({ ...before, samples: [] }, before.digest))
      .rejects.toThrow("reference-immutable-object-conflict");
    expect(await store.snapshot()).toEqual(before);
  });

  it("rejects invalid or conflicting immutable sample versions without changing state", async () => {
    const store = await JsonReferenceSampleStore.open(root, scopeA);
    const sample = sampleFor(scopeA, "immutable");
    await store.putSample(sample);

    await expect(store.putSample({ ...sample, sourceDigest: "not-a-digest" } as ReferenceSample))
      .rejects.toThrow();
    await expect(store.putSample({ ...sample, sourceKind: "external-style-only" }))
      .rejects.toThrow("reference-immutable-object-conflict");
    expect(await store.listSamples()).toEqual([sample]);
  });
});

function mappingPlanForDistinctChain(
  base: ReferenceMaterialMappingPlan,
  index: number,
): ReferenceMaterialMappingPlan {
  const suffix = String(index).padStart(3, "0");
  const sample = {
    ...base.bindings.sample,
    id: `reference-sample-capacity-${suffix}`,
    sourceAssetId: `reference-source-capacity-${suffix}`,
    sourceDigest: digest(`reference-source-capacity-${suffix}`),
  };
  const rights = {
    ...base.bindings.rights,
    id: `reference-rights-capacity-${suffix}`,
    referenceSampleId: sample.id,
    referenceSampleVersion: sample.version,
    referenceSampleDigest: sample.sourceDigest,
    contentDigest: digest(`reference-rights-capacity-${suffix}`),
  };
  const analysis = {
    ...base.bindings.analysis,
    id: `reference-analysis-capacity-${suffix}`,
    contentDigest: digest(`reference-analysis-capacity-${suffix}`),
  };
  const styleSelection = {
    ...base.bindings.styleSelection,
    id: `reference-selection-capacity-${suffix}`,
    contentDigest: digest(`reference-selection-capacity-${suffix}`),
    confirmation: {
      ...base.bindings.styleSelection.confirmation,
      receiptId: `reference-selection-confirmation-capacity-${suffix}`,
    },
  };
  return {
    ...base,
    id: `mapping-plan-capacity-${suffix}`,
    contentDigest: digest(`mapping-plan-capacity-${suffix}`),
    bindings: { sample, rights, analysis, styleSelection },
    mappings: base.mappings.map((mapping, mappingIndex) => ({
      ...mapping,
      id: `mapping-row-capacity-${suffix}-${mappingIndex}`,
      sourceBindingId: `source-binding-capacity-${suffix}-${mappingIndex}`,
    })),
    confirmationReceiptId: `mapping-confirmation-capacity-${suffix}`,
  };
}

async function seedLegacyConfirmedMapping(
  root: string,
  scope: StudioScope,
  mappingPlan: ReferenceMaterialMappingPlan,
): Promise<void> {
  const payload = {
    samples: [],
    rightsDeclarations: [],
    analyses: [],
    styleSelections: [],
    mappingPlans: [mappingPlan],
    variantPlans: [],
    drafts: [],
    executionReports: [],
    approvals: [],
    mobileReviews: [],
    editorOperations: [],
  };
  const workflowDirectory = join(
    root,
    "reference-workflows",
    digest(scope.customerId),
    digest(scope.sessionId),
  );
  await mkdir(workflowDirectory, { recursive: true });
  await writeFile(join(workflowDirectory, "snapshot.json"), `${JSON.stringify({
    ...payload,
    digest: referenceWorkflowDigest(payload),
  })}\n`);
}

function referenceDraftFixture(): ReferenceDraftPackage {
  return referenceRenderBundleFixture().draft;
}

function referenceRenderBundleFixture(): {
  mappingPlan: ReferenceMaterialMappingPlan;
  variantPlan: ReferenceVariantPlan;
  draft: ReferenceDraftPackage;
} {
  const sourceDigest = digest("reference-draft-source");
  const sampleBinding = {
    id: "reference-sample-store-1",
    version: 1,
    customerId: scopeA.customerId,
    sessionId: scopeA.sessionId,
    sourceAssetId: "external-reference-store-1",
    sourceDigest: digest("external-reference-store-1"),
    sourceKind: "external-style-only" as const,
    collectionMode: "dedicated-upload" as const,
    candidateScope: "reference-only" as const,
  };
  const rightsBinding = {
    id: "reference-rights-store-1",
    version: 1,
    customerId: scopeA.customerId,
    sessionId: scopeA.sessionId,
    referenceSampleId: sampleBinding.id,
    referenceSampleVersion: sampleBinding.version,
    referenceSampleDigest: sampleBinding.sourceDigest,
    sourceKind: sampleBinding.sourceKind,
    reusableElements: [],
    contentDigest: digest("reference-rights-store-1"),
  };
  const permission = {
    id: "permission-store-1",
    version: 1,
    epoch: 1,
    customerId: scopeA.customerId,
    sessionId: scopeA.sessionId,
    sourceAssetId: "asset-store-1",
    sourceDigest,
    view: "allowed" as const,
    use: "allowed" as const,
  };
  const { view: _view, use: _use, ...permissionIdentity } = permission;
  const mappingBinding = {
    id: "mapping-plan-store-1",
    version: 1,
    customerId: scopeA.customerId,
    sessionId: scopeA.sessionId,
    contentDigest: digest("mapping-plan-store-1"),
    permissionSetDigest: digest("permission-set-store-1"),
    permissions: [permissionIdentity],
    status: "confirmed" as const,
    confirmation: {
      receiptId: "mapping-confirmation-store-1",
      actorId: scopeA.actorId,
      confirmedAt: now(),
    },
  };
  const commonBindings = {
    sample: sampleBinding,
    rights: rightsBinding,
    analysis: {
      id: "analysis-store-1", version: 1, customerId: scopeA.customerId,
      sessionId: scopeA.sessionId, contentDigest: digest("analysis-store-1"),
    },
    styleSelection: {
      id: "selection-store-1", version: 1, customerId: scopeA.customerId,
      sessionId: scopeA.sessionId, contentDigest: digest("selection-store-1"), status: "confirmed" as const,
      confirmation: { receiptId: "selection-confirmation-store-1", actorId: scopeA.actorId, confirmedAt: now() },
    },
  };
  const mappingPlan: ReferenceMaterialMappingPlan = {
    id: mappingBinding.id,
    customerId: scopeA.customerId,
    sessionId: scopeA.sessionId,
    version: mappingBinding.version,
    contentDigest: mappingBinding.contentDigest,
    permissionSetDigest: mappingBinding.permissionSetDigest,
    bindings: commonBindings,
    mappings: [{
      id: "mapping-row-store-1",
      ruleId: "rule-store-1",
      referenceStartMs: 0,
      referenceEndMs: 30_000,
      targetRule: "reference-store-opening",
      sourceBindingId: "source-binding-store-1",
      sourceAssetId: permission.sourceAssetId,
      sourceDigest,
      sourceStartMs: 0,
      sourceEndMs: 30_000,
      sourceProvenance: "customer-current",
      permission,
      reuseRights: null,
      status: "matched",
      differenceReason: null,
    }],
    unresolvedCount: 0,
    status: "confirmed",
    confirmedByActorId: mappingBinding.confirmation.actorId,
    confirmationReceiptId: mappingBinding.confirmation.receiptId,
    confirmedAt: mappingBinding.confirmation.confirmedAt,
    createdAt: now(),
  };
  const variantPlan: ReferenceVariantPlan = {
    id: "variant-store-1",
    customerId: scopeA.customerId,
    sessionId: scopeA.sessionId,
    version: 1,
    bindings: { ...commonBindings, mappingPlan: mappingBinding },
    kind: "editorial-master",
    authorizedSources: [{
      id: "source-binding-store-1",
      mappingId: "mapping-row-store-1",
      customerId: scopeA.customerId,
      sessionId: scopeA.sessionId,
      sourceAssetId: permission.sourceAssetId,
      sourceDigest,
      provenance: "customer-current",
      permission,
      reuseRights: null,
    }],
    timeline: [{
      id: "timeline-store-1",
      mappingId: "mapping-row-store-1",
      ruleId: "rule-store-1",
      sourceBindingId: "source-binding-store-1",
      sourceStartMs: 0,
      sourceEndMs: 30_000,
      timelineStartMs: 0,
      transition: "cut",
      targetDurationRatio: 1,
      reasonCode: "reference.store.opening",
    }],
    audioOverlay: null,
    platformChangeCodes: [],
    status: "rendered",
    publishClaimed: false,
    createdAt: now(),
  };
  const draft: ReferenceDraftPackage = {
    id: "reference-draft-store-1",
    customerId: scopeA.customerId,
    sessionId: scopeA.sessionId,
    version: 1,
    bindings: {
      mappingPlan: mappingBinding,
      variantPlan: {
        id: "variant-store-1",
        version: 1,
        customerId: scopeA.customerId,
        sessionId: scopeA.sessionId,
        kind: "editorial-master",
      },
    },
    outputDigest: digest("reference-draft-output"),
    outputPath: "/trusted/reference-run/editorial-master.mp4",
    durationSeconds: 30,
    lineage: [{
      id: "lineage-store-1",
      use: "video",
      timelineEntryId: "timeline-store-1",
      mappingId: "mapping-row-store-1",
      sourceBindingId: "source-binding-store-1",
      sourceAssetId: "asset-store-1",
      sourceDigest,
      sourceStartMs: 0,
      sourceEndMs: 30_000,
    }],
    status: "reviewable",
    capability: { mode: "offline-fallback", label: "离线/模拟分析", version: 1, provider: null },
    publishClaimed: false,
    createdAt: now(),
  };
  return { mappingPlan, variantPlan, draft };
}

function sampleFor(scope: StudioScope, suffix: string): ReferenceSample {
  const sourceDigest = digest(`source-${suffix}`);
  return {
    id: `reference-sample-${suffix}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    sourceAssetId: `asset_${sourceDigest}`,
    sourceDigest,
    sourceKind: "self-owned",
    collectionMode: "historical-pointer",
    candidateScope: "reference-only",
    status: "pending-rights",
    viewPermission: "allowed",
    usePermission: "allowed",
    createdAt: now(),
  };
}

function acceptsOutputSnapshot(_snapshot: StudioAnalysisAssetSnapshot): void {}

async function fileTree(directory: string, prefix = ""): Promise<string[]> {
  const paths: string[] = [];
  const entries = await readdir(directory, { withFileTypes: true }).catch(() => []);
  for (const entry of entries) {
    const relative = prefix ? join(prefix, entry.name) : entry.name;
    if (entry.isDirectory()) {
      paths.push(...await fileTree(join(directory, entry.name), relative));
    } else {
      paths.push(relative);
    }
  }
  return paths.sort();
}
