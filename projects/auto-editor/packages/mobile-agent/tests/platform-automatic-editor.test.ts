import type { MaterialCard, StudioCreativeBrief } from "@wearable/contracts";
import { describe, expect, it } from "vitest";

import {
  OFFLINE_STUDIO_CAPABILITY,
  PlatformAutomaticEditorialAdapter,
  type StudioScope,
} from "../src";

const scope: StudioScope = {
  actorId: "actor-a",
  customerId: "customer-a",
  sessionId: "session-a",
};

const createdAt = "2026-10-12T06:00:00.000Z";
const cards: MaterialCard[] = [0, 1, 2, 3].map((index) => ({
  id: `card-${index}`,
  customerId: scope.customerId,
  sessionId: scope.sessionId,
  version: 1,
  sourceAssetId: `asset-${index}`,
  sourceDigest: String(index + 1).repeat(64),
  sourceStartMs: 0,
  sourceEndMs: 10_500,
  contentType: "single-product",
  technicalUsable: true,
  technicalFlags: [],
  observations: [],
  confirmedFacts: [],
  pendingFacts: [],
  capability: OFFLINE_STUDIO_CAPABILITY,
  createdAt,
}));
const brief: StudioCreativeBrief = {
  id: "brief-a",
  customerId: scope.customerId,
  sessionId: scope.sessionId,
  version: 1,
  demandId: "demand-a",
  matchAssessmentId: "match-a",
  parentBriefId: null,
  feedbackId: null,
  revisionProposalId: null,
  opportunityId: "opportunity-a",
  contentType: "single-product",
  goal: "自动形成平台样片",
  selectedAssetIds: cards.map((card) => card.sourceAssetId),
  confirmedFactIds: [],
  reviewRequirements: ["人工复核"],
  status: "confirmed",
  capability: OFFLINE_STUDIO_CAPABILITY,
  createdAt,
};

