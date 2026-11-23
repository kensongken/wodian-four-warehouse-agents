import { createHash } from "node:crypto";

import {
  ModelAnalysisReceiptV1Schema,
  REFERENCE_TRANSITION_CONFIDENCE_THRESHOLD,
  ReferenceRightsDeclarationSchema,
  ReferenceSampleSchema,
  ReferenceStyleAnalysisSchema,
  type ModelAnalysisReceiptV1,
  type ReferenceRightsDeclaration,
  type ReferenceSample,
  type ReferenceStyleAnalysis,
  type StudioAnalysisLayer,
} from "@wearable/contracts";

import type { ReferenceAnalysisAssetSnapshot } from "./customer-material-library";
import {
  createProviderStudioCapability,
  type ReferenceGrammarAdapter,
  type StudioScope,
} from "./studio-capabilities";

type ExtractGrammarInput = Readonly<{
  scope: StudioScope;
  sample: ReferenceSample;
  rights: ReferenceRightsDeclaration;
  source: ReferenceAnalysisAssetSnapshot;
  receipts: readonly ModelAnalysisReceiptV1[];
}>;

type SceneEvidence = Readonly<{
  id: string;
  startMs: number;
  endMs: number;
  representativeFrameMs: number;
  representativeFrameDigest: string;
}>;

type TransitionEvidence = ReferenceStyleAnalysis["transitions"][number];
type TranscriptSpan = ReferenceStyleAnalysis["transcriptSpans"][number];
type VisionObservation = Readonly<{
  id: string;
  text: string;
  startMs: number;
  endMs: number;
  confidence: number;
}>;

const REFERENCE_TRANSITION_RULE_WINDOW_MS = 350;

export type LocalReferenceGrammarAdapterOptions = Readonly<{
  now?: () => string;
}>;

/** Builds style guidance only from exact, successful, locally bound evidence receipts. */
export class LocalReferenceGrammarAdapter implements ReferenceGrammarAdapter {
  private readonly now: () => string;

  constructor(options: LocalReferenceGrammarAdapterOptions = {}) {
    this.now = options.now ?? (() => new Date().toISOString());
  }

  async extractGrammar(inputValue: ExtractGrammarInput): Promise<ReferenceStyleAnalysis> {
    const input = validateInput(inputValue);
    const receipts = validateReceipts(input);
    const sceneReceipt = receipts.get("scene");
    if (!sceneReceipt) throw new Error("reference-scene-receipt-required");

    const scenes = parseScenes(sceneReceipt);
    const transitions = parseTransitionEvidence(sceneReceipt);
    const beatMarkers = parseBeatMarkers(sceneReceipt);
    const transcriptReceipt = receipts.get("transcript");
    const transcriptSpans = transcriptReceipt ? parseTranscriptSpans(transcriptReceipt) : [];
    const visionReceipt = receipts.get("vision");
    const visionObservations = visionReceipt ? verifiedObservations(visionReceipt) : [];
    const sections = scenes.map((scene, index) => ({
      id: evidenceId("section", sceneReceipt.jobId, index),
      startMs: scene.startMs,
      endMs: scene.endMs,
      summary: `已验证场景区间 ${scene.startMs}-${scene.endMs}ms`,
    }));
    const rules = buildRules({
      sections,
      transitions,
      beatMarkers,
      transcriptSpans,
      visionObservations,
      sceneReceipt,
      transcriptReceipt,
      visionReceipt,
    });
    const warnings = degradedWarnings({
      transcriptSpans,
      beatMarkers,
      visionObservations,
      hasExecutableTransition: rules.some((rule) => rule.kind === "transition"),
      audioEvidenceTruncated: truncationFlag(sceneReceipt, "audioEvidenceTruncated"),
      transitionEvidenceTruncated: truncationFlag(sceneReceipt, "transitionEvidenceTruncated"),
    });
    const bindings = bindingsFor(input.sample, input.rights);
    const cuts = transitions
      .filter((transition) => transition.kind === "cut")
      .map((transition) => transition.atMs)
      .sort((left, right) => left - right);
    const evidenceReceipts = [...receipts.values()];
    const content = {
      bindings,
      cuts,
      sections,
      transcriptSpans,
      beatMarkers,
      transitions,
      rules,
      warnings,
      receiptIds: evidenceReceipts.map((receipt) => receipt.jobId).sort(compareText),
    };
    const contentDigest = sha256(canonicalJson(content));
    return ReferenceStyleAnalysisSchema.parse({
      id: `reference_analysis_${sha256(`${input.sample.id}\0${input.sample.version}\0${input.rights.id}\0${input.rights.version}\0${contentDigest}`)}`,
      customerId: input.scope.customerId,
      sessionId: input.scope.sessionId,
      version: 1,
      contentDigest,
      bindings,
      cuts,
      sections,
      transcriptSpans,
      beatMarkers,
      transitions,
      rules,
      warnings,
      confidence: Math.min(...evidenceReceipts.map((receipt) => receipt.confidence)),
      status: warnings.length === 0 ? "complete" : "degraded",
      capability: createProviderStudioCapability(evidenceReceipts.map((receipt) => ({
        layer: receipt.layer,
        adapterName: receipt.adapterName,
        modelName: receipt.modelName,
        modelVersion: receipt.modelVersion,
        checkpointDigest: receipt.checkpointDigest,
      }))),
      createdAt: this.now(),
    });
  }
}

