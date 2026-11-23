import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type {
  ContentDemand,
  MaterialCard,
  MaterialInventory,
  ModelAnalysisReceiptV1,
  StudioAnalysisLayer,
  StudioMediaAsset,
} from "@wearable/contracts";
import { StudioEvidenceDegradedReasonSchema } from "@wearable/contracts";
import { afterEach, describe, expect, it } from "vitest";

import {
  CustomerMaterialLibrary,
  OFFLINE_STUDIO_CAPABILITY,
  OfflineFixtureUnderstandingAdapter,
  PrivateContentStudio,
  STUDIO_REQUIRED_EVIDENCE_LAYERS,
  assessDemandFromEvidence,
  mergeStudioEvidence,
  studioProviderCapability,
  type StudioScope,
} from "../src";

const scope: StudioScope = {
  actorId: "actor-a",
  customerId: "customer-a",
  sessionId: "session-a",
};
const createdAt = "2026-09-12T10:00:00.000Z";
const sourceDigest = "a".repeat(64);
const requiredLayers = ["technical", "scene", "transcript", "embedding", "vision"] as const;

describe("studio evidence merger", () => {
  const cleanup: string[] = [];

  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("keeps vision, transcript, and tag claims pending while only explicit human facts become confirmed", () => {
    const withoutHumanReview = mergeStudioEvidence(mergeInput());

    expect(withoutHumanReview.card.confirmedFacts).toEqual([]);
    expect(withoutHumanReview.card.pendingFacts).toEqual(expect.arrayContaining([
      "18K gold",
      "designer explains the ring",
      "设计师讲解戒指灵感",
      "luxury",
      "technical-tag-must-remain-pending",
    ]));
    expect(withoutHumanReview.card.observations).toEqual(expect.arrayContaining([
      "duration-ms:12000",
      "video-stream:present",
      "frame-size:1920x1080",
      "scene-count:1",
    ]));
    expect(withoutHumanReview.card.observations).not.toContain("technical receipt says 18K gold");
    expect(withoutHumanReview.evidence.find((item) => item.value === "scene-count:1"))
      .toMatchObject({ layer: "scene", receiptJobId: "job-scene" });

    const reviewed = mergeStudioEvidence(mergeInput({ humanFacts: ["18K gold"] }));
    expect(reviewed.card.confirmedFacts).toEqual(["18K gold"]);
    expect(reviewed.card.pendingFacts).not.toContain("18K gold");
    expect(reviewed.evidence.find((item) => item.value === "18K gold" && item.kind === "human-confirmed-fact"))
      .toMatchObject({ sourceAssetId: "asset-a", sourceStartMs: 0, sourceEndMs: 12_000 });
  });

  it("keeps a receipt-verified transition kind as scene evidence without promoting it to a human fact", () => {
    const input = mergeInput();
    input.receipts = input.receipts.map((receiptValue) => receiptValue.layer === "scene"
      ? {
          ...receiptValue,
          qualitySignals: {
            ...receiptValue.qualitySignals,
            transitionEvidence: [{ atMs: 8_200, kind: "fade", confidence: 0.9 }],
          },
        }
      : receiptValue);

    const merged = mergeStudioEvidence(input);

    expect(merged.card.confirmedFacts).toEqual([]);
    expect(merged.card.observations).toContain("transition-kind:fade");
    expect(merged.evidence.find((item) => item.value === "transition-kind:fade")).toMatchObject({
      kind: "verified-technical-fact",
      layer: "scene",
      receiptJobId: "job-scene",
    });
  });

  it("rejects every failed, stale, or cross-scope receipt instead of partially merging it", () => {
    const mutations: Array<readonly [string, (receipt: ModelAnalysisReceiptV1) => ModelAnalysisReceiptV1]> = [
      ["customer", (receipt) => ({ ...receipt, customerId: "customer-b" })],
      ["session", (receipt) => ({ ...receipt, sessionId: "session-b" })],
      ["asset", (receipt) => ({ ...receipt, sourceAssetId: "asset-b" })],
      ["digest", (receipt) => ({ ...receipt, sourceDigest: "b".repeat(64) })],
      ["path", (receipt) => ({ ...receipt, sourcePath: "/private/swapped.mp4" })],
      ["start", (receipt) => ({ ...receipt, sourceStartMs: 1 })],
      ["end", (receipt) => ({ ...receipt, sourceEndMs: 11_999 })],
      ["failed", (receipt) => ({
        ...receipt,
        status: "failed-manual-review",
        failure: { code: "fixture-failed" },
        observations: [],
        suggestedTags: [],
        qualitySignals: {},
        confidence: 0,
      })],
    ];

    for (const [name, mutate] of mutations) {
      const input = mergeInput();
      input.receipts = input.receipts.map((receiptValue, index) =>
        index === 0 ? mutate(receiptValue) : receiptValue);
      expect(() => mergeStudioEvidence(input), name).toThrow(
        name === "failed"
          ? "studio-evidence-receipt-not-succeeded"
          : "studio-evidence-receipt-binding-mismatch",
      );
    }
  });

  it("hard-blocks blocked or withdrawn assets even when every receipt is successful", () => {
    for (const permission of ["blocked", "withdrawn"] as const) {
      const input = mergeInput();
      input.asset = { ...input.asset, usePermission: permission };
      expect(() => mergeStudioEvidence(input)).toThrow("studio-evidence-permission-blocked");
    }
  });

  it("derives one deterministic exact model-set capability and degrades when a required layer is missing", () => {
    expect(STUDIO_REQUIRED_EVIDENCE_LAYERS).toEqual(requiredLayers);
    const complete = mergeStudioEvidence(mergeInput());
    const reordered = mergeStudioEvidence(mergeInput({ receipts: [...receipts()].reverse() }));

    expect(complete.degradedReasons).toEqual([]);
    expect(complete.card.capability.mode).toBe("provider");
    expect(complete.card.capability.provider).toMatch(/^studio-model-set-v1:[a-f0-9]{64}$/);
    expect(complete.card.capability.label).toContain(complete.card.capability.provider!);
    expect(reordered.card.capability).toEqual(complete.card.capability);
    for (const key of ["adapterName", "modelName", "modelVersion", "checkpointDigest"] as const) {
      const changed = receipts().map((receiptValue, index) => index === 4
        ? {
            ...receiptValue,
            [key]: key === "checkpointDigest" ? "f".repeat(64) : String(receiptValue[key]) + "-changed",
          }
        : receiptValue) as ModelAnalysisReceiptV1[];
      expect(mergeStudioEvidence(mergeInput({ receipts: changed })).card.capability.provider)
        .not.toBe(complete.card.capability.provider);
    }

    const degraded = mergeStudioEvidence(mergeInput({ receipts: [receipt("technical")] }));
    expect(degraded.card.capability).toEqual(OFFLINE_STUDIO_CAPABILITY);
    expect(degraded.degradedReasons).toEqual(
      requiredLayers.slice(1).map((layer) => ({
        code: "studio-evidence-required-provider-missing",
        layer,
        message: "Required " + layer + " provider receipt is missing",
      })),
    );
  });

  it("distinguishes missing, offline, and synthetic required-provider degradation", () => {
    const degraded = mergeStudioEvidence(mergeInput({
      receipts: receipts()
        .filter((value) => value.layer !== "vision")
        .map((value) => value.layer === "scene"
          ? { ...value, providerMode: "offline-fallback", synthetic: true }
          : value),
    }));

    expect(degraded.card.capability).toEqual(OFFLINE_STUDIO_CAPABILITY);
    expect(degraded.degradedReasons).toEqual([
      {
        code: "studio-evidence-required-provider-offline",
        layer: "scene",
        message: "Required scene receipt uses offline-fallback",
      },
      {
        code: "studio-evidence-required-provider-synthetic",
        layer: "scene",
        message: "Required scene receipt is synthetic",
      },
      {
        code: "studio-evidence-required-provider-missing",
        layer: "vision",
        message: "Required vision provider receipt is missing",
      },
    ]);
    expect(StudioEvidenceDegradedReasonSchema.safeParse({
      ...degraded.degradedReasons[0],
      extra: true,
    }).success).toBe(false);
  });

  it("rejects incomplete or malformed provider identities at runtime", () => {
    const valid = {
      layer: "vision",
      adapterName: "vision-adapter",
      modelName: "vision-model",
      modelVersion: "vision-v1",
      checkpointDigest: "a".repeat(64),
    } as const;
    for (const key of Object.keys(valid)) {
      const missing = { ...valid } as Record<string, unknown>;
      delete missing[key];
      expect(() => studioProviderCapability([missing as never]), key)
        .toThrow("studio-provider-identity-invalid");
    }
    for (const key of ["adapterName", "modelName", "modelVersion"] as const) {
      expect(() => studioProviderCapability([{ ...valid, [key]: "   " }] as never))
        .toThrow("studio-provider-identity-invalid");
    }
    expect(() => studioProviderCapability([{ ...valid, layer: "identity" }] as never))
      .toThrow("studio-provider-identity-invalid");
    expect(() => studioProviderCapability([{ ...valid, checkpointDigest: "bad" }] as never))
      .toThrow("studio-provider-identity-invalid");
  });

  it("explains demand matches with source interval and evidence kind while exposing real gaps", () => {
    const merged = mergeStudioEvidence(mergeInput());
    const result = assessDemandFromEvidence({
      identity: { id: "match-a", customerId: scope.customerId, sessionId: scope.sessionId, version: 1 },
      demand: demandFixture(["18K gold"]),
      inventory: inventoryFixture(merged.card),
      cards: [merged.card],
      capability: merged.card.capability,
      evidenceByCardId: new Map([[merged.card.id, merged.evidence]]),
      degradedReasons: merged.degradedReasons,
      createdAt,
      canRenderTwoDrafts: () => ({ canRender: true, budgetExceeded: false }),
    });

    const requirement = result.assessment.matchedEvidence.find((row) => row.requirement === "18K gold");
    expect(requirement).toMatchObject({
      materialCardIds: [merged.card.id],
      sources: [expect.objectContaining({
        sourceAssetId: "asset-a",
        sourceStartMs: 0,
        sourceEndMs: 12_000,
        evidenceKind: "model-suggestion",
        reason: "Model output is a pending suggestion and requires human confirmation",
      })],
    });
    expect(result.assessment.verdict).toBe("partial");
    expect(result.assessment.unmetNeeds).toContain("待人工确认必须项证据：18K gold");
    expect(result.gapList?.gaps).toEqual(expect.arrayContaining([
      expect.objectContaining({ missingEvidence: "待人工确认必须项证据：18K gold" }),
    ]));
  });

  it("strictly rejects forged or cross-card evidence before it can make a demand ready", () => {
    const merged = mergeStudioEvidence(mergeInput());
    const pending = merged.evidence.find((item) => item.value === "18K gold")!;
    const mutations: Array<readonly [string, Record<string, unknown>]> = [
      ["unknown-field", { ...pending, injected: true }],
      ["asset", { ...pending, sourceAssetId: "asset-b" }],
      ["digest", { ...pending, sourceDigest: "b".repeat(64) }],
      ["start", { ...pending, sourceStartMs: 1 }],
      ["end", { ...pending, sourceEndMs: 11_999 }],
      ["fake-human", { ...pending, kind: "human-confirmed-fact", layer: null, receiptJobId: null }],
      ["fake-technical", { ...pending, kind: "verified-technical-fact", layer: "vision" }],
      ["missing-job", { ...pending, receiptJobId: null }],
    ];

    for (const [name, forged] of mutations) {
      const evidence = merged.evidence.map((item) => item === pending ? forged as never : item);
      expect(() => assessDemandFromEvidence({
        ...assessmentInput(merged.card),
        evidenceByCardId: new Map([[merged.card.id, evidence]]),
        degradedReasons: [],
      }), name).toThrow(/studio-evidence-record-(?:invalid|binding-mismatch|kind-mismatch)/u);
    }
  });

  it("extends PrivateContentStudio with current provider receipts and exposes a GapList", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-evidence-flow-"));
    cleanup.push(root);
    const library = new CustomerMaterialLibrary(root, () => true);
    const studio = new PrivateContentStudio(
      library,
      new OfflineFixtureUnderstandingAdapter(),
      { now: () => createdAt },
    );
    const asset = await library.importOriginal(scope.actorId, scope.customerId, {
      requestId: "provider-asset",
      bytes: Buffer.from("synthetic-provider-source"),
      mediaKind: "video",
      fileName: "provider.mp4",
      capturedAt: createdAt,
      sessionId: scope.sessionId,
      viewPermission: "allowed",
      usePermission: "allowed",
    });
    const currentSource = await library.inspectAnalysisAsset(
      scope.actorId,
      scope.customerId,
      scope.sessionId,
      asset.id,
    );
    const currentReceipts = receipts({
      sourceAssetId: asset.id,
      sourceDigest: asset.digest,
      sourcePath: currentSource.sourcePath,
    });

    const merged = await studio.analyzeProviderEvidence(scope, {
      assetId: asset.id,
      sourceStartMs: 0,
      sourceEndMs: 12_000,
      contentType: "single-product",
      receipts: currentReceipts,
      humanFacts: [],
    });
    expect(merged.card.capability.mode).toBe("provider");
    expect((await studio.inventory(scope)).capability).toEqual(merged.card.capability);
    await expect(studio.analyzeProviderEvidence(scope, {
      assetId: asset.id,
      sourceStartMs: 0,
      sourceEndMs: 12_000,
      contentType: "single-product",
      receipts: currentReceipts.map((value, index) => index === 0
        ? { ...value, sourcePath: "/private/stale-or-swapped.mp4" }
        : value),
      humanFacts: [],
    })).rejects.toThrow("studio-evidence-receipt-binding-mismatch");

    const demand = await studio.createDemand(scope, {
      contentType: "single-product",
      goal: "介绍戒指",
      targetAudience: "顾客",
      desiredEffect: "看懂素材证据",
      mustInclude: ["18K gold"],
      mustAvoid: [],
      submittedBy: "customer-self-declared",
      operationKey: "provider-demand",
    });
    const assessment = await studio.assessDemand(scope, demand.id, "provider-match");
    expect(assessment.capability).toEqual(merged.card.capability);
    expect(assessment.matchedEvidence.find((row) => row.requirement === "18K gold")?.sources?.[0])
      .toMatchObject({ sourceAssetId: asset.id, evidenceKind: "model-suggestion" });
    const discovery = await studio.discoverOpportunities(scope, {
      demandId: demand.id,
      matchAssessmentId: assessment.id,
    });
    expect(discovery.opportunities).toEqual([]);
    expect(discovery.gapList?.gaps).toEqual(expect.arrayContaining([
      expect.objectContaining({ missingEvidence: "待人工确认必须项证据：18K gold" }),
    ]));

    await library.withdraw(scope.actorId, scope.customerId, asset.id, "withdraw-provider-evidence");
    await expect(studio.analyzeProviderEvidence(scope, {
      assetId: asset.id,
      sourceStartMs: 0,
      sourceEndMs: 12_000,
      contentType: "single-product",
      receipts: currentReceipts,
      humanFacts: [],
    })).rejects.toThrow("studio-evidence-permission-blocked");
  });
});

