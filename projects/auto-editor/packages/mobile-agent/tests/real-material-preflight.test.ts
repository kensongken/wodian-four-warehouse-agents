import type {
  ContentSegment,
  ModelAnalysisReceiptV1,
  RealMaterialCapabilityReport,
  StudioAnalysisLayer,
  StoryType,
} from "@wearable/contracts";
import { describe, expect, it } from "vitest";

import {
  buildRealMaterialCapabilityReport,
  evaluateRealMaterialStories,
} from "../src/real-material-preflight";

describe("real material capability preflight", () => {
  it("marks every layer ready only for real local receipts and ready bound model artifacts", () => {
    const report = buildRealMaterialCapabilityReport({
      mediaToolsReady: true,
      receipts: [
        receipt("scene", { checkpointDigest: null }),
        receipt("transcript", { modelName: "whisper.cpp", modelVersion: "1.9.2-small-q5_1" }),
        receipt("vision", { modelName: "qwen3-vl", modelVersion: "2b" }),
        receipt("embedding", { modelName: "openclip", modelVersion: "vit-b-32" }),
      ],
      artifacts: [
        { id: "whisper.cpp-1.9.2-small-q5_1", status: "ready" },
        { id: "qwen3-vl-2b", status: "ready" },
        { id: "openclip-vit-b-32", status: "ready" },
      ],
    });

    expect(Object.fromEntries(report.layers.map((layer) => [layer.layer, layer.status])))
      .toEqual({ media: "ready", scene: "ready", transcript: "ready", vision: "ready", embedding: "ready" });
  });

  it("keeps technical media and scene ready while absent model layers are blocked", () => {
    const report = buildRealMaterialCapabilityReport({
      mediaToolsReady: true,
      receipts: [receipt("scene", { checkpointDigest: null })],
      artifacts: [],
    });

    expect(status(report, "media")).toBe("ready");
    expect(status(report, "scene")).toBe("ready");
    expect(status(report, "transcript")).toBe("blocked");
    expect(status(report, "vision")).toBe("blocked");
    expect(status(report, "embedding")).toBe("blocked");
  });

  it("blocks a synthetic receipt even when its model identity and artifact look valid", () => {
    const syntheticWhisperReceipt = receipt("transcript", {
      modelName: "whisper.cpp",
      modelVersion: "1.9.2-small-q5_1",
      synthetic: true,
    });
    const report = buildRealMaterialCapabilityReport({
      mediaToolsReady: true,
      receipts: [syntheticWhisperReceipt],
      artifacts: [{ id: "whisper.cpp-1.9.2-small-q5_1", status: "ready" }],
    });

    expect(status(report, "transcript")).toBe("blocked");
    expect(report.layers.find((layer) => layer.layer === "transcript")?.evidenceCodes)
      .toContain("synthetic-receipt-rejected");
  });

  it("blocks failed, fallback, digestless, unbound, and unapproved model receipts", () => {
    const cases: Array<[string, ModelAnalysisReceiptV1, Array<{ id: string; status: string }>]> = [
      ["failed", receipt("vision", { status: "failed-manual-review", failure: { code: "model-failed" } }), [{ id: "model-vision-v1", status: "ready" }]],
      ["fallback", receipt("vision", { providerMode: "offline-fallback" }), [{ id: "model-vision-v1", status: "ready" }]],
      ["digestless", receipt("vision", { checkpointDigest: null }), [{ id: "model-vision-v1", status: "ready" }]],
      ["unbound", receipt("vision"), [{ id: "another-model", status: "ready" }]],
      ["unapproved", receipt("vision"), [{ id: "model-vision-v1", status: "unapproved" }]],
    ];

    for (const [, modelReceipt, artifacts] of cases) {
      const report = buildRealMaterialCapabilityReport({
        mediaToolsReady: true,
        receipts: [modelReceipt],
        artifacts,
      });
      expect(status(report, "vision")).toBe("blocked");
    }
  });
});

