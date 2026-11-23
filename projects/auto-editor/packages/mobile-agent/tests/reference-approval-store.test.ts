import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type {
  ReferenceDraftApproval,
  ReferenceDraftPackage,
  ReferenceExecutionReport,
  ReferenceMaterialMappingPlan,
  ReferenceVariantKind,
  ReferenceVariantPlan,
} from "@wearable/contracts";

import {
  JsonReferenceSampleStore,
  referenceEditorOperationMetadata,
  referenceWorkflowDigest,
  type ReferenceEditorOperation,
  type StudioScope,
} from "../src";

const scope: StudioScope = {
  actorId: "server-reviewer",
  customerId: "customer-approval",
  sessionId: "session-approval",
};
const createdAt = "2026-09-21T01:00:00.000Z";
const digest = (value: string) => createHash("sha256").update(value).digest("hex");

describe("reference draft approval operation storage", () => {
  let root: string;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "reference-approval-store-"));
    await seedApprovalReadySnapshot(root);
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("atomically appends the exact reference approval and its operation receipt", async () => {
    const store = await JsonReferenceSampleStore.open(root, scope);
    const before = await store.snapshot();
    const approval = approvalFixture();
    const operation = approvalOperation(approval, "approve_master_once");
    const nextPayload = {
      ...withoutDigest(before),
      approvals: [...before.approvals, approval],
      editorOperations: [...before.editorOperations, operation],
    };

    await expect(store.commit({
      ...nextPayload,
      digest: referenceWorkflowDigest(nextPayload),
    }, before.digest)).resolves.toBeUndefined();

    const stored = await store.snapshot();
    expect(stored.approvals).toContainEqual(approval);
    expect(stored.editorOperations).toContainEqual(operation);

    await expect(store.commit(stored, stored.digest)).resolves.toBeUndefined();
    expect((await store.snapshot()).editorOperations).toHaveLength(1);
  });

  it("rejects a reference approval without its durable operation receipt", async () => {
    const store = await JsonReferenceSampleStore.open(root, scope);
    const before = await store.snapshot();
    const nextPayload = {
      ...withoutDigest(before),
      approvals: [...before.approvals, approvalFixture()],
    };

    await expect(store.commit({
      ...nextPayload,
      digest: referenceWorkflowDigest(nextPayload),
    }, before.digest)).rejects.toThrow("reference-approval-operation-required");
    await expect(store.snapshot()).resolves.toEqual(before);
  });

  it.each([
    ["reviewer", (approval: ReferenceDraftApproval) => { approval.reviewerId = "client-forged-reviewer"; }],
    ["report", (approval: ReferenceDraftApproval) => { approval.bindings.executionReport.version = 99; }],
    ["mapping digest", (approval: ReferenceDraftApproval) => {
      approval.bindings.mappingPlan.permissionSetDigest = digest("forged-permission-set");
    }],
    ["draft digest", (approval: ReferenceDraftApproval) => {
      approval.bindings.draft.outputDigest = digest("forged-draft");
      approval.fileDigest = approval.bindings.draft.outputDigest;
    }],
  ])("rejects an approval whose exact %s binding was forged", async (_label, mutate) => {
    const store = await JsonReferenceSampleStore.open(root, scope);
    const before = await store.snapshot();
    const approval = approvalFixture();
    mutate(approval);
    const operation = approvalOperation(approval, `approve_forged_${String(_label).replace(/ /g, "_")}`);
    const nextPayload = {
      ...withoutDigest(before),
      approvals: [...before.approvals, approval],
      editorOperations: [...before.editorOperations, operation],
    };

    await expect(store.commit({
      ...nextPayload,
      digest: referenceWorkflowDigest(nextPayload),
    }, before.digest)).rejects.toThrow("reference-operation-result-binding-mismatch");
    await expect(store.snapshot()).resolves.toEqual(before);
  });

  it("rejects reuse of one approval operation key with different bound input", async () => {
    const store = await JsonReferenceSampleStore.open(root, scope);
    const before = await store.snapshot();
    const approval = approvalFixture();
    const first = approvalOperation(approval, "approve_collision");
    const firstPayload = {
      ...withoutDigest(before),
      approvals: [...before.approvals, approval],
      editorOperations: [...before.editorOperations, first],
    };
    await store.commit({
      ...firstPayload,
      digest: referenceWorkflowDigest(firstPayload),
    }, before.digest);

    const committed = await store.snapshot();
    const conflictingApproval = {
      ...approval,
      id: "reference-approval-wechat",
      variant: "wechat-channels" as const,
      bindings: {
        ...approval.bindings,
        draft: {
          id: "draft-wechat-channels",
          version: 1,
          outputDigest: digest("output-wechat-channels"),
        },
      },
      fileDigest: digest("output-wechat-channels"),
    };
    const conflicting = approvalOperation(conflictingApproval, "approve_collision");
    const conflictingPayload = {
      ...withoutDigest(committed),
      approvals: [...committed.approvals, conflictingApproval],
      editorOperations: [...committed.editorOperations, conflicting],
    };

    await expect(store.commit({
      ...conflictingPayload,
      digest: referenceWorkflowDigest(conflictingPayload),
    }, committed.digest)).rejects.toThrow("reference-operation-key-conflict");
  });
});

