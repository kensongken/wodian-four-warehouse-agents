import type { StudioCreativeBrief } from "@wearable/contracts";
import type { StudioEditorialVisualEvidence } from "@wearable/mobile-agent";
import { describe, expect, it } from "vitest";

import { recommendStudioMusic } from "../src/studio-music-recommender";
import type { StudioMusicTrackRecord } from "../src/studio-music-library";

const brief = {
  id: "brief-a", customerId: "customer-a", sessionId: "session-a", version: 1,
  demandId: "demand-a", matchAssessmentId: "match-a", parentBriefId: null,
  feedbackId: null, revisionProposalId: null, opportunityId: "opportunity-a",
  contentType: "jewelry-making-process", goal: "用制作过程做有冲击力的开场",
  selectedAssetIds: ["asset-video"], confirmedFactIds: [], reviewRequirements: ["人工复核"],
  status: "confirmed", capability: { mode: "provider", label: "测试", version: 1, provider: "fixture" },
  createdAt: "2026-10-14T08:00:00.000Z",
} satisfies StudioCreativeBrief;

function track(assetId: string, energy: "soft" | "balanced" | "strong", tempoBpm: number): StudioMusicTrackRecord {
  return {
    assetId, digest: assetId === "strong" ? "a".repeat(64) : "b".repeat(64),
    fileName: `${assetId}.m4a`, durationSeconds: 12,
    mixPreference: { trackStartMs: 0, originalGainDb: 0, musicGainDb: -16 },
    audioProfile: { analyzer: "local-audio-energy-v1", energy, tempoBpm,
      highlightStartMs: 4_000, waveform: Array(96).fill(0.5), reason: "fixture" },
    rights: { sourceKind: "customer-owned", licenseNote: "客户自有",
      previewUseAllowed: true, publishUseAllowed: false,
      confirmedBy: "actor-a", confirmedAt: "2026-10-14T08:00:00.000Z" },
    createdAt: "2026-10-14T08:00:00.000Z",
  };
}

const evidence = [{
  sourceAssetId: "asset-video", sourceDigest: "c".repeat(64), sourceStartMs: 0, sourceEndMs: 8_000,
  receiptJobId: "vision-job-1",
  provider: { layer: "vision", adapterName: "mlx-vlm-worker", modelName: "qwen3-vl-2b-instruct-4bit",
    modelVersion: "2026-02", checkpointDigest: "d".repeat(64) },
  observations: ["珠宝制作过程", "快速工艺动作"],
}] satisfies StudioEditorialVisualEvidence[];

describe("studio music recommendation", () => {
  it("ranks a strong 120 BPM track first and binds the visual-model receipt", () => {
    const result = recommendStudioMusic({ brief, tracks: [track("soft", "soft", 72), track("strong", "strong", 120)], visualEvidence: evidence });
    expect(result[0]).toMatchObject({
      trackId: "strong", rank: 1, evidenceMode: "visual-model-supported",
      rankingEngine: "local-explainable-ranking-v1",
      modelReceipts: [{ receiptJobId: "vision-job-1", modelName: "qwen3-vl-2b-instruct-4bit" }],
    });
    expect(result[0]!.reasons.join(" ")).toContain("视觉模型回执");
  });

  it("labels the same ranking as technical-only when no visual receipt exists", () => {
    const [result] = recommendStudioMusic({ brief, tracks: [track("strong", "strong", 120)], visualEvidence: [] });
    expect(result).toMatchObject({ evidenceMode: "technical-only", modelReceipts: [] });
    expect(result!.reasons.join(" ")).toContain("只做音乐技术排序");
  });

  it("uses same-content selection feedback only as a bounded and visible ranking signal", () => {
    const result = recommendStudioMusic({
      brief,
      tracks: [track("a", "strong", 120), track("b", "strong", 120)],
      visualEvidence: evidence,
      preferences: [{
        trackId: "b", contentType: "jewelry-making-process",
        selectedCount: 4, clearedCount: 0, mixAdjustedCount: 2, preferenceScore: 12,
      }],
    });
    expect(result[0]).toMatchObject({
      trackId: "b", rank: 1,
      preferenceSignal: { selectedCount: 4, mixAdjustedCount: 2, preferenceScore: 12 },
    });
    expect(result[0]!.reasons.join(" ")).toContain("同类视频历史：选用 4 次");
  });
});
