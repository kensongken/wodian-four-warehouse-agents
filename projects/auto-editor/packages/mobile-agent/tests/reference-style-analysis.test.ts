import { createHash } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type {
  ModelAnalysisReceiptV1,
  ReferenceRightsDeclaration,
  ReferenceSample,
} from "@wearable/contracts";
import { afterEach, describe, expect, it } from "vitest";

import {
  CustomerMaterialLibrary,
  LocalReferenceGrammarAdapter,
  type ReferenceAnalysisAssetSnapshot,
  type StudioScope,
} from "../src";

const scope: StudioScope = {
  actorId: "actor-a",
  customerId: "customer-a",
  sessionId: "session-a",
};
const createdAt = "2026-09-19T08:00:00.000Z";

describe("reference style analysis", () => {
  const cleanup: string[] = [];

  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("returns warnings instead of invented style when transcript, audio, and vision evidence are absent", async () => {
    const fixture = await referenceFixture(cleanup);
    const adapter = new LocalReferenceGrammarAdapter({ now: () => createdAt });
    const analysis = await adapter.extractGrammar({
      scope,
      sample: fixture.sample,
      rights: fixture.rights,
      source: fixture.source,
      receipts: [sceneReceipt(fixture.source)],
    });

    expect(analysis.rules.some((rule) => rule.kind === "rhythm")).toBe(true);
    expect(analysis.rules.some((rule) => rule.kind === "transition")).toBe(true);
    expect(analysis.rules.filter((rule) => rule.kind === "transition").every((rule) =>
      rule.referenceEndMs - rule.referenceStartMs >= 350)).toBe(true);
    expect(analysis.rules.some((rule) => rule.kind === "narrative")).toBe(false);
    expect(analysis.rules.some((rule) => rule.kind === "audio")).toBe(false);
    expect(analysis.transcriptSpans).toEqual([]);
    expect(analysis.beatMarkers).toEqual([]);
    expect(analysis.warnings).toEqual(expect.arrayContaining([
      "reference-transcript-evidence-missing",
      "reference-audio-evidence-missing",
      "reference-vision-evidence-missing",
    ]));
    expect(analysis.status).toBe("degraded");
  });

  it("keeps low-confidence supported transition classifications non-executable", async () => {
    const fixture = await referenceFixture(cleanup);
    const receipt = sceneReceipt(fixture.source, {
      transitionEvidence: [{ atMs: 1_000, kind: "fade", confidence: 0.59 }],
    });

    const analysis = await new LocalReferenceGrammarAdapter({ now: () => createdAt }).extractGrammar({
      scope,
      sample: fixture.sample,
      rights: fixture.rights,
      source: fixture.source,
      receipts: [receipt],
    });

    expect(analysis.transitions).toEqual([
      expect.objectContaining({ atMs: 1_000, kind: "unsupported", confidence: 0.59 }),
    ]);
    expect(analysis.rules.some((rule) => rule.kind === "transition")).toBe(false);
  });

  it.each([1, 1_999])("keeps a transition window executable and inside source bounds at %dms", async (atMs) => {
    const fixture = await referenceFixture(cleanup);
    const analysis = await new LocalReferenceGrammarAdapter({ now: () => createdAt }).extractGrammar({
      scope,
      sample: fixture.sample,
      rights: fixture.rights,
      source: fixture.source,
      receipts: [sceneReceipt(fixture.source, {
        transitionEvidence: [{ atMs, kind: "fade", confidence: 0.9 }],
      })],
    });

    const rule = analysis.rules.find((candidate) => candidate.kind === "transition");
    expect(rule).toBeDefined();
    expect(rule!.referenceStartMs).toBeGreaterThanOrEqual(0);
    expect(rule!.referenceEndMs).toBeLessThanOrEqual(2_000);
    expect(rule!.referenceEndMs - rule!.referenceStartMs).toBe(350);
  });

  it.each([
    ["empty", []],
    ["all unsupported", [{ atMs: 1_000, kind: "unsupported", confidence: 0.4 }]],
  ])("degrades when transition evidence is %s", async (_name, transitionEvidence) => {
    const fixture = await referenceFixture(cleanup);
    const analysis = await new LocalReferenceGrammarAdapter().extractGrammar({
      scope, sample: fixture.sample, rights: fixture.rights, source: fixture.source,
      receipts: [sceneReceipt(fixture.source, { transitionEvidence })],
    });

    expect(analysis.rules.some((rule) => rule.kind === "transition")).toBe(false);
    expect(analysis.warnings).toContain("reference-transition-evidence-missing");
    expect(analysis.status).toBe("degraded");
  });

  it("creates only evidence-backed complete rules when every optional receipt is present", async () => {
    const fixture = await referenceFixture(cleanup);
    const scene = sceneReceipt(fixture.source, { beatMarkers: [250, 750, 1_250] });
    const transcript = receiptFor(fixture.source, "transcript", {
      spans: [{ startMs: 100, endMs: 800, text: "展示戒指" }],
    }, { confidence: 0.88 });
    const vision = receiptFor(fixture.source, "vision", {
      visualObservations: [{
        text: "戒指特写",
        startMs: 900,
        endMs: 1_600,
        confidence: 0.81,
        status: "pending-human-confirmation",
      }],
    }, { observations: ["pending-visual-observation:戒指特写"], confidence: 0.81 });

    const analysis = await new LocalReferenceGrammarAdapter({ now: () => createdAt }).extractGrammar({
      scope,
      sample: fixture.sample,
      rights: fixture.rights,
      source: fixture.source,
      receipts: [scene, transcript, vision],
    });

    expect(analysis.status).toBe("complete");
    expect(analysis.warnings).toEqual([]);
    expect(new Set(analysis.rules.map((rule) => rule.kind))).toEqual(
      new Set(["rhythm", "transition", "narrative", "audio"]),
    );
    expect(analysis.bindings.sample.version).toBe(fixture.sample.version);
    expect(analysis.bindings.rights.version).toBe(fixture.rights.version);
    expect(analysis.confidence).toBe(0.81);
  });

  it("keeps bounded worker truncation visible as degraded evidence", async () => {
    const fixture = await referenceFixture(cleanup);
    const scene = sceneReceipt(fixture.source, {
      beatMarkers: [250],
      audioEvidenceTruncated: true,
      transitionEvidenceTruncated: true,
    });
    const transcript = receiptFor(fixture.source, "transcript", {
      spans: [{ startMs: 100, endMs: 800, text: "展示戒指" }],
    });
    const vision = receiptFor(fixture.source, "vision", {
      visualObservations: [{
        text: "戒指特写", startMs: 900, endMs: 1_600, confidence: 0.81,
        status: "pending-human-confirmation",
      }],
    }, { observations: ["pending-visual-observation:戒指特写"] });

    const analysis = await new LocalReferenceGrammarAdapter().extractGrammar({
      scope, sample: fixture.sample, rights: fixture.rights, source: fixture.source,
      receipts: [scene, transcript, vision],
    });

    expect(analysis.status).toBe("degraded");
    expect(analysis.warnings).toEqual(expect.arrayContaining([
      "reference-audio-evidence-truncated",
      "reference-transition-evidence-truncated",
    ]));
  });

  it.each([
    ["customer", { customerId: "customer-b" }],
    ["session", { sessionId: "session-b" }],
    ["asset", { sourceAssetId: "asset-other" }],
    ["digest", { sourceDigest: digest("other") }],
    ["path", { sourcePath: "/tmp/not-the-authorized-source" }],
    ["start", { sourceStartMs: 1 }],
    ["end", { sourceEndMs: 1_999 }],
  ])("fails closed for a receipt with the wrong %s binding", async (_name, override) => {
    const fixture = await referenceFixture(cleanup);
    const receipt = { ...sceneReceipt(fixture.source), ...override } as ModelAnalysisReceiptV1;

    await expect(new LocalReferenceGrammarAdapter().extractGrammar({
      scope,
      sample: fixture.sample,
      rights: fixture.rights,
      source: fixture.source,
      receipts: [receipt],
    })).rejects.toThrow("reference-receipt-binding-mismatch");
  });

  it("fails closed for a non-success receipt instead of treating it as missing evidence", async () => {
    const fixture = await referenceFixture(cleanup);
    const receipt: ModelAnalysisReceiptV1 = {
      ...sceneReceipt(fixture.source),
      status: "failed-manual-review",
      failure: { code: "scene-analysis-failed" },
      confidence: 0,
    };

    await expect(new LocalReferenceGrammarAdapter().extractGrammar({
      scope,
      sample: fixture.sample,
      rights: fixture.rights,
      source: fixture.source,
      receipts: [receipt],
    })).rejects.toThrow("reference-receipt-not-succeeded");
  });

  it.each([
    ["synthetic receipt", { synthetic: true }],
    ["offline fallback", { providerMode: "offline-fallback" as const }],
  ])("rejects %s as real reference evidence", async (_name, override) => {
    const fixture = await referenceFixture(cleanup);
    const receipt = { ...sceneReceipt(fixture.source), ...override } as ModelAnalysisReceiptV1;

    await expect(new LocalReferenceGrammarAdapter().extractGrammar({
      scope,
      sample: fixture.sample,
      rights: fixture.rights,
      source: fixture.source,
      receipts: [receipt],
    })).rejects.toThrow("reference-receipt-real-local-evidence-required");
  });
});