function validateInput(input: ExtractGrammarInput): ExtractGrammarInput {
  if (!input || typeof input !== "object") throw new Error("reference-analysis-input-invalid");
  const sample = ReferenceSampleSchema.parse(input.sample);
  const rights = ReferenceRightsDeclarationSchema.parse(input.rights);
  const { scope, source } = input;
  if (!scope?.actorId?.trim() || !scope.customerId?.trim() || !scope.sessionId?.trim()) {
    throw new Error("reference-analysis-scope-invalid");
  }
  if (sample.status !== "ready") throw new Error("reference-sample-not-ready");
  if (sample.viewPermission !== "allowed") throw new Error("reference-sample-view-not-allowed");
  if (
    sample.customerId !== scope.customerId
    || sample.sessionId !== scope.sessionId
    || rights.customerId !== scope.customerId
    || rights.sessionId !== scope.sessionId
  ) throw new Error("reference-analysis-scope-mismatch");
  if (
    rights.sample.id !== sample.id
    || rights.sample.version !== sample.version
    || rights.sample.sourceAssetId !== sample.sourceAssetId
    || rights.sample.sourceDigest !== sample.sourceDigest
    || rights.sample.sourceKind !== sample.sourceKind
    || rights.sample.collectionMode !== sample.collectionMode
    || rights.sample.candidateScope !== sample.candidateScope
  ) throw new Error("reference-rights-sample-binding-mismatch");
  if (
    !source
    || source.authorization !== "reference-view-authorized"
    || source.asset.customerId !== scope.customerId
    || source.asset.id !== sample.sourceAssetId
    || source.asset.digest !== sample.sourceDigest
    || source.asset.viewPermission !== "allowed"
    || !source.sourcePath?.trim()
  ) throw new Error("reference-analysis-source-binding-mismatch");
  return { ...input, sample, rights };
}

