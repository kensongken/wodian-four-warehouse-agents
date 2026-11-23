import { createHash } from "node:crypto";

import {
  EditBlueprintSchema,
  StudioCapabilitySchema,
  type EditBlueprint,
  type MaterialCard,
  type StudioCaptionStyle,
  type StudioBeatSyncPlan,
  type StudioPlatformVariant,
  type StudioTransitionPlan,
} from "@wearable/contracts";

import type {
  EditorialDecisionAdapter,
  StudioEditorialBeatEvidence,
  StudioEditorialTranscriptSpan,
  StudioEditorialVisualEvidence,
  StudioScope,
} from "./studio-capabilities";
import { visualDemandMismatchReason } from "./studio-demand-rules";

export const PLATFORM_AUTOMATIC_EDITOR_CAPABILITY = Object.freeze(StudioCapabilitySchema.parse({
  mode: "provider",
  label: "本地平台自动剪辑 v1",
  version: 1,
  provider: "local-rule:platform-automatic-editor-v1",
}));

type PlatformProfile = Readonly<{
  variant: StudioPlatformVariant;
  targetDurationMs: number;
  order: readonly number[];
  direction: string;
  reason: string;
  captionStyle: StudioCaptionStyle;
  transitionPlan: StudioTransitionPlan;
  beatToleranceMs: number;
}>;

const PLATFORM_PROFILES: readonly PlatformProfile[] = Object.freeze([
  Object.freeze({
    variant: "editorial-master",
    targetDurationMs: 45_000,
    order: Object.freeze([0, 1, 2, 3]),
    direction: "platform-master-story",
    reason: "母版保留完整故事顺序",
    captionStyle: Object.freeze({
      template: "clean-outline", fontSizePx: 48, primaryColor: "#FFFFFF",
      outlineColor: "#000000", outlineWidthPx: 4, bottomMarginPx: 150, maxCharsPerLine: 14,
    }),
    transitionPlan: Object.freeze({ style: "fade-through-black", durationMs: 280 }),
    beatToleranceMs: 360,
  }),
  Object.freeze({
    variant: "xiaohongshu",
    targetDurationMs: 36_000,
    order: Object.freeze([3, 0, 1, 2]),
    direction: "platform-xiaohongshu-hook-first",
    reason: "小红书先展示结果，再补充过程",
    captionStyle: Object.freeze({
      template: "social-highlight", fontSizePx: 52, primaryColor: "#FFF4D6",
      outlineColor: "#000000", outlineWidthPx: 4, bottomMarginPx: 170, maxCharsPerLine: 12,
    }),
    transitionPlan: Object.freeze({ style: "fade-through-black", durationMs: 180 }),
    beatToleranceMs: 280,
  }),
  Object.freeze({
    variant: "douyin",
    targetDurationMs: 30_500,
    order: Object.freeze([2, 0, 3, 1]),
    direction: "platform-douyin-fast-hook",
    reason: "抖音缩短单镜头并提前冲突点",
    captionStyle: Object.freeze({
      template: "social-highlight", fontSizePx: 56, primaryColor: "#FFE066",
      outlineColor: "#000000", outlineWidthPx: 5, bottomMarginPx: 190, maxCharsPerLine: 10,
    }),
    transitionPlan: Object.freeze({ style: "cut", durationMs: 0 }),
    beatToleranceMs: 220,
  }),
  Object.freeze({
    variant: "wechat-channels",
    targetDurationMs: 42_000,
    order: Object.freeze([1, 2, 0, 3]),
    direction: "platform-wechat-context-first",
    reason: "视频号先交代背景，再展示结果",
    captionStyle: Object.freeze({
      template: "classic-bottom", fontSizePx: 48, primaryColor: "#FFFFFF",
      outlineColor: "#000000", outlineWidthPx: 3, bottomMarginPx: 140, maxCharsPerLine: 14,
    }),
    transitionPlan: Object.freeze({ style: "fade-through-black", durationMs: 240 }),
    beatToleranceMs: 320,
  }),
]);

