import { chmod, mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { runReferenceGuidedEditingPilot } from "../src/reference-guided-editing-pilot";

type LocalRunRecord = Readonly<{
  attempt: Readonly<{ id: string; runtimeRoot: string }>;
  inputs: ReadonlyArray<Readonly<{ path: string; digest: string }>>;
  outputs: ReadonlyArray<Readonly<{
    kind: string;
    path: string;
    digest: string;
    lineage: ReadonlyArray<Readonly<{ sourceDigest: string }>>;
    artifact: Readonly<{
      kind: string;
      variantPlanId: string;
      variantPlanVersion: number;
      draftId: string;
      draftVersion: number;
      outputDigest: string;
    }>;
    variantPlan: Readonly<{ id: string; version: number; contentDigest: string; status: string }>;
    draft: Readonly<{ id: string; version: number; status: string; outputDigest: string }>;
    executionReport: Readonly<{
      id: string;
      version: number;
      status: string;
      bindings: Readonly<{ mappingPlan: Readonly<{ id: string; version: number }> }>;
    }>;
  }>>;
  versionBindings: Readonly<{
    styleSelection: Readonly<{ id: string; version: number }>;
    mappingPlan: Readonly<{ id: string; version: number }>;
    executionReport: Readonly<{ id: string; version: number }>;
  }>;
  analysisPipeline: Readonly<{
    job: Readonly<{
      status: string;
      sourceAuthorization: string;
      layers: readonly string[];
      attempt: number;
      receiptJobIds: readonly string[];
    }>;
    allReceipts: ReadonlyArray<Readonly<{
      jobId: string;
      layer: string;
      status: string;
      providerMode: string;
      synthetic: boolean;
      failure: Readonly<{ code: string }> | null;
    }>>;
    evidenceReceiptIds: readonly string[];
    analysis: Readonly<{
      status: string;
      warnings: readonly string[];
      capability: Readonly<{ mode: string }>;
      rules: ReadonlyArray<Readonly<{ id: string; kind: string }>>;
    }>;
  }>;
  materialPipeline: ReadonlyArray<Readonly<{
    job: Readonly<{ status: string; sourceAuthorization: string; layers: readonly string[] }>;
    receipts: ReadonlyArray<Readonly<{
      layer: string;
      status: string;
      providerMode: string;
      synthetic: boolean;
      qualitySignals: Readonly<Record<string, unknown>>;
    }>>;
    card: Readonly<{
      id: string;
      sourceAssetId: string;
      sourceDigest: string;
      technicalUsable: boolean;
      technicalFlags: readonly string[];
      observations: readonly string[];
      confirmedFacts: readonly string[];
    }>;
    evidence: ReadonlyArray<Readonly<{
      kind: string;
      value: string;
      receiptJobId: string | null;
    }>>;
  }>>;
  mappingCounts: Readonly<{ matched: number; substituted: number; unachievable: number }>;
  mappings: ReadonlyArray<Readonly<{
    status: string;
    ruleId: string;
    sourceBindingId: string | null;
    sourceAssetId: string | null;
    sourceDigest: string | null;
    permission: unknown | null;
    differenceReason: string | null;
  }>>;
  invalidationEvidence: Readonly<{
    priorStyleSelection: Readonly<{ id: string; version: number; status: string }>;
    replacementStyleSelection: Readonly<{ id: string; version: number; status: string }>;
    mappingPlan: Readonly<{
      prior: Readonly<{ id: string; version: number; status: string }>;
      tombstone: Readonly<{ id: string; version: number; status: string }>;
    }>;
    executionReport: Readonly<{
      prior: Readonly<{ id: string; version: number; status: string }>;
      tombstone: Readonly<{ id: string; version: number; status: string }>;
    }>;
    drafts: ReadonlyArray<Readonly<{
      prior: Readonly<{ id: string; version: number; status: string; outputDigest: string }>;
      tombstone: Readonly<{ id: string; version: number; status: string; outputDigest: string }>;
    }>>;
    finalSnapshotVerified: boolean;
  }>;
  revisionEvidence: Readonly<{
    prompt: "开头缩短 1 秒";
    targetKind: "wechat-channels";
    priorReport: Readonly<{ id: string; version: number }>;
    priorDraft: Readonly<{ id: string; version: number; outputDigest: string }>;
    revisedReport: Readonly<{ id: string; version: number }>;
    revisedDraft: Readonly<{ id: string; version: number; outputDigest: string }>;
  }>;
  approvalEvidence: ReadonlyArray<Readonly<{
    kind: "editorial-master" | "xiaohongshu" | "douyin" | "wechat-channels";
    approval: Readonly<{ id: string; status: "active"; publishClaimed: false }>;
    draftBinding: Readonly<{ id: string; version: number; outputDigest: string }>;
    exactReplay: true;
  }>>;
  mobileApprovalEvidence: Readonly<{
    status: "approved";
    approvedArtifactKinds: readonly string[];
    publishClaimed: false;
  }>;
  withdrawalEvidence: Readonly<{
    sourceAssetId: string;
    withdrawal: Readonly<{
      assetId: string;
      actorId: string;
      customerId: string;
      action: "withdrawn";
    }>;
    approvalAuditInvalidated: true;
    failClosedErrors: Readonly<{
      mobileReview: "reference-workflow-asset-withdrawn";
      revise: "reference-workflow-asset-withdrawn";
      approve: "reference-workflow-asset-withdrawn";
      media: "reference-workflow-asset-withdrawn";
    }>;
  }>;
}>;

async function readRunRecord(path: string): Promise<LocalRunRecord> {
  return JSON.parse(await readFile(path, "utf8")) as LocalRunRecord;
}

describe("reference guided editing synthetic pilot", () => {
  const cleanup: string[] = [];

  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("proves a synthetic reference journey without claiming production", async () => {
    const workDir = await mkdtemp(join(tmpdir(), "reference-guided-editing-pilot-"));
    cleanup.push(workDir);

    const report = await runReferenceGuidedEditingPilot(workDir);

    expect(report.mappingCounts).toEqual({ matched: 2, substituted: 1, unachievable: 1 });
    expect(report.variants.map((item) => item.kind)).toEqual([
      "editorial-master",
      "xiaohongshu",
      "douyin",
      "wechat-channels",
    ]);
    expect(report.variants.every((item) => item.ffprobeDecodable)).toBe(true);
    expect(report.referenceFixture).toMatchObject({
      hardCut: true,
      fade: true,
      crossfade: true,
      audioPulseCount: 8,
    });
    expect(report.lineageExact).toBe(true);
    expect(report.referenceBytesReused).toBe(false);
    expect(report.oldDraftsInvalidatedAfterStyleChange).toBe(true);
    expect(report.platformRevisionExact).toBe(true);
    expect(report.allDraftsApproved).toBe(true);
    expect(report.approvalReplayExact).toBe(true);
    expect(report.withdrawalFailClosed).toBe(true);
    expect(report.publishedClaimed).toBe(false);
    expect(report.evidenceScope).toBe("local-synthetic");

    const localRunRecord = await readRunRecord(report.runRecordPath);
    expect(localRunRecord.inputs).toHaveLength(5);
    expect(localRunRecord.outputs).toHaveLength(4);
    expect(localRunRecord.inputs.every((item) =>
      typeof item.path === "string" && typeof item.digest === "string")).toBe(true);
    expect(localRunRecord.outputs.every((item) =>
      typeof item.path === "string" && typeof item.digest === "string")).toBe(true);
    expect(new Set(localRunRecord.outputs.map((item) => item.digest)).size).toBe(4);
    const referenceDigest = localRunRecord.inputs[0]?.digest;
    expect(localRunRecord.outputs.every((item) =>
      Array.isArray(item.lineage) && item.lineage.length > 0
      && item.lineage.every((row) => row.sourceDigest !== referenceDigest))).toBe(true);
    expect(localRunRecord.analysisPipeline.job).toMatchObject({
      status: "succeeded",
      sourceAuthorization: "reference-view-authorized",
      layers: ["scene", "transcript", "vision"],
      attempt: 1,
    });
    expect(localRunRecord.analysisPipeline.allReceipts.map((receipt) => [
      receipt.layer,
      receipt.status,
      receipt.providerMode,
      receipt.synthetic,
    ])).toEqual([
      ["scene", "succeeded", "local", false],
      ["transcript", "failed-manual-review", "offline-fallback", true],
      ["vision", "failed-manual-review", "offline-fallback", true],
    ]);
    expect(localRunRecord.analysisPipeline.allReceipts.slice(1).every((receipt) =>
      receipt.failure?.code === "studio-model-adapter-not-configured")).toBe(true);
    expect(localRunRecord.analysisPipeline.evidenceReceiptIds).toEqual([
      localRunRecord.analysisPipeline.allReceipts[0]?.jobId,
    ]);
    expect(localRunRecord.analysisPipeline.analysis).toMatchObject({
      status: "degraded",
      capability: { mode: "provider" },
    });
    expect(localRunRecord.analysisPipeline.analysis.warnings).toEqual(expect.arrayContaining([
      "reference-transcript-evidence-missing",
      "reference-vision-evidence-missing",
    ]));
    expect(localRunRecord.materialPipeline).toHaveLength(4);
    for (const material of localRunRecord.materialPipeline) {
      expect(material.job).toMatchObject({
        status: "succeeded",
        sourceAuthorization: "output-use-authorized",
        layers: ["technical", "scene"],
      });
      expect(material.receipts.map((receipt) => [
        receipt.layer,
        receipt.status,
        receipt.providerMode,
        receipt.synthetic,
      ])).toEqual([
        ["technical", "succeeded", "local", false],
        ["scene", "succeeded", "local", false],
      ]);
      expect(material.card).toMatchObject({ technicalUsable: true });
      expect(material.card.technicalFlags).toContain("verified-technical-evidence");
      expect(material.evidence.some((row) =>
        row.kind === "verified-technical-fact" && row.receiptJobId !== null)).toBe(true);
    }
    expect(localRunRecord.mappingCounts).toEqual(report.mappingCounts);
    expect(localRunRecord.mappingCounts).toEqual({ matched: 2, substituted: 1, unachievable: 1 });
    expect(localRunRecord.mappings).toHaveLength(localRunRecord.analysisPipeline.analysis.rules.length);
    expect(localRunRecord.mappings).toHaveLength(
      Object.values(localRunRecord.mappingCounts).reduce((sum, count) => sum + count, 0),
    );
    for (const status of ["matched", "substituted", "unachievable"] as const) {
      expect(localRunRecord.mappings.filter((mapping) => mapping.status === status)).toHaveLength(
        localRunRecord.mappingCounts[status],
      );
    }
    const matched = localRunRecord.mappings.filter((mapping) => mapping.status === "matched");
    for (const mapping of matched) {
      const material = localRunRecord.materialPipeline.find((candidate) =>
        candidate.card.sourceAssetId === mapping.sourceAssetId);
      expect(material?.card.confirmedFacts.some((fact) => fact.endsWith(mapping.ruleId))).toBe(true);
      expect(material?.evidence.some((row) =>
        row.kind === "human-confirmed-fact" && row.value.endsWith(mapping.ruleId))).toBe(true);
    }
    const substituted = localRunRecord.mappings.find((mapping) => mapping.status === "substituted");
    expect(substituted).toMatchObject({
      differenceReason: "authorized-material-content-differs",
    });
    expect(substituted?.sourceBindingId).not.toBeNull();
    expect(substituted?.sourceAssetId).not.toBeNull();
    expect(substituted?.sourceDigest).not.toBeNull();
    expect(substituted?.permission).not.toBeNull();
    const substituteMaterial = localRunRecord.materialPipeline.find((candidate) =>
      candidate.card.sourceAssetId === substituted?.sourceAssetId);
    expect(substituteMaterial?.card.confirmedFacts).toEqual([]);
    expect(substituteMaterial?.card.observations).toContain("transition-kind:fade");
    expect(substituteMaterial?.receipts.find((receipt) => receipt.layer === "scene")?.qualitySignals)
      .toMatchObject({ transitionEvidence: expect.arrayContaining([
        expect.objectContaining({ kind: "fade" }),
      ]) });
    expect(localRunRecord.mappings.find((mapping) => mapping.status === "unachievable")).toMatchObject({
      sourceBindingId: null,
      sourceAssetId: null,
      sourceDigest: null,
      permission: null,
      differenceReason: "no-authorized-material",
    });
    expect(localRunRecord.revisionEvidence).toMatchObject({
      prompt: "开头缩短 1 秒",
      targetKind: "wechat-channels",
    });
    expect(localRunRecord.revisionEvidence.revisedReport.version)
      .toBeGreaterThan(localRunRecord.revisionEvidence.priorReport.version);
    expect(localRunRecord.revisionEvidence.revisedDraft.id)
      .not.toBe(localRunRecord.revisionEvidence.priorDraft.id);
    expect(localRunRecord.approvalEvidence.map((item) => item.kind)).toEqual([
      "editorial-master",
      "xiaohongshu",
      "douyin",
      "wechat-channels",
    ]);
    expect(localRunRecord.approvalEvidence.every((item) =>
      item.approval.status === "active"
      && item.approval.publishClaimed === false
      && item.exactReplay === true)).toBe(true);
    expect(localRunRecord.mobileApprovalEvidence).toEqual({
      status: "approved",
      approvedArtifactKinds: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"],
      publishClaimed: false,
    });
    expect(localRunRecord.withdrawalEvidence.withdrawal).toMatchObject({
      assetId: localRunRecord.withdrawalEvidence.sourceAssetId,
      action: "withdrawn",
    });
    expect(localRunRecord.withdrawalEvidence.approvalAuditInvalidated).toBe(true);
    expect(localRunRecord.withdrawalEvidence.failClosedErrors).toEqual({
      mobileReview: "reference-workflow-asset-withdrawn",
      revise: "reference-workflow-asset-withdrawn",
      approve: "reference-workflow-asset-withdrawn",
      media: "reference-workflow-asset-withdrawn",
    });
  }, 300_000);

  it("isolates two runs that share one durable evidence root", async () => {
    const workDir = await mkdtemp(join(tmpdir(), "reference-guided-editing-pilot-rerun-"));
    cleanup.push(workDir);

    const firstReport = await runReferenceGuidedEditingPilot(workDir);
    const firstRecord = await readRunRecord(firstReport.runRecordPath);
    const secondReport = await runReferenceGuidedEditingPilot(workDir);
    const secondRecord = await readRunRecord(secondReport.runRecordPath);

    expect(secondReport.mappingCounts).toEqual(firstReport.mappingCounts);
    expect(secondReport.variants).toEqual(firstReport.variants);
    expect(secondReport.runRecordPath).toBe(firstReport.runRecordPath);
    expect(secondRecord.inputs.map((item) => item.digest)).toEqual(firstRecord.inputs.map((item) => item.digest));
    expect(secondRecord.outputs.map((item) => [item.kind, item.digest]))
      .toEqual(firstRecord.outputs.map((item) => [item.kind, item.digest]));
    expect(secondRecord.attempt.id).not.toBe(firstRecord.attempt.id);
    expect(secondRecord.attempt.runtimeRoot).not.toBe(firstRecord.attempt.runtimeRoot);
    expect(firstRecord.inputs.every((item) => item.path.startsWith(`${firstRecord.attempt.runtimeRoot}/`))).toBe(true);
    expect(firstRecord.outputs.every((item) => item.path.startsWith(`${firstRecord.attempt.runtimeRoot}/`))).toBe(true);
    expect(secondRecord.inputs.every((item) => item.path.startsWith(`${secondRecord.attempt.runtimeRoot}/`))).toBe(true);
    expect(secondRecord.outputs.every((item) => item.path.startsWith(`${secondRecord.attempt.runtimeRoot}/`))).toBe(true);
  }, 300_000);

  it("records exact artifact bindings and persisted invalidation tombstones", async () => {
    const workDir = await mkdtemp(join(tmpdir(), "reference-guided-editing-pilot-audit-"));
    cleanup.push(workDir);

    const report = await runReferenceGuidedEditingPilot(workDir);
    const record = await readRunRecord(report.runRecordPath);

    expect(record.outputs).toHaveLength(4);
    for (const output of record.outputs) {
      expect(output.artifact).toMatchObject({
        kind: output.kind,
        variantPlanId: output.variantPlan.id,
        variantPlanVersion: output.variantPlan.version,
        draftId: output.draft.id,
        draftVersion: output.draft.version,
        outputDigest: output.digest,
      });
      expect(output.variantPlan).toMatchObject({ status: "rendered" });
      expect(output.draft).toMatchObject({ status: "reviewable", outputDigest: output.digest });
      expect(output.executionReport).toMatchObject({
        id: record.versionBindings.executionReport.id,
        version: record.versionBindings.executionReport.version,
        status: "reviewable",
      });
      expect(output.executionReport.bindings.mappingPlan).toMatchObject(record.versionBindings.mappingPlan);
    }
    expect(record.invalidationEvidence.priorStyleSelection).toMatchObject({
      ...record.versionBindings.styleSelection,
      status: "confirmed",
    });
    expect(record.invalidationEvidence.replacementStyleSelection).toMatchObject({ status: "confirmed" });
    expect(record.invalidationEvidence.replacementStyleSelection.id)
      .not.toBe(record.invalidationEvidence.priorStyleSelection.id);
    expect(record.invalidationEvidence.mappingPlan.prior).toMatchObject({
      ...record.versionBindings.mappingPlan,
      status: "confirmed",
    });
    expect(record.invalidationEvidence.mappingPlan.tombstone).toMatchObject({
      id: record.invalidationEvidence.mappingPlan.prior.id,
      version: record.invalidationEvidence.mappingPlan.prior.version + 1,
      status: "invalidated",
    });
    expect(record.invalidationEvidence.executionReport.prior).toMatchObject({
      ...record.versionBindings.executionReport,
      status: "reviewable",
    });
    expect(record.invalidationEvidence.executionReport.tombstone).toMatchObject({
      id: record.invalidationEvidence.executionReport.prior.id,
      version: record.invalidationEvidence.executionReport.prior.version + 1,
      status: "invalidated",
    });
    expect(record.invalidationEvidence.drafts).toHaveLength(4);
    for (const output of record.outputs) {
      const evidence = record.invalidationEvidence.drafts.find((item) => item.prior.id === output.draft.id);
      expect(evidence?.prior).toEqual(output.draft);
      expect(evidence?.tombstone).toMatchObject({
        version: output.draft.version,
        status: "invalidated",
        outputDigest: output.digest,
      });
      expect(evidence?.tombstone.id).not.toBe(output.draft.id);
    }
    expect(record.invalidationEvidence.finalSnapshotVerified).toBe(true);
  }, 300_000);

  it("replaces a permissive durable record with mode 0600", async () => {
    const workDir = await mkdtemp(join(tmpdir(), "reference-guided-editing-pilot-mode-"));
    cleanup.push(workDir);
    const evidenceRoot = join(workDir, "evidence");
    const runRecordPath = join(evidenceRoot, "reference-guided-editing-run.json");
    await mkdir(evidenceRoot, { recursive: true });
    await writeFile(runRecordPath, "stale\n", "utf8");
    await chmod(runRecordPath, 0o644);

    const report = await runReferenceGuidedEditingPilot(workDir);

    expect(report.runRecordPath).toBe(runRecordPath);
    expect((await stat(runRecordPath)).mode & 0o777).toBe(0o600);
  }, 300_000);
});