function validateReceipts(input: ExtractGrammarInput): Map<StudioAnalysisLayer, ModelAnalysisReceiptV1> {
  if (!Array.isArray(input.receipts) || input.receipts.length === 0) {
    throw new Error("reference-scene-receipt-required");
  }
  const receipts = input.receipts.map((value) => ModelAnalysisReceiptV1Schema.parse(value));
  const scene = receipts.find((receipt) => receipt.layer === "scene");
  if (!scene) throw new Error("reference-scene-receipt-required");
  const sceneRows = scene.qualitySignals.scenes;
  const firstScene = Array.isArray(sceneRows) ? sceneRows[0] : undefined;
  const lastScene = Array.isArray(sceneRows) ? sceneRows[sceneRows.length - 1] : undefined;
  if (
    !isRecord(firstScene)
    || !isRecord(lastScene)
    || firstScene.startMs !== scene.sourceStartMs
    || lastScene.endMs !== scene.sourceEndMs
  ) throw new Error("reference-receipt-binding-mismatch");
  const allowed = new Set<StudioAnalysisLayer>(["scene", "transcript", "vision"]);
  const byLayer = new Map<StudioAnalysisLayer, ModelAnalysisReceiptV1>();
  for (const receipt of receipts) {
    if (!allowed.has(receipt.layer)) throw new Error("reference-receipt-layer-not-supported");
    if (byLayer.has(receipt.layer)) throw new Error("reference-receipt-layer-duplicate");
    if (receipt.status !== "succeeded") throw new Error("reference-receipt-not-succeeded");
    if (receipt.synthetic || receipt.providerMode !== "local") {
      throw new Error("reference-receipt-real-local-evidence-required");
    }
    if (
      receipt.customerId !== input.scope.customerId
      || receipt.sessionId !== input.scope.sessionId
      || receipt.sourceAssetId !== input.sample.sourceAssetId
      || receipt.sourceDigest !== input.sample.sourceDigest
      || receipt.sourcePath !== input.source.sourcePath
      || receipt.sourceStartMs !== scene.sourceStartMs
      || receipt.sourceEndMs !== scene.sourceEndMs
    ) throw new Error("reference-receipt-binding-mismatch");
    byLayer.set(receipt.layer, receipt);
  }
  return byLayer;
}

function parseScenes(receipt: ModelAnalysisReceiptV1): SceneEvidence[] {
  const value = receipt.qualitySignals.scenes;
  if (!Array.isArray(value) || value.length === 0) throw new Error("reference-scene-evidence-invalid");
  const scenes = value.map((row, index) => {
    if (!isRecord(row)) throw new Error("reference-scene-evidence-invalid");
    const startMs = safeInteger(row.startMs);
    const endMs = safeInteger(row.endMs);
    const representativeFrameMs = safeInteger(row.representativeFrameMs);
    if (
      startMs < receipt.sourceStartMs
      || endMs > receipt.sourceEndMs
      || endMs <= startMs
      || representativeFrameMs <= startMs
      || representativeFrameMs >= endMs
      || typeof row.representativeFrameDigest !== "string"
      || !/^[a-f0-9]{64}$/.test(row.representativeFrameDigest)
    ) throw new Error("reference-scene-evidence-invalid");
    return {
      id: evidenceId("scene", receipt.jobId, index),
      startMs,
      endMs,
      representativeFrameMs,
      representativeFrameDigest: row.representativeFrameDigest,
    };
  });
  if (scenes.some((scene, index) => index > 0 && scene.startMs !== scenes[index - 1]!.endMs)) {
    throw new Error("reference-scene-evidence-invalid");
  }
  return scenes;
}

function parseTransitionEvidence(receipt: ModelAnalysisReceiptV1): TransitionEvidence[] {
  const value = receipt.qualitySignals.transitionEvidence;
  if (!Array.isArray(value)) throw new Error("reference-transition-evidence-invalid");
  return value.map((row, index) => {
    if (!isRecord(row)) throw new Error("reference-transition-evidence-invalid");
    const atMs = safeInteger(row.atMs);
    if (
      atMs <= receipt.sourceStartMs
      || atMs >= receipt.sourceEndMs
      || typeof row.kind !== "string"
      || !["cut", "fade", "crossfade", "unsupported"].includes(row.kind)
      || typeof row.confidence !== "number"
      || !Number.isFinite(row.confidence)
      || row.confidence < 0
      || row.confidence > 1
    ) throw new Error("reference-transition-evidence-invalid");
    const kind = row.kind !== "unsupported" && row.confidence < REFERENCE_TRANSITION_CONFIDENCE_THRESHOLD
      ? "unsupported"
      : row.kind;
    return {
      id: evidenceId("transition", receipt.jobId, index),
      atMs,
      kind: kind as TransitionEvidence["kind"],
      confidence: row.confidence,
    };
  }).sort((left, right) => left.atMs - right.atMs);
}