/**
 * First production-facing automatic edit adapter for the local studio.
 *
 * It intentionally uses deterministic platform rules. A transcript/reference
 * planner such as FunClip or EditDNA can later replace this adapter without
 * bypassing the studio's authorization, lineage, review, or render gates.
 */
export class PlatformAutomaticEditorialAdapter implements EditorialDecisionAdapter {
  readonly capability = PLATFORM_AUTOMATIC_EDITOR_CAPABILITY;

  async createBlueprints(
    input: Parameters<EditorialDecisionAdapter["createBlueprints"]>[0],
  ): Promise<readonly EditBlueprint[]> {
    const cards = eligibleCards(input.cards);
    if (cards.length === 0) throw new Error("automatic-editor-renderable-material-required");
    const transcriptSpans = eligibleTranscriptSpans(input.transcriptSpans ?? [], cards);
    assertVisualCompatibility(input.brief.contentType, input.brief.goal, input.visualEvidence ?? []);
    return PLATFORM_PROFILES.map((profile) => buildPlatformBlueprint(
      input.scope,
      input.brief.id,
      input.brief.version,
      cards,
      profile,
      transcriptSpans,
      input.visualEvidence ?? [],
      input.beatEvidence ?? [],
      input.musicTrack ?? null,
      input.createdAt,
    ));
  }
}

function eligibleCards(cards: readonly MaterialCard[]): MaterialCard[] {
  return cards.filter((card) => card.technicalUsable && card.sourceEndMs > card.sourceStartMs);
}