type MergeOverrides = Partial<{
  asset: StudioMediaAsset;
  receipts: ModelAnalysisReceiptV1[];
  humanFacts: string[];
}>;

function mergeInput(overrides: MergeOverrides = {}) {
  return {
    scope,
    asset: overrides.asset ?? assetFixture(),
    sourcePath: "/private/current.mp4",
    sourceStartMs: 0,
    sourceEndMs: 12_000,
    contentType: "single-product" as const,
    receipts: overrides.receipts ?? receipts(),
    humanFacts: overrides.humanFacts ?? [],
    requiredLayers,
    createdAt,
    version: 1,
  };
}

function assetFixture(): StudioMediaAsset {
  return {
    id: "asset-a",
    importBindingId: "asset-a",
    blobId: "blob_" + sourceDigest,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    digest: sourceDigest,
    blobDigest: sourceDigest,
    mediaKind: "video",
    byteLength: 100,
    fileName: "private.mp4",
    capturedAt: createdAt,
    importedAt: createdAt,
    viewPermission: "allowed",
    usePermission: "allowed",
    captureContext: null,
  };
}

function receipts(
  binding: Partial<Pick<ModelAnalysisReceiptV1, "sourceAssetId" | "sourceDigest" | "sourcePath">> = {},
): ModelAnalysisReceiptV1[] {
  return requiredLayers.map((layer) => receipt(layer, binding));
}