function parseBeatMarkers(receipt: ModelAnalysisReceiptV1): number[] {
  const value = receipt.qualitySignals.beatMarkers;
  if (!Array.isArray(value) || value.length > 512) throw new Error("reference-audio-evidence-invalid");
  const markers = value.map(safeInteger);
  if (markers.some((marker) => marker < receipt.sourceStartMs || marker > receipt.sourceEndMs)) {
    throw new Error("reference-audio-evidence-invalid");
  }
  return [...new Set(markers)].sort((left, right) => left - right);
}

function parseTranscriptSpans(receipt: ModelAnalysisReceiptV1): TranscriptSpan[] {
  const value = receipt.qualitySignals.spans;
  if (!Array.isArray(value)) throw new Error("reference-transcript-evidence-invalid");
  return value.map((row, index) => {
    if (!isRecord(row)) throw new Error("reference-transcript-evidence-invalid");
    const startMs = safeInteger(row.startMs);
    const endMs = safeInteger(row.endMs);
    if (
      startMs < receipt.sourceStartMs
      || endMs > receipt.sourceEndMs
      || endMs <= startMs
      || typeof row.text !== "string"
      || !row.text.trim()
    ) throw new Error("reference-transcript-evidence-invalid");
    return {
      id: evidenceId("transcript", receipt.jobId, index),
      startMs,
      endMs,
      text: row.text.trim(),
    };
  });
}

function verifiedObservations(receipt: ModelAnalysisReceiptV1): VisionObservation[] {
  const value = receipt.qualitySignals.visualObservations;
  if (!Array.isArray(value)) throw new Error("reference-vision-evidence-invalid");
  return value.map((row, index) => {
    if (!isRecord(row)) throw new Error("reference-vision-evidence-invalid");
    const startMs = safeInteger(row.startMs);
    const endMs = safeInteger(row.endMs);
    if (
      typeof row.text !== "string"
      || !row.text.trim()
      || startMs < receipt.sourceStartMs
      || endMs > receipt.sourceEndMs
      || endMs <= startMs
      || typeof row.confidence !== "number"
      || !Number.isFinite(row.confidence)
      || row.confidence < 0
      || row.confidence > 1
      || row.status !== "pending-human-confirmation"
      || !receipt.observations.includes(`pending-visual-observation:${row.text.trim()}`)
    ) throw new Error("reference-vision-evidence-invalid");
    return {
      id: evidenceId("vision", receipt.jobId, index),
      text: row.text.trim(),
      startMs,
      endMs,
      confidence: row.confidence,
    };
  });
}

function buildRules(input: Readonly<{
  sections: ReferenceStyleAnalysis["sections"];
  transitions: ReferenceStyleAnalysis["transitions"];
  beatMarkers: readonly number[];
  transcriptSpans: ReferenceStyleAnalysis["transcriptSpans"];
  visionObservations: readonly VisionObservation[];
  sceneReceipt: ModelAnalysisReceiptV1;
  transcriptReceipt?: ModelAnalysisReceiptV1;
  visionReceipt?: ModelAnalysisReceiptV1;
}>): ReferenceStyleAnalysis["rules"] {
  const first = input.sections[0]!;
  const last = input.sections[input.sections.length - 1]!;
  const averageDuration = Math.round(
    input.sections.reduce((total, section) => total + section.endMs - section.startMs, 0)
      / input.sections.length,
  );
  const rules: ReferenceStyleAnalysis["rules"] = [{
    id: evidenceId("rule-rhythm", input.sceneReceipt.jobId, 0),
    kind: "rhythm",
    description: `已检测 ${input.sections.length} 个场景，平均时长 ${averageDuration}ms`,
    referenceStartMs: first.startMs,
    referenceEndMs: last.endMs,
    evidenceIds: input.sections.map((section) => section.id),
  }];
  for (const [index, transition] of input.transitions.entries()) {
    if (transition.kind === "unsupported") continue;
    const interval = transitionRuleInterval(transition.atMs, first.startMs, last.endMs);
    if (!interval) continue;
    rules.push({
      id: evidenceId("rule-transition", input.sceneReceipt.jobId, index),
      kind: "transition",
      description: `已检测 ${transition.kind} 转场，置信度 ${transition.confidence}`,
      referenceStartMs: interval.startMs,
      referenceEndMs: interval.endMs,
      evidenceIds: [transition.id],
    });
  }
  if (input.transcriptSpans.length > 0 || input.visionObservations.length > 0) {
    const evidenceIds = [
      ...input.transcriptSpans.map((span) => span.id),
      ...input.visionObservations.map((observation) => observation.id),
    ];
    rules.push({
      id: evidenceId("rule-narrative", input.transcriptReceipt?.jobId ?? input.visionReceipt!.jobId, 0),
      kind: "narrative",
      description: `已验证 ${input.transcriptSpans.length} 段转写和 ${input.visionObservations.length} 条视觉观察`,
      referenceStartMs: first.startMs,
      referenceEndMs: last.endMs,
      evidenceIds,
    });
  }
  if (input.beatMarkers.length > 0) {
    rules.push({
      id: evidenceId("rule-audio", input.sceneReceipt.jobId, 0),
      kind: "audio",
      description: `已检测 ${input.beatMarkers.length} 个本地音频能量峰值`,
      referenceStartMs: first.startMs,
      referenceEndMs: last.endMs,
      evidenceIds: input.beatMarkers.map((_marker, index) => evidenceId("beat", input.sceneReceipt.jobId, index)),
    });
  }
  return rules;
}