async function referenceFixture(cleanup: string[]): Promise<{
  source: ReferenceAnalysisAssetSnapshot;
  sample: ReferenceSample;
  rights: ReferenceRightsDeclaration;
}> {
  const root = await mkdtemp(join(tmpdir(), "reference-style-analysis-"));
  cleanup.push(root);
  const library = new CustomerMaterialLibrary(join(root, "library"), () => true);
  const asset = await library.importOriginal(scope.actorId, scope.customerId, {
    requestId: `reference-${cleanup.length}`,
    bytes: new TextEncoder().encode(`authorized-reference-${cleanup.length}`),
    mediaKind: "video",
    fileName: "reference.mov",
    capturedAt: null,
    sessionId: scope.sessionId,
    viewPermission: "allowed",
    usePermission: "review-required",
  });
  const source = await library.inspectReferenceAnalysisAsset(
    scope.actorId,
    scope.customerId,
    scope.sessionId,
    asset.id,
  );
  const sample: ReferenceSample = {
    id: "reference-sample-1",
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 2,
    sourceAssetId: asset.id,
    sourceDigest: asset.digest,
    sourceKind: "external-style-only",
    collectionMode: "dedicated-upload",
    candidateScope: "reference-only",
    status: "ready",
    viewPermission: "allowed",
    usePermission: "review-required",
    createdAt,
  };
  const rights: ReferenceRightsDeclaration = {
    id: "reference-rights-1",
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 3,
    contentDigest: digest("rights-v3"),
    sample: {
      id: sample.id,
      version: sample.version,
      customerId: sample.customerId,
      sessionId: sample.sessionId,
      sourceAssetId: sample.sourceAssetId,
      sourceDigest: sample.sourceDigest,
      sourceKind: sample.sourceKind,
      collectionMode: sample.collectionMode,
      candidateScope: sample.candidateScope,
    },
    sourceKind: sample.sourceKind,
    reusableElements: [],
    actorId: scope.actorId,
    createdAt,
  };
  return { source, sample, rights };
}

