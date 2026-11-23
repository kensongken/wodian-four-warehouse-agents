import { createHash } from "node:crypto";

import {
  MaterialCardSchema,
  StudioModelProviderIdentitySchema,
  StudioCapabilitySchema,
  StudioContentTypeSchema,
  type CaptureEnvelopeV1Input,
  type EditBlueprint,
  type MaterialCard,
  type ModelAnalysisReceiptV1,
  type ReferenceRightsDeclaration,
  type ReferenceSample,
  type ReferenceStyleAnalysis,
  type StudioCapability,
  type StudioContentType,
  type StudioCreativeBrief,
  type StudioMediaAsset,
  type StudioMusicRights,
  type StudioModelProviderIdentity,
} from "@wearable/contracts";

import type { ReferenceAnalysisAssetSnapshot } from "./customer-material-library";

export type StudioScope = Readonly<{
  actorId: string;
  customerId: string;
  sessionId: string;
}>;

export type FfprobeTechnicalMetadata = Readonly<{
  durationSeconds: number;
  hasVideo: boolean;
  hasAudio?: boolean;
  width?: number;
  height?: number;
}>;

export type OfflineAnalysisFixture = Readonly<{
  synthetic: true;
  contentType: StudioContentType;
  segment: Readonly<{ startMs: number; endMs: number }>;
  ffprobe: FfprobeTechnicalMetadata;
  observations?: readonly string[];
  merchandiseFacts?: readonly string[];
}>;

export type MediaUnderstandingRequest = Readonly<{
  scope: StudioScope;
  asset: StudioMediaAsset;
  originalBytes: Uint8Array;
  fixture: OfflineAnalysisFixture;
  capability: StudioCapability;
  createdAt: string;
}>;

export interface MediaUnderstandingAdapter {
  readonly capability: StudioCapability;
  analyze(request: MediaUnderstandingRequest): Promise<MaterialCard>;
}

export interface SpeechRecognitionAdapter {
  readonly capability: StudioCapability;
  transcribe(input: Readonly<{
    scope: StudioScope;
    asset: StudioMediaAsset;
    originalBytes: Uint8Array;
  }>): Promise<Readonly<{ transcript: string; observations: readonly string[] }>>;
}

export type StudioEditorialTranscriptSpan = Readonly<{
  sourceAssetId: string;
  sourceDigest: string;
  startMs: number;
  endMs: number;
  text: string;
}>;

export type StudioEditorialVisualEvidence = Readonly<{
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  receiptJobId: string;
  provider: StudioModelProviderIdentity;
  observations: readonly string[];
  localizedObservations?: readonly Readonly<{
    kind: "visible-person" | "visible-product" | "visible-action" | "visible-scene";
    text: string;
    startMs: number;
    endMs: number;
    confidence: number;
  }>[];
  highlightCandidates?: readonly Readonly<{
    reason: string;
    startMs: number;
    endMs: number;
    confidence: number;
  }>[];
}>;

export type StudioEditorialBeatEvidence = Readonly<{
  sourceAssetId: string;
  sourceDigest: string;
  beatTimesMs: readonly number[];
  confidence: number;
  detector: "audio-energy-onset-v1";
}>;

export type StudioEditorialMusicTrack = Readonly<{
  assetId: string;
  digest: string;
  fileName: string;
  bytes: Uint8Array;
  durationSeconds: number;
  mixPreference: Readonly<{
    trackStartMs: number;
    originalGainDb: number;
    musicGainDb: number;
  }>;
  rights: StudioMusicRights;
}>;

export interface EditorialDecisionAdapter {
  readonly capability: StudioCapability;
  createBlueprints(input: Readonly<{
    scope: StudioScope;
    brief: StudioCreativeBrief;
    cards: readonly MaterialCard[];
    transcriptSpans?: readonly StudioEditorialTranscriptSpan[];
    visualEvidence?: readonly StudioEditorialVisualEvidence[];
    beatEvidence?: readonly StudioEditorialBeatEvidence[];
    musicTrack?: Omit<StudioEditorialMusicTrack, "bytes"> | null;
    createdAt: string;
  }>): Promise<readonly EditBlueprint[]>;
}

export interface ReferenceGrammarAdapter {
  extractGrammar(input: Readonly<{
    scope: StudioScope;
    sample: ReferenceSample;
    rights: ReferenceRightsDeclaration;
    source: ReferenceAnalysisAssetSnapshot;
    receipts: readonly ModelAnalysisReceiptV1[];
  }>): Promise<ReferenceStyleAnalysis>;
}

