import { createHash } from "node:crypto";

import {
  EvidenceCardSchema,
  EvidenceDecisionSchema,
  ModelAnalysisReceiptV1Schema,
  RejectedEvidenceSuggestionSchema,
  type EvidenceCard,
  type EvidenceDecision,
  type EvidenceKind,
  type ModelAnalysisReceiptV1,
  type RejectedEvidenceSuggestion,
} from "@wearable/contracts";

import type {
  ContentLibrary,
  EvidenceDerivationCommit,
  EvidenceReviewCommitResult,
} from "./content-library";

const VISUAL_KINDS = new Set<EvidenceKind>([
  "visible-person",
  "visible-product",
  "visible-action",
  "visible-scene",
]);

const PENDING_CLAIM_REJECTIONS = {
  identity: "unsupported-identity-claim",
  material: "unsupported-material-claim",
  price: "unsupported-price-claim",
  ownership: "unsupported-ownership-claim",
  permission: "unsupported-permission-claim",
} as const;

export type EvidenceDerivationOptions = Readonly<{
  originBySourceId?: Readonly<Record<string, "new" | "historical">>;
}>;

export type ReviewEvidenceCardInput = Omit<EvidenceDecision, "id">;

export function deriveEvidenceCards(
  receiptInputs: readonly ModelAnalysisReceiptV1[],
  options: EvidenceDerivationOptions = {},
): EvidenceDerivationCommit {
  const receipts = ModelAnalysisReceiptV1Schema.array().parse(receiptInputs);
  const cards: EvidenceCard[] = [];
  const rejectedSuggestions: RejectedEvidenceSuggestion[] = [];

  for (const receipt of receipts) {
    if (receipt.status !== "succeeded" || receipt.providerMode !== "local") continue;
    if (receipt.layer === "transcript") {
      deriveTranscriptCards(receipt, options, cards);
      continue;
    }
    if (receipt.layer === "vision") {
      deriveVisualEvidence(receipt, options, cards, rejectedSuggestions);
    }
  }

  return {
    cards: uniqueById(cards),
    rejectedSuggestions: uniqueById(rejectedSuggestions),
  };
}

export async function reviewEvidenceCard(
  input: ReviewEvidenceCardInput,
  library: Pick<ContentLibrary, "commitEvidenceReview">,
): Promise<EvidenceReviewCommitResult> {
  const id = `evidence_decision_${digest(canonicalReviewInput(input)).slice(0, 24)}`;
  const decision = EvidenceDecisionSchema.parse({ id, ...input });
  return library.commitEvidenceReview(decision);
}

function deriveTranscriptCards(
  receipt: ModelAnalysisReceiptV1,
  options: EvidenceDerivationOptions,
  target: EvidenceCard[],
): void {
  if (receipt.speechState !== "speech") return;
  const signals = asRecord(receipt.qualitySignals);
  const spans = signals?.spans;
  if (!Array.isArray(spans)) throw new Error("evidence-transcript-spans-required");
  for (const [index, value] of spans.entries()) {
    const span = asRecord(value);
    const startMs = span?.startMs;
    const endMs = span?.endMs;
    const text = normalizedText(span?.text);
    if (
      typeof startMs !== "number"
      || !Number.isSafeInteger(startMs)
      || typeof endMs !== "number"
      || !Number.isSafeInteger(endMs)
      || !text
      || startMs < receipt.sourceStartMs
      || endMs > receipt.sourceEndMs
      || endMs <= startMs
    ) {
      throw new Error("evidence-transcript-span-invalid");
    }
    const noSpeechProbability = span?.noSpeechProbability;
    const confidence = typeof noSpeechProbability === "number"
      && Number.isFinite(noSpeechProbability)
      && noSpeechProbability >= 0
      && noSpeechProbability <= 1
      ? 1 - noSpeechProbability
      : receipt.confidence;
    target.push(buildCard(receipt, options, {
      discriminator: `transcript:${index}`,
      startMs,
      endMs,
      kind: "transcript-quote",
      value: text,
      confidence,
    }));
  }
}