function buildPlatformBlueprint(
  scope: StudioScope,
  briefId: string,
  version: number,
  cards: readonly MaterialCard[],
  profile: PlatformProfile,
  transcriptSpans: readonly StudioEditorialTranscriptSpan[],
  visualEvidence: readonly StudioEditorialVisualEvidence[],
  beatEvidence: readonly StudioEditorialBeatEvidence[],
  musicTrack: Parameters<EditorialDecisionAdapter["createBlueprints"]>[0]["musicTrack"],
  createdAt: string,
): EditBlueprint {
  const targetDurationMs = adaptiveTargetDuration(cards, profile);
  const ordered = cards.length === 1
    ? splitSingleSourceStory(cards[0]!, profile, targetDurationMs)
    : orderedCards(cards, profile.order);
  const durations = allocateDurations(ordered, targetDurationMs);
  let timelineStartMs = 0;
  const useCount = new Map<string, number>();
  const usedIntervals = new Set<string>();
  const plannedShots = ordered.map((card, index) => {
    const occurrence = useCount.get(card.sourceAssetId) ?? 0;
    useCount.set(card.sourceAssetId, occurrence + 1);
    const durationMs = durations[index]!;
    const availableMs = card.sourceEndMs - card.sourceStartMs;
    const maxOffsetMs = Math.max(0, availableMs - durationMs);
    const matchingSpans = orderedTranscriptSpans(
      transcriptSpans.filter((span) =>
        span.sourceAssetId === card.sourceAssetId && span.sourceDigest === card.sourceDigest),
      profile.variant,
    );
    const visualAnchors = eligibleVisualAnchors(visualEvidence, card, profile.variant);
    const visualAnchor = visualAnchors[index % Math.max(1, visualAnchors.length)];
    const transcriptAnchor = matchingSpans[index % Math.max(1, matchingSpans.length)];
    const visualAnchorOffsetMs = visualAnchor === undefined
      ? null
      : Math.max(0, Math.min(
        maxOffsetMs,
        Math.floor((visualAnchor.startMs + visualAnchor.endMs - durationMs) / 2) - card.sourceStartMs,
      ));
    const transcriptAnchorOffsetMs = transcriptAnchor === undefined
      ? null
      : Math.max(0, Math.min(maxOffsetMs, transcriptAnchor.startMs - card.sourceStartMs - Math.min(700, durationMs / 4)));
    const fallbackOffsetMs = occurrence === 0
      ? platformOffset(profile.variant, maxOffsetMs, index)
      : Math.min(maxOffsetMs, Math.floor(maxOffsetMs * ((occurrence + 1) / (occurrence + 2))));
    const offsetMs = uniqueOffset([
      visualAnchorOffsetMs,
      transcriptAnchorOffsetMs,
      fallbackOffsetMs,
      Math.floor(maxOffsetMs * ((index + 1) / (ordered.length + 1))),
      Math.max(0, maxOffsetMs - index),
    ], card.sourceAssetId, card.sourceStartMs, durationMs, maxOffsetMs, usedIntervals);
    const sourceStartMs = card.sourceStartMs + offsetMs;
    usedIntervals.add(`${card.sourceAssetId}:${sourceStartMs}:${sourceStartMs + durationMs}`);
    const shot = {
      id: `shot_${digest({ briefId, version, variant: profile.variant, index, card: card.id, sourceStartMs, durationMs })}`,
      sourceAssetId: card.sourceAssetId,
      sourceDigest: card.sourceDigest,
      sourceStartMs,
      sourceEndMs: sourceStartMs + durationMs,
      timelineStartMs,
      reason: `${profile.reason}；${storyRole(profile.variant, index, ordered.length)}`
        + (visualAnchor === undefined ? "" : "；根据本地视觉时间证据定位")
        + (visualAnchor === undefined && transcriptAnchor !== undefined ? "；根据本地中文转写定位" : ""),
    };
    timelineStartMs += durationMs;
    return shot;
  });
  const beatAlignment = alignShotsToBeats(plannedShots, beatEvidence, profile.beatToleranceMs);
  const shots = beatAlignment.shots;
  timelineStartMs = shots.reduce((total, shot) => total + shot.sourceEndMs - shot.sourceStartMs, 0);
  const repeatedSource = new Set(shots.map((shot) => shot.sourceAssetId)).size !== shots.length;
  const captionCues = buildCaptionCues(shots, transcriptSpans, briefId, profile.variant);
  return EditBlueprintSchema.parse({
    id: `blueprint_${digest({ briefId, version, variant: profile.variant, shots })}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version,
    briefId,
    platformVariant: profile.variant,
    direction: profile.direction,
    durationSeconds: timelineStartMs / 1_000,
    shots,
    captionCues,
    ...(captionCues.length > 0 ? { captionStyle: profile.captionStyle } : {}),
    transitionPlan: profile.transitionPlan,
    beatSyncPlan: beatAlignment.plan,
    ...(musicTrack ? {
      musicMixPlan: {
        mode: "mixed" as const,
        trackAssetId: musicTrack.assetId,
        trackDigest: musicTrack.digest,
        trackFileName: musicTrack.fileName,
        trackStartMs: musicTrack.mixPreference.trackStartMs,
        originalGainDb: musicTrack.mixPreference.originalGainDb,
        musicGainDb: musicTrack.mixPreference.musicGainDb,
        ducking: { enabled: true as const, threshold: 0.02, ratio: 8, attackMs: 20, releaseMs: 400 },
        loopToFit: musicTrack.durationSeconds - musicTrack.mixPreference.trackStartMs / 1_000 + 0.05 < timelineStartMs / 1_000,
        rights: musicTrack.rights,
        reason: "本地混音：保留原声；检测到原声时自动压低配乐",
      },
    } : {}),
    ...(repeatedSource ? { sourceReuseMode: "same-source-segments" as const } : {}),
    capability: PLATFORM_AUTOMATIC_EDITOR_CAPABILITY,
    publishClaimed: false,
    createdAt,
  });
}

function eligibleVisualAnchors(
  evidence: readonly StudioEditorialVisualEvidence[],
  card: MaterialCard,
  variant: StudioPlatformVariant,
): NonNullable<StudioEditorialVisualEvidence["localizedObservations"]> {
  const cardDurationMs = card.sourceEndMs - card.sourceStartMs;
  const anchors = evidence.flatMap((item) => {
    if (
      item.provider.layer !== "vision"
      || item.sourceAssetId !== card.sourceAssetId
      || item.sourceDigest !== card.sourceDigest
      || item.sourceStartMs > card.sourceStartMs
      || item.sourceEndMs < card.sourceEndMs
    ) return [];
    const sourceSignatures = new Set((item.localizedObservations ?? []).map((observation) =>
      `${observation.kind}\0${observation.text.normalize("NFKC").trim()}`));
    if ((item.localizedObservations?.length ?? 0) > 1 && sourceSignatures.size === 1) return [];
    const candidates = (item.localizedObservations ?? []).filter((observation) => {
      const durationMs = observation.endMs - observation.startMs;
      return observation.confidence >= 0.8
        && observation.startMs >= card.sourceStartMs
        && observation.endMs <= card.sourceEndMs
        && durationMs > 0
        && durationMs <= 6_000
        && durationMs < cardDurationMs * 0.8;
    });
    const distinct = new Map<string, typeof candidates[number]>();
    for (const observation of candidates) {
      const signature = `${observation.kind}\0${observation.text.normalize("NFKC").trim()}`;
      const existing = distinct.get(signature);
      if (!existing || observation.confidence > existing.confidence) distinct.set(signature, observation);
    }
    // Repeating the same description over several windows proves continuity,
    // not that one window is a better editorial choice than another.
    if (candidates.length > 1 && distinct.size === 1) return [];
    return [...distinct.values()];
  });
  return [...anchors].sort((left, right) => {
    if (variant === "douyin" || variant === "xiaohongshu") {
      return right.confidence - left.confidence || left.startMs - right.startMs;
    }
    return left.startMs - right.startMs || right.confidence - left.confidence;
  });
}

function alignShotsToBeats(
  shots: readonly Readonly<{
    id: string;
    sourceAssetId: string;
    sourceDigest: string;
    sourceStartMs: number;
    sourceEndMs: number;
    timelineStartMs: number;
    reason: string;
  }>[],
  evidence: readonly StudioEditorialBeatEvidence[],
  toleranceMs: number,
): Readonly<{ shots: EditBlueprint["shots"]; plan: StudioBeatSyncPlan }> {
  const valid = evidence.filter((item) =>
    item.detector === "audio-energy-onset-v1"
    && item.confidence >= 0.45
    && Number.isFinite(item.confidence)
    && item.beatTimesMs.length >= 3);
  const bySource = new Map(valid.map((item) => [`${item.sourceAssetId}:${item.sourceDigest}`, item]));
  let timelineStartMs = 0;
  const cutTimesMs: number[] = [];
  const adjusted = shots.map((shot) => ({ ...shot }));
  const alignedIndexes = new Set<number>();
  for (let index = 0; index < adjusted.length - 1; index += 1) {
    const shot = adjusted[index]!;
    const originalEndMs = shot.sourceEndMs;
    const item = bySource.get(`${shot.sourceAssetId}:${shot.sourceDigest}`);
    const nearest = item?.beatTimesMs
      .filter((timeMs) => timeMs > shot.sourceStartMs + 800 && Math.abs(timeMs - originalEndMs) <= toleranceMs)
      .sort((left, right) => Math.abs(left - originalEndMs) - Math.abs(right - originalEndMs))[0];
    if (nearest === undefined) continue;
    const boundaryPartner = adjusted.findIndex((candidate, candidateIndex) =>
      candidateIndex !== index
      && candidate.sourceAssetId === shot.sourceAssetId
      && candidate.sourceDigest === shot.sourceDigest
      && candidate.sourceStartMs === originalEndMs);
    if (nearest > originalEndMs && boundaryPartner >= 0) {
      const partner = adjusted[boundaryPartner]!;
      if (partner.sourceEndMs - nearest < 800) continue;
      partner.sourceStartMs = nearest;
    }
    shot.sourceEndMs = nearest;
    shot.reason += "；切点已对齐本地音频重拍";
    alignedIndexes.add(index);
  }
  const aligned = adjusted.map((shot, index) => {
    const next = { ...shot, timelineStartMs };
    timelineStartMs += shot.sourceEndMs - shot.sourceStartMs;
    if (alignedIndexes.has(index)) cutTimesMs.push(timelineStartMs);
    return next;
  });
  const detectedBeatCount = valid.reduce((total, item) => total + item.beatTimesMs.length, 0);
  const confidence = valid.length === 0
    ? 0
    : Number((valid.reduce((total, item) => total + item.confidence, 0) / valid.length).toFixed(3));
  const synced = cutTimesMs.length > 0;
  return {
    shots: aligned,
    plan: {
      mode: synced ? "beat-synced" : "rule-fallback",
      detector: "audio-energy-onset-v1",
      detectedBeatCount,
      alignedCutCount: cutTimesMs.length,
      cutTimesMs,
      toleranceMs,
      confidence,
      reason: synced
        ? `已将 ${cutTimesMs.length} 个镜头切点对齐到可靠重拍`
        : detectedBeatCount === 0 ? "未检测到可靠重拍，沿用平台固定节奏" : "重拍距离镜头切点过远，沿用平台固定节奏",
    },
  };
}

function eligibleTranscriptSpans(
  spans: readonly StudioEditorialTranscriptSpan[],
  cards: readonly MaterialCard[],
): StudioEditorialTranscriptSpan[] {
  const cardByAsset = new Map(cards.map((card) => [card.sourceAssetId, card]));
  return spans.filter((span) => {
    const card = cardByAsset.get(span.sourceAssetId);
    return card !== undefined
      && span.sourceDigest === card.sourceDigest
      && Number.isSafeInteger(span.startMs)
      && Number.isSafeInteger(span.endMs)
      && span.startMs >= card.sourceStartMs
      && span.endMs <= card.sourceEndMs
      && span.endMs > span.startMs
      && isUsableTranscriptText(span.text);
  }).map((span) => ({ ...span, text: span.text.trim().slice(0, 500) }));
}

function isUsableTranscriptText(value: string): boolean {
  const text = value.normalize("NFKC").trim();
  const compact = [...text.replace(/[\s\p{P}\p{S}]/gu, "")];
  if (compact.length < 2) return false;
  const supported = compact.filter((character) => /[\p{Script=Han}A-Za-z0-9]/u.test(character));
  if (supported.length / compact.length < 0.6) return false;
  const frequencies = new Map<string, number>();
  for (const character of compact) frequencies.set(character, (frequencies.get(character) ?? 0) + 1);
  return Math.max(...frequencies.values()) / compact.length < 0.7;
}

function assertVisualCompatibility(
  contentType: MaterialCard["contentType"],
  goal: string,
  evidence: readonly StudioEditorialVisualEvidence[],
): void {
  if (visualDemandMismatchReason(contentType, goal, evidence)) {
    throw new Error("automatic-editor-content-mismatch");
  }
}

function orderedTranscriptSpans(
  spans: readonly StudioEditorialTranscriptSpan[],
  variant: StudioPlatformVariant,
): StudioEditorialTranscriptSpan[] {
  if (variant === "editorial-master" || variant === "wechat-channels") {
    return [...spans].sort((left, right) => left.startMs - right.startMs);
  }
  return [...spans].sort((left, right) =>
    hookScore(right.text) - hookScore(left.text)
    || (variant === "douyin" ? (left.endMs - left.startMs) - (right.endMs - right.startMs) : 0)
    || left.startMs - right.startMs);
}

function hookScore(text: string): number {
  const keywords = ["结果", "重点", "自动", "完成", "可以", "功能", "解决", "最好", "最终", "现在"];
  return Math.min(40, Array.from(text).length) + keywords.reduce(
    (score, keyword) => score + (text.includes(keyword) ? 20 : 0),
    0,
  );
}

function uniqueOffset(
  candidates: readonly (number | null)[],
  sourceAssetId: string,
  sourceBaseMs: number,
  durationMs: number,
  maxOffsetMs: number,
  usedIntervals: ReadonlySet<string>,
): number {
  for (const candidate of candidates) {
    if (candidate === null) continue;
    const offset = Math.max(0, Math.min(maxOffsetMs, Math.floor(candidate)));
    const sourceStartMs = sourceBaseMs + offset;
    if (!usedIntervals.has(`${sourceAssetId}:${sourceStartMs}:${sourceStartMs + durationMs}`)) return offset;
  }
  for (let offset = 0; offset <= maxOffsetMs; offset += 1) {
    const sourceStartMs = sourceBaseMs + offset;
    if (!usedIntervals.has(`${sourceAssetId}:${sourceStartMs}:${sourceStartMs + durationMs}`)) return offset;
  }
  return 0;
}

function buildCaptionCues(
  shots: readonly Readonly<{
    id: string;
    sourceAssetId: string;
    sourceDigest: string;
    sourceStartMs: number;
    sourceEndMs: number;
    timelineStartMs: number;
  }>[],
  spans: readonly StudioEditorialTranscriptSpan[],
  briefId: string,
  variant: StudioPlatformVariant,
) {
  return shots.flatMap((shot) => spans.flatMap((span) => {
    if (span.sourceAssetId !== shot.sourceAssetId || span.sourceDigest !== shot.sourceDigest) return [];
    const sourceStartMs = Math.max(span.startMs, shot.sourceStartMs);
    const sourceEndMs = Math.min(span.endMs, shot.sourceEndMs);
    if (sourceEndMs - sourceStartMs < 300) return [];
    const timelineStartMs = shot.timelineStartMs + sourceStartMs - shot.sourceStartMs;
    const timelineEndMs = shot.timelineStartMs + sourceEndMs - shot.sourceStartMs;
    return [{
      id: `caption_${digest({ briefId, variant, shotId: shot.id, sourceStartMs, sourceEndMs, text: span.text })}`,
      sourceAssetId: shot.sourceAssetId,
      sourceDigest: shot.sourceDigest,
      sourceStartMs,
      sourceEndMs,
      timelineStartMs,
      timelineEndMs,
      text: span.text,
      reviewStatus: "model-suggestion" as const,
    }];
  }));
}

function orderedCards(cards: readonly MaterialCard[], order: readonly number[]): MaterialCard[] {
  return order.map((index) => cards[index % cards.length]!).slice(0, 4);
}

function splitSingleSourceStory(
  card: MaterialCard,
  profile: PlatformProfile,
  targetDurationMs: number,
): MaterialCard[] {
  const availableMs = card.sourceEndMs - card.sourceStartMs;
  const weights: Readonly<Record<StudioPlatformVariant, readonly [number, number]>> = {
    "editorial-master": [0.25, 0.5],
    xiaohongshu: [0.28, 0.47],
    douyin: [0.22, 0.5],
    "wechat-channels": [0.3, 0.42],
  };
  const [firstWeight, secondWeight] = weights[profile.variant];
  const firstDuration = Math.max(1, Math.floor(targetDurationMs * firstWeight));
  const secondDuration = Math.max(1, Math.floor(targetDurationMs * secondWeight));
  const thirdDuration = targetDurationMs - firstDuration - secondDuration;
  if (thirdDuration <= 0) throw new Error("automatic-editor-insufficient-duration");
  const spareMs = Math.max(0, availableMs - targetDurationMs);
  const windowRatios: Readonly<Record<StudioPlatformVariant, number>> = {
    "editorial-master": 0,
    xiaohongshu: 1,
    douyin: 0.5,
    "wechat-channels": 0.25,
  };
  const windowStartMs = card.sourceStartMs + Math.floor(spareMs * windowRatios[profile.variant]);
  const chronological = [firstDuration, secondDuration, thirdDuration].map((durationMs, index, all) => {
    const sourceStartMs = windowStartMs + all.slice(0, index).reduce((sum, value) => sum + value, 0);
    return { ...card, sourceStartMs, sourceEndMs: sourceStartMs + durationMs };
  });
  const orders: Readonly<Record<StudioPlatformVariant, readonly number[]>> = {
    "editorial-master": [0, 1, 2],
    xiaohongshu: [2, 0, 1],
    douyin: [1, 2, 0],
    "wechat-channels": [0, 1, 2],
  };
  return orders[profile.variant].map((index) => chronological[index]!);
}

function storyRole(variant: StudioPlatformVariant, index: number, shotCount: number): string {
  if (shotCount === 1) return "镜头 1";
  const labels: Readonly<Record<StudioPlatformVariant, readonly string[]>> = {
    "editorial-master": ["开场交代环境", "发展呈现主体", "收尾保留余韵"],
    xiaohongshu: ["结果钩子", "补充背景", "展开过程"],
    douyin: ["高光钩子", "快速结果", "极短背景"],
    "wechat-channels": ["背景开场", "主体发展", "完整收尾"],
  };
  return labels[variant][index] ?? `镜头 ${index + 1}`;
}

function adaptiveTargetDuration(cards: readonly MaterialCard[], profile: PlatformProfile): number {
  if (cards.length !== 1) return profile.targetDurationMs;
  const available = cards[0]!.sourceEndMs - cards[0]!.sourceStartMs;
  const ratios: Readonly<Record<StudioPlatformVariant, number>> = {
    "editorial-master": 1,
    xiaohongshu: 0.88,
    douyin: 0.72,
    "wechat-channels": 0.94,
  };
  return Math.min(available, Math.max(6_000, Math.floor(available * ratios[profile.variant])));
}

function allocateDurations(cards: readonly MaterialCard[], targetDurationMs: number): number[] {
  if (cards.length === 0) throw new Error("automatic-editor-renderable-material-required");
  const available = cards.map((card) => card.sourceEndMs - card.sourceStartMs);
  const capacity = available.reduce((sum, value) => sum + value, 0);
  if (capacity < 6_000) throw new Error("automatic-editor-insufficient-duration");
  const target = Math.min(60_000, Math.max(6_000, Math.min(targetDurationMs, capacity)));
  const durations = available.map(() => 0);
  let remaining = target;
  let open = available.map((_value, index) => index);
  while (remaining > 0 && open.length > 0) {
    const share = Math.max(1, Math.floor(remaining / open.length));
    const nextOpen: number[] = [];
    for (const index of open) {
      const room = available[index]! - durations[index]!;
      const addition = Math.min(room, share, remaining);
      durations[index]! += addition;
      remaining -= addition;
      if (durations[index]! < available[index]!) nextOpen.push(index);
    }
    open = nextOpen;
  }
  if (remaining !== 0 || durations.some((duration) => duration <= 0)) {
    throw new Error("automatic-editor-insufficient-duration");
  }
  return durations;
}

function platformOffset(
  variant: StudioPlatformVariant,
  maxOffsetMs: number,
  index: number,
): number {
  if (maxOffsetMs === 0) return 0;
  const ratios: Readonly<Record<StudioPlatformVariant, readonly number[]>> = {
    "editorial-master": [0, 0.15, 0.3, 0.45],
    xiaohongshu: [0.7, 0, 0.35, 0.15],
    douyin: [0.45, 0.75, 0.1, 0.6],
    "wechat-channels": [0.2, 0.45, 0, 0.65],
  };
  return Math.floor(maxOffsetMs * ratios[variant][index % ratios[variant].length]!);
}

function digest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}