export interface CaptureEnvelopeAdapter {
  readonly capability: StudioCapability;
  capture(input: Readonly<{
    scope: StudioScope;
    requestId: string;
  }>): Promise<CaptureEnvelopeV1Input>;
}

export interface PreferenceRankingAdapter {
  readonly capability: StudioCapability;
  rank(input: Readonly<{
    scope: StudioScope;
    candidateIds: readonly string[];
  }>): Promise<readonly string[]>;
}

export const OFFLINE_STUDIO_CAPABILITY = Object.freeze(StudioCapabilitySchema.parse({
  mode: "offline-fallback",
  label: "离线/模拟分析",
  version: 1,
  provider: null,
}));

export type { StudioModelProviderIdentity } from "@wearable/contracts";

/** Creates an order-independent identity for the exact set of model providers used. */
export function createProviderStudioCapability(
  identities: readonly StudioModelProviderIdentity[],
): StudioCapability {
  if (!Array.isArray(identities) || identities.length === 0) {
    throw new Error("studio-provider-identities-required");
  }
  let validated: StudioModelProviderIdentity[];
  try {
    validated = identities.map((identity) => StudioModelProviderIdentitySchema.parse(identity));
  } catch {
    throw new Error("studio-provider-identity-invalid");
  }
  const normalized = [...new Map(validated.map((parsed) => {
    const key = JSON.stringify(parsed);
    return [key, parsed] as const;
  })).values()].sort((left, right) => compareText(JSON.stringify(left), JSON.stringify(right)));
  const digest = stableDigest({ schemaVersion: 1, identities: normalized });
  const provider = "studio-model-set-v1:" + digest;
  return StudioCapabilitySchema.parse({
    mode: "provider",
    label: "本地模型证据集 " + provider,
    version: Number.parseInt(digest.slice(0, 12), 16) + 1,
    provider,
  });
}

export const studioProviderCapability = createProviderStudioCapability;

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

/**
 * Deterministic test/offline adapter. It accepts only a caller's explicit
 * synthetic declarations and ffprobe-shaped technical facts. It never turns a
 * merchandise declaration into a confirmed fact.
 */
export class OfflineFixtureUnderstandingAdapter implements MediaUnderstandingAdapter {
  get capability(): StudioCapability {
    return OFFLINE_STUDIO_CAPABILITY;
  }

  async analyze(request: MediaUnderstandingRequest): Promise<MaterialCard> {
    const { asset, fixture, scope } = request;
    assertScope(scope);
    assertFixture(fixture);
    assertOfflineCapability(request.capability);
    if (asset.customerId !== scope.customerId) throw new Error("customer-mismatch");
    if (!(request.originalBytes instanceof Uint8Array) || request.originalBytes.byteLength === 0) {
      throw new Error("original-bytes-required");
    }
    if (asset.byteLength !== request.originalBytes.byteLength) {
      throw new Error("original-byte-length-mismatch");
    }

    const technicalFlags = technicalFlagsFor(fixture.ffprobe);
    const segmentDurationMs = fixture.segment.endMs - fixture.segment.startMs;
    const mediaDurationMs = Math.round(fixture.ffprobe.durationSeconds * 1_000);
    const technicalUsable = asset.mediaKind === "video" &&
      fixture.ffprobe.hasVideo &&
      fixture.segment.endMs <= mediaDurationMs &&
      segmentDurationMs > 0;
    if (!technicalUsable) technicalFlags.push("ffprobe:segment-not-usable");

    const identity = stableDigest({
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      assetId: asset.id,
      assetDigest: asset.digest,
      fixture,
    });
    return MaterialCardSchema.parse({
      id: `card_${identity}`,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 1,
      sourceAssetId: asset.id,
      sourceDigest: asset.digest,
      sourceStartMs: fixture.segment.startMs,
      sourceEndMs: fixture.segment.endMs,
      contentType: fixture.contentType,
      technicalUsable,
      technicalFlags,
      observations: normalizeUnique(fixture.observations ?? []),
      confirmedFacts: [],
      pendingFacts: normalizeUnique(fixture.merchandiseFacts ?? []),
      capability: OFFLINE_STUDIO_CAPABILITY,
      createdAt: request.createdAt,
    });
  }
}

