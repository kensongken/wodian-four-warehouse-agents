import type { StudioCreativeBrief } from "@wearable/contracts";
import type { StudioEditorialVisualEvidence } from "@wearable/mobile-agent";

import type { StudioMusicPreferenceSummary, StudioMusicTrackRecord } from "./studio-music-library";

export type StudioMusicRecommendation = Readonly<{
  trackId: string;
  rank: number;
  score: number;
  evidenceMode: "visual-model-supported" | "technical-only";
  rankingEngine: "local-explainable-ranking-v1";
  preferenceSignal: StudioMusicPreferenceSummary;
  reasons: readonly string[];
  modelReceipts: readonly Readonly<{
    receiptJobId: string;
    modelName: string;
    modelVersion: string;
    checkpointDigest: string | null;
  }>[];
}>;

export function recommendStudioMusic(input: Readonly<{
  brief: StudioCreativeBrief;
  tracks: readonly StudioMusicTrackRecord[];
  visualEvidence: readonly StudioEditorialVisualEvidence[];
  preferences?: readonly StudioMusicPreferenceSummary[];
}>): StudioMusicRecommendation[] {
  const relevantEvidence = input.visualEvidence.filter((evidence) =>
    input.brief.selectedAssetIds.includes(evidence.sourceAssetId));
  const evidenceText = relevantEvidence.flatMap((evidence) => evidence.observations).join(" ");
  const desiredEnergy = inferDesiredEnergy(`${input.brief.goal} ${evidenceText}`);
  const evidenceMode = relevantEvidence.length > 0 ? "visual-model-supported" as const : "technical-only" as const;
  const receipts = uniqueReceipts(relevantEvidence);
  const preferenceByTrack = new Map((input.preferences ?? []).map((item) => [item.trackId, item]));
  return input.tracks.map((track) => {
    const energyDistance = Math.abs(energyIndex(track.audioProfile.energy) - energyIndex(desiredEnergy));
    const energyScore = energyDistance === 0 ? 48 : energyDistance === 1 ? 30 : 10;
    const tempoScore = Math.max(4, scoreTempo(track.audioProfile.tempoBpm, desiredEnergy) - 3);
    const waveformScore = track.audioProfile.waveform.length >= 48 ? 10 : 4;
    const receiptScore = relevantEvidence.length > 0 ? 8 : 0;
    const preferenceSignal = preferenceByTrack.get(track.assetId) ?? {
      trackId: track.assetId,
      contentType: input.brief.contentType,
      selectedCount: 0,
      clearedCount: 0,
      mixAdjustedCount: 0,
      preferenceScore: 0,
    };
    const score = Math.max(0, Math.min(100,
      energyScore + tempoScore + waveformScore + receiptScore + preferenceSignal.preferenceScore));
    const reasons = [
      `内容需要${energyLabel(desiredEnergy)}的音乐；该曲分析为${energyLabel(track.audioProfile.energy)}`,
      track.audioProfile.tempoBpm === null
        ? "未取得稳定 BPM，排序不依赖节拍速度"
        : `本地测得约 ${track.audioProfile.tempoBpm} BPM`,
      relevantEvidence.length > 0
        ? `使用 ${relevantEvidence.length} 条当前素材的视觉模型回执辅助判断内容场景`
        : "没有当前素材的真实视觉模型回执，本次只做音乐技术排序",
      preferenceSignal.selectedCount + preferenceSignal.clearedCount + preferenceSignal.mixAdjustedCount > 0
        ? `同类视频历史：选用 ${preferenceSignal.selectedCount} 次、取消 ${preferenceSignal.clearedCount} 次、调音 ${preferenceSignal.mixAdjustedCount} 次`
        : "同类视频还没有配乐选择记录，本次不增加偏好分",
      track.rights.publishUseAllowed ? "权利记录声明允许发布" : "当前权利范围仅允许预览，发布前仍需补授权",
    ];
    return {
      trackId: track.assetId, rank: 0, score, evidenceMode,
      rankingEngine: "local-explainable-ranking-v1" as const,
      reasons, modelReceipts: receipts, preferenceSignal,
    };
  }).sort((left, right) => right.score - left.score || left.trackId.localeCompare(right.trackId))
    .map((item, index) => ({ ...item, rank: index + 1 }));
}

function inferDesiredEnergy(text: string): "soft" | "balanced" | "strong" {
  if (/(快节奏|冲击|有力|活力|热烈|爆发|制作过程|工艺过程|运动|开场抓人)/u.test(text)) return "strong";
  if (/(温柔|安静|舒缓|治愈|细腻|高级|黄昏|珠宝细节|情感)/u.test(text)) return "soft";
  return "balanced";
}

function energyIndex(value: "soft" | "balanced" | "strong"): number {
  return value === "soft" ? 0 : value === "balanced" ? 1 : 2;
}

function energyLabel(value: "soft" | "balanced" | "strong"): string {
  return value === "soft" ? "柔和" : value === "strong" ? "有力" : "均衡";
}

function scoreTempo(tempo: number | null, energy: "soft" | "balanced" | "strong"): number {
  if (tempo === null) return 8;
  const [minimum, maximum] = energy === "soft" ? [55, 105] : energy === "strong" ? [100, 160] : [80, 135];
  return tempo >= minimum && tempo <= maximum ? 25 : tempo >= minimum - 20 && tempo <= maximum + 20 ? 15 : 5;
}

function uniqueReceipts(evidence: readonly StudioEditorialVisualEvidence[]) {
  return [...new Map(evidence.map((item) => [item.receiptJobId, {
    receiptJobId: item.receiptJobId,
    modelName: item.provider.modelName,
    modelVersion: item.provider.modelVersion,
    checkpointDigest: item.provider.checkpointDigest,
  }])).values()];
}