function sceneReceipt(
  source: ReferenceAnalysisAssetSnapshot,
  qualityOverrides: Record<string, unknown> = {},
): ModelAnalysisReceiptV1 {
  return receiptFor(source, "scene", {
    scenes: [
      { startMs: 0, endMs: 1_000, representativeFrameMs: 500, representativeFrameDigest: digest("frame-1") },
      { startMs: 1_000, endMs: 2_000, representativeFrameMs: 1_500, representativeFrameDigest: digest("frame-2") },
    ],
    transitionEvidence: [{ atMs: 1_000, kind: "cut", confidence: 0.93 }],
    beatMarkers: [],
    ...qualityOverrides,
  });
}

function receiptFor(
  source: ReferenceAnalysisAssetSnapshot,
  layer: ModelAnalysisReceiptV1["layer"],
  qualitySignals: Record<string, unknown>,
  override: Partial<ModelAnalysisReceiptV1> = {},
): ModelAnalysisReceiptV1 {
  return {
    schemaVersion: 1,
    jobId: `job-${layer}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    sourceAssetId: source.asset.id,
    sourceDigest: source.asset.digest,
    sourcePath: source.sourcePath,
    sourceStartMs: 0,
    sourceEndMs: 2_000,
    layer,
    operationKey: `operation-${layer}`,
    adapterName: `adapter-${layer}`,
    modelName: `model-${layer}`,
    modelVersion: "v1",
    checkpointDigest: null,
    inputDigest: digest(`input-${layer}`),
    promptVersion: `prompt-${layer}`,
    observations: [],
    suggestedTags: [],
    qualitySignals,
    confidence: 0.9,
    warnings: [],
    startedAt: createdAt,
    completedAt: createdAt,
    runtimeMs: 1,
    status: "succeeded",
    failure: null,
    synthetic: false,
    providerMode: "local",
    ...override,
  };
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}