function transitionRuleInterval(
  atMs: number,
  sourceStartMs: number,
  sourceEndMs: number,
): Readonly<{ startMs: number; endMs: number }> | null {
  if (sourceEndMs - sourceStartMs < REFERENCE_TRANSITION_RULE_WINDOW_MS) return null;
  const centeredStartMs = atMs - Math.floor(REFERENCE_TRANSITION_RULE_WINDOW_MS / 2);
  const startMs = Math.max(
    sourceStartMs,
    Math.min(centeredStartMs, sourceEndMs - REFERENCE_TRANSITION_RULE_WINDOW_MS),
  );
  return { startMs, endMs: startMs + REFERENCE_TRANSITION_RULE_WINDOW_MS };
}

function degradedWarnings(input: Readonly<{
  transcriptSpans: readonly TranscriptSpan[];
  beatMarkers: readonly number[];
  visionObservations: readonly VisionObservation[];
  hasExecutableTransition: boolean;
  audioEvidenceTruncated: boolean;
  transitionEvidenceTruncated: boolean;
}>): string[] {
  const warnings: string[] = [];
  if (input.transcriptSpans.length === 0) warnings.push("reference-transcript-evidence-missing");
  if (input.beatMarkers.length === 0) warnings.push("reference-audio-evidence-missing");
  if (input.visionObservations.length === 0) warnings.push("reference-vision-evidence-missing");
  if (!input.hasExecutableTransition) warnings.push("reference-transition-evidence-missing");
  if (input.audioEvidenceTruncated) warnings.push("reference-audio-evidence-truncated");
  if (input.transitionEvidenceTruncated) warnings.push("reference-transition-evidence-truncated");
  return warnings;
}

function truncationFlag(receipt: ModelAnalysisReceiptV1, key: string): boolean {
  const value = receipt.qualitySignals[key];
  if (value === undefined) return false;
  if (typeof value !== "boolean") throw new Error("reference-bounded-evidence-flag-invalid");
  return value;
}

function bindingsFor(sample: ReferenceSample, rights: ReferenceRightsDeclaration) {
  return {
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
    rights: {
      id: rights.id,
      version: rights.version,
      customerId: rights.customerId,
      sessionId: rights.sessionId,
      referenceSampleId: sample.id,
      referenceSampleVersion: sample.version,
      referenceSampleDigest: sample.sourceDigest,
      sourceKind: rights.sourceKind,
      reusableElements: rights.reusableElements,
      contentDigest: rights.contentDigest,
    },
  };
}

function safeInteger(value: unknown): number {
  if (!Number.isSafeInteger(value)) throw new Error("reference-evidence-millisecond-invalid");
  return value as number;
}

function evidenceId(kind: string, jobId: string, index: number): string {
  return `${kind}_${sha256(`${jobId}\0${index}`)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort(compareText).map((key) =>
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}