function approvalFixture(): ReferenceDraftApproval {
  return {
    kind: "reference-draft",
    id: "reference-approval-master",
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    bindings: {
      executionReport: { id: "report-approval", version: 1 },
      draft: {
        id: "draft-editorial-master",
        version: 1,
        outputDigest: digest("output-editorial-master"),
      },
      mappingPlan: {
        id: "mapping-approval",
        version: 1,
        contentDigest: digest("mapping-approval"),
        permissionSetDigest: digest("permission-set-approval"),
      },
    },
    variant: "editorial-master",
    reviewerId: scope.actorId,
    fileDigest: digest("output-editorial-master"),
    approvedAt: createdAt,
    status: "active",
    publishClaimed: false,
  };
}

function approvalOperation(
  approval: ReferenceDraftApproval,
  operationKey: string,
): ReferenceEditorOperation {
  const intent = {
    method: "approveDraft" as const,
    report: approval.bindings.executionReport,
    draft: approval.bindings.draft,
    mappingPlan: approval.bindings.mappingPlan,
    variant: approval.variant,
    fileDigest: approval.fileDigest,
    expectedResult: { id: approval.id, version: approval.version },
  };
  const result = { kind: "approval" as const, id: approval.id, version: approval.version };
  return {
    ...referenceEditorOperationMetadata(scope, "approveDraft", operationKey, intent),
    result,
    resultDigest: digest(canonicalJson(approval)),
  };
}

async function seedApprovalReadySnapshot(root: string): Promise<void> {
  const mappingPlan = mappingFixture();
  const kinds: ReferenceVariantKind[] = ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"];
  const variantPlans = kinds.map((kind) => variantFixture(mappingPlan, kind));
  const drafts = variantPlans.map((variant) => draftFixture(variant));
  const report = reportFixture(mappingPlan, variantPlans, drafts);
  const payload = {
    samples: [],
    rightsDeclarations: [],
    analyses: [],
    styleSelections: [],
    mappingPlans: [mappingPlan],
    variantPlans,
    drafts,
    executionReports: [report],
    approvals: [],
    mobileReviews: [],
    editorOperations: [],
  };
  const directory = join(
    root,
    "reference-workflows",
    digest(scope.customerId),
    digest(scope.sessionId),
  );
  await mkdir(directory, { recursive: true });
  await writeFile(join(directory, "snapshot.json"), `${JSON.stringify({
    ...payload,
    digest: referenceWorkflowDigest(payload),
  })}\n`);
}