function receipt(
  layer: StudioAnalysisLayer,
  binding: Partial<Pick<ModelAnalysisReceiptV1, "sourceAssetId" | "sourceDigest" | "sourcePath">> = {},
): ModelAnalysisReceiptV1 {
  const index = requiredLayers.indexOf(layer);
  const checkpoint = layer === "technical" || layer === "scene"
    ? null
    : String(index + 1).repeat(64);
  const qualitySignals: Record<string, unknown> = layer === "technical"
    ? {
        durationMs: 12_000,
        hasVideo: true,
        hasAudio: true,
        width: 1920,
        height: 1080,
        videoCodec: "h264",
        audioCodec: "aac",
        blackRatio: 0.01,
        freezeRatio: 0.02,
        blurScore: 123.4,
        untrustedMaterialClaim: "18K gold",
      }
    : layer === "scene"
      ? { scenes: [{ startMs: 0, endMs: 12_000, representativeFrameMs: 6_000, representativeFrameDigest: "f".repeat(64) }] }
      : layer === "transcript"
        ? { detectedLanguage: "zh", spans: [{ startMs: 500, endMs: 2_500, text: "设计师讲解戒指灵感" }] }
        : layer === "embedding"
          ? { embedding: [0.6, 0.8], dimension: 2 }
          : { pendingObservations: ["18K gold"] };
  const observations = layer === "technical"
    ? ["technical receipt says 18K gold"]
    : layer === "vision"
      ? ["18K gold"]
      : layer === "transcript"
        ? ["designer explains the ring"]
        : [];
  return {
    schemaVersion: 1,
    jobId: "job-" + layer,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    sourceAssetId: binding.sourceAssetId ?? "asset-a",
    sourceDigest: binding.sourceDigest ?? sourceDigest,
    sourcePath: binding.sourcePath ?? "/private/current.mp4",
    sourceStartMs: 0,
    sourceEndMs: 12_000,
    layer,
    operationKey: "operation-" + layer,
    adapterName: layer + "-adapter",
    modelName: layer + "-model",
    modelVersion: layer + "-v1",
    checkpointDigest: checkpoint,
    inputDigest: "e".repeat(64),
    promptVersion: layer + "-prompt-v1",
    observations,
    suggestedTags: layer === "vision"
      ? ["luxury"]
      : layer === "technical"
        ? ["technical-tag-must-remain-pending"]
        : [],
    qualitySignals,
    confidence: 0.8,
    warnings: [],
    startedAt: createdAt,
    completedAt: "2026-09-12T10:00:00.100Z",
    runtimeMs: 100,
    status: "succeeded",
    failure: null,
    synthetic: false,
    providerMode: "local",
  };
}

function demandFixture(mustInclude: string[]): ContentDemand {
  return {
    id: "demand-a",
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    contentType: "single-product",
    goal: "介绍戒指",
    targetAudience: "顾客",
    desiredEffect: "看懂素材证据",
    mustInclude,
    mustAvoid: [],
    submittedBy: "customer-self-declared",
    status: "current",
    createdAt,
  };
}

function inventoryFixture(card: MaterialCard): MaterialInventory {
  return {
    id: "inventory-a",
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    asOf: createdAt,
    items: [{
      materialCardId: card.id,
      sourceAssetId: card.sourceAssetId,
      selectionReason: "current authorized evidence",
    }],
    capability: card.capability,
  };
}

function assessmentInput(card: MaterialCard) {
  return {
    identity: { id: "match-forged", customerId: scope.customerId, sessionId: scope.sessionId, version: 1 },
    demand: demandFixture(["18K gold"]),
    inventory: inventoryFixture(card),
    cards: [card],
    capability: card.capability,
    createdAt,
    canRenderTwoDrafts: () => ({ canRender: true, budgetExceeded: false }),
  };
}