describe("real material story readiness", () => {
  it("accepts one confirmed product with two process stages", () => {
    const result = evaluateRealMaterialStories({
      requestedStoryTypes: ["product-journey"],
      segments: [
        segment("design", { products: [{ id: "ring-01", status: "confirmed" }], processStages: ["design"] }),
        segment("finished", { products: [{ id: "ring-01", status: "confirmed" }], processStages: ["finished"] }),
      ],
      capability: capability(),
    });

    expect(result).toEqual([expect.objectContaining({
      storyType: "product-journey",
      status: "ready",
      errorCode: null,
      evidence: ["confirmed-product", "process-change"],
    })]);
  });

  it("does not turn product photos without a second stage into a product journey", () => {
    const result = evaluateRealMaterialStories({
      requestedStoryTypes: ["product-journey"],
      segments: [segment("product-photo", {
        baseTypes: ["product-image"],
        products: [{ id: "ring-01", status: "confirmed" }],
        processStages: ["finished"],
      })],
      capability: capability(),
    });

    expect(result[0]).toMatchObject({
      status: "insufficient-evidence",
      errorCode: "insufficient-process-evidence",
      missingEvidence: ["process-change"],
    });
  });

  it("does not invent an inner journey from a first-person choice without emotion change", () => {
    const result = evaluateRealMaterialStories({
      requestedStoryTypes: ["inner-journey"],
      segments: [segment("choice", {
        baseTypes: ["persona-copy"],
        transcript: "我最后决定重新做一次",
        technicalFlags: ["ffprobe:has-audio"],
      })],
      capability: capability(),
    });

    expect(result[0]).toMatchObject({
      status: "insufficient-evidence",
      errorCode: "insufficient-inner-journey-evidence",
      evidence: ["first-person-choice"],
      missingEvidence: ["emotion-change"],
    });
  });

  it("reports a real audio-bearing story as capability-blocked when transcript is unavailable", () => {
    const result = evaluateRealMaterialStories({
      requestedStoryTypes: ["inner-journey"],
      segments: [segment("audio-bearing", { technicalFlags: ["ffprobe:has-audio"] })],
      capability: capability({ transcript: "blocked" }),
    });

    expect(result[0]).toMatchObject({
      status: "blocked-by-capability",
      errorCode: "transcript-capability-unavailable",
    });
  });

  it("accepts a clear opinion with supporting evidence", () => {
    const result = evaluateRealMaterialStories({
      requestedStoryTypes: ["persona-monologue"],
      segments: [segment("opinion", {
        baseTypes: ["persona-copy", "handmade"],
        transcript: "我认为真正的手工应该留下人的痕迹",
      })],
      capability: capability(),
    });

    expect(result[0]).toMatchObject({ status: "ready", errorCode: null });
  });

  it("evaluates story types independently and preserves requested order", () => {
    const order: StoryType[] = ["persona-monologue", "product-journey", "inner-journey"];
    const result = evaluateRealMaterialStories({
      requestedStoryTypes: order,
      segments: [segment("opinion", {
        baseTypes: ["persona-copy", "handmade"],
        transcript: "我觉得手工应该保留温度",
      })],
      capability: capability(),
    });

    expect(result.map((story) => story.storyType)).toEqual(order);
    expect(result.map((story) => story.status)).toEqual([
      "ready", "insufficient-evidence", "insufficient-evidence",
    ]);
  });
});

function status(report: RealMaterialCapabilityReport, layer: string) {
  return report.layers.find((result) => result.layer === layer)?.status;
}

function capability(overrides: Partial<Record<"media" | "scene" | "transcript" | "vision" | "embedding", "ready" | "degraded" | "blocked">> = {}): RealMaterialCapabilityReport {
  const layers = (["media", "scene", "transcript", "vision", "embedding"] as const).map((layer) => ({
    layer,
    status: overrides[layer] ?? "ready" as const,
    evidenceCodes: [`${layer}-fixture`],
    message: `${layer} fixture`,
  }));
  return { schemaVersion: 1, runId: "fixture-run", generatedAt: "2026-09-22T00:00:00.000Z", layers };
}

function segment(id: string, patch: Partial<ContentSegment["tags"]> = {}): ContentSegment {
  return {
    id,
    assetId: `asset-${id}`,
    batchId: "current-batch",
    startMs: 0,
    endMs: 4_000,
    permissionState: "allowed",
    tags: {
      baseTypes: ["scene"],
      people: [],
      products: [],
      processStages: [],
      topics: [],
      emotions: [],
      transcript: "",
      technicalFlags: [],
      usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
      ...patch,
    },
  };
}

function receipt(
  layer: StudioAnalysisLayer,
  overrides: Partial<ModelAnalysisReceiptV1> = {},
): ModelAnalysisReceiptV1 {
  return {
    schemaVersion: 1,
    jobId: `job-${layer}`,
    customerId: "customer-01",
    sessionId: "session-01",
    sourceAssetId: "asset-01",
    sourceDigest: "a".repeat(64),
    sourcePath: "/private/material.mp4",
    sourceStartMs: 0,
    sourceEndMs: 4_000,
    layer,
    operationKey: `operation-${layer}`,
    adapterName: `adapter-${layer}`,
    modelName: "model",
    modelVersion: `${layer}-v1`,
    checkpointDigest: layer === "technical" || layer === "scene" ? null : "b".repeat(64),
    inputDigest: "c".repeat(64),
    promptVersion: `${layer}-prompt-v1`,
    observations: [],
    suggestedTags: [],
    qualitySignals: {},
    confidence: 0.9,
    warnings: [],
    startedAt: "2026-09-22T00:00:00.000Z",
    completedAt: "2026-09-22T00:00:01.000Z",
    runtimeMs: 1_000,
    status: "succeeded",
    failure: null,
    synthetic: false,
    providerMode: "local",
    ...overrides,
  };
}