function assertOfflineCapability(value: StudioCapability): void {
  const parsed = StudioCapabilitySchema.safeParse(value);
  if (!parsed.success ||
      parsed.data.mode !== OFFLINE_STUDIO_CAPABILITY.mode ||
      parsed.data.label !== OFFLINE_STUDIO_CAPABILITY.label ||
      parsed.data.version !== OFFLINE_STUDIO_CAPABILITY.version ||
      parsed.data.provider !== OFFLINE_STUDIO_CAPABILITY.provider) {
    throw new Error("offline-capability-mismatch");
  }
}

function assertFixture(fixture: OfflineAnalysisFixture): void {
  if (!fixture || fixture.synthetic !== true) {
    throw new Error("offline-fixture-synthetic-declaration-required");
  }
  assertAllowedKeys(
    fixture as unknown as Record<string, unknown>,
    ["synthetic", "contentType", "segment", "ffprobe", "observations", "merchandiseFacts"],
    "offline-fixture-field-not-allowed",
  );
  assertAllowedKeys(
    fixture.segment as unknown as Record<string, unknown>,
    ["startMs", "endMs"],
    "offline-fixture-field-not-allowed",
  );
  assertAllowedKeys(
    fixture.ffprobe as unknown as Record<string, unknown>,
    ["durationSeconds", "hasVideo", "hasAudio", "width", "height"],
    "offline-fixture-field-not-allowed",
  );
  StudioContentTypeSchema.parse(fixture.contentType);
  assertInteger(fixture.segment?.startMs, "fixture-segment-start-invalid", true);
  assertInteger(fixture.segment?.endMs, "fixture-segment-end-invalid", false);
  if (fixture.segment.endMs <= fixture.segment.startMs) {
    throw new Error("fixture-segment-range-invalid");
  }
  if (!fixture.ffprobe || !Number.isFinite(fixture.ffprobe.durationSeconds) ||
      fixture.ffprobe.durationSeconds <= 0 || typeof fixture.ffprobe.hasVideo !== "boolean") {
    throw new Error("ffprobe-technical-metadata-required");
  }
  if (fixture.ffprobe.hasAudio !== undefined && typeof fixture.ffprobe.hasAudio !== "boolean") {
    throw new Error("ffprobe-audio-flag-invalid");
  }
  for (const dimension of [fixture.ffprobe.width, fixture.ffprobe.height]) {
    if (dimension !== undefined && (!Number.isInteger(dimension) || dimension <= 0)) {
      throw new Error("ffprobe-video-dimension-invalid");
    }
  }
  normalizeUnique(fixture.observations ?? []);
  normalizeUnique(fixture.merchandiseFacts ?? []);
}

function assertAllowedKeys(
  value: Record<string, unknown>,
  allowedKeys: readonly string[],
  error: string,
): void {
  const allowed = new Set(allowedKeys);
  if (Object.keys(value).some((key) => !allowed.has(key))) throw new Error(error);
}

function technicalFlagsFor(metadata: FfprobeTechnicalMetadata): string[] {
  const flags = [
    `ffprobe:duration=${formatNumber(metadata.durationSeconds)}s`,
    `ffprobe:video=${metadata.hasVideo ? "present" : "absent"}`,
  ];
  if (metadata.hasVideo && metadata.width !== undefined && metadata.height !== undefined) {
    flags[1] = `ffprobe:video=${metadata.width}x${metadata.height}`;
  }
  if (metadata.hasAudio !== undefined) {
    flags.push(`ffprobe:audio=${metadata.hasAudio ? "present" : "absent"}`);
  }
  return flags;
}

function normalizeUnique(values: readonly string[]): string[] {
  if (!Array.isArray(values)) throw new Error("fixture-declarations-invalid");
  const normalized = values.map((value) => {
    if (typeof value !== "string" || !value.trim()) throw new Error("fixture-declaration-empty");
    return value.trim();
  });
  if (new Set(normalized).size !== normalized.length) throw new Error("fixture-declaration-duplicate");
  return normalized;
}

function assertInteger(value: number, error: string, allowZero: boolean): void {
  if (!Number.isInteger(value) || (allowZero ? value < 0 : value <= 0)) throw new Error(error);
}

function assertScope(scope: StudioScope): void {
  for (const [value, error] of [
    [scope?.actorId, "actor-id-required"],
    [scope?.customerId, "customer-id-required"],
    [scope?.sessionId, "session-id-required"],
  ] as const) {
    if (typeof value !== "string" || !value.trim()) throw new Error(error);
  }
}

function formatNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(3)));
}

function stableDigest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}