function deriveVisualEvidence(
  receipt: ModelAnalysisReceiptV1,
  options: EvidenceDerivationOptions,
  cards: EvidenceCard[],
  rejected: RejectedEvidenceSuggestion[],
): void {
  const signals = asRecord(receipt.qualitySignals);
  const typedVisualEvidence = [
    "studio-qwen3-vl-zh-json-v2",
    "studio-qwen3-vl-zh-json-v3",
    "studio-qwen3-vl-zh-json-v4",
    "studio-qwen3-vl-zh-json-v5",
    "studio-qwen3-vl-highlight-json-v1",
    "studio-qwen3-vl-highlight-json-v2",
    "studio-qwen3-vl-highlight-json-v3",
    "studio-qwen3-vl-highlight-json-v4",
    "studio-qwen3-vl-highlight-json-v5",
    "studio-qwen3-vl-highlight-json-v6",
    "studio-qwen3-vl-highlight-json-v7",
    "studio-qwen3-vl-highlight-json-v8",
    "studio-qwen3-vl-highlight-json-v9",
    "studio-qwen3-vl-highlight-json-v11",
    "studio-qwen3-vl-highlight-pair-json-v12",
    "studio-qwen3-vl-highlight-pair-json-v13",
    "studio-qwen3-vl-highlight-pair-json-v14",
    "studio-qwen3-vl-highlight-pair-json-v15",
    "studio-qwen3-vl-highlight-pair-json-v16",
    "studio-qwen3-vl-highlight-pair-json-v17",
    "studio-qwen3-vl-highlight-pair-json-v18",
    "studio-qwen3-vl-highlight-pair-json-v19",
  ].includes(receipt.promptVersion);
  const observations = signals?.visualObservations;
  if (Array.isArray(observations)) {
    for (const [index, value] of observations.entries()) {
      const row = asRecord(value);
      const text = normalizedText(row?.text);
      const startMs = row?.startMs;
      const endMs = row?.endMs;
      const confidence = row?.confidence;
      if (
        !text
        || typeof startMs !== "number"
        || !Number.isSafeInteger(startMs)
        || typeof endMs !== "number"
        || !Number.isSafeInteger(endMs)
        || startMs < receipt.sourceStartMs
        || endMs > receipt.sourceEndMs
        || endMs <= startMs
        || typeof confidence !== "number"
        || !Number.isFinite(confidence)
        || confidence < 0
        || confidence > 1
      ) {
        throw new Error("evidence-visual-observation-invalid");
      }
      const unsupported = classifyUnsupportedText(text);
      if (unsupported) {
        rejected.push(buildRejectedSuggestion(receipt, {
          discriminator: `visual:${index}`,
          startMs,
          endMs,
          value: text,
          rejectionCode: unsupported,
        }));
        continue;
      }
      const kind = row?.kind;
      if (
        !typedVisualEvidence
        || typeof kind !== "string"
        || !VISUAL_KINDS.has(kind as EvidenceKind)
      ) {
        rejected.push(buildRejectedSuggestion(receipt, {
          discriminator: `visual:${index}`,
          startMs,
          endMs,
          value: text,
          rejectionCode: "unsupported-untyped-visual-observation",
        }));
        continue;
      }
      cards.push(buildCard(receipt, options, {
        discriminator: `visual:${index}`,
        startMs,
        endMs,
        kind: kind as EvidenceKind,
        value: text,
        confidence,
      }));
    }
  }

  const pending = signals?.pendingObservations;
  if (!Array.isArray(pending)) return;
  for (const [index, value] of pending.entries()) {
    const row = asRecord(value);
    const text = normalizedText(row?.value);
    const kind = row?.kind;
    if (!text || typeof kind !== "string") continue;
    const rejectionCode = PENDING_CLAIM_REJECTIONS[
      kind as keyof typeof PENDING_CLAIM_REJECTIONS
    ];
    rejected.push(buildRejectedSuggestion(receipt, {
      discriminator: `pending:${index}`,
      startMs: receipt.sourceStartMs,
      endMs: receipt.sourceEndMs,
      value: text,
      rejectionCode: rejectionCode ?? "unsupported-untyped-visual-observation",
    }));
  }
}