describe("platform automatic editor", () => {
  it("falls back to four platform timelines when no transcript is available", async () => {
    const blueprints = await new PlatformAutomaticEditorialAdapter().createBlueprints({
      scope,
      brief,
      cards,
      createdAt,
    });

    expect(blueprints.map((blueprint) => blueprint.platformVariant)).toEqual([
      "editorial-master",
      "xiaohongshu",
      "douyin",
      "wechat-channels",
    ]);
    expect(blueprints.map((blueprint) => blueprint.transitionPlan)).toEqual([
      { style: "fade-through-black", durationMs: 280 },
      { style: "fade-through-black", durationMs: 180 },
      { style: "cut", durationMs: 0 },
      { style: "fade-through-black", durationMs: 240 },
    ]);
    expect(blueprints.every((blueprint) => blueprint.captionCues?.length === 0)).toBe(true);
    expect(blueprints.every((blueprint) => blueprint.beatSyncPlan?.mode === "rule-fallback")).toBe(true);
    expect(new Set(blueprints.map((blueprint) => JSON.stringify(blueprint.shots))).size).toBe(4);
  });

  it("aligns internal shot cuts to trusted local audio beats", async () => {
    const blueprints = await new PlatformAutomaticEditorialAdapter().createBlueprints({
      scope,
      brief,
      cards,
      beatEvidence: cards.map((card) => ({
        sourceAssetId: card.sourceAssetId,
        sourceDigest: card.sourceDigest,
        beatTimesMs: Array.from({ length: 42 }, (_value, index) => (index + 1) * 250),
        confidence: 0.91,
        detector: "audio-energy-onset-v1" as const,
      })),
      createdAt,
    });

    expect(blueprints.every((blueprint) => blueprint.beatSyncPlan?.mode === "beat-synced")).toBe(true);
    expect(blueprints.every((blueprint) => blueprint.beatSyncPlan?.alignedCutCount === blueprint.shots.length - 1)).toBe(true);
    expect(blueprints.flatMap((blueprint) => blueprint.beatSyncPlan?.cutTimesMs ?? []).every((timeMs) => timeMs > 0)).toBe(true);
    expect(blueprints.flatMap((blueprint) => blueprint.shots.slice(0, -1)).every((shot) =>
      shot.reason.includes("切点已对齐本地音频重拍"))).toBe(true);
    expect(blueprints.every((blueprint) => {
      const chronological = [...blueprint.shots].sort((left, right) => left.sourceStartMs - right.sourceStartMs);
      return chronological.every((shot, index) => index === 0
        || chronological[index - 1]!.sourceAssetId !== shot.sourceAssetId
        || chronological[index - 1]!.sourceEndMs <= shot.sourceStartMs);
    })).toBe(true);
  });

  it("uses bound transcript spans for shot anchors and reviewable caption cues", async () => {
    const blueprints = await new PlatformAutomaticEditorialAdapter().createBlueprints({
      scope,
      brief,
      cards,
      transcriptSpans: cards.flatMap((card) => [
        { sourceAssetId: card.sourceAssetId, sourceDigest: card.sourceDigest, startMs: 1_000, endMs: 2_500, text: "现在展示制作过程" },
        { sourceAssetId: card.sourceAssetId, sourceDigest: card.sourceDigest, startMs: 5_000, endMs: 7_000, text: "重点是自动完成剪辑" },
      ]),
      createdAt,
    });

    expect(blueprints.every((blueprint) => (blueprint.captionCues?.length ?? 0) > 0)).toBe(true);
    expect(blueprints.flatMap((blueprint) => blueprint.captionCues ?? []).every((cue) =>
      cue.reviewStatus === "model-suggestion")).toBe(true);
    expect(blueprints.flatMap((blueprint) => blueprint.shots).some((shot) =>
      shot.reason.includes("根据本地中文转写定位"))).toBe(true);
  });

  it("drops a gibberish transcript before it can become captions", async () => {
    const blueprints = await new PlatformAutomaticEditorialAdapter().createBlueprints({
      scope,
      brief,
      cards,
      transcriptSpans: cards.map((card) => ({
        sourceAssetId: card.sourceAssetId,
        sourceDigest: card.sourceDigest,
        startMs: 0,
        endMs: 5_000,
        text: "ლ ლ ლ ლ ლ ლ ლ",
      })),
      createdAt,
    });

    expect(blueprints.every((blueprint) => blueprint.captionCues?.length === 0)).toBe(true);
  });

  it("keeps one short source short instead of repeating it to thirty seconds", async () => {
    const [single] = cards;
    const shortCard = { ...single!, sourceEndMs: 17_903 };
    const blueprints = await new PlatformAutomaticEditorialAdapter().createBlueprints({
      scope,
      brief: { ...brief, selectedAssetIds: [shortCard.sourceAssetId] },
      cards: [shortCard],
      createdAt,
    });

    expect(blueprints.map((blueprint) => blueprint.durationSeconds)).toEqual([
      17.903, 15.754, 12.89, 16.828,
    ]);
    expect(blueprints.every((blueprint) => blueprint.shots.length === 3)).toBe(true);
    expect(blueprints.every((blueprint) => blueprint.sourceReuseMode === "same-source-segments")).toBe(true);
    expect(blueprints.every((blueprint) => {
      const chronological = [...blueprint.shots].sort((left, right) => left.sourceStartMs - right.sourceStartMs);
      return chronological.every((shot, index) => index === 0
        || chronological[index - 1]!.sourceEndMs <= shot.sourceStartMs);
    })).toBe(true);
    expect(blueprints.flatMap((blueprint) => blueprint.shots).every((shot) =>
      /开场|发展|收尾|钩子|背景|过程|结果/u.test(shot.reason))).toBe(true);
    expect(new Set(blueprints.map((blueprint) => JSON.stringify(blueprint.shots))).size).toBe(4);
  });

  it("does not change cuts when visual evidence only describes the whole source", async () => {
    const [single] = cards;
    const shortCard = { ...single!, sourceEndMs: 17_903 };
    const input = {
      scope,
      brief: { ...brief, selectedAssetIds: [shortCard.sourceAssetId] },
      cards: [shortCard],
      createdAt,
    };
    const baseline = await new PlatformAutomaticEditorialAdapter().createBlueprints(input);
    const broad = await new PlatformAutomaticEditorialAdapter().createBlueprints({
      ...input,
      visualEvidence: [{
        sourceAssetId: shortCard.sourceAssetId,
        sourceDigest: shortCard.sourceDigest,
        sourceStartMs: 0,
        sourceEndMs: 17_903,
        receiptJobId: "vision-broad",
        provider: {
          layer: "vision" as const,
          adapterName: "mlx-vlm-local",
          modelName: "qwen3-vl-2b-instruct-4bit",
          modelVersion: "local-test",
          checkpointDigest: "a".repeat(64),
        },
        observations: ["visible-scene|0|17903|黄昏飞行场景"],
        localizedObservations: [{
          kind: "visible-scene" as const,
          text: "黄昏飞行场景",
          startMs: 0,
          endMs: 17_903,
          confidence: 0.96,
        }],
      }],
    });

    expect(broad.map((blueprint) => blueprint.shots)).toEqual(
      baseline.map((blueprint) => blueprint.shots),
    );
    expect(broad.flatMap((blueprint) => blueprint.shots).every((shot) =>
      !shot.reason.includes("根据本地视觉时间证据定位"))).toBe(true);
  });

  it("changes a cut only when a trusted visual observation has a local time window", async () => {
    const [single] = cards;
    const shortCard = { ...single!, sourceEndMs: 17_903 };
    const input = {
      scope,
      brief: { ...brief, selectedAssetIds: [shortCard.sourceAssetId] },
      cards: [shortCard],
      createdAt,
    };
    const baseline = await new PlatformAutomaticEditorialAdapter().createBlueprints(input);
    const localized = await new PlatformAutomaticEditorialAdapter().createBlueprints({
      ...input,
      visualEvidence: [{
        sourceAssetId: shortCard.sourceAssetId,
        sourceDigest: shortCard.sourceDigest,
        sourceStartMs: 0,
        sourceEndMs: 17_903,
        receiptJobId: "vision-localized",
        provider: {
          layer: "vision" as const,
          adapterName: "mlx-vlm-local",
          modelName: "qwen3-vl-2b-instruct-4bit",
          modelVersion: "local-test",
          checkpointDigest: "a".repeat(64),
        },
        observations: ["visible-scene|7161|10741|机翼与落日颜色清晰"],
        localizedObservations: [{
          kind: "visible-scene" as const,
          text: "机翼与落日颜色清晰",
          startMs: 7_161,
          endMs: 10_741,
          confidence: 0.96,
        }],
      }],
    });

    expect(localized.map((blueprint) => blueprint.shots)).not.toEqual(
      baseline.map((blueprint) => blueprint.shots),
    );
    expect(localized.flatMap((blueprint) => blueprint.shots).some((shot) =>
      shot.reason.includes("根据本地视觉时间证据定位"))).toBe(true);
  });

  it("does not invent a highlight when every local window has the same visual description", async () => {
    const [single] = cards;
    const shortCard = { ...single!, sourceEndMs: 17_903 };
    const input = {
      scope,
      brief: { ...brief, selectedAssetIds: [shortCard.sourceAssetId] },
      cards: [shortCard],
      createdAt,
    };
    const baseline = await new PlatformAutomaticEditorialAdapter().createBlueprints(input);
    const repeated = await new PlatformAutomaticEditorialAdapter().createBlueprints({
      ...input,
      visualEvidence: [{
        sourceAssetId: shortCard.sourceAssetId,
        sourceDigest: shortCard.sourceDigest,
        sourceStartMs: 0,
        sourceEndMs: 17_903,
        receiptJobId: "vision-repeated",
        provider: {
          layer: "vision" as const,
          adapterName: "mlx-vlm-local",
          modelName: "qwen3-vl-2b-instruct-4bit",
          modelVersion: "local-test",
          checkpointDigest: "a".repeat(64),
        },
        observations: ["五个窗口都是相同的黄昏机翼描述"],
        localizedObservations: [
          { kind: "visible-scene" as const, text: "黄昏机翼与城市灯光", startMs: 0, endMs: 3_580, confidence: 0.95 },
          { kind: "visible-scene" as const, text: "黄昏机翼与城市灯光", startMs: 3_580, endMs: 7_160, confidence: 0.95 },
          { kind: "visible-scene" as const, text: "黄昏机翼与城市灯光", startMs: 7_160, endMs: 10_740, confidence: 0.95 },
        ],
      }],
    });

    expect(repeated.map((blueprint) => blueprint.shots)).toEqual(
      baseline.map((blueprint) => blueprint.shots),
    );
    expect(repeated.flatMap((blueprint) => blueprint.shots).every((shot) =>
      !shot.reason.includes("根据本地视觉时间证据定位"))).toBe(true);
  });

  it("blocks a product brief when trusted local vision only shows an airplane sunset", async () => {
    const [single] = cards;
    await expect(new PlatformAutomaticEditorialAdapter().createBlueprints({
      scope,
      brief: { ...brief, goal: "生成产品介绍短片", selectedAssetIds: [single!.sourceAssetId] },
      cards: [single!],
      visualEvidence: [{
        sourceAssetId: single!.sourceAssetId,
        sourceDigest: single!.sourceDigest,
        sourceStartMs: 0,
        sourceEndMs: 10_500,
        receiptJobId: "vision-receipt-a",
        provider: {
          layer: "vision",
          adapterName: "mlx-vlm-local",
          modelName: "qwen3-vl-2b-instruct-4bit",
          modelVersion: "local-test",
          checkpointDigest: "a".repeat(64),
        },
        observations: [
          "visible-scene|0|10500|飞机机翼与黄昏天空，远处有城市灯光",
        ],
      }],
      createdAt,
    })).rejects.toThrow("automatic-editor-content-mismatch");
  });
});