function mappingFixture(): ReferenceMaterialMappingPlan {
  const sourceDigest = digest("source-approval");
  const permission = {
    id: "permission-approval",
    version: 1,
    epoch: 1,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    sourceAssetId: "asset-approval",
    sourceDigest,
    view: "allowed" as const,
    use: "allowed" as const,
  };
  return {
    id: "mapping-approval",
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    contentDigest: digest("mapping-approval"),
    permissionSetDigest: digest("permission-set-approval"),
    bindings: {
      sample: {
        id: "sample-approval", version: 1, customerId: scope.customerId, sessionId: scope.sessionId,
        sourceAssetId: "reference-asset-approval", sourceDigest: digest("reference-source-approval"),
        sourceKind: "external-style-only", collectionMode: "dedicated-upload", candidateScope: "reference-only",
      },
      rights: {
        id: "rights-approval", version: 1, customerId: scope.customerId, sessionId: scope.sessionId,
        referenceSampleId: "sample-approval", referenceSampleVersion: 1,
        referenceSampleDigest: digest("reference-source-approval"), sourceKind: "external-style-only",
        reusableElements: [], contentDigest: digest("rights-approval"),
      },
      analysis: {
        id: "analysis-approval", version: 1, customerId: scope.customerId, sessionId: scope.sessionId,
        contentDigest: digest("analysis-approval"),
      },
      styleSelection: {
        id: "selection-approval", version: 1, customerId: scope.customerId, sessionId: scope.sessionId,
        contentDigest: digest("selection-approval"), status: "confirmed",
        confirmation: { receiptId: "selection-receipt", actorId: scope.actorId, confirmedAt: createdAt },
      },
    },
    mappings: [{
      id: "mapping-row-approval",
      ruleId: "rule-approval",
      referenceStartMs: 0,
      referenceEndMs: 30_000,
      targetRule: "approval-opening",
      sourceBindingId: "source-binding-approval",
      sourceAssetId: "asset-approval",
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
    confirmedByActorId: scope.actorId,
    confirmationReceiptId: "mapping-receipt",
    confirmedAt: createdAt,
    createdAt,
  };
}

function mappingBinding(plan: ReferenceMaterialMappingPlan) {
  const permission = plan.mappings[0]!.permission!;
  const { view: _view, use: _use, ...identity } = permission;
  return {
    id: plan.id,
    version: plan.version,
    customerId: plan.customerId,
    sessionId: plan.sessionId,
    contentDigest: plan.contentDigest,
    permissionSetDigest: plan.permissionSetDigest,
    permissions: [identity],
    status: "confirmed" as const,
    confirmation: {
      receiptId: plan.confirmationReceiptId!,
      actorId: plan.confirmedByActorId!,
      confirmedAt: plan.confirmedAt!,
    },
  };
}

function variantFixture(
  mappingPlan: ReferenceMaterialMappingPlan,
  kind: ReferenceVariantKind,
): ReferenceVariantPlan {
  const mapping = mappingPlan.mappings[0]!;
  return {
    id: `variant-${kind}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    bindings: { ...mappingPlan.bindings, mappingPlan: mappingBinding(mappingPlan) },
    kind,
    authorizedSources: [{
      id: mapping.sourceBindingId!,
      mappingId: mapping.id,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      sourceAssetId: mapping.sourceAssetId!,
      sourceDigest: mapping.sourceDigest!,
      provenance: mapping.sourceProvenance!,
      permission: mapping.permission!,
      reuseRights: null,
    }],
    timeline: [{
      id: `timeline-${kind}`,
      mappingId: mapping.id,
      ruleId: mapping.ruleId,
      sourceBindingId: mapping.sourceBindingId!,
      sourceStartMs: 0,
      sourceEndMs: 30_000,
      timelineStartMs: 0,
      transition: "cut",
      targetDurationRatio: 1,
      reasonCode: "approval.timeline",
    }],
    audioOverlay: null,
    platformChangeCodes: kind === "editorial-master" ? [] : ["approval.platform-change"],
    status: "rendered",
    publishClaimed: false,
    createdAt,
  };
}

function draftFixture(variant: ReferenceVariantPlan): ReferenceDraftPackage {
  const source = variant.authorizedSources[0]!;
  const timeline = variant.timeline[0]!;
  return {
    id: `draft-${variant.kind}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    bindings: {
      mappingPlan: variant.bindings.mappingPlan,
      variantPlan: {
        id: variant.id,
        version: variant.version,
        customerId: variant.customerId,
        sessionId: variant.sessionId,
        kind: variant.kind,
      },
    },
    outputDigest: digest(`output-${variant.kind}`),
    outputPath: `/trusted/approval/${variant.kind}.mp4`,
    durationSeconds: 30,
    lineage: [{
      id: `lineage-${variant.kind}`,
      use: "video",
      timelineEntryId: timeline.id,
      mappingId: timeline.mappingId,
      sourceBindingId: source.id,
      sourceAssetId: source.sourceAssetId,
      sourceDigest: source.sourceDigest,
      sourceStartMs: timeline.sourceStartMs,
      sourceEndMs: timeline.sourceEndMs,
    }],
    status: "reviewable",
    capability: { mode: "offline-fallback", label: "离线/模拟分析", version: 1, provider: null },
    publishClaimed: false,
    createdAt,
  };
}

function reportFixture(
  mappingPlan: ReferenceMaterialMappingPlan,
  variants: ReferenceVariantPlan[],
  drafts: ReferenceDraftPackage[],
): ReferenceExecutionReport {
  const section = { status: "matched" as const, summaryCode: "approval.matched", evidenceRefs: ["approval-evidence"] };
  return {
    id: "report-approval",
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    bindings: { ...mappingPlan.bindings, mappingPlan: mappingBinding(mappingPlan) },
    artifacts: variants.map((variant, index) => ({
      kind: variant.kind,
      variantPlanId: variant.id,
      variantPlanVersion: variant.version,
      draftId: drafts[index]!.id,
      draftVersion: drafts[index]!.version,
      outputDigest: drafts[index]!.outputDigest,
    })),
    rhythm: section,
    transitions: section,
    narrative: section,
    audio: section,
    mappingCounts: { matched: 1, substituted: 0, unachievable: 0 },
    platformChanges: [
      { kind: "xiaohongshu", changeCodes: ["approval.xhs"] },
      { kind: "douyin", changeCodes: ["approval.douyin"] },
      { kind: "wechat-channels", changeCodes: ["approval.wechat"] },
    ],
    status: "reviewable",
    publishClaimed: false,
    createdAt,
  };
}

function withoutDigest<T extends { digest: string }>(snapshot: T): Omit<T, "digest"> {
  const { digest: _digest, ...payload } = snapshot;
  return payload;
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value as Record<string, unknown>)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, child]) => [key, canonicalize(child)]));
}