function buildCard(
  receipt: ModelAnalysisReceiptV1,
  options: EvidenceDerivationOptions,
  evidence: Readonly<{
    discriminator: string;
    startMs: number;
    endMs: number;
    kind: EvidenceKind;
    value: string;
    confidence: number;
  }>,
): EvidenceCard {
  const identity = [
    receipt.jobId,
    evidence.discriminator,
    receipt.sourceDigest,
    evidence.startMs,
    evidence.endMs,
    evidence.kind,
    evidence.value,
  ].join("\0");
  return EvidenceCardSchema.parse({
    id: `evidence_card_${digest(identity).slice(0, 24)}`,
    customerId: receipt.customerId,
    creativeSessionId: receipt.sessionId,
    runId: receipt.operationKey,
    sourceId: receipt.sourceAssetId,
    sourceDigest: receipt.sourceDigest,
    sourceStartMs: evidence.startMs,
    sourceEndMs: evidence.endMs,
    kind: evidence.kind,
    origin: options.originBySourceId?.[receipt.sourceAssetId] ?? "new",
    value: evidence.value,
    modelReceiptId: receipt.jobId,
    confidence: evidence.confidence,
    reviewStatus: "pending",
    reviewedBy: null,
    reviewedAt: null,
    correctionReason: null,
  });
}

function buildRejectedSuggestion(
  receipt: ModelAnalysisReceiptV1,
  evidence: Readonly<{
    discriminator: string;
    startMs: number;
    endMs: number;
    value: string;
    rejectionCode: RejectedEvidenceSuggestion["rejectionCode"];
  }>,
): RejectedEvidenceSuggestion {
  const identity = [
    receipt.jobId,
    evidence.discriminator,
    receipt.sourceDigest,
    evidence.startMs,
    evidence.endMs,
    evidence.rejectionCode,
    evidence.value,
  ].join("\0");
  return RejectedEvidenceSuggestionSchema.parse({
    id: `rejected_evidence_${digest(identity).slice(0, 24)}`,
    customerId: receipt.customerId,
    creativeSessionId: receipt.sessionId,
    runId: receipt.operationKey,
    sourceId: receipt.sourceAssetId,
    sourceDigest: receipt.sourceDigest,
    sourceStartMs: evidence.startMs,
    sourceEndMs: evidence.endMs,
    modelReceiptId: receipt.jobId,
    value: evidence.value,
    rejectionCode: evidence.rejectionCode,
    status: "rejected",
    createdAt: receipt.completedAt,
  });
}

function classifyUnsupportedText(
  value: string,
): RejectedEvidenceSuggestion["rejectionCode"] | null {
  if (/(属于|归.{0,12}所有|owned by|belongs to)/iu.test(value)) {
    return "unsupported-ownership-claim";
  }
  if (/(动机|目的|打算|为了|想要|intends?|motive|wants? to)/iu.test(value)) {
    return "unsupported-motive-claim";
  }
  if (/(焦虑|后悔|开心|高兴|悲伤|愤怒|紧张|情绪|anxious|regret|happy|sad|angry|emotion)/iu.test(value)) {
    return "unsupported-emotion-claim";
  }
  return null;
}

function canonicalReviewInput(input: ReviewEvidenceCardInput): string {
  return JSON.stringify({
    cardId: input.cardId,
    customerId: input.customerId,
    creativeSessionId: input.creativeSessionId,
    sourceDigest: input.sourceDigest,
    modelReceiptId: input.modelReceiptId,
    operationKey: input.operationKey,
    action: input.action,
    correctedValue: input.correctedValue,
    correctionReason: input.correctionReason,
    reviewerId: input.reviewerId,
    reviewedAt: input.reviewedAt,
  });
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function normalizedText(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.normalize("NFKC").trim();
  return normalized || null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function uniqueById<T extends { id: string }>(values: readonly T[]): T[] {
  const seen = new Set<string>();
  return values.filter((value) => {
    if (seen.has(value.id)) return false;
    seen.add(value.id);
    return true;
  });
}
